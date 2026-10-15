"""E5：完整 stdout 来自真正验收子进程；全部资料是 SYNTHETIC。"""
import os
from pathlib import Path
import re
import subprocess

import accept_t2_05 as acceptance

ROOT = Path(__file__).resolve().parents[2]


def test_full_subprocess_stdout_tooling_only(tmp_path):
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    result = subprocess.run([str(ROOT / '.venv/bin/python'), 'tools/accept_t2_05.py'],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    # 失败时也完整保留证据，便于外部检查循环复核。
    (tmp_path / 'accept_t2_05.stdout.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'TOOLING_PASS T2-05（8/8）' in result.stdout
    assert 'RELEASE_PASS' not in result.stdout
    assert acceptance.PENDING in result.stdout
    counts = re.findall(r'计数：executed=(\d+) passed=(\d+) failed=(\d+) error=(\d+) skipped=(\d+)', result.stdout)
    assert len(counts) == 8
    assert all(int(n) > 0 and n == p and (f, e, s) == ('0', '0', '0') for n, p, f, e, s in counts)
    for number in range(1, 9):
        assert f'PASS 标准 {number}' in result.stdout
    assert result.stdout.count('命令：') == 8
    assert 'SYNTHETIC / 合成数据，非真实验收结果' in result.stdout


def test_twenty_synthetic_still_zero_real():
    # 复用明确标注的纯条件计算向量，无真实会议或模型。
    import sys
    sys.path.insert(0, str(ROOT / 'tests/unit'))
    from test_shadow_scoring import vector
    from mmwf.domain.shadow import aggregate_metrics
    data = aggregate_metrics(*vector(20)).data
    assert data['quality_pass'] and len(data['meetings']) == 20
    assert not data['eligible_for_stage2']
    assert data['real_meetings_counted'] == 0 and data['real_run_status'] == 'PENDING'
