"""闸口展示与正文差异投影；只计算，不回写抽取结果。"""
from copy import deepcopy

MISSING_SOURCE = "暂无出处，请核对"


def related(topic, item):
    """只采用明确关联，不按位置猜测事项归属。"""
    return (topic.get("id") is not None and item.get("topic_id") == topic["id"]) or any(
        item.get(key) == topic.get("title") for key in ("topic", "topic_title")
        if item.get(key) is not None
    )


def topic_paragraphs(topic, state):
    parts = [topic.get("title", ""), topic.get("discussion", "")]
    parts.extend(topic.get("readings") or [])
    for name in ("resolutions", "action_items"):
        parts.extend(item.get("text", "") for item in state.get(name) or [] if related(topic, item))
    return [part for part in parts if part]


def system_text(gate, state):
    """G2 固定字段 / 数组顺序；无关联事项仍单独展示，避免漏掉可编辑正文。"""
    if gate == "G3":
        return state.get("minutes_markdown") or ""
    topics = state.get("topics") or []
    parts = [part for topic in topics for part in topic_paragraphs(topic, state)]
    for name in ("resolutions", "action_items"):
        parts.extend(item.get("text", "") for item in state.get(name) or []
                     if not any(related(topic, item) for topic in topics))
    return "\n\n".join(parts)


def display_topics(state, segments, gate_risk, threshold):
    """来源置信取已关联分段的最低值；缺失仍为 None，不造抽取置信。"""
    by_id = {s["segment_id"]: s for s in segments}
    result = deepcopy({name: state.get(name) or [] for name in ("topics", "resolutions", "action_items")})
    for topic in result["topics"]:
        basis = []
        for ref in topic.get("refs") or []:
            segment = by_id.get(ref.get("segment_id"))
            if segment is not None:
                basis.append({"segment_id": segment["segment_id"], "start_ms": segment.get("start_ms"),
                              "text": segment.get("text", ""), "confidence": segment.get("confidence")})
        known = [b["confidence"] for b in basis if b["confidence"] is not None]
        confidence = min(known) if known else None
        topic.update(title=topic.get("title", ""), extracted="\n\n".join(topic_paragraphs(topic, state)),
                     raw_text="\n".join(b["text"] for b in basis) or MISSING_SOURCE,
                     basis=basis, risk=topic.get("risk") or gate_risk,
                     confidence=confidence, low_confidence=confidence is not None and confidence < threshold,
                     confirmed=topic.get("confirmed") is True)
    return result
