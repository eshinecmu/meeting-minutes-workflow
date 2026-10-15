"""T2-01 验收脚本的漏召回回归锁。"""

import importlib.util
from pathlib import Path


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
