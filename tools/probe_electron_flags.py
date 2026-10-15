"""打包产物启动开关探测（T1 收尾用）。

目的：在**不改产品代码**的前提下，找出「双击 mmwf.exe 能出窗口」的最小开关集。

为什么要做成矩阵而不是逐个手试：
  上一次排查时，我把「--no-sandbox」和「GPU 开关」混在一次运行里跑，
  结果误判成「GPU 开关生效」——两个变量同时变，结论不可信。
  这里每轮只变一个变量，每轮独立起停，结论才站得住。

判据（全部来自客观证据，不靠"看起来好像出来了"）：
  A. 进程存活       —— 12 秒后 mmwf.exe 仍在
  B. 窗口标题非空   —— tasklist /V 的 Window Title 字段（Electron 主窗口）
  C. 握手文件齐     —— %LOCALAPPDATA%/mmwf/{port, session.token} 都在
  D. 后端就绪       —— 日志出现 "Uvicorn running on"
  E. 无 GPU 致命错  —— 日志不出现 "GPU process isn't usable" / "exit_code=-1073741819"

用法：
  python tools/probe_electron_flags.py                 # 跑默认矩阵
  python tools/probe_electron_flags.py --only no-sandbox
  python tools/probe_electron_flags.py --exe <路径>
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = PROJECT_ROOT / "dist" / "win-unpacked" / "mmwf.exe"

USER_DIR = Path(os.path.expanduser("~")) / "AppData" / "Local" / "mmwf"
PORT_FILE = USER_DIR / "port"
TOKEN_FILE = USER_DIR / "session.token"
BACKEND_LOG = USER_DIR / "logs" / "backend.log"

WAIT_SECONDS = 12.0

# 每一行是一个候选开关集。命名即含义。
MATRIX: list[tuple[str, list[str]]] = [
    ("baseline", []),
    ("disable-gpu-sandbox", ["--disable-gpu-sandbox"]),
    ("no-sandbox", ["--no-sandbox"]),
    ("in-process-gpu", ["--in-process-gpu"]),
    ("swiftshader", ["--use-gl=swiftshader", "--disable-gpu-sandbox"]),
    ("no-sandbox+gpu-off", ["--no-sandbox", "--disable-gpu"]),
]

CRASH_MARKERS = (
    "GPU process isn't usable",
    "exit_code=-1073741819",
    "GPU process exited unexpectedly",
)


def _kill_mmwf() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "mmwf.exe"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    # 后端是 python 子进程，父进程被强杀后可能残留；按命令行匹配清理。
    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*mmwf.main*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    time.sleep(0.8)


def _reset_handshake() -> None:
    for p in (PORT_FILE, TOKEN_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    # 日志留着会串味，直接清空
    try:
        BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        BACKEND_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass


def _window_titles() -> list[str]:
    """用 tasklist /V 取窗口标题（比 WMIC 稳，比 psutil 少依赖）。"""
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq mmwf.exe", "/V", "/FO", "CSV"],
        capture_output=True,
        text=True,
        errors="replace",
    ).stdout
    titles: list[str] = []
    reader = csv.reader(io.StringIO(out))
    next(reader, None)  # 表头
    for row in reader:
        if len(row) >= 9:
            t = row[8].strip()
            if t and t != "N/A":
                titles.append(t)
    return titles


def _proc_count() -> int:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq mmwf.exe", "/FO", "CSV"],
        capture_output=True,
        text=True,
        errors="replace",
    ).stdout
    return max(0, sum(1 for line in out.splitlines() if line.startswith('"mmwf.exe"')))


def run_case(name: str, flags: list[str], exe: Path) -> dict:
    _kill_mmwf()
    _reset_handshake()

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)  # ★ 不清掉这个，Electron 会当纯 Node 跑

    log_path = PROJECT_ROOT / "build" / f"probe-{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "wb") as fh:
        proc = subprocess.Popen(
            [str(exe), *flags],
            cwd=str(exe.parent),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
        )

    time.sleep(WAIT_SECONDS)

    alive = _proc_count()
    titles = _window_titles()
    port_ok = PORT_FILE.exists()
    token_ok = TOKEN_FILE.exists()
    log_text = ""
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

    backend_ready = "Uvicorn running on" in log_text
    crashes = [m for m in CRASH_MARKERS if m in log_text]

    _kill_mmwf()

    verdict = (
        alive > 0
        and bool(titles)
        and port_ok
        and token_ok
        and backend_ready
        and not crashes
    )
    return {
        "case": name,
        "flags": " ".join(flags) or "(none)",
        "alive": alive,
        "window": titles[0] if titles else "",
        "handshake": f"{'P' if port_ok else '-'}{'T' if token_ok else '-'}",
        "backend": backend_ready,
        "crash": crashes[0] if crashes else "",
        "pass": verdict,
        "log": str(log_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--only", default=None, help="只跑名字匹配的用例（子串）")
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"[FATAL] 找不到可执行文件：{exe}", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("[SKIP] 本脚本只针对 Windows 打包产物", file=sys.stderr)
        return 3

    cases = [c for c in MATRIX if not args.only or args.only in c[0]]
    results = []
    for name, flags in cases:
        print(f"\n=== [{name}] {flags or '(no flags)'} ===", flush=True)
        r = run_case(name, flags, exe)
        results.append(r)
        mark = "PASS" if r["pass"] else "FAIL"
        print(
            f"  {mark}  alive={r['alive']}  window={r['window']!r}  "
            f"handshake={r['handshake']}  backend={r['backend']}  crash={r['crash']!r}",
            flush=True,
        )

    print("\n" + "=" * 78)
    print(f"{'case':<22}{'alive':<7}{'win':<5}{'hs':<5}{'be':<5}{'crash':<7}pass")
    print("-" * 78)
    for r in results:
        print(
            f"{r['case']:<22}{r['alive']:<7}{('Y' if r['window'] else 'N'):<5}"
            f"{r['handshake']:<5}{('Y' if r['backend'] else 'N'):<5}"
            f"{('Y' if r['crash'] else 'N'):<7}{'PASS' if r['pass'] else 'FAIL'}"
        )
    print("=" * 78)

    winners = [r for r in results if r["pass"]]
    if winners:
        print(f"\n可用开关集：{winners[0]['flags']}")
    else:
        print("\n没有单开关可用 —— 需要组合或走启动器方案。")
    return 0 if winners else 1


if __name__ == "__main__":
    raise SystemExit(main())
