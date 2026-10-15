"""D6：验收失败、异常继续、空收集与跳过反例。"""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance():
    path = Path(__file__).resolve().parents[2] / 'tools/accept_t2_04.py'
    spec = importlib.util.spec_from_file_location('accept_t2_04_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_broken_hard_route_fails_script(acceptance, monkeypatch, capsys):
    from mmwf.graph import build
    monkeypatch.setattr(build, 'route_after_validate', lambda state: 'give_up')
    monkeypatch.setattr(acceptance, '_run_evidence', lambda *args: True)
    monkeypatch.setattr('sys.argv', ['accept_t2_04.py', '--only', '1'])
    assert acceptance.main() == 1
    output = capsys.readouterr().out
    assert 'FAIL 标准 1' in output and '共 1 项，通过 0 项' in output


def test_exception_continues_remaining_standards(acceptance, monkeypatch, capsys):
    def broken(tmp):
        raise RuntimeError('验收故障')
    monkeypatch.setattr(acceptance, 'check_standard_1', broken)
    monkeypatch.setattr(acceptance, '_run_evidence', lambda *args: True)
    monkeypatch.setattr('sys.argv', ['accept_t2_04.py'])
    assert acceptance.main() == 1
    output = capsys.readouterr().out
    assert 'FAIL 标准 1' in output and 'PASS 标准 8' in output
    assert '共 8 项，通过 7 项' in output
    assert 'V3/V4/R-4/R-5 未全闭环' in output


@pytest.mark.parametrize('source,expected', [
    ('def test_rule():\n    assert True\n', True),
    ('def test_rule():\n    assert False\n', False),
    ('def test_rule():\n    import pytest; pytest.skip("跳过不算通过")\n', False),
    ('# 零执行\n', False),
    ('def test_rule(:\n', False),
    ('def test_rule():\n    import socket\n    socket.create_connection(("127.0.0.1", 9))\n', False),
])
def test_evidence_requires_executed_passes(acceptance, tmp_path, monkeypatch, source, expected):
    path = tmp_path / 'test_evidence.py'
    path.write_text(source, encoding='utf-8')
    monkeypatch.setitem(acceptance.CASES, 1, (str(path),))
    assert acceptance._run_evidence(1, tmp_path) is expected
