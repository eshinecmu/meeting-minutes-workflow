"""F5.9 H2：仅替换外部进程，核对转换参数、同源字节与失败清理。"""
import errno
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmwf.domain.errors import MmwfError
from test_export_service import env, _svc, FONTS_OK


@pytest.fixture
def converter(env, tmp_path, monkeypatch):
    executable = tmp_path / 'soffice'
    executable.write_bytes(b'stub')
    svc = _svc(env, soffice_path=str(executable))
    source = svc.export_docx(installed_fonts=FONTS_OK)
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        Path(args[-1]).with_suffix('.pdf').write_bytes(b'%PDF-1.7\nstub')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', run)
    return svc, source, calls


def test_exact_command_source_and_cleanup(converter, monkeypatch):
    svc, source, calls = converter
    original = svc.store.read(source.rel_path)
    run = subprocess.run

    def inspect(args, **kw):
        copied = Path(args[-1])
        assert copied.read_bytes() == original
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == source.sha256
        assert copied.parent.parent == svc.store.resolve('export')
        assert copied.parent != svc.store.resolve('export')
        return run(args, **kw)

    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', inspect)
    got = svc.convert_to_pdf(source.rel_path)
    assert got.status == 'ok'
    args, kw = calls[0]
    assert args == [svc.soffice_path, '--headless', '--convert-to', 'pdf', '--outdir', args[5], args[6]]
    assert kw == dict(capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    assert not Path(args[5]).exists()
    assert svc.store.read(got.rel_path).startswith(b'%PDF-')
    assert svc.store.verify_manifest() == []


@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'nonzero', 'missing', 'empty', 'bad'])
def test_bad_conversion_does_not_pick_up_old_pdf(converter, monkeypatch, failure):
    svc, source, calls = converter
    rel = str(Path(source.rel_path).with_suffix('.pdf'))
    svc.store.write(rel, b'%PDF-old', stage='export')
    work = []

    def run(args, **kw):
        work.append(Path(args[5]))
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 180)
        if failure == 'oserror':
            raise OSError('本地调用失败')
        if failure in ('empty', 'bad'):
            Path(args[-1]).with_suffix('.pdf').write_bytes(b'' if failure == 'empty' else b'not pdf')
        return SimpleNamespace(returncode=1 if failure == 'nonzero' else 0,
                               stdout='敏感正文不应进入原因', stderr='敏感正文不应进入原因')

    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', run)
    got = svc.convert_to_pdf(source.rel_path)
    assert got.status == 'failed' and got.rel_path is None
    assert '敏感正文' not in got.reason
    assert svc.store.read(rel) == b'%PDF-old'
    assert all(not path.exists() for path in work)


def test_configured_missing_path_does_not_fallback(converter, monkeypatch):
    svc, source, calls = converter
    svc.soffice_path += '-missing'
    monkeypatch.setattr('mmwf.app.services.export.shutil.which', lambda _: pytest.fail('配置优先，不查 PATH'))
    assert svc.convert_to_pdf(source.rel_path).status == 'skipped'
    assert calls == []


def test_path_lookup_when_unconfigured(converter, monkeypatch):
    svc, source, calls = converter
    executable = svc.soffice_path
    svc.soffice_path = None
    monkeypatch.setattr('mmwf.app.services.export.shutil.which', lambda _: executable)
    assert svc.convert_to_pdf(source.rel_path).ok
    assert calls[0][0][0] == executable


@pytest.mark.parametrize('err,code', [(PermissionError(), 'E5004'), (OSError(errno.EIO, '写入失败'), 'E5003'),
                                     (OSError(errno.ENOSPC, '磁盘已满'), None)])
def test_pdf_formal_write_errors(converter, monkeypatch, err, code):
    svc, source, calls = converter
    write = svc.store.write

    def fail(rel, *args, **kw):
        if rel.endswith('.pdf'):
            raise err
        return write(rel, *args, **kw)

    monkeypatch.setattr(svc.store, 'write', fail)
    with pytest.raises(MmwfError if code else OSError) as caught:
        svc.convert_to_pdf(source.rel_path)
    assert (caught.value.code if code else caught.value.errno) == (code or errno.ENOSPC)
    assert not list(svc.store.resolve('export').glob('.pdf-*'))


def test_direct_entry_has_no_arbitrary_path_bypass(converter):
    svc, source, calls = converter
    with pytest.raises(MmwfError) as caught:
        svc.export_pdf(approved=True)
    assert caught.value.code == 'E4001' and not calls
    with pytest.raises(MmwfError) as caught:
        svc.export_pdf(artifact='record')
    assert caught.value.code == 'E1001' and not calls
