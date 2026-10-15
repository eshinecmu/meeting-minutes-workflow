"""API 层单元测试（T1-11）。

覆盖施工卡的验收标准：

    E6001   无令牌 / 错令牌 → 信封，不是 HTTP 401
    E6002   非回环来源 → 信封
    关闭文档 /docs、/openapi.json → 404
    未捕获异常 → 信封，**不含堆栈**
    端口文件与令牌文件写入正确路径
    信封形状（ok/code/message/data/trace_id）
    统一 200 状态码（含错误）

⚠️ 所有用例都把 `LOCALAPPDATA` 指到 `tmp_path`
—— `deps.py` 的 `app_dir()` 会读它。不隔离的话，
测试会在用户真实目录里写令牌文件（而且互相干扰）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmwf.api import deps as api_deps
from mmwf.api import envelope as env
from mmwf.domain.errors import (
    MmwfError,
    TokenInvalidError,
    UnexpectedSystemError,
)

API = "/api/v1"


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_appdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把启动目录隔离到 tmp_path（否则会写用户真实 LOCALAPPDATA）。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path):
    """一份**指向临时目录**的配置。

    ⚠️ 不能用项目根下的 `config/`：`Settings.resolve_path()` 是按
    `config_dir.parent` 解析相对路径的，用真 `config/` 会算出
    `<项目>/data/mmwf.db` —— 测试就会往**真实数据库**里插会议。
    所以这里 `model_copy` 出一份把 `app.data_dir` / `log_dir` 换成
    `tmp_path` 的副本，路径语义（相对 / 绝对）保持一致。
    """
    from mmwf.settings import load_settings

    root = Path(__file__).resolve().parents[2]
    cfg = load_settings(root / "config")
    return cfg.model_copy(
        update={
            "app": cfg.app.model_copy(
                update={
                    "data_dir": tmp_path / "data",
                    "log_dir": tmp_path / "data" / "logs",
                }
            )
        }
    )


@pytest.fixture
def migrated_db(settings):
    """把临时库推到最新版本 —— 端到端链路必须有个真库。"""
    from mmwf.infra.migrations.runner import migrate

    db_path = settings.resolve_path(settings.app.data_dir) / "mmwf.db"
    migrate(db_path)
    return db_path


def _make_client(app, *, token: str | None = None, raise_server_exceptions: bool = True):
    """造一个 TestClient。

    `raise_server_exceptions=False` 是**故意**的：Starlette 的
    `ServerErrorMiddleware` 在 `Exception` 处理器返回响应之后**仍会把
    异常重新抛出**（它的职责是把异常交给 `raise_server_exceptions` 决定
    是"抛给调用方"还是"吞掉只留响应"）。默认 `True` 时，`TestClient`
    会在拿到响应的同时把异常再抛一次 —— 那测的就不是"用户看到什么"了。
    关掉它，才能断言"响应体里没有堆栈"。
    """
    c = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    if token:
        c.headers.update({"X-MMWF-Token": token})
    return c


@pytest.fixture
def client(settings, isolated_appdir, migrated_db):
    """带令牌的客户端。

    `TestClient` 的 `client.host` 是 `"testclient"`，已在
    `LOOPBACK_HOSTS` 白名单里 —— **这是刻意保留的**：
    如果把中间件在测试里绕过，就永远测不到它。
    """
    from mmwf.main import create_app

    app = create_app(settings)
    return _make_client(app, token=api_deps.create_session_token())


@pytest.fixture
def anon_client(settings, isolated_appdir, migrated_db):
    """不带令牌的客户端。"""
    from mmwf.main import create_app

    return _make_client(create_app(settings))


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------

class TestEnvelope:
    def test_ok_shape(self):
        r = env.ok({"a": 1})
        assert r["ok"] is True
        assert r["code"] == "OK"
        assert r["message"] == env.DEFAULT_OK_MESSAGE
        assert r["data"] == {"a": 1}
        assert set(r) == {"ok", "code", "message", "data", "trace_id"}

    def test_fail_shape(self):
        r = env.fail("E6001", "会话已失效")
        assert r["ok"] is False
        assert r["code"] == "E6001"
        assert r["message"] == "会话已失效"
        assert r["data"] is None

    def test_ok_without_data(self):
        """`data=None` 必须合法 —— "做完即可"的接口没有返回值。"""
        r = env.ok()
        assert r["ok"] is True and r["data"] is None

    def test_from_error_uses_user_message_only(self):
        """★ 只取 user_message，**绝不泄露 detail**。"""
        exc = TokenInvalidError("内部细节：token=abc123 与库里的不匹配")
        r = env.from_error(exc)
        assert r["code"] == "E6001"
        assert "内部细节" not in json.dumps(r, ensure_ascii=False)
        assert "abc123" not in json.dumps(r, ensure_ascii=False)
        assert r["message"] == "会话已失效，请重新启动程序。"

    def test_from_exception_unknown_uses_fallback(self):
        r = env.from_exception(RuntimeError("some internal failure"))
        assert r["ok"] is False
        assert r["code"] == env.FALLBACK_CODE

    def test_from_exception_passes_through_mmwf_error(self):
        r = env.from_exception(UnexpectedSystemError("boom"))
        assert r["code"] == "E9001"
        assert "程序出错" == _title_of("E9001")

    def test_is_warning(self):
        assert env.is_warning(MmwfError(code="E4003")) is True
        assert env.is_warning(MmwfError(code="E6001")) is False

    def test_trace_id_is_hex_8(self):
        t = env.new_trace_id()
        assert len(t) == 8
        int(t, 16)  # 必须是合法十六进制

    def test_trace_ids_differ(self):
        assert env.new_trace_id() != env.new_trace_id()


# ---------------------------------------------------------------------------
# 令牌（E6001）
# ---------------------------------------------------------------------------

class TestToken:
    def test_no_token_returns_e6001(self, anon_client):
        r = anon_client.post(f"{API}/system/handshake")
        assert r.status_code == 200, "★ 必须是 200 + 信封，不是 401"
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "E6001"

    def test_wrong_token_returns_e6001(self, anon_client):
        r = anon_client.post(
            f"{API}/system/handshake", headers={"X-MMWF-Token": "wrong-token"}
        )
        assert r.json()["code"] == "E6001"

    def test_all_protected_endpoints_reject_without_token(self, anon_client):
        """★ 逐个端点验一遍 —— 免得将来新增端点忘了挂依赖。

        这份清单要与 `routes.build_router` 的端点保持同步；
        `tools/accept_t1_11.py` 会再核一次总数。
        """
        paths = [
            ("POST", f"{API}/system/handshake"),
            ("POST", f"{API}/meetings"),
            ("GET", f"{API}/meetings"),
            ("GET", f"{API}/meetings/m-x"),
            ("POST", f"{API}/meetings/m-x/start"),
            ("GET", f"{API}/meetings/m-x/topics"),
            ("POST", f"{API}/meetings/m-x/gate/G2/decide"),
            ("GET", f"{API}/meetings/m-x/minutes"),
            ("GET", f"{API}/meetings/m-x/record"),
            ("GET", f"{API}/meetings/m-x/issues"),
            ("POST", f"{API}/meetings/m-x/export"),
            ("POST", f"{API}/meetings/m-x/export/undo"),
            ("GET", f"{API}/system/selfcheck"),
            # -- T1-14 新增 10 个（字体 1 + 审计 2 + 术语库 7）--------------
            # ⚠️ 漏一个就是安全缺口：令牌依赖挂在路由器上，
            # 新端点默认受保护，但**这份清单**是防"将来有人改了挂载方式"
            # 的唯一回归网 —— 所以新增端点必须同步补进来。
            ("GET", f"{API}/system/fonts"),
            ("GET", f"{API}/audit/logs"),
            ("POST", f"{API}/audit/export"),
            ("GET", f"{API}/glossary"),
            ("POST", f"{API}/glossary"),
            ("PUT", f"{API}/glossary/term:x"),
            ("DELETE", f"{API}/glossary/term:x"),
            ("POST", f"{API}/glossary/import"),
            ("GET", f"{API}/glossary/export"),
            # -- T1-15 新增 4 个（语音稿：读 / 改 / 批量接受 / 追加音频）------
            # ⚠️ 同样是"漏一个就是安全缺口"。这 4 条里
            # `PATCH /segments/{seg_id}` 尤其要注意：它的路径**不含
            # meeting_id**，一眼看不出它属于本项目的受保护资源。
            ("GET", f"{API}/meetings/m-x/transcript"),
            ("PATCH", f"{API}/segments/m-x:00001"),
            ("POST", f"{API}/meetings/m-x/review/accept"),
            ("POST", f"{API}/meetings/m-x/audio"),
            # -- T1-16 新增 6 个（模板 3 + 差异 1 + 导出下载 1 + 方法守卫 1）---
            # ⚠️ 多了 1 条**方法守卫**（`GET .../export/undo`）。它不做事、
            # 恒抛错，但**依然必须挂令牌依赖** —— 判据是"这条路由能不能
            # 在不带令牌时被触发"，而不是"它有没有业务价值"。
            # 守卫路由同样会把 `meeting_id` 拿去 `_require_meeting` 之前的
            # 路径上跑一遍路由匹配，放行等于给攻击者一个免费的探测面。
            ("GET", f"{API}/templates"),
            ("POST", f"{API}/templates"),
            ("PUT", f"{API}/templates/tpl-x"),
            ("GET", f"{API}/meetings/m-x/diff"),
            ("GET", f"{API}/meetings/m-x/export/x.docx"),
            ("GET", f"{API}/meetings/m-x/export/undo"),
        ]
        for method, path in paths:
            r = anon_client.request(method, path, json={})
            assert r.status_code == 200, f"{method} {path} 返回了 {r.status_code}"
            assert r.json()["code"] == "E6001", f"{method} {path} 未拦下无令牌请求"

    def test_protected_endpoint_list_matches_router(self, settings):
        """端点清单必须与路由表**逐条一致** —— 防止"新增端点忘补清单"。

        上面那份清单是手工维护的，而手工维护的清单一定会漂移。
        这里从真实的 `router` 反查端点，双向比对：

          · 清单有、路由没有 → 清单过时（把已删端点当成"验过了"）
          · 路由有、清单没有 → **潜在裸奔**（新端点没被无令牌用例覆盖）

        两条都让测试红。宁可 CI 里报错，也不要在验收时才发现某个端点裸奔。

        ⚠️ 踩过的坑（第一版就写错了）：
          不能用 `app.routes`。本环境的 FastAPI 把 `include_router` 的
          结果包成 `_IncludedRouter`，**它没有 `.path`** —— 遍历它得到空集，
          于是断言报"清单里的端点全都不在路由表里"（一大串），
          根本看不出真正漏了哪条。
          正确做法：直接 `build_router(cfg)` 拿原始路由器
          （`build_router` 不负责加前缀，前缀在 `main.py` 的
          `include_router(prefix=API_PREFIX)` 那一步），
          路径自己拼上 `API_PREFIX` 再比。
        """
        from mmwf.api.routes import build_router
        from mmwf.main import API_PREFIX

        router = build_router(settings)
        actual = set()
        for r in router.routes:
            path = getattr(r, "path", None)
            if path is None:
                continue
            for m in getattr(r, "methods", None) or []:
                if m not in {"HEAD", "OPTIONS"}:
                    actual.add((m, API_PREFIX + path))

        declared = {
            ("POST", f"{API}/system/handshake"),
            ("POST", f"{API}/meetings"),
            ("GET", f"{API}/meetings"),
            ("GET", f"{API}/meetings/{{meeting_id}}"),
            ("POST", f"{API}/meetings/{{meeting_id}}/start"),
            ("GET", f"{API}/meetings/{{meeting_id}}/topics"),
            ("POST", f"{API}/meetings/{{meeting_id}}/gate/{{gate}}/decide"),
            # T2-03 新增，Claude 按规则 9 裁决（D11）。
            ("POST", f"{API}/meetings/{{meeting_id}}/gate/{{gate}}/diff"),
            ("GET", f"{API}/meetings/{{meeting_id}}/minutes"),
            # T2-04 D10：同一问题清单的 JSON / text 只读表示。
            ("GET", f"{API}/meetings/{{meeting_id}}/issues"),
            ("GET", f"{API}/meetings/{{meeting_id}}/record"),
            ("POST", f"{API}/meetings/{{meeting_id}}/export"),
            ("POST", f"{API}/meetings/{{meeting_id}}/export/undo"),
            ("GET", f"{API}/system/selfcheck"),
            ("GET", f"{API}/system/fonts"),
            ("GET", f"{API}/audit/logs"),
            ("POST", f"{API}/audit/export"),
            ("GET", f"{API}/glossary"),
            ("POST", f"{API}/glossary"),
            ("PUT", f"{API}/glossary/{{entry_id}}"),
            ("DELETE", f"{API}/glossary/{{entry_id}}"),
            ("POST", f"{API}/glossary/import"),
            ("GET", f"{API}/glossary/export"),
            # T1-15：语音稿 4 端点（21 → 25）
            ("GET", f"{API}/meetings/{{meeting_id}}/transcript"),
            ("PATCH", f"{API}/segments/{{segment_id}}"),
            ("POST", f"{API}/meetings/{{meeting_id}}/review/accept"),
            ("POST", f"{API}/meetings/{{meeting_id}}/audio"),
            # T1-16 收口为 SAD 30 条 + 1 条导出方法守卫。
            # T2-02 新增 2 个 owner 已批准的模板历史端点：
            # SAD 34 条 / 路由表 35 条 / 去重路径 30 条（含 T2-04 D10）。
            ("GET", f"{API}/templates"),
            ("POST", f"{API}/templates"),
            ("PUT", f"{API}/templates/{{template_id}}"),
            ("GET", f"{API}/templates/{{template_id}}/versions"),
            ("GET", f"{API}/templates/{{template_id}}/versions/{{version_no}}"),
            ("GET", f"{API}/meetings/{{meeting_id}}/diff"),
            ("GET", f"{API}/meetings/{{meeting_id}}/export/{{file}}"),
            ("GET", f"{API}/meetings/{{meeting_id}}/export/undo"),
        }

        stale = declared - actual
        assert not stale, f"清单里有已不存在的端点：{sorted(stale)}"
        unlisted = actual - declared
        assert not unlisted, f"路由表有未登记的端点（潜在裸奔）：{sorted(unlisted)}"
        assert len(actual) == 35, f"端点总数变了：{len(actual)}（清单 35 条）"

        # ── SAD §9.1 收口核对（这才是"进度对照"的权威口径）──────────────
        # 判据不是"数够 34 个数字"，而是**逐条点名**：
        # SAD 里列的每一条都必须真的在路由表里；多出来的只能是我们
        # **明确知道并写下理由**的那些（当前恰好 1 条方法守卫）。
        # 这样将来任何人删掉一条 SAD 端点，这里会指名报出是哪条。
        # 与验收工具共享 SAD 清单；无令牌清单仍独立维护并双向核对。
        from tools.accept_t1_17 import SAD_9_1

        sad_expected = SAD_9_1
        missing = sad_expected - actual
        assert not missing, f"SAD §9.1 里有端点没实现：{sorted(missing)}"
        extra = actual - sad_expected
        # 多出来的**只允许**是登记在案的方法守卫。
        assert extra == {("GET", f"{API}/meetings/{{meeting_id}}/export/undo")}, (
            f"超出 SAD §9.1 的端点（要么补文档要么删）：{sorted(extra)}"
        )

    def test_correct_token_passes(self, client):
        r = client.post(f"{API}/system/handshake")
        body = r.json()
        assert body["ok"] is True
        assert body["data"]["version"]

    def test_token_file_written(self, isolated_appdir):
        tok = api_deps.create_session_token()
        p = api_deps.token_path()
        assert p.exists()
        assert p.read_text(encoding="utf-8") == tok

    def test_token_regenerated_on_restart(self, isolated_appdir):
        """★ 每次启动换令牌：旧令牌必须立刻失效。"""
        first = api_deps.create_session_token()
        second = api_deps.create_session_token()
        assert first != second
        assert api_deps.read_session_token() == second
        with pytest.raises(TokenInvalidError):
            api_deps.verify_token(first)

    def test_missing_token_file_is_e6001_not_crash(self, isolated_appdir):
        with pytest.raises(TokenInvalidError):
            api_deps.verify_token("anything")


# ---------------------------------------------------------------------------
# 端口文件
# ---------------------------------------------------------------------------

class TestPortFile:
    def test_write_and_read(self, isolated_appdir):
        api_deps.write_port(53412)
        assert api_deps.read_port() == 53412

    def test_read_missing_returns_none(self, isolated_appdir):
        assert api_deps.read_port() is None

    def test_read_garbage_returns_none(self, isolated_appdir):
        api_deps.port_path().write_text("not-a-number", encoding="utf-8")
        assert api_deps.read_port() is None

    def test_app_dir_created(self, isolated_appdir):
        d = api_deps.app_dir()
        assert d.exists() and d.is_dir()


# ---------------------------------------------------------------------------
# 回环（E6002）
# ---------------------------------------------------------------------------

class TestLoopback:
    def test_loopback_whitelist_contents(self):
        from mmwf.main import LOOPBACK_HOSTS

        assert "127.0.0.1" in LOOPBACK_HOSTS
        assert "::1" in LOOPBACK_HOSTS

    def test_non_loopback_rejected(self, settings, isolated_appdir):
        """模拟非回环来源（E6002）。

        为什么不能靠 `TestClient`：它的 `client.host` 恒为 `"testclient"`，
        而这个伪地址**在白名单里**（刻意留的，好让其它用例能穿过中间件）。
        要造"外网来源"只能**自己驱动 ASGI**，在 scope 里塞一个外部 IP。

        ⚠️ 必须调用 `app(scope, receive, send)`（即 `__call__`），
        **不能**取 `app.middleware_stack` —— 后者在应用"启动"之前是
        `None`（Starlette 在 lifespan 里才构造栈），直接调用会
        `TypeError: 'NoneType' object is not callable`。
        `__call__` 会自己按需构造。
        """
        import asyncio

        from mmwf.main import create_app

        app = create_app(settings)
        api_deps.create_session_token()

        async def _run():
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "path": f"{API}/system/handshake",
                "raw_path": f"{API}/system/handshake".encode(),
                "root_path": "",
                "headers": [
                    (b"host", b"127.0.0.1:8000"),
                    (b"content-type", b"application/json"),
                    (b"content-length", b"0"),
                    (b"x-mmwf-token", b"whatever"),
                ],
                "client": ("203.0.113.9", 51234),   # ★ 明确的外网地址
                "server": ("127.0.0.1", 8000),
                "scheme": "http",
                "query_string": b"",
            }
            out: dict = {}
            started = {"status": None}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(msg):
                if msg["type"] == "http.response.start":
                    started["status"] = msg["status"]
                elif msg["type"] == "http.response.body":
                    out["body"] = out.get("body", b"") + msg.get("body", b"")

            await app(scope, receive, send)
            return started, out

        started, out = asyncio.run(_run())
        text = out.get("body", b"").decode("utf-8")
        # ★ 非回环也要走"统一 200 + 信封"，而不是 403
        assert started["status"] == 200, f"非回环应回 200+信封，实际 {started['status']}"
        assert "E6002" in text, f"非回环请求未被拦下：{text}"
        assert "/api/v1" not in text, "非回环请求不该进入路由（不该有路由级响应）"

    def test_loopback_client_passes_middleware(self, settings, isolated_appdir):
        """反证：把 client 换成 127.0.0.1 就该正常放行 —— 否则上一个
        用例可能只是"所有请求都被拦"而误判成通过。"""
        import asyncio

        from mmwf.main import create_app

        app = create_app(settings)
        api_deps.create_session_token()

        async def _run():
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "path": f"{API}/system/handshake",
                "raw_path": f"{API}/system/handshake".encode(),
                "root_path": "",
                "headers": [
                    (b"host", b"127.0.0.1:8000"),
                    (b"content-type", b"application/json"),
                    (b"content-length", b"0"),
                    (b"x-mmwf-token", api_deps.read_session_token().encode()),
                ],
                "client": ("127.0.0.1", 51234),     # ★ 回环
                "server": ("127.0.0.1", 8000),
                "scheme": "http",
                "query_string": b"",
            }
            out: dict = {}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(msg):
                if msg["type"] == "http.response.body":
                    out["body"] = out.get("body", b"") + msg.get("body", b"")

            await app(scope, receive, send)
            return out

        out = asyncio.run(_run())
        text = out.get("body", b"").decode("utf-8")
        assert '"ok":true' in text.replace(" ", ""), f"回环请求应被放行：{text}"


# ---------------------------------------------------------------------------
# 关闭文档路由
# ---------------------------------------------------------------------------

class TestDocsDisabled:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_docs_routes_are_404(self, client, path):
        """★ 保密检测会问"这个 /docs 是干什么的"，必须关掉。"""
        r = client.get(path)
        assert r.status_code == 404, f"{path} 未关闭（返回 {r.status_code}）"


# ---------------------------------------------------------------------------
# 状态码与异常
# ---------------------------------------------------------------------------

class TestStatusCodeAndErrors:
    def test_business_error_is_200(self, client):
        """业务错误也走 200 —— 与 SAD §6.3 一致。"""
        r = client.get(f"{API}/meetings/m-does-not-exist")
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_unknown_exception_no_traceback(self, settings, isolated_appdir, migrated_db):
        """★ 未捕获异常 → 信封，且**响应体里不含堆栈**（N3.3 / SAD §9.3）。

        ⚠️ 三个坑叠在一起，少一个这个用例就测不出东西：

        ① 路由要**加在 `create_app` 之后** —— 直接 `@app.get(...)` 即可，
           FastAPI 允许动态加路由（`include_router` 的懒包装不影响这点）。
        ② `TestClient` 默认 `raise_server_exceptions=True`，Starlette 的
           `ServerErrorMiddleware` 在处理完异常、生成响应之后**仍会把异常
           重新抛给调用方**。不关掉它，`pytest` 会先在 `c.get()` 那一行
           炸掉，断言根本执行不到。
        ③ 必须注册了 `MmwfError` 之外的 `Exception` 处理器 —— `RuntimeError`
           不是业务异常，全靠兜底那个。
        """
        from mmwf.main import create_app

        app = create_app(settings)
        api_deps.create_session_token()

        @app.get("/api/v1/_boom")
        def _boom():
            raise RuntimeError("secret internal detail: /etc/passwd")

        c = TestClient(app, raise_server_exceptions=False)
        c.headers.update({"X-MMWF-Token": api_deps.read_session_token() or ""})
        r = c.get("/api/v1/_boom")
        assert r.status_code == 200, f"★ 未捕获异常也必须回 200，实际 {r.status_code}"
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == env.FALLBACK_CODE
        raw = json.dumps(body, ensure_ascii=False)
        for leak in ("Traceback", "secret internal detail", "/etc/passwd", "RuntimeError"):
            assert leak not in raw, f"响应体泄露了 {leak!r}：{raw}"

    def test_trace_id_present_on_error(self, client):
        r = client.get(f"{API}/meetings/m-nope")
        assert r.json()["trace_id"]

    def test_trace_header_on_success(self, client):
        r = client.post(f"{API}/system/handshake")
        assert r.headers.get("x-mmwf-trace")


# ---------------------------------------------------------------------------
# 会议端点契约
# ---------------------------------------------------------------------------

class TestMeetingEndpoints:
    def test_create_requires_title(self, client):
        r = client.post(f"{API}/meetings", json={"audio_paths": []})
        assert r.json()["ok"] is False
        assert r.json()["code"] == "E1004"

    def test_create_requires_some_input(self, client):
        r = client.post(f"{API}/meetings", json={"title": "有标题但没内容"})
        assert r.json()["code"] == "E1004"

    def test_create_rejects_bad_classification(self, client):
        r = client.post(
            f"{API}/meetings",
            json={"title": "x", "record_markdown": "# 记录\n", "classification": "bogus"},
        )
        assert r.json()["code"] == "E1004"

    def test_create_with_record_markdown(self, client, settings):
        r = client.post(
            f"{API}/meetings",
            json={
                "title": "第三季度工作推进会",
                "record_markdown": "# 会议记录\n\n张三：讨论接口对接。\n",
                "classification": "internal",
                "operator": "刘秘书",
            },
        )
        body = r.json()
        assert body["ok"] is True, body
        assert body["data"]["meeting_id"].startswith("m-")

    def test_unknown_meeting_is_e1002(self, client):
        r = client.get(f"{API}/meetings/m-nonexistent")
        assert r.json()["code"] == "E1002"

    def test_unknown_gate_is_e4001(self, client, settings):
        r = client.post(
            f"{API}/meetings",
            json={"title": "t", "record_markdown": "# 记录\n"},
        )
        mid = r.json()["data"]["meeting_id"]
        r2 = client.post(
            f"{API}/meetings/{mid}/gate/G9/decide", json={"decision": "approve"}
        )
        assert r2.json()["code"] == "E4001"

    def test_unknown_decision_is_e4001(self, client):
        r = client.post(
            f"{API}/meetings", json={"title": "t", "record_markdown": "# 记录\n"}
        )
        mid = r.json()["data"]["meeting_id"]
        r2 = client.post(
            f"{API}/meetings/{mid}/gate/G2/decide", json={"decision": "bogus"}
        )
        assert r2.json()["code"] == "E4001"

    def test_list_meetings_includes_created(self, client):
        client.post(f"{API}/meetings", json={"title": "甲", "record_markdown": "# a\n"})
        client.post(f"{API}/meetings", json={"title": "乙", "record_markdown": "# b\n"})
        r = client.get(f"{API}/meetings")
        titles = [m["title"] for m in r.json()["data"]]
        assert "甲" in titles and "乙" in titles

    def test_list_filter_by_status(self, client):
        client.post(f"{API}/meetings", json={"title": "丙", "record_markdown": "# c\n"})
        r = client.get(f"{API}/meetings", params={"status": "created"})
        assert all(m["status"] == "created" for m in r.json()["data"])


# ---------------------------------------------------------------------------
# 决策文案
# ---------------------------------------------------------------------------

class TestDecisionMessage:
    def test_edit_message_says_not_rerun(self):
        """★ `edit` 的文案必须让用户明白"之前的活没白干"。"""
        from mmwf.api.routes import _decision_message

        msg = _decision_message("edit")
        assert "未重做" in msg or "不重做" in msg

    def test_reject_message(self):
        from mmwf.api.routes import _decision_message

        assert _decision_message("reject") == "已退回重做"

    def test_all_three_covered(self):
        from mmwf.api.routes import ALLOWED_DECISIONS, _decision_message

        for d in ALLOWED_DECISIONS:
            assert _decision_message(d)


# ---------------------------------------------------------------------------
# 错误码表（T1-11 补齐 E3xxx/E5xxx/E6xxx/E9xxx）
# ---------------------------------------------------------------------------

class TestErrorTable:
    """码表自检。

    ⚠️ 为什么是"**按族扫前缀**"而不是"数总数"：
    E3xxx 曾经**整族 4 个码一个都没登记**，同时总数看着还有 23 条 ——
    只数总数完全发现不了。整族缺失是真实发生过的缺陷（T1-11 抓出），
    所以这条规则必须写进测试，不能只留在验收脚本里。
    """

    def test_every_family_has_codes(self):
        from mmwf.domain.errors import ALL_SPECS, ERROR_FAMILIES

        for prefix, label in ERROR_FAMILIES.items():
            hits = [c for c in ALL_SPECS if c.startswith(prefix)]
            assert hits, f"{prefix}xxx（{label}）整族没有登记任何错误码"

    def test_no_codes_outside_declared_families(self):
        """表外的"野码"说明有人随手造了一个码 —— 码表是权威，必须先登记。"""
        from mmwf.domain.errors import ALL_SPECS, ERROR_FAMILIES

        stray = sorted({c[:2] for c in ALL_SPECS} - set(ERROR_FAMILIES))
        assert not stray, f"码表里有未声明族的前缀：{stray}"

    def test_seven_families_per_sad(self):
        """SAD §9.3 是 7 族：E1/E2/E3/E4/E5/E6/E9。"""
        from mmwf.domain.errors import ERROR_FAMILIES

        assert set(ERROR_FAMILIES) == {"E1", "E2", "E3", "E4", "E5", "E6", "E9"}

    def test_every_code_has_chinese_user_message(self):
        """★ 空文案 = 界面上只显示一个编号，用户看不懂。"""
        from mmwf.domain.errors import ALL_SPECS

        empty = [c for c, s in ALL_SPECS.items() if not (s.user_message or "").strip()]
        assert not empty, f"这些码没有用户文案：{empty}"

    def test_every_code_has_title(self):
        from mmwf.domain.errors import ALL_SPECS

        empty = [c for c, s in ALL_SPECS.items() if not (s.title or "").strip()]
        assert not empty, f"这些码没有标题：{empty}"

    @pytest.mark.parametrize(
        "cls_name,code",
        [
            ("SchemaViolationError", "E3002"),
            ("ChunkingError", "E3001"),
            ("GenerationTimeoutError", "E3003"),
            ("ModelUnavailableError", "E3004"),
            ("TokenInvalidError", "E6001"),
            ("NonLoopbackError", "E6002"),
            ("UnexpectedSystemError", "E9001"),
        ],
    )
    def test_exception_class_binds_to_code(self, cls_name, code):
        """异常类必须把自己绑到正确的码上，且能取到文案。

        这条防的是"类建了但 code 写错"，那会让界面显示错误的提示。
        """
        import mmwf.domain.errors as E

        exc = getattr(E, cls_name)("细节不该外泄")
        assert exc.code == code
        assert exc.user_message.strip(), f"{cls_name} 取不到用户文案"
        assert exc.detail == "细节不该外泄", "detail 应原样保留（给日志用）"

    def test_e3002_is_warning_not_error(self):
        """★ E3002 是**过程**信号（自检没过、正在重试），必须是 WARNING。

        把它标成 ERROR，前端会在一次瞬时重试时就弹红框，
        而用户其实只要等一等就成功了。
        """
        from mmwf.domain.errors import SchemaViolationError, Severity

        assert SchemaViolationError("x").severity is Severity.WARNING


# ---------------------------------------------------------------------------
# 分层守卫（T1-11 把 api/ 纳入 check_layers.py）
# ---------------------------------------------------------------------------

class TestLayerGuard:
    """`tools/check_layers.py` 的规则必须真的生效。

    ⚠️ 背景：T1-11 之前 `check_layers.py` **只扫 `domain/` 与 `graph/`**，
    `api/` 与 `main.py` 完全在 C01 视野之外 —— 在 `api/routes.py` 里
    `import docx` 不会有任何东西报警。这些用例锁住"新增的 API 层规则
    确实会拦下违规"，免得将来有人把扫描范围又改窄回去。
    """

    def _check(self, source: str, filename: str, tmp_path):
        """把一段源码写成文件，跑 `check_api_layer`，返回错误列表。"""
        import importlib

        import check_layers

        importlib.reload(check_layers)
        p = tmp_path / filename
        p.write_text(source, encoding="utf-8")
        # `check_api_layer` 里会 `relative_to(PROJECT_ROOT)`，所以文件
        # 必须真的位于项目内 —— 直接测底层函数会因路径断言失败。
        # 改用"源码字符串喂给 AST"的更稳做法：临时替换相对路径逻辑。
        orig = check_layers.PROJECT_ROOT
        try:
            check_layers.PROJECT_ROOT = tmp_path.parent
            return check_layers.check_api_layer(p)
        finally:
            check_layers.PROJECT_ROOT = orig

    @pytest.mark.parametrize(
        "bad",
        ["import sqlite3", "import httpx", "import torch",
         "import docx", "import langgraph", "import requests"],
    )
    def test_api_layer_forbidden_imports(self, bad, tmp_path):
        src = f"from __future__ import annotations\n{bad}\n"
        if "import" in bad:
            mod = bad.split()[1]
            src = f"import {mod}\n"
        errs = self._check(src, "fake_api.py", tmp_path)
        assert errs, f"API 层应禁止 {bad!r}，但检查没报错"

    def test_api_layer_allows_fastapi(self, tmp_path):
        """反证：fastapi 是 API 层本职，不该被拦。"""
        errs = self._check("from fastapi import APIRouter\n", "fake_api.py", tmp_path)
        assert not errs, f"fastapi 不该被禁：{errs}"

    def test_api_layer_rejects_wildcard_bind(self, tmp_path):
        """★ C11 的代码层兜底：写死 0.0.0.0 必须被拦。"""
        errs = self._check('HOST = "0.0.0.0"\n', "fake_api.py", tmp_path)
        assert any("0.0.0.0" in e for e in errs), errs

    def test_real_api_package_is_clean(self):
        """★ 真实代码本身必须干净 —— 这条是"当前状态"的锚点。"""
        import subprocess
        import sys as _sys
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[2]
        r = subprocess.run(
            [_sys.executable, str(root / "tools" / "check_layers.py")],
            cwd=str(root), capture_output=True,
        )
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
        assert r.returncode == 0, out


# ---------------------------------------------------------------------------
# ★ Review 修复的回归测试
#
# 下面每个类对应 T1-11 代码复查中抓到的一个**真实缺陷**。
# 它们必须留下 —— 验收脚本 `accept_t1_11.py` 当时 8/8 全绿，
# 而 10 个端点里有 4 个完全不可用：**它只验了令牌门与错误路径，
# 从来没走过一遍正常流程**。这些用例补的正是那个缺口。
# ---------------------------------------------------------------------------

#: 一份"看着正常"的文字稿 —— 带时间码、说话人、决议、待办。
SAMPLE_RECORD = (
    "# 会议记录\n\n"
    "[00:00:12] 张三：今天研究季度工作安排和安全生产检查。\n\n"
    "[00:01:05] 李四：决定下周一开始设备安全专项检查。\n\n"
    "[00:02:30] 王五：请赵六负责落实检查台账。\n\n"
    "[00:03:40] 张三：会议认为要抓好末端落实。\n"
)


def _new_meeting(client, **extra) -> str:
    """建一个带记录稿的会议，返回 meeting_id。"""
    payload = {"title": "季度工作会", "record_markdown": SAMPLE_RECORD, **extra}
    r = client.post(f"{API}/meetings", json=payload)
    assert r.json()["ok"] is True, r.json()
    return r.json()["data"]["meeting_id"]


class TestHappyPath:
    """★ 缺陷 4/5/9：`/start` 根本没跑通过。

    当时的三个叠加原因（都要靠"走正常流程"才暴露）：
      · 缺陷 4 `_runner()` 没传 `checkpoint_path` → 图无检查点；
      · 缺陷 5 `with _require_meeting(...)` 把 dict 当上下文管理器；
      · 缺陷 9 `_runner()` 没传 `deps` → 节点用 `get_settings()`
        另加载一份配置，读写**与 API 不同的数据目录**。

    缺陷 9 最隐蔽：它在生产上不报错（两处恰好都是同一个 `data/`），
    只在测试/多实例下炸，而且症状（"转写分段不存在"）把排查方向
    指向数据本身，而不是配置。
    """

    def test_start_reaches_g2_gate(self, client):
        mid = _new_meeting(client)
        r = client.post(f"{API}/meetings/{mid}/start")
        body = r.json()
        assert body["ok"] is True, body
        assert body["data"]["status"] == "G2_awaiting"
        assert body["data"]["pending_gate"]["gate"] == "G2"

    def test_start_then_topics_then_g2_approve(self, client):
        """整条主链：start -> topics 有内容 -> G2 approve -> 进 G3。"""
        mid = _new_meeting(client)
        client.post(f"{API}/meetings/{mid}/start")

        topics = client.get(f"{API}/meetings/{mid}/topics").json()
        assert topics["ok"] is True
        assert topics["data"]["topics"], "议题不该是空的"

        r = client.post(
            f"{API}/meetings/{mid}/gate/G2/decide", json={"decision": "approve", "checkpoint_id": client.get(f"{API}/meetings/{mid}").json()["data"]["run"]["pending_gate"]["checkpoint_id"]}
        )
        body = r.json()
        assert body["ok"] is True, body
        assert body["data"]["next_state"] == "G3_awaiting"

    def test_full_loop_through_export(self, client):
        """★ 完整闭环：start -> G2 -> G3 -> export -> undo。

        这是验收脚本当时**完全没覆盖**的路径。
        """
        mid = _new_meeting(client)
        assert client.post(f"{API}/meetings/{mid}/start").json()["ok"] is True

        g2 = client.post(
            f"{API}/meetings/{mid}/gate/G2/decide", json={"decision": "approve", "checkpoint_id": client.get(f"{API}/meetings/{mid}").json()["data"]["run"]["pending_gate"]["checkpoint_id"]}
        ).json()
        assert g2["ok"] is True and g2["data"]["next_state"] == "G3_awaiting"

        assert client.get(f"{API}/meetings/{mid}/minutes").json()["ok"] is True

        g3 = client.post(
            f"{API}/meetings/{mid}/gate/G3/decide", json={"decision": "approve", "checkpoint_id": client.get(f"{API}/meetings/{mid}").json()["data"]["run"]["pending_gate"]["checkpoint_id"]}
        ).json()
        assert g3["ok"] is True, g3
        assert g3["data"]["next_state"] == "archived"

        ex = client.post(f"{API}/meetings/{mid}/export", json={}).json()
        assert ex["ok"] is True, ex
        rel = ex["data"]["path"]
        assert rel.startswith("export/")

        un = client.post(
            f"{API}/meetings/{mid}/export/undo", json={"rel_path": rel}
        ).json()
        assert un["ok"] is True, un

    def test_runner_passes_settings_to_nodes(self, settings, isolated_appdir, migrated_db):
        """★ 缺陷 9 的**定点**断言：`Deps.settings` 必须是注入的那份。

        只断言"闭环跑通"还不够 —— 万一将来有人把 `deps` 又去掉，
        而生产恰好仍能跑，这个回归就会复活。所以直接查对象身份。
        """
        from mmwf.graph.build import Deps
        from mmwf.graph.runner import GraphRunner

        deps = Deps(settings=settings)
        rn = GraphRunner("m-x", deps=deps)
        assert rn.deps.settings is settings
        rn.close()

    def test_runner_passes_checkpoint_path(self, settings):
        """★ 缺陷 4 的定点断言：没有检查点，图就没有持久化。"""
        from mmwf.graph.runner import GraphRunner

        db = settings.resolve_path(settings.app.data_dir) / "mmwf.db"
        rn = GraphRunner("m-x", checkpoint_path=db)
        try:
            assert rn.app is not None          # 能编译出来
            assert rn._saver is not None, "传了 checkpoint_path 就必须建 saver"
        finally:
            rn.close()

    def test_start_without_any_input_is_e1004(self, client, settings):
        """★ 缺陷 6：既没音频也没记录稿时，应当是**输入错误**而不是 E9001。

        拦在这里的价值：让用户当场知道"这次没有可处理的文稿"，
        而不是等一轮之后看到一个指向系统的内部错误。
        """
        mid = _new_meeting(client)
        # 把记录稿从存储层抹掉，模拟"建了会议但没有任何正文"
        from mmwf.infra.filestore import make_filestore

        store = make_filestore(mid, settings.resolve_path(settings.app.data_dir))
        store.delete("record/v1.md")

        r = client.post(f"{API}/meetings/{mid}/start")
        assert r.json()["code"] == "E1004", r.json()


class TestTextImportSegments:
    """★ 缺陷 6：文字稿导入必须产出 `transcript/segments.json`。

    否则 `review` 节点的输入契约（T1-08 定的"从存储层读"）
    在**这条路**上永远没人满足 —— 整条闭环跑不起来。
    """

    def test_start_writes_segments_file(self, client, settings):
        mid = _new_meeting(client)
        client.post(f"{API}/meetings/{mid}/start")

        from mmwf.infra.filestore import make_filestore

        store = make_filestore(mid, settings.resolve_path(settings.app.data_dir))
        assert store.exists("transcript/segments.json")
        raw = store.read_json("transcript/segments.json")
        assert raw["engine"] == "text_import"
        # 四行正文 -> 四段（标题与空行不算）
        assert len(raw["segments"]) == 4

    def test_segments_have_parsed_timecodes(self, client, settings):
        """时间码必须**真的解出来**，不能是 None —— 否则溯源给不出"第几分钟"。"""
        mid = _new_meeting(client)
        client.post(f"{API}/meetings/{mid}/start")

        from mmwf.infra.filestore import make_filestore

        store = make_filestore(mid, settings.resolve_path(settings.app.data_dir))
        segs = store.read_json("transcript/segments.json")["segments"]
        assert [s["start_ms"] for s in segs] == [12_000, 65_000, 150_000, 220_000]
        # 置信度必须是 None（文字稿没有 ASR 置信度，编一个是撒谎）
        assert all(s["confidence"] is None for s in segs)

    def test_timecodes_are_monotonic(self, settings):
        """★ 时间轴**单调**是唯一的硬要求（下游排序/定位都靠它）。

        人工整理的稿子常只给每个话题一个时间码，后面几行没有 ——
        这时必须递推出单调序列，而不是留 None 或复制同一个值。
        """
        from mmwf.domain.review import record_source

        md = "[00:00:10] 第一句。\n第二句没有时间码。\n第三句也没有。\n[00:05:00] 第四句。\n"
        starts = [p.start_ms for p in record_source.extract_lines(md)]
        assert starts == sorted(starts), starts
        assert len(set(starts)) == len(starts), "不许出现重复时间点"
        assert starts[0] == 10_000 and starts[-1] == 300_000

    def test_headings_and_blank_lines_are_not_segments(self):
        from mmwf.domain.review import record_source

        md = "# 会议记录\n\n> 版本 v1\n\n---\n\n| a | b |\n|---|---|\n\n真正文。\n"
        texts = [p.text for p in record_source.extract_lines(md)]
        assert texts == ["真正文。"]

    def test_speaker_is_preserved_in_text(self):
        from mmwf.domain.review import record_source

        segs = record_source.to_raw_segments("[00:00:01] 李四：决定下周开工。\n")
        assert segs[0]["text"] == "李四：决定下周开工。"


class TestRecordBodySeparation:
    """★ 缺陷 7：`record_markdown` 必须是**正文**，不是"给人看的完整稿"。

    完整稿含八项元信息表与待核对清单，而 `extract` 把它当作
    "这次会议说了什么"的唯一输入 —— 于是元信息表被逐行抽成议题。
    实测一篇正常记录稿产出 15 条自检问题、**14 条是误报**，
    并且因为 `route_after_validate` 看到 issues 就重试，
    **每篇纪要都会走满重试路径**、最后带着 15 条噪音进定稿。
    """

    def test_no_false_issues_from_metadata_table(self, client):
        mid = _new_meeting(client, host="张三", attendees=["张三", "李四"])
        client.post(f"{API}/meetings/{mid}/start")
        r = client.post(
            f"{API}/meetings/{mid}/gate/G2/decide", json={"decision": "approve", "checkpoint_id": client.get(f"{API}/meetings/{mid}").json()["data"]["run"]["pending_gate"]["checkpoint_id"]}
        ).json()
        issues = r["data"]["pending_gate"]["payload"]["validation_issues"]
        # D4 生产检查新增会议必填、时间与一致性提示；仍须防止元信息表伪造议题。
        assert not any(i.get("blocking") for i in issues)
        assert not any(i["rule"] in {"topic_without_refs", "resolution_without_refs"} for i in issues)
        assert not any("|" in str(i.get("evidence", {})) for i in issues)
        assert any(i["source"] == "template" and "缺失" in i["detail"] for i in issues)

    def test_g2_payload_has_no_metadata_rows(self, client):
        """议题里不许出现 `| 会议时间 | 待补充 |` 这类表格行。"""
        mid = _new_meeting(client, host="张三")
        run = client.post(f"{API}/meetings/{mid}/start").json()["data"]
        titles = [t["title"] for t in run["pending_gate"]["payload"]["topics"]]
        assert not any(t.startswith("|") for t in titles), titles
        assert not any("记录稿版本" in t for t in titles), titles

    def test_extract_body_drops_all_sections(self):
        from mmwf.app.services.review import extract_body
        from mmwf.domain.review.record import RECORD_BODY_HEADING

        md = (
            "# 会议记录\n\n> 记录稿版本：v2\n\n"
            "## 一、会议基本信息\n\n| 项目 | 内容 |\n|---|---|\n| 会议时间 | 待补充 |\n\n"
            f"## {RECORD_BODY_HEADING}\n\n[00:00:12] 张三：真正文。\n\n"
            "## 三、待核对事项\n\n- **[00:00:12]**（识别置信度偏低）\n"
        )
        body = extract_body(md)
        assert body == "[00:00:12] 张三：真正文。"
        assert "待补充" not in body and "待核对" not in body

    def test_extract_body_falls_back_to_whole_text(self):
        """★ 认不出结构时必须**退回全文**，不能返回空串。

        返回空串的后果是产出一篇空纪要 —— 比"多几条误报"严重得多。
        """
        from mmwf.app.services.review import extract_body

        assert extract_body("没有小标题的一段话。") == "没有小标题的一段话。"

    def test_record_body_heading_is_single_source(self):
        """渲染与取正文必须共用同一个标题常量，否则会静默漂移。"""
        from mmwf.domain.review.record import RECORD_BODY_HEADING, render_record
        from mmwf.domain.review.record import RecordMetadata

        md = render_record(
            [], RecordMetadata(generated_at="2026-01-01T00:00:00Z"),
            {"asr": {"low_confidence": 0.7, "warn_avg_confidence": 0.85,
                     "warn_low_conf_ratio": 0.15}},
            version=1,
        ).markdown
        assert f"## {RECORD_BODY_HEADING}" in md


class TestReviewCacheInvalidation:
    """★ 缺陷 10：`record_markdown` 必须进 `review` 的幂等键。

    "文字稿导入"路上 `transcript` 恒为空表 ——
    键里带的 `transcript=[]` 从来没变过。于是用户改了记录稿、
    再点"重新生成"，`review` 会**幂等命中**上一轮结果，
    改动永远不生效：不报错、不提示，就是"改了没反应"。
    """

    def test_record_markdown_in_review_idem_keys(self):
        from mmwf.graph.idempotency import IDEMPOTENT_INPUT_KEYS
        from mmwf.graph import state as S

        assert "record_markdown" in IDEMPOTENT_INPUT_KEYS[S.NODE_REVIEW]

    def test_changed_record_produces_different_key(self):
        from mmwf.graph.idempotency import (
            IDEMPOTENT_INPUT_KEYS,
            make_idempotency_key,
            node_input_from_state,
        )
        from mmwf.graph import state as S

        base = {"transcript": [], "record_markdown": "第一版正文"}
        changed = {"transcript": [], "record_markdown": "改过的正文"}

        k1 = make_idempotency_key("m-1", S.NODE_REVIEW,
                                  node_input_from_state(S.NODE_REVIEW, base))
        k2 = make_idempotency_key("m-1", S.NODE_REVIEW,
                                  node_input_from_state(S.NODE_REVIEW, changed))
        assert k1 != k2, "改过记录稿却算出同一个幂等键 —— 改动会被静默吞掉"


class TestReviewPrecondition:
    """★ 缺陷 8：`ALLOWED_PREV_STATES` 把正常流程挡在了外面。

    从 API 建的会议，`meeting.status` 是建表默认值 `created`，
    而**流水线从来不写它**（编排层只写 `task_state`）。
    于是"从 API 正常从头跑一遍"的会议，状态恰好是被挡住的那个 ——
    每次 `start` 都在 `review` 抛错。错误信息还把排查引向
    "谁把状态弄错了"，而真相是"这道门槛拦错了人"。
    """

    def test_created_is_allowed(self):
        from mmwf.app.services.review import ALLOWED_PREV_STATES

        assert "created" in ALLOWED_PREV_STATES

    def test_all_graph_upstream_states_allowed(self):
        """图上游所有可能的状态都必须在白名单里。

        用 `NODE_STATUS` 反查而不是硬列 —— 将来加节点时，
        这条断言会自动覆盖到新状态。
        """
        from mmwf.app.services.review import ALLOWED_PREV_STATES
        from mmwf.graph import state as S

        upstream_nodes = S.NODE_SEQUENCE[: S.NODE_SEQUENCE.index(S.NODE_REVIEW)]
        required = {S.NODE_STATUS[n] for n in upstream_nodes}
        required.add(S.STATUS_CREATED)
        missing = required - ALLOWED_PREV_STATES
        assert not missing, f"这些上游状态会导致 start 失败：{sorted(missing)}"

    def test_generates_from_created_status(self, settings, migrated_db):
        """端到端：状态是 `created` 时也能生成记录稿。"""
        from mmwf.app.services.review import make_review_service
        from mmwf.infra.db import connect_ctx
        from mmwf.infra.filestore import make_filestore

        mid = "m-created"
        db = settings.resolve_path(settings.app.data_dir) / "mmwf.db"
        with connect_ctx(db) as conn:
            conn.execute(
                "INSERT INTO meeting (meeting_id,title,classification,status,"
                "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (mid, "t", "internal", "created", "u",
                 "2026-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00"),
            )
        store = make_filestore(mid, settings.resolve_path(settings.app.data_dir))
        store.write_json("transcript/segments.json", {
            "engine": "e", "model": "m",
            "segments": [{"text": "正文。", "start_ms": 0, "end_ms": 1000,
                          "confidence": None}],
        })
        out = make_review_service(mid, settings).generate_record(write_state=False)
        assert out.version >= 1


# ---------------------------------------------------------------------------
# 系统自检端点（T1-13）
# ---------------------------------------------------------------------------

class TestSelfcheckEndpoint:
    """`GET /system/selfcheck` 的端点层验证。

    判定逻辑本身在 `tests/unit/test_selfcheck.py` 里测（那里用假进程
    造出确定的"有外发/无外发"）；这一层只关心**端点契约**：
    信封形状、SAD §9.2 的字段、以及"自检不该 500"。
    """

    def test_selfcheck_returns_envelope(self, client):
        r = client.get(f"{API}/system/selfcheck")
        assert r.status_code == 200, "★ 统一 200 + 信封"
        body = r.json()
        assert body["ok"] is True
        assert body["code"] == "OK"
        assert body["trace_id"], "自检响应也必须带 trace_id"
        assert body["message"]

    def test_selfcheck_has_four_checks_per_sad_9_2(self, client):
        """★ 四项检查一项都不能少，字段名照 SAD §9.2。"""
        d = client.get(f"{API}/system/selfcheck").json()["data"]
        for key in ("network_probe", "asr_quality",
                    "font_availability", "audit_chain"):
            assert key in d, f"缺 {key}"
            assert "verdict" in d[key] or key == "audit_chain", (
                f"{key} 缺 verdict"
            )

    def test_selfcheck_network_fields(self, client):
        """`outbound_connections` 必须在，且（无外发时）为 0。

        ★ 这是 SEC-02 验收项在单元层的体现。
        """
        net = client.get(f"{API}/system/selfcheck").json()["data"]["network_probe"]
        assert "outbound_connections" in net
        assert isinstance(net["outbound_connections"], int)
        assert "listening_ports" in net
        assert "dns_queries_attempted" in net
        # 本项目的服务进程不该有外发连接
        assert net["outbound_connections"] == 0, (
            "★ 测试进程出现了对外连接 —— 要么产品真发了请求，"
            "要么判定范围写错了（把别进程的连接算进来了）"
        )

    def test_selfcheck_audit_chain_pass_on_fresh_db(self, client):
        """新迁移的空库：链是"空即完好" -> pass。"""
        chain = client.get(f"{API}/system/selfcheck").json()["data"]["audit_chain"]
        assert chain["integrity"] == "pass"

    def test_selfcheck_writes_audit_record(self, client, settings):
        """★ AUD-08：自检本身必须留痕（`selfcheck` 是 8 类最小核查项之一）。"""
        import sqlite3
        db = settings.resolve_path(settings.app.data_dir) / "mmwf.db"
        client.get(f"{API}/system/selfcheck")
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='selfcheck'"
        ).fetchone()[0]
        conn.close()
        assert n >= 1, "自检没有写审计记录 —— 谁来查过这件事本身也要留痕"

    def test_selfcheck_overall_is_valid_value(self, client):
        d = client.get(f"{API}/system/selfcheck").json()["data"]
        assert d["overall"] in ("pass", "degraded", "fail")

    def test_selfcheck_never_returns_500(self, client):
        """★ 自检跑在什么环境下都不该 500（缺依赖/没权限/空库）。"""
        r = client.get(f"{API}/system/selfcheck")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _title_of(code: str) -> str:
    from mmwf.domain.errors import ALL_SPECS

    return ALL_SPECS[code].title
