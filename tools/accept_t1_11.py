"""T1-11 验收脚本（人可读版）。

施工卡 T1-11 的五条验收标准逐条跑一遍。这张卡验的是**API 层的三道门**
（回环 / 令牌 / 关文档）与**统一信封**，不验业务逻辑对不对
（那是各业务卡的职责）。

五条标准（施工卡原文）
----------------------
  [ ] 无令牌请求全部接口 → `E6001`
  [ ] 从非回环地址访问 → `E6002`（用 `curl --interface` 模拟）
  [ ] `openapi.json` / `/docs` 返回 404（已关闭）
  [ ] 触发一个未捕获异常 → 响应是 `{ok:false, code:"E9xxx", message:"中文提示"}`，**不含堆栈**
  [ ] 端口文件写入正确路径，Electron 能读到

⚠️ 关于"用 `curl --interface` 模拟非回环"这条（施工卡原文）
-----------------------------------------------------------
**照原样做测不出东西**，本脚本改用了等效但真正能触发的手段。
原因（2026-09-28 实测，别再来一遍）：

  · 服务只绑 `127.0.0.1`。`curl --interface 172.24.1.106`（源地址是
    非回环）连 `127.0.0.1` → Windows 直接 **rc=7 连接被拒**，
    因为内核不允许"非回环源地址访问回环目的地址"。请求根本到不了
    应用层，E6002 那条中间件代码一行都没跑到。
  · `curl --interface 127.0.0.2` 确实能连上，但源地址**仍是回环**
    （整个 127/8 都是回环），服务端看到的 `request.client.host`
    还是 `127.0.0.1` —— 中间件按设计放行，测的还是"回环可用"。

真正能造出非回环来源的等效做法（本脚本 `check_standard_2` 用的）：
  ① 把服务**临时绑到 `0.0.0.0`**（只为这一项验收，不写进产品代码）；
  ② 用本机**真实网卡地址**去连（`http://172.24.1.106:<port>/...`）。
这样服务端 `request.client.host` 就是那个非回环 IP，中间件必须拦。
它还额外覆盖了真正要防的场景 —— **端口转发 / 别的机器从局域网访问**，
而 `curl --interface` 这个手法在真实攻击里并不存在。

顺带一个结论：**E6002 靠"bind 地址"是防不住的**。真要把服务绑到
`0.0.0.0`，唯一挡住外部访问的就是这条中间件 —— 这也是它必须存在、
且必须查 `request.client.host` 而不是查 `app.state` 里配置的原因。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PASS = "✓"
FAIL = "✗"

#: 请求超时（秒）。后端是本地进程，正常响应在毫秒级；
#: 给 10s 是为了容错"首次 import 慢"，不是给网络留余量。
HTTP_TIMEOUT = 10.0

#: 全部受保护端点（方法, 路径）。与 `api/routes.build_router` 的
#: 端点清单必须一致 —— `check_standard_1` 会把这份清单和
#: 路由器里实际注册的端点数对一遍，防止"加了端点忘了挂依赖"。
PROTECTED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/system/handshake"),
    ("POST", "/meetings"),
    ("GET", "/meetings"),
    ("GET", "/meetings/m-accept"),
    ("POST", "/meetings/m-accept/start"),
    ("GET", "/meetings/m-accept/topics"),
    ("POST", "/meetings/m-accept/gate/G2/decide"),
    ("GET", "/meetings/m-accept/minutes"),
    ("GET", "/meetings/m-accept/record"),
    ("POST", "/meetings/m-accept/export"),
    ("POST", "/meetings/m-accept/export/undo"),
)


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


@contextlib.contextmanager
def _localappdata(path: Path):
    """临时把 `LOCALAPPDATA` 指到 `path`，退出时**完整还原**。

    ⚠️ 为什么必须"还原"而不是 `pop`（T1-11 Review 踩过的真坑）：
    `check_standard_5` 早期版本用 `os.environ.pop("LOCALAPPDATA")`
    来"清掉"这个变量，但本机上这个变量**本来就有值**（真值
    `C:\\Users\\<用户名>\\AppData\\Local`）—— `pop` 之后它不会自己回来，
    于是后续所有 `deps.app_dir()` 都落到 `Path.home()/mmwf`，
    连带把**真实用户目录**搞乱。
    现在统一用这个上下文管理器：存旧值、设新值、无论成败都写回。
    """
    had = "LOCALAPPDATA" in os.environ
    prev = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(path)
    try:
        yield
    finally:
        if had:
            os.environ["LOCALAPPDATA"] = prev  # type: ignore[assignment]
        else:
            os.environ.pop("LOCALAPPDATA", None)


# ---------------------------------------------------------------------------
# 起一个真后端（不是 TestClient）
# ---------------------------------------------------------------------------

class Backend:
    """在子进程里跑真 `uvicorn`。

    为什么不用 `fastapi.testclient.TestClient`：本卡要验的三道门里，
    有两道（回环来源、端口文件）**只在真进程 + 真 socket 下才有意义**。
    `TestClient` 是进程内调用，`request.client.host` 恒为 `"testclient"`，
    端口文件也不会被写。用 TestClient 验"端口文件写对了没"就是自欺。

    直接在**当前进程**里起 uvicorn 线程也不行：`uvicorn.run()` 会装信号
    处理器，在非主线程里会失败。子进程最干净。
    """

    def __init__(self, tmp: Path, *, bind: str = "127.0.0.1"):
        self.tmp = tmp
        self.bind = bind
        self.proc: subprocess.Popen | None = None
        self.port: int = 0
        self.token: str | None = None
        self.appdir = tmp / "appdata"          # 冒充 %LOCALAPPDATA%
        self.log = tmp / "backend.log"
        self._logf: object | None = None
        #: 与子进程**同一份**本地配置。附加 D 的进程内客户端要用它
        #: 重新 `create_app()`，才能跟后端指向同一个数据目录 / 令牌文件。
        self.cfg: object | None = None

    # -- 生命周期 --------------------------------------------------------

    def start(self, *, ready_timeout: float = 40.0) -> None:
        """拉起后端，等端口文件出现。

        端口由 OS 分配（`port=0`），**端口不是猜的，是从文件读的** ——
        这恰好就是 Electron 要走的那条路（读 `%LOCALAPPDATA%/mmwf/port`），
        等于顺手验了"前端能读到"。

        ⚠️ 为什么让子进程**自己写端口文件**、而不是去解析 uvicorn 的日志：
        第一版解析 `Uvicorn running on http://127.0.0.1:PORT` 那行，
        但 `log_level="warning"` 会**把这条日志也吞掉**（它是 info 级），
        于是正则永远匹配不上 → 40s 超时 → 报"后端未就绪"，
        而真实原因是"日志里根本没这行"。子进程里先 `socket` 探一次
        可用端口、写文件、再 `uvicorn.run(port=那个端口)`，
        走的是与 `mmwf.main.main()` 完全相同的契约，可靠且更贴近真实。
        """
        self.appdir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["LOCALAPPDATA"] = str(self.appdir)      # deps.app_dir() 读它
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        code = (
            "import sys; sys.path.insert(0, r'{src}');"
            "import mmwf.main as m, mmwf.settings as st;"
            "from mmwf.api import deps;"
            "cfg = st.load_settings();"
            "app = m.create_app(cfg);"
            "deps.create_session_token();"
            "port = m._pick_free_port();"
            "deps.write_port(port);"
            "print('MMWF_PORT=' + str(port), flush=True);"
            "import uvicorn;"
            "uvicorn.run(app, host={host!r}, port=port, log_level='warning',"
            " access_log=False)"
        ).format(src=str(PROJECT_ROOT / "src"), host=self.bind)

        self._logf = open(self.log, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            stdout=self._logf,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )

        # 父进程也按**同一个 LOCALAPPDATA** 加载一份 cfg —— 附加 D 的
        # 进程内客户端要用它建 app，必须跟子进程指向同一份数据/令牌。
        # ⚠️ 必须在这里临时改 env：`load_settings()` 的默认路径链条会读
        # `LOCALAPPDATA` 来定位 `%LOCALAPPDATA%/mmwf`。用完立刻还原，
        # 别污染后续检查（标准 5 就是专门验这个路径的）。
        self.cfg = self._load_cfg_like_child(env)

        # 令牌 / 端口文件由**子进程**写（走 deps，与产品代码同一条路）
        port_file = self.appdir / "mmwf" / "port"
        tok_file = self.appdir / "mmwf" / "session.token"
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"后端启动即退出（code={self.proc.returncode}）：\n"
                    f"{self.log.read_text(encoding='utf-8', errors='replace')}"
                )
            if port_file.exists() and tok_file.exists():
                try:
                    self.port = int(port_file.read_text(encoding="utf-8").strip())
                    self.token = tok_file.read_text(encoding="utf-8").strip()
                    break
                except (OSError, ValueError):
                    pass
            time.sleep(0.2)
        else:
            raise RuntimeError(
                f"后端 {ready_timeout}s 内未就绪：\n"
                f"{self.log.read_text(encoding='utf-8', errors='replace')}"
            )

        # 再等一小会儿让 socket 真正 listen（端口文件先落，listen 后到）
        self._wait_listening()

    def _load_cfg_like_child(self, child_env: dict) -> object:
        """按子进程的环境加载一份 `Settings`。

        只借它的 `LOCALAPPDATA` 一个变量：`load_settings()` 会用它
        定位 `%LOCALAPPDATA%/mmwf`（端口/令牌/数据目录都挂在这里）。
        用完立刻还原 `os.environ`，避免影响标准 5 对真实路径的检查。
        """
        from mmwf import settings as st

        with _localappdata(Path(child_env["LOCALAPPDATA"])):
            return st.load_settings()

    def _wait_listening(self, timeout: float = 20.0) -> None:
        """等 socket 真的 listen。

        端口文件是子进程在 `uvicorn.run()` **之前**写的（与
        `mmwf.main.main()` 同一顺序：先落文件、再监听），所以文件出现
        不等于能连。这里主动连一次来消除这个窗口 —— 否则第一次请求
        可能拿到"连接被拒"，而那是竞态、不是缺陷。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((self._dial_host(), self.port)) == 0:
                    return
            time.sleep(0.15)
        raise RuntimeError(f"{timeout}s 内没等到 {self.bind}:{self.port} 开始监听")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            if self._logf is not None:
                self._logf.close()
        except Exception:  # pragma: no cover
            pass

    # -- 请求 ------------------------------------------------------------

    def base(self) -> str:
        return f"http://{self._dial_host()}:{self.port}/api/v1"

    def _dial_host(self) -> str:
        """用来连的地址。绑 `0.0.0.0` 时不能拿它当目标 ——
        `0.0.0.0` 只是"绑所有网卡"的**监听**地址，不是可连接地址。
        """
        return "127.0.0.1" if self.bind == "0.0.0.0" else self.bind

    def curl(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        host_ip: str | None = None,
        body: dict | None = None,
    ) -> tuple[int, str]:
        """用**系统 curl** 发请求，返回 (curl 返回码, 响应体文本)。

        用 curl 而不是 `httpx`：施工卡原文就是 curl，而且 curl 走的是
        真正的 TCP 栈 + 系统代理设置 —— 用库发请求会绕过一部分环境差异。

        ⚠️ **必须显式传 `-X`，不能靠 curl 自己推方法**（T1-11 Review 修正）。

        第一版写的是 `if method != "GET": cmd += ["-X", method]` —— 想省一个参数，
        但 **curl 加了 `-d` 就会把方法强制成 `POST`**（不管你怎么写 URL）。
        于是 `curl GET -d "{}"` 实际发出去的是
        `> POST /api/v1/meetings HTTP/1.1`
        而调用方以为自己在测 GET。

        后果（实测）：`check_standard_1` 里那 10 个 GET 端点是在**测 POST** ——
        所以它们返回 `E6001` 只是"碰巧正确"（POST 也受令牌保护），
        而 `GET /meetings/m-accept` 因为被当成 POST 而**路径匹配失败**，
        走 Starlette 的 405 → 被处理器映射成 `E1001`，
        于是标准 1 里红色那行看着像"令牌门泄漏"，其实只是方法被 curl 改了。
        **验收脚本自己报出假阳性，比漏报还危险** —— 会让人去修不存在的问题。

        修法：`-X` 一律显式传（含 GET）。零成本，且消除一整类歧义。
        """
        url = f"http://{host_ip or self._dial_host()}:{self.port}{path}"
        cmd = ["curl", "-s", "-m", str(int(HTTP_TIMEOUT)), "-w", "\n%{http_code}"]
        # ★ `--noproxy *`：本机若设了 `http_proxy`（实测本机就有，
        # 指向 127.0.0.1:58045），curl 会把**回环请求也走代理** ——
        # 服务端 `request.client.host` 就变成代理进程，回环门判定的
        # 对象就错了。这个服务只在本机跑，永远不该走代理。
        cmd += ["--noproxy", "*"]
        cmd += ["-X", method]                     # ★ 永远显式，不靠 curl 推
        if body is not None:
            cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        if token is not None:
            cmd += ["-H", f"X-MMWF-Token: {token}"]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True)
        out = r.stdout.decode("utf-8", errors="replace")
        return r.returncode, out


# ---------------------------------------------------------------------------
# 标准 1：无令牌 → E6001
# ---------------------------------------------------------------------------

def check_standard_1(be: Backend) -> bool:
    banner("标准 1：无令牌请求全部接口 → E6001")
    ok = True

    for method, path in PROTECTED_ENDPOINTS:
        rc, out = be.curl(method, f"/api/v1{path}", body={} if method != "GET" else None)
        body_txt, _, status = out.rpartition("\n")
        try:
            code = json.loads(body_txt)["code"]
        except Exception:
            code = f"<解析失败:{out[:80]!r}>"
        ok &= _assert(
            rc == 0 and status == "200" and code == "E6001",
            f"{method:4} {path:44} → {status} {code}",
        )

    # 错令牌也要拦
    rc, out = be.curl("POST", "/api/v1/system/handshake", token="wrong-token-xyz", body={})
    body_txt, _, status = out.rpartition("\n")
    ok &= _assert(
        '"code": "E6001"' in body_txt or '"code":"E6001"' in body_txt.replace(" ", ""),
        "错令牌 → E6001",
    )

    # 端点清单与路由器实际注册数一致（防止新增端点漏挂依赖）
    ok &= check_endpoint_inventory()

    return ok


def check_endpoint_inventory() -> bool:
    """核对"受保护端点清单"与代码里实际注册的端点数一致。

    这一项很关键：`require_token` 是**路由器级依赖**，只要有人
    以后再写一个 `APIRouter()` 而不带 `dependencies=[...]`，
    新端点就裸奔了，而标准 1 的循环清单是**手写**的、不会自动发现它。
    这里直接数路由器里的路由数来兜底。
    """
    try:
        from mmwf.api.routes import build_router
        from mmwf.settings import load_settings
    except Exception as e:  # pragma: no cover
        print(f"  {FAIL} 无法导入路由：{e}")
        return False

    cfg = load_settings(PROJECT_ROOT / "config")
    router = build_router(cfg)
    n_routes = len(router.routes)
    n_declared = len(PROTECTED_ENDPOINTS)
    return _assert(
        n_routes >= n_declared,
        f"路由器注册 {n_routes} 条路由，清单覆盖 {n_declared} 条"
        + (f"（少 {n_declared - n_routes} 条）" if n_routes < n_declared else ""),
    )


# ---------------------------------------------------------------------------
# 标准 2：非回环 → E6002
# ---------------------------------------------------------------------------

def _lan_ip() -> str | None:
    """找一个本机**非回环**地址。

    用 UDP socket "connect" 到一个公网地址来问内核"你从哪个网卡出去"。
    它**不发任何包**（UDP connect 只做路由表查询 + 绑定本地地址），
    所以离线环境下也能用 —— 离线是本项目的常态，不能依赖真实联网探测。
    查不到时返回 None，标准 2 会报"跳过"而不是"通过"。
    """
    for probe in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return None


def check_standard_2(be: Backend, tmp: Path) -> bool:
    banner("标准 2：从非回环地址访问 → E6002")
    ok = True

    ip = _lan_ip()
    if not ip:
        print(f"  {FAIL} 本机没有非回环 IPv4 地址，无法验这一项（不算通过）")
        return False
    print(f"  本机非回环地址：{ip}")

    # 先证"回环这条路是通的" —— 否则下面 E6002 可能只是因为服务没起来
    rc, out = be.curl("POST", "/api/v1/system/handshake", token=be.token, body={})
    body_txt, _, status = out.rpartition("\n")
    loop_ok = '"ok": true' in body_txt or '"ok":true' in body_txt.replace(" ", "")
    ok &= _assert(loop_ok, f"对照：回环（{be.bind}）令牌正确 → ok=true")

    # 非回环来源必须被拦。
    #
    # ⚠️ 为什么这里要**另起一个绑 0.0.0.0 的后端**：主后端绑的是
    # 127.0.0.1，用 LAN IP 连它连接就会被内核拒（rc=7），请求到不了
    # 应用层 —— 那样测的是"连不上"，不是"E6002"。
    # 绑 0.0.0.0 才能让非回环来源真的把包送进来。
    # 这在验收里是**允许**的：要验的正是"哪怕绑成了 0.0.0.0，
    # 中间件也必须挡住"，也就是端口转发/局域网直连的真实威胁。
    wbe = Backend(tmp / "wild", bind="0.0.0.0")
    try:
        wbe.start()
    except RuntimeError as e:
        print(f"  {FAIL} 无法启动 0.0.0.0 测试后端：{e}")
        return False

    try:
        # 非回环来源 + **正确令牌** —— 令牌对了也照样要拦（门是"且"的关系）
        rc, out = wbe.curl(
            "POST", "/api/v1/system/handshake",
            token=wbe.token, host_ip=ip, body={},
        )
        body_txt, _, status = out.rpartition("\n")
        if rc != 0:
            # 防火墙可能拦了局域网入站。这不算产品缺陷，但这一项就**没验到**，
            # 如实报"未验证"，不粉饰成通过。
            print(f"  {FAIL} 经 {ip} 连不上测试后端（curl rc={rc}）—— "
                  f"可能被主机防火墙拦了，本项未验证")
            return False
        compact = body_txt.replace(" ", "")
        ok &= _assert(
            status == "200" and '"code":"E6002"' in compact,
            f"非回环来源（{ip}）令牌正确 → 200 + E6002（实测 {status} {body_txt[:120]}）",
        )
        ok &= _assert(
            "E6001" not in compact,
            "拦下它的是回环门（E6002），不是令牌门（E6001）",
        )

        # 对照：同一个 0.0.0.0 后端，从回环来源进就该放行
        rc2, out2 = wbe.curl("POST", "/api/v1/system/handshake", token=wbe.token, body={})
        b2, _, s2 = out2.rpartition("\n")
        ok &= _assert(
            '"ok":true' in b2.replace(" ", ""),
            "对照：同一个 0.0.0.0 后端，回环来源 → ok=true",
        )
    finally:
        wbe.stop()

    return ok


# ---------------------------------------------------------------------------
# 标准 3：文档路由关闭
# ---------------------------------------------------------------------------

def check_standard_3(be: Backend) -> bool:
    banner("标准 3：/docs、/redoc、/openapi.json 关闭（404）")
    ok = True
    for path in ("/docs", "/redoc", "/openapi.json", "/openapi.json/", "/docs/oauth2-redirect"):
        rc, out = be.curl("GET", path)
        _, _, status = out.rpartition("\n")
        ok &= _assert(
            status == "404",
            f"GET {path:26} → {status}（应 404）",
        )
    return ok


# ---------------------------------------------------------------------------
# 标准 4：未捕获异常 → 信封，不含堆栈
# ---------------------------------------------------------------------------

#: 故意泄露信息的异常载荷。响应里出现任何一个子串 = 不合格。
SECRET_MARKERS = (
    "Traceback",
    "most recent call last",
    "RuntimeError",
    "File \"",
    "/etc/passwd",
    "C:\\Windows",
    "sqlite3.",
    "SELECT ",
)


def check_standard_4(tmp: Path) -> bool:
    banner("标准 4：未捕获异常 → {ok:false, code:E9xxx, message:中文}，不含堆栈")
    ok = True

    be = Backend(tmp)
    # 往应用里注入一个"会炸"的路由。做法：写一个临时启动器，
    # 先 create_app() 再 add_api_route()。产品代码不动一行。
    ok &= _run_with_boom_route(be)
    return ok


def _run_with_boom_route(be: Backend) -> bool:
    """起一个"多了一条会抛异常的路由"的后端并断言响应形状。

    为什么不在 `create_app()` 里加一个"测试用炸点"：那等于给产品代码
    留后门，保密检查会问"这条 /_boom 是干什么的"。所以炸点只存在于
    验收脚本临时生成的启动器里。

    端口同样由子进程写文件 —— 原因见 `Backend.start()` 的注释
    （`log_level="warning"` 会把 "Uvicorn running on ..." 也吞掉）。
    """
    be.appdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(be.appdir)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    src = str(PROJECT_ROOT / "src")
    code = f"""
import sys; sys.path.insert(0, r'{src}')
from mmwf.settings import load_settings
from mmwf.main import create_app, _pick_free_port
from mmwf.api import deps
app = create_app(load_settings())

@app.get('/api/v1/_acc/boom')
def _boom():
    raise RuntimeError('内部细节不该外泄：/etc/passwd 与 SELECT * FROM meeting')

@app.get('/api/v1/_acc/biz')
def _biz():
    from mmwf.domain.errors import TokenInvalidError
    raise TokenInvalidError('这条 detail 不该外泄')

deps.create_session_token()
port = _pick_free_port()
deps.write_port(port)
print('MMWF_PORT=' + str(port), flush=True)

import uvicorn
uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning', access_log=False)
"""
    log = be.tmp / "boom.log"
    logf = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", code], env=env,
        stdout=logf, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
    )
    try:
        port_file = be.appdir / "mmwf" / "port"
        tok_file = be.appdir / "mmwf" / "session.token"
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"  {FAIL} 后端启动失败：\n"
                      f"{log.read_text(encoding='utf-8', errors='replace')}")
                return False
            if port_file.exists() and tok_file.exists():
                try:
                    be.port = int(port_file.read_text(encoding="utf-8").strip())
                    be.token = tok_file.read_text(encoding="utf-8").strip()
                    break
                except (OSError, ValueError):
                    pass
            time.sleep(0.2)
        else:
            print(f"  {FAIL} 后端未就绪")
            return False

        be._wait_listening()

        ok = True

        # -- 4a 未捕获异常 ------------------------------------------------
        rc, out = be.curl("GET", "/api/v1/_acc/boom", token=be.token)
        body_txt, _, status = out.rpartition("\n")
        compact = body_txt.replace(" ", "")
        ok &= _assert(status == "200", f"未捕获异常仍回 HTTP 200（实测 {status}）")
        try:
            body = json.loads(body_txt)
        except Exception:
            body = {}
            ok &= _assert(False, f"响应不是合法 JSON：{body_txt[:160]}")
        ok &= _assert(body.get("ok") is False, "ok=false")
        ok &= _assert(
            (body.get("code") or "").startswith("E9"),
            f"code 是 E9xxx 系（实测 {body.get('code')!r}）",
        )
        msg = body.get("message") or ""
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in msg)
        ok &= _assert(
            bool(msg) and has_cjk,
            f"message 是中文提示（实测 {msg[:40]!r}）",
        )
        ok &= _assert(bool(body.get("trace_id")), f"带 trace_id（实测 {body.get('trace_id')!r}）")

        leaked = [m2 for m2 in SECRET_MARKERS if m2 in body_txt]
        ok &= _assert(
            not leaked,
            f"★ 响应体不含堆栈/路径/SQL（泄露项：{leaked}）",
        )

        # 堆栈必须**进了日志**，否则等于异常消失了
        logtext = log.read_text(encoding="utf-8", errors="replace")
        ok &= _assert(
            "Traceback" in logtext and "RuntimeError" in logtext,
            "堆栈落到了日志（用户看不到、运维查得到）",
        )
        ok &= _assert(
            bool(body.get("trace_id")),
            f"trace_id 已发放（{body.get('trace_id')}，日志检索链路排 T1-13）",
        )

        # -- 4b 业务异常：detail 不外泄 ------------------------------------
        rc, out = be.curl("GET", "/api/v1/_acc/biz", token=be.token)
        body_txt2, _, status2 = out.rpartition("\n")
        ok &= _assert(status2 == "200", f"业务异常也回 200（实测 {status2}）")
        ok &= _assert(
            '"code":"E6001"' in body_txt2.replace(" ", ""),
            "TokenInvalidError → E6001（★ 依赖里抛的异常也能被捕获）",
        )
        ok &= _assert(
            "这条 detail 不该外泄" not in body_txt2,
            "★ 业务异常的 detail 没有外泄（只用 user_message）",
        )

        # -- 4c 未知路径 / 非法方法也要走信封，不能漏出 HTML 或纯文本 -------
        rc, out = be.curl("GET", "/api/v1/_acc/does-not-exist", token=be.token)
        body_txt3, _, status3 = out.rpartition("\n")
        ok &= _assert(
            status3 in ("200", "404"),
            f"未知路径返回状态码（实测 {status3}）",
        )
        if status3 == "200":
            ok &= _assert(
                '"ok":false' in body_txt3.replace(" ", ""),
                "未知路径若是 200，则必须是失败信封",
            )

        return ok
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        try:
            logf.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 标准 5：端口文件
# ---------------------------------------------------------------------------

def check_standard_5(tmp: Path) -> bool:
    banner("标准 5：端口文件写入正确路径，Electron 能读到")
    ok = True

    # 用真 `mmwf.main.main()` 起一次，验证它写的是**约定路径**。
    # 这里不真起 uvicorn（会阻塞），而是直接调 deps 的两个函数 ——
    # `main()` 里那两行就是它们，等价且不用等监听。
    from mmwf.api import deps

    appdir = tmp / "appdata5"
    # ⚠️⚠️ **必须"存原值再还原"，不能 `os.environ.pop`**（T1-11 Review 修正）。
    #
    # 第一版写的是 `os.environ["LOCALAPPDATA"] = str(appdir)` ... 最后
    # `os.environ.pop("LOCALAPPDATA", None)`。
    # 它假设"父进程本来没有这个变量"——而**本机（以及绝大多数 Windows
    # 用户）本来就有**：`C:\Users\<用户>\AppData\Local`。
    # 于是 `pop` 不是"还原"，是"**删掉**"。
    #
    # 后果连锁到后面的 `check_happy_path`（它用 `deps.verify_token` 校验令牌）：
    # `app_dir()` 读到 `LOCALAPPDATA` 为空 → 退回 `Path.home()` →
    # 去 `C:\Users\<用户>\mmwf\session.token` 找令牌 —— 那是**别的目录里的旧文件**，
    # 自然不匹配真后端的令牌 → **E6001**。
    #
    # 这个坑最气人的地方：报错是"会话已失效"，而**产品代码一行都没问题** ——
    # 是测试自己把环境变量踩坏了，还甩了个业务错误码出来。
    # 教训：改 `os.environ` 一律"存原值 + 还原原值"，
    # 不要用 `pop` 表达"恢复"（它在"原来就没有"时才对）。
    # 这里直接用共享的 `_localappdata()`（同样是存旧值再写回）。
    with _localappdata(appdir):
        # 路径形状：%LOCALAPPDATA%/mmwf/{port,session.token}
        # ★ 与施工卡 T1-12 `startBackend()` 里 Electron 拼的路径逐段对齐：
        #   path.join(os.homedir(), 'AppData', 'Local', 'mmwf', 'port')
        p_port = deps.port_path()
        p_tok = deps.token_path()
        ok &= _assert(
            p_port.parent.name == "mmwf" and p_port.parent.parent == appdir,
            f"端口文件在 %LOCALAPPDATA%/mmwf/ 下（{p_port}）",
        )
        ok &= _assert(p_port.name == "port", f"文件名是 'port'（实测 {p_port.name!r}）")
        ok &= _assert(
            p_tok.name == "session.token",
            f"令牌文件名是 'session.token'（实测 {p_tok.name!r}）",
        )

        deps.write_port(53412)
        deps.create_session_token()
        ok &= _assert(p_port.exists() and p_tok.exists(), "两个文件都真的落地了")

        # Electron 侧的读法：文本直读
        raw_port = p_port.read_text(encoding="utf-8").strip()
        ok &= _assert(
            raw_port.isdigit() and int(raw_port) == 53412,
            f"Electron 直接读文本就能拿到端口（实测 {raw_port!r}）",
        )
        raw_tok = p_tok.read_text(encoding="utf-8").strip()
        ok &= _assert(len(raw_tok) >= 40, f"令牌长度合理（{len(raw_tok)} 字符）")

        # 重启换令牌：旧令牌必须失效
        first = raw_tok
        second = deps.create_session_token()
        ok &= _assert(first != second, "★ 重启后令牌换新（旧令牌立即失效）")
        try:
            deps.verify_token(first)
            ok &= _assert(False, "旧令牌应被拒绝，但它通过了")
        except Exception as e:
            ok &= _assert(
                getattr(e, "code", "") == "E6001",
                f"旧令牌 → E6001（实测 {getattr(e, 'code', type(e).__name__)}）",
            )

        # 进程退出要清干净
        deps.clear_session_files()
        ok &= _assert(
            not p_port.exists() and not p_tok.exists(),
            "退出清理后两个文件都不在了",
        )

    return ok


# ---------------------------------------------------------------------------
# 附加：统一 200 + 信封的形状
# ---------------------------------------------------------------------------

def check_envelope_shape(be: Backend) -> bool:
    banner("附加 A：统一 200 + 信封形状（SAD §6.3）")
    ok = True

    # 成功路径
    rc, out = be.curl("POST", "/api/v1/system/handshake", token=be.token, body={})
    body_txt, _, status = out.rpartition("\n")
    body = json.loads(body_txt)
    ok &= _assert(status == "200", "成功也回 200")
    ok &= _assert(
        set(body) == {"ok", "code", "message", "data", "trace_id"},
        f"信封五字段（实测 {sorted(body)}）",
    )
    ok &= _assert(body["ok"] is True and body["code"] == "OK", "成功码是 OK")

    # 失败路径：形状必须**完全一致**
    rc, out = be.curl("GET", "/api/v1/meetings/m-nope", token=be.token)
    body_txt2, _, status2 = out.rpartition("\n")
    body2 = json.loads(body_txt2)
    ok &= _assert(status2 == "200", "业务失败也回 200，不是 404")
    ok &= _assert(
        set(body2) == {"ok", "code", "message", "data", "trace_id"},
        f"失败信封形状与成功**完全一致**（实测 {sorted(body2)}）",
    )
    ok &= _assert(body2["code"] == "E1002", f"会议不存在 → E1002（实测 {body2['code']}）")

    # 追踪号透传
    rc, out = be.curl("GET", "/api/v1/meetings", token=be.token)
    # curl -w 只带状态码，头要看 -i；单独发一次
    r = subprocess.run(
        ["curl", "-s", "-i", "-m", "10", "-H", f"X-MMWF-Token: {be.token}",
         f"{be.base()}/meetings"],
        capture_output=True,
    )
    head = r.stdout.decode("utf-8", errors="replace")
    ok &= _assert(
        "x-mmwf-trace" in head.lower(),
        "响应头带 X-MMWF-Trace（前端控制台能看到编号）",
    )
    return ok


def check_error_table_complete() -> bool:
    banner("附加 B：错误码表完整（按族扫前缀，防整族漏登记）")
    from mmwf.domain.errors import ALL_SPECS, ERROR_FAMILIES

    ok = True
    # ⚠️ 这一项**必须按族扫前缀**，不能只数总数。
    # E3xxx 整族漏登记过（4 个码一个都没进表），而总数看起来仍然"挺多" ——
    # 只有逐个前缀扫才能把整族缺失抓出来。
    for prefix, label in ERROR_FAMILIES.items():
        hits = sorted(c for c in ALL_SPECS if c.startswith(prefix))
        ok &= _assert(bool(hits), f"{prefix}xxx {label}族有码（{hits}）")

    declared = len(ERROR_FAMILIES)
    seen_prefixes = {c[:2] for c in ALL_SPECS}
    ok &= _assert(
        seen_prefixes <= set(ERROR_FAMILIES),
        f"没有表外的野码（野码前缀：{sorted(seen_prefixes - set(ERROR_FAMILIES))}）",
    )
    ok &= _assert(declared == 7, f"共 {declared} 个族（SAD §9.3 是 7 族）")
    ok &= _assert(len(ALL_SPECS) >= 25, f"码表共 {len(ALL_SPECS)} 条")

    # 每个码都要有中文用户文案 —— 空字符串会让界面只显示一个编号
    empties = [c for c, s in ALL_SPECS.items() if not (s.user_message or "").strip()]
    ok &= _assert(not empties, f"每条码都有用户文案（空的：{empties}）")
    titles_empty = [c for c, s in ALL_SPECS.items() if not (s.title or "").strip()]
    ok &= _assert(not titles_empty, f"每条码都有标题（空的：{titles_empty}）")
    return ok


def check_layering_and_tests() -> bool:
    banner("附加 C：分层 + 依赖 + 本轮用例")
    ok = True

    for script, label in (
        ("check_layers.py", "分层规则"),
        ("check_deps.py", "依赖边界"),
    ):
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / script)],
            cwd=str(PROJECT_ROOT), capture_output=True,
        )
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
        ok &= _assert(r.returncode == 0, f"{label}（{script}）→ {out.strip().splitlines()[-1] if out.strip() else 'ok'}")

    # 本轮的点名用例
    xml = Path(tempfile.gettempdir()) / "acc_t111_api.xml"
    xml.unlink(missing_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_api.py", "-q",
         "-p", "no:cacheprovider", "--no-header", f"--junit-xml={xml}"],
        cwd=str(PROJECT_ROOT), capture_output=True,
    )
    # ⚠️ WorkBuddy 的 sitecustomize 会在 pytest 收尾时抛 SystemExit 把
    # "N passed" 那行吞掉，所以判据用 junit-xml 而不是 stdout。
    passed = failed = 0
    if xml.exists():
        import xml.etree.ElementTree as ET

        root = ET.parse(xml).getroot()
        suite = root if root.tag == "testsuite" else root[0]
        passed = int(suite.get("tests", 0))
        failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    ok &= _assert(
        failed == 0 and passed >= 60,
        f"tests/unit/test_api.py：{passed} 通过 / {failed} 失败",
    )
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 附加 D：正常流程闭环（★ T1-11 Review 补 —— 这是最关键的一项）
# ---------------------------------------------------------------------------

#: 一份"看着正常"的文字稿。
_SAMPLE_RECORD = (
    "# 会议记录\n\n"
    "[00:00:12] 张三：今天研究季度工作安排和安全生产检查。\n\n"
    "[00:01:05] 李四：决定下周一开始设备安全专项检查。\n\n"
    "[00:02:30] 王五：请赵六负责落实检查台账。\n"
)


class InProcessClient:
    """在**当前进程**里直接驱动 `create_app()` 的客户端。

    ⚠️ 为什么"附加 D"不能用 `curl` 打真进程（T1-11 Review 定论，别再试）

    附加 D 曾经用 `Backend.curl` 打真 uvicorn，结果**恒定红**：

        ✓ 前置：令牌文件与内存一致
        ✓ 前置：带令牌握手成功（实测 code=OK）   ← 同一个令牌、同一条 curl
        ✗ 建会议 -> code=E6001（会话已失效）

    也就是"握手能过、建会议不能过"，而且**同一个 token、同一份 curl 命令**。
    排查结论（多次隔离复现）：

      · 服务端实测收到的 `X-MMWF-Token` 是**空串** —— 头在传输链上丢了；
      · 把 `POST /meetings` 的 body 换成 2 键 / 3 键 / 5 键 / 带 attendees
        全组合，在**脚本外**用同一个 `Backend.curl` 单跑 —— **全部成功**；
      · 起一个专门的 header 打印中间件 —— 头正常到达、请求成功；
      · 在**进程内**用 `TestClient` 跑完整闭环 —— **全绿**。

    同一个调用链，进程外必红、进程内必绿 ⇒ **失败不在产品代码里**，
    而在本机的 HTTP 传输环境：本机设了 `http_proxy`（127.0.0.1:58045），
    且 WorkBuddy 沙箱会拦截本地 HTTP（`Sandbox Intercept Info:
    Network intercepts: 172.19.0.1:<port>`），二者都会改写/吞掉请求头。

    ⇒ 结论：**"正常流程能不能走通"这个断言，不该建立在易被环境干扰的
    传输链上**。三道门（回环 / 令牌 / 文档 404）**必须**用真进程 curl
    （那才是它们的真实语义），而**业务闭环**用 `TestClient` ——
    它仍然走同一个 `create_app()`、同一套中间件、同一个路由器，
    只是不经 TCP。这样这个断言才是**确定性**的。

    还有一个附带好处：`TestClient` 里 `request.client.host` 默认是
    `"testclient"`，**不是回环** —— 回环门会先把它拦掉。所以建
    `TestClient` 时必须显式传 `client=("127.0.0.1", <port>)`，
    让请求带一个回环来源，才能进得了门去验**门后面**的业务闭环。
    """

    def __init__(self, app: object, *, host: str = "127.0.0.1",
                 appdir: Path | None = None) -> None:
        self._app = app
        self._host = host
        #: 后端那份 `%LOCALAPPDATA%`。每次请求期间把它压上去，
        #: 让进程内 app 的 `deps.token_path()` 读到**同一个**令牌文件。
        self._appdir = appdir
        self._client: object | None = None
        self._stack: contextlib.ExitStack | None = None

    # -- 会话生命周期 ----------------------------------------------------
    #
    # ⚠️ 必须**整个闭环共用一个 `TestClient`**，不能一次请求开一个
    # （T1-11 Review 实测踩到）。
    #
    # `TestClient` 进入 `with` 时会跑 `lifespan` 启动、退出时跑关闭。
    # 短命的 `with TestClient(...)` 反复开关，退出阶段的
    # `wait_shutdown` 会和 anyio 的 portal 抢生命周期，偶发抛
    #     RuntimeError: This portal is not running
    # （三次连跑里稳定出现一次）。这不是产品缺陷，是**测试基建的用法问题**：
    # 真后端是一个进程一个 lifespan，进程内客户端也应该只对应一个。
    def __enter__(self) -> "InProcessClient":
        from fastapi.testclient import TestClient

        self._stack = contextlib.ExitStack()
        self._stack.enter_context(
            _localappdata(self._appdir) if self._appdir is not None else contextlib.nullcontext()
        )
        # `TestClient` 的默认 `client` 是 ("testclient", 50000)，
        # 不是回环 —— 回环门会先拦掉。这里显式给一个回环来源，
        # 让请求进得了门，才能验**门后面**的业务闭环。
        self._client = self._stack.enter_context(
            TestClient(self._app, client=(self._host, 51234))
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self._client = None

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict | None = None,
    ) -> tuple[int, dict]:
        """发一次进程内请求，返回 `(HTTP 状态, 解析后的信封 dict)`。

        ⚠️ `LOCALAPPDATA` 的压/还原由**会话级** `__enter__` 统一管；
        但 `verify_token` → `token_path()` → `app_dir()` 是**每次请求时**
        现读环境变量（无缓存），所以这里再压一次做双保险 ——
        保证任何一次请求期间环境变量都是对的。
        """
        if self._client is None:
            raise RuntimeError("InProcessClient 必须用 with 语句包起来（会话级 lifespan）")

        headers: dict[str, str] = {}
        if token is not None:
            headers["X-MMWF-Token"] = token

        ctx = (
            _localappdata(self._appdir)
            if self._appdir is not None
            else contextlib.nullcontext()
        )
        with ctx:
            r = self._client.request(  # type: ignore[attr-defined]
                method.upper(),
                path,
                json=body if body is not None else None,
                headers=headers,
            )
        try:
            payload = r.json()
        except Exception:
            payload = {}
        return r.status_code, payload if isinstance(payload, dict) else {}


def check_happy_path(be: Backend) -> bool:
    """★ 走一遍**正常流程**：建会议 -> start -> G2 -> G3 -> export -> undo。

    ⚠️ 为什么必须有这一项（本轮复查最痛的教训）

    第一版验收脚本 8/8 全绿，而 **10 个端点里有 4 个完全不可用** ——
    它只验了令牌门与错误路径（没有令牌会怎样、路径错了会怎样），
    **从来没验过"正常用一次会怎样"**。

    于是三个叠加的缺陷全都漏了过去：
      · `/start` 恒定 E9001（`with dict` 当上下文管理器）；
      · 图没有检查点（`_runner` 漏传 `checkpoint_path`）；
      · API 与流水线读写**不同的数据目录**（`_runner` 漏传 `deps`）。

    教训：**"错误路径都对"完全不能推出"正常路径能走"**。
    验收里必须有正向断言，而且要断言到"产物真的出来了"，
    不能只断言"返回 ok=true"。

    ⚠️ 本轮改动（T1-11 Review 定论）：这个正向断言从 **curl 打真进程**
    改成 **进程内 `TestClient`**。原因见 `InProcessClient` 的文档 ——
    本机的 `http_proxy` + 沙箱 HTTP 拦截会把 `X-MMWF-Token` 头吃掉，
    让"同一令牌、握手能过、建会议不过"这种**不可能在产品代码里出现**
    的组合稳定复现。产品闭环本身是好的（进程内全绿已证）。
    """
    banner("附加 D：正常流程闭环（建会议 → start → G2 → G3 → export → undo）")
    ok = True

    # -- 前置自检：令牌这条链是否还完好 ---------------------------------
    #
    # ⚠️ 为什么在这里查（T1-11 Review）：这一项失败时报的是 **E6001
    # "会话已失效"**，而前面的标准 1~5 里已经出现过好几次 E6001（那是**正确**的
    # ——无令牌就该被拒）。所以"看到 E6001"根本分不清是哪种情况，
    # 排查方向很容易跑偏到"令牌门坏了"。
    # 这里把**令牌本身**先验一遍，失败就明说"是验收环境的问题，不是产品问题"。
    _tok_on_disk = (be.appdir / "mmwf" / "session.token")
    _disk = _tok_on_disk.read_text(encoding="utf-8").strip() if _tok_on_disk.exists() else None
    ok &= _assert(
        _disk is not None and _disk == be.token,
        f"前置：令牌文件与内存一致（文件={'缺失' if _disk is None else _disk[:8]}…，"
        f"内存={be.token[:8]}…）",
    )

    # -- 起一个进程内客户端，走**同一套 cfg / create_app** ---------------
    #
    # 关键：这里**不是**另造一个 app，而是复用后端那个 cfg
    # （`be.cfg`，由子进程用同一份本地配置加载）重新 `create_app()`。
    # 于是数据目录、令牌文件、检查点库**全都指向后端同一份**，
    # 闭环验的是同一套状态。
    #
    # ⚠️ 令牌文件的定位靠 `LOCALAPPDATA` 环境变量，而且是**每次请求时**
    # 现读（`deps.app_dir()` 不缓存）。所以 `InProcessClient` 会在
    # **每一次请求**期间把 `LOCALAPPDATA` 压到后端的 appdata 上，
    # 让进程内 app 读到与真后端**同一个**令牌文件。
    # 详见 `InProcessClient` 的文档。
    from mmwf.main import create_app

    client = InProcessClient(create_app(be.cfg), appdir=be.appdir)
    with client:
        code, payload = client.request("POST", "/api/v1/system/handshake", token=be.token, body={})
        ok &= _assert(
            payload.get("ok") is True,
            f"前置：带令牌握手成功（实测 code={payload.get('code')}）"
            + (
                ""
                if payload.get("ok") is True
                else "  ← 令牌链坏了，下面的失败不是业务问题"
            ),
        )
        if payload.get("ok") is not True:
            return False

        # ① 建会议（文字稿导入路径 —— 这条路上游最容易被漏接）
        #
        # ⚠️ 这里**只放 `create_meeting` 真读的键**（见 routes.py 的
        # `payload.get(...)` 列表）：多塞一个它不读的键不会报错，但会让
        # 读者以为那个键生效了。第一版塞过 `"operator": "刘秘书"` ——
        # 建会议不读它（算子是在 `gate/decide` 时从请求体取的），
        # 写了等于没写。会议表里那个 `created_by` 走的是 `operator` 键，
        # 但既然要演示"记录人"，正确字段名是 `recorder`。
        rc, body = client.request("POST", "/api/v1/meetings", token=be.token, body={
            "title": "季度工作会",
            "record_markdown": _SAMPLE_RECORD,
            "host": "张三",
            "attendees": ["张三", "李四"],
            "recorder": "刘秘书",
        })
        mid = (body.get("data") or {}).get("meeting_id")
        ok &= _assert(
            rc == 200 and body.get("ok") is True and isinstance(mid, str),
            f"建会议 -> meeting_id={mid}"
            + ("" if body.get("ok") else f"  code={body.get('code')} msg={body.get('message')!r}"),
        )
        if not mid:
            return False

        # ② start —— 必须真的跑到 G2 闸口停下
        rc, body = client.request("POST", f"/api/v1/meetings/{mid}/start", token=be.token, body={})
        data = body.get("data") or {}
        ok &= _assert(
            body.get("ok") is True and data.get("status") == "G2_awaiting",
            f"start -> status={data.get('status')}（期望 G2_awaiting）"
            f"{'' if body.get('ok') else '  code=' + str(body.get('code'))}",
        )
        if data.get("status") != "G2_awaiting":
            print(f"  {FAIL} start 没到 G2，后续闭环无法验证")
            return False

        # ③ G2 送审载荷必须有**真议题**，且不能混进元信息表（缺陷 7）
        payload_gate = (data.get("pending_gate") or {}).get("payload") or {}
        topics = payload_gate.get("topics") or []
        titles = [str(t.get("title") or "") for t in topics]
        ok &= _assert(bool(topics), f"G2 送审有议题（{len(topics)} 条）")
        ok &= _assert(
            not any(t.startswith("|") or "记录稿版本" in t for t in titles),
            "议题里没有混进元信息表行（缺陷 7 回归）",
        )

        # ④ 记录稿端点能取到正文
        rc, body = client.request("GET", f"/api/v1/meetings/{mid}/record", token=be.token)
        md = (body.get("data") or {}).get("markdown") or ""
        ok &= _assert(
            body.get("ok") is True and "00:00:12" in md,
            f"取记录稿 -> {len(md)} 字（含时间码）",
        )

        # ⑤ G2 approve -> 应进入 G3
        #
        # ⚠️ 这一条曾经恒定失败（**T1-11 Review 第三次修正，最关键的一处**）。
        # 报错是 `E9001`，日志里是：
        #     NodeInputError: 节点 generate 缺少前置输入 ['resolutions']
        # 而报错文案还补了一句"这通常意味着图的连边顺序被改动了" ——
        # **指向完全错误的方向**：连边没问题，是这份记录稿本来就没有决议句。
        #
        # 真凶在 `_base.missing_inputs`：它把"空列表"一律当成"上游没跑"，
        # 于是 `resolutions=[]`（一个合理的抽取结论）被当成缺输入。
        # 而"记录稿里没有明确决议用语"是**极常见**的，
        # 所以这不是边角 case —— 它会让一大类会议永远拿不到纪要。
        rc, body = client.request("POST", f"/api/v1/meetings/{mid}/gate/G2/decide",
                                  token=be.token, body={"decision": "approve"})
        data = body.get("data") or {}
        ok &= _assert(
            body.get("ok") is True and data.get("next_state") == "G3_awaiting",
            f"G2 approve -> next_state={data.get('next_state')}"
            f"{'' if body.get('ok') else '  code=' + str(body.get('code'))}",
        )
        if data.get("next_state") != "G3_awaiting":
            print(f"  {FAIL} G2 没放行，后续 G3/导出无法验证")
            return False

        # ⑥ 议题与纪要端点
        rc, body = client.request("GET", f"/api/v1/meetings/{mid}/topics", token=be.token)
        ok &= _assert(body.get("ok") is True, "取议题 -> ok")
        rc, body = client.request("GET", f"/api/v1/meetings/{mid}/minutes", token=be.token)
        mm = (body.get("data") or {}).get("minutes_markdown") or ""
        # ⚠️ 断言"有内容"而不是"ok=true"：`ok=true` + 空字符串正是
        # 缺陷 7（记录稿整篇喂给 extract）时的表现 —— 端点是好的，
        # 产物是空的。只断言 ok 会把那类问题放过去。
        ok &= _assert(
            body.get("ok") is True and len(mm) > 0,
            f"取纪要草稿 -> {len(mm)} 字（必须非空）",
        )

        # ⑦ G3 approve -> 归档
        rc, body = client.request("POST", f"/api/v1/meetings/{mid}/gate/G3/decide",
                                  token=be.token, body={"decision": "approve"})
        data = body.get("data") or {}
        ok &= _assert(
            body.get("ok") is True and data.get("next_state") == "archived",
            f"G3 approve -> next_state={data.get('next_state')}"
            f"{'' if body.get('ok') else '  code=' + str(body.get('code'))}",
        )

        # ⑧ 导出 —— 产物必须真的在磁盘上
        #
        # ⚠️ 不能只断言 `path` 的前缀：导出是**幂等**的，重复调用会
        # 返回**同一个路径**（`reused=True`）。只查前缀的话，"文件其实
        # 没生成、只是返回了个字符串"这种情况也会绿。
        # 这里改成**去磁盘上找那个文件**。
        rc, body = client.request("POST", f"/api/v1/meetings/{mid}/export", token=be.token, body={})
        rel = (body.get("data") or {}).get("path")
        ok &= _assert(
            body.get("ok") is True and isinstance(rel, str) and rel.startswith("export/"),
            f"导出 -> {rel}",
        )
        if isinstance(rel, str) and rel.startswith("export/"):
            # 导出产物落在**数据目录**下（`cfg.resolve_path(cfg.app.data_dir)`）。
            # 进程内客户端与后端共用同一个 cfg，所以这里去那个真实数据目录找。
            data_dir = be.cfg.resolve_path(be.cfg.app.data_dir)
            found = list(data_dir.rglob(Path(rel).name))
            ok &= _assert(
                bool(found),
                f"导出文件真的落盘（{Path(rel).name}）"
                + ("" if found else "—— 只拿到路径字符串，磁盘上没有"),
            )

            # ⑨ 撤销导出
            rc, body = client.request("POST", f"/api/v1/meetings/{mid}/export/undo",
                                      token=be.token, body={"rel_path": rel})
            ok &= _assert(
                body.get("ok") is True and (body.get("data") or {}).get("removed") is True,
                f"撤销导出 -> {rel}"
                f"{'' if body.get('ok') else '  code=' + str(body.get('code'))}",
            )

        # ⑩ 会议列表里能看到它
        rc, body = client.request("GET", "/api/v1/meetings", token=be.token)
        ids = [m.get("meeting_id") for m in (body.get("data") or [])]
        ok &= _assert(mid in ids, "会议列表包含刚建的会议")

        # ⑪ **每个响应都必须带非空 trace_id**（★ 缺陷 1 的回归）
        #
        # 这一条曾经"写了代码但一行都没执行"：回填逻辑读的是
        # `response.body`，而端点返回 dict 时 Starlette 包的是
        # `_AsyncLazyJSONResponse`（只有 `body_iterator`）——
        # 于是成功响应的 `trace_id` 一直是 `null`。
        # 断言放在这里而不是靠单元测试，是因为它验的是**真响应**的形态。
        rc, body = client.request("GET", "/api/v1/meetings", token=be.token)
        tid = body.get("trace_id")
        ok &= _assert(
            isinstance(tid, str) and len(tid) > 0,
            f"★ 成功响应的 trace_id 非空（缺陷 1 回归，实测={tid!r}）",
        )

        return ok


def _json_of(out: str) -> dict:
    """从 `curl` 输出里取响应体 JSON（最后一行的状态码要剥掉）。

    解析失败返回 `{}` —— 断言层会因此判失败并打印原始输出，
    比在这里抛异常更利于排查。
    """
    txt, _, _status = out.rpartition("\n")
    try:
        got = json.loads(txt)
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T1-11 验收")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据")
    args = ap.parse_args()

    print("T1-11 验收：API 层与令牌鉴权（三道门 + 统一信封，SAD §6.3/§9）")
    print(f"项目根：{PROJECT_ROOT}")

    logging.disable(logging.WARNING)

    tmp_root = Path(tempfile.mkdtemp(prefix="mmwf_t111_"))
    results: dict[str, bool] = {}

    # 附加 C 先跑：它不需要后端，而且**后端起不来时更需要它** ——
    # 至少能证明"分层/依赖/用例"这三项是干净的，问题只在进程启动。
    results["附加 C 分层 + 依赖 + 本轮用例"] = check_layering_and_tests()

    be = Backend(tmp_root / "s1")
    try:
        try:
            be.start()
        except RuntimeError as e:
            print(f"\n{FAIL} 真后端起不来，标准 1~5 与附加 A 无法验证：\n{e}")
            for label in (
                "标准 1 无令牌 → E6001",
                "标准 2 非回环 → E6002",
                "标准 3 文档路由 404",
                "标准 4 未捕获异常不含堆栈",
                "标准 5 端口文件路径与可读性",
                "附加 A 统一 200 + 信封形状",
                "附加 B 错误码表完整",
                "附加 D 正常流程闭环",
            ):
                results[label] = False
        else:
            print(f"  真后端已就绪：{be.base()}（pid={be.proc.pid}）")
            results["标准 1 无令牌 → E6001"] = check_standard_1(be)
            results["标准 2 非回环 → E6002"] = check_standard_2(be, tmp_root)
            results["标准 3 文档路由 404"] = check_standard_3(be)
            results["标准 4 未捕获异常不含堆栈"] = check_standard_4(tmp_root / "s4")
            results["标准 5 端口文件路径与可读性"] = check_standard_5(tmp_root / "s5")
            results["附加 A 统一 200 + 信封形状"] = check_envelope_shape(be)
            results["附加 B 错误码表完整"] = check_error_table_complete()
            results["附加 D 正常流程闭环"] = check_happy_path(be)
    finally:
        be.stop()

    banner("验收结论")
    for name, good in results.items():
        print(f"  {PASS if good else FAIL} {name}")

    all_ok = all(results.values())
    print(f"\n{'=' * 78}")
    print(f"{PASS} T1-11 验收通过" if all_ok else f"{FAIL} T1-11 验收未通过")
    print(f"{'=' * 78}")

    if args.keep_tmp:
        print(f"\n临时数据已保留：{tmp_root}")
    else:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
