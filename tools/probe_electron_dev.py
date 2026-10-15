"""开发模式下的 GPU 开关隔离探测（T1 收尾排查用）。

与 `probe_electron_flags.py` 的区别：
  - 那个测**打包产物**（`mmwf.exe`），每轮要重新打包才能改开关；
  - 这个测**开发模式**（`electron.exe .`），改 `main.js` 立即生效。

排查目的：找出「最小且不牺牲安全」的开关集。
当前产品代码打的是 `disable-gpu + disable-gpu-compositing + disable-software-rasterizer`，
打包产物实测**仍然崩**。怀疑 `disable-software-rasterizer` 是自伤 ——
它在关掉硬件加速之后又把唯一的软件兜底也关了，GPU 进程无事可做直接崩。

用法：
  python tools/probe_electron_dev.py
  python tools/probe_electron_dev.py --only rm-rasterizer
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_DIR = PROJECT_ROOT / "src" / "desktop"
ELECTRON = DESKTOP_DIR / "node_modules" / "electron" / "dist" / "electron.exe"

USER_DIR = Path(os.path.expanduser("~")) / "AppData" / "Local" / "mmwf"
PORT_FILE = USER_DIR / "port"
TOKEN_FILE = USER_DIR / "session.token"

WAIT_SECONDS = 13.0
PROC_NAME = "electron.exe"

# (用例名, MMWF_GPU_MODE, MMWF_GPU_SWITCHES)
#   mode='off'    -> 什么都不做（保留硬件加速）
#   mode='custom' -> 只打 switches
MATRIX: list[tuple[str, str, str]] = [
    ("gpu-on (baseline)", "off", ""),
    ("dhw-accel-only", "custom", ""),
    ("disable-gpu", "custom", "disable-gpu"),
    ("disable-gpu+compositing", "custom", "disable-gpu disable-gpu-compositing"),
    ("product-current (3 sw)", "custom", "disable-gpu disable-gpu-compositing disable-software-rasterizer"),
    ("in-process-gpu", "custom", "in-process-gpu"),
    ("disable-gpu-sandbox", "custom", "disable-gpu-sandbox"),
    ("swiftshader", "custom", "use-gl=swiftshader"),
    ("disable-gpu+compositing+swiftshader", "custom", "disable-gpu disable-gpu-compositing use-gl=swiftshader"),
]

CRASH_MARKERS = (
    "GPU process isn't usable",
    "exit_code=-1073741819",
    "GPU process exited unexpectedly",
)


def _kill() -> None:
    subprocess.run(["taskkill", "/F", "/IM", PROC_NAME], capture_output=True, errors="replace")
    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*mmwf.main*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
        errors="replace",
    )
    time.sleep(0.8)


def _reset_handshake() -> None:
    for p in (PORT_FILE, TOKEN_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _tasklist_csv() -> list[list[str]]:
    raw = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {PROC_NAME}", "/V", "/FO", "CSV"],
        capture_output=True,
    ).stdout
    # ★ 中文 Windows 的 tasklist 输出是 GBK；用 UTF-8 解会得到一串替换符，
    #   窗口标题就变成乱码 —— 上一版就栽在这，误把乱码当"标题非空"的证据。
    text = raw.decode("gbk", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def _window_titles() -> list[str]:
    rows = _tasklist_csv()
    titles = []
    for row in rows[1:]:
        if len(row) >= 9:
            t = row[8].strip()
            if t and t != "N/A":
                titles.append(t)
    return titles


def _proc_count() -> int:
    rows = _tasklist_csv()
    return max(0, sum(1 for r in rows if r and r[0].strip().lower() == PROC_NAME))


def run_case(name: str, mode: str, switches: str) -> dict:
    _kill()
    _reset_handshake()

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["MMWF_GPU_MODE"] = mode
    env["MMWF_GPU_SWITCHES"] = switches

    log_path = PROJECT_ROOT / "build" / f"devprobe-{name.replace(' ', '_').replace('+', '-')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "wb") as fh:
        subprocess.Popen(
            [str(ELECTRON), "."],
            cwd=str(DESKTOP_DIR),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
        )

    time.sleep(WAIT_SECONDS)

    alive = _proc_count()
    titles = _window_titles()
    port_ok = PORT_FILE.exists()
    token_ok = TOKEN_FILE.exists()
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""

    backend_ready = "Uvicorn running on" in log_text
    crashes = [m for m in CRASH_MARKERS if m in log_text]
    _kill()

    ok = alive > 0 and bool(titles) and port_ok and token_ok and backend_ready and not crashes
    return {
        "case": name,
        "mode": mode,
        "switches": switches or "(none)",
        "alive": alive,
        "window": titles[0] if titles else "",
        "hs": f"{'P' if port_ok else '-'}{'T' if token_ok else '-'}",
        "be": backend_ready,
        "crash": crashes[0] if crashes else "",
        "pass": ok,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    if not ELECTRON.exists():
        print(f"[FATAL] 找不到 electron：{ELECTRON}", file=sys.stderr)
        return 2

    cases = [c for c in MATRIX if not args.only or args.only in c[0]]
    results = []
    for name, mode, sw in cases:
        print(f"\n=== [{name}] mode={mode} switches={sw or '(none)'} ===", flush=True)
        r = run_case(name, mode, sw)
        results.append(r)
        print(
            f"  {'PASS' if r['pass'] else 'FAIL'}  alive={r['alive']}  "
            f"window={r['window']!r}  hs={r['hs']}  be={r['be']}  crash={r['crash']!r}",
            flush=True,
        )

    print("\n" + "=" * 92)
    print(f"{'case':<38}{'alive':<7}{'win':<5}{'hs':<5}{'be':<5}{'crash':<7}pass")
    print("-" * 92)
    for r in results:
        print(
            f"{r['case']:<38}{r['alive']:<7}{('Y' if r['window'] else 'N'):<5}"
            f"{r['hs']:<5}{('Y' if r['be'] else 'N'):<5}"
            f"{('Y' if r['crash'] else 'N'):<7}{'PASS' if r['pass'] else 'FAIL'}"
        )
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
