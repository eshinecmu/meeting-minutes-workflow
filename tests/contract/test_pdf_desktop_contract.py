"""F5.9：桌面源码合同；不代替 Electron 实际操作与 PDF 目视。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'src/desktop/renderer'


def test_format_options_record_restriction_and_request():
    js = (ROOT / 'pages/export.js').read_text()
    assert "['docx', 'DOCX'], ['pdf', 'PDF（普通 PDF）']" in js
    assert "const record = artifact.value === 'record'" in js
    assert 'option[value="pdf"]' in js and '.disabled = record' in js
    assert "if (record) format.value = 'docx'" in js
    assert 'const payload = { artifact: artifact.value, format: format.value }' in js
    assert "if (format.value === 'pdf') payload.convert_doc = false" in js
    for control in ('exportBtn', 'artifact', 'format'):
        assert f'{control}.disabled = true' in js and f'{control}.disabled = false' in js


def test_success_main_path_download_undo_and_font_warnings():
    js = (ROOT / 'pages/export.js').read_text()
    assert 'const path = data.path' in js
    assert 'api.downloadExport(meeting.meeting_id, path)' in js
    assert '`/meetings/${meeting.meeting_id}/export/undo`' in js
    assert '{ rel_path: path }' in js
    assert "result.replaceChildren(S.notice('info', '已撤销导出。'))" in js
    assert 'data.font_warnings || []' in js
    assert '导出文件请按会议密级管理' in js and '人工核对 PDF 版式' in js


def test_failed_pdf_has_no_success_or_stale_link():
    js = (ROOT / 'pages/export.js').read_text()
    request = js.index("exportBtn.addEventListener('click'")
    clear = js.index('result.replaceChildren();', request)
    call = js.index('const data = await api.call', clear)
    guard = js.index("data.pdf?.status !== 'ok'", call)
    success = js.index('const path = data.path', guard)
    assert request < clear < call < guard < success
    assert "throw new api.AppError('E5003'" in js[guard:success]
    assert 'result.replaceChildren();\n      S.showError(err);\n    } finally' in js


def test_download_uses_auth_existing_endpoint_and_error_envelope():
    js = (ROOT / 'api-client.js').read_text().split('export async function downloadExport', 1)[1]
    assert "const filename = path.split('/').pop()" in js
    assert '/export/${encodeURIComponent(filename)}' in js
    assert "'X-MMWF-Token': TOKEN" in js
    assert 'application/json' in js and 'throw new AppError(env.code, env.message)' in js
    assert 'return downloadExport(meetingId, path, false)' in js
    assert 'link.download = filename' in js and 'URL.revokeObjectURL(url)' in js
