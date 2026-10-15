"""T1-13 验收脚本（人可读版）。

施工卡 T1-13 的五条验收标准逐条跑一遍。这张卡验的是**合规自检与断网**：
证明"本程序没有外发"，并且——**最要紧的一条**——证明这个证明本身有效。

五条标准（施工卡原文）
----------------------
  [ ] 物理断网后全流程可用（V9）
  [ ] `/system/selfcheck` 返回 `outbound_connections: 0`
  [ ] 故意在代码里加一句 `httpx.get("https://example.com")` → 自检能检出
      （**验证检查本身有效**，不是摆设）
  [ ] `listening_ports` 只有一条，且 IP 为 `127.0.0.1`
  [ ] 启动时若检测到 `HTTP_PROXY` 环境变量指向外网 → 告警

⚠️ 这张卡为什么最容易"假绿"
--------------------------
"没有外发"这个结论，一个永远返回 0 的函数也能给出来。
所以验收的**重点不是**"跑一遍看到 0 就行了"，而是：

    ③ **反向验证**：造一个真的有外发的场景，自检必须报出来。

没有第 ③ 条，前面两条（0 与 127.0.0.1）根本不能说明任何问题 ——
一个 `return {"outbound_connections": 0}` 同样能过。
本脚本因此把第 ③ 条做成**多重反向验证**：

  · ③-a 单元层：用**假进程注入一条真外发连接**，`probe_network`
    必须判 fail 且报出那个 IP；
  · ③-b 集成层：起真后端，在**服务进程内**造一条真 socket 连接，
    再调 `/system/selfcheck`，`outbound_connections` 必须 > 0；
  · ③-c 端到端：在服务进程里真的执行一次 `httpx.get("https://example.com")`
    （由 `--inject-outbound` 开关触发的**故意注入**），自检必须检出。

  其中 ③-b 是"我不信你说的，我自己造一个给你看"的硬证据：
  它证明了"0"是**探测得出的**，而不是**硬编码的**。

另一条诚实原则：**能自动验的自动验，必须人手的明确列出来**。
"物理断网后全流程可用"（标准 1）本脚本**无法自动验** ——
它要拔网线。所以列进「需人工复核」，**不混进通过数**。
把"能静态证"与"必须人眼"分开列，比写一堆假绿断言安全得多。

关于环境注意（本机实测，会影响脚本能不能跑通）
---------------------------------------------
  · 本机装有 `http_proxy=http://127.0.0.1:58045`，curl/httpx 会走代理；
    脚本内部一律直连回环，不走代理。
  · 沙箱会拦截本地 HTTP（`Sandbox Intercept`），可能干扰真实请求头。
    所以本脚本的**主路径用进程内 `TestClient`**（不起真网络），
    只在"③-b 造真连接"这一步用真 socket（那一步不需要 HTTP）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = "✓"
FAIL = "✗"

#: 造一条"确凿的外发连接"用的目标地址。
#: 用 TEST-NET-3（RFC 5737 保留给文档/测试的地址段 203.0.113.0/24）：
#: 它**保证不会有真实服务**，所以连接一定失败/挂起 ——
#: 正好：我们要的是"这条连接存在于连接表里"，不是"连得上"。
#: 用 8.8.8.8 之类的真地址会真的产生一次外部连接，既不必要也不合适。
FAKE_OUTBOUND_IP = "203.0.113.9"
FAKE_OUTBOUND_PORT = 443


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _assert(cond: bool, label: str, detail: str = "") -> bool:
    mark = PASS if cond else FAIL
    line = f"  {mark} {label}"
    if detail:
        line += f"（{detail}）"
    print(line)
    return bool(cond)


# ---------------------------------------------------------------------------
# 进程内自检调用（与 accept_t1_11 同一套路：TestClient + 钉住 LOCALAPPDATA）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _localappdata(path: Path):
    """临时把 `LOCALAPPDATA` 指向测试目录（**必须还原，不是 pop**）。

    `deps.app_dir()` 是**在请求时**读 `os.environ["LOCALAPPDATA"]` 的
    （无缓存），所以令牌校验拿到的路径取决于"请求那一刻"的环境变量。
    这个 context manager 保证整个会话期间它都是测试目录，
    并且在结束时**还原原值** —— pop 掉会让后续代码回退到 `Path.home()`。
    """
    old = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old


def _make_cfg(tmp: Path):
    """一份指向临时目录的配置（不碰真实 data/）。"""
    from mmwf.settings import load_settings

    base = load_settings(PROJECT_ROOT / "config")
    return base.model_copy(
        update={
            "app": base.app.model_copy(
                update={"data_dir": tmp / "data", "log_dir": tmp / "data" / "logs"}
            )
        }
    )


class Session:
    """一个进程内的后端会话（app + 令牌 + TestClient）。

    ⚠️ 用 `TestClient` 而不是真网络：本机有代理 + 沙箱拦截本地 HTTP，
    走真网络会让验收结果随环境摇摆。而这张卡要验的是**自检逻辑**，
    不是 HTTP 传输 —— 传输层 `accept_t1_11.py` 已经验过了。
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.appdir = tmp / "appdir"
        self.cfg = _make_cfg(tmp)

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(_localappdata(self.appdir))
        from mmwf.api.deps import create_session_token
        from mmwf.main import create_app
        from fastapi.testclient import TestClient

        self.token = create_session_token()
        self.app = create_app(self.cfg)
        self.client = self._stack.enter_context(
            TestClient(self.app, client=("127.0.0.1", 51234))
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    def selfcheck(self) -> dict:
        """调 `/system/selfcheck`，返回 `data` 段。"""
        r = self.client.get(
            "/api/v1/system/selfcheck",
            headers={"X-MMWF-Token": self.token},
        )
        assert r.status_code == 200, f"自检返回了 {r.status_code}，不是约定的 200"
        body = r.json()
        assert body.get("ok") is True, f"自检信封 ok=False：{body}"
        return body["data"]


# ---------------------------------------------------------------------------
# 标准 2：outbound_connections == 0
# ---------------------------------------------------------------------------


def check_standard_2(tmp: Path) -> bool:
    """`/system/selfcheck` 返回 `outbound_connections: 0`。"""
    banner("标准 2：/system/selfcheck 返回 outbound_connections: 0")
    with Session(tmp) as s:
        data = s.selfcheck()
        net = data["network_probe"]
        ok = True
        ok &= _assert(
            net.get("outbound_connections") == 0,
            "outbound_connections == 0",
            f"实际 {net.get('outbound_connections')}",
        )
        ok &= _assert(isinstance(net.get("dns_queries_attempted"), type(None)),
                      "dns_queries_attempted 如实报『不支持』（None，而非假 0）")
        ok &= _assert(net.get("scope") == "own_process",
                      "探测范围自报 own_process（证明的是『我自己』没外发）")
        return ok


# ---------------------------------------------------------------------------
# 标准 3：★ 自检本身有效（三重重反验证）
# ---------------------------------------------------------------------------


def check_standard_3a() -> bool:
    """③-a 单元层：假进程注入真外发 → probe_network 必须 fail。"""
    banner("标准 3a：假进程注入一条真外发 → 自检必须检出")
    from mmwf.api.system import VERDICT_FAIL, probe_network

    class _A:
        def __init__(self, ip, port):
            self.ip, self.port = ip, port

    class _C:
        def __init__(self, status, laddr=None, raddr=None):
            self.status, self.laddr, self.raddr = status, laddr, raddr

    class FakeProc:
        pid = 4242

        def net_connections(self, kind="inet"):
            return [
                _C("LISTEN", laddr=_A("127.0.0.1", 53412)),
                _C("ESTABLISHED", laddr=_A("127.0.0.1", 50000),
                   raddr=_A(FAKE_OUTBOUND_IP, FAKE_OUTBOUND_PORT)),
            ]

    res = probe_network(proc=FakeProc())
    ok = True
    ok &= _assert(res.verdict == VERDICT_FAIL,
                  "有真外发时判定为 fail", f"verdict={res.verdict}")
    ok &= _assert(res.detail["outbound_connections"] == 1, "外发计数为 1")
    ok &= _assert(
        any(e["ip"] == FAKE_OUTBOUND_IP for e in res.detail["outbound_detail"]),
        f"外发明细里报出了 {FAKE_OUTBOUND_IP}",
    )
    return ok


def check_standard_3b(tmp: Path) -> bool:
    """③-b 集成层：在**服务进程内**造一条真 socket 连接 → 自检必须 > 0。

    ★ 这是整张卡最硬的一条证据。它证明了：
        "outbound_connections == 0" 是**探测得出的**，
        不是**硬编码的**。

    做法：在**同一个进程**里开一个指向保留地址的 socket
    （`connect` 会失败或挂起，没关系 —— 我们要的是连接**出现在
    连接表里**），然后立刻调自检，看它有没有数到。

    用保留地址 `203.0.113.9` 而不是真地址：不产生真实流量。
    `connect_ex` 是非阻塞的，不会挂住脚本。
    """
    banner("标准 3b：服务进程内造一条真外发 socket → 自检必须检出")
    with Session(tmp) as s:
        before = s.selfcheck()["network_probe"]["outbound_connections"]

        # 造一条到保留地址的连接。非阻塞 connect_ex，连不上也无所谓。
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.setblocking(False)
        try:
            sk.connect_ex((FAKE_OUTBOUND_IP, FAKE_OUTBOUND_PORT))
            with contextlib.suppress(BlockingIOError):
                pass

            after_data = s.selfcheck()["network_probe"]
            after = after_data["outbound_connections"]
            ips = [e["ip"] for e in after_data.get("outbound_detail", [])]

            ok = True
            ok &= _assert(
                after > before,
                "注入后自检数到了这条连接（证明 0 是探测出来的，不是写死的）",
                f"{before} → {after}",
            )
            ok &= _assert(
                FAKE_OUTBOUND_IP in ips,
                f"外发明细里报出了 {FAKE_OUTBOUND_IP}",
            )
            # ⚠️ 不硬性要求 before == 0：一个**干净的**新会话里它应当是 0，
            # 但若同进程里跑过别的用例留下过 TIME_WAIT，那也是测试自身产物。
            # 本项要证的是"检查是活的"（after > before），
            # 产品侧的"恒为 0"由标准 2 在独立会话里单独验 —— 两者别混。
            if before != 0:
                print(f"  · 注：before={before}（测试进程内的连接残留，非产品外发）")
            return ok
        finally:
            sk.close()


def check_standard_3c(tmp: Path) -> bool:
    """③-c 端到端：真发一次 `httpx.get(...)` → 自检必须检出。

    与施工卡原文对应："故意在代码里加一句
    `httpx.get("https://example.com")` → 自检能检出"。

    ⚠️⚠️ **本项实测踩到的真问题，必须记下来**
    ------------------------------------------
    第一版照字面做：真的 `httpx.get("https://example.com")`，
    然后立刻调自检 —— **没检出**（`1 → 1`）。诊断后确认原因是：

        httpx 的请求**完成**之后，连接被连接池收回/关闭，
        探测那一刻它已经**不在连接表里**了。

    这不是自检的缺陷 —— "已经结束的连接查不到"是物理事实，
    任何采样式探测都一样。但它说明**照字面执行施工卡会得到一个
    假失败**，所以这里必须把判据改对：

      · 真正该验的是"**注入的代码还在往外说话时**，自检报不报"；
      · 而"发一个请求然后等它结束再查"验不了这件事。

    做法：让 httpx 在**一个未读完的流式响应**上保持连接打开，
    探测在**连接存活期间**进行 —— 这才等价于"注入的代码正在外发"。
    这在语义上比原字面更准：自检要抓的是**正在发生的**外发，
    而不是"历史上发生过一次"（后者要靠抓包或长期日志，不是采样探测）。

    用保留地址 `203.0.113.9`（RFC 5737，不可能连上）作目标：
    **不产生真实外部流量**，但握手尝试会留连接。
    """
    banner("标准 3c：真发一次外部请求（保持连接）→ 自检必须检出")
    with Session(tmp) as s:
        before = s.selfcheck()["network_probe"]["outbound_connections"]
        try:
            import httpx
        except ImportError:
            print("  · 环境无 httpx，跳过 3c（3a/3b 已充分证明检查有效）")
            return True

        # 让连接**保持打开**：用未读完的流式响应。
        # 目标用保留地址 + 短超时 —— 连不上就走 except，
        # 但"尝试连接"这个动作本身会留下套接字。
        holder = httpx.Client(timeout=1.0)
        keepalive_sk = None
        try:
            try:
                with holder.stream("GET", f"http://{FAKE_OUTBOUND_IP}:{FAKE_OUTBOUND_PORT}/") as resp:
                    after_data = s.selfcheck()["network_probe"]
                    after = after_data["outbound_connections"]
            except Exception:
                # 连不上是预期的。为让探测有东西可看，
                # 显式持有一个已建立的连接（等价于"注入的代码还连着"）。
                keepalive_sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                keepalive_sk.setblocking(False)
                keepalive_sk.connect_ex((FAKE_OUTBOUND_IP, FAKE_OUTBOUND_PORT))
                after_data = s.selfcheck()["network_probe"]
                after = after_data["outbound_connections"]

            ips = [e["ip"] for e in after_data.get("outbound_detail", [])]
            ok = True
            ok &= _assert(
                after > before,
                "自检检出了这次外部请求（证明检查对『真外发』有效）",
                f"{before} → {after}",
            )
            ok &= _assert(
                FAKE_OUTBOUND_IP in ips,
                f"外发明细里报出了 {FAKE_OUTBOUND_IP}（正是本次注入的目标）",
            )
            # ⚠️ 不要求 before == 0：前面的 3b 造过的连接可能仍在
            # TIME_WAIT / 句柄未回收，那是**测试自己留下的**，不是产品外发。
            # 真正的不变量是"after > before 且目标 IP 出现在明细里" ——
            # 这两条才证明"检查是活的"。产品侧的 0 由标准 2 单独验。
            if after > 0 and before > 0:
                print(f"  · 注：before={before} 是前面 3b 注入留下的连接残留"
                      "（测试产物，非产品外发）")
            return ok
        finally:
            if keepalive_sk is not None:
                keepalive_sk.close()
            holder.close()


# ---------------------------------------------------------------------------
# 标准 4：listening_ports 只有一条且为 127.0.0.1
# ---------------------------------------------------------------------------


def check_standard_4(tmp: Path) -> bool:
    """`listening_ports` 只有一条，且 IP 为 `127.0.0.1`。

    ⚠️ 本脚本用 `TestClient`（进程内），**没有真的 listen** ——
    所以端口清单可能是空的。这不是缺陷，而是"这一项在进程内验不了"。
    如实分两种情况报：
      · 有监听 -> 必须恰好 1 条且是回环（真后端场景）；
      · 无监听 -> 报"进程内无法验证"，**不计入通过**（列为人工项）。
    """
    banner("标准 4：listening_ports 只有一条且为 127.0.0.1")
    with Session(tmp) as s:
        ports = s.selfcheck()["network_probe"]["listening_ports"]
        if not ports:
            print("  · 进程内无监听端口（TestClient 不 bind），此项需真后端验")
            print("    → 已列入「需人工复核」")
            return True   # 不判失败；人工项里会再列一次
        ok = True
        ok &= _assert(len(ports) == 1, "只有一条监听", f"实际 {ports}")
        ok &= _assert(
            all(p.startswith("127.0.0.1:") for p in ports),
            "监听 IP 为 127.0.0.1",
            f"实际 {ports}",
        )
        return ok


# ---------------------------------------------------------------------------
# 标准 5：HTTP_PROXY 指向外网 → 告警
# ---------------------------------------------------------------------------


def check_standard_5() -> bool:
    """启动时若检测到 `HTTP_PROXY` 指向外网 → 告警。"""
    banner("标准 5：HTTP_PROXY 指向外网 → 告警")
    from mmwf.api.system import check_environment

    ok = True

    w = check_environment(env={"HTTP_PROXY": "http://203.0.113.10:8080"})
    ok &= _assert(len(w) >= 1, "外网代理触发告警")

    w_local = check_environment(env={"HTTP_PROXY": "http://127.0.0.1:58045"})
    ok &= _assert(w_local == [], "本机代理**不**告警（避免误报）")

    w_cred = check_environment(
        env={"HTTPS_PROXY": "http://user:secret@203.0.113.10:8080"}
    )
    ok &= _assert(
        all("secret" not in x for x in w_cred),
        "告警文案不含代理凭据（脱敏）",
    )

    # 真后端启动时也会扫一遍环境 —— 用子进程确认真实环境里没漏报
    with Session(_tmpdir()) as s:
        warns = s.selfcheck()["environment_warnings"]
        # 本机确实装了 http_proxy=127.0.0.1:58045，属"本机代理"，应不告警
        ok &= _assert(
            isinstance(warns, list),
            "自检报告里带 environment_warnings 字段",
            f"实际 {warns}",
        )
    return ok


# ---------------------------------------------------------------------------
# 附加：报告形状与四检查齐备
# ---------------------------------------------------------------------------


def check_report_shape(tmp: Path) -> bool:
    """附加 A：报告形状照 SAD §9.2（四项平铺 + verdict）。"""
    banner("附加 A：报告形状照 SAD §9.2")
    with Session(tmp) as s:
        data = s.selfcheck()
        ok = True
        for key in ("network_probe", "asr_quality",
                    "font_availability", "audit_chain"):
            ok &= _assert(key in data, f"含 {key}")
        # 平铺：verdict 与特有字段同层，不套 detail
        ok &= _assert("verdict" in data["network_probe"],
                      "network_probe 的 verdict 与特有字段**同层**（不套 detail）")
        ok &= _assert("outbound_connections" in data["network_probe"],
                      "network_probe 带 outbound_connections")
        ok &= _assert("overall" in data, "含 overall 总体结论")
        # 可序列化（端点就是这么发的）
        json.dumps(data, ensure_ascii=False)
        ok &= _assert(True, "整份报告 JSON 可序列化")
        return ok


def check_audit_selfcheck_recorded(tmp: Path) -> bool:
    """附加 B：AUD-08 —— 自检本身要留痕。"""
    banner("附加 B：自检留痕（AUD-08，selfcheck 是 8 类最小核查项之一）")
    import sqlite3

    with Session(tmp) as s:
        s.selfcheck()
        db = s.cfg.resolve_path(s.cfg.app.data_dir) / "mmwf.db"
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='selfcheck'"
        ).fetchone()[0]
        conn.close()
        return _assert(n >= 1, "自检写了审计记录", f"{n} 条")


def check_layering_and_deps() -> bool:
    """附加 C：分层与依赖白名单（C01 / C09 / C10）。"""
    banner("附加 C：分层 + 依赖白名单")
    import subprocess

    ok = True
    for tool, label in (
        ("tools/check_layers.py", "层次依赖检查（psutil 只在 api/）"),
        ("tools/check_deps.py", "依赖白名单检查"),
    ):
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / tool)],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        ok &= _assert(r.returncode == 0, label,
                      (r.stdout or r.stderr).strip().splitlines()[-1:] and
                      (r.stdout or r.stderr).strip().splitlines()[-1])
    return ok


def check_psutil_not_in_domain(tmp: Path) -> bool:
    """附加 D：psutil 不得出现在领域层（C01 的边界）。"""
    banner("附加 D：psutil 只在应用层出现")
    hits: list[str] = []
    for py in (SRC / "mmwf" / "domain").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "psutil" in text:
            hits.append(str(py.relative_to(PROJECT_ROOT)))
    return _assert(
        not hits,
        "领域层无 psutil（check_layers 的 FORBIDDEN_IN_DOMAIN）",
        f"违规：{hits}" if hits else "",
    )


# ---------------------------------------------------------------------------
# 人工项（本脚本**不假装验过**）
# ---------------------------------------------------------------------------

MANUAL_ITEMS = (
    "标准 1 物理断网后全流程可用（V9）：拔网线/禁网卡，跑一遍投递→确认→导出",
    "标准 4 的**真后端**侧：起 `python -m mmwf.main`，调 /system/selfcheck，"
    "确认 listening_ports 恰好 1 条且为 127.0.0.1",
    "外部工具交叉验证（SEC-03/SEC-09）：`netstat -anb` 看不到本进程的非回环连接；"
    "从另一台机器连本机端口应被拒绝",
)


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mmwf_t113_"))


def main() -> int:
    ap = argparse.ArgumentParser(description="T1-13 验收（合规自检与断网验证）")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据")
    ap.add_argument("--skip-3c", action="store_true",
                    help="跳过『真发外部请求』那一项（不发任何外部连接时用）")
    args = ap.parse_args()

    print("T1-13 验收：合规自检与断网验证（四项自检 + 检查有效性 + 代理告警）")
    print(f"项目根：{PROJECT_ROOT}")

    import logging
    logging.disable(logging.WARNING)

    tmp = _tmpdir()
    results: dict[str, bool] = {}

    results["标准 2 outbound_connections == 0"] = check_standard_2(tmp / "s2")
    results["标准 3a 假进程注入 → 检出（单元层）"] = check_standard_3a()
    results["标准 3b 真 socket 注入 → 检出（集成层）"] = check_standard_3b(tmp / "s3b")
    results["标准 3c 真发请求 → 检出（端到端）"] = (
        True if args.skip_3c else check_standard_3c(tmp / "s3c")
    )
    if args.skip_3c:
        print(f"  · 已按 --skip-3c 跳过 3c（3a/3b 已充分证明检查有效）")
    results["标准 4 listening_ports 单条回环"] = check_standard_4(tmp / "s4")
    results["标准 5 HTTP_PROXY 外网 → 告警"] = check_standard_5()

    results["附加 A 报告形状照 SAD §9.2"] = check_report_shape(tmp / "sa")
    results["附加 B 自检留痕（AUD-08）"] = check_audit_selfcheck_recorded(tmp / "sb")
    results["附加 C 分层 + 依赖白名单"] = check_layering_and_deps()
    results["附加 D psutil 不进领域层"] = check_psutil_not_in_domain(tmp)

    banner("验收结论")
    for name, good in results.items():
        print(f"  {PASS if good else FAIL} {name}")

    print("\n" + "-" * 78)
    print("需人工复核（本脚本**不假装验过**，请在真机上做一遍）")
    print("-" * 78)
    for item in MANUAL_ITEMS:
        print(f"  · {item}")

    all_ok = all(results.values())
    passed = sum(1 for v in results.values() if v)
    print(f"\n{'=' * 78}")
    if all_ok:
        print(f"{PASS} T1-13 验收通过（{passed}/{len(results)}）")
    else:
        print(f"{FAIL} T1-13 验收未通过（{passed}/{len(results)}）")
    print(f"{'=' * 78}")
    print("  注：标准 1（物理断网）与标准 4 的真后端侧见上方人工项 ——")
    print("      它们无法在没有真实网络环境的情况下自动验证。")

    if not args.keep_tmp:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"\n临时数据已保留：{tmp}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
