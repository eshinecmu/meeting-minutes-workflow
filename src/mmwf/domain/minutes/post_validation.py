"""后置校验：权威源重绑与硬诊断，不执行图路由或外部副作用。"""
from __future__ import annotations

from mmwf.domain.extraction.segmentize import segmentize
from mmwf.domain.grounding.binder import bind_grounding, union_refs
from .issues import aggregate_issues, make_issue


KINDS = ("resolutions", "action_items", "pending_decisions")


def business_entries(minutes):
    """canonical 议题与事项，路径始终来自当前工作稿。"""
    for i, topic in enumerate(minutes.get("topics", [])):
        path = f"topics[{i}]"
        yield path, topic
        for kind in KINDS:
            for j, item in enumerate(topic.get(kind, [])):
                yield f"{path}.{kind}[{j}]", item


def source_segments(segments=None, record_markdown=None):
    if segments is not None:
        return list(segments)
    return segmentize(record_markdown or "")[0]


def verify_references(entry, segments, target=""):
    """逐个显式引用核验；任何坏引用都独立留下阻断问题。"""
    refs, issues = [], []
    def error(code, message, evidence=None):
        issues.append(make_issue(code, target, message, source="grounding", level="error",
                                 blocking=True, evidence=evidence))
    if not segments:
        error("SOURCE_UNAVAILABLE", "原文不可用，无法验证依据")
        return refs, issues
    if entry.get("ungrounded"):
        error("UNGROUNDED", "内容标记为无原文依据")
    explicit = entry.get("source_refs", entry.get("refs"))
    quotes = entry.get("source_quote")
    # source_refs 与 source_quote 若同时提交，两种证据都须验证。
    requests = list(explicit) if isinstance(explicit, (list, tuple)) else []
    if explicit is not None and not isinstance(explicit, (list, tuple)):
        requests.append(None)
    if explicit is not None and not explicit:
        error("UNGROUNDED", "显式引用列表为空")
    if quotes is not None:
        requests.extend(quotes if isinstance(quotes, list) else [quotes])
    if not requests:
        error("UNGROUNDED", "内容缺少原文引用")
    for request in requests:
        sid = request.get("segment_id") if isinstance(request, dict) else None
        quote = request.get("quote") if isinstance(request, dict) else request
        if not isinstance(quote, str) or not quote.strip():
            error("REF_INVALID", "引用缺少可验证的原文片段")
            continue
        if sid is not None and sid not in {f"seg-{s.seq}" for s in segments}:
            error("REF_INVALID", "引用段落不存在", {"segment_id": sid})
            continue
        # 有段 id 时仅在包含该段的连续窗口中重绑，避免跳到别处掩盖伪造。
        candidates = []
        if sid is None:
            candidates = [segments]
        else:
            index = next(i for i, s in enumerate(segments) if f"seg-{s.seq}" == sid)
            candidates = [segments[a:b] for a in range(max(0, index - 2), index + 1)
                          for b in range(index + 1, min(len(segments), a + 3) + 1)]
        results = [bind_grounding(quote, group) for group in candidates]
        results = [r for r in results if r.ok and (sid is None or any(x["segment_id"] == sid for x in r.refs))]
        if not results:
            error("REF_MISMATCH", "引用与权威原文不匹配", {"quote": quote, "segment_id": sid})
            continue
        result = min(results, key=lambda r: (-r.confidence, tuple(x["segment_id"] for x in r.refs)))
        refs = union_refs(refs, result.refs)
        for issue in result.issues:
            issues.append({**issue, "target": target})
    return refs, issues


def check_grounding(minutes, segments=None, *, record_markdown=None, conclusion_title_paths=()):
    """§2.3：导航标题免逐字匹配；结论标题路径由呈现方显式传入。"""
    source = source_segments(segments, record_markdown)
    issues = []
    if segments is None and record_markdown:
        issues.append(make_issue("SOURCE_TIME_LOSS", "", "记录稿反解时间有损，不能视为真实发言结束时间", source="grounding"))
    for path, entry in business_entries(minutes):
        # 呈现上下文不写入 canonical，不接受自创的条目开关。
        is_topic = path.count(".") == 0
        if is_topic and not entry.get("discussion", "").strip() and path not in conclusion_title_paths:
            continue
        _, found = verify_references(entry, source, path)
        issues.extend(found)
    return aggregate_issues(issues)


def validate_hard(minutes, segments=None, *, record_markdown=None, conclusion_title_paths=()):
    """返回全量 grounding 诊断；调用者只以 blocking 判定硬阻断。"""
    return check_grounding(minutes, segments, record_markdown=record_markdown,
                           conclusion_title_paths=conclusion_title_paths)


def post_validate(minutes, *, segments=None, record_markdown=None, terms=(), template=None,
                  extraction_issues=(), granularity_issues=()):
    """聚合三类模板检查和领域检查；不保留上轮当前诊断。"""
    from .consistency import check_numbers, check_person_names
    from .template_checks import check_template
    template_issues = [] if template is None else [
        {**i, "source": "template"} for i in check_template(minutes, template)]
    return aggregate_issues(extraction_issues, granularity_issues, template_issues,
                            validate_hard(minutes, segments, record_markdown=record_markdown),
                            check_numbers(minutes, segments, record_markdown=record_markdown),
                            check_person_names(minutes, segments, record_markdown=record_markdown, terms=terms))
