"""人工风险标签的计数；不从诊断文案推断误报或误降级。"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from ._types import IndexedIssue, RiskCounts, RiskResult, IssueReview, cast
from mmwf.domain.minutes.issues import aggregate_issues, canonical_json
from .models import thaw


def index_issues(issues: Iterable[Mapping[str, object]]) -> list[IndexedIssue]:
    """每次只处理一场会议；身份只取规范四元组，复用领域去重。"""
    result: list[IndexedIssue] = []
    for issue in aggregate_issues(cast(Iterable[Mapping[str, object]], thaw(issues))):
        identity = [cast(Mapping[str, object], issue)[k] for k in ("source", "code", "target", "evidence")]
        result.append({**issue, "issue_id": hashlib.sha256(canonical_json(identity).encode()).hexdigest()})
    return result


def _counts(items: Sequence[IndexedIssue], reviews: Mapping[str, IssueReview], *,
            downgrade: bool = False) -> RiskCounts:
    labels = [reviews.get(i["issue_id"]) for i in items]
    true = sum(r is not None and r["verdict"] == "true_positive" for r in labels)
    false = sum(r is not None and r["verdict"] == "false_positive" for r in labels)
    uncertain = sum(r is not None and (r["verdict"] == "uncertain" or
                    (downgrade and r.get("false_downgrade") is None)) for r in labels)
    unreviewed = sum(r is None for r in labels)
    decided = [r for r in labels if r and r["verdict"] != "uncertain" and
               (not downgrade or type(r.get("false_downgrade")) is bool)]
    numerator = sum(cast(bool, r["false_downgrade"]) for r in decided) if downgrade else false
    denominator = len(decided)
    return {"total": len(items), "reviewed": denominator, "true_positive": true,
            "false_positive": false, "uncertain": uncertain, "unreviewed": unreviewed,
            "numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None,
            "coverage": denominator / len(items) if items else None,
            "complete": not uncertain and not unreviewed}


def aggregate_risks(issues: Iterable[Mapping[str, object]],
                    reviews: Iterable[Mapping[str, object]]) -> RiskResult:
    """一场会议的风险；跨会议的汇总保留各场独立身份。"""
    items = index_issues(issues)
    labels: dict[str, IssueReview] = {}
    for review in reviews:
        key = cast(str, review["issue_id"])
        if key in labels or key not in {i["issue_id"] for i in items}:
            raise ValueError("issue 审查重复或引用未知 issue_id")
        if review.get("verdict") not in ("true_positive", "false_positive", "uncertain"):
            raise ValueError("issue verdict 非法")
        if not isinstance(review.get("reason"), str) or not cast(str, review["reason"]).strip():
            raise ValueError("issue 审查须写理由")
        if review.get("false_downgrade") is not None and type(review["false_downgrade"]) is not bool:
            raise ValueError("false_downgrade 必须为 bool/null")
        labels[key] = cast(IssueReview, review)
    rules = [i for i in items if i["source"] in {"template", "consistency", "granularity"}]
    groups: defaultdict[tuple[str, str], list[IndexedIssue]] = defaultdict(list)
    for issue in rules:
        groups[(issue["source"], issue["code"])].append(issue)
    return {"issues": items,
            "downgrade": _counts([i for i in items if i["code"] == "ACTION_DOWNGRADED"], labels, downgrade=True),
            "rules": _counts(rules, labels),
            "groups": [{"source": s, "code": c, **_counts(group, labels)}
                       for (s, c), group in sorted(groups.items())],
            "complete": len(labels) == len(items) and all(r["verdict"] != "uncertain" for r in labels.values()) and
                        all(cast(Mapping[str, object], labels.get(i["issue_id"], {})).get("false_downgrade") is not None
                            for i in items if i["code"] == "ACTION_DOWNGRADED")}
