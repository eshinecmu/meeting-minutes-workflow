"""SYNTHETIC 人工风险标签，非真实质量结果。"""
from copy import deepcopy
import hashlib

import pytest
from mmwf.domain.minutes.issues import canonical_json, make_issue
from mmwf.domain.shadow import aggregate_risks, index_issues


def issue(code="ACTION_DOWNGRADED", source="granularity", target="topics[0]", evidence=None):
    return make_issue(code, target, "SYNTHETIC", source=source, evidence=evidence or {"original_path": "actions[0]"})


def review(item, verdict="true_positive", false_downgrade=False):
    return {"issue_id": index_issues([item])[0]["issue_id"], "verdict": verdict,
            "false_downgrade": false_downgrade, "reason": "SYNTHETIC 人工标签"}


def test_stable_identity_dedupe_keeps_tuple_and_message_is_not_identity():
    a = issue(evidence={"b": 2, "a": 1})
    b = {**a, "message": "换消息", "evidence": {"a": 1, "b": 2}}
    indexed = index_issues([a, b])
    assert len(indexed) == 1
    expected = hashlib.sha256(canonical_json([a[k] for k in ("source", "code", "target", "evidence")]).encode()).hexdigest()
    assert indexed[0]["issue_id"] == expected
    assert indexed[0]["evidence"] == a["evidence"]
    assert index_issues([issue(target="other")])[0]["issue_id"] != expected


def test_downgrade_and_rule_denominators_groups_and_coverage():
    items = [issue(target=f"topics[{i}]") for i in range(4)]
    items += [issue("NOISE", "template"), issue("MISMATCH", "consistency"), issue("OTHER", "extraction")]
    labels = [review(items[0], false_downgrade=True), review(items[1], "false_positive"),
              review(items[2], "uncertain", None), review(items[4], "false_positive"), review(items[5])]
    out = aggregate_risks(items + deepcopy(items), labels)
    assert (out["downgrade"]["numerator"], out["downgrade"]["denominator"]) == (1, 2)
    assert out["downgrade"]["total"] == 4 and out["downgrade"]["coverage"] == .5
    assert out["downgrade"]["unreviewed"] == 1 and out["downgrade"]["uncertain"] == 1
    assert (out["rules"]["numerator"], out["rules"]["denominator"]) == (2, 4)
    assert len(out["groups"]) == 3
    assert not out["complete"]


@pytest.mark.parametrize("labels", ["none", "uncertain", "null"])
def test_no_decided_downgrade_is_NA(labels):
    a = issue()
    reviews = [] if labels == "none" else [review(a, "uncertain" if labels == "uncertain" else "true_positive", None)]
    result = aggregate_risks([a], reviews)
    assert result["downgrade"]["rate"] is None and not result["complete"]
    assert result["downgrade"]["coverage"] == 0


def test_no_alert_is_NA_not_zero_risk_and_all_reviewed_errors_are_observations():
    out = aggregate_risks([], [])
    assert out["downgrade"]["rate"] is None and out["rules"]["rate"] is None
    assert out["rules"]["coverage"] is None
    a = issue()
    out = aggregate_risks([a], [review(a, "false_positive", True)])
    assert out["complete"] and out["downgrade"]["rate"] == 1 and out["rules"]["rate"] == 1


@pytest.mark.parametrize("bad", ["extra", "duplicate", "verdict", "bool", "reason"])
def test_bad_review_rejected(bad):
    a = issue()
    labels = [review(a)]
    if bad == "extra": labels[0]["issue_id"] = "unknown"
    if bad == "duplicate": labels *= 2
    if bad == "verdict": labels[0]["verdict"] = "automatic"
    if bad == "bool": labels[0]["false_downgrade"] = 1
    if bad == "reason": labels[0]["reason"] = ""
    with pytest.raises(ValueError):
        aggregate_risks([a], labels)
