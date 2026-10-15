"""E3 文件格式合同：只验合成输入，不作质量或放开结论。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import csv
import json
from pathlib import Path

import pytest

from mmwf.domain.shadow import (
    RECORD_FIELDS, SCORE_FIELDS, parse_manifest, parse_records, parse_scores,
)
import shadow_run

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/shadow/synthetic"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_manifest_and_score_are_frozen_typed_inputs():
    batch = parse_manifest(read("manifest.json"))
    assert batch.dataset_kind == "synthetic"
    assert len(batch.meetings) == 2
    with pytest.raises(FrozenInstanceError):
        batch.batch_id = "changed"
    with pytest.raises(TypeError):
        batch.meetings[0].meeting_meta["title"] = "changed"
    scores = parse_scores(read("scores.json"))
    assert len(scores) == 2
    with pytest.raises(TypeError):
        scores[0].data["reviewer"] = "changed"


@pytest.mark.parametrize("field,value", [
    ("duration_seconds", True), ("duration_seconds", 0), ("duration_seconds", -1),
    ("duration_seconds", 1.5), ("meeting_id", "../escape"), ("name", ""),
    ("meeting_kind", None), ("meeting_meta", []), ("source_version", None),
    ("fake_response_path", None),
])
def test_manifest_rejects_bad_meeting_fields(field, value):
    raw = read("manifest.json")
    raw["meetings"][0][field] = value
    with pytest.raises(ValueError):
        parse_manifest(raw)


def test_manifest_no_global_kind_enum_and_duplicate_rejected():
    raw = read("manifest.json")
    raw["meetings"][0]["meeting_kind"] = "单位自定类型"
    assert parse_manifest(raw).meetings[0].meeting_kind == "单位自定类型"
    raw["meetings"].append(deepcopy(raw["meetings"][0]))
    with pytest.raises(ValueError, match="重复"):
        parse_manifest(raw)


@pytest.mark.parametrize("field,value", [("format_version", True), ("format_version", 2), ("dataset_kind", "fake"), ("meetings", [])])
def test_manifest_envelope(field, value):
    raw = read("manifest.json")
    raw[field] = value
    with pytest.raises(ValueError):
        parse_manifest(raw)


def test_real_requires_controlled_environment_and_operator():
    raw = read("manifest.json")
    raw["dataset_kind"] = "real"
    with pytest.raises(ValueError, match="controlled_environment"):
        parse_manifest(raw)
    raw["controlled_environment_confirmed"] = True
    with pytest.raises(ValueError, match="operator"):
        parse_manifest(raw)
    raw["operator_declaration"] = "SYNTHETIC 条件计算声明，非真实运行"
    assert parse_manifest(raw).dataset_kind == "real"


@pytest.mark.parametrize("change", [
    {"reviewer": "合成撰写人甲"}, {"author_blind_to_candidate": False},
    {"statements_coverage_complete": None}, {"structure_compliant": 1},
    {"element_presence": {"title": True}}, {"required_elements": []},
    {"human_topic_ids": ["duplicate", "duplicate"]},
    {"topic_matches": [{"human_id": "extra", "candidate_id": "topics[0]", "evidence": "x"}]},
    {"divergences": None}, {"candidate_fingerprint": None},
    {"candidate_fingerprint": "bad-hash"}, {"reviewed_at": "not-a-date"},
    {"reviewed_at": "2026-01-01T00:00:00"},
    {"issue_reviews": [{"issue_id": "x", "verdict": "automatic", "reason": "bad"}]},
])
def test_score_basic_validation(change):
    raw = read("scores.json")
    raw["scores"][0].update(change)
    with pytest.raises(ValueError):
        parse_scores(raw)


def test_score_repeated_matches_and_unreviewed_statements_rejected():
    raw = read("scores.json")
    raw["scores"][0]["topic_matches"] *= 2
    with pytest.raises(ValueError, match="交集"):
        parse_scores(raw)
    raw = read("scores.json")
    raw["scores"][0]["statements"][0]["supported"] = None
    with pytest.raises(ValueError):
        parse_scores(raw)
    raw = read("scores.json")
    raw["scores"].append(deepcopy(raw["scores"][0]))
    with pytest.raises(ValueError, match="重复"):
        parse_scores(raw)


def test_empty_templates_and_runtime_formats_match(tmp_path):
    templates = ROOT / "docs/templates"
    with (templates / "T2-05-records.template.csv").open(encoding="utf-8", newline="") as f:
        assert list(csv.reader(f)) == [list(RECORD_FIELDS)]
    blank = json.loads((templates / "T2-05-records.template.json").read_text())
    assert blank["records"] == [] and blank["batch_id"] is None
    score = json.loads((templates / "T2-05-score.template.json").read_text())
    assert set(score["scores"][0]) == set(SCORE_FIELDS)
    assert all(value is None for value in score["scores"][0].values())
    score.update(batch_id="synthetic-e3", dataset_kind="synthetic")
    with pytest.raises(ValueError):
        parse_scores(score)
    output = tmp_path / "batch"
    shadow_run.run_batch(FIXTURES / "manifest.json", output)
    records = json.loads((output / "records.template.json").read_text())
    parsed = parse_records(records)
    assert len(parsed) == 2
    assert parsed[0]["elements_complete"] is None
    with (output / "records.template.csv").open(encoding="utf-8", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 2
    assert csv_rows[0]["elements_complete"] == ""
    assert set(csv_rows[0]) == set(RECORD_FIELDS)
    generated = json.loads((output / "synthetic-1/score.template.json").read_text())
    assert set(generated["scores"][0]) == set(SCORE_FIELDS)
    with pytest.raises(ValueError):
        parse_scores(generated)
    for field, value in (("topics_matched", -1), ("structure_compliant", 1), ("dataset_kind", "real")):
        bad = deepcopy(records)
        bad["records"][0][field] = value
        with pytest.raises(ValueError):
            parse_records(bad)


def test_csv_quotes_and_unicode_round_trip(tmp_path):
    # 用真实 driver 验证名称中的逗号、引号、换行按标准 CSV 转义。
    import shutil
    source = tmp_path / "input"
    shutil.copytree(FIXTURES, source)
    manifest = read("manifest.json")
    name = '合成,"会议"\n二'
    manifest["meetings"][0]["name"] = name
    (source / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "output"
    shadow_run.run_batch(source / "manifest.json", output)
    with (output / "records.template.csv").open(encoding="utf-8", newline="") as f:
        got = next(csv.DictReader(f))
    records = json.loads((output / "records.template.json").read_text())["records"]
    assert got["meeting_name"] == records[0]["meeting_name"] == name
    assert got["divergence_classes"] == ""
    assert records[0]["divergence_classes"] is None


@pytest.mark.parametrize("field,value", [
    ("required_elements_min", -0.1), ("topic_recall_min", 1.1),
    ("resolution_recall_min", True), ("topic_recall_min", float("nan")),
    ("topic_recall_min", float("inf")), ("ungrounded_max", .001),
    ("structure_min", .99999), ("required_elements_min", None),
])
def test_threshold_ranges_and_redlines(field, value):
    from mmwf.domain.shadow import parse_thresholds
    raw = read("thresholds.json")
    raw[field] = value
    with pytest.raises(ValueError):
        parse_thresholds(raw)


@pytest.mark.parametrize("change", [
    {"confirmed": None}, {"required_elements": []}, {"meeting_kinds": ["a"]},
    {"owner": ""}, {"long_meeting_boundary_seconds": 0},
])
def test_threshold_agreement_contract(change):
    from mmwf.domain.shadow import parse_thresholds
    raw = read("thresholds.json")
    raw["agreement"].update(change)
    with pytest.raises(ValueError):
        parse_thresholds(raw)


@pytest.mark.parametrize("change", [
    {"statements": [{"id": "s", "candidate_location": "x", "source_locations": [], "supported": True, "reason": "SYNTHETIC"}]},
    {"divergences_review_complete": False}, {"required_elements": ["x", "x"]},
    {"author": "合成第三方丙", "reviewer": " 合成第三方丙 "},
    {"statements": [{"id": "s", "candidate_location": "x", "source_locations": [], "supported": False, "reason": ""}]},
])
def test_full_score_declarations_and_manual_judgment_required(change):
    raw = read("scores.json")
    raw["scores"][0].update(change)
    with pytest.raises(ValueError):
        parse_scores(raw)


def test_shadow_domain_has_no_framework_state_schema_or_io_imports():
    import ast
    allowed_stdlib = {"__future__", "dataclasses", "datetime", "types", "collections", "re", "math", "hashlib", "fractions", "html", "json"}
    for path in (ROOT / "src/mmwf/domain/shadow").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    if isinstance(node, ast.ImportFrom) and node.level:
                        continue
                    assert name.split(".")[0] in allowed_stdlib or name.startswith("mmwf.domain.")
            if isinstance(node, ast.Name):
                assert node.id not in {"PipelineState", "MinutesSchema", "MinutesService", "MinutesStore", "open"}
