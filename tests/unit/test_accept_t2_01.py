"""T2-01 验收脚本的漏召回回归锁。"""

import errno
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("error,blocked", [
    (PermissionError("sandbox blocked"), True),
    (OSError(errno.EPERM, "Operation not permitted"), True),
    (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), False),
])
def test_probe_local_llm_distinguishes_sandbox_block(monkeypatch, error, blocked):
    path = Path(__file__).resolve().parents[2] / "tools" / "accept_t2_01.py"
    spec = importlib.util.spec_from_file_location("accept_t2_01", path)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)

    def fail_connection(*args, **kwargs):
        raise error

    monkeypatch.setattr(acceptance.shutil, "which", lambda name: None)
    monkeypatch.setattr(acceptance.socket, "create_connection", fail_connection)
    result = acceptance.probe_local_llm()
    if blocked:
        assert "探测被沙箱拦截（不能作为无模型证据）" in result
        assert "unreachable" not in result
    else:
        assert "unreachable（ConnectionRefusedError:" in result
        assert "探测被沙箱拦截" not in result


def test_standard_10_fails_when_fake_drops_one_resolution(monkeypatch, tmp_path, capsys):
    """全知假客户端少返回一项时，验收必须失败，且测试不建立网络连接。"""
    path = Path(__file__).resolve().parents[2] / "tools" / "accept_t2_01.py"
    spec = importlib.util.spec_from_file_location("accept_t2_01", path)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)

    from mmwf.adapters.llm import fake_client

    original = fake_client.build_oracle_minutes

    def drop_one(prompt, items):
        return original(prompt, items[1:])

    monkeypatch.setattr(fake_client, "build_oracle_minutes", drop_one)
    monkeypatch.setattr(acceptance, "probe_local_llm", lambda: "测试替身：未探测端口")
    assert acceptance.check_standard_10(tmp_path) is False
    output = capsys.readouterr().out
    assert "24/25" in output
    assert "96%（fake）" in output
    assert "漏召回：R01" in output
    assert "真模型召回率：UNSUPPORTED" in output
