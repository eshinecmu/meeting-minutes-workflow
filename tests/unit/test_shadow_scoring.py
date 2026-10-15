"""SYNTHETIC 条件计算测试；所有 real 标签仅为逻辑向量，非真实证据。"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from mmwf.domain.shadow import EvaluationBatch, aggregate_metrics, parse_thresholds, validate_scores
from mmwf.domain.shadow.models import freeze, thaw

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/shadow/synthetic"


def vector(n=2, kind="synthetic"):
    """纯合成满分输入；不启动模型、文件流水线或真实作业。"""
    threshold_raw = json.loads((FIXTURES / "thresholds.json").read_text())
    threshold_raw["agreement"]["confirmed"] = True
    thresholds = parse_thresholds(threshold_raw)
    base = json.loads((FIXTURES / "scores.json").read_text())["scores"][0]
    meetings, scores = [], []
    for i in range(n):
        mid = f"synthetic-{i}"
        row = deepcopy(base)
        row.update(meeting_id=mid, issue_reviews=[], divergences=[], supplementary=None,
                   element_presence={"title": True, "topics": True}, structure_compliant=True)
        row["statements"] = [{"id": "s1", "candidate_location": "candidate.json#/minutes/topics/0/discussion",
                               "source_locations": ["source.json#seg-1"], "supported": True, "reason": "SYNTHETIC 人工标签"}]
        candidate = {k: row[k] for k in ("candidate_fingerprint", "source_fingerprint", "template_fingerprint")}
        candidate["minutes"] = {"topics": [{"discussion": "SYNTHETIC正文", "resolutions": ["SYNTHETIC议定"], "pending_decisions": ["SYNTHETIC待议"]}]}
        meetings.append({"meeting_id": mid, "name": "SYNTHETIC 合成会议", "meeting_kind": thresholds.agreement["meeting_kinds"][i % 4],
                         "duration_seconds": 7200 if i % 2 else 600, "status": "generated", "failure_reason": "",
                         "candidate": candidate, "issues": [],
                         "documents": {"candidate.json": candidate, "source.json": [{"seq": 1, "text": "SYNTHETIC源"}]}})
        scores.append(row)
    return (EvaluationBatch("synthetic-test", kind, tuple(freeze(m) for m in meetings), freeze({})),
            {"format_version": 1, "batch_id": "synthetic-test", "dataset_kind": kind, "scores": scores}, thresholds)


def calculate(args):
    return aggregate_metrics(*args).data


def test_micro_not_mean_and_raw_counts():
    batch, scores, t = vector()
    scores["scores"][0].update(human_topic_ids=[f"h{i}" for i in range(9)], topic_matches=[])
    result = calculate((batch, scores, t))
    assert result["metrics"]["topic_recall"]["numerator"] == 1
    assert result["metrics"]["topic_recall"]["denominator"] == 10
    assert result["metrics"]["topic_recall"]["value"] == .1
    assert result["metrics"]["required_elements"]["denominator"] == 2
    assert result["metrics"]["ungrounded"]["denominator"] == 2


@pytest.mark.parametrize("metric,field,num,den,passed", [
    ("required_elements", "required_elements_min", 95, 100, True),
    ("required_elements", "required_elements_min", 94999, 100000, False),
    ("topic_recall", "topic_recall_min", 90, 100, True),
    ("topic_recall", "topic_recall_min", 89999, 100000, False),
    ("resolution_recall", "resolution_recall_min", 85, 100, True),
    ("resolution_recall", "resolution_recall_min", 84999, 100000, False),
    ("structure", "structure_min", 1, 1, True), ("structure", "structure_min", 999, 1000, False),
    ("ungrounded", "ungrounded_max", 0, 10, True), ("ungrounded", "ungrounded_max", 1, 100000, False),
])
def test_exact_integer_threshold_boundaries(metric, field, num, den, passed):
    # 直接验证聚合使用的整数比较器，另以下场景核对实际会议分母。
    from mmwf.domain.shadow.scoring import _metric
    t = vector(0)[2]
    assert _metric(num, den, getattr(t, field), maximum=metric == "ungrounded")["passed"] is passed


def test_elements_95_percent_and_structure_require_every_meeting():
    b, s, t = vector(20)
    s["scores"][0]["element_presence"]["title"] = False
    assert calculate((b, s, t))["metrics"]["required_elements"]["passed"]
    s["scores"][1]["element_presence"]["title"] = False
    assert not calculate((b, s, t))["metrics"]["required_elements"]["passed"]
    s["scores"][0]["structure_compliant"] = False
    assert not calculate((b, s, t))["metrics"]["structure"]["passed"]


@pytest.mark.parametrize("zero", ["meetings", "topics", "resolutions", "statements"])
def test_zero_batch_denominators_never_pass(zero):
    b, s, t = vector(0 if zero == "meetings" else 2)
    for row in s["scores"]:
        if zero in ("topics", "resolutions"):
            key = "topic" if zero == "topics" else "resolution"
            row[f"human_{key}_ids"] = []
            row[f"{key}_matches"] = []
        elif zero == "statements":
            row["statements"] = []
    out = calculate((b, s, t))
    assert out["status"] == "NOT_EVALUABLE"
    assert not out["quality_pass"] and not out["eligible_for_stage2"]


def test_single_zero_row_still_participates_and_missing_keeps_denominator():
    b, s, t = vector()
    s["scores"][0].update(human_topic_ids=[], topic_matches=[])
    assert calculate((b, s, t))["metrics"]["topic_recall"]["denominator"] == 1
    s["scores"].pop()
    out = calculate((b, s, t))
    assert out["status"] == "INCOMPLETE" and len(out["records"]) == 2
    assert out["records"][1]["topics_matched"] is None
    assert out["metrics"]["required_elements"]["denominator"] == 2
    assert out["metrics"]["required_elements"]["passed"] is None


@pytest.mark.parametrize("cls,passed", [("更激进", False), ("系统错误", False), ("更保守", True), (None, True)])
def test_divergence_AND_counts_items_and_meetings(cls, passed):
    b, s, t = vector(20)
    if cls:
        s["scores"][0]["divergences"] = [{"id": f"d{i}", "class": cls,
            "location": "candidate.json#/minutes/topics/0", "reason": "SYNTHETIC"} for i in range(2)]
    out = calculate((b, s, t))
    assert out["quality_pass"] is passed
    if cls:
        assert out["divergences"][cls]["count"] == 2 and out["divergences"][cls]["meeting_count"] == 1
    assert not out["eligible_for_stage2"] and out["real_meetings_counted"] == 0 and out["real_run_status"] == "PENDING"


@pytest.mark.parametrize("condition", ["all", "19", "kind", "short", "long", "agreement", "quality", "failed"])
def test_real_stage2_conditions_SYNTHETIC_METADATA_ONLY(condition):
    b, s, t = vector(19 if condition == "19" else 20, "real")
    meetings = [thaw(m) for m in b.meetings]
    if condition == "kind":
        for m in meetings:
            m["meeting_kind"] = "regular_meeting"
    if condition in ("short", "long"):
        for m in meetings:
            m["duration_seconds"] = 7200 if condition == "short" else 600
    if condition == "agreement":
        t = replace(t, agreement=freeze({**t.agreement, "confirmed": False}))
    if condition == "quality":
        s["scores"][0]["statements"][0].update(supported=False, source_locations=[])
    if condition == "failed":
        meetings[0]["status"] = "failed"
    b = replace(b, meetings=tuple(freeze(m) for m in meetings))
    out = calculate((b, s, t))
    assert out["eligible_for_stage2"] is (condition == "all")
    if condition == "quality":
        assert any("阶段 1" in g for g in out["gaps"])


@pytest.mark.parametrize("bad", ["batch", "kind", "extra", "duplicate", "fingerprint", "source", "candidate", "match", "pending", "evidence", "divergence", "required"])
def test_score_binding_and_locations_reject(bad):
    b, s, t = vector()
    r = s["scores"][0]
    if bad == "batch": s["batch_id"] = "other"
    elif bad == "kind": s["dataset_kind"] = "real"
    elif bad == "extra": r["meeting_id"] = "extra"
    elif bad == "duplicate": s["scores"].append(deepcopy(r))
    elif bad == "fingerprint": r["candidate_fingerprint"] = "0" * 64
    elif bad == "source": r["statements"][0]["source_locations"] = ["source.json#seg-999"]
    elif bad == "candidate": r["statements"][0]["candidate_location"] = "candidate.json#/minutes/nope"
    elif bad == "match": r["topic_matches"][0]["candidate_id"] = "topics[999]"
    elif bad == "pending": r["resolution_matches"][0]["candidate_id"] = "topics[0].pending_decisions[0]"
    elif bad == "evidence": r["topic_matches"][0]["evidence"] = "candidate.json#/minutes/topics/0/discussion"
    elif bad == "divergence": r["divergences"] = [{"id": "d1", "class": "更激进", "location": "candidate.json#/minutes/missing", "reason": "SYNTHETIC"}]
    elif bad == "required": r["required_elements"] = ["x"]; r["element_presence"] = {"x": True}
    with pytest.raises(ValueError):
        aggregate_metrics(b, s, t)


def test_true_reference_can_be_human_unsupported_and_divergence_independent():
    b, s, t = vector()
    s["scores"][0]["statements"][0]["supported"] = False
    out = calculate((b, s, t))
    assert out["metrics"]["ungrounded"]["numerator"] == 1
    assert out["divergences"]["更激进"]["count"] == 0
    assert not out["quality_pass"]


def test_same_issue_cross_meetings_not_merged_and_optional_population_is_separate():
    from mmwf.domain.minutes.issues import make_issue
    from mmwf.domain.shadow import index_issues
    b, s, t = vector()
    issue = make_issue("ACTION_DOWNGRADED", "topics[0]", "SYNTHETIC", source="granularity")
    iid = index_issues([issue])[0]["issue_id"]
    meetings = [thaw(m) for m in b.meetings]
    for m, score in zip(meetings, s["scores"]):
        m["issues"] = [issue, issue]
        score["issue_reviews"] = [{"issue_id": iid, "verdict": "false_positive", "false_downgrade": True, "reason": "SYNTHETIC"}]
        score["supplementary"] = {"real_action_ids": ["a1", "a2", "a3"],
            "downgrade_action_matches": [{"issue_id": iid, "action_id": "a1"}]}
    out = calculate((replace(b, meetings=tuple(freeze(m) for m in meetings)), s, t))
    assert out["risks"]["downgrade"]["numerator"] == out["risks"]["downgrade"]["denominator"] == 2
    assert out["risks"]["meetings"][0]["action_population"]["denominator"] == 3
    assert out["risks"]["meetings"][0]["action_population"]["rate"] == 1/3
    assert out["risks"]["action_population"]["numerator"] == 2
    assert out["risks"]["action_population"]["denominator"] == 6
    assert out["quality_pass"]  # 风险无上游合格阈值，不因误报/误降级观测自动否决质量。


@pytest.mark.parametrize("field", ["candidate_fingerprint", "source_fingerprint", "template_fingerprint"])
def test_all_version_bindings_reject_old_score(field):
    b, s, t = vector()
    s["scores"][0][field] = "0" * 64
    with pytest.raises(ValueError, match="不匹配"):
        validate_scores(b, s)


@pytest.mark.parametrize("invalid", ["duplicate", "unknown", "partial"])
def test_optional_action_population_relationship_validation(invalid):
    b, s, t = vector()
    s["scores"][0]["supplementary"] = {"real_action_ids": ["a", "a"] if invalid == "duplicate" else ["a"]}
    if invalid == "unknown":
        s["scores"][0]["supplementary"]["downgrade_action_matches"] = [{"issue_id": "unknown", "action_id": "a"}]
    with pytest.raises(ValueError):
        aggregate_metrics(b, s, t)
