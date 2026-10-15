"""F5.9：真实验收进程与完整计数；本文件不进入脚本自身标准 8。"""
import os
from pathlib import Path
import re
import subprocess
import sys

import accept_f5_9 as acceptance

ROOT = Path(__file__).resolve().parents[2]
COUNTS = r'计数：executed=(\d+) passed=(\d+) failed=(\d+) error=(\d+) skipped=(\d+)'


def run_accept(*args):
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    return subprocess.run([sys.executable, 'tools/accept_f5_9.py', *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


def test_full_stdout_and_required_evidence(tmp_path):
    result = run_accept()
    output = result.stdout + result.stderr
    (tmp_path / 'accept_f5_9.stdout.txt').write_text(output, encoding='utf-8')
    counts = re.findall(COUNTS, output)
    assert len(counts) == 8, output
    assert output.count('命令：') == 8
    for number in (1, 2, 3, 4, 5, 6, 8):
        assert f'PASS 标准 {number}' in output, output
        executed, passed, failed, errors, skipped = map(int, counts[number - 1])
        assert executed == passed > 0 and (failed, errors, skipped) == (0, 0, 0)
    real_pending = 'PENDING 标准 7' in output
    if real_pending:
        assert tuple(map(int, counts[6])) == (0, 0, 0, 0, 1)
    else:
        assert 'PASS 标准 7' in output, output
        assert int(counts[6][0]) == int(counts[6][1]) > 0
    assert result.returncode == 0, output
    assert 'TOOLING_PASS F5.9（7/7）' in output
    assert 'RELEASE_PASS' not in output and '8/8' not in output
    assert 'PENDING' in output and '目标机' in output


def test_real_only_skip_exit_is_nonzero():
    result = run_accept('--only', '7')
    output = result.stdout + result.stderr
    assert len(re.findall(COUNTS, output)) == 1, output
    if 'PENDING 标准 7' in output:
        assert result.returncode == 1 and acceptance.REAL_SKIP in output
        assert 'SELECTED_FAIL' in output and 'passed=0' in output
    else:
        assert result.returncode == 0 and 'PASS 标准 7' in output, output
    assert 'TOOLING_PASS' not in output and 'RELEASE_PASS' not in output
