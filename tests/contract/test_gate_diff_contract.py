"""C5 无状态差异、投影与本轮确认的真实 HTTP 合同。"""
from copy import deepcopy
import pytest
from mmwf.infra.db import connect_ctx


def diff(e, gate, payload, checkpoint=None):
    return e.client.post(e.base + f'/gate/{gate}/diff', json={
        'checkpoint_id': checkpoint or e.pending['checkpoint_id'], 'payload': payload}).json()


def snapshot(e):
    with connect_ctx(e.rt.db_path) as conn:
        db = {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')]
              for table in ('task_state', 'audit_log', 'idempotency_record')}
    root = e.rt.cfg.resolve_path(e.rt.cfg.app.data_dir) / e.mid
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with e.rt.runner(e.mid) as runner:
        return db, files, runner.values(), runner.status().to_dict(), list(e.calls)


def test_g2_projection_and_no_writes(gate_env):
    e = gate_env
    before = snapshot(e)
    payload = {'topics': deepcopy(e.pending['payload']['topics'])}
    payload['topics'][0]['title'] = '人工修订预算'
    body = diff(e, 'g2', payload)
    assert body['ok'], body
    assert body['data']['summary']['changed'] > 0
    assert set(body['data']['summary']) == {'changed', 'unchanged', 'added', 'removed'}
    assert snapshot(e) == before
    projected = e.client.get(e.base + '/topics').json()['data']['topics']
    for topic in projected:
        assert set(('title', 'extracted', 'raw_text', 'basis', 'risk', 'confidence', 'confirmed')) <= topic.keys()
        assert topic['confirmed'] is False
        assert topic['basis'] and topic['raw_text'] != '暂无出处，请核对'
        assert 'segment_id' in topic['basis'][0] and 'start_ms' in topic['basis'][0]
    assert snapshot(e) == before


@pytest.mark.parametrize('decision', ['approve', 'edit', 'reject'])
def test_round_confirmation(gate_env, decision):
    e = gate_env
    payload = {'topics': deepcopy(e.pending['payload']['topics'])}
    body = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': decision, 'operator': '秘书', 'payload': payload}).json()
    assert body['ok'], body
    topics = e.client.get(e.base + '/topics').json()['data']['topics']
    assert topics and all(t['confirmed'] is (decision != 'reject') for t in topics)
    with e.rt.runner(e.mid) as runner:
        assert all('extracted' not in t and 'basis' not in t for t in runner.values()['topics'])


@pytest.mark.parametrize('gate,checkpoint,payload', [
    ('G2', 'stale', {'topics': []}), ('G3', None, {'minutes_markdown': '正文'}),
    ('bad', None, {}), ('G2', None, {}), ('G2', None, {'topics': None}),
    ('G2', None, {'minutes_markdown': '越权'}), ('G2', None, {'topics': [{'title': '新增'}]}),
])
def test_invalid_is_read_only(gate_env, gate, checkpoint, payload):
    before = snapshot(gate_env)
    body = diff(gate_env, gate, payload, checkpoint)
    assert body['ok'] is False and body['code'] == 'E4001'
    assert snapshot(gate_env) == before


def test_auth_and_shape(gate_env):
    e = gate_env
    before = snapshot(e)
    body = e.client.post(e.base + '/gate/G2/diff', json={}, headers={'X-MMWF-Token': 'bad'}).json()
    assert body['code'] == 'E6001'
    for body in ({}, {'payload': {}}, {'checkpoint_id': 1, 'payload': {}}, [],
                 {'checkpoint_id': e.pending['checkpoint_id'], 'payload': {}, 'extra': 1}):
        result = e.client.post(e.base + '/gate/G2/diff', json=body).json()
        assert result['code'] == 'E4001'
    assert snapshot(e) == before


@pytest.mark.parametrize('base,edited', [
    ('甲\n乙\n\n丙', '甲\r\n乙\r\n\r\n丙'),
    ('甲\n乙\n\n丙', '甲\n修改\n\n丙'),
    ('甲\n\n乙', '甲\n\n新增\n\n乙'),
    ('甲\n\n删除\n\n乙', '甲\n\n乙'),
    ('\n\n'.join(f'段{i}' for i in range(100)), '\n\n'.join(f'段{i}' for i in range(100))),
])
def test_g3_vectors(gate_env, base, edited):
    e = gate_env
    result = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书'}).json()
    checkpoint = result['data']['pending_gate']['checkpoint_id']
    # D07：旧任意正文向量不再是合法的业务编辑；保留它们作为 HTTP 拒绝反例。
    before = snapshot(e)
    legacy = diff(e, 'G3', {'minutes_markdown': edited}, checkpoint)
    assert not legacy['ok'] and legacy['code'] == 'E4001'
    assert snapshot(e) == before
    body = diff(e, 'G3', {'minutes': {'topics': [{'name': '预算执行议题'}, {}]}}, checkpoint)
    assert body['ok'], body
    assert body['data']['summary']['changed'] > 0
    assert body['data']['minutes_markdown']
    assert snapshot(e) == before
