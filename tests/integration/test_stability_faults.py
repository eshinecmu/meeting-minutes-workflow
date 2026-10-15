"""G3 batch 2: deterministic faults; real SIGKILL, SQLite and persisted files.

裁决：§03 STAB-05 只要求重试及有界超时；按 §01 §9.3 使用 E9001，
不擅定新的锁超时码。恢复只要求从最后已提交节点续跑，不要求恢复半次 ASR。
STAB-01/02 的资源曲线与真实八小时待机留目标机/人工，本文件不替代它们。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import errno
import json
import os
import shutil
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mmwf.api.deps import create_session_token
from mmwf.domain.asr_types import TranscriptResult, TranscriptSegmentResult
from mmwf.graph.build import make_checkpointer
from mmwf.graph.runner import GraphRunner
from mmwf.infra.db import connect_ctx
from mmwf.infra.filestore import make_filestore
from mmwf.infra.migrations.runner import migrate
from mmwf.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]
MID = 'stability'
TEXT = '今天研究季度工作安排。'


def settings_at(root):
    cfg = load_settings(ROOT / 'config')
    return cfg.model_copy(update={'app': cfg.app.model_copy(update={
        'data_dir': root / 'data', 'log_dir': root / 'logs'})})


def seed(cfg):
    root = cfg.resolve_path(cfg.app.data_dir)
    migrate(root / 'mmwf.db')
    with connect_ctx(root / 'mmwf.db') as conn:
        conn.execute("INSERT OR IGNORE INTO meeting (meeting_id,title,created_by,created_at,updated_at) VALUES (?,?,?,?,?)",
                     (MID, '稳定性合成会议', 'test', 'now', 'now'))
    store = make_filestore(MID, root)
    if not store.resolve('audio/original.wav').exists():
        store.write('audio/original.wav', b'SYNTHETIC audio', stage='audio')
    return store


class ASR:
    name = 'deterministic'

    def __init__(self, entered=None, release=None):
        self.entered, self.release = entered, release
        self.calls = 0

    def available(self):
        return True

    def transcribe(self, path, hotwords=None):
        self.calls += 1
        if self.entered:
            self.entered.set()
            assert self.release.wait(15), 'ASR release not received'
        return TranscriptResult(engine=self.name, model='fake', segments=(
            TranscriptSegmentResult(seq=1, text=TEXT, start_ms=0, end_ms=1000,
                                    confidence=1.0),))


def runner_at(root, engine):
    # Only external ingest/review/export are stubs; transcribe + checkpoints are real.
    import harness
    cfg = settings_at(root)
    saver = make_checkpointer(root / 'checkpoint.db')
    runs = harness.Runs()
    app = harness.build_test_app(
        runs=runs, real=('transcribe',), checkpointer=saver,
        replace={'ingest': {'chunk_plan': [{'source': 'synthetic.wav'}]}},
        settings=cfg, provider_factory=lambda _: engine,
        glossary_factory=lambda *_: None)
    runner = GraphRunner(MID, app=app, deps=SimpleNamespace(settings=cfg))
    runner._saver = saver  # injected app still gives runner ownership of its connection
    return runner, runs


def worker(root, stop):
    cfg = settings_at(root)
    store = seed(cfg)

    class BlockingASR(ASR):
        def transcribe(self, path, hotwords=None):
            if stop == 'transcribe':
                print('READY:transcribe', flush=True)
                sys.stdin.readline()  # parent kills; no normal-close path
            return super().transcribe(path, hotwords)

    with runner_at(root, BlockingASR())[0] as runner:
        status = runner.start()
        assert status.pending_gate.gate == 'G2'
        (root / 'before.json').write_text(json.dumps(runner.values(), ensure_ascii=False), encoding='utf-8')
        assert store.read_json('transcript/segments.json')['segments'][0]['text'] == TEXT
        print('READY:G2', flush=True)
        sys.stdin.readline()


@contextmanager
def killed_worker(root, stop):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / 'src'), str(ROOT / 'tests/fixtures/graph'))))
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(root), stop],
                            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(proc.stdout.readline)
            try:
                line = ready.result(timeout=20)
            except FutureTimeout:
                proc.kill()
                raise AssertionError('child did not reach fault barrier')
        assert line.strip() == f'READY:{stop}', line
        proc.kill()
        assert proc.wait(timeout=10) == -signal.SIGKILL
        yield
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


def test_stab03_kill_during_transcribe_recovers_committed_input(tmp_path):
    with killed_worker(tmp_path, 'transcribe'):
        cfg = settings_at(tmp_path)
        store = make_filestore(MID, cfg.resolve_path(cfg.app.data_dir))
        old_audio = store.resolve('audio/original.wav').read_bytes()
        assert not store.resolve('transcript/segments.json').exists()
        engine = ASR()
        with runner_at(tmp_path, engine)[0] as runner:
            assert runner.checkpoint().next == ('transcribe',)
            runner._invoke(None)  # LangGraph continuation of the interrupted checkpoint
            assert runner.status().pending_gate.gate == 'G2'
            assert runner.values()['transcript'][0]['text'] == TEXT
            assert runner.values()['record_markdown']
        assert engine.calls == 1
        assert store.read_json('transcript/segments.json')['segments'][0]['text'] == TEXT
        assert store.resolve('audio/original.wav').read_bytes() == old_audio
        assert store.verify_manifest() == []


def test_it08_kill_at_g2_preserves_payload_and_does_not_retranscribe(tmp_path):
    with killed_worker(tmp_path, 'G2'):
        cfg = settings_at(tmp_path)
        store = make_filestore(MID, cfg.resolve_path(cfg.app.data_dir))
        saved = store.resolve('transcript/segments.json').read_bytes()
        before = json.loads((tmp_path / 'before.json').read_text(encoding='utf-8'))
        engine = ASR()
        runner, runs = runner_at(tmp_path, engine)
        with runner:
            status = runner.status()
            assert status.status == 'G2_awaiting'
            assert status.pending_gate.payload['topics'] == before['topics']
            assert runner.values() == before
            assert runner.resume({'decision': 'approve'}).pending_gate.gate == 'G3'
            assert runs.get('ingest') == runs.get('transcribe') == runs.get('review') == 0
        assert engine.calls == 0
        assert store.resolve('transcript/segments.json').read_bytes() == saved
        assert store.verify_manifest() == []


def test_it10_cancel_during_transcribe_preserves_disk_products(tmp_path):
    store = seed(settings_at(tmp_path))
    store.write_text('record/v1.md', '已完成的记录稿', stage='record')
    original = store.resolve('audio/original.wav').read_bytes()
    record = store.resolve('record/v1.md').read_bytes()
    manifest = store.manifest_path.read_bytes()
    entered, release = Event(), Event()
    runner, _ = runner_at(tmp_path, ASR(entered, release))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.start)
            try:
                assert entered.wait(10)
                assert not future.done()
                assert runner.cancel(reason='用户取消').status == 'cancelled'
                assert store.resolve('audio/original.wav').read_bytes() == original
                assert store.manifest_path.read_bytes() == manifest
            finally:
                release.set()
            assert future.result(timeout=10).status == 'cancelled'
        assert runner.status().status == 'cancelled'
        assert store.read('audio/original.wav') == b'SYNTHETIC audio'
        assert store.resolve('record/v1.md').read_bytes() == record
        assert store.verify_manifest() == []
    finally:
        release.set()
        runner.close()


@pytest.fixture
def api(tmp_path, monkeypatch):
    from mmwf.main import create_app
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    cfg = settings_at(tmp_path)
    app = create_app(cfg)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.headers['X-MMWF-Token'] = create_session_token()
        response = client.post('/api/v1/meetings', json={
            'title': '已有会议', 'record_markdown': '已有记录稿。'}).json()
        assert response['ok'], response
        yield SimpleNamespace(client=client, app=app, cfg=cfg,
                              mid=response['data']['meeting_id'], root=tmp_path)


@pytest.mark.parametrize('fault', ['product', 'manifest'])
def test_stab04_it23_enospc_reports_e9002_preserves_old_data(api, monkeypatch, fault):
    store = make_filestore(api.mid, api.cfg.resolve_path(api.cfg.app.data_dir))
    store.write_json('transcript/segments.json', {'segments': [{'text': '已有转写'}]})
    old_file = store.resolve('transcript/segments.json').read_bytes()
    old_manifest = store.manifest_path.read_bytes()
    db = api.app.state.runtime.db_path
    with connect_ctx(db) as conn:
        before = [tuple(r) for r in conn.execute('SELECT * FROM meeting')]
    write_bytes, write_text = Path.write_bytes, Path.write_text

    def full_bytes(path, data):
        if fault == 'product' and path.parent == store.resolve('transcript'):
            write_bytes(path, b'partial')
            raise OSError(errno.ENOSPC, 'fault: disk full')
        return write_bytes(path, data)

    def full_text(path, data, *args, **kwargs):
        if fault == 'manifest' and path == store.manifest_path.with_suffix('.json.tmp'):
            write_text(path, '{partial', *args, **kwargs)
            raise OSError(errno.ENOSPC, 'fault: disk full')
        return write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_bytes', full_bytes)
    monkeypatch.setattr(Path, 'write_text', full_text)
    import harness
    saver = make_checkpointer(api.root / 'disk-checkpoint.db')
    app = harness.build_test_app(runs=harness.Runs(), real=('transcribe',), checkpointer=saver,
        settings=api.cfg, provider_factory=lambda _: ASR(), glossary_factory=lambda *_: None,
        replace={'ingest': {'chunk_plan': [{'source': 'synthetic.wav'}]}})
    runner = GraphRunner(api.mid, app=app)
    monkeypatch.setattr(api.app.state.runtime, 'runner', lambda _: runner)
    try:
        response = api.client.post(f'/api/v1/meetings/{api.mid}/start')
    finally:
        saver.conn.close()
    body = response.json()
    assert response.status_code == 200
    assert not body['ok'] and body['code'] == 'E9002', body
    assert '磁盘空间不足' in body['message'] and '清理' in body['message'] and '重试' in body['message']
    assert 'fault:' not in body['message']
    assert store.resolve('transcript/segments.json').read_bytes() == old_file
    assert store.manifest_path.read_bytes() == old_manifest
    assert store.verify_manifest() == []
    assert list(store.store_root.rglob('*.tmp')) == []
    with connect_ctx(db) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert [tuple(r) for r in conn.execute('SELECT * FROM meeting')] == before


def test_stab05_database_lock_waits_then_succeeds(api):
    # SQLite busy_timeout retries internally; use a real write lock, not a mocked error.
    with connect_ctx(api.app.state.runtime.db_path) as lock:
        lock.execute('BEGIN IMMEDIATE')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api.client.post, '/api/v1/meetings', json={
                'title': '等待锁后写入', 'record_markdown': '记录。'})
            try:
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.2)
            finally:
                lock.execute('ROLLBACK')
            assert future.result(timeout=10).json()['ok']


def test_stab05_database_lock_times_out_with_e9001(api, monkeypatch):
    # 裁决：§03 未定超时码；§01 §9.3 的数据库不可用 E9001。
    # Keep SQLite's real busy handler, but inject 50 ms instead of waiting 5 s.
    from mmwf.infra import db as dbmod
    connect = dbmod.connect
    timeouts = []

    def short_timeout(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.execute('PRAGMA busy_timeout = 50')
        timeouts.append(conn.execute('PRAGMA busy_timeout').fetchone()[0])
        return conn

    monkeypatch.setattr(dbmod, 'connect', short_timeout)
    with connect_ctx(api.app.state.runtime.db_path) as lock:
        lock.execute('BEGIN IMMEDIATE')
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                future = pool.submit(api.client.post, '/api/v1/meetings', json={
                    'title': '不应写入', 'record_markdown': '记录。'})
                response = future.result(timeout=12)
            finally:
                # Release before executor shutdown, even if the bounded wait fails.
                lock.execute('ROLLBACK')
        assert timeouts and all(value == 50 for value in timeouts)
        body = response.json()
        assert not body['ok'] and body['code'] == 'E9001', body
        assert '重试' in body['message']
        assert 'database is locked' not in body['message']
        assert lock.execute("SELECT count(*) FROM meeting WHERE title='不应写入'").fetchone()[0] == 0
        assert lock.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.fixture
def model_load_failure(api, monkeypatch):
    # Exercise the real provider warmup/error translation with a fake AutoModel;
    # this proves error handling, not real FunASR/model compatibility.
    from mmwf.adapters.asr.funasr_local import FunASRLocalProvider
    import harness
    model_dir = api.root / 'model'
    model_dir.mkdir()
    (model_dir / 'model.bin').write_bytes(b'fake')
    audio = api.root / 'synthetic.wav'
    audio.write_bytes(b'fake')

    def broken_model(**kwargs):
        raise RuntimeError('PRIVATE_MODEL_LOAD_FAILURE')

    monkeypatch.setitem(sys.modules, 'funasr', SimpleNamespace(AutoModel=broken_model))
    provider = FunASRLocalProvider(model_dir=model_dir, device='cpu')
    saver = make_checkpointer(api.root / 'model-checkpoint.db')
    app = harness.build_test_app(runs=harness.Runs(), real=('transcribe',), checkpointer=saver,
        settings=api.cfg, provider_factory=lambda _: provider, glossary_factory=lambda *_: None,
        replace={'ingest': {'chunk_plan': [{'source': str(audio)}]}})
    runner = GraphRunner(api.mid, app=app)
    monkeypatch.setattr(api.app.state.runtime, 'runner', lambda _: runner)
    try:
        response = api.client.post(f'/api/v1/meetings/{api.mid}/start')
        body = response.json()
        assert response.status_code == 200
        assert not body['ok'] and body['code'] == 'E2001', body
        assert '模型' in body['message'] and '技术人员' in body['message']
        assert 'PRIVATE_MODEL_LOAD_FAILURE' not in json.dumps(body)
        assert body['trace_id']
        # Same action remains usable after the environment is repaired.
        provider._model = SimpleNamespace(generate=lambda **_: [{'text': TEXT, 'timestamp': [[0, 1000]]}])
        retried = api.client.post(f'/api/v1/meetings/{api.mid}/start').json()
        assert retried['ok'], retried
        assert retried['data']['pending_gate']['gate'] == 'G2'
        yield body
    finally:
        saver.conn.close()


def test_stab06_model_load_failure_api_guidance_and_retry(model_load_failure):
    assert model_load_failure['code'] == 'E2001'


def test_stab06_renderer_guidance_and_retry(model_load_failure, tmp_path):
    """Import unmodified ES modules with Node; only DOM/fetch are deterministic fakes."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node unavailable: renderer interaction remains unverified')
    renderer = tmp_path / 'renderer'
    shutil.copytree(ROOT / 'src/desktop/renderer', renderer)
    (renderer / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    envelope = tmp_path / 'failure.json'
    envelope.write_text(json.dumps(model_load_failure, ensure_ascii=False), encoding='utf-8')
    script = tmp_path / 'renderer-check.mjs'
    script.write_text(r"""
    import assert from 'node:assert/strict';
    import {readFile} from 'node:fs/promises';
    const failure = JSON.parse(await readFile(new URL('./failure.json', import.meta.url), 'utf8'));
    class Element {
      constructor() { this.children = []; this.fields = {}; this.value = ''; }
      replaceChildren() { this.children = []; }
      appendChild(el) { this.children.push(el); }
      prepend(el) { this.children.unshift(el); }
      querySelector(key) { return this.fields[key] ||= new Element(); }
      addEventListener(name, callback) { this[name] = callback; }
    }
    const elements = {};
    globalThis.document = {
      getElementById: id => elements[id] ||= new Element(),
      createElement: () => new Element(),
    };
    globalThis.window = {mmwf: {getBackendInfo: async () => ({port: 1234, token: 'fake'})}};
    let requests = 0, routed = 0;
    globalThis.fetch = async () => ({ok: true, json: async () => {
      requests++;
      return requests === 2 ? failure : {ok: true, data: {meeting_id: 'synthetic'}};
    }});
    const {shell} = await import('./renderer/app.js');
    const {renderSubmit} = await import('./renderer/pages/submit.js');
    const view = elements.view;
    shell.route = async () => { routed++; };
    await renderSubmit(view, shell);
    const card = view.children[0];
    card.querySelector('#m-title').value = '合成会议';
    card.querySelector('#m-record').value = '合成记录';
    const button = card.querySelector('#m-create');
    await button.click();
    assert.equal(requests, 2);
    assert.equal(routed, 0, 'failure must stop navigation');
    assert.equal(view.children[0].className, 'notice error');
    assert.equal(view.children[0].textContent, failure.message, 'Chinese recovery guidance');
    assert.ok(view.children.includes(card), 'form remains usable');
    await button.click();
    assert.equal(requests, 4);
    assert.equal(routed, 1, 'same button retries successfully');
    console.log('PASS');
    """, encoding='utf-8')
    completed = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == 'PASS', completed.stdout


if __name__ == '__main__':
    worker(Path(sys.argv[1]), sys.argv[2])
