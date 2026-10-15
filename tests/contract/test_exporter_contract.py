"""03 §3 六例裁决：产物、幂等、版本隔离、登记、失败、占用。

exporter 包仍为空；对实际生产 ExportService 的 docx/doc 两条格式路径共用契约。
DOCX 真实渲染；DOC 仅替换 LibreOffice 子进程，不声称验证真实 LO 的格式质量。
错误码遵循 01 §9.3 E5003/E5004；不以异常类名代替错误码。
"""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

from mmwf.app.services.export import ExportService
from mmwf.domain.errors import MmwfError
from mmwf.infra.audit import AuditLogger, query
from mmwf.infra.filestore import make_filestore
from mmwf.infra.migrations.runner import migrate

FONTS = {"方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312", "宋体", "Times New Roman"}
DOC_BYTES = b"deterministic LibreOffice result"


@pytest.fixture(params=["docx", "doc"])
def provider(request, tmp_path, monkeypatch):
    db = tmp_path / "mmwf.db"
    migrate(db)
    store = make_filestore("export-contract", tmp_path / "data", encrypt_enabled=False)
    store.write_text("record/v1.md", "# 工作会议\n\n完成预算报告。", stage="record")
    soffice = tmp_path / "soffice"
    soffice.write_bytes(b"stub")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert args[1:4] == ["--headless", "--convert-to", "doc"]
        assert kwargs["timeout"] == 180
        Path(args[-1]).with_suffix(".doc").write_bytes(DOC_BYTES)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("mmwf.app.services.export.subprocess.run", run)
    service = ExportService(store, db_path=db, audit=AuditLogger(db), soffice_path=str(soffice))
    return SimpleNamespace(format=request.param, service=service, store=store, db=db, calls=calls)


def export(provider, version=1):
    result = provider.service.export_docx(version, installed_fonts=FONTS,
                                         convert_doc=provider.format == "doc")
    if provider.format == "doc":
        assert result.doc_conversion.status == "ok"
        path = result.doc_conversion.rel_path
    else:
        path = result.rel_path
    return result, path


def test_output_contract(provider):
    result, path = export(provider)
    payload = provider.store.read(path)
    assert path.endswith("." + provider.format)
    assert payload and result.encrypted is False
    assert result.meeting_id == "export-contract" and result.version == 1
    assert result.size_bytes > 0 and len(result.sha256) == 64
    if provider.format == "docx":
        document = Document(io.BytesIO(payload))
        assert "完成预算报告。" in "\n".join(p.text for p in document.paragraphs)
        assert provider.calls == []
    else:
        assert payload == DOC_BYTES and len(provider.calls) == 1


def test_idempotency_contract(provider):
    first, path = export(provider)
    payload = provider.store.read(path)
    second, again = export(provider)
    assert again == path
    assert provider.store.read(again) == payload
    assert second.idempotency_key == first.idempotency_key
    assert second.reused is True
    assert len(list(provider.store.resolve("export").glob("*." + provider.format))) == 1


def test_version_isolation_contract(provider):
    first, path = export(provider)
    before = provider.store.read(path)
    provider.store.write_text("record/v2.md", "# 工作会议\n\n完成安全检查。", stage="record")
    second, next_path = export(provider, 2)
    assert next_path != path and second.idempotency_key != first.idempotency_key
    assert provider.store.read(path) == before
    assert provider.store.resolve(next_path).is_file()


def test_manifest_and_audit_contract(provider):
    _, path = export(provider)
    entries = [e for e in provider.store.manifest() if e.path == path]
    assert len(entries) == 1
    entry = entries[0]
    payload = provider.store.read(path)
    assert entry.plain_sha256 == hashlib.sha256(payload).hexdigest()
    assert entry.encrypted is False
    records = query(provider.db)
    assert len(records) == 1 and records[0].action == "export"
    assert "完成预算报告" not in str(records[0].detail)


def test_export_failure_is_e5003(provider, monkeypatch):
    original = provider.store.write

    def fail(path, *args, **kwargs):
        if path.endswith("." + provider.format):
            raise OSError("private exporter failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(provider.store, "write", fail)
    with pytest.raises(MmwfError) as caught:
        export(provider)
    assert caught.value.code == "E5003"
    assert "重试" in caught.value.user_message
    assert "private exporter failure" not in str(caught.value.to_dict())
    assert not any(r.result == "success" for r in query(provider.db))


def test_occupied_target_is_e5004(provider, monkeypatch):
    original = provider.store.write

    def fail(path, *args, **kwargs):
        if path.endswith("." + provider.format):
            raise PermissionError("private locked path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(provider.store, "write", fail)
    with pytest.raises(MmwfError) as caught:
        export(provider)
    assert caught.value.code == "E5004"
    assert "关闭" in caught.value.user_message and "重试" in caught.value.user_message
    assert "private locked path" not in str(caught.value.to_dict())
    assert not any(r.result == "success" for r in query(provider.db))
