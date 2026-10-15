"""E5：SYNTHETIC 故障注入，失败不得被工具验收吞掉。"""
from pathlib import Path
import subprocess

import pytest
import accept_t2_05 as acceptance


@pytest.mark.parametrize('source,expected', [
    ('def test_rule():\n    assert True\n', True),
    ('def test_rule():\n    assert False\n', False),
    ('def test_rule():\n    raise RuntimeError("故障")\n', False),
    ('def test_rule():\n    import pytest; pytest.skip("跳过不通过")\n', False),
    ('# 零执行\n', False),
    ('def test_rule(:\n', False),
    ('def test_rule():\n    import socket; socket.create_connection(("127.0.0.1", 9))\n', False),
])
def test_evidence_counts_require_execution(tmp_path, monkeypatch, capsys, source, expected):
    path = tmp_path / 'test_evidence.py'
    path.write_text(source, encoding='utf-8')
    monkeypatch.setitem(acceptance.CASES, 1, (str(path),))
    assert acceptance.main(['--only', '1']) == (0 if expected else 1)
    output = capsys.readouterr().out
    assert ('PASS 标准 1' if expected else 'FAIL 标准 1') in output
    assert acceptance.PENDING in output
    assert '共 1 项' in output and 'RELEASE_PASS' not in output
    assert all(k + '=' in output for k in ('executed', 'passed', 'failed', 'error', 'skipped'))


@pytest.mark.parametrize('report', [None, '<broken', '<testsuites/>'])
def test_missing_invalid_empty_report_fails(tmp_path, monkeypatch, report):
    def fake_run(*args, **kwargs):
        if report is not None:
            (tmp_path / 'report.xml').write_text(report)
        return subprocess.CompletedProcess(args, 0, stdout='SYNTHETIC 空证据\n')
    monkeypatch.setattr(acceptance.subprocess, 'run', fake_run)
    assert not acceptance._run_evidence(1, tmp_path)


# 注入真实调用结果的破坏，然后重跑对应固定证据；不是把验收返回值改成 False。
MUTATIONS = {
    'release_and': (5, '''
from mmwf.domain import shadow
from mmwf.domain.shadow.models import ShadowSummary, freeze, thaw
original = shadow.aggregate_metrics
def broken(*args, **kwargs):
    data = thaw(original(*args, **kwargs).data)
    data['quality_pass'] = all(m['passed'] for m in data['metrics'].values())
    return ShadowSummary(freeze(data))
shadow.aggregate_metrics = broken
'''),
    'synthetic_block': (5, '''
from mmwf.domain import shadow
from mmwf.domain.shadow.models import ShadowSummary, freeze, thaw
original = shadow.aggregate_metrics
def broken(*args, **kwargs):
    data = thaw(original(*args, **kwargs).data)
    if data['dataset_kind'] == 'synthetic' and data['quality_pass']:
        data.update(eligible_for_stage2=True, real_meetings_counted=len(data['meetings']), real_run_status='REVIEWED')
    return ShadowSummary(freeze(data))
shadow.aggregate_metrics = broken
'''),
    'delivery_guard': (1, '''
import shadow_run
from mmwf.app.services.minutes import MinutesService
original = shadow_run.run_batch
def broken(*args, **kwargs):
    result = original(*args, **kwargs)
    # 模拟工具绕过领域链，偷调用业务写入接缝；现有 spy 必须拦住。
    MinutesService.save_draft(None, None)
    return result
shadow_run.run_batch = broken
'''),
}


@pytest.mark.parametrize('mutation', MUTATIONS)
def test_mutation_makes_matching_standard_fail(tmp_path, monkeypatch, capsys, mutation):
    number, source = MUTATIONS[mutation]
    (tmp_path / 't205_mutation.py').write_text(source, encoding='utf-8')
    real_run = subprocess.run

    def injected(command, **kwargs):
        command = [*command[:3], '-p', 't205_mutation', *command[3:]]
        kwargs['env']['PYTHONPATH'] += ':' + str(tmp_path)
        return real_run(command, **kwargs)

    monkeypatch.setattr(acceptance.subprocess, 'run', injected)
    assert acceptance.main(['--only', str(number)]) == 1
    output = capsys.readouterr().out
    assert f'FAIL 标准 {number}' in output and 'TOOLING_FAIL' in output
    assert 'AssertionError' in output and 'failed' in output


def test_exception_continues_and_always_pending(monkeypatch, capsys):
    def run(number, tmp):
        if number == 1:
            raise RuntimeError('SYNTHETIC 故障')
        return True
    monkeypatch.setattr(acceptance, '_run_evidence', run)
    assert acceptance.main([]) == 1
    output = capsys.readouterr().out
    assert 'FAIL 标准 1' in output and 'PASS 标准 8' in output
    assert '共 8 项，通过 7 项' in output and acceptance.PENDING in output


def test_invalid_selection_still_pending(capsys):
    with pytest.raises(SystemExit) as error:
        acceptance.main(['--only', '9'])
    assert error.value.code == 2
    assert acceptance.PENDING in capsys.readouterr().out
