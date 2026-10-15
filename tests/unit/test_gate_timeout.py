"""C4 单次巡检、可注入时钟、持久化时间及后台生命周期。"""
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from mmwf.api.routes._context import Runtime
from mmwf.app.services.gate_timeout import GateTimeout
from mmwf.graph.runner import GraphRunner
from mmwf.infra.audit import all_records, verify_chain
from mmwf.infra.db import connect_ctx


def records(e):
    return [r for r in all_records(e.rt.db_path) if r.action == "gate_decision"]


def waiting(e):
    return e.rt.gate_service().store.awaiting_since(e.mid)


def set_waiting(e, stamp):
    with connect_ctx(e.rt.db_path) as conn:
        conn.execute("UPDATE task_state SET awaiting_since=? WHERE task_id=?", (stamp, e.mid))


@pytest.mark.parametrize("gate,target", [("G2", "extract"), ("G3", "assemble")])
def test_exact_boundary_and_actual_reject_without_requests(gate_env, gate, target):
    e = gate_env
    if gate == "G3":
        assert e.client.post(e.base + "/gate/G2/decide", json={
            "checkpoint_id": e.pending["checkpoint_id"], "decision": "approve", "operator": "秘书",
        }).json()["ok"]
    since = datetime.fromisoformat(waiting(e))
    clock = [since + timedelta(hours=72) - timedelta(microseconds=1)]
    patrol = GateTimeout(e.rt, clock=lambda: clock[0])
    e.calls.clear()
    assert patrol.patrol_once() == []
    clock[0] += timedelta(microseconds=1)
    result = patrol.patrol_once()
    assert len(result) == 1 and result[0]["transition_state"] == {"G2": "extracting", "G3": "assembling"}[gate]
    assert result[0]["pending_gate"]["gate"] == gate
    names = [name for _, name in e.calls]
    assert names.count(target) == 1 and names.count("transcribe") == 0
    if gate == "G3":
        assert names.count("extract") == 0
    audit = records(e)[-1]
    assert audit.operator == "system" and audit.result == "success"
    assert audit.detail["decision"] == "reject" and audit.detail["auto"] is True
    assert audit.detail["reason"] == "timeout" and audit.detail["approval_timeout_hours"] == 72
    assert datetime.fromisoformat(audit.detail["awaiting_since"]) == since
    assert datetime.fromisoformat(audit.detail["decided_at"]) == clock[0]
    assert verify_chain(e.rt.db_path).ok


@pytest.mark.parametrize("missing", ["row", "time"])
def test_restart_recovers_missing_wait_from_checkpoint(gate_env, missing):
    e = gate_env
    with e.rt.runner(e.mid) as runner:
        created = runner.checkpoint().created_at
    with connect_ctx(e.rt.db_path) as conn:
        if missing == "row":
            conn.execute("DELETE FROM task_state WHERE task_id=?", (e.mid,))
        else:
            conn.execute("UPDATE task_state SET awaiting_since=NULL WHERE task_id=?", (e.mid,))
    patrol = GateTimeout(Runtime(e.rt.cfg))
    assert len(patrol.patrol_once(now=datetime.fromisoformat(created) + timedelta(hours=72))) == 1
    assert records(e)[0].detail["awaiting_since"] == created


def test_reads_and_restart_never_refresh_persisted_time(gate_env):
    e = gate_env
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    set_waiting(e, since.isoformat())
    e.client.get(e.base)
    fresh = Runtime(e.rt.cfg)
    patrol = GateTimeout(fresh, clock=lambda: since + timedelta(hours=71))
    assert patrol.patrol_once() == []
    assert waiting(e) == since.isoformat()
    assert len(patrol.patrol_once(now=since + timedelta(hours=72))) == 1
    assert records(e)[0].detail["awaiting_since"] == since.isoformat()
    assert waiting(e) != since.isoformat()  # 下一轮以新的 checkpoint 起算。


@pytest.mark.parametrize("stamp", [None, "bad-time", "2000-01-01T00:00:00"])
def test_unknown_time_keeps_gate_for_human(gate_env, monkeypatch, caplog, stamp):
    e = gate_env
    set_waiting(e, stamp)
    checkpoint = GraphRunner.checkpoint
    monkeypatch.setattr(GraphRunner, "checkpoint", lambda self: checkpoint(self)._replace(created_at=None))
    e.calls.clear()
    assert GateTimeout(e.rt).patrol_once(now=datetime(2099, 1, 1, tzinfo=timezone.utc)) == []
    assert not records(e) and not e.calls
    assert "保留人工处理" in caplog.text


def test_repeat_timeout_history_survives_restart_and_limit(gate_env, caplog):
    e = gate_env
    for count in range(1, 4):
        patrol = GateTimeout(Runtime(e.rt.cfg))
        now = datetime.fromisoformat(waiting(e)) + timedelta(hours=72)
        assert len(patrol.patrol_once(now=now)) == 1
        detail = records(e)[-1].detail
        assert detail["timeout_count"] == count
        assert detail["flow_design_warning"] is (count >= 2)
        if count == 1:
            assert "同一会议多次审批超时" not in caplog.text
        else:
            assert "同一会议多次审批超时，建议检查流程设计" in caplog.text
    e.calls.clear()
    assert patrol.patrol_once(now=datetime.fromisoformat(waiting(e)) + timedelta(hours=72)) == []
    assert len(records(e)) == 3 and not e.calls
    assert all(r.detail["decision"] == "reject" for r in records(e))
    assert "驳回次数已达上限" in caplog.text


def test_one_meeting_failure_does_not_skip_next(gate_env, monkeypatch, caplog):
    e = gate_env
    patrol = GateTimeout(e.rt)
    real_pending = patrol._pending
    monkeypatch.setattr(patrol.service.store, "meeting_ids", lambda: ["broken", e.mid])

    def pending(mid):
        if mid == "broken":
            raise RuntimeError("损坏会议")
        return real_pending(mid)

    monkeypatch.setattr(patrol, "_pending", pending)
    now = datetime.fromisoformat(waiting(e)) + timedelta(hours=72)
    assert len(patrol.patrol_once(now=now)) == 1
    assert "broken" in caplog.text and len(records(e)) == 1


def test_loop_ticks_immediately_waits_60_and_survives_exception(gate_env, monkeypatch, caplog):
    patrol = GateTimeout(gate_env.rt)
    order = []

    def tick():
        order.append("tick")
        if len(order) == 1:
            raise RuntimeError("枚举失败")

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            order.append(seconds)
            return len(order) == 4

    monkeypatch.setattr(patrol, "patrol_once", tick)
    patrol.patrol_loop(Stop())
    assert order == ["tick", 60, "tick", 60]
    assert "下一节拍继续" in caplog.text
    stopped = Event()
    stopped.set()
    patrol.patrol_loop(stopped)
    assert len(order) == 4


@pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_rejected(gate_env, hours):
    cfg = gate_env.rt.cfg.model_copy(deep=True)
    object.__setattr__(cfg.thresholds.gate, "approval_timeout_hours", hours)
    with pytest.raises(ValueError, match="必须为正数"):
        GateTimeout(Runtime(cfg))


def test_lifespan_shares_runtime_and_joins_thread(gate_env, monkeypatch):
    from fastapi.testclient import TestClient
    from mmwf.main import create_app
    from threading import current_thread

    e = gate_env
    app = create_app(e.rt.cfg)
    entered = Event()
    seen = []

    def loop(self, stop):
        seen.append((self.runtime, current_thread(), stop))
        entered.set()
        stop.wait()  # 实际 Event 被 shutdown 唤醒，无定时睡眠。

    monkeypatch.setattr(GateTimeout, "patrol_loop", loop)
    with TestClient(app):
        assert entered.wait(5)
        assert seen[0][0] is app.state.runtime
        assert seen[0][1].is_alive() and not seen[0][2].is_set()
    assert seen[0][2].is_set() and not seen[0][1].is_alive()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_candidate_recheck_cannot_decide_next_checkpoint(gate_env, monkeypatch, decision):
    e = gate_env
    now = datetime.now(timezone.utc)
    set_waiting(e, (now - timedelta(hours=72)).isoformat())
    patrol = GateTimeout(e.rt, clock=lambda: now)
    original = patrol._pending
    calls = []

    def pending(mid):
        calls.append(mid)
        if len(calls) == 2:
            # 在候选发现与锁内复读之间模拟已获胜的人工决定。
            e.rt.gate_service().apply_decision(
                mid, "G2", e.pending["checkpoint_id"], decision, None, "秘书", "")
        return original(mid)

    monkeypatch.setattr(patrol, "_pending", pending)
    assert patrol.patrol_once() == []
    assert len(records(e)) == 1 and records(e)[0].detail["auto"] is False
    with e.rt.runner(e.mid) as runner:
        assert runner.status().pending_gate.checkpoint_id != e.pending["checkpoint_id"]


def test_expiry_compares_utc_instants(gate_env):
    e = gate_env
    since = datetime(2000, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    set_waiting(e, since.isoformat())
    now = datetime(2000, 1, 4, tzinfo=timezone.utc)
    assert len(GateTimeout(e.rt, clock=lambda: now).patrol_once()) == 1
