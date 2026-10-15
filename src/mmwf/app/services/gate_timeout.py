"""单后端闸口超时巡检；时间可注入，单次巡检不等待。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math

logger = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("等待时间必须含 UTC 时区")
    return stamp.astimezone(timezone.utc)


class GateTimeout:
    def __init__(self, runtime, *, clock=utc_now):
        self.runtime = runtime
        self.clock = clock
        self.service = runtime.gate_service()
        hours = runtime.cfg.thresholds.gate.approval_timeout_hours
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("approval_timeout_hours 必须为正数")
        self.timeout = timedelta(hours=hours)

    def _pending(self, meeting_id):
        # runner / SQLite 连接只在当前线程创建并释放，读取也在共享会议锁内。
        with self.runtime.runner(meeting_id) as runner:
            if not runner.status().pending_gate:
                return None
            status = self.service.observe_pending(meeting_id, runner)
        if not status.pending_gate:
            return None
        since = self.service.store.awaiting_since(meeting_id)
        if since is None:
            logger.warning("无法确定闸口等待时间，保留人工处理：meeting_id=%s", meeting_id)
            return None
        try:
            stamp = as_utc(since)
        except (ValueError, TypeError, AttributeError):
            logger.warning("闸口等待时间无效，保留人工处理：meeting_id=%s", meeting_id)
            return None
        return status.pending_gate, stamp

    def patrol_once(self, *, now=None):
        """发现候选后重新加锁复读，旧候选不能决定新一轮闸口。"""
        now = as_utc(now if now is not None else self.clock())
        results = []
        for meeting_id in self.service.store.meeting_ids():
            try:
                with self.runtime.meeting_lock(meeting_id):
                    candidate = self._pending(meeting_id)
                if candidate is None or now - candidate[1] < self.timeout:
                    continue
                with self.runtime.meeting_lock(meeting_id):
                    current = self._pending(meeting_id)
                    if current is None:
                        continue
                    pending, since = current
                    old_pending, old_since = candidate
                    if (pending.gate, pending.checkpoint_id, since) != (old_pending.gate, old_pending.checkpoint_id, old_since):
                        continue
                    if now - since < self.timeout or self.service.store.get(meeting_id, pending.gate, pending.checkpoint_id):
                        continue
                    results.append(self.service.apply_decision(
                        meeting_id, pending.gate, pending.checkpoint_id, "reject", None,
                        "system", "", auto=True, reason="timeout",
                        decided_at=now.isoformat().replace("+00:00", "Z")))
            except Exception:
                logger.exception("闸口超时巡检失败，继续处理其他会议：meeting_id=%s", meeting_id)
        return results

    def patrol_loop(self, stop_event):
        # 首次立即巡检；Event.wait 可在退出时唤醒，不用 sleep 拖延关停。
        while not stop_event.is_set():
            try:
                self.patrol_once()
            except Exception:
                logger.exception("闸口超时巡检失败，下一节拍继续")
            if stop_event.wait(60):
                break
