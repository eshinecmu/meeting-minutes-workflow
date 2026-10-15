"""T2-04 离线验收：复用定向测试证据，自动标准与 Electron 人工项分开。

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

NAMES = ("无依据硬阻断与出口防线", "责任人及粒度规则", "双引擎生产模板", "数字与人名一致性", "统一问题清单与文本下载", "模糊与多段溯源", "旧会议恢复与实际交付", "验收证据边界")
UNIT = "tests/unit/"
CONTRACT = "tests/contract/"
INTEGRATION = "tests/integration/"
PIPELINE = INTEGRATION + "test_post_validation_pipeline.py"
FINALIZATION = INTEGRATION + "test_minutes_finalization.py"
EVIDENCE = INTEGRATION + "test_accept_t2_04_evidence.py"
# 固定入口，缺文件或收集失败必须报 FAIL；只运行本卡定向证据。
CASES = {
    1: (PIPELINE + "::test_blocked_priority_and_persisted_failed",
        EVIDENCE + "::test_http_hard_route_and_failed_download",
        FINALIZATION + "::test_direct_runner_blocks_and_persists_failed",
        FINALIZATION + "::test_graph_cache_and_http_download_do_not_hide_bad_candidate",
        FINALIZATION + "::test_registered_decision_replay_rechecks_before_final",
        UNIT + "test_grounding.py::test_partial_bad_ref_cannot_hide",
        CONTRACT + "test_post_validation_contract.py::test_missing_source_precheck_preserves_g3"),
    2: (UNIT + "test_minutes_granularity.py",),
    3: (PIPELINE + "::test_both_engines_use_template_snapshot_and_checks",
        PIPELINE + "::test_a6_chunk_metadata_missing_aggregates_without_schema_retry"),
    4: (UNIT + "test_minutes_consistency.py", EVIDENCE + "::test_real_joint_issues_and_repair"),
    5: (EVIDENCE + "::test_real_joint_issues_and_repair", CONTRACT + "test_post_validation_contract.py",
        UNIT + "test_minutes_issues.py", CONTRACT + "test_gate_desktop_contract.py"),
    6: (UNIT + "test_grounding.py", UNIT + "test_extraction_grounding.py"),
    7: (INTEGRATION + "test_minutes_checkpoint_compat.py", FINALIZATION,
        UNIT + "test_minutes_state_compat.py", UNIT + "test_minutes_store.py"),
    8: (UNIT + "test_accept_t2_04.py",),
}
MANUAL = (
    "Electron G3 结构化编辑、问题级别 / fuzzy 提示、来源展示及真实 UTF-8 文本下载",
    "Electron 纪要 / 记录稿选择、DOCX 正文与版式、目标机 soffice 转换",
    "V3：IT-24 与要素齐备率的目标机证据",
    "V4：随机 10 条引用可点击且内容吻合的操作记录 / 截图；真实音频时间元数据",
    "R-4：自动阻断证据已覆盖；完整红线验收仍待目标机 / 人工证据",
    "R-5：T2-05 影子无依据率=0、语义支持与规则误报 / 待办误降级评估",
)


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'} {label}")
    return bool(cond)


# 通过 -p accept_t2_04 加载到证据子进程；ASGI 不建立网络连接。
def pytest_configure(config):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("T2-04 验收禁止网络连接")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    socket.socket.sendto = forbidden
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = forbidden


def _run_evidence(number: int, tmp: Path) -> bool:
    report = tmp / "report.xml"
    report.unlink(missing_ok=True)
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
               "-p", "accept_t2_04", "--basetemp", str(tmp / "data"), "--junitxml", str(report),
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
    from mmwf.graph import build

    # 真路由探针也在父进程执行，反例测试可直接破坏生产函数。
    ok = True
    for retry in (0, build.MAX_VALIDATE_RETRY, build.MAX_VALIDATE_RETRY + 1):
        state = {"validation_issues": [{"blocking": True}], "gen_retry": retry}
        ok = _assert(build.route_after_validate(state) == "blocked",
                     f"hard route 优先于 retry={retry}") and ok
    return _run_evidence(1, tmp) and ok


def check_standard_2(tmp: Path) -> bool:
    return _run_evidence(2, tmp)


def check_standard_3(tmp: Path) -> bool:
    return _run_evidence(3, tmp)


def check_standard_4(tmp: Path) -> bool:
    return _run_evidence(4, tmp)


def check_standard_5(tmp: Path) -> bool:
    return _run_evidence(5, tmp)


def check_standard_6(tmp: Path) -> bool:
    return _run_evidence(6, tmp)


def check_standard_7(tmp: Path) -> bool:
    return _run_evidence(7, tmp)


def check_standard_8(tmp: Path) -> bool:
    return _run_evidence(8, tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description="T2-04 八项自动证据验收（人工另列）")
    parser.add_argument("--only", type=int, choices=range(1, 9), help="只运行指定标准（1-8）")
    args = parser.parse_args()
    results = []
    print("临时数据 / DB / 令牌：每项隔离，结束清理；真实图 + 进程内 HTTP，无模型 / 无网络")
    print("证据复用现有定向 pytest；命令与输出中的临时路径统一显示为 <临时目录>。")
    print("Claude 规则 9：遵循 D01～D10；D6 仅补验收与文档，无生产状态 / 端点 / schema 变更。")
    with tempfile.TemporaryDirectory(prefix="t204_") as directory:
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
    banner("T2-04 验收结果（仅自动证据）")
    for number, name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} 标准 {number}  {name}")
    passed = sum(bool(ok) for _, _, ok in results)
    print(f"\n  共 {len(results)} 项，通过 {passed} 项")
    success = bool(results) and passed == len(results)
    print(f"{'PASS' if success else 'FAIL'} T2-04 自动验收{'通过' if success else '未通过'}（{passed}/{len(results)}）")
    print("人工项不计通过；V3/V4/R-4/R-5 未全闭环，下一张 T2-05。")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
