"""影子作业文件 v1：仅验证显式数据，不读文件、不自动判卷。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from collections.abc import Mapping, Sequence
from ._types import _MeetingArgs, _ThresholdArgs, cast, overload
import re

WARNING = "影子生成，禁止交付"
SYNTHETIC = "SYNTHETIC / 合成数据，非真实验收结果"
RECORD_FIELDS = (
    "sequence", "meeting_name", "meeting_kind", "duration_seconds",
    "elements_complete", "topics_matched", "topics_human", "resolutions_matched",
    "resolutions_human", "ungrounded_count", "statement_count", "structure_compliant",
    "divergence_classes", "notes", "meeting_id", "batch_id", "dataset_kind", "score_path",
)
SCORE_FIELDS = (
    "meeting_id", "candidate_fingerprint", "source_fingerprint", "template_fingerprint",
    "human_final_fingerprint", "reviewer", "author", "reviewed_at", "author_blind_to_candidate",
    "required_elements", "element_presence", "human_topic_ids", "topic_matches",
    "human_resolution_ids", "resolution_matches", "statements", "statements_coverage_complete",
    "statement_exclusion_policy", "structure_compliant", "structure_evidence", "divergences",
    "divergences_review_complete", "issue_reviews", "supplementary",
)


@overload
def freeze(value: Mapping[str, object]) -> Mapping[str, object]: ...
@overload
def freeze(value: object) -> object: ...

def freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


@overload
def thaw(value: Mapping[str, object]) -> dict[str, object]: ...
@overload
def thaw(value: object) -> object: ...


def thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


def text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def identifier(value: object, field: str) -> str:
    value = text(value, field)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError(f"{field} 非法标识或路径穿越")
    return value


def integer(value: object, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} 必须是 >= {minimum} 的整数")
    return value


def envelope(raw: object) -> None:
    if not isinstance(raw, dict) or type(raw.get("format_version")) is not int or raw["format_version"] != 1:
        raise ValueError("format_version 必须为整数 1")
    identifier(raw.get("batch_id"), "batch_id")
    if raw.get("dataset_kind") not in ("synthetic", "real"):
        raise ValueError("dataset_kind 必须为 synthetic 或 real")


@dataclass(frozen=True)
class ShadowMeeting:
    meeting_id: str
    name: str
    meeting_kind: str
    duration_seconds: int
    record_path: str
    segments_path: str
    meeting_meta: Mapping[str, object]
    template_snapshot_path: str
    source_version: str
    fake_response_path: str | None = None


@dataclass(frozen=True)
class ShadowBatch:
    batch_id: str
    dataset_kind: str
    meetings: tuple[ShadowMeeting, ...]
    metadata: Mapping[str, object]


def parse_manifest(raw: object) -> ShadowBatch:
    envelope(raw)
    raw = cast(dict[str, object], raw)
    if raw["dataset_kind"] == "real":
        if raw.get("controlled_environment_confirmed") is not True:
            raise ValueError("real 必须声明 controlled_environment_confirmed=true")
        text(raw.get("operator_declaration"), "operator_declaration")
    if not isinstance(raw.get("meetings"), list) or not raw["meetings"]:
        raise ValueError("meetings 必须为非空数组")
    meetings: list[ShadowMeeting] = []
    seen: set[str] = set()
    for row in cast(list[dict[str, object]], raw["meetings"]):
        if not isinstance(row, dict):
            raise ValueError("meeting 必须为对象")
        mid = identifier(row.get("meeting_id"), "meeting_id")
        if mid in seen:
            raise ValueError("meeting_id 重复")
        seen.add(mid)
        for field in ("name", "meeting_kind", "record_path", "segments_path", "template_snapshot_path", "source_version"):
            text(row.get(field), field)
        integer(row.get("duration_seconds"), "duration_seconds", 1)
        if not isinstance(row.get("meeting_meta"), dict):
            raise ValueError("meeting_meta 必须为对象")
        if raw["dataset_kind"] == "synthetic":
            text(row.get("fake_response_path"), "fake_response_path")
        meetings.append(ShadowMeeting(**cast("_MeetingArgs", {k: freeze(row[k]) for k in ShadowMeeting.__dataclass_fields__ if k in row})))
    boundary = raw.get("long_meeting_boundary_seconds")
    if boundary is not None:
        integer(boundary, "long_meeting_boundary_seconds", 1)
    return ShadowBatch(cast(str, raw["batch_id"]), cast(str, raw["dataset_kind"]), tuple(meetings),
                       freeze({k: v for k, v in raw.items() if k != "meetings"}))


@dataclass(frozen=True)
class MeetingScore:
    """已通过 E3 基本格式校验；不表示 E4 的版本/定位完整性已通过。"""
    meeting_id: str
    data: Mapping[str, object]


def parse_scores(raw: object) -> tuple[MeetingScore, ...]:
    envelope(raw)
    raw = cast(dict[str, object], raw)
    if not isinstance(raw.get("scores"), list):
        raise ValueError("scores 必须为数组")
    out: list[MeetingScore] = []
    seen: set[str] = set()
    for row in cast(list[dict[str, object]], raw["scores"]):
        if not isinstance(row, dict) or any(k not in row or row[k] is None for k in SCORE_FIELDS if k != "supplementary"):
            raise ValueError("评分字段缺失或尚未填写")
        mid = identifier(row["meeting_id"], "meeting_id")
        if mid in seen:
            raise ValueError("评分 meeting_id 重复")
        seen.add(mid)
        for k in SCORE_FIELDS[:8]:
            text(row[k], k)
        for k in ("candidate_fingerprint", "source_fingerprint", "template_fingerprint", "human_final_fingerprint"):
            if not re.fullmatch(r"[0-9a-f]{64}", cast(str, row[k])):
                raise ValueError(f"{k} 必须为 SHA-256 十六进制指纹")
        try:
            reviewed_at = datetime.fromisoformat(cast(str, row["reviewed_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reviewed_at 必须为带时区的 ISO 时间") from exc
        if reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at 必须带时区")
        if cast(str, row["reviewer"]).strip() == cast(str, row["author"]).strip() or row["author_blind_to_candidate"] is not True:
            raise ValueError("必须独立盲写与第三方评分")
        for key in ("required_elements", "human_topic_ids", "human_resolution_ids"):
            items = row[key]
            if not isinstance(items, list) or any(not isinstance(i, str) or not i.strip() for i in items) or len(set(items)) != len(items):
                raise ValueError(f"{key} 必须为唯一字符串数组")
        presence = row["element_presence"]
        if not row["required_elements"] or not isinstance(presence, dict) or set(presence) != set(cast(Sequence[str], row["required_elements"])) or any(type(v) is not bool for v in presence.values()):
            raise ValueError("element_presence 键集与布尔值不合约")
        for kind in ("topic", "resolution"):
            matches = row[f"{kind}_matches"]
            if not isinstance(matches, list):
                raise ValueError("matches 必须为数组")
            ids = []
            for match in matches:
                if not isinstance(match, dict):
                    raise ValueError("match 必须为对象")
                for k in ("human_id", "candidate_id", "evidence"):
                    text(match.get(k), k)
                ids.append(match["human_id"])
            if len(ids) != len(set(ids)) or not set(ids) <= set(cast(Sequence[str], row[f"human_{kind}_ids"])):
                raise ValueError("人工交集重复或超出总数")
        for k in ("statements_coverage_complete", "divergences_review_complete"):
            if row[k] is not True:
                raise ValueError(f"{k} 必须明确声明完成")
        if type(row["structure_compliant"]) is not bool:
            raise ValueError("structure_compliant 必须为布尔值")
        for k in ("structure_evidence", "statement_exclusion_policy"):
            text(row[k], k)
        for key in ("statements", "divergences", "issue_reviews"):
            if not isinstance(row[key], list):
                raise ValueError(f"{key} 必须为数组")
            ids = []
            for item in cast(list[dict[str, object]], row[key]):
                if not isinstance(item, dict):
                    raise ValueError(f"{key} 条目必须为对象")
                id_key = "issue_id" if key == "issue_reviews" else "id"
                ids.append(text(item.get(id_key), id_key))
                text(item.get("reason"), "reason")
                if key == "statements":
                    text(item.get("candidate_location"), "candidate_location")
                    if type(item.get("supported")) is not bool or not isinstance(item.get("source_locations"), list):
                        raise ValueError("陈述必须逐条标记布尔支持与源定位数组")
                    if any(not isinstance(loc, str) or not loc.strip() for loc in cast(list[object], item["source_locations"])):
                        raise ValueError("source_locations 必须为非空定位字符串数组")
                    if item["supported"] and not item["source_locations"]:
                        raise ValueError("supported=true 必须填写源定位")
                elif key == "divergences":
                    text(item.get("location"), "location")
                    if item.get("class") not in ("更保守", "更激进", "系统错误"):
                        raise ValueError("分歧分类非法")
                else:
                    if item.get("verdict") not in ("true_positive", "false_positive", "uncertain"):
                        raise ValueError("issue verdict 非法")
                    if item.get("false_downgrade") is not None and type(item["false_downgrade"]) is not bool:
                        raise ValueError("false_downgrade 必须为 bool/null")
            if len(ids) != len(set(ids)):
                raise ValueError(f"{key} id 重复")
        if row["supplementary"] is not None and not isinstance(row["supplementary"], dict):
            raise ValueError("supplementary 必须为对象或 null")
        out.append(MeetingScore(mid, freeze(row)))
    return tuple(out)


def score_template() -> dict[str, object]:
    return {k: None for k in SCORE_FIELDS}


def record_template() -> dict[str, object]:
    return {k: None for k in RECORD_FIELDS}


def parse_records(raw: object) -> tuple[Mapping[str, object], ...]:
    """记录表投影的基本类型校验；与权威 scores 的逐行核对留给 E4。"""
    envelope(raw)
    raw = cast(dict[str, object], raw)
    if not isinstance(raw.get("records"), list):
        raise ValueError("records 必须为数组")
    seen: set[str] = set()
    for sequence, row in enumerate(cast(list[dict[str, object]], raw["records"]), 1):
        if not isinstance(row, dict) or set(row) != set(RECORD_FIELDS):
            raise ValueError("记录表字段不合约")
        if type(row["sequence"]) is not int or row["sequence"] != sequence:
            raise ValueError("sequence 必须为连续整数")
        mid = identifier(row["meeting_id"], "meeting_id")
        if mid in seen:
            raise ValueError("记录表 meeting_id 重复")
        seen.add(mid)
        if row["batch_id"] != raw["batch_id"] or row["dataset_kind"] != raw["dataset_kind"]:
            raise ValueError("记录表混批")
        for k in ("meeting_name", "meeting_kind", "score_path"):
            text(row[k], k)
        integer(row["duration_seconds"], "duration_seconds", 1)
        for k in ("elements_complete", "structure_compliant"):
            if row[k] is not None and type(row[k]) is not bool:
                raise ValueError(f"{k} 必须为 bool/null")
        for k in ("topics_matched", "topics_human", "resolutions_matched", "resolutions_human", "ungrounded_count", "statement_count"):
            if row[k] is not None:
                integer(row[k], k)
        for a, b in (("topics_matched", "topics_human"), ("resolutions_matched", "resolutions_human"), ("ungrounded_count", "statement_count")):
            if row[a] is not None and row[b] is not None and cast(int, row[a]) > cast(int, row[b]):
                raise ValueError("记录表分子超过分母")
        classes = row["divergence_classes"]
        if classes is not None and (not isinstance(classes, list) or any(c not in ("更保守", "更激进", "系统错误") for c in classes)):
            raise ValueError("divergence_classes 必须为分类数组或 null")
        if not isinstance(row["notes"], str):
            raise ValueError("notes 必须为字符串")
    return tuple(freeze(row) for row in cast(list[dict[str, object]], raw["records"]))


@dataclass(frozen=True)
class EvaluationBatch:
    """文件层已装载的批次及定位文档；领域函数不接触路径或文件。"""
    batch_id: str
    dataset_kind: str
    meetings: tuple[Mapping[str, object], ...]
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class Thresholds:
    required_elements_min: float
    topic_recall_min: float
    resolution_recall_min: float
    ungrounded_max: float
    structure_min: float
    agreement: Mapping[str, object]


@dataclass(frozen=True)
class Metric:
    numerator: int
    denominator: int
    value: float | None
    threshold: float
    passed: bool | None


@dataclass(frozen=True)
class ShadowSummary:
    data: Mapping[str, object]


def parse_thresholds(raw: object) -> Thresholds:
    """阈值是显式工具输入，不读取或修改项目配置。"""
    import math
    fields = tuple(Thresholds.__dataclass_fields__)
    if not isinstance(raw, dict) or set(raw) != {"format_version", *fields} or type(raw["format_version"]) is not int or raw["format_version"] != 1:
        raise ValueError("thresholds 字段不合约")
    for key in fields[:-1]:
        v = raw[key]
        if type(v) not in (int, float) or not math.isfinite(cast(float, v)) or not 0 <= cast(float, v) <= 1:
            raise ValueError(f"{key} 必须为 [0,1] 有限比例")
    if raw["ungrounded_max"] != 0 or raw["structure_min"] != 1:
        raise ValueError("无依据与结构红线不得放宽")
    a = raw["agreement"]
    if not isinstance(a, dict):
        raise ValueError("agreement 必须为对象")
    for key in ("id", "date", "owner"):
        text(a.get(key), f"agreement.{key}")
    if type(a.get("confirmed")) is not bool:
        raise ValueError("agreement.confirmed 必须为布尔值")
    for key in ("required_elements", "meeting_kinds"):
        v = a.get(key)
        if not isinstance(v, list) or not v or any(not isinstance(i, str) or not i.strip() for i in v) or len(v) != len(set(v)):
            raise ValueError(f"agreement.{key} 必须为唯一非空数组")
    if len(a["meeting_kinds"]) != 4:
        raise ValueError("agreement 必须约定四类会议或其映射")
    integer(a.get("long_meeting_boundary_seconds"), "long_meeting_boundary_seconds", 1)
    return Thresholds(**cast("_ThresholdArgs", {k: freeze(raw[k]) for k in fields}))
