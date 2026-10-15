"""生产纪要节点装配：快照上下文与纯领域函数，保留直接工厂接缝。"""
from mmwf.domain.errors import GroundingBlockedError
from mmwf.domain.minutes.granularity import normalize_granularity
from mmwf.domain.minutes.issues import aggregate_issues
from mmwf.domain.minutes.post_validation import (
    business_entries, post_validate, source_segments, validate_hard, verify_references,
)
from mmwf.domain.minutes.state_compat import authoritative_segments, upgrade_legacy_state
from mmwf.domain.minutes.template_application import apply_template, render_template_markdown
from mmwf.domain.template import parse_template
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes.ingest import _resolve


def template_of(state, context_factory):
    context = (state.get("minutes") or {}).get("_context", {})
    snapshot = context.get("template_snapshot")
    if not snapshot:
        context = context_factory(state)
        snapshot = context["template_snapshot"]
    return context, parse_template({**snapshot, "version": snapshot["version_no"]})


def production_node(name, fn, context_factory, *, deps):
    """每稿复用已持久化模板；闭包不持有会议状态或问题。"""
    cfg = _resolve(deps, settings=None, idem_store=None)

    def run(state):
        if name != "extract":
            B.require_inputs(name, state)
            B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state), name)
        context, template = template_of(state, context_factory)
        if name == "extract":
            result = fn(state)
            fresh = {**state, **result, "minutes": result.get("minutes") or {},
                     "issues": result.get("issues") or [],
                     "minutes_structured": {}, "validation_issues": []}
            fresh = upgrade_legacy_state(fresh, context)
            result.update(minutes=fresh["minutes"], issues=fresh["issues"],
                          minutes_structured={}, minutes_markdown="", validation_issues=[],
                          export_paths={}, idem_keys={}, gen_retry=0)
            return result
        adapted = upgrade_legacy_state(state, context)
        if name == "generate":
            normalized = normalize_granularity(adapted["minutes"])
            # 重试保留抽取与修正事实，当前诊断由 validate 重新计算。
            facts = [i for i in adapted["issues"] if i["source"] not in
                     {"template", "grounding", "consistency"}]
            return {"minutes_structured": apply_template(normalized.minutes, template),
                    "gen_retry": int(state.get("gen_retry") or 0) + 1,
                    "_gate_route": None,
                    "minutes": normalized.minutes,
                    "issues": aggregate_issues(facts, normalized.issues)}
        structured = adapted.get("minutes_structured") or adapted["minutes"]
        if name == "validate":
            facts = [i for i in adapted["issues"] if i["source"] not in
                     {"template", "grounding", "consistency"}]
            issues = post_validate(structured, segments=authoritative_segments(state),
                                   record_markdown=state.get("record_markdown"),
                                   template=template, extraction_issues=facts)
            blocked = any(i["blocking"] for i in issues)
            # 证据只回填真实命中段落；失败候选保留原引用供核对。
            segments = source_segments(authoritative_segments(state), state.get("record_markdown"))
            for path, entry in business_entries(structured):
                if "." not in path and not entry.get("discussion", "").strip():
                    continue
                refs, diagnostics = verify_references(entry, segments, path)
                if not any(i["blocking"] for i in diagnostics):
                    entry["source_refs"] = refs
            errors = [e for e in state.get("errors") or [] if e.get("node") != "validate"]
            if blocked:
                errors.append({**GroundingBlockedError().to_dict(), "node": "validate"})
            return {"minutes_structured": apply_template(structured, template),
                    "minutes": adapted["minutes"], "issues": issues,
                    "validation_issues": [i for i in issues if i["source"] in
                                          {"template", "grounding", "consistency"}],
                    "stage": "validation_blocked" if blocked else "validating", "errors": errors}
        if name == "assemble":
            hard = validate_hard(structured, authoritative_segments(state),
                                 record_markdown=state.get("record_markdown"))
            if any(i["blocking"] for i in hard):
                raise GroundingBlockedError(issues=hard)
            return {"minutes_markdown": render_template_markdown(
                apply_template(structured, template), adapted["issues"]), "_gate_route": None}
        return fn(state)
    run.__name__ = name
    return run
