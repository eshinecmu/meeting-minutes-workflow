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


class MinutesService:
    """版本与交付共用的校验入口；任何缓存查询都必须在此之后。"""
    def __init__(self, settings):
        self.cfg = settings
        self.db_path = settings.resolve_path(settings.app.data_dir) / 'mmwf.db'

    def template_store(self):
        from mmwf.infra.template_store import make_template_store
        return make_template_store(self.db_path, config_dir=self.cfg.config_dir)

    def glossary_service(self):
        from .glossary import make_glossary_service
        return make_glossary_service(self.cfg, db_path=self.db_path)

    def store(self):
        from mmwf.infra.minutes_store import MinutesStore
        return MinutesStore(self.db_path)

    def version_note(self, state):
        """计算配对身份，供存储和 HTTP 审定证明共用。"""
        from mmwf.graph.idempotency import content_hash
        structured = state['minutes_structured']
        context = (state.get('minutes') or {}).get('_context') or structured.get('_context', {})
        note = {'source_fingerprint': content_hash({'record': state.get('record_markdown'), 'segments': state.get('transcript')}),
                'source_version': context.get('source_version'),
                'template_fingerprint': content_hash(context.get('template_snapshot') or context.get('template_fingerprint')),
                'issues': state.get('issues') or []}
        note['pair_fingerprint'] = content_hash({**note, 'structured': structured, 'markdown': state['minutes_markdown']})
        return note

    def save_draft(self, meeting_id, state, *, created_by='system', node_id='assemble'):
        from mmwf.graph.idempotency import content_hash
        structured = state['minutes_structured']
        note = self.version_note(state)
        row = self.store().save(meeting_id, structured, state['minutes_markdown'], note, created_by=created_by)
        from mmwf.graph.idempotency import IdempotencyStore, make_idempotency_key
        key = make_idempotency_key(meeting_id, node_id, {
            'source': note['source_fingerprint'], 'template': note['template_fingerprint'],
            'structured': content_hash(structured), 'markdown': content_hash(state['minutes_markdown'])})
        IdempotencyStore(self.db_path).put(key, meeting_id, node_id, row['version_id'])
        return row

    def validate(self, meeting_id, state):
        return gate_candidate(self, meeting_id, state)

    def deliver(self, meeting_id, state, *, approved=False, version=None, created_by='system'):
        # 当前工作稿也须复检，不能拿历史 final 或旧缓存掩盖当前错误。
        candidate = self.validate(meeting_id, state)
        if not approved:
            raise MmwfError(code='E4001', user_message='请先通过纪要审定（G3）再导出。')
        if state.get('stage') == 'archived' and self.store().get(meeting_id) is None:
            raise MmwfError(code='E4001', detail='旧归档会议不重建纪要历史，请使用已有归档文件。')
        current = self.save_draft(meeting_id, {**state, **candidate}, created_by=created_by, node_id='export')
        selected = current if version is None else self.store().get(meeting_id, version)
        if selected is None:
            raise MmwfError(code='E1002', detail=f'纪要版本 v{version} 不存在')
        if selected['version_no'] != current['version_no']:
            if not selected['is_final']:
                raise MmwfError(code='E4001', detail='该纪要版本尚未审定')
            self.validate(meeting_id, {**state, 'minutes_structured': selected['structured_json'],
                                      'minutes_markdown': selected['markdown']})
        return self.store().mark_final(meeting_id, selected['version_no'], selected['note']['pair_fingerprint'])

    def require_final(self, meeting_id, filename):
        import re
        from pathlib import Path
        matched = re.fullmatch(re.escape(meeting_id) + r'-纪要-v(\d+)\.(?:docx|doc|pdf)', Path(filename).name)
        row = self.store().get(meeting_id, int(matched[1])) if matched else None
        if not row or not row['is_final']:
            raise MmwfError(code='E4001', detail='该文件没有审定后的纪要配对版本')
        if Path(filename).suffix == '.pdf':
            from .export import make_export_service
            make_export_service(meeting_id, self.cfg).require_pdf_source(
                'export/' + Path(filename).name, row)
        return row


def http_export(runtime, meeting_id, payload):
    """HTTP 默认类型以工作流存在为准；失败纪要不降级记录稿。"""
    fmt = payload.get('format', 'docx')
    if not isinstance(fmt, str) or fmt not in {'docx', 'pdf'}:
        raise MmwfError(code='E1001', detail='format 必须为 docx 或 pdf')
    if fmt == 'pdf':
        convert_doc = payload.get('convert_doc', False)
        if type(convert_doc) is not bool or convert_doc or payload.get('artifact') not in (None, 'minutes'):
            raise MmwfError(code='E1001', detail='PDF 仅支持纪要，且不能同时请求 DOC')
        version = payload.get('version')
        if version is not None and (type(version) is not int or version < 1):
            raise MmwfError(code='E1001', detail='version 必须为正整数')
    with runtime.meeting_lock(meeting_id), runtime.runner(meeting_id) as runner:
        state = runner.values()
        artifact = payload.get('artifact')
        workflow = bool(state or MinutesService(runtime.cfg).store().get(meeting_id))  # 会议一旦有检查点，就沿纪要交付合同。
        if artifact is None:
            artifact = 'minutes' if workflow else 'record'
        if fmt == 'pdf' and artifact != 'minutes':
            raise MmwfError(code='E1001', detail='记录稿不能导出 PDF')
        status = runner.status()
        approved = False
        if artifact == 'minutes':
            service = MinutesService(runtime.cfg)
            candidate = service.validate(meeting_id, state)
            final = service.store().get(meeting_id)
            # HTTP 只复用图在 G3 后定稿的同一配对；next 本身不是审定证明。
            approved = (status.pending_gate is None and final is not None and bool(final['is_final'])
                and final['note']['pair_fingerprint'] == service.version_note({**state, **candidate})['pair_fingerprint']
                and (status.status == 'archived' or bool({'export', 'archive'} & set(runner.checkpoint().next))))
        exporter = runtime.export_service(meeting_id)
        export = exporter.export_pdf if fmt == 'pdf' else exporter.export_docx
        return export(
            version=payload.get('version'), convert_doc=payload.get('convert_doc', False) if fmt == 'pdf' else payload.get('convert_doc'),
            artifact=artifact, minutes_state=state, approved=approved)


def check_download(runtime, meeting_id, filename):
    """下载同样检查当前纪要；记录稿独立标识，不能混用文件名。"""
    import re
    # 一期 {id}-vN 文件实际导出记录稿，按 record 兼容；不赋予纪要 final 身份。
    if re.fullmatch(re.escape(meeting_id) + r'-(?:记录稿-)?v\d+\.(?:docx|doc)', filename):
        return
    with runtime.meeting_lock(meeting_id), runtime.runner(meeting_id) as runner:
        state = runner.values()
        if not state and '-纪要-v' not in filename and not filename.lower().endswith('.pdf'):
            return  # 一期独立记录稿导出兼容。
        service = MinutesService(runtime.cfg)
        service.validate(meeting_id, state)
        service.require_final(meeting_id, filename)
