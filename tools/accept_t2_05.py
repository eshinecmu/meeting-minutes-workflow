"""T2-05 仅合成工具验收；每项隔离子进程，完整输出，不代替真实影子验收。"""
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
PENDING = "真实 ≥20 场：PENDING；本机未运行真实会议；计入真实验收 0；须使用单位受控环境人工比对"
NAMES = ("批量生成但不交付", "安全失败与隔离", "人工评分与记录表", "五维与建议阈值",
         "放开与渐进授权", "附加风险计数", "报告可复核", "验收脚本可信")
PIPELINE = "tests/integration/test_shadow_pipeline.py"
SCORING = "tests/unit/test_shadow_scoring.py"
CONTRACT = "tests/contract/test_shadow_contract.py"
CLI = "tests/integration/test_shadow_cli.py"
# 仅固定定向证据；完整脚本子进程证据由外层 integration 执行，避免递归。
CASES = {
    1: (PIPELINE + "::test_synthetic_equals_production_pre_g3_and_zero_delivery_writes",),
    2: (PIPELINE,),
    3: (CONTRACT, SCORING + "::test_score_binding_and_locations_reject"),
    4: (SCORING, CONTRACT + "::test_threshold_ranges_and_redlines"),
    5: (SCORING + "::test_divergence_AND_counts_items_and_meetings",
        SCORING + "::test_real_stage2_conditions_SYNTHETIC_METADATA_ONLY",
        "tests/unit/test_shadow_report.py::test_report_json_markdown_records_same_source_and_pending_ledger"),
    6: ("tests/unit/test_shadow_risks.py",
        SCORING + "::test_same_issue_cross_meetings_not_merged_and_optional_population_is_separate",
        SCORING + "::test_optional_action_population_relationship_validation"),
    7: ("tests/unit/test_shadow_report.py", CLI),
    8: ("tests/unit/test_accept_t2_05.py",),
}


def _assert(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'} {label}")
    return bool(cond)


def pytest_configure(config):
    """所有证据子进程禁止 socket 出站；不启动真实模型。"""
    from _netguard import install_network_guard

    install_network_guard()


def _run_evidence(number: int, tmp: Path) -> bool:
    report = tmp / "report.xml"
    report.unlink(missing_ok=True)
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
               "-p", "accept_t2_05", "--basetemp", str(tmp / "data"), "--junitxml", str(report),
               *CASES[number]]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(PROJECT_ROOT / "tools"), str(PROJECT_ROOT / "src")))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    # 原样记录实际 argv 和 stdout；临时路径也保留，便于复核。
    print("命令：" + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if not report.exists():
        print("计数：executed=0 passed=0 failed=0 error=1 skipped=0")
        return _assert(False, "证据报告缺失")
    try:
        suites = list(ET.parse(report).getroot().iter("testsuite"))
        counts = {key: sum(int(s.get(key, 0)) for s in suites)
                  for key in ("tests", "failures", "errors", "skipped")}
    except (ET.ParseError, ValueError) as exc:
        print(f"计数：executed=0 passed=0 failed=0 error=1 skipped=0；报告错误 {exc}")
        return False
    executed = counts["tests"] - counts["skipped"]
    passed = executed - counts["failures"] - counts["errors"]
    print(f"计数：executed={executed} passed={passed} failed={counts['failures']} "
          f"error={counts['errors']} skipped={counts['skipped']}")
    return _assert(result.returncode == 0 and counts["tests"] > 0
                   and not any(counts[k] for k in ("failures", "errors", "skipped")),
                   f"定向证据 {counts['tests']} 项，失败 {counts['failures']}，错误 {counts['errors']}，跳过 {counts['skipped']}")


def main(argv=None):
    # 在参数解析前声明，连非法 --only 也不隐去真实待验边界。
    print(PENDING, flush=True)
    parser = argparse.ArgumentParser(description="T2-05 八项 SYNTHETIC 工具验收")
    parser.add_argument("--only", type=int, choices=range(1, 9))
    args = parser.parse_args(argv)
    results = []
    print("SYNTHETIC / 合成数据，非真实验收结果；隔离临时目录，无真实模型 / 无网络。")
    with tempfile.TemporaryDirectory(prefix="t205_") as directory:
        for number, name in enumerate(NAMES, 1):
            if args.only and args.only != number:
                continue
            print(f"\n标准 {number}：{name}", flush=True)
            tmp = Path(directory) / str(number)
            tmp.mkdir()
            try:
                ok = _run_evidence(number, tmp)
            except Exception as exc:
                print("计数：executed=0 passed=0 failed=0 error=1 skipped=0")
                ok = _assert(False, f"执行异常：{type(exc).__name__}: {exc}")
            results.append((number, ok))
    for number, ok in results:
        print(f"{'PASS' if ok else 'FAIL'} 标准 {number} {NAMES[number - 1]}")
    passed = sum(ok for _, ok in results)
    print(f"共 {len(results)} 项，通过 {passed} 项（仅本次所选标准）")
    success = bool(results) and passed == len(results)
    print(f"{'TOOLING_PASS' if success else 'TOOLING_FAIL'} T2-05（{passed}/{len(results)}）")
    print(PENDING)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
