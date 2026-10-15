"""C6 验收反例锁；复用真实领域函数制造规则破坏。"""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance():
    path = Path(__file__).resolve().parents[2] / 'tools/accept_t2_03.py'
    spec = importlib.util.spec_from_file_location('accept_t2_03_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_broken_confirmed_rule_fails_standard(acceptance, monkeypatch, capsys):
    original = acceptance.display_topics

    def broken(*args):
        result = original(*args)
        # 模拟把字符串 true 误当服务器已确认；过滤会错误隐藏待处理项。
        result['topics'][-1]['confirmed'] = True
        return result

    monkeypatch.setattr(acceptance, 'display_topics', broken)
    # 此处只隔离其余定向证据；标准 4 真值表仍执行真实投影的错误结果。
    monkeypatch.setattr(acceptance, '_run_evidence', lambda *args: True)
    monkeypatch.setattr('sys.argv', ['accept_t2_03.py', '--only', '4'])
    assert acceptance.main() == 1
    output = capsys.readouterr().out
    assert 'FAIL 标准 4' in output and '共 1 项，通过 0 项' in output
    assert 'V5 未闭环' in output


def test_exception_fails_and_remaining_standards_continue(acceptance, monkeypatch, capsys):
    def fail(tmp):
        raise RuntimeError('验收故障')

    monkeypatch.setattr(acceptance, 'check_standard_1', fail)
    monkeypatch.setattr(acceptance, '_run_evidence', lambda *args: True)
    monkeypatch.setattr('sys.argv', ['accept_t2_03.py'])
    assert acceptance.main() == 1
    output = capsys.readouterr().out
    assert 'FAIL 标准 1' in output and 'PASS 标准 6' in output
    assert '共 6 项，通过 5 项' in output


@pytest.mark.parametrize('body,expected', [
    ('assert True', True),
    ('assert False, "模拟验收规则被破坏"', False),
    ('import pytest; pytest.skip("不能把跳过算通过")', False),
])
def test_evidence_runner_requires_executed_passes(acceptance, tmp_path, monkeypatch, body, expected):
    source = tmp_path / 'test_evidence.py'
    source.write_text('def test_rule():\n    ' + body + '\n', encoding='utf-8')
    monkeypatch.setitem(acceptance.CASES, 1, (str(source),))
    assert acceptance._run_evidence(1, tmp_path) is expected
