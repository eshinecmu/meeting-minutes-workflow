"""验收脚本反例锁：规则失效必须报 FAIL 并非零退出。"""
import importlib.util
from pathlib import Path
from _netguard import install_network_guard

import pytest


@pytest.fixture
def acceptance():
    path = Path(__file__).resolve().parents[2] / "tools" / "accept_t2_02.py"
    spec = importlib.util.spec_from_file_location("accept_t2_02", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_required_rule_breaks_acceptance(acceptance, monkeypatch, capsys):
    """把检查器变成空操作，标准 2 必须真的失败。"""
    monkeypatch.setattr(acceptance, "check_required_elements", lambda *args: [])
    monkeypatch.setattr("sys.argv", ["accept_t2_02.py", "--only", "2"])
    assert acceptance.main() == 1
    output = capsys.readouterr().out
    assert "FAIL 标准 2" in output
    assert "共 1 项，通过 0 项" in output


def test_complete_acceptance_is_offline_and_does_not_mutate_repo(acceptance, monkeypatch, capsys):
    """正向覆盖八项；禁用实际网络连接并比对仓库模板字节。"""
    templates = acceptance.PROJECT_ROOT / "config" / "templates"
    before = {p.name: p.read_bytes() for p in templates.glob("*.yaml")}

    install_network_guard(monkeypatch.setattr)
    monkeypatch.setattr("sys.argv", ["accept_t2_02.py"])
    assert acceptance.main() == 0
    assert "共 8 项，通过 8 项" in capsys.readouterr().out
    assert before == {p.name: p.read_bytes() for p in templates.glob("*.yaml")}


def test_exception_is_a_failure_and_other_standards_continue(acceptance, monkeypatch, capsys):
    def broken(tmp):
        raise RuntimeError("验收故障")

    monkeypatch.setattr(acceptance, "check_standard_1", broken)
    monkeypatch.setattr("sys.argv", ["accept_t2_02.py"])
    assert acceptance.main() == 1
    output = capsys.readouterr().out
    assert "FAIL 标准 1" in output
    assert "PASS 标准 8" in output
    assert "共 8 项，通过 7 项" in output
