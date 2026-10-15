"""D5a：真实 HTTP 问题清单、G3 配对与预检；不覆盖 D5b 交付。"""
import pytest

from mmwf.domain.errors import GroundingBlockedError, SchemaViolationError
from mmwf.domain.minutes.issues import make_issue
from mmwf.infra.db import connect_ctx


def g3(e):
    result = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书'}).json()
    assert result['ok'], result
    return result['data']['pending_gate']


def decision(e, pending, payload=None, action='edit'):
    return e.client.post(e.base + '/gate/G3/decide', json={
        'checkpoint_id': pending['checkpoint_id'], 'decision': action,
        'payload': payload, 'operator': '秘书'}).json()


def test_issues_json_text_failed_and_unknown(gate_env):
    e = gate_env
    g3(e)
    issues = [make_issue('CHUNK_META_MISSING', 'title', '缺元信息', source='extraction'),
              make_issue('REQUIRED_TITLE', 'title', '缺标题', source='template'),
              make_issue('REF_MISMATCH', 'topics[0]', '依据不符', source='grounding', level='error', blocking=True)]
    with e.rt.runner(e.mid) as runner:
        runner.app.update_state(runner._cfg, {'issues': issues + issues, 'stage': 'validation_blocked'})
    data = e.client.get(e.base + '/issues').json()['data']
    assert set(data) == {'meeting_id', 'issues', 'counts', 'complete', 'source_fingerprint'}
    assert data['counts'] == {'error': 1, 'warning': 2, 'info': 0} and data['complete']
    response = e.client.get(e.base + '/issues?format=text')
    assert response.headers['content-type'] == 'text/plain; charset=utf-8'
    assert response.headers['content-disposition'] == f'attachment; filename="issues-{e.mid}.txt"'
    assert '\r' not in response.text
    for item in data['issues']:
        assert response.text.count(item['code']) == 1
        assert item['message'] in response.text
    assert e.client.get(e.base + '/issues?format=bad').json()['code'] == 'E4001'
    assert e.client.get(e.base + '/issues', headers={'X-MMWF-Token': 'bad'}).json()['code'] == 'E6001'
    assert not e.client.get('/api/v1/meetings/m-missing/issues').json()['ok']
    # 没有跑过检查的会议与零问题有别。
    created = e.client.post('/api/v1/meetings', json={'title': '新会', 'record_markdown': '原文'}).json()['data']['meeting_id']
    unknown = e.client.get(f'/api/v1/meetings/{created}/issues').json()['data']
    assert unknown['issues'] == [] and unknown['complete'] is False
    assert '历史检查未完成' in e.client.get(f'/api/v1/meetings/{created}/issues?format=text').text


def test_g3_structured_preview_double_write_and_no_rerun(gate_env):
    e = gate_env
    pending = g3(e)
    payload = {'minutes': {'topics': [{'name': '预算执行议题'}, {}]}}
    with e.rt.runner(e.mid) as runner:
        original = runner.values()
        checkpoint = runner.status().pending_gate.checkpoint_id
    preview = e.client.post(e.base + '/gate/G3/diff', json={'checkpoint_id': checkpoint, 'payload': payload}).json()
    assert preview['ok'], preview
    with e.rt.runner(e.mid) as runner:
        assert runner.values() == original
        assert runner.status().pending_gate.checkpoint_id == checkpoint
    e.calls.clear()
    payload['minutes_markdown'] = preview['data']['minutes_markdown'].replace('\n', '\r\n')
    result = decision(e, pending, payload)
    assert result['ok'], result
    assert result['data']['rerun_nodes'] == []
    assert all(name not in {'transcribe', 'extract', 'generate', 'assemble'} for _, name in e.calls)
    with e.rt.runner(e.mid) as runner:
        values = runner.values()
    assert values['minutes_markdown'] == preview['data']['minutes_markdown']
    assert values['minutes_structured']['topics'][0]['name'] == '预算执行议题'
    assert values['minutes_structured']['topics'][0]['source_refs'] == original['minutes_structured']['topics'][0]['source_refs']
    assert values['issues'] == preview['data']['issues']
    assert decision(e, pending, payload)['data']['resumed'] is False


@pytest.mark.parametrize('payload', [
    {'minutes_markdown': '# 随意改写'}, {'minutes': {'issues': []}},
    {'minutes': {'topics': []}}, {'minutes': {'topics': [{'source_refs': []}, {}]}},
    {'minutes': {'topics': [{'name': '预算'}, {}]}, 'minutes_markdown': '不同步'},
])
def test_invalid_g3_preserves_checkpoint_and_decision(gate_env, payload):
    e = gate_env
    pending = g3(e)
    with e.rt.runner(e.mid) as runner:
        before = runner.values()
    result = decision(e, pending, payload)
    assert result['code'] == 'E4001', result
    with e.rt.runner(e.mid) as runner:
        assert runner.values() == before
        assert runner.status().pending_gate.checkpoint_id == pending['checkpoint_id']
    with connect_ctx(e.rt.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='gate_decision'").fetchone()[0] == 1


def test_legacy_markdown_layout_success(gate_env):
    e = gate_env
    pending = g3(e)
    old = pending['payload']['minutes_markdown']
    result = decision(e, pending, {'minutes_markdown': old.replace('\n', '\r\n\r\n')})
    assert result['ok'], result
    with e.rt.runner(e.mid) as runner:
        assert runner.values()['minutes_structured'] == pending['payload']['minutes_structured']


@pytest.mark.parametrize('action', ['approve', 'edit'])
def test_missing_source_precheck_preserves_g3(gate_env, action):
    e = gate_env
    pending = g3(e)
    with e.rt.runner(e.mid) as runner:
        runner.app.update_state(runner._cfg, {'record_markdown': '', 'transcript': []}, as_node='assemble')
        runner.app.invoke(None, runner._cfg)
        pending = runner.status().pending_gate.to_dict()
        before = runner.values()
    result = decision(e, pending, {'minutes': {'title': '预算会'}}, action)
    assert result['code'] == 'E3002' and '已停止成文' in result['message'], result
    with e.rt.runner(e.mid) as runner:
        assert runner.values() == before
        assert runner.status().pending_gate.checkpoint_id == pending['checkpoint_id']


def test_error_severity_compatibility():
    assert GroundingBlockedError().to_dict()['severity'] == 'error'
    assert SchemaViolationError().to_dict()['severity'] == 'warning'


def test_candidate_repairs_current_diagnostics(gate_env):
    e = gate_env
    pending = g3(e)
    checkpoint = pending['checkpoint_id']
    payload = {'minutes': {'host': '张三'}}
    first = e.client.post(e.base + '/gate/G3/diff', json={'checkpoint_id': checkpoint, 'payload': payload}).json()
    assert first['ok'], first
    assert not any(i['source'] == 'compat' and i['target'] == 'host' and i['code'] == 'META_MISSING'
                   for i in first['data']['issues'])
    again = e.client.post(e.base + '/gate/G3/diff', json={'checkpoint_id': checkpoint, 'payload': payload}).json()
    assert again['data'] == first['data']
    before = e.client.get(e.base + '/issues').json()['data']
    assert decision(e, pending, payload)['ok']
    current = e.client.get(e.base + '/issues').json()['data']
    assert len(current['issues']) < len(before['issues'])


def test_endpoint_inventory_d10(gate_env):
    from tools.accept_t1_17 import SAD_9_1, KNOWN_EXTRA
    from mmwf.api.routes import build_router
    actual = {(method, '/api/v1' + route.path) for route in build_router(gate_env.rt.cfg).routes
              for method in (getattr(route, 'methods', None) or []) if method not in {'HEAD', 'OPTIONS'}}
    assert len(SAD_9_1) == 34
    assert actual == SAD_9_1 | KNOWN_EXTRA
    assert len(actual) == 35 and len({path for _, path in actual}) == 30
