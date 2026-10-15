"""CDP 矩阵：找出「渲染层真的能渲染」的最小开关集。

判据不是"进程还活着"，而是 CDP 里 `document.title` 与正文是否出来。
上一轮踩过的坑：`--disable-gpu-sandbox` 能让进程活着、窗口也在，
但渲染进程其实**卡死**（title 为空、连 `1+1` 都算不出来）——
只看进程数会得出完全错误的结论。

用法：
  python tools/cdp_matrix.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

CANDIDATES: list[tuple[str, str]] = [
    ("no-sandbox", "no-sandbox"),
    ("gpuoff+gpusb-off", "disable-gpu disable-gpu-compositing disable-gpu-sandbox"),
    ("gpusb-off+noCodeIntegrity", "disable-gpu-sandbox --disable-features=RendererCodeIntegrity"),
    ("no-zygote", "no-zygote"),
    ("gpusb-off+no-zygote", "disable-gpu-sandbox no-zygote"),
    ("single-process", "single-process"),
    ("gpusb-off", "disable-gpu-sandbox"),
]


def run_one(name: str, switches: str) -> tuple[bool, str]:
    out = PROJECT_ROOT / "build" / f"mx-{name.replace('+', '_').replace(' ', '')}.txt"
    cmd = [
        str(PY), str(PROJECT_ROOT / "tools" / "cdp_probe.py"),
        "--gpu-switches", switches,
        "--out", f"build/mx-{name.replace('+', '_').replace(' ', '')}",
    ]
    with open(out, "wb") as fh:
        subprocess.run(cmd, cwd=str(PROJECT_ROOT), stdout=fh, stderr=subprocess.STDOUT)
    text = out.read_text(encoding="utf-8", errors="replace")

    m = re.search(r"\[DOM\] title=(.*?) readyState=(.*?) bodyTextLen=(-?\d+)", text)
    if not m:
        # 抓一行失败原因，便于判断是崩了还是卡了
        reason = ""
        for line in text.splitlines():
            if line.startswith("[FAIL]") or line.startswith("[CDP]"):
                reason = line.strip()
                break
        return False, reason or "no DOM output"

    title, ready, blen = m.group(1).strip("'"), m.group(2).strip("'"), int(m.group(3))
    ok = bool(title) and blen > 0
    return ok, f"title={title!r} ready={ready!r} len={blen}"


def main() -> int:
    print(f"{'case':<30}{'ok':<5}detail")
    print("-" * 100)
    rows = []
    for name, sw in CANDIDATES:
        ok, detail = run_one(name, sw)
        rows.append((name, ok, detail))
        print(f"{name:<30}{('YES' if ok else 'NO'):<5}{detail}", flush=True)

    print("-" * 100)
    winners = [r for r in rows if r[1]]
    if winners:
        print("渲染层可用的开关集：")
        for n, _, d in winners:
            print(f"  - {n}   ({d})")
    else:
        print("没有任何候选能让渲染层工作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
