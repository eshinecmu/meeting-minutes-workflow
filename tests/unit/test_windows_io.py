"""W3：模拟中文 Windows 的编码、换行、路径别名与文件占用。"""
import importlib
import io
import json
import locale
import ntpath
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mmwf.infra import filestore
from mmwf.infra.crypto import FileCipher


@pytest.fixture
def windows_text_defaults(monkeypatch):
    """通过真实 TextIOWrapper 模拟 cp936 默认编码和 CRLF 写入转换。"""
    native_open = io.open

    def windows_open(file, mode='r', buffering=-1, encoding=None, errors=None,
                     newline=None, closefd=True, opener=None):
        if 'b' not in mode:
            if encoding in (None, 'locale'):
                encoding = 'cp936'
            if newline is None and any(flag in mode for flag in 'wax'):
                newline = '\r\n'
        return native_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def install():
        monkeypatch.setattr(locale, 'getpreferredencoding', lambda *args: 'cp936')
        monkeypatch.setattr(io, 'open', windows_open)

    return install


def test_shadow_reports_keep_utf8_lf(tmp_path, windows_text_defaults):
    import shadow_report
    from mmwf.domain.shadow import aggregate_metrics
    from test_shadow_scoring import vector

    batch, scores, thresholds = vector()
    windows_text_defaults()
    # 先确认模拟确实会改变无显式编码/换行的写入。
    control = tmp_path / 'control.txt'
    control.write_text('中文\n')
    assert control.read_bytes() == '中文\r\n'.encode('cp936')
    output = tmp_path / 'reports'
    shadow_report.write_reports(output, aggregate_metrics(batch, scores, thresholds))
    for name in ('report.md', 'report.json', 'records.csv', 'records.json'):
        raw = (output / name).read_bytes()
        assert b'\r\n' not in raw and b'\n' in raw
        assert 'SYNTHETIC' in raw.decode('utf-8')
    assert '合成' in (output / 'report.md').read_bytes().decode('utf-8')
    assert not list(tmp_path.glob('.shadow-report-*'))


def test_shadow_candidate_keeps_utf8_lf(tmp_path, monkeypatch, windows_text_defaults):
    import shadow_run

    windows_text_defaults()
    batch = SimpleNamespace(batch_id='b', dataset_kind='synthetic')
    meeting = SimpleNamespace(meeting_id='m', source_version=1)
    state = {'minutes_markdown': '# 会议纪要\n\n中文正文。\n'}
    monkeypatch.setattr(shadow_run, 'load_inputs', lambda *a: ({}, {}, {}))
    monkeypatch.setattr(shadow_run, 'make_client', lambda *a: (object(), object(), {}))
    monkeypatch.setattr(shadow_run, 'make_llm_extractor', lambda *a: object())
    monkeypatch.setattr(shadow_run, 'generate_candidate', lambda *a, **kw: (state, 'generated', ''))
    assert shadow_run.run_meeting(batch, meeting, {}, tmp_path)['status'] == 'generated'
    for path in (tmp_path / 'm').iterdir():
        raw = path.read_bytes()
        assert b'\r\n' not in raw
        text = raw.decode('utf-8')
        if path.suffix == '.json':
            json.loads(text)
    assert (tmp_path / 'm/candidate.md').read_bytes().endswith(state['minutes_markdown'].encode('utf-8'))


def test_windows_case_alias_updates_one_manifest_entry(tmp_path, monkeypatch):
    store = filestore.FileStore('m', tmp_path, cipher=FileCipher(b'x' * 32))
    native_resolve = store.resolve
    # NTFS 的大小写别名映射由接缝模拟，宿主文件系统无需改变。
    monkeypatch.setattr(store, 'resolve', lambda rel: native_resolve(str(rel).lower()))
    monkeypatch.setattr(filestore, 'normcase', ntpath.normcase)
    store.write('export/report.md', b'old')
    result = store.write('EXPORT/Report.md', b'new')
    assert not result.encrypted
    assert store.read('export/REPORT.md') == b'new'
    assert len(store.manifest()) == 1
    assert store.manifest()[0].path == 'EXPORT/Report.md'
    assert store.verify_manifest() == []
    store.delete('export/REPORT.md')
    assert store.manifest() == [] and store.verify_manifest() == []


@pytest.mark.parametrize('name', ['accept_t2_03', 'accept_t2_04', 'accept_t2_05', 'accept_f5_9'])
def test_evidence_utf8_under_cp936(tmp_path, monkeypatch, capsys, name):
    module = importlib.import_module(name)
    native_run = subprocess.run
    # subprocess 内部的 locale 接缝（仅 monkeypatch getpreferredencoding 在新 Python 无效）。
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda *a: 'cp936')
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp936')

    def child(args, **kwargs):
        assert kwargs['env']['PYTHONIOENCODING'] == 'utf-8'
        # 保留被测工具全部解码参数；用离线子进程模拟 pytest 的 UTF-8 及损坏诊断字节。
        return native_run([sys.executable, '-c',
            "import sys; sys.stdout.buffer.write('中文证据\\n'.encode('utf-8') + b'\\xff')"], **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', child)
    module._run_evidence(1, tmp_path)
    output = capsys.readouterr().out
    assert '中文证据\n\ufffd' in output


@pytest.mark.parametrize('name,operation', [('probe_electron_dev', '_kill'),
    ('cdp_probe', '_kill'), ('probe_electron_flags', '_kill_mmwf')])
def test_probe_diagnostics_ignore_locale(monkeypatch, name, operation):
    module = importlib.import_module(name)
    native_run = subprocess.run
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp936')
    calls = []

    def child(args, **kwargs):
        result = native_run([sys.executable, '-c',
            "import sys; sys.stdout.buffer.write('诊断'.encode('utf-8') + b'\\xff')"], **kwargs)
        calls.append(result.stdout)
        return result

    monkeypatch.setattr(module.subprocess, 'run', child)
    monkeypatch.setattr(module.time, 'sleep', lambda *a: None)
    getattr(module, operation)()
    assert len(calls) >= 2 and all(text == '诊断\ufffd' for text in calls)


def test_tasklist_gbk_titles_preserved(monkeypatch):
    import probe_electron_flags

    rows = '"图像名","PID","会话名","会话号","内存","状态","用户名","CPU","窗口标题"\r\n'
    rows += '"mmwf.exe","1","Console","1","10 K","Running","用户","0","会议纪要"\r\n'

    def tasklist(args, **kwargs):
        assert 'encoding' not in kwargs and not kwargs.get('text')
        return SimpleNamespace(stdout=rows.encode('gbk'))

    monkeypatch.setattr(probe_electron_flags.subprocess, 'run', tasklist)
    assert probe_electron_flags._window_titles() == ['会议纪要']
    assert probe_electron_flags._proc_count() == 1


@pytest.mark.parametrize('header,expected', [(b'PK\x03\x04rest', True), (b'bad', False)])
def test_word_header_closes_handle_before_delete(tmp_path, monkeypatch, header, expected):
    import verify_docx_in_word

    path = tmp_path / '纪要.docx'
    path.write_bytes(header)
    native_open = Path.open
    handles = []

    def tracked_open(self, *args, **kwargs):
        handle = native_open(self, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, 'open', tracked_open)
    assert verify_docx_in_word._looks_like_zip_or_ole(path) is expected
    assert handles and all(handle.closed for handle in handles)
    path.unlink()


def test_layer_checks_decode_utf8_under_cp936(tmp_path, monkeypatch, capsys):
    import accept_t1_16

    native_run = subprocess.run
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp936')

    def child(args, **kwargs):
        return native_run([sys.executable, '-c',
            "import sys; sys.stdout.buffer.write('分层诊断'.encode('utf-8') + b'\\xff'); sys.exit(1)"], **kwargs)

    monkeypatch.setattr(accept_t1_16.subprocess, 'run', child)
    assert accept_t1_16.check_layering_and_deps(tmp_path) is False
    assert capsys.readouterr().out.count('分层诊断\ufffd') == 2


def test_shadow_git_diagnostics_decode_without_locale(tmp_path, monkeypatch, windows_text_defaults):
    import shutil
    import shadow_run

    fixtures = Path(__file__).resolve().parents[1] / 'fixtures/shadow/synthetic'
    source = tmp_path / 'input'
    shutil.copytree(fixtures, source)
    native_run = subprocess.run
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp936')
    calls = []

    def child(args, **kwargs):
        assert args == ['git', 'rev-parse', 'HEAD']
        result = native_run([sys.executable, '-c',
            "import sys; sys.stdout.buffer.write('版本'.encode('utf-8') + b'\\xff')"], **kwargs)
        calls.append(result.stdout)
        return result

    monkeypatch.setattr(shadow_run.subprocess, 'run', child)
    windows_text_defaults()
    run = shadow_run.run_batch(source / 'manifest.json', tmp_path / 'batch')
    assert run['complete'] and calls == ['版本\ufffd']
    assert run['code_version'] == '版本\ufffd'
    for path in (tmp_path / 'batch').rglob('*'):
        if path.suffix in ('.json', '.md', '.csv'):
            assert b'\r\n' not in path.read_bytes()
            path.read_bytes().decode('utf-8')


def test_word_probe_tolerates_non_utf8_diagnostics(tmp_path, monkeypatch):
    import verify_docx_in_word

    native_run = subprocess.run

    def child(args, **kwargs):
        result = native_run([sys.executable, '-c',
            "import sys; sys.stderr.buffer.write(b'\\xff')"], **kwargs)
        assert result.stderr == '\ufffd'
        (tmp_path / 'word_probe.json').write_text('{"ok": true}', encoding='utf-8-sig')
        return result

    monkeypatch.setattr(verify_docx_in_word.subprocess, 'run', child)
    assert verify_docx_in_word._run_word_probe(tmp_path / '纪要.docx', tmp_path) == {'ok': True}
    assert (tmp_path / 'word_probe.ps1').read_bytes().startswith(b'\xef\xbb\xbf')


def test_windows_case_alias_shares_delivery_lock(monkeypatch):
    from pathlib import PureWindowsPath
    from mmwf.app.services import export

    monkeypatch.setattr(export, 'normcase', ntpath.normcase)
    monkeypatch.setattr(export, '_PDF_LOCKS', {})
    stores = [SimpleNamespace(store_root=SimpleNamespace(resolve=lambda value=value: PureWindowsPath(value)))
              for value in ('C:/Data/store/M', 'c:/data/STORE/m', 'C:/Data/store/other')]
    assert export.delivery_lock(stores[0]) is export.delivery_lock(stores[1])
    assert export.delivery_lock(stores[0]) is not export.delivery_lock(stores[2])
