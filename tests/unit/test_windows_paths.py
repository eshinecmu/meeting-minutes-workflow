"""Windows 相对路径模拟：在 macOS 上验证持久化与对外引用的一致性。"""
import json
import ntpath
import os
from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from mmwf.app.services.export import ARCHIVE_MANIFEST, PDF_REGISTRY, DocConversion, ExportResult
from mmwf.domain.errors import MmwfError
from mmwf.graph.idempotency import IdempotencyStore, make_idempotency_key, node_input_from_state
from mmwf.graph.nodes.export import _paths_of
from mmwf.infra.audit import AuditQuery, query
from mmwf.infra.filestore import FileStore, FileStoreError
from mmwf.infra.paths import export_paths, relative_posix
from test_export_service import env, _svc
from test_pdf_export_service import converter
from test_api_diff_export import client, settings, isolated_appdir, db_path, meeting, fs


@pytest.mark.parametrize('path', [r'export\纪要.pdf', PureWindowsPath('export/纪要.pdf'),
                                  PurePosixPath('export/纪要.pdf'), 'export/纪要.pdf'])
def test_relative_posix_with_windows_inputs(path, monkeypatch):
    monkeypatch.setattr(os, 'sep', '\\')
    assert relative_posix(path) == 'export/纪要.pdf'
    assert relative_posix(ntpath.join('export', '纪要.pdf')) == 'export/纪要.pdf'
    assert relative_posix('') == ''


@pytest.mark.parametrize('area', ['audio', 'transcript', 'record', 'minutes', 'export', 'archive'])
def test_store_windows_registration_read_delete(tmp_path, area):
    store = FileStore('m', tmp_path, encrypt_enabled=False)
    windows = ntpath.join(area, '子目录', '文件.json')
    posix = f'{area}/子目录/文件.json'
    stored = store.write(windows, b'{}')
    assert stored.rel_path == posix
    assert stored.abs_path == store.store_root / area / '子目录' / '文件.json'
    assert store.read(windows) == store.read(posix) == b'{}'
    assert store.manifest()[0].path == posix
    assert store.manifest()[0].stage == area
    assert store.verify_manifest() == []
    store.write(posix, b'new')
    assert len(store.manifest()) == 1
    store.delete(windows)
    assert store.manifest() == []
    assert store.verify_manifest() == []


def test_verify_normalises_legacy_entries_and_windows_scan(tmp_path, monkeypatch):
    store = FileStore('m', tmp_path, encrypt_enabled=False)
    store.write('export/纪要.pdf', b'%PDF-stub')
    payload = json.loads(store.manifest_path.read_text(encoding='utf-8'))
    payload['entries'][0]['path'] = r'export\纪要.pdf'
    store.manifest_path.write_text(json.dumps(payload), encoding='utf-8')
    # 仅把仓储扫描的相对结果换成 Windows 语义，不影响宿主文件 I/O。
    path_type = type(tmp_path)
    original = path_type.relative_to

    def relative_to(path, *other, **kwargs):
        rel = original(path, *other, **kwargs)
        return PureWindowsPath(rel) if other == (store.store_root,) else rel

    monkeypatch.setattr(path_type, 'relative_to', relative_to)
    assert store.verify_manifest() == []
    store.resolve('export/未登记.pdf').write_bytes(b'other')
    assert store.verify_manifest() == ['存在未登记的文件：export/未登记.pdf']
    rebuilt = store.rebuild_manifest()
    assert {e.path for e in rebuilt} == {'export/纪要.pdf', 'export/未登记.pdf'}
    assert store.verify_manifest() == []


@pytest.mark.parametrize('path', [r'..\secret', r'export\..\secret', 'export/../secret',
    r'C:\secret', 'C:secret', r'\secret', r'\\server\share\secret', '//server/share/secret', '/secret'])
def test_windows_resolve_keeps_traversal_drive_unc_rejection(tmp_path, path):
    store = FileStore('m', tmp_path, encrypt_enabled=False)
    with pytest.raises(FileStoreError):
        store.write(path, b'secret')


@pytest.mark.parametrize('target', ['pdf', 'doc'])
def test_converter_windows_source_returns_posix(converter, monkeypatch, target):
    svc, source, calls = converter

    def run(args, **kwargs):
        from pathlib import Path
        Path(args[-1]).with_suffix('.' + target).write_bytes(b'%PDF-stub' if target == 'pdf' else b'DOC')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', run)
    got = svc._convert_with_soffice(ntpath.join('export', source.filename), target)
    assert got.ok
    assert got.rel_path == 'export/' + PurePosixPath(source.filename).with_suffix('.' + target).name
    assert got.to_dict()['path'] == got.rel_path
    assert svc.store.verify_manifest() == []


def test_pdf_windows_source_cache_archive_audit(env, monkeypatch):
    svc = _svc(env)
    source_rel = f'export/{svc.store.meeting_id}-纪要-v1.docx'
    stored = svc.store.write(source_rel, b'DOCX', stage='export')
    source = ExportResult(svc.store.meeting_id, 1, source_rel, stored.sha256, 4, False, 'docx', reused=True)
    # 模拟 Windows 导出依赖返回的宿主路径，覆盖服务边界而非只测辅助函数。
    source.rel_path = ntpath.join('export', source.filename)
    snapshot = {'version_no': 1, 'note': {'pair_fingerprint': 'paired'}}
    svc.minutes_service = SimpleNamespace(deliver=lambda *a, **kw: snapshot)
    monkeypatch.setattr(svc, 'export_docx', lambda *a, **kw: source)
    monkeypatch.setattr(svc, 'valid_pdf_entries', lambda *a: {})
    conversions = []

    def convert(rel):
        conversions.append(rel)
        pdf = str(PureWindowsPath(rel).with_suffix('.pdf'))
        svc.store.write(pdf, b'%PDF-stub', stage='export')
        return DocConversion(status='ok', rel_path=pdf)

    monkeypatch.setattr(svc, 'convert_to_pdf', convert)
    svc._write_json(ARCHIVE_MANIFEST, {'meeting_id': svc.store.meeting_id, 'archived_at': 'now',
        'export_paths': {'docx': source.rel_path}})
    first = svc.export_pdf(minutes_state={'stage': 'archived'}, approved=True)
    second = svc.export_pdf(minutes_state={'stage': 'archived'}, approved=True)
    rel = source_rel[:-5] + '.pdf'
    assert first.to_dict()['path'] == first.to_dict()['pdf']['path'] == rel
    assert first.to_dict()['source_docx']['path'] == source_rel
    assert not first.reused and second.reused and conversions == [source_rel]
    registry = svc.store.read_json(PDF_REGISTRY)['entries']
    archive = svc.store.read_json(ARCHIVE_MANIFEST)['export_paths']
    assert list(registry) == [rel]
    assert registry[rel]['source_docx_path'] == source_rel
    assert archive['pdf'] == registry
    assert set(archive['artifact_sha256']) == {source_rel, rel}
    assert IdempotencyStore(svc.db_path).get(first.idempotency_key).result_ref == rel
    records = query(svc.db_path, AuditQuery(action='export'))
    assert len(records) == 2
    assert all(r.detail['path'] == rel and r.detail['files'] == [rel] for r in records)
    assert svc._valid_file(ntpath.join('export', source.filename), source.sha256)
    assert svc.store.verify_manifest() == []


def test_graph_archive_keys_and_legacy_bindings(env):
    paths = {'docx': r'export\m.docx', 'doc': {'rel_path': r'export\m.doc'},
        'pdf': {r'export\m.pdf': {'source_docx_path': r'export\m.docx'}},
        'artifact_sha256': {r'export\m.pdf': 'hash'}}
    normal = export_paths(paths)
    windows_input = node_input_from_state('archive', {'export_paths': paths})
    posix_input = node_input_from_state('archive', {'export_paths': normal})
    assert windows_input == posix_input
    assert make_idempotency_key('m', 'archive', windows_input) == make_idempotency_key('m', 'archive', posix_input)
    assert normal['pdf'] == {'export/m.pdf': {'source_docx_path': 'export/m.docx'}}
    assert normal['artifact_sha256'] == {'export/m.pdf': 'hash'}
    got = _paths_of(SimpleNamespace(rel_path=PureWindowsPath('export/m.docx'),
        doc_conversion=SimpleNamespace(rel_path=r'export\m.doc')))
    assert got['docx'] == 'export/m.docx' and got['doc']['rel_path'] == 'export/m.doc'
    svc = _svc(env)
    svc._write_pdf_entries(paths['pdf'])
    assert svc.pdf_entries() == normal['pdf']
    # 绕过服务写入口，模拟旧 Windows 版本已经持久化的登记。
    svc.store.write_json(PDF_REGISTRY, {'format_version': 1,
        'meeting_id': svc.store.meeting_id, 'entries': paths['pdf']}, stage='archive')
    assert svc.pdf_entries() == normal['pdf']
    svc.store.write_json(ARCHIVE_MANIFEST, {'meeting_id': svc.store.meeting_id,
        'archived_at': 'now', 'export_paths': paths}, stage='archive')
    assert svc._archive_for_pdf(True)['export_paths'] == normal


def test_windows_encrypted_area_and_plaintext_export(tmp_path):
    from mmwf.infra.crypto import FileCipher, is_encrypted
    store = FileStore('m', tmp_path, cipher=FileCipher(b'x' * 32))
    for rel, encrypted in [(r'transcript\segments.json', True), (r'record\v1.md', True),
                           (r'minutes\final.md', True), (r'export\交付.pdf', False)]:
        result = store.write(rel, b'payload')
        assert result.encrypted is encrypted
        assert is_encrypted(result.abs_path) is encrypted
        assert store.read(result.rel_path) == b'payload'
    assert store.verify_manifest() == []


def test_gate_snapshot_ref_is_posix_with_windows_checkpoint(env):
    from mmwf.infra.gate_store import GateStore
    data, db, _ = env
    gate = GateStore(db, data / 'gates')
    ref = gate.snapshot('m', r'checkpoint\part', {'gate': 'G3'})
    assert ref == 'gate/ckpt-checkpoint%5Cpart/snapshot.json'
    assert json.loads((data / 'gates' / 'm' / ref).read_text(encoding='utf-8')) == {'gate': 'G3'}


def test_minutes_windows_delivery_reference(monkeypatch):
    from mmwf.app.services.minutes import MinutesService
    service = object.__new__(MinutesService)
    row = {'is_final': True, 'version_no': 2}
    service.cfg = object()
    service.store = lambda: SimpleNamespace(get=lambda mid, version: row if version == 2 else None)
    seen = []
    monkeypatch.setattr('mmwf.app.services.export.make_export_service', lambda *args:
        SimpleNamespace(require_pdf_source=lambda rel, final: seen.append((rel, final))))
    assert service.require_final('m', PureWindowsPath('export/m-纪要-v2.pdf')) == row
    assert seen == [('export/m-纪要-v2.pdf', row)]


def test_download_accepts_posix_reference_with_windows_written_file(client, meeting, fs):
    fs.write(r'export\交付.docx', b'PK-stub', stage='export')
    response = client.get(f'/api/v1/meetings/{meeting}/export/export/交付.docx')
    assert response.status_code == 200 and response.content == b'PK-stub'
    assert fs.verify_manifest() == []


@pytest.mark.parametrize('bad', ['C:secret.docx', r'C:\secret.docx', r'\\server\share\a.docx',
    'export/C:secret.docx', 'export/../secret.docx', 'export/sub/a.docx',
    r'export\a.docx', 'export/%2e%2e.docx', 'record/v1.md'])
def test_download_rejects_windows_drives_and_traversal(settings, isolated_appdir, db_path, meeting, bad):
    from mmwf.api.routes import build_router
    router = build_router(settings)
    endpoint = next(r.endpoint for r in router.routes
                    if getattr(r, 'path', None) == '/meetings/{meeting_id}/export/{file:path}')
    with pytest.raises(MmwfError) as error:
        endpoint(meeting_id=meeting, file=bad)
    assert error.value.code == 'E1001'


def test_windows_relative_idempotency_refs(env):
    from mmwf.graph.idempotency import _default_result_ref
    assert _default_result_ref(SimpleNamespace(rel_path=PureWindowsPath('record/v1.md'))) == 'record/v1.md'
    assert _default_result_ref({'rel_path': r'record\v1.md'}) == 'record/v1.md'


def test_windows_audit_record_and_export_details(env):
    svc = _svc(env)
    record = svc.audit.record_generate('tester', svc.store.meeting_id, rel_path=r'record\v1.md')
    exported = svc.audit.export_event('tester', svc.store.meeting_id,
        files=[r'export\交付.docx'], detail={'path': r'export\交付.docx'})
    assert record.detail['path'] == 'record/v1.md'
    assert exported.detail['path'] == 'export/交付.docx'
    assert exported.detail['files'] == ['export/交付.docx']


@pytest.mark.parametrize('tail', ['export/..%2fsecret.docx', 'export%2f..%2fsecret.docx',
    'export/%2e%2e%5csecret.docx', 'export/..%252fsecret.docx'])
def test_download_routed_encoded_traversal_is_400(client, meeting, fs, monkeypatch, tail):
    # 必须经过 URL 解码和路由匹配；拒绝发生在读取交付物之前。
    fs.write('export/secret.docx', b'PK-secret', stage='export')
    monkeypatch.setattr(FileStore, 'read', lambda *a, **kw: pytest.fail('非法路径不能读取文件'))
    response = client.get(f'/api/v1/meetings/{meeting}/export/{tail}')
    assert response.status_code == 400
    body = response.json()
    assert body['ok'] is False and body['code'] == 'E1001' and body['trace_id']
    assert b'PK-secret' not in response.content


def test_posix_backslash_name_is_store_directory_separator(tmp_path):
    # 仓储引用不接受 POSIX 字面反斜杠文件名；统一解释为目录分隔符。
    store = FileStore('m', tmp_path, encrypt_enabled=False)
    expected = store.store_root / 'a' / 'b.docx'
    resolved = store.resolve(r'a\b.docx')
    stored = store.write(r'a\b.docx', b'DOCX')
    assert resolved == stored.abs_path == expected
    assert stored.rel_path == 'a/b.docx'
    assert store.resolve('a/b.docx') == expected
    assert store.read(r'a\b.docx') == store.read('a/b.docx') == b'DOCX'
    assert store.verify_manifest() == []
