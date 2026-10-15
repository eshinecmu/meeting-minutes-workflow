"""G6 batch 5: local security acceptance, with no external services.

裁决：03 的单会话按 01 §11.3 理解为新启动的会话；普通占用 E9003，
state_secret 非终态处理期间拒绝新启动用 §9.3 E6002，原会话继续可用。
HTTP 注入在传输边界记录连接元数据，不以真实外网/抓包代替人工验收。
"""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from threading import Event
import time
from types import ModuleType, SimpleNamespace

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from mmwf.api import system
from mmwf.api.deps import create_session_token
from mmwf.app.services.ingest import make_ingest_service
from mmwf.domain.asr_types import TranscriptResult, TranscriptSegmentResult
from mmwf.infra.db import connect_ctx
from mmwf.infra.keystore import load_or_create_key
from mmwf.main import create_app
from mmwf.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]
API = '/api/v1/meetings'
SENTINELS = {
    'title': '绝密预算标题哨兵', 'host': '主持人哨兵甲',
    'attendees': ['参会人哨兵乙'], 'absentees': ['列席人哨兵丙'],
    'recorder': '记录人哨兵丁', 'location': '会议地址哨兵戊',
}
TEXT = '决定本周完成绝密项目预算核对哨兵。'


@pytest.fixture(autouse=True)
def credential_backend(monkeypatch):
    """Exercise real keystore selection against an isolated credential-manager fake."""
    entries = {}
    module = ModuleType('keyring')
    module.get_password = lambda service, account: entries.get((service, account))
    module.set_password = lambda service, account, value: entries.__setitem__((service, account), value)
    monkeypatch.setitem(sys.modules, 'keyring', module)
    return entries


@pytest.fixture
def flow(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('business flow must not access a socket')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    cfg = load_settings(ROOT / 'config')
    cfg = cfg.model_copy(update={'app': cfg.app.model_copy(update={
        'data_dir': tmp_path / 'data', 'log_dir': tmp_path / 'logs'})})

    class Audio:
        def probe_duration_ms(self, path):
            return 4000

        def cut_chunk(self, src, dst, start_ms, end_ms):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b'fake local audio')

    class Asr:
        name = 'security-fake'
        calls = 0

        def available(self):
            return True

        def transcribe(self, path, *, hotwords):
            assert Path(path).is_file()
            self.calls += 1
            return TranscriptResult(segments=(
                TranscriptSegmentResult(1, TEXT, 0, 3000, 0.99),
            ), engine=self.name, model='fake', avg_confidence=0.99)

    asr = Asr()
    monkeypatch.setattr('mmwf.app.services.ingest.make_ingest_service',
                        lambda settings, **kw: make_ingest_service(settings, adapter=Audio(), **kw))
    monkeypatch.setattr('mmwf.adapters.asr.build_from_settings', lambda settings: asr)
    app = create_app(cfg)
    with TestClient(app) as client:
        client.headers['X-MMWF-Token'] = create_session_token()
        yield SimpleNamespace(client=client, rt=app.state.runtime, cfg=cfg, asr=asr, root=tmp_path)


def ok(response):
    assert response.status_code == 200
    body = response.json()
    assert body['ok'], body
    return body['data']


def complete(flow, observer=lambda stage, mid: None):
    audio = flow.root / 'input.wav'
    audio.write_bytes(b'audio fixture')
    mid = ok(flow.client.post(API, json={**SENTINELS, 'operator': '验收操作人',
                                       'audio_paths': [str(audio)]}))['meeting_id']
    observer('import', mid)
    result = ok(flow.client.post(f'{API}/{mid}/start'))
    assert result['pending_gate']['gate'] == 'G2'
    observer('transcribe', mid)
    for gate in ('G2', 'G3'):
        result = ok(flow.client.post(f'{API}/{mid}/gate/{gate}/decide', json={
            'checkpoint_id': result['pending_gate']['checkpoint_id'],
            'decision': 'approve', 'operator': '验收操作人'}))
        observer(gate, mid)
    with flow.rt.runner(mid) as runner:
        assert runner.status().status == 'archived'
    return mid


def test_sec04_http_injection_is_detected(monkeypatch):
    conns = []
    proc = SimpleNamespace(net_connections=lambda **kw: list(conns))
    # The HTTP call itself is mandatory; only its I/O boundary is substituted.
    def transport(request):
        assert str(request.url) == 'https://example.com/'
        conns.append(SimpleNamespace(laddr=SimpleNamespace(ip='127.0.0.1', port=51234),
                                     raddr=SimpleNamespace(ip='93.184.216.34', port=443), status='ESTABLISHED'))
        return httpx.Response(200, text='injected')
    monkeypatch.setattr(system, '_collect_machine_overview', lambda *a: None)
    before = system.probe_network(proc=proc)
    assert before.verdict == 'pass' and before.detail['outbound_connections'] == 0
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        assert client.get('https://example.com/').text == 'injected'
        after = system.probe_network(proc=proc)
    assert after.verdict == 'fail'
    assert after.detail['outbound_connections'] == 1
    assert after.detail['outbound_detail'][0]['ip'] == '93.184.216.34'


@contextmanager
def backend(tmp_path, *, listen=False):
    config = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', config)
    (config / 'app.yaml').write_text(
        f'app:\n  port: {0 if listen else 54321}\n  data_dir: {tmp_path / "data"}\n  log_dir: {tmp_path / "logs"}\n', encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower()}
    env.update(LOCALAPPDATA=str(tmp_path), MMWF_CONFIG_DIR=str(config),
               PYTHONPATH=str(ROOT / 'src'), HTTP_PROXY='http://user:password@203.0.113.8:8080')
    log = tmp_path / 'backend.log'
    ready = tmp_path / 'ready'
    # Lock tests need real processes and main(), but no listening socket. Block
    # only uvicorn's I/O boundary; app assembly, migration and session files run.
    launcher = '''
import sys
from pathlib import Path
import uvicorn
from mmwf.main import main
def serve(*args, **kwargs):
    Path(sys.argv[1]).write_text('ready', encoding='utf-8')
    sys.stdin.read()
uvicorn.run = serve
raise SystemExit(main())
'''
    command = ([sys.executable, '-m', 'mmwf.main'] if listen else
               [sys.executable, '-c', launcher, str(ready)])
    with log.open('w', encoding='utf-8') as output:
        proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert proc.poll() is None, log.read_text(encoding='utf-8')
                port_file = tmp_path / 'mmwf' / 'port'
                if port_file.exists():
                    port = int(port_file.read_text(encoding='utf-8'))
                    if not listen and ready.exists():
                        break
                    if not listen:
                        Event().wait(0.02)
                        continue
                    with socket.socket() as sock:
                        sock.settimeout(0.1)
                        if sock.connect_ex(('127.0.0.1', port)) == 0:
                            break
                Event().wait(0.02)
            else:
                pytest.fail('backend did not listen within 15 seconds')
            yield SimpleNamespace(proc=proc, env=env, log=log, port=port,
                                  db=tmp_path / 'data' / 'mmwf.db', command=command)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdin.close()


def test_sec05_real_startup_has_one_loopback_listener(tmp_path):
    # 先独立探测环境权限；只门控沙箱拒绝，不吞后端启动/监听断言失败。
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', 0))
        except PermissionError:
            pytest.skip('SEC-05 待验：环境禁止绑定回环端口，须在允许监听的目标环境重跑；不计通过')
    with backend(tmp_path, listen=True) as running:
        listeners = [c for c in psutil.Process(running.proc.pid).net_connections(kind='inet')
                     if c.status == psutil.CONN_LISTEN]
        assert [(c.laddr.ip, c.laddr.port) for c in listeners] == [('127.0.0.1', running.port)]


def test_sec06_actual_startup_warns_about_external_proxy(tmp_path):
    with backend(tmp_path) as running:
        logs = running.log.read_text(encoding='utf-8')
        assert 'WARNING' in logs and 'HTTP_PROXY' in logs and '203.0.113.8' in logs
        assert 'password' not in logs


@pytest.mark.parametrize('secret', [False, True], ids=['ACL01-IT15', 'ACL02'])
def test_second_instance_refused_without_rotating_active_token(tmp_path, secret):
    with backend(tmp_path) as running:
        if secret:
            with connect_ctx(running.db) as conn:
                conn.execute("INSERT INTO meeting (meeting_id,title,classification,status,created_by,created_at,updated_at) "
                             "VALUES ('secret','secret','state_secret','G2_awaiting','操作人','now','now')")
        token_file = tmp_path / 'mmwf' / 'session.token'
        token = token_file.read_bytes()
        second = subprocess.run(running.command, cwd=ROOT, input='',
                                env=running.env, capture_output=True, text=True, timeout=10)
        assert second.returncode != 0
        assert ('E6002' if secret else 'E9003') in second.stderr
        assert ('锁定' if secret else '使用中') in second.stderr
        if not secret:
            import getpass
            assert getpass.getuser() in second.stderr
        assert token_file.read_bytes() == token
        assert running.proc.poll() is None
    # Abnormal owner exit must release the OS lock; a recovered session can start.
    restarted = subprocess.run(running.command, cwd=ROOT, input='', env=running.env,
                               capture_output=True, text=True, timeout=10)
    assert restarted.returncode == 0, restarted.stderr
    assert not token_file.exists()
    assert not (tmp_path / 'mmwf' / 'port').exists()


def test_aud04_business_flow_records_import_transcribe_gates_export(flow):
    mid = complete(flow)
    assert flow.asr.calls == 1
    with connect_ctx(flow.rt.db_path) as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM audit_log WHERE target_id=? ORDER BY log_id', (mid,))]
    actions = [r['action'] for r in rows]
    assert {'import_audio', 'transcribe_done', 'gate_decision', 'export'} <= set(actions)
    decisions = [json.loads(r['detail']) for r in rows if r['action'] == 'gate_decision']
    assert {d['gate'] for d in decisions} == {'G2', 'G3'}
    assert all(r['result'] == 'success' and r['operator'] for r in rows)
    assert system.probe_audit_chain(flow.rt.db_path).verdict == 'pass'


def test_enc03_database_wal_and_checkpoints_hide_sensitive_fields(flow):
    words = [v for value in SENTINELS.values() for v in (value if isinstance(value, list) else [value])] + [TEXT]
    stages = []
    # Keep a reader open: WAL must exist and be scanned before checkpoint/truncation.
    with connect_ctx(flow.rt.db_path) as keeper:
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        def scan(stage, mid):
            stages.append(stage)
            wal = Path(str(flow.rt.db_path) + '-wal')
            assert wal.exists()
            if stage != 'checkpointed':
                assert wal.stat().st_size > 0
            for path in (flow.rt.db_path, wal):
                raw = path.read_bytes()
                for word in words:
                    for encoding in ('utf-8', 'utf-16le', 'utf-16be'):
                        assert word.encode(encoding) not in raw, (stage, path.name, word)
                    assert json.dumps(word, ensure_ascii=True)[1:-1].encode() not in raw
        mid = complete(flow, scan)
        assert stages == ['import', 'transcribe', 'G2', 'G3']
        assert keeper.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0] > 0
        assert keeper.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        scan('checkpointed', mid)
    raw = flow.rt.db_path.read_bytes()
    assert all(w.encode('utf-8') not in raw for w in words)
    # Reopen/decrypt persisted business state, not just absence of plaintext in empty tables.
    with flow.rt.runner(mid) as runner:
        assert TEXT in str(runner.values())
        assert runner.status().status == 'archived'
    with connect_ctx(flow.rt.db_path) as conn:
        row = conn.execute('SELECT * FROM meeting WHERE meeting_id=?', (mid,)).fetchone()
        assert row['host'] == SENTINELS['host']
        assert json.loads(row['attendees']) == SENTINELS['attendees']


def test_enc03_transcript_replace_and_edit_never_write_plaintext(flow):
    mid = complete(flow)
    store = flow.rt.transcript_store(mid)
    speaker = '发言人加密哨兵己'
    edited = '人工修订机密预算哨兵庚'
    with connect_ctx(flow.rt.db_path) as keeper:
        store.replace_segments([{'seq': 1, 'start_ms': 0, 'end_ms': 3000,
                                 'text': TEXT, 'speaker': speaker, 'confidence': 0.99}])
        segment_id = f'{mid}:00001'
        result = ok(flow.client.patch(f'/api/v1/segments/{segment_id}', json={
            'text': edited, 'operator': '验收操作人'}))
        assert edited in str(result)
        for path in (flow.rt.db_path, Path(str(flow.rt.db_path) + '-wal')):
            raw = path.read_bytes()
            assert all(word.encode('utf-8') not in raw for word in (TEXT, edited, speaker))
        assert keeper.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    with connect_ctx(flow.rt.db_path) as conn:
        row = conn.execute('SELECT text,speaker,version FROM transcript_segment WHERE segment_id=?',
                           (segment_id,)).fetchone()
        assert tuple(row) == (edited, speaker, 2)


def test_enc04_key_only_in_credential_backend_not_configuration_or_code(flow, credential_backend):
    mid = complete(flow)
    source = load_or_create_key(flow.cfg.app.data_dir, allow_file_fallback=False)
    assert source.secure and source.backend == 'keyring'
    assert credential_backend[('mmwf', 'master-key')] == source.key.hex()
    assert load_or_create_key(flow.cfg.app.data_dir, allow_file_fallback=False).key == source.key
    assert not list(flow.root.rglob('.master.key'))
    assert not list(flow.root.rglob('KEY_STORAGE_UNHARDENED.txt'))
    needles = (source.key, source.key.hex().encode(), base64.b64encode(source.key))
    paths = [p for root in (ROOT / 'src', ROOT / 'config', flow.root) for p in root.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts]
    assert paths
    for path in paths:
        assert all(needle not in path.read_bytes() for needle in needles), path
    with flow.rt.runner(mid) as runner:
        assert runner.status().status == 'archived'
