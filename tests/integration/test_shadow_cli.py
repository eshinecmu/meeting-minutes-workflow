"""SYNTHETIC 批次→人工编排评分→报告；不运行真实会议或真实模型。"""
from copy import deepcopy
import csv
import json
from pathlib import Path
import shutil
from _netguard import install_network_guard
from portable_support import directory_symlink
import subprocess
import sys

import pytest
import shadow_run
import shadow_report

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/shadow/synthetic"
PYTHON = sys.executable


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    install_network_guard(monkeypatch.setattr)


def setup_batch(tmp_path):
    source = tmp_path / "input"
    shutil.copytree(FIXTURES, source)
    batch = tmp_path / "batch"
    assert shadow_run.main(["--manifest", str(source / "manifest.json"), "--output", str(batch)]) == 0
    return source, batch


def args(source, batch, output):
    return ["--batch", str(batch), "--scores", str(source / "scores.json"),
            "--thresholds", str(source / "thresholds.json"), "--output", str(output)]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_subprocess_cli_synthetic_roundtrip_and_projection(tmp_path):
    source = tmp_path / "input"
    shutil.copytree(FIXTURES, source)
    batch, output = tmp_path / "batch", tmp_path / "report"
    # 子进程也显式封锁 socket，不能靠父进程 monkeypatch 作为禁网证据。
    wrapper = "import runpy,sys; sys.path.insert(0, 'tools'); from _netguard import install_network_guard; install_network_guard(); p=sys.argv.pop(1); runpy.run_path(p,run_name='__main__')"
    before = snapshot(source)
    run = subprocess.run([str(PYTHON), "-c", wrapper, str(ROOT / "tools/shadow_run.py"),
                          "--manifest", str(source / "manifest.json"), "--output", str(batch)], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    report = subprocess.run([str(PYTHON), "-c", wrapper, str(ROOT / "tools/shadow_report.py"), *args(source, batch, output)], cwd=ROOT, capture_output=True, text=True)
    assert report.returncode == 1, report.stderr
    assert snapshot(source) == before
    data = shadow_report.read_json(output / "report.json")
    records = shadow_report.read_json(output / "records.json")
    assert data["records"] == records["records"]
    assert data["dataset_kind"] == "synthetic" and data["real_meetings_counted"] == 0
    assert data["real_run_status"] == "PENDING" and not data["eligible_for_stage2"]
    with (output / "records.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for csv_row, row in zip(rows, records["records"]):
        for key, value in row.items():
            expected = "" if value is None else json.dumps(value, ensure_ascii=False) if isinstance(value, (list, bool)) else str(value)
            assert csv_row[key] == expected
    assert "SYNTHETIC" in (output / "report.md").read_text()
    assert set(p.name for p in output.iterdir()) == {"report.md", "report.json", "records.csv", "records.json"}
    assert "REPORT_GENERATED_NOT_ACCEPTANCE" == data["tool_validation_status"]


@pytest.mark.parametrize("bad", ["mixed", "kind", "extra", "duplicate", "old_score", "changed_candidate", "changed_source", "bad_issue", "source_traversal", "bad_location", "invalid_json"])
def test_report_rejects_invalid_inputs_without_success_artifacts(tmp_path, bad):
    source, batch = setup_batch(tmp_path)
    scores = shadow_report.read_json(source / "scores.json")
    if bad == "mixed": scores["batch_id"] = "other"
    if bad == "kind": scores["dataset_kind"] = "real"
    if bad == "extra": scores["scores"][0]["meeting_id"] = "extra"
    if bad == "duplicate": scores["scores"].append(deepcopy(scores["scores"][0]))
    if bad == "old_score": scores["scores"][0]["candidate_fingerprint"] = "0" * 64
    if bad == "changed_candidate":
        p = batch / "synthetic-1/candidate.json"
        value = shadow_report.read_json(p)
        value["minutes"]["topics"][0]["discussion"] = "SYNTHETIC 改稿"
        write(p, value)
    if bad == "changed_source":
        (source / "synthetic-1.segments.json").write_text("[]")
    if bad == "bad_issue": scores["scores"][0]["issue_reviews"][0]["issue_id"] = "unknown"
    if bad == "source_traversal": scores["scores"][0]["statements"][0]["source_locations"] = ["../source.json#seg-1"]
    if bad == "bad_location": scores["scores"][0]["statements"][0]["candidate_location"] = "candidate.json#/minutes/missing"
    write(source / "scores.json", scores)
    if bad == "invalid_json": (source / "scores.json").write_text('{"format_version":1,"format_version":2}')
    output = tmp_path / "report"
    assert shadow_report.main(args(source, batch, output)) == 2
    assert not output.exists()


@pytest.mark.parametrize("missing", ["score", "review", "uncertain", "downgrade_null", "failed"])
def test_incomplete_keeps_rows_counts_gaps_and_returns_2(tmp_path, missing):
    source, batch = setup_batch(tmp_path)
    scores = shadow_report.read_json(source / "scores.json")
    if missing == "score": scores["scores"].pop()
    if missing == "review": scores["scores"][0]["issue_reviews"].pop()
    if missing == "uncertain": scores["scores"][0]["issue_reviews"][0]["verdict"] = "uncertain"
    if missing == "downgrade_null":
        for row in scores["scores"]:
            for review in row["issue_reviews"]:
                review["false_downgrade"] = None
    if missing == "failed":
        run = shadow_report.read_json(batch / "run.json")
        run["meetings"][0].update(status="failed", failure_reason="SYNTHETIC 失败")
        write(batch / "run.json", run)
        candidate = shadow_report.read_json(batch / "synthetic-1/candidate.json")
        candidate.update(status="failed", failure_reason="SYNTHETIC 失败")
        write(batch / "synthetic-1/candidate.json", candidate)
    write(source / "scores.json", scores)
    output = tmp_path / "report"
    assert shadow_report.main(args(source, batch, output)) == 2
    report = shadow_report.read_json(output / "report.json")
    assert len(report["records"]) == 2 and report["status"] == "INCOMPLETE"
    assert not report["quality_pass"] and report["gaps"]
    assert report["metrics"]["structure"]["denominator"] == 2


@pytest.mark.parametrize("conflict", ["reuse", "input", "batch", "parent", "symlink", "traversal", "production"])
def test_output_conflicts_do_not_modify_inputs(tmp_path, conflict):
    source, batch = setup_batch(tmp_path)
    output = tmp_path / "report"
    if conflict == "reuse":
        assert shadow_report.main(args(source, batch, output)) == 1
    if conflict == "input": output = source / "report"
    if conflict == "batch": output = batch / "report"
    if conflict == "parent": output = tmp_path
    if conflict == "symlink":
        directory_symlink(tmp_path / "link", source)
        output = tmp_path / "link/report"
    if conflict == "traversal": output = tmp_path / "x/../report"
    if conflict == "production":
        production = tmp_path / "production"
        run = shadow_report.read_json(batch / "run.json")
        run["manifest"]["production_data_dirs"] = [str(production)]
        write(batch / "run.json", run)
        output = production / "report"
    before_source, before_batch = snapshot(source), snapshot(batch)
    before_output = snapshot(output) if output.exists() else None
    assert shadow_report.main(args(source, batch, output)) == 2
    assert snapshot(source) == before_source and snapshot(batch) == before_batch
    if before_output is not None:
        assert snapshot(output) == before_output


def test_publish_failure_cleans_new_report_directory(tmp_path, monkeypatch):
    source, batch = setup_batch(tmp_path)
    def fail(*args):
        raise OSError("SYNTHETIC 写入失败")
    monkeypatch.setattr(shadow_report.os, "replace", fail)
    output = tmp_path / "report"
    assert shadow_report.main(args(source, batch, output)) == 2
    assert not output.exists() and not list(tmp_path.glob(".shadow-report-*"))


@pytest.mark.parametrize("projection", ["records.json", "records.csv"])
def test_modified_projection_cannot_override_authoritative_scores(tmp_path, projection):
    source, batch = setup_batch(tmp_path)
    first = tmp_path / "first"
    assert shadow_report.main(args(source, batch, first)) == 1
    shutil.copyfile(first / projection, batch / projection)
    assert shadow_report.main(args(source, batch, tmp_path / "matching")) == 1
    path = batch / projection
    if projection.endswith("json"):
        data = shadow_report.read_json(path)
        data["records"][0]["topics_matched"] = 0
        write(path, data)
    else:
        path.write_text(path.read_text().replace("SYNTHETIC", "tampered"), encoding="utf-8")
        # 名称本身未必含大写标识，直接修改一个会议名称。
        path.write_text(path.read_text().replace("合成会议", "改动会议"), encoding="utf-8")
    output = tmp_path / "bad_projection"
    assert shadow_report.main(args(source, batch, output)) == 2
    assert not output.exists()


@pytest.mark.parametrize("which", ["template", "source_context"])
def test_candidate_context_fingerprint_binding(tmp_path, which):
    source, batch = setup_batch(tmp_path)
    path = batch / "synthetic-1/candidate.json"
    candidate = shadow_report.read_json(path)
    if which == "template":
        candidate["context"]["template_snapshot"]["name"] = "SYNTHETIC 改模板"
    else:
        candidate["context"]["source_fingerprint"] = "0" * 64
    write(path, candidate)
    output = tmp_path / "report"
    assert shadow_report.main(args(source, batch, output)) == 2 and not output.exists()


def test_report_cli_exit0_only_for_stage2_condition_calculation(tmp_path, monkeypatch):
    # SYNTHETIC 条件计算测试：只替换文件装载，不执行 real driver 或真实模型。
    # 通过固定 unit 向量的纯数据验证 CLI 0/1 分支，不形成真实影子证据。
    import importlib.util
    from mmwf.domain.shadow.models import thaw
    spec = importlib.util.spec_from_file_location("synthetic_shadow_vectors", ROOT / "tests/unit/test_shadow_scoring.py")
    vectors = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vectors)
    b, s, t = vectors.vector(20, "real")
    source = tmp_path / "input"
    source.mkdir()
    write(source / "scores.json", s)
    thresholds = {"format_version": 1, **{k: thaw(getattr(t, k)) for k in t.__dataclass_fields__}}
    write(source / "thresholds.json", thresholds)
    batch = tmp_path / "batch"
    batch.mkdir()
    monkeypatch.setattr(shadow_report, "load_batch", lambda *args: (b, [source, batch]))
    output = tmp_path / "condition-calculation"
    assert shadow_report.main(args(source, batch, output)) == 0
    assert shadow_report.read_json(output / "report.json")["eligible_for_stage2"]
    thresholds["agreement"]["confirmed"] = False
    write(source / "thresholds.json", thresholds)
    assert shadow_report.main(args(source, batch, tmp_path / "unconfirmed")) == 1


def test_unavailable_model_driver_does_not_fallback_and_report_keeps_failures(tmp_path, monkeypatch):
    # 仅合成元数据测试失败分支；available() 为假，不运行模型或 real 会议。
    from unittest.mock import Mock
    from mmwf.adapters import llm
    source = tmp_path / "input"
    shutil.copytree(FIXTURES, source)
    manifest = shadow_report.read_json(source / "manifest.json")
    manifest.update(dataset_kind="real", controlled_environment_confirmed=True,
                    operator_declaration="SYNTHETIC 条件计算测试，非真实作业",
                    production_data_dirs=[str(tmp_path / "production")],
                    model_config={"llm": {"provider": "ollama_local", "base_url": "http://127.0.0.1:11434", "model": "SYNTHETIC-unavailable"}})
    write(source / "manifest.json", manifest)
    client = Mock()
    client.available.return_value = False
    monkeypatch.setattr(llm, "make_llm_client", Mock(return_value=client))
    fake = Mock(side_effect=AssertionError("禁止 real 回退 Fake"))
    monkeypatch.setattr(llm, "FakeLlmClient", fake)
    batch = tmp_path / "batch"
    assert shadow_run.main(["--manifest", str(source / "manifest.json"), "--output", str(batch)]) == 1
    write(source / "scores.json", {"format_version": 1, "batch_id": manifest["batch_id"], "dataset_kind": "real", "scores": []})
    output = tmp_path / "report"
    assert shadow_report.main(args(source, batch, output)) == 2
    report = shadow_report.read_json(output / "report.json")
    assert len(report["meetings"]) == 2 and all(m["status"] == "failed" for m in report["meetings"])
    assert not report["eligible_for_stage2"] and report["status"] == "INCOMPLETE"
    assert not client.chat.called and not fake.called


def test_unfinished_raw_issue_collection_never_complete(tmp_path):
    source, batch = setup_batch(tmp_path)
    path = batch / "synthetic-1/issues.json"
    issues = shadow_report.read_json(path)
    issues["complete"] = False
    write(path, issues)
    output = tmp_path / "report"
    assert shadow_report.main(args(source, batch, output)) == 2
    report = shadow_report.read_json(output / "report.json")
    assert not report["complete"] and any("issues 检查未完成" in g for g in report["gaps"])
