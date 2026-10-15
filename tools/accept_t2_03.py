"""T2-03 离线验收：复用定向测试证据，自动标准与 Electron 人工项分开。

每项在独立 pytest 子进程运行，tmp_path 的数据、DB、令牌均归临时目录。
真实 HTTP 为 TestClient 的进程内 ASGI 传输；禁止 socket 出站，不启动模型。
复用已有向量避免另写一套较弱断言；本脚本不会运行全套测试。
"""
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
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mmwf.domain.review.gate_projection import display_topics

NAMES = ("HTTP 三态与检查点", "真实节点计数与编辑成稿", "无状态段落差异", "待确认项与四要素", "超时巡检与有限恢复", "决策审计与事务")
CONTRACT = "tests/contract/"
UNIT = "tests/unit/"
INTEGRATION = "tests/integration/"
DECISIONS = INTEGRATION + "test_gate_decisions.py"
DESKTOP = CONTRACT + "test_gate_desktop_contract.py"
# 精确列出证据入口；修改或删除测试会报收集错误，不能静默跳过。
CASES = {
    1: (CONTRACT + "test_gate_contract.py", UNIT + "test_gate_edit.py",
        DECISIONS + "::test_concurrent_duplicates_and_stale_g2",
        DECISIONS + "::test_old_reject_cannot_decide_new_g2",
        DECISIONS + "::test_reject_limit_is_not_a_successful_rerun",
        DESKTOP + "::test_edit_and_checkpoint_wiring"),
    2: (DECISIONS + "::test_real_callbacks",
        *(UNIT + "test_graph_runner.py::TestU_ST_" + name for name in (
            "02_ApproveDoesNotRerun", "03_EditDoesNotRerun", "04_RejectRerunOnlyTarget")),
        *(INTEGRATION + "test_graph_pipeline.py::TestIT" + name for name in (
            "04_G2Edit", "05_G2Reject", "06_G3Edit"))),
    3: (CONTRACT + "test_gate_diff_contract.py", UNIT + "test_api_diff_export.py::TestDiff",
        DESKTOP + "::test_server_diff_and_styles"),
    4: (UNIT + "test_gate_projection.py", CONTRACT + "test_gate_diff_contract.py::test_g2_projection_and_no_writes",
        CONTRACT + "test_gate_diff_contract.py::test_round_confirmation",
        DESKTOP + "::test_four_factors_and_draft_filter"),
    5: (UNIT + "test_gate_timeout.py", DECISIONS + "::test_human_and_timeout_tick_compete",
        DECISIONS + "::test_registered_not_resumed_recovery",
        DECISIONS + "::test_startup_recovers_registered_decision",
        DECISIONS + "::test_startup_isolates_failed_record_and_recovers_next",
        DECISIONS + "::test_consumed_decisions_skip_historical_runners"),
    6: (INTEGRATION + "test_accept_t2_03_evidence.py",
        DECISIONS + "::test_exact_detail_snapshot_and_export",
        DECISIONS + "::test_snapshot_failure_prevents_registration_and_resume",
        DECISIONS + "::test_audit_failure_rolls_back_registration_leaves_harmless_snapshot",
        DECISIONS + "::test_registered_not_resumed_recovery",
        DECISIONS + "::test_consumed_decisions_skip_historical_runners",
        UNIT + "test_gate_timeout.py::test_exact_boundary_and_actual_reject_without_requests",
        UNIT + "test_audit.py", UNIT + "test_api_audit.py"),
}
MANUAL = (
    "G2/G3 三按钮真实操作、编辑表单错误提示",
    "四要素原文 / 时间跳转、低置信视觉区分",
    "已核对项隐藏、全部恢复、刷新后未提交草稿回到待确认",
    "系统稿 / 人工稿段落高亮、未变灰显与折叠",
    "连续超时提示可见、提交中禁重复点击、重启后仍待处理",
)


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'} {label}")
    return bool(cond)


# 通过 -p accept_t2_03 加载到证据子进程；ASGI 不建立网络连接。
def pytest_configure(config):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("T2-03 验收禁止网络连接")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden


def _run_evidence(number: int, tmp: Path) -> bool:
    report = tmp / "report.xml"
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
               "-p", "accept_t2_03", "--basetemp", str(tmp / "data"), "--junitxml", str(report),
               *CASES[number]]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(PROJECT_ROOT / "tools"), str(PROJECT_ROOT / "src")))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    # 输出路径用占位符表示，正文完整保留；实际命令仍使用本次临时目录。
    shown = [".venv/bin/python", *command[1:]]
    print("命令：" + shlex.join(shown).replace(str(tmp), "<临时目录>"), flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout.replace(str(tmp), "<临时目录>"), end="")
    if not report.exists():
        return _assert(False, "证据报告缺失")
    suites = list(ET.parse(report).getroot().iter("testsuite"))
    counts = {key: sum(int(s.get(key, 0)) for s in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    return _assert(result.returncode == 0 and counts["tests"] > 0
                   and not any(counts[k] for k in ("failures", "errors", "skipped")),
                   f"定向证据 {counts['tests']} 项，失败 {counts['failures']}，错误 {counts['errors']}，跳过 {counts['skipped']}")


def check_standard_1(tmp: Path) -> bool:
    return _run_evidence(1, tmp)


def check_standard_2(tmp: Path) -> bool:
    return _run_evidence(2, tmp)


def check_standard_3(tmp: Path) -> bool:
    return _run_evidence(3, tmp)


def check_standard_4(tmp: Path) -> bool:
    state = {"topics": [{"title": str(i), "confirmed": value}
                        for i, value in enumerate((True, False, False, "true"))]}
    topics = display_topics(state, [], "medium", 0.7)["topics"]
    reviewed = {1}
    # Python 真值表 + Electron 源码契约联证；不声称执行过 JS 或点击过 UI。
    pending = [i for i, t in enumerate(topics) if not (t["confirmed"] is True or i in reviewed)]
    refreshed = [i for i, t in enumerate(topics) if not (t["confirmed"] is True)]
    ok = _assert([t["confirmed"] for t in topics] == [True, False, False, False]
                 and pending == [2, 3] and refreshed == [1, 2, 3]
                 and len(topics) == 4, "混合权威状态 / 草稿过滤真值表、关闭恢复全部、刷新不持久化草稿")
    return _run_evidence(4, tmp) and ok


def check_standard_5(tmp: Path) -> bool:
    return _run_evidence(5, tmp)


def check_standard_6(tmp: Path) -> bool:
    return _run_evidence(6, tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description="T2-03 六项自动证据验收（人工另列）")
    parser.add_argument("--only", type=int, choices=range(1, 7), help="只运行指定标准（1-6）")
    args = parser.parse_args()
    results = []
    print("临时数据 / DB / 令牌：每项隔离，结束清理；真实图 + 进程内 HTTP，无模型 / 无网络")
    print("证据复用现有定向 pytest；命令与输出中的临时路径统一显示为 <临时目录>。")
    print("Claude 规则 9：D10 不改 PipelineState；D11 仅无状态 gate diff、整闸口提交；D12 不改 schema、仅有限恢复。")
    with tempfile.TemporaryDirectory(prefix="t203_") as directory:
        for number, name in enumerate(NAMES, 1):
            if args.only and args.only != number:
                continue
            banner(f"标准 {number}：{name}（自动证据）")
            tmp = Path(directory) / str(number)
            tmp.mkdir()
            try:
                ok = globals()[f"check_standard_{number}"](tmp)
            except Exception as exc:
                ok = _assert(False, f"执行异常：{type(exc).__name__}: {exc}")
            results.append((number, name, ok))
    banner("Electron / 视觉人工项（未执行，不计通过数）")
    for item in MANUAL:
        print(f"  待验 {item}")
    banner("T2-03 验收结果（仅自动证据）")
    for number, name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} 标准 {number}  {name}")
    passed = sum(bool(ok) for _, _, ok in results)
    print(f"\n  共 {len(results)} 项，通过 {passed} 项")
    success = passed == len(results)
    print(f"{'PASS' if success else 'FAIL'} T2-03 自动验收{'通过' if success else '未通过'}（{passed}/{len(results)}）")
    print("人工项仍待操作记录 / 截图；V5 未闭环。")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
