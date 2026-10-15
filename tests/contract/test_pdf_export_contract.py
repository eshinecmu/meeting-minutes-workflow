"""F5.9 H2：既有 HTTP 信封、请求范围与 PDF 下载反绕过。"""
import pytest


@pytest.mark.parametrize('payload', [
    {'format': None}, {'format': 1}, {'format': True}, {'format': []}, {'format': 'doc'},
    {'format': 'PDF'}, {'format': 'pdf', 'artifact': 'record'},
    {'format': 'pdf', 'convert_doc': True}, {'format': 'pdf', 'convert_doc': None},
    {'format': 'pdf', 'convert_doc': 0}, {'format': 'pdf', 'convert_doc': 'false'},
    {'format': 'pdf', 'version': True}, {'format': 'pdf', 'version': '1'},
    {'format': 'pdf', 'version': 0}, {'format': 'pdf', 'version': -1},
])
def test_invalid_requests_are_global_business_envelopes(gate_env, monkeypatch, payload):
    e = gate_env
    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', lambda *a, **kw: pytest.fail('非法请求不能转换'))
    response = e.client.post(e.base + '/export', json=payload)
    result = response.json()
    assert response.status_code == 200
    assert result['ok'] is False and result['code'] == 'E1001' and result['trace_id']
    assert 'path' not in (result.get('data') or {})


def test_before_g3_pdf_is_not_a_delivery(gate_env, monkeypatch):
    e = gate_env
    pending = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书'}).json()
    assert pending['ok']
    monkeypatch.setattr('mmwf.app.services.export.subprocess.run', lambda *a, **kw: pytest.fail('无 G3 不能转换'))
    assert e.client.post(e.base + '/export', json={'format': 'pdf'}) .json()['code'] == 'E4001'
    fs = e.rt.filestore(e.mid)
    for name in (f'{e.mid}-纪要-v1.pdf', f'{e.mid}-记录稿-v1.pdf', '伪名.pdf'):
        fs.write('export/' + name, b'%PDF-stub', stage='export')
        assert e.client.get(e.base + '/export/' + name).json()['code'] == 'E4001'
