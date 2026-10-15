"""D4：Runtime 双引擎、真实图硬路由及持久失败。"""
import socket
from pathlib import Path

import pytest

from mmwf.adapters.llm import FakeLlmClient
from mmwf.api.routes._context import Runtime
from mmwf.domain.errors import GroundingBlockedError, SchemaViolationError
from mmwf.graph import nodes as N
from mmwf.graph.build import route_after_validate
from mmwf.infra.db import connect_ctx
from mmwf.infra.migrations.runner import migrate
from mmwf.settings import ExtractionConfig, load_settings

RECORD = "[00:00:12] 张三：今天研究方案，差不多可以。\n[00:00:30] 李四：决定按计划实施。"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("禁止网络")
    monkeypatch.setattr(socket, "socket", forbidden)
    cfg = load_settings(Path(__file__).resolve().parents[2] / "config")
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={"data_dir": tmp_path})})
    migrate(tmp_path / "mmwf.db")
    rt = Runtime(cfg)
    with connect_ctx(rt.db_path) as conn:
        conn.execute("INSERT INTO meeting (meeting_id,title,status,created_by,created_at,updated_at) VALUES ('d4','测试会议','created','tester','now','now')")
        conn.execute("INSERT INTO task_state (task_id,current_node,status,updated_at) VALUES ('d4','ingest','created','now')")
    # 外部音频/文件交付依赖替身；四个纪要节点和路由保持真实。
    monkeypatch.setattr(N, "make_ingest", lambda d: lambda s: {})
    monkeypatch.setattr(N, "make_transcribe", lambda d: lambda s: {"transcript": []})
    monkeypatch.setattr(N, "make_review", lambda d: lambda s: {"record_markdown": RECORD})
    return rt


@pytest.mark.parametrize("template_id", ["tpl-regular-meeting-v1", "tpl-party-group-v1",
                                        "tpl-special-topic-v1", "tpl-coordination-v1"])
@pytest.mark.parametrize("engine", ["naive", "llm"])
def test_both_engines_use_template_snapshot_and_checks(runtime, monkeypatch, engine, template_id):
    template = runtime.template_store().select(template_id)
    row = template.to_row_payload()
    with connect_ctx(runtime.db_path) as conn:
        conn.execute("INSERT INTO template (template_id,name,meeting_kind,schema_json,style_json,created_at) VALUES (?,?,?,?,?,?)",
                     (template_id, template.name, template.meeting_kind, row["schema_json"], row["style_json"], "now"))
        conn.execute("UPDATE meeting SET template_id=? WHERE meeting_id='d4'", (template_id,))
    if engine == "llm":
        from mmwf.adapters import llm
        fake = FakeLlmClient(oracle_items=[{"quote": "决定按计划实施。", "content": "差不多实施", "topic": "实施", "owner": "暂无"}])
        monkeypatch.setattr(llm, "make_llm_client", lambda cfg: fake)
        runtime.cfg = runtime.cfg.model_copy(update={"extraction": ExtractionConfig(engine="llm")})
    with runtime.runner("d4") as runner:
        assert runner.start().pending_gate.gate == "G2"
        snapshot = runner.values()["minutes"]["_context"]["template_snapshot"]
        assert snapshot["template_id"] == template_id
        # 当前会议的后续节点必须复用原模板快照，即使模板服务已不可用。
        monkeypatch.setattr(runtime, "template_store", lambda: (_ for _ in ()).throw(AssertionError("重复选模")))
        status = runner.resume({"decision": "approve"})
        assert status.pending_gate.gate == "G3"
        values = runner.values()
        assert values["minutes"]["_context"]["template_snapshot"] == snapshot
        assert any(i["source"] == "template" for i in values["issues"])
        assert any("差不多" in i["detail"] for i in values["issues"])
        assert values["minutes_structured"]["sections"]
        assert runner.run_counts()["generate"] == 1


@pytest.mark.parametrize("retry", [0, 2, 100])
def test_blocked_priority_and_persisted_failed(runtime, retry):
    with runtime.runner("d4") as runner:
        runner.start()
        values = runner.values()
        values["resolutions"][0]["refs"] = []
        from mmwf.graph.nodes.minutes_context import production_node
        values["gen_retry"] = retry
        generated = production_node("generate", None, runner.deps.extra["minutes_context"], deps=runner.deps)(values)
        generated["gen_retry"] = retry
        runner.app.update_state(runner._cfg, generated, as_node="generate")
        with pytest.raises(GroundingBlockedError) as caught:
            runner._invoke(None)
        assert caught.value.to_dict()["severity"] == "error"
        assert "已停止成文" in caught.value.to_dict()["message"]
        assert SchemaViolationError().to_dict()["severity"] == "warning"
        assert runner.status().status == "failed"
        assert not runner.checkpoint().next
        assert runner.values()["issues"]
        assert all(runner.run_counts().get(n, 0) == 0 for n in ("assemble", "gate_g3", "export", "archive"))
    with runtime.runner("d4") as rebuilt:
        assert rebuilt.status().status == "failed"
    with connect_ctx(runtime.db_path) as conn:
        assert conn.execute("SELECT status FROM task_state WHERE task_id='d4'").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM meeting WHERE meeting_id='d4'").fetchone()[0] == "failed"
    assert route_after_validate({"validation_issues": [{"blocking": True}], "gen_retry": retry}) == "blocked"


def test_a6_chunk_metadata_missing_aggregates_without_schema_retry(runtime, monkeypatch):
    import json
    from mmwf.adapters import llm
    payload = {"topics": [{"name": "实施", "discussion": "", "resolutions": [
        {"content": "差不多实施", "source_quote": "决定按计划实施。",
         "source_start_ms": 30000, "source_end_ms": None}], "pending_decisions": [
        {"content": "核对实施安排", "source_quote": "决定按计划实施。",
         "source_start_ms": 30000, "source_end_ms": None}]}]}
    fake = FakeLlmClient(script=[json.dumps(payload)])
    monkeypatch.setattr(llm, "make_llm_client", lambda cfg: fake)
    runtime.cfg = runtime.cfg.model_copy(update={"extraction": ExtractionConfig(engine="llm")})
    with runtime.runner("d4") as runner:
        runner.start()
        assert runner.resume({"decision": "approve"}).pending_gate.gate == "G3"
        assert len(fake.calls) == 1
        assert runner.values()["minutes_structured"]["topics"][0]["pending_decisions"][0]["content"] == "核对实施安排"
        issues = runner.values()["issues"]
        assert any(i["code"] == "CHUNK_META_MISSING" and i["source"] == "extraction" for i in issues)
        assert any(i["source"] == "template" and "缺失" in i["message"] for i in issues)
        assert runner.values()["minutes"]["topics"][0]["resolutions"][0]["source_quote"] == "决定按计划实施。"
