"""D5b：配对加密、会议内编号、重放及事务回滚。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from mmwf.domain.errors import MmwfError
from mmwf.infra.db import connect_ctx
from mmwf.infra.minutes_store import MinutesStore
from mmwf.infra.migrations.runner import migrate


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'mmwf.db'
    migrate(path)
    with connect_ctx(path) as conn:
        conn.execute("INSERT INTO meeting (meeting_id,title,status,created_by,created_at,updated_at) VALUES ('m','会议','created','tester','now','now')")
    return MinutesStore(path)


def save(store, number=1):
    return store.save('m', {'title': '敏感结构', 'number': number}, '# 敏感正文',
        {'pair_fingerprint': str(number), 'issues': [{'message': '敏感清单'}]}, created_by='秘书')


def test_pair_encrypted_roundtrip_and_replay(store):
    first = save(store)
    assert first['version_no'] == 1 and first['is_final'] == 0
    assert first['created_by'] == '秘书'
    assert first['structured_json']['title'] == '敏感结构'
    assert first['markdown'] == '# 敏感正文'
    assert first['note']['issues'][0]['message'] == '敏感清单'
    assert save(store)['version_id'] == first['version_id']
    assert MinutesStore(store.db_path).get('m') == first
    with connect_ctx(store.db_path) as conn:
        row = conn.execute('SELECT structured_json,markdown,note FROM minutes_version').fetchone()
    assert all('敏感' not in text and 'encrypted' in text for text in row)
    assert save(store, 2)['version_no'] == 2
    assert store.get('m', 999) is None


def test_lock_transaction_numbering_and_same_pair(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda i: save(store, i), [1, 2, 1, 3, 2, 4]))
    assert len({r['version_id'] for r in rows}) == 4
    assert sorted({r['version_no'] for r in rows}) == [1, 2, 3, 4]


def test_final_checks_pair_and_failed_write_rolls_back(store, monkeypatch):
    save(store)
    with pytest.raises(MmwfError) as caught:
        store.mark_final('m', 1, 'wrong')
    assert caught.value.code == 'E4001'
    assert store.get('m', 1)['is_final'] == 0
    assert store.mark_final('m', 1, '1')['is_final'] == 1
    encode = store._encode
    def broken(value):
        if isinstance(value, str):
            raise RuntimeError('模拟写入中断')
        return encode(value)
    monkeypatch.setattr(store, '_encode', broken)
    with pytest.raises(RuntimeError):
        save(store, 2)
    monkeypatch.setattr(store, '_encode', encode)
    assert save(store, 2)['version_no'] == 2
