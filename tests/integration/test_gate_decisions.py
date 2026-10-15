"""C3 实际节点计数、防重竞争、同事务审计及有限恢复。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import pytest

from mmwf.infra.audit import all_records, verify_chain, AuditLogger
from mmwf.infra.db import connect_ctx
from mmwf.app.services.gate import GateService


def send(e, gate, ckpt, decision="approve", payload=None, comment=""):
    return e.client.post(e.base + f"/gate/{gate}/decide", json={"checkpoint_id": ckpt, "decision": decision,
             "operator": "秘书", "payload": payload, "comment": comment}).json()


def audits(e):
    return [r for r in all_records(e.rt.db_path) if r.action == "gate_decision"]


@pytest.mark.parametrize("gate", ["G2", "G3"])
@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
def test_real_callbacks(gate_env, gate, decision):
    e = gate_env
    pending = e.pending
    if gate == "G3":
        pending = send(e, "G2", pending["checkpoint_id"])["data"]["pending_gate"]
    payload = None
    if decision == "edit":
        if gate == "G2":
            items = deepcopy(pending["payload"]["topics"])
            items[0]["title"] = "人工修正议题"
            payload = {"topics": items}
        else:
            payload = {"minutes": {"topics": [{"name": "人工修正预算议题"}, {}]}}
    e.calls.clear()
    body = send(e, gate, pending["checkpoint_id"], decision, payload)
    assert body["ok"], body
    names = [n for mid, n in e.calls]
    assert names.count("transcribe") == 0
    assert names.count("extract") == int(gate == "G2" and decision == "reject")
    if gate == "G3":
        assert names.count("assemble") == int(decision == "reject")
    elif decision != "reject":
        assert names.count("assemble") == 1
    assert body["data"]["rerun_nodes"] == (["extract" if gate == "G2" else "assemble"] if decision == "reject" else [])
    with e.rt.runner(e.mid) as runner:
        values = runner.values()
    if decision == "edit":
        assert "人工修正" in values["minutes_markdown"]
    if gate == "G2" and decision != "reject":
        assert all(t["confirmed"] is True for t in values["topics"])
    if gate == "G2" and decision == "reject":
        assert all(t.get("confirmed", False) is False for t in values["topics"])


def test_concurrent_duplicates_and_stale_g2(gate_env):
    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(e, "G2", ckpt), range(2)))
    assert all(r["ok"] for r in results), results
    assert sorted(r["data"]["resumed"] for r in results) == [False, True]
    assert len(audits(e)) == 1
    assert verify_chain(e.rt.db_path).ok
    current = results[0]["data"]["pending_gate"]
    conflict = send(e, "G2", ckpt, "reject")
    assert conflict["code"] == "E4001"
    assert e.client.get(e.base).json()["data"]["run"]["pending_gate"] == current
    assert send(e, "G2", "never-registered")["code"] == "E4001"


def test_old_reject_cannot_decide_new_g2(gate_env):
    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    first = send(e, "G2", ckpt, "reject")
    e.calls.clear()
    repeat = send(e, "G2", ckpt, "reject")
    assert repeat["ok"] and repeat["data"]["resumed"] is False
    assert repeat["data"]["pending_gate"] == first["data"]["pending_gate"]
    assert not e.calls and len(audits(e)) == 1


def test_registered_not_resumed_recovery(gate_env, monkeypatch):
    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    original = GateService._resume

    def fail(*args):
        raise RuntimeError("故障：登记完成但尚未 resume")

    monkeypatch.setattr(GateService, "_resume", fail)
    body = send(e, "G2", ckpt)
    assert body["ok"] is False
    assert len(audits(e)) == 1
    assert e.client.get(e.base).json()["data"]["run"]["pending_gate"]["checkpoint_id"] == ckpt
    monkeypatch.setattr(GateService, "_resume", original)
    # 重建 Runtime 模拟重启；检查点与登记来自磁盘。
    from mmwf.api.routes._context import Runtime
    fresh = Runtime(e.rt.cfg)
    recovered = fresh.gate_service().recover_pending_decisions()
    assert len(recovered) == 1 and recovered[0]["next_state"] == "G3_awaiting"
    e.calls.clear()
    assert fresh.gate_service().recover_pending_decisions() == []
    assert not e.calls and len(audits(e)) == 1
    assert send(e, "G2", ckpt)["data"]["resumed"] is False


def test_audit_failure_rolls_back_registration_leaves_harmless_snapshot(gate_env, monkeypatch):
    e = gate_env
    original = AuditLogger.log

    def fail(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args[1] == "gate_decision":
            raise RuntimeError("故障：审计已 INSERT，提交前失败")
        return result

    monkeypatch.setattr(AuditLogger, "log", fail)
    e.calls.clear()
    assert send(e, "G2", e.pending["checkpoint_id"])["ok"] is False
    assert e.rt.gate_service().store.registered() == []
    assert not audits(e) and not e.calls
    snapshots = list((e.rt.cfg.app.data_dir / e.mid / "gate").rglob("snapshot.json"))
    assert len(snapshots) == 1
    assert e.rt.gate_service().recover_pending_decisions() == []
    monkeypatch.setattr(AuditLogger, "log", original)
    assert send(e, "G2", e.pending["checkpoint_id"])["ok"]
    assert len(audits(e)) == 1


def test_exact_detail_snapshot_and_export(gate_env):
    e = gate_env
    items = deepcopy(e.pending["payload"]["topics"])
    items[0]["title"] = "涉密人工修改文本"
    comment = "涉密自由备注"
    assert send(e, "G2", e.pending["checkpoint_id"], "edit", {"topics": items}, comment)["ok"]
    rec = audits(e)[0]
    detail = rec.detail
    assert set(detail) == {"decision", "meeting_id", "gate", "checkpoint_id", "from_node", "to_node", "auto", "reason",
                           "changed_keys", "changed", "before_sha256", "after_sha256", "snapshot_ref", "comment_present",
                           "comment_sha256", "timeout_count", "flow_design_warning"}
    assert detail["auto"] is False and detail["reason"] == "human"
    assert detail["changed_keys"] == ["topics[0].title"]
    assert detail["comment_present"] is True
    assert detail["timeout_count"] == 0 and detail["flow_design_warning"] is False
    assert rec.operator == "秘书" and rec.result == "success" and rec.target_type == "meeting"
    assert "涉密" not in rec.detail_raw
    snapshot = json.loads((e.rt.cfg.app.data_dir / e.mid / detail["snapshot_ref"]).read_text())
    from mmwf.domain.review.gate_edit import fingerprint, field_diff
    assert snapshot["comment"] == comment
    assert detail["before_sha256"] == fingerprint(snapshot["before"])
    assert detail["after_sha256"] == fingerprint(snapshot["after"])
    assert detail["changed"] == field_diff(snapshot["before"], snapshot["after"])
    assert detail["comment_sha256"] == fingerprint(comment)
    assert verify_chain(e.rt.db_path).ok
    # 审计查询 / 导出保留完整摘要，沿用既有 AUD 端点。
    from mmwf.infra.audit import query, AuditQuery
    assert any(r.log_id == rec.log_id for r in query(e.rt.db_path, AuditQuery(action="gate_decision", operator="秘书")))


def test_wait_time_comes_from_checkpoint_and_is_stable(gate_env):
    e = gate_env
    with e.rt.runner(e.mid) as runner:
        created = runner.checkpoint().created_at
        e.rt.gate_service().observe_pending(e.mid, runner)
    with connect_ctx(e.rt.db_path) as conn:
        first = dict(conn.execute("SELECT * FROM task_state WHERE task_id=?", (e.mid,)).fetchone())
    e.client.get(e.base)
    with connect_ctx(e.rt.db_path) as conn:
        second = dict(conn.execute("SELECT * FROM task_state WHERE task_id=?", (e.mid,)).fetchone())
    assert first["awaiting_since"] == second["awaiting_since"] == created


def test_human_and_timeout_tick_compete(gate_env):
    """同一检查点的真实巡检与 HTTP 人工决定只允许一个赢家。"""
    from datetime import datetime, timedelta, timezone
    from threading import Barrier
    from mmwf.app.services.gate_timeout import GateTimeout

    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    now = datetime.now(timezone.utc)
    with connect_ctx(e.rt.db_path) as conn:
        conn.execute("UPDATE task_state SET awaiting_since=? WHERE task_id=?",
                     ((now - timedelta(hours=72)).isoformat(), e.mid))
    barrier = Barrier(2)

    def automatic():
        barrier.wait(timeout=5)
        return GateTimeout(e.rt, clock=lambda: now).patrol_once()

    def human():
        barrier.wait(timeout=5)
        return send(e, "G2", ckpt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        human_request = pool.submit(human)
        auto = pool.submit(automatic)
        human_result, auto_result = human_request.result(), auto.result()
    assert bool(human_result["ok"]) + bool(auto_result) == 1
    assert len(audits(e)) == 1 and verify_chain(e.rt.db_path).ok
    assert audits(e)[0].detail["checkpoint_id"] == ckpt
    with connect_ctx(e.rt.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM idempotency_record WHERE node_id='gate_decision'").fetchone()[0] == 1


def test_snapshot_failure_prevents_registration_and_resume(gate_env, monkeypatch):
    e = gate_env
    from mmwf.infra.gate_store import GateStore

    def fail(*args):
        raise OSError("故障：快照未完成")

    monkeypatch.setattr(GateStore, "snapshot", fail)
    e.calls.clear()
    assert send(e, "G2", e.pending["checkpoint_id"])["ok"] is False
    assert not audits(e) and not e.rt.gate_service().store.registered() and not e.calls


def test_reject_limit_is_not_a_successful_rerun(gate_env):
    e = gate_env
    pending = e.pending
    for _ in range(3):
        body = send(e, "G2", pending["checkpoint_id"], "reject")
        assert body["ok"], body
        pending = body["data"]["pending_gate"]
    e.calls.clear()
    body = send(e, "G2", pending["checkpoint_id"], "reject")
    assert body["ok"] is False and body["code"] == "E4001"
    assert len(audits(e)) == 3 and not e.calls


def test_startup_recovers_registered_decision(gate_env, monkeypatch):
    e = gate_env
    original = GateService._resume

    def fail(*args):
        raise RuntimeError("登记后退出")

    monkeypatch.setattr(GateService, "_resume", fail)
    assert not send(e, "G2", e.pending["checkpoint_id"])["ok"]
    monkeypatch.setattr(GateService, "_resume", original)
    from mmwf.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(e.rt.cfg)):
        pass
    with e.rt.runner(e.mid) as runner:
        assert runner.status().pending_gate.gate == "G3"
    assert len(audits(e)) == 1


@pytest.mark.parametrize("failure_at", ["runner", "resume"])
def test_startup_isolates_failed_record_and_recovers_next(gate_env, monkeypatch, caplog, failure_at):
    """首条登记恢复异常不阻断 lifespan；第二个会议仍从磁盘恢复。"""
    from fastapi.testclient import TestClient
    from mmwf.api.routes._context import Runtime
    from mmwf.main import create_app

    e = gate_env
    created = e.client.post("/api/v1/meetings", json={
        "title": "第二个会议", "record_markdown": "张三：讨论第二季度计划。",
    }).json()
    assert created["ok"], created
    second_mid = created["data"]["meeting_id"]
    second_base = f"/api/v1/meetings/{second_mid}"
    started = e.client.post(second_base + "/start").json()
    assert started["ok"], started

    def fail_before_resume(*args):
        raise RuntimeError("模拟登记后退出")

    with monkeypatch.context() as registration_patch:
        registration_patch.setattr(GateService, "_resume", fail_before_resume)
        assert not send(e, "G2", e.pending["checkpoint_id"])["ok"]
        result = e.client.post(second_base + "/gate/G2/decide", json={
            "checkpoint_id": started["data"]["pending_gate"]["checkpoint_id"],
            "decision": "approve", "operator": "秘书",
        }).json()
        assert not result["ok"]

    records = e.rt.gate_service().store.registered()
    assert [r["meeting_id"] for r in records] == [e.mid, second_mid]
    original_runner = Runtime.runner
    original_resume = GateService._resume

    def runner_with_failure(self, meeting_id):
        if meeting_id == e.mid:
            raise RuntimeError("模拟损坏检查点导致 runner 构造失败")
        return original_runner(self, meeting_id)

    def resume_with_failure(self, runner, record):
        if record["meeting_id"] == e.mid:
            raise RuntimeError("模拟单条登记恢复失败")
        return original_resume(self, runner, record)

    caplog.clear()
    with monkeypatch.context() as startup_patch:
        if failure_at == "runner":
            startup_patch.setattr(Runtime, "runner", runner_with_failure)
        else:
            startup_patch.setattr(GateService, "_resume", resume_with_failure)
        with TestClient(create_app(e.rt.cfg)) as restarted:
            assert restarted.get("/docs").status_code == 404

    with e.rt.runner(e.mid) as first_runner, e.rt.runner(second_mid) as second_runner:
        assert first_runner.status().pending_gate.checkpoint_id == e.pending["checkpoint_id"]
        assert second_runner.status().pending_gate.gate == "G3"
    errors = [r for r in caplog.records if r.name == "mmwf.app.services.gate" and r.exc_info]
    assert len(errors) == 1 and e.mid in errors[0].getMessage()
    assert [r["meeting_id"] for r in e.rt.gate_service().store.registered()] == [e.mid]
    assert len(audits(e)) == 2 and verify_chain(e.rt.db_path).ok


def test_consumed_decisions_skip_historical_runners(gate_env, monkeypatch):
    """消费标记保留重试决定，恢复无需再构造历史会议的 runner。"""
    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    assert send(e, "G2", ckpt)["ok"]
    service = e.rt.gate_service()
    record = service.store.get(e.mid, "G2", ckpt)
    assert record["consumed"] is True
    assert service.store.registered() == []
    # 兼容标记引入前的旧登记：检查点已前进时只补消费标记，不 resume。
    with connect_ctx(e.rt.db_path) as conn:
        conn.execute("UPDATE idempotency_record SET result_ref=json_remove(result_ref, '$.consumed') WHERE idem_key=?",
                     (service.store.key(e.mid, "G2", ckpt),))
    e.calls.clear()
    assert service.recover_pending_decisions() == []
    assert not e.calls
    assert service.store.get(e.mid, "G2", ckpt)["consumed"] is True

    def unexpected_runner(*args):
        pytest.fail("历史决定不应再次构造 runner")

    monkeypatch.setattr(e.rt, "runner", unexpected_runner)
    assert service.recover_pending_decisions() == []
    assert len(audits(e)) == 1


def test_missing_reject_count_warns_after_successful_resume(gate_env, monkeypatch, caplog):
    """节点实际执行但计数缺失时仍如实返回停点，不将已生效决定报成错误。"""
    from mmwf.graph.runner import GraphRunner

    e = gate_env
    monkeypatch.setattr(GraphRunner, "run_counts", lambda self: {})
    e.calls.clear()
    response = send(e, "G2", e.pending["checkpoint_id"], "reject")
    assert response["ok"], response
    data = response["data"]
    assert data["resumed"] is True and data["rerun_nodes"] == []
    assert data["pending_gate"]["checkpoint_id"] != e.pending["checkpoint_id"]
    assert [name for _, name in e.calls].count("extract") == 1
    assert "未观测到驳回目标执行" in caplog.text
    assert e.rt.gate_service().store.registered() == []
    assert len(audits(e)) == 1
    retry = send(e, "G2", e.pending["checkpoint_id"], "reject")
    assert retry["ok"] and retry["data"]["resumed"] is False


@pytest.mark.parametrize('missing_source', [False, True])
def test_g3_registered_candidate_recovery_rechecks(gate_env, monkeypatch, missing_source):
    e = gate_env
    pending = send(e, 'G2', e.pending['checkpoint_id'])['data']['pending_gate']
    original = GateService._resume

    def fail(*args):
        raise RuntimeError('故障：G3 登记完成但尚未 resume')

    monkeypatch.setattr(GateService, '_resume', fail)
    payload = {'minutes': {'topics': [{'name': '恢复后的预算议题'}, {}]}}
    result = send(e, 'G3', pending['checkpoint_id'], 'edit', payload)
    assert not result['ok']
    record = e.rt.gate_service().store.get(e.mid, 'G3', pending['checkpoint_id'])
    assert record['resume']['_g3_candidate']['minutes_structured']['topics'][0]['name'] == '恢复后的预算议题'
    if missing_source:
        # 模拟登记后权威源暂不可读；保留同一检查点身份验证恢复预检。
        monkeypatch.setattr('mmwf.app.services.minutes.authoritative_segments', lambda s: [])
    monkeypatch.setattr(GateService, '_resume', original)
    from mmwf.api.routes._context import Runtime
    fresh = Runtime(e.rt.cfg)
    e.calls.clear()
    recovered = fresh.gate_service().recover_pending_decisions()
    if missing_source:
        assert recovered == [] and e.calls == []
        with fresh.runner(e.mid) as runner:
            assert runner.status().pending_gate.checkpoint_id == pending['checkpoint_id']
    else:
        assert len(recovered) == 1 and recovered[0]['rerun_nodes'] == []
        with fresh.runner(e.mid) as runner:
            values = runner.values()
            assert values['minutes_structured']['topics'][0]['name'] == '恢复后的预算议题'
            assert '恢复后的预算议题' in values['minutes_markdown']
        assert all(name not in {'transcribe', 'extract', 'generate', 'assemble'} for _, name in e.calls)
        assert fresh.gate_service().recover_pending_decisions() == []
    assert len(audits(e)) == 2


def test_g3_recovery_failure_does_not_stop_other_meetings(gate_env, monkeypatch):
    from mmwf.api.routes._context import Runtime
    from mmwf.app.services import minutes

    e = gate_env
    first = send(e, 'G2', e.pending['checkpoint_id'])['data']['pending_gate']
    created = e.client.post('/api/v1/meetings', json={
        'title': '第二个预算会', 'record_markdown': '张三：研究预算执行。\n\n李四：讨论年度计划。'}).json()
    assert created['ok'], created
    second_id = created['data']['meeting_id']
    base = f'/api/v1/meetings/{second_id}'
    started = e.client.post(base + '/start').json()
    assert started['ok'], started
    second = e.client.post(base + '/gate/G2/decide', json={
        'checkpoint_id': started['data']['pending_gate']['checkpoint_id'],
        'decision': 'approve', 'operator': '秘书'}).json()['data']['pending_gate']
    original_resume = GateService._resume

    def fail(*args):
        raise RuntimeError('故障：已登记，尚未恢复')

    monkeypatch.setattr(GateService, '_resume', fail)
    for meeting_id, pending in ((e.mid, first), (second_id, second)):
        with pytest.raises(RuntimeError):
            e.rt.gate_service().apply_decision(meeting_id, 'G3', pending['checkpoint_id'],
                                              'edit', {'minutes': {'title': '预算会修订'}}, '秘书', '')
    monkeypatch.setattr(GateService, '_resume', original_resume)
    authoritative_segments = minutes.authoritative_segments

    def unavailable_for_first(state):
        return [] if state['meeting_id'] == e.mid else authoritative_segments(state)

    monkeypatch.setattr(minutes, 'authoritative_segments', unavailable_for_first)
    fresh = Runtime(e.rt.cfg)
    service = fresh.gate_service()
    registered = service.store.registered()
    assert [r['meeting_id'] for r in registered] == [e.mid, second_id]
    e.calls.clear()
    recovered = service.recover_pending_decisions()
    assert len(recovered) == 1 and recovered[0]['from_checkpoint'] == second['checkpoint_id']
    with fresh.runner(e.mid) as runner:
        assert runner.status().pending_gate.checkpoint_id == first['checkpoint_id']
    with fresh.runner(second_id) as runner:
        assert runner.status().status == 'archived'
        assert runner.values()['minutes_structured']['title'] == '预算会修订'
    assert all(mid != e.mid for mid, _ in e.calls)
    assert [r['meeting_id'] for r in service.store.registered()] == [e.mid]
