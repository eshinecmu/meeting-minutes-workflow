"""G3 候选稿的副本适配、重建与复检；不推进检查点。"""
from copy import deepcopy

from mmwf.domain.errors import GroundingBlockedError, MmwfError
from mmwf.domain.minutes.state_compat import authoritative_segments, upgrade_legacy_state
from mmwf.domain.minutes.post_validation import post_validate
from mmwf.domain.minutes.template_application import apply_template, render_template_markdown
from mmwf.domain.review.gate_edit import validate_edit
from mmwf.graph.nodes.minutes_context import template_of


def gate_baseline(runtime, meeting_id, state):
    """读取结构化编辑基准，不改变旧草稿及检查点身份。"""
    # Runtime 的上下文工厂只在旧状态缺快照时使用，保持一次成文的模板版本。
    from mmwf.infra.db import connect_ctx
    from mmwf.graph.idempotency import content_hash
    import json

    def context_factory(values):
        with connect_ctx(runtime.db_path) as conn:
            row = conn.execute("SELECT * FROM meeting WHERE meeting_id=?", (meeting_id,)).fetchone()
        meta = dict(row) if row else {}
        if isinstance(meta.get("attendees"), str):
            meta["attendees"] = json.loads(meta["attendees"])
        template = runtime.template_store().select(meta.get("template_id"), meeting_kind=meta.get("meeting_kind"))
        snapshot = template.to_dict()
        return {"meeting_meta": meta, "template_snapshot": snapshot,
                "template_id": template.template_id, "template_version": template.version_no,
                "template_fingerprint": content_hash(snapshot),
                "source_fingerprint": content_hash(values.get("record_markdown") or "")}

    snapshot_state = deepcopy(state)
    if not (snapshot_state.get("minutes") or {}).get("_context"):
        snapshot_state["minutes"] = {**(snapshot_state.get("minutes") or {}),
            "_context": (snapshot_state.get("minutes_structured") or {}).get("_context", {})}
    context, template = template_of(snapshot_state, context_factory)
    adapted = upgrade_legacy_state(state, context)
    adapted["minutes_structured"] = adapted.get("minutes_structured") or adapted["minutes"]
    return adapted, context, template


def gate_candidate(runtime, meeting_id, state, payload=None):
    adapted, context, template = gate_baseline(runtime, meeting_id, state)
    structured = adapted["minutes_structured"]
    baseline = {**adapted, "minutes_structured": structured}
    after = validate_edit("G3", baseline, payload) if payload is not None else {
        "minutes": deepcopy(structured), "minutes_markdown": state.get("minutes_markdown") or ""}
    facts = [i for i in adapted["issues"] if i["source"] not in {"template", "grounding", "consistency", "edit"}]
    facts = [i for i in facts if not (i["source"] == "compat" and i["code"] == "META_MISSING"
             and after["minutes"].get(i["target"]))]
    issues = post_validate(after["minutes"], segments=authoritative_segments(state),
                           record_markdown=state.get("record_markdown"), template=template,
                           extraction_issues=facts,
                           terms=runtime.glossary_service().list_entries(enabled_only=True))
    if any(i["blocking"] for i in issues):
        raise GroundingBlockedError(issues=issues)
    work = apply_template(after["minutes"], template)
    work["_context"] = deepcopy(context)
    markdown = render_template_markdown(work, issues)
    if payload is not None and "minutes" in payload:
        if "minutes_markdown" in payload and after["minutes_markdown"] != markdown:
            raise MmwfError(code="E4001", detail="纪要正文与服务端预览不一致")
    else:
        # 旧稿无法无损映射时保留草稿，不静默批准改稿。
        if "".join(after["minutes_markdown"].split()) != "".join(markdown.split()):
            raise MmwfError(code="E4001", user_message="请在结构化编辑中核对后继续。")
        markdown = after["minutes_markdown"]
    return {"minutes_structured": work, "minutes_markdown": markdown, "issues": issues}
