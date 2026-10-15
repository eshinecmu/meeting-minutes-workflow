"""E3 合成链与生产前 G3 联证；禁网并监测全部交付写入接缝。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from _netguard import install_network_guard
from portable_support import directory_symlink
from unittest.mock import Mock

import pytest

from mmwf.adapters.llm import FakeLlmClient
from mmwf.domain.minutes.template_application import render_template_markdown
from mmwf.graph.build import Deps, route_after_validate
from mmwf.graph.nodes.extraction import make_extract, make_llm_extractor
from mmwf.graph.nodes.minutes_context import production_node
from mmwf.infra.db import connect_ctx
from mmwf.infra.migrations.runner import migrate
from mmwf.settings import load_settings
import shadow_run

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/shadow/synthetic"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    install_network_guard(monkeypatch.setattr)


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def copy_fixture(tmp_path):
    source = tmp_path / "input"
    shutil.copytree(FIXTURES, source)
    return source


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_synthetic_equals_production_pre_g3_and_zero_delivery_writes(tmp_path, monkeypatch):
    from mmwf.app.services.minutes import MinutesService
    from mmwf.app.services.export import ExportService
    from mmwf.infra.minutes_store import MinutesStore
    from mmwf.graph import nodes
    from mmwf.graph.nodes import export
    from mmwf.graph.runner import GraphRunner

    production = tmp_path / "production"
    production.mkdir()
    cfg = load_settings(ROOT / "config")
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={"data_dir": production})})
    db = production / "mmwf.db"
    migrate(db)
    (production / "export").mkdir()
    (production / "export/existing.docx").write_bytes(b'SYNTHETIC existing output sentinel')
    (production / "archive").mkdir()
    (production / "archive/existing.json").write_text('{"synthetic": true}')
    batch, _, paths = shadow_run.prepare_batch(FIXTURES / "manifest.json", tmp_path / "unused")
    references = {}
    for meeting in batch.meetings:
        state, context, _ = shadow_run.load_inputs(batch, meeting, paths)
        state.update(meeting_id=meeting.meeting_id)
        with connect_ctx(db) as conn:
            conn.execute("INSERT INTO meeting (meeting_id,title,status,created_by,created_at,updated_at) VALUES (?,?,'created','synthetic','now','now')", (meeting.meeting_id, meeting.name))
            conn.execute("INSERT INTO task_state (task_id,current_node,status,updated_at) VALUES (?,'extract','created','now')", (meeting.meeting_id,))
        response = shadow_run.read_json(paths[f"{meeting.meeting_id}/fake_response_path"])
        fake = FakeLlmClient(script=response["script"])
        extractor = make_llm_extractor(fake, cfg)
        deps = Deps(settings=cfg)
        context_factory = lambda s, context=context: deepcopy(context)
        state.update(production_node("extract", make_extract(deps, extractor=extractor), context_factory, deps=deps)(state))
        while True:
            state.update(production_node("generate", None, context_factory, deps=deps)(state))
            state.update(production_node("validate", None, context_factory, deps=deps)(state))
            route = route_after_validate(state)
            if route != "retry":
                break
        assert route == "ok"
        assert fake.calls and all(c["has_schema"] for c in fake.calls)
        references[meeting.meeting_id] = deepcopy(state)
    with connect_ctx(db) as conn:
        versions_before = list(conn.execute("SELECT * FROM minutes_version"))
    # 生产参考只跑到 validate；从此开始，影子工具不得访问 DB、正式 runner 或交付包装器。
    spies = []
    for owner, name in ((MinutesService, "__init__"), (MinutesService, "save_draft"), (MinutesService, "deliver"),
                        (MinutesStore, "__init__"), (MinutesStore, "save"), (MinutesStore, "mark_final"),
                        (ExportService, "export_docx"), (export, "make_export"), (export, "make_archive"),
                        (nodes, "make_export"), (nodes, "make_archive"), (GraphRunner, "start"), (GraphRunner, "resume")):
        spy = Mock(side_effect=AssertionError(f"禁止调用 {owner}.{name}"))
        monkeypatch.setattr(owner, name, spy)
        spies.append(spy)
    import mmwf.infra.db as db_module
    db_spy = Mock(side_effect=AssertionError("影子工具禁止连接业务 DB"))
    original_connect = db_module.connect
    monkeypatch.setattr(db_module, "connect", db_spy)
    before_prod, before_input = snapshot(production), snapshot(FIXTURES)
    output = tmp_path / "shadow"
    run = shadow_run.run_batch(FIXTURES / "manifest.json", output)
    assert run["complete"] and len(run["meetings"]) == 2
    assert run["real_meetings_counted"] == 0 and run["real_run_status"] == "PENDING"
    assert run["eligible_for_stage2"] is False
    assert [m["duration_class"] for m in run["meetings"]] == ["short", "long"]
    assert snapshot(production) == before_prod
    assert snapshot(FIXTURES) == before_input
    assert db_spy.call_count == 0
    assert all(spy.call_count == 0 for spy in spies)
    monkeypatch.setattr(db_module, "connect", original_connect)
    with connect_ctx(db) as conn:
        assert list(conn.execute("SELECT * FROM minutes_version")) == versions_before == []
    for mid, reference in references.items():
        folder = output / mid
        candidate = shadow_run.read_json(folder / "candidate.json")
        issues = shadow_run.read_json(folder / "issues.json")
        assert candidate["minutes"] == reference["minutes_structured"]
        assert [{k: v for k, v in i.items() if k != "issue_id"} for i in issues["issues"]] == reference["issues"]
        assert candidate["context"] == reference["minutes"]["_context"]
        assert candidate["model"]["calls"] >= 1
        text = (folder / "candidate.md").read_text()
        assert text.startswith(shadow_run.WARNING + "\n\n" + shadow_run.SYNTHETIC)
        assert text.endswith(render_template_markdown(reference["minutes_structured"], reference["issues"]))
        score = shadow_run.read_json(FIXTURES / "scores.json")["scores"][int(mid[-1]) - 1]
        assert score["candidate_fingerprint"] == candidate["candidate_fingerprint"]
        assert score["source_fingerprint"] == candidate["source_fingerprint"]
        assert score["template_fingerprint"] == candidate["template_fingerprint"]
        assert candidate["source_file_fingerprints"]
    assert not list(output.rglob("*.docx"))
    assert not list(output.rglob("archive"))
    assert not list(output.rglob("*final*"))


@pytest.mark.parametrize("failure", ["missing_source", "bad_json", "blocked", "wrong_template", "bad_segments"])
def test_failure_keeps_all_planned_meetings_and_no_usable_draft(tmp_path, failure):
    source = copy_fixture(tmp_path)
    if failure == "missing_source":
        (source / "synthetic-1.record.md").unlink()
    elif failure == "bad_json":
        write_json(source / "synthetic-1.fake.json", {"script": ["not-json"]})
    elif failure == "blocked":
        segments = shadow_run.read_json(source / "synthetic-1.segments.json")
        segments[1]["text"] = "权威源没有候选引用的句子。"
        write_json(source / "synthetic-1.segments.json", segments)
    elif failure == "wrong_template":
        template = shadow_run.read_json(source / "synthetic-1.template.json")
        template["meeting_kind"] = "different"
        write_json(source / "synthetic-1.template.json", template)
    else:
        write_json(source / "synthetic-1.segments.json", [])
    before = snapshot(source)
    output = tmp_path / "shadow"
    assert shadow_run.main(["--manifest", str(source / "manifest.json"), "--output", str(output)]) == 1
    run = shadow_run.read_json(output / "run.json")
    assert len(run["meetings"]) == 2 and not run["complete"]
    first, second = run["meetings"]
    assert first["status"] == ("blocked" if failure == "blocked" else "failed")
    assert first["failure_reason"] and second["status"] == "generated"
    assert not (output / "synthetic-1/candidate.md").exists()
    assert (output / "synthetic-1/issues.json").exists()
    assert len(shadow_run.read_json(output / "records.template.json")["records"]) == 2
    assert snapshot(source) == before
    if failure in ("blocked", "bad_json"):
        assert shadow_run.read_json(output / "synthetic-1/issues.json")["issues"]


@pytest.mark.parametrize("problem", ["duplicate", "traversal_id", "traversal_input", "existing", "input_overlap", "symlink_overlap", "production_overlap"])
def test_paths_and_reuse_fail_before_output(tmp_path, problem):
    source = copy_fixture(tmp_path)
    manifest = source / "manifest.json"
    raw = shadow_run.read_json(manifest)
    output = tmp_path / "output"
    if problem == "duplicate":
        raw["meetings"].append(deepcopy(raw["meetings"][0]))
    elif problem == "traversal_id":
        raw["meetings"][0]["meeting_id"] = "../escape"
    elif problem == "traversal_input":
        raw["meetings"][0]["record_path"] = "../escape.md"
    elif problem == "existing":
        output.mkdir()
    elif problem == "input_overlap":
        output = source / "output"
    elif problem == "symlink_overlap":
        directory_symlink(tmp_path / "link", source)
        output = tmp_path / "link/output"
    else:
        formal = tmp_path / "production"
        formal.mkdir()
        raw["production_data_dirs"] = [str(formal)]
        output = formal / "shadow"
    write_json(manifest, raw)
    before = snapshot(source)
    assert shadow_run.main(["--manifest", str(manifest), "--output", str(output)]) == 2
    assert snapshot(source) == before
    assert not (output / "run.json").exists()


def test_real_unavailable_no_fake_or_naive_fallback(tmp_path, monkeypatch):
    # 只使用合成元数据验证 real 的失败分支；无真实资料或模型调用。
    from mmwf.adapters import llm
    source = copy_fixture(tmp_path)
    raw = shadow_run.read_json(source / "manifest.json")
    raw.update(dataset_kind="real", controlled_environment_confirmed=True,
               operator_declaration="SYNTHETIC 条件测试，非真实受控运行证据",
               production_data_dirs=[str(tmp_path / "production")],
               model_config={"llm": {"provider": "ollama_local", "base_url": "http://127.0.0.1:11434", "model": "SYNTHETIC-unavailable"}})
    write_json(source / "manifest.json", raw)
    unavailable = Mock()
    unavailable.available.return_value = False
    monkeypatch.setattr(llm, "make_llm_client", Mock(return_value=unavailable))
    fake = Mock(side_effect=AssertionError("real 禁止 Fake"))
    monkeypatch.setattr(llm, "FakeLlmClient", fake)
    output = tmp_path / "shadow"
    assert shadow_run.main(["--manifest", str(source / "manifest.json"), "--output", str(output)]) == 1
    run = shadow_run.read_json(output / "run.json")
    assert len(run["meetings"]) == 2 and all(m["status"] == "failed" for m in run["meetings"])
    assert all("不回退" in m["failure_reason"] for m in run["meetings"])
    assert fake.call_count == 0 and unavailable.chat.call_count == 0


def test_synthetic_cli_success_and_new_output_required(tmp_path, capsys):
    output = tmp_path / "shadow"
    args = ["--manifest", str(FIXTURES / "manifest.json"), "--output", str(output)]
    assert shadow_run.main(args) == 0
    assert "计入真实验收 0" in capsys.readouterr().out
    before = snapshot(output)
    assert shadow_run.main(args) == 2
    assert snapshot(output) == before


def test_bad_response_schema_retries_are_recorded(tmp_path):
    source = copy_fixture(tmp_path)
    write_json(source / "synthetic-1.fake.json", {"script": ["not-json"]})
    output = tmp_path / "shadow"
    run = shadow_run.run_batch(source / "manifest.json", output)
    assert not run["complete"]
    candidate = shadow_run.read_json(output / "synthetic-1/candidate.json")
    calls = [s for s in candidate["steps"] if s["step"] == "extract_chat"]
    assert len(calls) == candidate["model"]["calls"] == 3
    assert [c["schema_retry"] for c in calls] == [False, True, True]
    assert all(s["step"] != "render_template_markdown" for s in candidate["steps"])


def test_production_route_limits_are_reused(tmp_path, monkeypatch):
    # 旧式软问题触发有限重试；blocked 无论重试次数多少都优先停止。
    source = copy_fixture(tmp_path)
    real_validate = shadow_run.post_validate

    def with_legacy(*args, **kwargs):
        return real_validate(*args, **kwargs) + [{"source": "template", "rule": "legacy"}]

    monkeypatch.setattr(shadow_run, "post_validate", with_legacy)
    output = tmp_path / "retries"
    run = shadow_run.run_batch(source / "manifest.json", output)
    assert run["complete"]
    candidate = shadow_run.read_json(output / "synthetic-1/candidate.json")
    assert [s["route"] for s in candidate["steps"] if s["step"] == "validate"] == ["retry", "retry", "give_up"]
    segments = shadow_run.read_json(source / "synthetic-1.segments.json")
    segments[1]["text"] = "源内容不符，必须停止。"
    write_json(source / "synthetic-1.segments.json", segments)
    blocked_output = tmp_path / "blocked"
    blocked = shadow_run.run_batch(source / "manifest.json", blocked_output)
    assert blocked["meetings"][0]["status"] == "blocked"
    candidate = shadow_run.read_json(blocked_output / "synthetic-1/candidate.json")
    assert [s["route"] for s in candidate["steps"] if s["step"] == "validate"] == ["blocked"]
    assert not (blocked_output / "synthetic-1/candidate.md").exists()
