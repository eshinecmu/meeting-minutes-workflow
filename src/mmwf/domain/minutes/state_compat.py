"""旧图状态的副本适配；不读取检查点、数据库或文件。"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import cast

from mmwf.domain.extraction.segmentize import Segment, segmentize
from .issues import aggregate_issues, make_issue
from .template_application import from_graph_payload

META = ("title", "meeting_time", "location", "host", "attendees", "recorder")


def authoritative_segments(values: Mapping[str, object]) -> list[Segment] | None:
    """有真实分段时保留时间；否则由记录稿有损反解。"""
    transcript = cast(Sequence[Mapping[str, object]], values.get("transcript") or [])
    if not transcript:
        return None
    return [Segment(seq=cast(int, s.get("seq", i)), text=cast(str, s.get("text", "")),
                    speaker=cast(str | None, s.get("speaker")), start_ms=cast(int | None, s.get("start_ms")),
                    end_ms=cast(int | None, s.get("end_ms"))) for i, s in enumerate(transcript, 1)]


def upgrade_legacy_state(values: Mapping[str, object], context: Mapping[str, object] | None = None) -> dict[str, object]:
    """缺键合法；G2 三列表覆盖丰富快照，无法关联的待决策保留并提示。"""
    out = deepcopy(cast(dict[str, object], values))
    context = deepcopy(context or {})
    rich = deepcopy(cast(dict[str, object], out.get("minutes") or {}))
    old = deepcopy(cast(dict[str, object], out.get("minutes_structured") or {}))
    def nonempty(value: object) -> bool:
        return bool(value.strip()) if isinstance(value, str) else bool(value)
    meta = {k: next((deepcopy(v) for v in (rich.get(k),
                    cast(Mapping[str, object], context.get("meeting_meta", {})).get(k)) if nonempty(v)),
                    [] if k == "attendees" else "") for k in META}
    issues = [i for i in cast(Sequence[Mapping[str, object]], out.get("issues", out.get("validation_issues", [])) or [])
              if not (i.get("source") == "compat" and i.get("code") == "META_MISSING")]
    for key in META:
        if not meta[key]:
            issues.append(make_issue("META_MISSING", key, "会议资料缺失，请核对", source="compat"))
    if "topics" in out:
        canonical = from_graph_payload(out, meeting_meta=meta)
        previous = cast(list[dict[str, object]], rich.get("topics", []))
        matched_topics = set()
        for i, topic in enumerate(cast(list[dict[str, object]], canonical["topics"])):
            raw = cast(list[dict[str, object]], out.get("topics") or [])[i] if i < len(cast(list[dict[str, object]], out.get("topics") or [])) else {}
            match = next((t for t in previous if (
                raw.get("id") and raw.get("id") == t.get("id")) or (
                raw.get("seq") is not None and raw.get("seq") == t.get("seq")) or
                t.get("name") == topic.get("name")), None)
            if match:
                matched_topics.add(id(match))
                for key in ("discussion", "readings", "source_quote", "source_refs"):
                    if key not in topic and key in match:
                        topic[key] = deepcopy(match[key])
                topic["pending_decisions"] = deepcopy(match.get("pending_decisions", []))
            # 旧朴素标题实际呈现正文，不能当作免检导航。
            if "discussion" not in raw and not topic.get("discussion") and raw.get("title"):
                topic["discussion"] = raw["title"]
        for topic in previous:
            if topic.get("pending_decisions") and id(topic) not in matched_topics:
                cast(list[dict[str, object]], canonical["topics"]).append({**deepcopy(topic), "resolutions": [], "action_items": []})
                issues.append(make_issue("PENDING_UNMAPPED", "topics", "待决策事项无法关联，请核对", source="compat"))
    else:
        canonical = {**meta, **deepcopy(rich or old)}
        if not canonical.get("topics"):
            canonical["topics"] = [{"name": str(s.get("heading", "")),
                "discussion": str(item), "resolutions": [], "action_items": [], "pending_decisions": []}
                for s in cast(Sequence[Mapping[str, object]], old.get("sections", [])) for item in cast(Sequence[object], s.get("items", [])) if isinstance(item, str)]
    canonical["schema_version"] = rich.get("schema_version", "1.0")
    canonical["_context"] = {**context, **cast(Mapping[str, object], rich.get("_context", {}))}
    cast(dict[str, object], canonical["_context"])["topic_paths"] = {
        str(t.get("id") or t.get("seq") or t.get("name")): f"topics[{i}]"
        for i, t in enumerate(cast(Sequence[Mapping[str, object]], canonical.get("topics", [])))}
    segments = authoritative_segments(out)
    if segments is None:
        segments = segmentize(cast(str, out.get("record_markdown") or ""))[0]

    def adapt(entry: dict[str, object]) -> None:
        if "quote" in entry and "source_quote" not in entry:
            entry["source_quote"] = entry.pop("quote")
        if "refs" in entry and "source_refs" not in entry:
            refs = []
            for ref in cast(Sequence[dict[str, object]], entry.pop("refs") or []):
                ref = deepcopy(ref)
                # 只有时间码的旧引用须反查真实段落，绝不信任其 text。
                if not ref.get("quote") and ref.get("start_ms") is not None:
                    hits = [s for s in segments if s.start_ms == ref["start_ms"]]
                    if len(hits) == 1 and (not ref.get("segment_id") or
                            ref["segment_id"] == f"seg-{hits[0].seq}"):
                        s = hits[0]
                        ref = {"segment_id": f"seg-{s.seq}", "quote": s.text,
                               "start_ms": s.start_ms, "end_ms": s.end_ms}
                refs.append(ref)
            entry["source_refs"] = refs
        for kind in ("resolutions", "action_items", "pending_decisions"):
            for item in cast(Sequence[dict[str, object]], entry.get(kind, [])):
                adapt(item)
    for topic in cast(Sequence[dict[str, object]], canonical.get("topics", [])):
        adapt(topic)
    out["minutes"] = canonical
    out["issues"] = aggregate_issues(issues)
    if old:
        # 工作稿的已有业务编辑优先；不以三列表覆盖 G3 草稿。
        work = deepcopy(old)
        if not work.get("topics") or "name" not in cast(Sequence[Mapping[str, object]], work["topics"])[0]:
            work = deepcopy(canonical)
        for topic in cast(Sequence[dict[str, object]], work.get("topics", [])):
            adapt(topic)
        out["minutes_structured"] = work
    return out
