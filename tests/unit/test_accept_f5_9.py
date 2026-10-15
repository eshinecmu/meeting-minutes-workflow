"""F5.9：真实子进程故障与硬检/缓存破坏反例，禁止假通过。"""
from pathlib import Path
import subprocess

import pytest
import accept_f5_9 as acceptance


@pytest.mark.parametrize('source,ok', [
    ('def test_rule():\n    assert True\n', True),
    ('def test_rule():\n    assert False\n', False),
    ('def test_rule():\n    raise RuntimeError("故障")\n', False),
    ('def test_rule():\n    import pytest; pytest.skip("必需项不能跳过")\n', False),
    ('# 零收集\n', False),
    ('def test_rule(:\n', False),
    ('def test_rule():\n    import socket; socket.create_connection(("127.0.0.1", 9))\n', False),
])
def test_real_counts(tmp_path, monkeypatch, capsys, source, ok):
    path = tmp_path / 'test_evidence.py'
    path.write_text(source, encoding='utf-8')
    monkeypatch.setitem(acceptance.CASES, 1, (str(path),))
    assert acceptance.main(['--only', '1']) == (0 if ok else 1)
    out = capsys.readouterr().out
    assert f"{'PASS' if ok else 'FAIL'} 标准 1" in out
    assert 'SELECTED_' in out and 'TOOLING_PASS' not in out
    assert acceptance.PENDING in out
    assert all(k + '=' in out for k in ('executed', 'passed', 'failed', 'error', 'skipped'))


@pytest.mark.parametrize('report', [None, '<broken', '<testsuites/>',
    '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>',
    '<testsuite tests="-1" failures="0" errors="0" skipped="0"/>',
    '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase><failure/></testcase></testsuite>',
    '<testsuite tests="1" failures="1" errors="0" skipped="1"><testcase><failure/><skipped/></testcase></testsuite>',
])
def test_untrusted_reports_fail(tmp_path, monkeypatch, report):
    def fake_run(*args, **kwargs):
        if report is not None:
            (tmp_path / 'report.xml').write_text(report)
        return subprocess.CompletedProcess(args, 0, stdout='空证据\n')
    monkeypatch.setattr(acceptance.subprocess, 'run', fake_run)
    assert acceptance._run_evidence(1, tmp_path) == 'FAIL'


@pytest.mark.parametrize('number,reason,status', [
    (7, acceptance.REAL_SKIP, 'PENDING'), (7, '其他理由', 'FAIL'),
    (1, acceptance.REAL_SKIP, 'FAIL'),
])
def test_only_fixed_real_skip_is_pending(tmp_path, monkeypatch, number, reason, status):
    path = tmp_path / 'test_gate.py'
    path.write_text(f'def test_gate():\n    import pytest; pytest.skip({reason!r})\n', encoding='utf-8')
    monkeypatch.setitem(acceptance.CASES, number, (str(path),))
    assert acceptance._run_evidence(number, tmp_path) == status


@pytest.mark.parametrize('statuses,only,code,label', [
    ({7: 'PENDING'}, None, 0, 'TOOLING_PASS F5.9（7/7）'),
    ({7: 'PENDING'}, 7, 1, 'SELECTED_FAIL'),
    ({7: 'FAIL'}, None, 1, 'TOOLING_FAIL'),
    ({6: 'FAIL', 7: 'PENDING'}, None, 1, 'TOOLING_FAIL'),
    ({1: 'PENDING', 7: 'PENDING'}, None, 1, 'TOOLING_FAIL'),
    ({7: 'PASS'}, None, 0, '人工版式/字体与桌面操作仍 PENDING'),
])
def test_summary_boundaries(monkeypatch, capsys, statuses, only, code, label):
    monkeypatch.setattr(acceptance, '_run_evidence', lambda n, tmp: statuses.get(n, 'PASS'))
    assert acceptance.main([] if only is None else ['--only', str(only)]) == code
    out = capsys.readouterr().out
    assert label in out and 'RELEASE_PASS' not in out and '8/8' not in out
    if only is None and statuses == {7: 'PENDING'}:
        assert '7 PASS + 1 PENDING + 0 FAIL' in out


@pytest.mark.parametrize('mutation,number,node', [
    ('hard_check', 2, 'test_current_bad_working_copy_blocks_history_cache_download_and_archive'),
    ('cache', 4, 'test_damaged_cache_is_never_reused[pdf]'),
])
def test_mutation_fails_fixed_evidence(tmp_path, monkeypatch, capsys, mutation, number, node):
    # 在真实业务接缝破坏规则，然后运行固定反例；不伪造验收返回值。
    sources = {
        'hard_check': '''
from mmwf.app.services.minutes import MinutesService
def broken(self, meeting_id, state, **kwargs):
    return self.store().get(meeting_id, kwargs.get('version'))
MinutesService.deliver = broken
''',
        'cache': '''
from mmwf.app.services.export import ExportService
ExportService._valid_pdf_entry = lambda *a, **kw: True
''',
    }
    (tmp_path / 'f59_mutation.py').write_text(sources[mutation], encoding='utf-8')
    real_run = subprocess.run

    def injected(command, **kwargs):
        command = [*command[:3], '-p', 'f59_mutation', *command[3:]]
        kwargs['env']['PYTHONPATH'] += ':' + str(tmp_path)
        return real_run(command, **kwargs)

    monkeypatch.setattr(acceptance.subprocess, 'run', injected)
    monkeypatch.setitem(acceptance.CASES, number, (acceptance.PIPELINE + '::' + node,))
    assert acceptance.main(['--only', str(number)]) == 1
    out = capsys.readouterr().out
    assert f'FAIL 标准 {number}' in out and 'AssertionError' in out


def test_execution_exception_continues(monkeypatch, capsys):
    def run(number, tmp):
        if number == 1:
            raise OSError('进程不可用')
        return 'PENDING' if number == 7 else 'PASS'
    monkeypatch.setattr(acceptance, '_run_evidence', run)
    assert acceptance.main([]) == 1
    out = capsys.readouterr().out
    assert 'FAIL 标准 1' in out and 'PASS 标准 8' in out


def test_invalid_only(capsys):
    with pytest.raises(SystemExit) as caught:
        acceptance.main(['--only', '9'])
    assert caught.value.code == 2 and acceptance.PENDING in capsys.readouterr().out


@pytest.mark.parametrize('returncode,body,expected', [
    (0, '<testcase/><testcase><skipped message="必需项"/></testcase>', (1, 1, 0, 0, 1)),
    (1, '<testcase/>', (1, 1, 0, 0, 0)),
    (1, '<testcase><error message="setup 失败"/></testcase>', (1, 0, 0, 1, 0)),
])
def test_exact_counts_and_process_failure(tmp_path, monkeypatch, capsys, returncode, body, expected):
    executed, passed, failed, errors, skipped = expected
    report = (f'<testsuite tests="{executed + skipped}" failures="{failed}" '
              f'errors="{errors}" skipped="{skipped}">{body}</testsuite>')

    def fake_run(*args, **kwargs):
        (tmp_path / 'report.xml').write_text(report, encoding='utf-8')
        return subprocess.CompletedProcess(args, returncode, stdout='完整 stdout 哨兵\n')

    monkeypatch.setattr(acceptance.subprocess, 'run', fake_run)
    assert acceptance._run_evidence(1, tmp_path) == 'FAIL'
    out = capsys.readouterr().out
    assert '完整 stdout 哨兵' in out
    assert (f'executed={executed} passed={passed} failed={failed} '
            f'error={errors} skipped={skipped}') in out


def test_plugin_import_failure_is_not_pass(tmp_path, monkeypatch, capsys):
    # 同名模块优先加载并实际导入失败；自动加载关闭时也不能缺插件继续验收。
    (tmp_path / 'accept_f5_9.py').write_text('raise ImportError("F59_PLUGIN_BROKEN")\n')
    real_run = subprocess.run

    def broken_plugin(command, **kwargs):
        assert kwargs['env']['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
        assert ['-p', 'accept_f5_9'] == command[command.index('accept_f5_9') - 1:command.index('accept_f5_9') + 1]
        kwargs['env']['PYTHONPATH'] = str(tmp_path)
        return real_run(command, **kwargs)

    monkeypatch.setattr(acceptance.subprocess, 'run', broken_plugin)
    assert acceptance.main(['--only', '1']) == 1
    out = capsys.readouterr().out
    assert 'F59_PLUGIN_BROKEN' in out and 'FAIL 标准 1' in out
    assert 'executed=0 passed=0 failed=0 error=1 skipped=0' in out
