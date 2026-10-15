"""通过 Chrome DevTools 协议（CDP）核实渲染层是否真的渲染了页面。

为什么需要它：
  Electron 窗口是 Chromium 的**合成表面**，用 BitBlt / PrintWindow 截出来的
  往往只有背景色（一片空白）——**不能据此判断"界面白屏"**。
  上一轮我就差点把截图里的空白当成"渲染失败"。
  走 CDP 的 `Page.captureScreenshot` 拿的是合成后的真实像素，
  `Runtime.evaluate` 还能直接问 DOM 要文本，两条互为佐证。

不引入任何新依赖：手写最小 WebSocket 客户端（stdlib socket + base64 + struct）。

用法：
  python tools/cdp_probe.py --gpu-switches disable-gpu-sandbox
  python tools/cdp_probe.py --gpu-switches in-process-gpu --out build/cdp-inproc
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_DIR = PROJECT_ROOT / "src" / "desktop"
ELECTRON = DESKTOP_DIR / "node_modules" / "electron" / "dist" / "electron.exe"

USER_DIR = Path(os.path.expanduser("~")) / "AppData" / "Local" / "mmwf"
PORT_FILE = USER_DIR / "port"
TOKEN_FILE = USER_DIR / "session.token"

CDP_PORT = 9333


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端
# ---------------------------------------------------------------------------
class WS:
    def __init__(self, url: str, timeout: float = 20.0) -> None:
        # ws://127.0.0.1:9333/devtools/page/XXXX
        assert url.startswith("ws://"), url
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手期间连接被关闭")
            head += chunk
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise RuntimeError(f"WebSocket 握手失败：{status_line}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    # -- 收 --
    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        b0, b1 = self._read_exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return (opcode, payload, fin)

    def recv_text(self) -> str:
        """收到完整文本消息为止，自动跳过 ping/pong、拼装分片。"""
        parts: list[bytes] = []
        while True:
            opcode, payload, fin = self._read_frame()
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise RuntimeError("服务端关闭了 WebSocket")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    # -- 发 --
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def call(self, msg_id: int, method: str, params: dict | None = None) -> dict:
        self._send_frame(0x1, json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode())
        while True:
            msg = json.loads(self.recv_text())
            if msg.get("id") == msg_id:
                return msg

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _kill() -> None:
    for name in ("electron.exe", "mmwf.exe"):
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, errors="replace")
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


def _wait_cdp(deadline: float) -> list[dict]:
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as r:
                targets = json.loads(r.read().decode("utf-8"))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages
            last = f"目标里还没有 page：{[t.get('type') for t in targets]}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.7)
    raise RuntimeError(f"CDP 未就绪（{last}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="build/cdp-probe")
    ap.add_argument("--wait", type=float, default=25.0)
    ap.add_argument(
        "--exe", default=None,
        help="打包产物 mmwf.exe 的路径；不传则跑开发模式（electron .）",
    )
    ap.add_argument(
        "--electron-arg", action="append", default=[],
        help="透传给 Electron 的命令行开关（可重复），如 --electron-arg=--no-sandbox",
    )
    args = ap.parse_args()

    if args.exe:
        target = Path(args.exe)
        if not target.exists():
            print(f"[FATAL] 找不到可执行文件：{target}", file=sys.stderr)
            return 2
        argv = [str(target)]
        cwd = str(target.parent)
        label = f"打包产物 {target.name}"
    else:
        if not ELECTRON.exists():
            print(f"[FATAL] 找不到 electron：{ELECTRON}", file=sys.stderr)
            return 2
        argv = [str(ELECTRON), "."]
        cwd = str(DESKTOP_DIR)
        label = "开发模式"
    argv.append(f"--remote-debugging-port={CDP_PORT}")
    argv.extend(args.electron_arg)
    print(f"[RUN] {label}：{' '.join(argv[1:]) or '(无附加开关)'}")

    out_base = PROJECT_ROOT / args.out
    out_base.parent.mkdir(parents=True, exist_ok=True)

    _kill()
    for p in (PORT_FILE, TOKEN_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)

    log_path = out_base.with_suffix(".log")
    with open(log_path, "wb") as fh:
        subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
        )

    rc = 0
    try:
        pages = _wait_cdp(time.time() + args.wait)
        page = pages[0]
        print(f"[CDP] page target: title={page.get('title')!r} url={page.get('url')!r}")

        ws = WS(page["webSocketDebuggerUrl"])
        mid = 0

        mid += 1
        ws.call(mid, "Runtime.enable")
        mid += 1
        r = ws.call(
            mid,
            "Runtime.evaluate",
            {
                "expression": (
                    "JSON.stringify({"
                    "title: document.title,"
                    "readyState: document.readyState,"
                    "bodyLen: (document.body ? document.body.innerText.length : -1),"
                    "text: (document.body ? document.body.innerText : '').slice(0,600),"
                    "errors: (window.__mmwfErrors || [])"
                    "})"
                ),
                "returnByValue": True,
            },
        )
        payload = r.get("result", {}).get("result", {}).get("value")
        info = json.loads(payload) if payload else {}
        print(f"[DOM] title={info.get('title')!r} readyState={info.get('readyState')!r} "
              f"bodyTextLen={info.get('bodyLen')}")
        print("[DOM] 正文前 600 字：")
        print("      " + (info.get("text") or "").replace("\n", "\n      "))

        # 截图单独兜底：没有可用合成器时 captureScreenshot 会一直挂着，
        # 但那不该抹掉上面已经拿到的 DOM 证据。
        try:
            mid += 1
            ws.call(mid, "Page.enable")
            mid += 1
            shot = ws.call(mid, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            data = shot.get("result", {}).get("data")
            if data:
                png = out_base.with_suffix(".png")
                png.write_bytes(base64.b64decode(data))
                print(f"[SHOT] 已保存真实合成截图：{png}（{png.stat().st_size} 字节）")
            else:
                print(f"[SHOT] 截图失败：{json.dumps(shot)[:300]}")
        except Exception as exc:  # noqa: BLE001
            print(f"[SHOT] 截图不可用（{type(exc).__name__}）—— 以 DOM 证据为准")

        ws.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        rc = 1
    finally:
        _kill()

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
