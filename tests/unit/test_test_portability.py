"""W2：在 macOS 上模拟 Windows 启动、socketpair 与权限/退出码语义。"""
import asyncio
import importlib.util
import io
from pathlib import Path, PureWindowsPath
import socket
from types import SimpleNamespace

import pytest

from _netguard import install_network_guard, require_loopback
import portable_support

ROOT = Path(__file__).resolve().parents[2]


def load_test(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tests/integration' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('host', ['127.0.0.1', '127.12.34.56', '::1'])
def test_guard_accepts_only_numeric_loopback(host):
    require_loopback((host, 1234))


@pytest.mark.parametrize('host', ['203.0.113.1', '192.168.1.1', '0.0.0.0', '::', '2001:db8::1', 'localhost'])
@pytest.mark.parametrize('operation', ['connect', 'connect_ex', 'create_connection'])
def test_external_connect_fails_before_dns_or_network(monkeypatch, host, operation):
    install_network_guard(monkeypatch.setattr)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('守卫不应解析 DNS'))
    with socket.socket() as sock, pytest.raises(AssertionError, match='非回环'):
        if operation == 'create_connection':
            socket.create_connection((host, 1234))
        else:
            getattr(sock, operation)((host, 1234))


def test_windows_tcp_socketpair_event_loop_works(monkeypatch):
    """模拟 Windows 无原生 socketpair 时以回环 TCP 连接唤醒事件循环。"""
    pairs = []
    connections = []
    native_socketpair = socket.socketpair
    # 沙箱禁止 bind：用真实本地 socketpair 承载数据，模拟 TCP fallback 的 connect 接缝。
    original_connect = socket.socket.connect

    def connect(self, address):
        if address == ('127.0.0.1', 1234):
            connections.append(address)
            return None
        return original_connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', connect)
    install_network_guard(monkeypatch.setattr)

    def tcp_socketpair(*args, **kwargs):
        with socket.socket() as connector:
            connector.connect(('127.0.0.1', 1234))
        pair = native_socketpair()
        pairs.append(pair)
        return pair

    monkeypatch.setattr(socket, 'socketpair', tcp_socketpair)
    loop = asyncio.new_event_loop()
    try:
        assert pairs and connections == [('127.0.0.1', 1234)]
        future = loop.create_future()
        loop.call_soon_threadsafe(future.set_result, '已唤醒')
        assert loop.run_until_complete(future) == '已唤醒'
    finally:
        loop.close()
    assert all(sock.fileno() == -1 for pair in pairs for sock in pair)


def test_loopback_connect_ex_and_ipv6_delegate(monkeypatch):
    calls = []
    monkeypatch.setattr(socket.socket, 'connect_ex', lambda self, addr: calls.append(addr) or 0)
    install_network_guard(monkeypatch.setattr)
    with socket.socket() as sock:
        assert sock.connect_ex(('::1', 1234, 0, 0)) == 0
    assert calls == [('::1', 1234, 0, 0)]


@pytest.mark.parametrize('name', ['test_accept_f5_9_evidence', 'test_accept_t2_05_evidence', 'test_shadow_cli'])
def test_cli_uses_current_windows_interpreter(monkeypatch, tmp_path, name):
    # 路径包含空格，必须作为一个 argv 元素原样传入，不能拼接 POSIX bin/python。
    executable = str(PureWindowsPath('C:/Program Files/mmwf/.venv/Scripts/python.exe'))
    monkeypatch.setattr('sys.executable', executable)
    module = load_test(name)
    commands = []

    class Captured(Exception):
        pass

    def run(command, **kwargs):
        commands.append(command)
        raise Captured

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(Captured):
        if name == 'test_accept_f5_9_evidence':
            module.run_accept('--only', '7')
        elif name == 'test_accept_t2_05_evidence':
            module.test_full_subprocess_stdout_tooling_only(tmp_path)
        else:
            module.test_subprocess_cli_synthetic_roundtrip_and_projection(tmp_path)
    assert commands[0][0] == executable


def test_killed_worker_windows_positive_exit(monkeypatch, tmp_path):
    module = load_test('test_stability_faults')
    calls = []

    class Process:
        stdout = io.StringIO('READY:transcribe\n')
        returncode = None

        def kill(self):
            calls.append('kill')
            self.returncode = 1  # Windows Popen.kill 的 TerminateProcess 退出码。

        def wait(self, timeout):
            assert timeout == 10
            return self.returncode

        def poll(self):
            return self.returncode

        def communicate(self, timeout):
            calls.append('communicate')
            return '', ''

    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: Process())
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32', executable='python.exe'))
    monkeypatch.setattr(module, 'signal', SimpleNamespace())  # Windows 无 SIGKILL。
    with module.killed_worker(tmp_path, 'transcribe'):
        assert calls == ['kill']
    assert calls == ['kill', 'communicate']


@pytest.mark.parametrize('platform,winerror,skipped', [('win32', 1314, True), ('win32', 5, False), ('darwin', 1314, False)])
def test_symlink_skip_only_windows_privilege_error(monkeypatch, platform, winerror, skipped):
    error = OSError('模拟创建失败')
    error.winerror = winerror

    def symlink_to(*args, **kwargs):
        raise error

    monkeypatch.setattr(portable_support, 'sys', SimpleNamespace(platform=platform))
    link = SimpleNamespace(symlink_to=symlink_to)
    expected = pytest.skip.Exception if skipped else OSError
    with pytest.raises(expected, match='WinError 1314' if skipped else '模拟创建失败'):
        portable_support.directory_symlink(link, Path('source'))


def test_windows_symlink_success_keeps_real_assertion(monkeypatch, tmp_path):
    monkeypatch.setattr(portable_support, 'sys', SimpleNamespace(platform='win32'))
    target = tmp_path / 'source'
    target.mkdir()
    link = tmp_path / 'link'
    portable_support.directory_symlink(link, target)
    assert link.is_symlink() and link.resolve() == target


def test_pdf_stub_launches_python_without_posix_permissions(monkeypatch, tmp_path):
    module = load_test('test_pdf_soffice_stub')
    executable = str(PureWindowsPath('C:/Program Files/mmwf/.venv/Scripts/python.exe'))
    monkeypatch.setattr('sys.executable', executable)
    monkeypatch.setattr(Path, 'chmod', lambda *a, **k: pytest.fail('不能依赖 chmod'))
    commands = []

    class Captured(Exception):
        pass

    def capture(command, *args, **kwargs):
        commands.append(command)
        raise Captured

    monkeypatch.setattr(module.subprocess, 'run', capture)
    with pytest.raises(Captured):
        module.test_gated_body_with_configured_executable(tmp_path, '')
    spec = importlib.util.spec_from_file_location('f59_stub_config', tmp_path / 'f59_stub_config.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    plugin.pytest_configure(None)
    script = str(tmp_path / 'Program Files' / 'LibreOffice' / 'program' / 'soffice.exe')
    with pytest.raises(Captured):
        module.subprocess.run([script, '--version'], check=True, timeout=15)
    assert commands[-1] == [executable, script, '--version']
