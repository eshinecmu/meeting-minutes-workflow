"""Internal typing views for shadow wire data and aggregation results.

Keeping typing support here lets the domain modules retain their existing
runtime import boundary. These declarations add no parsing or coercion.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypedDict, NotRequired
from typing import cast as cast, overload as overload

from mmwf.domain.minutes.issues import Issue


# These views describe the existing wire fields. Runtime parsers still validate
# untrusted objects below; casts do not coerce or fill in any input values.
class Match(TypedDict):
    human_id: str
    candidate_id: str
    evidence: str


class Statement(TypedDict):
    id: str
    reason: str
    candidate_location: str
    supported: bool
    source_locations: Sequence[str]


class IssueReview(TypedDict):
    issue_id: str
    verdict: str
    reason: str
    false_downgrade: NotRequired[bool | None]


class ActionMatch(TypedDict):
    issue_id: str
    action_id: str


class Supplementary(TypedDict, total=False):
    real_action_ids: Sequence[str]
    downgrade_action_matches: Sequence[ActionMatch]


class ScoreData(TypedDict):
    meeting_id: str
    candidate_fingerprint: str
    source_fingerprint: str
    template_fingerprint: str
    human_final_fingerprint: str
    reviewer: str
    author: str
    reviewed_at: str
    author_blind_to_candidate: bool
    required_elements: Sequence[str]
    element_presence: Mapping[str, bool]
    human_topic_ids: Sequence[str]
    topic_matches: Sequence[Match]
    human_resolution_ids: Sequence[str]
    resolution_matches: Sequence[Match]
    statements: Sequence[Statement]
    statements_coverage_complete: bool
    statement_exclusion_policy: str
    structure_compliant: bool
    structure_evidence: str
    divergences: Sequence[Mapping[str, str]]
    divergences_review_complete: bool
    issue_reviews: Sequence[IssueReview]
    supplementary: Supplementary | None


class Agreement(TypedDict):
    id: str
    date: str
    owner: str
    confirmed: bool
    required_elements: Sequence[str]
    meeting_kinds: Sequence[str]
    long_meeting_boundary_seconds: int


class EvaluationMeeting(TypedDict):
    meeting_id: str
    name: str
    meeting_kind: str
    duration_seconds: int
    status: str
    candidate: Mapping[str, object]
    documents: Mapping[str, object]
    issues: Sequence[Mapping[str, object]]
    issues_complete: NotRequired[bool]
    failure_reason: NotRequired[str]


class Record(TypedDict):
    sequence: int
    meeting_name: str
    meeting_kind: str
    duration_seconds: int
    meeting_id: str
    batch_id: str
    dataset_kind: str
    score_path: str
    notes: str
    elements_complete: bool | None
    topics_matched: int | None
    topics_human: int | None
    resolutions_matched: int | None
    resolutions_human: int | None
    ungrounded_count: int | None
    statement_count: int | None
    structure_compliant: bool | None
    divergence_classes: list[str] | None


class _MeetingArgs(TypedDict):
    meeting_id: str
    name: str
    meeting_kind: str
    duration_seconds: int
    record_path: str
    segments_path: str
    meeting_meta: Mapping[str, object]
    template_snapshot_path: str
    source_version: str
    fake_response_path: NotRequired[str | None]


class _ThresholdArgs(TypedDict):
    required_elements_min: float
    topic_recall_min: float
    resolution_recall_min: float
    ungrounded_max: float
    structure_min: float
    agreement: Mapping[str, object]


class IndexedIssue(Issue):
    issue_id: str


class RiskCounts(TypedDict):
    total: int
    reviewed: int
    true_positive: int
    false_positive: int
    uncertain: int
    unreviewed: int
    numerator: int
    denominator: int
    rate: float | None
    coverage: float | None
    complete: bool


class RiskGroup(RiskCounts):
    source: str
    code: str


class ActionPopulation(TypedDict):
    status: str
    numerator: int | None
    denominator: int | None
    rate: float | None
    real_action_ids: NotRequired[list[str]]
    downgrade_action_matches: NotRequired[object]


class RiskResult(TypedDict):
    issues: list[IndexedIssue]
    downgrade: RiskCounts
    rules: RiskCounts
    groups: list[RiskGroup]
    complete: bool
    meeting_id: NotRequired[str]
    action_population: NotRequired[ActionPopulation]


class MetricData(TypedDict):
    numerator: int
    denominator: int
    value: float | None
    threshold: float
    passed: bool | None


class _EmptyRecordMetrics(TypedDict):
    elements_complete: None
    topics_matched: None
    topics_human: None
    resolutions_matched: None
    resolutions_human: None
    ungrounded_count: None
    statement_count: None
    structure_compliant: None
    divergence_classes: None


class ProjectedRecords(TypedDict):
    format_version: int
    batch_id: str
    dataset_kind: str
    labels: list[str]
    records: list[dict[str, object]]
