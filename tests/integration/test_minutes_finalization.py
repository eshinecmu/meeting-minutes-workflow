"""D5b：真图、HTTP、已登记恢复和缓存均不能跳过纪要硬检。"""
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

import pytest

from mmwf.app.services.minutes import MinutesService
from mmwf.domain.errors import GroundingBlockedError, MmwfError
from test_post_validation_pipeline import runtime
from test_minutes_checkpoint_compat import write_old_checkpoint
from mmwf.graph.idempotency import IdempotencyStore, make_idempotency_key, node_input_from_state
from mmwf.graph.nodes.export import make_export
from mmwf.infra.db import connect_ctx


def to_g3(e):
    result = e.client.post(e.base + '/gate/G2/decide', json={
        'checkpoint_id': e.pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书'}).json()
    assert result['ok'], result
    return result['data']['pending_gate']


def approve(e, pending, decision='approve', payload=None):
    return e.client.post(e.base + '/gate/G3/decide', json={
        'checkpoint_id': pending['checkpoint_id'], 'decision': decision,
        'payload': payload, 'operator': '秘书'}).json()


def rows(e):
    with connect_ctx(e.rt.db_path) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM minutes_version WHERE meeting_id=? ORDER BY version_no', (e.mid,))]


def corrupt(state):
    bad = deepcopy(state)
    bad['minutes_structured']['topics'][0]['discussion'] = '完全编造的结论没有依据'
    bad['minutes_structured']['topics'][0]['source_refs'] = []
    return bad


@pytest.mark.parametrize('decision', ['approve', 'edit', 'reject'])
def test_g3_versions_and_actual_docx(gate_env, decision):
    e = gate_env
    pending = to_g3(e)
    assert len(rows(e)) == 1 and rows(e)[0]['is_final'] == 0
    fs = e.rt.filestore(e.mid)
    for entry in fs.manifest():
        if entry.path.startswith('record/') and entry.path.endswith('.md'):
            fs.write_text(entry.path, '# 记录稿专用哨兵：不应成为纪要正文')
    payload = {'minutes': {'topics': [{'name': '预算执行议题'}, {}]}} if decision == 'edit' else None
    result = approve(e, pending, decision, payload)
    assert result['ok'], result
    if decision == 'reject':
        assert not any(r['is_final'] for r in rows(e))
        assert not e.rt.filestore(e.mid).resolve('export').exists()
        pending = result['data']['pending_gate']
        result = approve(e, pending)
        assert result['ok'], result
    with e.rt.runner(e.mid) as runner:
        state = runner.values()
        assert runner.status().status == 'archived'
    service = MinutesService(e.rt.cfg)
    latest = service.store().get(e.mid)
    assert latest['is_final'] == 1
    assert latest['structured_json'] == state['minutes_structured']
    assert latest['markdown'] == state['minutes_markdown']
    if decision == 'edit':
        assert latest['created_by'] == '秘书'
        assert len(rows(e)) == 2
    path = state['export_paths']['docx']
    response = e.client.get(e.base + '/export/' + path.split('/')[-1])
    assert response.headers['content-type'].startswith('application/vnd.openxmlformats')
    with ZipFile(BytesIO(response.content)) as archive:
        xml = archive.read('word/document.xml').decode()
    assert ('预算执行议题' if decision == 'edit' else '预算') in xml
    assert '记录稿专用哨兵' not in xml
    for topic in state['minutes_structured']['topics']:
        assert topic['discussion'] in xml
    assert 'rec://' not in xml
    count = len(rows(e))
    repeated = e.client.post(e.base + '/export', json={}).json()
    assert repeated['ok'] and repeated['data']['path'] == path
    assert repeated['data']['reused']
    assert len(rows(e)) == count
    assert e.client.post(e.base + '/export', json={'artifact': 'minutes', 'version': 999}).json()['code'] == 'E1002'
    assert e.client.post(e.base + '/export', json={'artifact': 'other'}).json()['code'] == 'E4001'


def test_direct_runner_blocks_and_persists_failed(gate_env):
    e = gate_env
    to_g3(e)
    with e.rt.runner(e.mid) as runner:
        runner.app.update_state(runner._cfg, corrupt(runner.values()), as_node='assemble')
        runner._invoke(None)
        with pytest.raises(GroundingBlockedError):
            runner.resume({'decision': 'approve'})
        assert runner.status().status == 'failed'
    with e.rt.runner(e.mid) as runner:
        assert runner.status().status == 'failed'
    assert not any(r['is_final'] for r in rows(e))
    assert not e.rt.filestore(e.mid).resolve('export').exists()
    assert not e.rt.filestore(e.mid).exists('archive/manifest.json')
    for payload in ({}, {'artifact': 'minutes'}, {'artifact': 'minutes', 'version': 1}):
        result = e.client.post(e.base + '/export', json=payload).json()
        assert result['code'] == 'E3002', result
    # 显式记录稿仍独立可用，但不会把纪要版本标 final。
    result = e.client.post(e.base + '/export', json={'artifact': 'record'}).json()
    assert result['ok'] and '记录稿' in result['data']['path']
    assert not any(r['is_final'] for r in rows(e))


def test_graph_cache_and_http_download_do_not_hide_bad_candidate(gate_env, monkeypatch):
    e = gate_env
    pending = to_g3(e)
    assert approve(e, pending)['ok']
    with e.rt.runner(e.mid) as runner:
        state = runner.values()
        deps = runner.deps
    store = IdempotencyStore(e.rt.db_path)
    deps.idem_store = store
    node = make_export(deps)
    first = node(state)
    assert node(state)['export_paths'] == first['export_paths']
    # 为坏候选预置命中记录，校验仍须早于 get。
    bad = corrupt(state)
    key = make_idempotency_key(e.mid, 'export', node_input_from_state('export', bad))
    store.put(key, e.mid, 'export', '{"paths": {"docx": "export/缓存.docx"}, "warnings": []}')
    def no_cache(*args):
        raise AssertionError('硬检失败时不应查询缓存')
    monkeypatch.setattr(store, 'get', no_cache)
    with pytest.raises(GroundingBlockedError):
        node(bad)
    # 草稿文件即使提前存在，也不能通过下载泄露。
    with e.rt.runner(e.mid) as runner:
        runner.app.update_state(runner._cfg, bad)
    assert e.client.post(e.base + '/export', json={}).json()['code'] == 'E3002'
    name = first['export_paths']['docx'].split('/')[-1]
    assert e.client.get(e.base + '/export/' + name).json()['code'] == 'E3002'
    assert e.rt.filestore(e.mid).exists('archive/manifest.json')  # 已归档文件不追溯删除。


@pytest.mark.parametrize('invalid', [False, True])
def test_registered_decision_replay_rechecks_before_final(gate_env, monkeypatch, invalid):
    e = gate_env
    pending = to_g3(e)
    service = e.rt.gate_service()
    def power_loss(*args):
        raise RuntimeError('模拟登记后断电')
    with monkeypatch.context() as patch:
        patch.setattr(service, '_resume', power_loss)
        with pytest.raises(RuntimeError):
            service.apply_decision(e.mid, 'G3', pending['checkpoint_id'], 'approve', None, '秘书', '')
    record = service.store.get(e.mid, 'G3', pending['checkpoint_id'])
    assert record
    with e.rt.runner(e.mid) as runner:
        if invalid:
            bad = corrupt(runner.values())
            record['resume']['_g3_candidate'] = {k: bad[k] for k in ('minutes_structured', 'minutes_markdown', 'issues')}
            with pytest.raises(GroundingBlockedError):
                service._resume(runner, record)
            assert runner.status().pending_gate.gate == 'G3'
        else:
            assert service._resume(runner, record)['next_state'] == 'archived'
    assert len(rows(e)) == 1
    assert bool(rows(e)[0]['is_final']) is not invalid
    assert e.rt.filestore(e.mid).resolve('export').exists() is not invalid


def test_http_before_g3_cannot_finalize(gate_env):
    e = gate_env
    to_g3(e)
    assert e.client.post(e.base + '/export', json={}).json()['code'] == 'E4001'
    assert not any(r['is_final'] for r in rows(e))
    assert not e.rt.filestore(e.mid).resolve('export').exists()


def test_nonfinal_download_and_record_version_scope(gate_env):
    e = gate_env
    to_g3(e)
    fs = e.rt.filestore(e.mid)
    name = f'{e.mid}-纪要-v1.docx'
    fs.write('export/' + name, '草稿缓存'.encode('utf-8'), stage='export')
    assert e.client.get(e.base + '/export/' + name).json()['code'] == 'E4001'
    assert not any(r['is_final'] for r in rows(e))
    assert e.client.post(e.base + '/export', json={'artifact': 'record', 'version': 999}).json()['code'] == 'E1002'


def test_legacy_record_cache_is_rebuilt_as_minutes(gate_env):
    e = gate_env
    pending = to_g3(e)
    assert approve(e, pending)['ok']
    with e.rt.runner(e.mid) as runner:
        state, deps = runner.values(), runner.deps
    store = IdempotencyStore(e.rt.db_path)
    deps.idem_store = store
    key = make_idempotency_key(e.mid, 'export', node_input_from_state('export', state))
    store.drop(key)
    store.put(key, e.mid, 'export', '{"paths": {"docx": "export/旧记录稿.docx"}, "warnings": []}')
    result = make_export(deps)(state)
    assert '-纪要-v1.docx' in result['export_paths']['docx']
    assert len(rows(e)) == 1


@pytest.mark.parametrize('missing_source', [False, True])
def test_frozen_next_export_rechecks_without_fabricating_history(runtime, missing_source):
    before, counts, checkpoint_id = write_old_checkpoint(runtime, stop='export', missing_source=missing_source)
    original_counts = deepcopy(counts)
    with runtime.runner('d4') as runner:
        assert runner.checkpoint().next == ('export',)
        assert runner.checkpoint().config['configurable']['checkpoint_id'] == checkpoint_id
        with pytest.raises(MmwfError) as caught:
            runner._invoke(None)
        # 缺源必须硬挡；旧 Markdown 无法无损同步时不能静默重写并批准。
        assert caught.value.code == ('E3002' if missing_source else 'E4001')
        assert runner.values()['minutes_markdown'] == before['minutes_markdown']
        assert all(runner.run_counts().get(n, 0) == 0 for n in ('transcribe', 'extract', 'generate', 'assemble', 'archive'))
    assert counts == original_counts
    assert MinutesService(runtime.cfg).store().get('d4') is None
    assert not runtime.filestore('d4').resolve('export').exists()


def test_archived_without_versions_does_not_backfill_history(gate_env):
    e = gate_env
    pending = to_g3(e)
    assert approve(e, pending)['ok']
    fs = e.rt.filestore(e.mid)
    before = sorted(str(p) for p in fs.resolve('export').iterdir())
    with connect_ctx(e.rt.db_path) as conn:
        conn.execute('DELETE FROM minutes_version WHERE meeting_id=?', (e.mid,))
    result = e.client.post(e.base + '/export', json={}).json()
    assert result['code'] == 'E4001', result
    assert rows(e) == []
    assert sorted(str(p) for p in fs.resolve('export').iterdir()) == before


@pytest.mark.parametrize('next_node', ['generate', 'assemble', 'export'])
def test_http_after_g3_reject_cannot_finalize_from_checkpoint_position(gate_env, monkeypatch, next_node):
    e = gate_env
    pending = to_g3(e)
    # 真实 reject 已登记并推进；模拟重做 assemble 时中断，保留可恢复检查点。
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise RuntimeError('模拟驳回后重做中断')
        patch.setattr(MinutesService, 'save_draft', interrupted)
        assert not approve(e, pending, 'reject')['ok']
    with e.rt.runner(e.mid) as runner:
        assert runner.checkpoint().next == ('assemble',)
        record = e.rt.gate_service().store.get(e.mid, 'G3', pending['checkpoint_id'])
        assert record['request']['decision'] == 'reject'
        if next_node != 'assemble':
            # 覆盖重试调度和伪造 next=export；位置不能代替 G3 审定。
            runner.app.update_state(runner._cfg, {'_gate_route': 'continue'},
                                    as_node='gate_g2' if next_node == 'generate' else 'gate_g3')
        assert runner.checkpoint().next == (next_node,)
        assert runner.status().pending_gate is None
    result = e.client.post(e.base + '/export', json={'artifact': 'minutes'}).json()
    assert result['code'] == 'E4001', result
    assert not any(r['is_final'] for r in rows(e))
    assert not e.rt.filestore(e.mid).resolve('export').exists()


@pytest.mark.parametrize('archived', [False, True])
def test_phase1_record_filename_download_with_workflow(gate_env, archived):
    e = gate_env
    pending = to_g3(e)
    if archived:
        assert approve(e, pending)['ok']
    fs = e.rt.filestore(e.mid)
    record = e.rt.export_service(e.mid).export_docx(artifact='record')
    payload = fs.read(record.rel_path)
    # 一期路径无类型标签；历史交付件仍为记录稿，不回填纪要历史。
    legacy = f'{e.mid}-v1.docx'
    fs.write('export/' + legacy, payload, stage='export')
    before = rows(e)
    response = e.client.get(e.base + '/export/' + legacy)
    assert response.content == payload
    assert response.headers['content-type'].startswith('application/vnd.openxmlformats')
    assert rows(e) == before
    if not archived:
        assert not any(r['is_final'] for r in before)
        assert e.client.post(e.base + '/export', json={}).json()['code'] == 'E4001'
