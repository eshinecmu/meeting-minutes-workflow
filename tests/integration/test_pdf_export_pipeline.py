"""F5.9 H2：真实审定、HTTP、FileStore 与归档，外部转换用合成 stub。"""
import errno
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from mmwf.app.services.export import PDF_REGISTRY, ARCHIVE_MANIFEST
from mmwf.app.services.minutes import MinutesService
from mmwf.domain.errors import MmwfError
from mmwf.graph.idempotency import IdempotencyStore
from mmwf.graph.nodes.export import make_archive
from mmwf.infra.audit import AuditQuery, query
from mmwf.infra.db import connect_ctx


@pytest.fixture
def pdf_env(gate_env, tmp_path, monkeypatch):
    e = gate_env
    pending = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书'}).json()
    assert pending['ok'], pending
    approved = e.client.post(e.base + '/gate/G3/decide', json={
        'checkpoint_id': pending['data']['pending_gate']['checkpoint_id'],
        'decision': 'approve', 'operator': '秘书'}).json()
    assert approved['ok'], approved
    executable = tmp_path / 'soffice'
    executable.write_bytes(b'stub')
    e.rt.cfg = e.rt.cfg.model_copy(update={'export': e.rt.cfg.export.model_copy(update={'soffice_path': executable})})
    e.converted = []
    e.fs = e.rt.filestore(e.mid)
    with e.rt.runner(e.mid) as runner:
        e.state, e.deps = runner.values(), runner.deps
    e.nodes_before = list(e.calls)

    def run(args, **kw):
        assert args[1:4] == ['--headless', '--convert-to', 'pdf']
        data = Path(args[-1]).read_bytes()
        e.converted.append(hashlib.sha256(data).hexdigest())
        assert data == e.fs.read(e.state['export_paths']['docx'])
        Path(args[-1]).with_suffix('.pdf').write_bytes(b'%PDF-1.7\nstub')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', run)
    return e


def request(e, **payload):
    return e.client.post(e.base + '/export', json={'format': 'pdf', **payload}).json()


def direct(e, **kw):
    return e.rt.export_service(e.mid).export_pdf(minutes_state=e.state, approved=True, **kw)


def test_pdf_result_download_archive_and_idempotency(pdf_env):
    e = pdf_env
    before = e.fs.read_json(ARCHIVE_MANIFEST)
    docx = e.client.post(e.base + '/export', json={'convert_doc': False}).json()['data']
    first = request(e)
    assert first['ok'], first
    result = first['data']
    rel = f'export/{e.mid}-纪要-v1.pdf'
    assert result['format'] == 'pdf' and result['path'] == rel
    assert result['pdf'] == {'status': 'ok', 'path': rel, 'reason': ''}
    assert result['doc']['status'] == 'skipped' and '未要求 DOC' in result['doc']['reason']
    assert result['encrypted'] is False and result['reused'] is False
    assert result['source_docx'] == {'path': docx['path'], 'sha256': docx['sha256']}
    assert result['sha256'] == hashlib.sha256(e.fs.read(rel)).hexdigest()
    assert result['size_bytes'] == len(e.fs.read(rel))
    assert result['idempotency_key'] != docx['idempotency_key']
    repeated = request(e)['data']
    assert repeated['reused'] and repeated['sha256'] == result['sha256']
    assert len(e.converted) == 1 and e.converted == [docx['sha256']]
    archive = e.fs.read_json(ARCHIVE_MANIFEST)
    entry = e.fs.read_json(PDF_REGISTRY)['entries'][rel]
    assert set(entry) == {'version', 'pair_fingerprint', 'source_docx_path', 'source_docx_sha256', 'pdf_sha256', 'size_bytes'}
    assert archive['export_paths']['pdf'] == {rel: entry}
    assert archive['archived_at'] == before['archived_at']
    assert archive['export_paths']['artifact_sha256'][rel] == result['sha256']
    assert not e.fs.resolve(PDF_REGISTRY).read_bytes().startswith(b'{')
    response = e.client.get(e.base + '/export/' + Path(rel).name)
    assert response.content == e.fs.read(rel)
    assert response.headers['content-type'] == 'application/pdf'
    assert "filename*=UTF-8''" + quote(Path(rel).name) in response.headers['content-disposition']
    assert response.headers['content-disposition'].startswith('attachment;')
    assert e.fs.verify_manifest() == [] and e.calls == e.nodes_before
    events = query(e.rt.db_path, AuditQuery(target_id=e.mid))
    assert any(r.action == 'export_download' for r in events)
    assert any(r.action == 'export' and 'pdf' in str(r.detail) for r in events)


@pytest.mark.parametrize('damage', ['pdf', 'missing_pdf', 'source', 'registry', 'manifest', 'archive'])
def test_damaged_cache_is_never_reused(pdf_env, damage):
    e = pdf_env
    result = request(e)['data']
    rel = result['path']
    if damage == 'pdf':
        e.fs.resolve(rel).write_bytes(b'%PDF-changed')
    elif damage == 'missing_pdf':
        e.fs.resolve(rel).unlink()
    elif damage == 'source':
        e.fs.resolve(result['source_docx']['path']).write_bytes(b'broken docx')
    elif damage == 'registry':
        e.fs.write_json(PDF_REGISTRY, {'format_version': 1, 'meeting_id': e.mid, 'entries': {}})
    elif damage == 'manifest':
        e.fs.delete(rel)
        e.fs.resolve(rel).write_bytes(b'%PDF-1.7\nstub')
    else:
        archive = e.fs.read_json(ARCHIVE_MANIFEST)
        archive['export_paths'].pop('pdf')
        e.fs.write_json(ARCHIVE_MANIFEST, archive)
    got = request(e)
    assert got['ok'], got
    assert not got['data']['reused'] and len(e.converted) == 2
    assert e.fs.verify_manifest() == []


def test_current_bad_working_copy_blocks_history_cache_download_and_archive(pdf_env, monkeypatch):
    e = pdf_env
    result = request(e)['data']
    bad = deepcopy(e.state)
    bad['minutes_structured']['topics'][0]['discussion'] = '凭空编造结论'
    bad['minutes_structured']['topics'][0]['source_refs'] = []
    with e.rt.runner(e.mid) as runner:
        runner.app.update_state(runner._cfg, bad)
    assert request(e, version=1)['code'] == 'E3002'
    assert e.client.get(e.base + '/export/' + Path(result['path']).name).json()['code'] == 'E3002'
    with pytest.raises(MmwfError) as caught:
        e.rt.export_service(e.mid).export_pdf(version=1, minutes_state=bad, approved=True)
    assert caught.value.code == 'E3002'
    with pytest.raises(MmwfError) as caught:
        make_archive(e.deps)(bad)
    assert caught.value.code == 'E3002' and len(e.converted) == 1


@pytest.mark.parametrize('damage,code', [('pair', 'E4001'), ('missing', 'E1002'), ('hash', 'E1002'), ('unknown', 'E4001')])
def test_download_final_and_source_checks(pdf_env, damage, code):
    e = pdf_env
    result = request(e)['data']
    rel = result['path']
    registry = e.fs.read_json(PDF_REGISTRY)
    if damage == 'pair':
        registry['entries'][rel]['pair_fingerprint'] = 'changed'
        e.fs.write_json(PDF_REGISTRY, registry)
    elif damage == 'missing':
        e.fs.resolve(rel).unlink()
    elif damage == 'hash':
        e.fs.resolve(rel).write_bytes(b'%PDF-other')
    else:
        rel = f'export/{e.mid}-v1.pdf'
        e.fs.write(rel, b'%PDF-forged', stage='export')
    assert e.client.get(e.base + '/export/' + Path(rel).name).json()['code'] == code


@pytest.mark.parametrize('target', [PDF_REGISTRY, ARCHIVE_MANIFEST])
@pytest.mark.parametrize('err,code', [(PermissionError('占用'), 'E5004'), (OSError(errno.EIO, '写入失败'), 'E5003'),
                                    (OSError(errno.ENOSPC, '满盘'), 'E9002')])
def test_registration_failure_and_retry(pdf_env, monkeypatch, target, err, code):
    e = pdf_env
    from mmwf.infra.filestore import FileStore
    write = FileStore.write_json

    def fail(self, rel, *a, **kw):
        if rel == target:
            raise err
        return write(self, rel, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(FileStore, 'write_json', fail)
        got = request(e)
        assert not got['ok'] and got['code'] == code and 'path' not in (got.get('data') or {})
    got = request(e)
    assert got['ok'] and not got['data']['reused'] and len(e.converted) == 2
    assert request(e)['data']['reused']
    events = query(e.rt.db_path, AuditQuery(target_id=e.mid))
    assert any(r.action == 'export' and r.result == 'failure' for r in events)


def test_missing_soffice_explicit_error_and_old_doc_soft_failure(pdf_env):
    e = pdf_env
    e.rt.cfg = e.rt.cfg.model_copy(update={'export': e.rt.cfg.export.model_copy(update={'soffice_path': Path('/missing/soffice')})})
    got = request(e)
    assert got['code'] == 'E5003' and e.converted == []
    old = e.client.post(e.base + '/export', json={'convert_doc': True}).json()
    assert old['ok'] and old['data']['doc']['status'] == 'skipped'
    assert not e.fs.exists(PDF_REGISTRY)


def test_missing_archive_does_not_fabricate_history(pdf_env):
    e = pdf_env
    e.fs.delete(ARCHIVE_MANIFEST)
    got = request(e)
    assert got['code'] == 'E5003' and e.converted == []
    assert not e.fs.exists(ARCHIVE_MANIFEST)


def test_undo_only_pdf_and_retry_after_partial_failure(pdf_env, monkeypatch):
    e = pdf_env
    result = request(e)['data']
    rel, source = result['path'], result['source_docx']['path']
    from mmwf.infra.filestore import FileStore
    delete = FileStore.delete

    def fail(self, path, *a, **kw):
        if path == rel:
            raise PermissionError('占用')
        return delete(self, path, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(FileStore, 'delete', fail)
        assert not e.client.post(e.base + '/export/undo', json={'rel_path': rel}).json()['ok']
    assert e.client.post(e.base + '/export/undo', json={'rel_path': rel}).json()['ok']
    assert not e.fs.exists(rel) and e.fs.exists(source)
    assert e.client.post(e.base + '/export/undo', json={'rel_path': rel}).json()['code'] == 'E1002'
    assert rel not in e.fs.read_json(PDF_REGISTRY)['entries']
    assert rel not in e.fs.read_json(ARCHIVE_MANIFEST)['export_paths']['pdf']
    assert MinutesService(e.rt.cfg).store().get(e.mid)['is_final']
    assert request(e)['ok'] and len(e.converted) == 2


def test_same_meeting_direct_concurrency_serializes_conversion(pdf_env):
    e = pdf_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: direct(e), range(2)))
    assert sorted(r.reused for r in results) == [False, True]
    assert len(e.converted) == 1 and results[0].sha256 == results[1].sha256
    assert not list(e.fs.resolve('export').glob('.pdf-*'))


def test_before_archive_merge_collection_key_and_repair(pdf_env):
    e = pdf_env
    # 合成归档前状态：不恢复图、不再审批，保留已存在的真实 final。
    e.state = {**e.state, 'stage': 'exporting'}
    with connect_ctx(e.rt.db_path) as conn:
        conn.execute("UPDATE meeting SET status='processing' WHERE meeting_id=?", (e.mid,))
    e.fs.delete(ARCHIVE_MANIFEST)
    result = direct(e)
    assert not e.fs.exists(ARCHIVE_MANIFEST)
    e.deps.idem_store = IdempotencyStore(e.rt.db_path)
    node = make_archive(e.deps)
    first = node(e.state)
    manifest = e.fs.read_json(ARCHIVE_MANIFEST)
    assert result.rel_path in manifest['export_paths']['pdf']
    assert node(e.state)['idem_keys'] == first['idem_keys']
    e.fs.resolve(result.rel_path).write_bytes(b'broken')
    second = node(e.state)
    assert second['idem_keys']['archive'] != first['idem_keys']['archive']
    assert not e.fs.read_json(ARCHIVE_MANIFEST)['export_paths'].get('pdf')
    assert not e.fs.read_json(PDF_REGISTRY)['entries']
    assert e.fs.read_json(ARCHIVE_MANIFEST)['archived_at'] == manifest['archived_at']


@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'nonzero', 'missing', 'empty', 'bad'])
def test_explicit_conversion_failures_never_return_old_link(pdf_env, monkeypatch, failure):
    import subprocess
    e = pdf_env
    result = request(e)['data']
    e.fs.resolve(result['path']).write_bytes(b'%PDF-old-damaged')
    previous = e.fs.read_json(PDF_REGISTRY)

    def fail(args, **kw):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 180)
        if failure == 'oserror':
            raise OSError('外部程序调用失败')
        if failure in ('empty', 'bad'):
            Path(args[-1]).with_suffix('.pdf').write_bytes(b'' if failure == 'empty' else b'bad')
        return SimpleNamespace(returncode=1 if failure == 'nonzero' else 0,
                               stdout='敏感转换输出哨兵', stderr='敏感转换输出哨兵')

    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', fail)
    got = request(e)
    assert got['code'] == 'E5003' and not got['ok'] and got['trace_id']
    assert not got.get('data') and e.fs.read_json(PDF_REGISTRY) == previous
    assert not list(e.fs.resolve('export').glob('.pdf-*'))
    events = query(e.rt.db_path, AuditQuery(target_id=e.mid))
    assert any(r.action == 'export' and r.result == 'failure' for r in events)
    assert '敏感转换输出哨兵' not in str([r.detail for r in events])


def test_font_fail_blocks_even_with_pdf_cache(pdf_env, monkeypatch):
    e = pdf_env
    request(e)
    svc = e.rt.export_service(e.mid)
    svc.font_policy = 'fail'
    with pytest.raises(MmwfError) as caught:
        svc.export_pdf(minutes_state=e.state, approved=True, installed_fonts={'其他字体'})
    assert caught.value.code == 'E5001' and len(e.converted) == 1


def test_selected_version_missing_draft_and_new_final(pdf_env):
    from mmwf.app.services.minutes import gate_candidate
    e = pdf_env
    first = direct(e)
    service = MinutesService(e.rt.cfg)

    def renamed(name):
        candidate = gate_candidate(service, e.mid, e.state,
                                   {'minutes': {'topics': [{'name': name}, {}]}})
        return {**e.state, **candidate}

    draft_state = renamed('第二版议题')
    draft = service.save_draft(e.mid, draft_state)
    e.state = renamed('第三版议题')
    with pytest.raises(MmwfError) as caught:
        direct(e, version=999)
    assert caught.value.code == 'E1002'
    with pytest.raises(MmwfError) as caught:
        direct(e, version=draft['version_no'])
    assert caught.value.code == 'E4001'
    assert len(e.converted) == 1
    # 新 final 不混入旧归档版本集合。
    with pytest.raises(MmwfError) as caught:
        direct(e)
    assert caught.value.code == 'E4001' and len(e.converted) == 1
    # 归档前新版本可显式生成；键、路径和来源均独立。
    e.state['stage'] = 'exporting'
    with connect_ctx(e.rt.db_path) as conn:
        conn.execute("UPDATE meeting SET status='processing' WHERE meeting_id=?", (e.mid,))
    e.fs.delete(ARCHIVE_MANIFEST)
    # stub 仍逐字验证所选纪要 DOCX，而非记录稿。
    new_version = service.store().get(e.mid)['version_no']
    e.state['export_paths'] = {**e.state['export_paths'], 'docx': f'export/{e.mid}-纪要-v{new_version}.docx'}
    latest = direct(e)
    assert latest.version != first.version and latest.idempotency_key != first.idempotency_key
    assert latest.rel_path != first.rel_path and len(e.converted) == 2
    assert e.fs.exists(first.rel_path)
