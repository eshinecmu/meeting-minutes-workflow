"""F5.9：唯一真实 PDF 转换门控；检测到程序后任何转换失败均须失败。"""
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from mmwf.app.services.export import PDF_REGISTRY
from mmwf.settings import load_settings

REASON = 'F5.9 真转换待验：未安装/未配置 soffice，须目标机 LibreOffice 运行'


@pytest.fixture
def real_soffice():
    cfg = load_settings(Path(__file__).resolve().parents[2] / 'config')
    configured = cfg.export.soffice_path
    executable = str(configured) if configured else shutil.which('soffice')
    if not executable or not Path(executable).exists():
        pytest.skip(REASON)
    # 已检测到程序：执行权限、版本探测及实际转换失败不能再 skip。
    version = subprocess.run([executable, '--version'], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=180, check=True)
    print(f'soffice 命令：{executable} --version\n版本：{version.stdout.strip()}')
    return executable


def test_real_pdf_source_registry(real_soffice, gate_env):
    e = gate_env
    e.rt.cfg = e.rt.cfg.model_copy(update={
        'export': e.rt.cfg.export.model_copy(update={'soffice_path': Path(real_soffice)})})
    pending = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书'}).json()
    assert pending['ok'], pending
    approved = e.client.post(e.base + '/gate/G3/decide', json={
        'checkpoint_id': pending['data']['pending_gate']['checkpoint_id'],
        'decision': 'approve', 'operator': '秘书'}).json()
    assert approved['ok'], approved
    response = e.client.post(e.base + '/export', json={'artifact': 'minutes', 'format': 'pdf'}).json()
    assert response['ok'], response
    result = response['data']
    fs = e.rt.filestore(e.mid)
    pdf = fs.read(result['path'])
    source = fs.read(result['source_docx']['path'])
    assert pdf.startswith(b'%PDF-') and len(pdf) > 5
    assert hashlib.sha256(pdf).hexdigest() == result['sha256']
    assert hashlib.sha256(source).hexdigest() == result['source_docx']['sha256']
    entry = fs.read_json(PDF_REGISTRY)['entries'][result['path']]
    assert entry['pdf_sha256'] == result['sha256']
    assert entry['source_docx_sha256'] == result['source_docx']['sha256']
    assert entry['size_bytes'] == len(pdf) and fs.verify_manifest() == []
    print(f"DOCX SHA256：{result['source_docx']['sha256']}\nPDF SHA256：{result['sha256']}")
    print(f"字体诊断：{result.get('font_warnings', [])}；目标机字体版本、人工版式与签字仍须另附")
