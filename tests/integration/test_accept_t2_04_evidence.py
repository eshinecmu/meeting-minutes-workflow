"""D6：真实 HTTP + 图的红线及三阶段清单联证，不手工灌入 issues。"""
from copy import deepcopy
import json

import pytest

from mmwf.adapters.llm import FakeLlmClient
from mmwf.domain.minutes.issues import render_issues_text
from mmwf.infra.db import connect_ctx
from mmwf.settings import ExtractionConfig


@pytest.mark.parametrize('retry', [0, 2, 100])
def test_http_hard_route_and_failed_download(gate_env, retry):
    e = gate_env
    with e.rt.runner(e.mid) as runner:
        state = deepcopy(runner.values())
        state['resolutions'] = [{'text': '批准预算', 'topic': state['topics'][0]['title'], 'owner': '暂无', 'refs': []}]
        runner.app.update_state(runner._cfg, {'resolutions': state['resolutions'], 'gen_retry': retry}, as_node='extract')
        runner._invoke(None)
        pending = runner.status().pending_gate.to_dict()
    e.calls.clear()
    response = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': pending['checkpoint_id'], 'decision': 'approve', 'operator': '验收秘书'})
    body = response.json()
    assert body['code'] == 'E3002', body
    assert body['ok'] is False and '已停止成文' in body['message']
    assert all(n not in {'assemble', 'gate_g3', 'export', 'archive'} for _, n in e.calls)
    with e.rt.runner(e.mid) as runner:
        assert runner.status().status == 'failed'
        assert not runner.checkpoint().next
    data = e.client.get(e.base + '/issues').json()['data']
    assert data['complete'] and any(i['blocking'] for i in data['issues'])
    text = e.client.get(e.base + '/issues?format=text')
    assert text.status_code == 200 and 'UNGROUNDED' in text.text
    assert e.client.post(e.base + '/export', json={}).json()['code'] == 'E3002'
    with connect_ctx(e.rt.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM minutes_version WHERE meeting_id=? AND is_final=1', (e.mid,)).fetchone()[0] == 0
    assert not e.rt.filestore(e.mid).resolve('export').exists()
    assert not e.rt.filestore(e.mid).exists('archive/manifest.json')


def test_real_joint_issues_and_repair(gate_env, monkeypatch):
    e = gate_env
    from mmwf.adapters import llm
    # 缺会议元信息由 A6 产 extraction 问题；内容与引用数字不同但引用真实。
    payload = {'topics': [{'name': '预算', 'discussion': '', 'resolutions': [
        {'content': '差不多批准300万元', 'source_quote': '预算为三十万元。',
         'source_start_ms': 0, 'source_end_ms': None}]}]}
    fake = FakeLlmClient(script=[json.dumps(payload, ensure_ascii=False)])
    monkeypatch.setattr(llm, 'make_llm_client', lambda cfg: fake)
    e.rt.cfg = e.rt.cfg.model_copy(update={'extraction': ExtractionConfig(engine='llm')})
    created = e.client.post('/api/v1/meetings', json={
        'title': '联合清单验收', 'record_markdown': '[00:00:00] 张三：预算为三十万元。'}).json()
    base = '/api/v1/meetings/' + created['data']['meeting_id']
    pending = e.client.post(base + '/start').json()['data']['pending_gate']
    result = e.client.post(base + '/gate/G2/decide', json={
        'checkpoint_id': pending['checkpoint_id'], 'decision': 'approve', 'operator': '验收秘书'}).json()
    assert result['ok'], result
    pending = result['data']['pending_gate']
    data = e.client.get(base + '/issues').json()['data']
    issues = data['issues']
    assert {i['source'] for i in issues} >= {'extraction', 'template', 'consistency'}
    assert any(i['code'] == 'NUMERIC_MISMATCH' for i in issues)
    assert not any(i['blocking'] for i in issues)
    text = e.client.get(base + '/issues?format=text')
    assert text.headers['content-type'] == 'text/plain; charset=utf-8'
    assert 'attachment;' in text.headers['content-disposition']
    with e.rt.runner(created['data']['meeting_id']) as runner:
        context = runner.values()['minutes']['_context']
    assert text.content.decode('utf-8') == render_issues_text(
        data['meeting_id'], issues, complete=data['complete'],
        source_fingerprint=data['source_fingerprint'], source_version=context.get('source_version', ''))
    assert e.client.get(base + '/issues').json()['data'] == data
    request = {'checkpoint_id': pending['checkpoint_id'], 'decision': 'edit', 'operator': '验收秘书',
               'payload': {'minutes': {'topics': [{'resolutions': [{'content': '批准三十万元'}]}]}}}
    repaired = e.client.post(base + '/gate/G3/decide', json=request).json()
    assert repaired['ok'], repaired
    current = e.client.get(base + '/issues').json()['data']
    assert not any(i['code'] == 'NUMERIC_MISMATCH' for i in current['issues'])
    assert len(current['issues']) < len(issues)
    assert e.client.post(base + '/gate/G3/decide', json=request).json()['data']['resumed'] is False
    assert e.client.get(base + '/issues').json()['data'] == current
