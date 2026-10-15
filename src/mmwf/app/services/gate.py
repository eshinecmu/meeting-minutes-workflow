"""闸口决策编排；HTTP 与后续巡检共享会议锁和持久化登记。"""
from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from mmwf.domain.errors import MmwfError
from mmwf.domain.review.gate_edit import (
    canonical_json, field_diff, fingerprint, gate_content, normalise, validate_edit,
)
from mmwf.graph.interrupts import MAX_GATE_REJECTS, GATE_REJECT_KEY
from mmwf.infra.gate_store import GateStore

logger = logging.getLogger(__name__)


class DecisionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    checkpoint_id: StrictStr
    decision: Literal["approve", "edit", "reject"]
    payload: Any = None
    operator: StrictStr
    comment: StrictStr = ""


class G2Edit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    topics: list[dict] | None = None
    resolutions: list[dict] | None = None
    action_items: list[dict] | None = None


class G3Edit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    minutes_markdown: StrictStr | None = None
    minutes: dict | None = None


def validate_payload(gate, baseline, payload):
    try:
        model = G2Edit if gate == "G2" else G3Edit
        model.model_validate(payload)
    except ValidationError:
        raise MmwfError(code="E4001", detail="编辑载荷形状错误") from None
    return validate_edit(gate, baseline, payload)


class GateService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = GateStore(runtime.db_path, runtime.cfg.resolve_path(runtime.cfg.app.data_dir))

    def observe_pending(self, meeting_id, runner):
        status = runner.status()
        snapshot = runner.checkpoint()
        created_at = getattr(snapshot, "created_at", None)
        if status.pending_gate and not created_at:
            logger.warning("闸口检查点缺少创建时间，保留人工处理：%s", meeting_id)
        self.store.observe(meeting_id, status, created_at)
        return status

    def topics(self, meeting_id):
        from mmwf.domain.review.gate_projection import display_topics
        from mmwf.infra.filestore import make_filestore

        with self.runtime.meeting_lock(meeting_id), self.runtime.runner(meeting_id) as runner:
            state = runner.values()
            pending = runner.status().pending_gate
            store = make_filestore(meeting_id, self.runtime.cfg.resolve_path(self.runtime.cfg.app.data_dir))
            raw = store.read_json("transcript/segments.json") if store.exists("transcript/segments.json") else {}
            # 文件分段的 seq 与图 refs 的 seg-N 使用同一编号；不按时间近似配对。
            segments = [dict(s, segment_id=s.get("segment_id") or f"seg-{index}")
                        for index, s in enumerate(sorted(raw.get("segments", []), key=lambda s: s.get("start_ms", 0)), 1)]
            return display_topics(state, segments, pending.risk if pending else "medium",
                                  self.runtime.cfg.thresholds.asr.low_confidence)

    def diff(self, meeting_id, gate, body):
        from mmwf.domain.review.gate_projection import system_text
        from mmwf.domain.minutes.validator import diff_versions, summarize_blocks

        gate = gate.upper()
        if not isinstance(body, dict) or set(body) != {"checkpoint_id", "payload"}:
            raise MmwfError(code="E4001", detail="差异请求必须包含 checkpoint_id 和 payload")
        checkpoint = body["checkpoint_id"]
        if gate not in {"G2", "G3"} or not isinstance(checkpoint, str) or not checkpoint.strip():
            raise MmwfError(code="E4001", detail="闸口或检查点错误")
        with self.runtime.meeting_lock(meeting_id), self.runtime.runner(meeting_id) as runner:
            pending = runner.status().pending_gate
            if not pending or pending.gate != gate or pending.checkpoint_id != checkpoint:
                raise MmwfError(code="E4001", detail="闸口或检查点已过期、不匹配或已决定")
            before = runner.values()
            if gate == "G3":
                from .minutes import gate_candidate
                candidate = gate_candidate(self.runtime, meeting_id, before, body["payload"])
                after = candidate
            else:
                after = validate_payload(gate, before, body["payload"])
            segments = diff_versions(system_text(gate, before), system_text(gate, after))
            result = {"segments": segments, "summary": summarize_blocks(segments)}
            if gate == "G3":
                result.update(minutes_markdown=candidate["minutes_markdown"], issues=candidate["issues"])
            return result

    def apply_decision(self, meeting_id, gate, checkpoint_id, decision, payload,
                       operator, comment, *, auto=False, reason="human", decided_at=None):
        gate = gate.upper()
        try:
            request = DecisionInput(checkpoint_id=checkpoint_id, decision=decision,
                                    payload=payload, operator=operator, comment=comment)
        except ValidationError:
            raise MmwfError(code="E4001", detail="决策参数错误或缺少 checkpoint_id") from None
        if gate not in {"G2", "G3"} or not request.checkpoint_id.strip() or not operator.strip():
            raise MmwfError(code="E4001", detail="闸口、检查点和操作人不能为空")
        if (auto, reason) not in {(False, "human"), (True, "timeout")} or (auto and (decision != "reject" or operator != "system")):
            raise MmwfError(code="E4001", detail="自动决策只能是系统超时驳回")
        signature = {"decision": decision, "payload": normalise(payload) if decision == "edit" and isinstance(payload, dict) else (payload if decision == "edit" else {}),
                     "operator": operator, "comment": comment, "auto": auto, "reason": reason}
        with self.runtime.meeting_lock(meeting_id), self.runtime.runner(meeting_id) as runner:
            status = runner.status()
            old = self.store.get(meeting_id, gate, checkpoint_id)
            pending = status.pending_gate
            same = pending is not None and pending.gate == gate and pending.checkpoint_id == checkpoint_id
            if old:
                if canonical_json(old["request"]) != canonical_json(signature):
                    raise MmwfError(code="E4001", detail="该检查点已有其他决定")
                if same:
                    return self._resume(runner, old)
                self.store.mark_consumed(old)
                return self._response(status, old, resumed=False)
            if not same:
                raise MmwfError(code="E4001", detail="闸口或检查点已过期、不匹配或已决定")
            values = runner.values()
            if decision == "reject" and int(values.get(GATE_REJECT_KEY) or 0) >= MAX_GATE_REJECTS:
                raise MmwfError(code="E4001", detail="驳回次数已达上限，请人工介入")
            before = gate_content(gate, values)
            after = validate_payload(gate, values, payload) if decision == "edit" and gate == "G2" else deepcopy(before)
            candidate = None
            if gate == "G3" and decision != "reject":
                from .minutes import gate_candidate
                candidate = gate_candidate(self.runtime, meeting_id, values, payload if decision == "edit" else None)
                after = {"minutes": candidate["minutes_structured"], "minutes_markdown": candidate["minutes_markdown"]}
            changed = field_diff(before, after) if decision == "edit" else {}
            target = ("extract" if gate == "G2" else "assemble") if decision == "reject" else ("generate" if gate == "G2" else "export")
            transition = {"extract": "extracting", "assemble": "assembling", "generate": "generating", "export": "exporting"}[target]
            resume = {"decision": decision, "operator": operator, "comment": comment,
                      "payload": ({"minutes_markdown": after["minutes_markdown"]} if gate == "G3" else after) if decision == "edit" else {}}
            if candidate is not None:
                resume["_g3_candidate"] = candidate
            if gate == "G2" and decision != "reject":
                # 只通过服务生成内部增量，confirmed 不属于用户白名单。
                resume["_confirmed_topics"] = [dict(t, confirmed=True) for t in after["topics"]]
            record = {"meeting_id": meeting_id, "gate": gate, "checkpoint_id": checkpoint_id,
                      "request": signature, "operator": operator, "resume": resume,
                      "target": target, "transition_state": transition}
            ref = self.store.snapshot(meeting_id, checkpoint_id, {
                "before": before, "after": after, "changed_keys": sorted(changed), "changed": changed,
                "before_sha256": fingerprint(before), "after_sha256": fingerprint(after), "comment": comment})
            count = self.store.timeout_count(meeting_id) + int(auto)
            detail = {"decision": decision, "meeting_id": meeting_id, "gate": gate,
                      "checkpoint_id": checkpoint_id, "from_node": f"gate_{gate.lower()}", "to_node": target,
                      "auto": auto, "reason": reason, "changed_keys": sorted(changed), "changed": changed,
                      "before_sha256": fingerprint(before), "after_sha256": fingerprint(after), "snapshot_ref": ref,
                      "comment_present": bool(comment), "comment_sha256": fingerprint(comment),
                      "timeout_count": count, "flow_design_warning": count >= 2}
            if auto:
                from mmwf.infra.gate_store import now_iso
                self.observe_pending(meeting_id, runner)
                detail.update(approval_timeout_hours=self.runtime.cfg.thresholds.gate.approval_timeout_hours,
                              awaiting_since=self.store.awaiting_since(meeting_id),
                              decided_at=decided_at or now_iso())
            registered = self.store.register(record, detail)
            if canonical_json(registered["request"]) != canonical_json(signature):
                raise MmwfError(code="E4001", detail="该检查点已有其他决定")
            if auto and count >= 2:
                logger.warning("同一会议多次审批超时，建议检查流程设计：meeting_id=%s timeout_count=%s",
                               meeting_id, count)
            return self._resume(runner, registered)

    def _resume(self, runner, record):
        if record["gate"] == "G3" and record["request"]["decision"] != "reject":
            from .minutes import gate_candidate
            state = runner.values()
            saved = record["resume"].get("_g3_candidate")
            if saved:
                state = {**state, **saved}
            record["resume"]["_g3_candidate"] = gate_candidate(self.runtime, record["meeting_id"], state)
        before = runner.run_counts()
        status = runner.resume(record["resume"])
        self.observe_pending(record["meeting_id"], runner)
        rerun = []
        if record["request"]["decision"] == "reject":
            target = record["target"]
            if runner.run_counts().get(target, 0) <= before.get(target, 0):
                logger.warning("闸口已恢复，但未观测到驳回目标执行：meeting_id=%s target=%s checkpoint_id=%s",
                               record["meeting_id"], target, record["checkpoint_id"])
            else:
                rerun = [target]
        self.store.mark_consumed(record)
        return self._response(status, record, resumed=True, rerun=rerun)

    @staticmethod
    def _response(status, record, *, resumed, rerun=None):
        return {"resumed": resumed, "from_checkpoint": record["checkpoint_id"],
                "transition_state": record["transition_state"], "next_state": status.status,
                "status_label": status.status_label, "rerun_nodes": rerun or [],
                "pending_gate": status.pending_gate.to_dict() if status.pending_gate else None}

    def recover_pending_decisions(self):
        recovered = []
        for record in self.store.registered():
            try:
                meeting_id = record["meeting_id"]
                with self.runtime.meeting_lock(meeting_id), self.runtime.runner(meeting_id) as runner:
                    pending = runner.status().pending_gate
                    if pending and pending.gate == record["gate"] and pending.checkpoint_id == record["checkpoint_id"]:
                        recovered.append(self._resume(runner, record))
                    else:
                        # 兼容旧登记及推进后、标记前退出的窗口，不再反复打开历史 runner。
                        self.store.mark_consumed(record)
            except Exception:
                logger.exception("闸口决策恢复失败，继续处理其他会议：meeting_id=%s checkpoint_id=%s",
                                 record.get("meeting_id"), record.get("checkpoint_id"))
        return recovered
