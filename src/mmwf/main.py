"""API 应用装配与启动（T1-11）。

本模块是 API 层的"总装车间"：把 FastAPI 应用、三道门（回环/令牌/关文档）、
统一信封、路由表拼在一起，并提供 `python -m mmwf.main` 的启动入口。

三道门为什么缺一不可
--------------------
本服务没有登录体系，所以"谁能调"完全靠这三条：

    ① **只听回环** —— 请求来源不是 127.0.0.1/::1 一律 E6002。
       注意这**不是**靠 uvicorn 的 bind 地址实现的：bind 只决定
       "能不能连上"，而代理、端口转发、`curl --interface` 都能绕过 bind。
       所以必须在**中间件里查 `request.client.host`**（施工卡验收项
       明确要求用 `curl --interface` 模拟）。
    ② **启动令牌** —— 见 `deps.py`。防止"本机上别的程序"乱调。
    ③ **关闭文档** —— `docs_url` / `redoc_url` / `openapi_url` 全设 None。
       FastAPI 默认开启，浏览器打开 `/docs` 就是一个可直接点着玩的
       接口列表 —— 保密检测一定会问"这个 /docs 是干什么的"。

统一 200 + 信封
--------------
`HTTPException` 默认返回非 200 状态码，与本项目约定冲突（SAD §6.3）。
所以这里**不 raise HTTPException**，而是注册异常处理器直接返回
`JSONResponse(status_code=200, ...)`。这条最容易被写"半半拉愣"：
只要有一个端点漏了，前端就要处理两种响应形态。
下面的处理器覆盖三类来源：

    · `MmwfError`              —— 业务异常（含 E6001/E6002）
    · `RequestValidationError` —— FastAPI 自己的入参校验失败
    · `Exception`              —— 兜底（E9001），**绝不含堆栈**

堆栈只进日志
-----------
用户看到的永远是"程序遇到一个未预期的问题…"（N3.3）。完整堆栈由
`envelope.from_exception()` 打到 `app.log`，用户报 `trace_id`
给运维，运维拿编号去日志里捞 —— 这条链路是一期约定，
日志侧打通排在 T1-13 自检。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
import logging
import socket
from typing import Any

from mmwf.api import deps as api_deps
from mmwf.api import envelope as env
from mmwf.domain.errors import MmwfError
from mmwf.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: 允许的客户端地址。`::1` 是 IPv6 回环；`testclient` 是 FastAPI
#: `TestClient` 在进程内调用时使用的伪客户端名 —— **只在测试里出现**，
#: 真实 uvicorn 永远不会产生这个名字。留着它是为了让单元测试能走
#: 中间件，而不是把中间件在测试里禁用掉（那样就测不到它了）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

API_PREFIX = "/api/v1"


def _pick_free_port() -> int:
    """让操作系统分配一个空闲端口。

    为什么不用固定端口：本机可能同时跑着别的实例（开发/测试），
    固定端口会撞车，而"撞车后自动 +1 重试"又会带来竞态。
    让 OS 分配（bind 0）是唯一无竞态的做法，代价是必须把
    实际端口写进文件告诉前端 —— 这就是 `port` 文件的存在理由。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def create_app(settings: Settings | None = None) -> Any:
    """构造 FastAPI 应用。

    `settings` 可注入是为了测试：测试用临时目录的配置，
    避免碰到真实的 `data/` 库。
    """
    cfg = settings or get_settings()

    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.responses import Response

    # C11 兜底断言：配置层已经校验过 host，这里再断一次。
    # 理由：配置能被环境变量覆盖，而这是**合规红线**，
    # 宁可启动失败也不能默默对外监听。
    assert cfg.app.host in LOOPBACK_HOSTS, (
        f"app.host={cfg.app.host!r} 不是回环地址，拒绝启动（C11）"
    )

    @asynccontextmanager
    async def lifespan(app):
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(app.state.runtime.gate_service().recover_pending_decisions)
        yield

    app = FastAPI(
        lifespan=lifespan,
        title="mmwf",
        version=cfg.app.version,
        # 三个都关掉：离线环境不需要文档页，且它等于一份"接口说明书"
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # -- ① 仅回环 + 追踪号注入 -------------------------------------------
    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        if client not in LOOPBACK_HOSTS:
            logger.warning("拒绝非回环请求：client=%s path=%s", client, request.url.path)
            # 非回环从**没有** trace_id（请求没进业务），但形态要一致 ——
            # 仍然给一个编号，前端不必为此写分支。
            return JSONResponse(
                env.fail("E6002", _user_message("E6002"), trace_id=env.new_trace_id()),
                status_code=200,
            )
        # 每个请求一个追踪号，挂在 state 上供异常处理器与下面回填时取用
        trace = env.new_trace_id()
        request.state.trace_id = trace
        response = await call_next(request)
        # 透传到响应头，方便前端在控制台里看到编号
        response.headers["X-MMWF-Trace"] = trace

        # ★ 回填到成功信封的 body 里（T1-11 Review 补）。
        #
        # 为什么必须回填：响应头是**给开发者看**的，而 `trace_id` 的设计
        # 用途是"念给用户听"（界面要显示这个编号，用户报给运维）。
        # 第一版只有错误路径（走异常处理器）在 body 里带了 trace_id，
        # **成功路径的 body 里是 `null`** —— 于是前端要么读 header
        # （跨域/代理下不总能读到），要么写成两条分支。
        # 那正是"统一信封"要消灭的东西，所以在这里补齐，
        # 让 `ok=true` 与 `ok=false` 的 body 形态完全一致。
        #
        # ⚠️⚠️ **第一版这段是死代码，实测抓出来的**（T1-11 Review 第二次修正）
        #
        # 第一版写的是 `getattr(response, "body", None)` —— 对
        # `JSONResponse` 成立，但端点返回的是 `env.ok(...)` 的返回值，
        # 它是**普通 `dict`**：FastAPI 把它交给 `jsonable_encoder` 校验后
        # 包进 Starlette 的 `_AsyncLazyJSONResponse` —— 那个类有
        # `body_iterator`，**没有 `.body`**。于是 `body` 恒为 `None`，
        # 整个 `if body:` 分支**一次都没进过**：白写一段代码，
        # 而且"成功信封 trace_id 非空"因此一直是红的（实测全 `None`）。
        return await _backfill_trace(response, trace)

    async def _backfill_trace(response: Any, trace: str) -> Any:
        """把 `trace_id` 回填进成功信封的 body；不需要改写时**原样返回**。

        抽成嵌套函数，是为了让中间件主体只剩"三道门"的主线逻辑。
        """
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response
        raw = await _peek_body(response)
        if raw is None:
            return response
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return response
        # 只回填**自家信封**（有 ok/code 两个键），
        # 别的 JSON 响应（如 Starlette 内部的）不动它。
        if not (isinstance(payload, dict) and {"ok", "code"} <= set(payload)):
            return response
        if payload.get("trace_id") is not None:
            return response
        payload["trace_id"] = trace
        new_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = dict(response.headers)
        headers["content-length"] = str(len(new_body))
        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type="application/json",
        )

    async def _peek_body(response: Any) -> bytes | None:
        """读出响应体字节；读不出来时返回 `None`（那就别改它）。

        **三种形态都要覆盖**（都是实测得到的，别凭印象写）：
          · `bytes`        —— `_AsyncLazyJSONResponse` 收 dict/list 时
          · 异步生成器      —— 同一个类收 BaseModel 时
          · 同步迭代器      —— 一般的 `Response`
        另外中间件自己造的 `JSONResponse`（如 E6002）有 `.body`，先看它。
        """
        direct = getattr(response, "body", None)
        if isinstance(direct, bytes):
            return direct
        it = getattr(response, "body_iterator", None)
        if it is None:
            return None
        if isinstance(it, (bytes, bytearray)):
            return bytes(it)
        chunks: list[bytes] = []
        try:
            if hasattr(it, "__aiter__"):
                async for chunk in it:
                    chunks.append(chunk)
            else:
                for chunk in it:
                    chunks.append(chunk)
        except Exception:  # pragma: no cover - 流式响应读不了就不改它
            logger.debug("响应体不可重读，跳过 trace_id 回填", exc_info=True)
            return None
        merged = b"".join(
            c if isinstance(c, bytes) else str(c).encode("utf-8") for c in chunks
        )
        # ⚠️ 生成器**已被消费**，必须把字节放回去：否则把响应交还给
        # Starlette 时它会从已耗尽的生成器里读 —— **响应体凭空变成空**。
        # 这个坑很隐蔽，因为它只在"走生成器分支"的响应上出现。
        response.body_iterator = _single_chunk(merged)
        return merged

    async def _single_chunk(data: bytes):
        """把字节重新包成一个异步生成器（供放回 `body_iterator`）。"""
        yield data

    # -- ② 异常 → 信封（三类来源）--------------------------------------
    #
    # ⚠️ 必须**同时**注册 `MmwfError` 和 `Exception` 两个处理器，不能只写后者。
    # 原因（踩过的坑）：Starlette 的 `Exception` 兜底处理器**不会捕获
    # 依赖（Depends）里抛出的异常** —— 依赖在路由函数之前、
    # 在另一层调用栈里执行，异常不经过路由函数的 try 边界。
    # 只注册 `Exception` 时，`require_token` 抛的 E6001 会**原样冒出去**，
    # 用户看到的是 500 + 堆栈。所以业务异常要单独注册一个处理器。
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        """路由层错误（404 / 405 等）→ 信封（T1-11 Review 补）。

        ★ **这是"统一 200 + 信封"最容易漏的一条，而且我第一版就漏了。**

        为什么 `Exception` 与 `MmwfError` 两个处理器都盖不住它：
        路径匹配失败（404）与方法不匹配（405）是在 **Starlette 的路由层**
        决定的 —— 那时还没有任何 endpoint 函数被调用，
        Starlette 直接抛自己的 `HTTPException`，由它自带的
        `http_exception_handler` 渲染成 `{"detail": "Not Found"}` + 404。

        后果：前端按信封解析会**当场崩**（拿到的是 `{"detail": ...}`，
        既没有 `ok` 也没有 `code`），而且 HTTP 状态码也不是约定的 200。

        映射规则：
          · 404 → E1002（找不到要处理的文件）—— 用户视角就是"找不到"
          · 405 → E1001（提交的内容格式不正确）—— 方法不对属于请求不合法
          · 其他 → E9001 兜底，不把 detail 暴露出去

        ⚠️ **例外：被刻意关闭的文档路由必须保持真 404**（本处理器不接管）。

        由这条处理器把 `/docs` 也变成"200 + 信封"，会让保密检查过不去：
        `docs_url=None` 只做到"没有这页"，而探测脚本看的是
        **HTTP 状态码** —— 一个 200 会被记成"文档页开着"。
        这样"关闭文档"这道门就成了摆设：状态码层面看不出区别。

        所以这里显式放行这类路径，让 Starlette 的原生 404 透出去。
        判据是**前缀**（`/api/v1` 之外的一律不接管）：宁可让某个未来
        新增的非 API 路由返回原生 404（前端对非 API 路径本来也不该
        按信封解析），也不能让"文档已关闭"这件事在证据上失去可辨识度。
        """
        trace = getattr(request.state, "trace_id", None)
        if not request.url.path.startswith(API_PREFIX):
            logger.info(
                "非 API 路径的原生 %s：%s（保持状态码，不转信封）",
                exc.status_code, request.url.path,
            )
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )

        code = {
            404: "E1002",
            405: "E1001",
        }.get(exc.status_code, "E9001")
        logger.info(
            "路由层错误：%s %s -> %s（映射 %s）",
            request.method, request.url.path, exc.status_code, code,
        )
        return JSONResponse(
            env.fail(code, _user_message(code), trace_id=trace),
            status_code=200,
        )

    @app.exception_handler(MmwfError)
    async def _business(request: Request, exc: MmwfError):
        trace = getattr(request.state, "trace_id", None)
        return JSONResponse(env.from_error(exc, trace_id=trace), status_code=200)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        """兜底处理器。**绝不把堆栈放进响应体**（N3.3 / SAD §9.3 严禁项）。

        它的存在价值是"最后一道防线"：任何没被路由内 try 捕获的异常
        到这里都会变成合法信封。如果没有它，FastAPI 会返回
        `500 Internal Server Error` + 纯文本 —— 前端解析失败直接白屏。
        """
        trace = getattr(request.state, "trace_id", None)
        return JSONResponse(env.from_exception(exc, trace_id=trace), status_code=200)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        """入参校验失败。

        用函数名而不是 `raise HTTPException(422)` —— 那会返回 422，
        破坏"统一 200 + 信封"。这里把它转成 E1001 系的输入错误文案，
        并把字段级细节放进 `data`（**不含用户输入的值本身**，
        避免把可能涉密的内容回显到响应里）。
        """
        trace = getattr(request.state, "trace_id", None)
        fields = [
            {"loc": [str(p) for p in e.get("loc", [])], "type": e.get("type", "")}
            for e in exc.errors()
        ]
        logger.info("入参校验失败：%s", fields)
        return JSONResponse(
            env.fail(
                "E1001",
                "提交的内容格式不正确，请检查后重试。",
                {"fields": fields},
                trace_id=trace,
            ),
            status_code=200,
        )

    _migrate_once(cfg)
    _register_routes(app, cfg)
    return app


#: 已迁移过的库路径（模块级，跨 `create_app` 调用共享）。
#:
#: ⚠️ 为什么要做"只迁移一次"的缓存（T1-11 Review 补的严重缺陷）：
#:
#: 在 `create_app()` 里跑迁移本身是**必须**的 —— 否则全新安装的机器上
#: `data/` 目录还没有 `mmwf.db`，第一个请求就会撞上
#: `sqlite3.OperationalError: no such table: meeting`，被包成 **E9001**。
#: 那不是"边角情况"：**全新安装 = 每一个用户的第一次使用**。
#: 而且症状会指向完全错误的方向（"程序遇到未预期的问题"，
#: 而真相是"数据库还没建"）。
#:
#: 但迁移不能每次 `create_app()` 都跑：`tests/unit/test_api.py` 每个用例
#: 建一次 app，1800 多个用例就是 1800 多次迁移（每次都要开库、
#: 读 schema_version、逐个比对 DDL），会把回归时间拖垮。
#: 缓存键用**库文件的绝对路径**，所以每个临时库仍然各迁移一次。
_MIGRATED: set[str] = set()


def _migrate_once(cfg: Settings) -> None:
    """确保这个库已推到最新版本（幂等，但每个库只真跑一次）。

    迁移失败**不吞**：宁可启动失败也不要让用户撞上"表不存在"。
    """
    from mmwf.infra.migrations.runner import migrate

    db_path = cfg.resolve_path(cfg.app.data_dir) / "mmwf.db"
    key = str(db_path)
    if key in _MIGRATED:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrate(db_path)
    _MIGRATED.add(key)
    logger.info("数据库已就绪：%s", db_path)


def _user_message(code: str) -> str:
    """从码表取用户文案（中间件里拿不到 MmwfError 实例，只能按码取）。"""
    from mmwf.domain.errors import ALL_SPECS

    spec = ALL_SPECS.get(code)
    return spec.user_message if spec else "操作没有完成，请重试。"


def _register_routes(app: Any, cfg: Settings) -> None:
    """挂载路由表。

    单独抽成函数，是为了 `create_app()` 读起来只有"三道门"，
    路由细节不干扰主线。见 `api/routes/`。
    """
    from mmwf.api.routes import build_router

    from mmwf.api.routes._context import Runtime

    rt = Runtime(cfg)
    app.state.runtime = rt
    app.include_router(build_router(cfg, runtime=rt), prefix=API_PREFIX)


def main(argv: list[str] | None = None) -> int:
    """启动服务（`python -m mmwf.main`）。

    顺序很重要：
        1. 建令牌文件 —— 前端轮询它判断"后端好了没"，
           所以要**在监听之前**写好，否则前端可能先看到端口文件、
           再读令牌时扑空。
        2. 选端口、写端口文件。
        3. 才 uvicorn.run()。

    先把两个文件都落好再监听，前端就只会观察到"两个文件都在 = 就绪"，
    不会看到中间态。
    """
    import uvicorn

    from mmwf.settings import load_settings

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_settings()
    assert cfg.app.host in LOOPBACK_HOSTS, "C11：只允许回环地址"

    token = api_deps.create_session_token()
    port = cfg.app.port or _pick_free_port()
    api_deps.write_port(port)

    logger.info("mmwf 后端就绪：http://%s:%d%s", cfg.app.host, port, API_PREFIX)
    logger.info("本次会话令牌已写入：%s", api_deps.token_path())

    try:
        uvicorn.run(
            create_app(cfg),
            host="127.0.0.1",       # 硬编码回环，不用 cfg.app.host 的别名形式
            port=port,
            log_level="info",
            access_log=False,        # 访问日志会记录路径，含会议 id；改由审计日志承担
        )
    finally:
        # 退出时清掉令牌：进程没了，令牌就不该还能用。
        api_deps.clear_session_files()
    return 0


#: 供 `python -m mmwf.main` 使用
if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())


__all__ = ["API_PREFIX", "LOOPBACK_HOSTS", "create_app", "main"]
