"""F5.9：固定定向证据与真实转换分账；不替代目标机人工签字。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PENDING = '真实转换/人工版式 PENDING，目标机通过 0；Electron 实际操作待验，人工签字不得自动生成'
NAMES = ('同源转换与范围', '全出口硬阻断', '失败语义', '幂等与恢复',
         '留档与撤销', 'API 与桌面联证', '真实转换（目标机项）', '可信验收脚本')
UNIT = 'tests/unit/test_pdf_export_service.py'
CONTRACT = 'tests/contract/test_pdf_export_contract.py'
PIPELINE = 'tests/integration/test_pdf_export_pipeline.py'
CASES = {
    1: (UNIT + '::test_exact_command_source_and_cleanup',
        UNIT + '::test_direct_entry_has_no_arbitrary_path_bypass', CONTRACT,
        PIPELINE + '::test_selected_version_missing_draft_and_new_final'),
    2: (PIPELINE + '::test_current_bad_working_copy_blocks_history_cache_download_and_archive',
        PIPELINE + '::test_download_final_and_source_checks',
        PIPELINE + '::test_font_fail_blocks_even_with_pdf_cache', CONTRACT),
    3: (UNIT, PIPELINE + '::test_explicit_conversion_failures_never_return_old_link',
        PIPELINE + '::test_registration_failure_and_retry',
        PIPELINE + '::test_missing_soffice_explicit_error_and_old_doc_soft_failure',
        'tests/contract/test_exporter_contract.py',
        'tests/unit/test_export_service.py::TestDocConversion::test_skipped_without_soffice',
        'tests/unit/test_export_service.py::TestDocConversion::test_skipped_does_not_fail_export',
        'tests/unit/test_export_service.py::TestDocConversion::test_not_attempted_by_default',
        'tests/unit/test_export_service.py::TestResultObject'),
    4: (PIPELINE + '::test_pdf_result_download_archive_and_idempotency',
        PIPELINE + '::test_damaged_cache_is_never_reused',
        PIPELINE + '::test_same_meeting_direct_concurrency_serializes_conversion',
        PIPELINE + '::test_registration_failure_and_retry',
        PIPELINE + '::test_selected_version_missing_draft_and_new_final',
        'tests/unit/test_export_service.py::TestIdempotency',
        'tests/integration/test_export_performance.py'),
    5: (PIPELINE + '::test_before_archive_merge_collection_key_and_repair',
        PIPELINE + '::test_pdf_result_download_archive_and_idempotency',
        PIPELINE + '::test_missing_archive_does_not_fabricate_history',
        PIPELINE + '::test_undo_only_pdf_and_retry_after_partial_failure'),
    6: (CONTRACT, PIPELINE + '::test_pdf_result_download_archive_and_idempotency',
        PIPELINE + '::test_download_final_and_source_checks',
        'tests/unit/test_api_diff_export.py',
        'tests/contract/test_pdf_desktop_contract.py'),
    7: ('tests/integration/test_pdf_soffice.py',),
    # 外层 integration 不进入自身证据子进程，避免递归。
    8: ('tests/unit/test_accept_f5_9.py',),
}
REAL_SKIP = 'F5.9 真转换待验：未安装/未配置 soffice，须目标机 LibreOffice 运行'


def pytest_configure(config):
    """验收仅用合成资料，禁止 socket 出站。"""
    from _netguard import install_network_guard

    install_network_guard()


def _counts(report: Path):
    """按叶子 testcase 计数；拒绝空报告、重复结果和不一致的汇总。"""
    root = ET.parse(report).getroot()
    suites = list(root.iter('testsuite'))
    if root.tag not in ('testsuites', 'testsuite') or not suites:
        raise ValueError('无 testsuite')
    cases = list(root.iter('testcase'))
    skipped = sum(len(c.findall('skipped')) for c in cases)
    failed = sum(len(c.findall('failure')) for c in cases)
    errors = sum(len(c.findall('error')) for c in cases)
    if any(sum(len(c.findall(k)) for k in ('skipped', 'failure', 'error')) > 1 for c in cases):
        raise ValueError('testcase 结果冲突')
    # pytest JUnit 无嵌套 suite；不接受其他结构，以免双重累计。
    if any(s.findall('testsuite') for s in suites):
        raise ValueError('嵌套 testsuite 不受支持')
    totals = [sum(int(s.attrib[k]) for s in suites)
              for k in ('tests', 'failures', 'errors', 'skipped')]
    if totals != [len(cases), failed, errors, skipped]:
        raise ValueError('报告汇总与 testcase 不一致')
    return len(cases) - skipped, len(cases) - skipped - failed - errors, failed, errors, skipped, cases


def _run_evidence(number: int, tmp: Path) -> str:
    report = tmp / 'report.xml'
    report.unlink(missing_ok=True)
    capture_flags = ['-s'] if number == 7 else []  # 真转换版本/哈希须进入 stdout。
    command = [sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-ra',
               *capture_flags, '-p', 'no:cacheprovider', '-p', 'accept_f5_9',
               '--basetemp', str(tmp / 'data'), '--junitxml', str(report), *CASES[number]]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(PROJECT_ROOT / 'tools'), str(PROJECT_ROOT / 'src')))
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    env.pop('PYTEST_ADDOPTS', None)
    print('命令：' + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end='', flush=True)
    try:
        executed, passed, failed, errors, skipped, cases = _counts(report)
    except (OSError, ET.ParseError, ValueError, KeyError) as exc:
        print(f'计数：executed=0 passed=0 failed=0 error=1 skipped=0；报告不可用：{exc}')
        return 'FAIL'
    print(f'计数：executed={executed} passed={passed} failed={failed} error={errors} skipped={skipped}')
    if result.returncode == 0 and executed > 0 and not (failed or errors or skipped):
        return 'PASS'
    # 只有固定真转换的缺环境理由才是 PENDING；其他 skip 都是失败。
    if (number == 7 and result.returncode == 0 and executed == 0 and skipped == 1
            and not (failed or errors) and len(cases) == 1
            and cases[0].find('skipped').get('message') == REAL_SKIP):
        return 'PENDING'
    return 'FAIL'


def main(argv=None):
    print(PENDING, flush=True)
    parser = argparse.ArgumentParser(description='F5.9 八项定向验收；自动项 REQUIRED，7 独立门控')
    parser.add_argument('--only', type=int, choices=range(1, 9))
    args = parser.parse_args(argv)
    results = {}
    with tempfile.TemporaryDirectory(prefix='f59_') as directory:
        for number, name in enumerate(NAMES, 1):
            if args.only and args.only != number:
                continue
            print(f'\n标准 {number}：{name}', flush=True)
            tmp = Path(directory) / str(number)
            tmp.mkdir()
            try:
                status = _run_evidence(number, tmp)
            except Exception as exc:
                print(f'计数：executed=0 passed=0 failed=0 error=1 skipped=0；执行异常：{exc}')
                status = 'FAIL'
            results[number] = status
    for number, status in results.items():
        print(f'{status} 标准 {number} {NAMES[number - 1]}')
    passed = sum(s == 'PASS' for s in results.values())
    pending = sum(s == 'PENDING' for s in results.values())
    print(f'本次所选 {len(results)} 项：{passed} PASS + {pending} PENDING + {len(results) - passed - pending} FAIL')
    if args.only:
        success = results[args.only] == 'PASS'
        print(f"SELECTED_{'PASS' if success else 'FAIL'} F5.9（仅标准 {args.only}，不代表全八项）")
    else:
        auto_pass = sum(results[n] == 'PASS' for n in (1, 2, 3, 4, 5, 6, 8))
        success = auto_pass == 7 and results[7] in ('PASS', 'PENDING')
        print(f"TOOLING_{'PASS' if success else 'FAIL'} F5.9（{auto_pass}/7）")
    if results.get(7) == 'PASS':
        print('真实转换自动证据通过；人工版式/字体与桌面操作仍 PENDING，目标机签字通过 0')
    else:
        print(PENDING)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
