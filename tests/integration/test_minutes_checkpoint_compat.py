"""冻结旧 schema 写真实 SQLite；关闭后由新 Runtime/runner 恢复。"""
from copy import deepcopy

import pytest
from langgraph.graph import START, END, StateGraph

import frozen_pre_d4_state as OLD
from mmwf.domain.errors import GroundingBlockedError
from mmwf.graph import interrupts as I
from mmwf.graph.build import make_checkpointer, thread_config
from mmwf.graph.nodes.extraction import naive_extract, naive_generate, naive_assemble
from test_post_validation_pipeline import runtime, RECORD


def write_old_checkpoint(runtime, stop="gate_g2", missing_source=False):
    """旧 schema 与旧拓扑独立构造；不从新 TypedDict 删除字段冒充旧库。"""
    counts = {}
    def counted(name, fn):
        def run(s):
            counts[name] = counts.get(name, 0) + 1
            return fn(s)
        return run
    saver = make_checkpointer(runtime.db_path)
    graph = StateGraph(OLD.PipelineState)
    for name, fn in {
        "ingest": lambda s: {}, "transcribe": lambda s: {"transcript": []},
        "review": lambda s: {"record_markdown": "" if missing_source else RECORD},
        "extract": lambda s: naive_extract(RECORD),
        "generate": lambda s: {"minutes_structured": naive_generate(s)},
        "validate": lambda s: {"validation_issues": []},
        "assemble": lambda s: {"minutes_markdown": naive_assemble(s["minutes_structured"], [])},
        "export": lambda s: {"export_paths": {"docx": "legacy.docx"}},
        "archive": lambda s: {"stage": "archived"},
        "gate_g2": I.make_gate_g2(), "gate_g3": I.make_gate_g3(),
    }.items():
        graph.add_node(name, counted(name, fn))
    graph.add_edge(START, "ingest")
    for left, right in zip(OLD.NODE_SEQUENCE, OLD.NODE_SEQUENCE[1:]):
        if left not in {"gate_g2", "gate_g3", "validate"}:
            graph.add_edge(left, right)
    graph.add_conditional_edges("gate_g2", I.route_after_g2,
                                {"continue": "generate", "redo": "extract"})
    graph.add_conditional_edges("gate_g3", I.route_after_g3,
                                {"continue": "export", "redo": "assemble"})
    def route_after_validate(state):
        return "ok"
    graph.add_conditional_edges("validate", route_after_validate,
                                {"ok": "assemble", "retry": "generate", "give_up": "assemble"})
    graph.add_edge("archive", END)
    app = graph.compile(checkpointer=saver, interrupt_before=[stop] if stop == "assemble" else [])
    cfg = thread_config("d4")
    app.invoke({"meeting_id": "d4", "task_id": "d4"}, cfg)
    if stop == "assemble":
        from langgraph.types import Command
        app.invoke(Command(resume={"decision": "approve"}), cfg)
    snapshot = app.get_state(cfg)
    assert "minutes" not in snapshot.values and "issues" not in snapshot.values
    saver.conn.close()
    return deepcopy(snapshot.values), counts, snapshot.config["configurable"]["checkpoint_id"]


@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
def test_frozen_g2_real_checkpoint_resume(runtime, decision):
    before, counts, checkpoint_id = write_old_checkpoint(runtime)
    original_counts = deepcopy(counts)
    with runtime.runner("d4") as runner:
        assert runner.status().pending_gate.gate == "G2"
        assert runner.status().pending_gate.checkpoint_id == checkpoint_id
        assert runner.values() == before
        payload = {"decision": decision}
        if decision == "edit":
            payload["payload"] = {"topics": [{**before["topics"][0], "title": "今天研究方案"}]}
        status = runner.resume(payload)
        assert all(runner.run_counts().get(n, 0) == 0 for n in ("ingest", "transcribe", "review"))
        assert counts == original_counts
        if decision == "reject":
            assert status.pending_gate.gate == "G2"
            assert runner.run_counts()["extract"] == 1
        else:
            assert status.pending_gate.gate == "G3"
            assert runner.run_counts().get("extract", 0) == 0
            assert runner.values()["minutes_structured"]["topics"]
            assert runner.values()["minutes"]["_context"]["template_snapshot"]
            assert runner.values()["issues"]
            assert runner.run_counts()["generate"] == 1


def test_frozen_g2_missing_source_blocks_without_reextract(runtime):
    before, counts, checkpoint_id = write_old_checkpoint(runtime, missing_source=True)
    with runtime.runner("d4") as runner:
        assert runner.status().pending_gate.checkpoint_id == checkpoint_id
        with pytest.raises(GroundingBlockedError):
            runner.resume({"decision": "approve"})
        assert runner.status().status == "failed"
        assert any(i["code"] == "SOURCE_UNAVAILABLE" for i in runner.values()["issues"])
        assert all(runner.run_counts().get(n, 0) == 0 for n in ("transcribe", "extract", "assemble", "export"))
        assert runner.values()["topics"] == before["topics"]


@pytest.mark.parametrize("missing_source", [False, True])
def test_frozen_next_assemble_adapts_and_rechecks(runtime, missing_source):
    before, counts, checkpoint_id = write_old_checkpoint(runtime, stop="assemble", missing_source=missing_source)
    with runtime.runner("d4") as runner:
        assert runner.checkpoint().config["configurable"]["checkpoint_id"] == checkpoint_id
        assert runner.checkpoint().next == ("assemble",)
        if missing_source:
            with pytest.raises(GroundingBlockedError):
                runner._invoke(None)
            assert runner.status().status == "failed"
            assert runner.values()["minutes_structured"] == before["minutes_structured"]
        else:
            runner._invoke(None)
            assert runner.status().pending_gate.gate == "G3"
            assert "决定按计划实施" in runner.values()["minutes_markdown"]
        assert all(runner.run_counts().get(n, 0) == 0 for n in ("transcribe", "extract", "generate", "export"))
