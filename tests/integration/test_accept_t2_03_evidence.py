"""C6 补齐真实六分支审计逐轮唯一性，供标准 6 复用。"""
from copy import deepcopy
import json

import pytest

from mmwf.domain.review.gate_edit import fingerprint
from mmwf.infra.audit import all_records, verify_chain


@pytest.mark.parametrize('gate', ['G2', 'G3'])
@pytest.mark.parametrize('decision', ['approve', 'edit', 'reject'])
def test_each_real_decision_has_one_audit(gate_env, gate, decision):
    e = gate_env
    pending = e.pending
    if gate == 'G3':
        pending = e.client.post(e.base + '/gate/G2/decide', json={
            'checkpoint_id': pending['checkpoint_id'], 'decision': 'approve', 'operator': '秘书',
        }).json()['data']['pending_gate']
    payload = None
    if decision == 'edit':
        if gate == 'G2':
            topics = deepcopy(pending['payload']['topics'])
            topics[0]['title'] = '验收人工修订'
            payload = {'topics': topics}
        else:
            payload = {'minutes_markdown': '# 验收人工修订\n\n审定正文。'}
    request = {'checkpoint_id': pending['checkpoint_id'], 'decision': decision,
               'operator': '验收秘书', 'payload': payload, 'comment': '自由备注不外泄'}
    url = e.base + f'/gate/{gate}/decide'
    body = e.client.post(url, json=request).json()
    assert body['ok'], body
    expected = gate + '_awaiting' if decision == 'reject' else ('G3_awaiting' if gate == 'G2' else 'archived')
    assert body['data']['next_state'] == expected
    e.calls.clear()
    retry = e.client.post(url, json=request).json()
    assert retry['ok'] and retry['data']['resumed'] is False and not e.calls
    records = [r for r in all_records(e.rt.db_path) if r.action == 'gate_decision'
               and r.detail['checkpoint_id'] == pending['checkpoint_id']]
    assert len(records) == 1
    rec = records[0]
    detail = rec.detail
    assert rec.operator == '验收秘书' and rec.ts and rec.result == 'success'
    assert detail['decision'] == decision and detail['gate'] == gate
    assert detail['auto'] is False and detail['reason'] == 'human'
    snapshot = json.loads((e.rt.cfg.app.data_dir / e.mid / detail['snapshot_ref']).read_text())
    assert detail['before_sha256'] == fingerprint(snapshot['before'])
    assert detail['after_sha256'] == fingerprint(snapshot['after'])
    assert '自由备注不外泄' not in rec.detail_raw and '验收人工修订' not in rec.detail_raw
    if decision == 'edit':
        assert detail['changed_keys'] == ['topics[0].title' if gate == 'G2' else 'minutes_markdown']
    else:
        assert detail['changed_keys'] == [] and detail['changed'] == {}
    assert verify_chain(e.rt.db_path).ok
