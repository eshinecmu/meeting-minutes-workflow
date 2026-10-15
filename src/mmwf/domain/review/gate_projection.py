"""闸口展示与正文差异投影；只计算，不回写抽取结果。"""
from copy import deepcopy
from typing import Mapping, Sequence, TypedDict, cast


class _Basis(TypedDict):
    segment_id: str
    start_ms: int | None
    text: str
    confidence: float | None


MISSING_SOURCE = "暂无出处，请核对"


def related(topic: Mapping[str, object], item: Mapping[str, object]) -> bool:
    """只采用明确关联，不按位置猜测事项归属。"""
    return (topic.get("id") is not None and item.get("topic_id") == topic["id"]) or any(
        item.get(key) == topic.get("title") for key in ("topic", "topic_title")
        if item.get(key) is not None
    )


def topic_paragraphs(topic: Mapping[str, object], state: Mapping[str, object]) -> list[str]:
    parts = [cast(str, topic.get("title", "")), cast(str, topic.get("discussion", ""))]
    parts.extend(cast(list[str], topic.get("readings")) or [])
    for name in ("resolutions", "action_items"):
        parts.extend(cast(str, item.get("text", "")) for item in cast(list[dict[str, object]], state.get(name)) or [] if related(topic, item))
    return [part for part in parts if part]


def system_text(gate: str, state: Mapping[str, object]) -> str:
    """G2 固定字段 / 数组顺序；无关联事项仍单独展示，避免漏掉可编辑正文。"""
    if gate == "G3":
        return cast(str, state.get("minutes_markdown")) or ""
    topics = cast(list[dict[str, object]], state.get("topics")) or []
    parts = [part for topic in topics for part in topic_paragraphs(topic, state)]
    for name in ("resolutions", "action_items"):
        parts.extend(cast(str, item.get("text", "")) for item in cast(list[dict[str, object]], state.get(name)) or []
                     if not any(related(topic, item) for topic in topics))
    return "\n\n".join(parts)


def display_topics(state: Mapping[str, object], segments: Sequence[Mapping[str, object]],
                   gate_risk: str, threshold: float) -> dict[str, list[dict[str, object]]]:
    """来源置信取已关联分段的最低值；缺失仍为 None，不造抽取置信。"""
    by_id = {s["segment_id"]: s for s in segments}
    result = deepcopy({name: cast(list[dict[str, object]], state.get(name)) or [] for name in ("topics", "resolutions", "action_items")})
    for topic in result["topics"]:
        basis: list[_Basis] = []
        for ref in cast(list[Mapping[str, object]], topic.get("refs")) or []:
            segment = by_id.get(ref.get("segment_id"))
            if segment is not None:
                basis.append({"segment_id": cast(str, segment["segment_id"]), "start_ms": cast(int | None, segment.get("start_ms")),
                              "text": cast(str, segment.get("text", "")), "confidence": cast(float | None, segment.get("confidence"))})
        known = [b["confidence"] for b in basis if b["confidence"] is not None]
        confidence = min(known) if known else None
        topic.update(title=cast(str, topic.get("title", "")), extracted="\n\n".join(topic_paragraphs(topic, state)),
                     raw_text="\n".join(b["text"] for b in basis) or MISSING_SOURCE,
                     basis=basis, risk=topic.get("risk") or gate_risk,
                     confidence=confidence, low_confidence=confidence is not None and confidence < threshold,
                     confirmed=topic.get("confirmed") is True)
    return result
