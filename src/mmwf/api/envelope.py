"""统一响应信封（T1-11，SAD §6.3）。

为什么要有信封
--------------
Electron 前端只应该处理**一种**响应形态。如果一半接口用 HTTP 200 +
JSON、另一半用 401/500 加错误体，前端就要写两套解析、两套错误分支，
而且很容易漏掉某条分支导致"界面白屏但控制台没报错"。

所以本项目的约定是（SAD §6.3 原文）：

    > 所有 HTTP 响应（含 4xx/5xx）均返回 200 状态码 + 信封体内的
    > `ok/code`。理由：Electron 前端只需处理一种响应形态；
    > HTTP 状态码仅用于表达"服务不可达"这类传输层故障。

**这条约定是全项目最容易"半半拉愣"的地方**：只要有一个端点
写成 `raise HTTPException(status_code=400)`，前端就得同时处理两种形态。
所以本模块提供 `fail()` 与 `respond()`，并在 `main.py` 里把
**所有**异常（包括 FastAPI 自己的 `RequestValidationError`）
都收敛到信封 —— 见 `main.py` 的异常处理器。

`trace_id` 为什么留位
--------------------
用户看到的报错只有一句中文（N3.3 要求不含堆栈），那排查时怎么对得上日志？
靠 `trace_id`：界面显示编号，运维拿编号去 `app.log` 里捞完整堆栈。
一期先把它放在信封结构里并透传，日志侧在 T1-13 自检时打通。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from mmwf.domain.errors import MmwfError, Severity

logger = logging.getLogger(__name__)

#: 成功响应的业务码。没有用 "0" 或 "SUCCESS" 这类写法，因为
#: 失败码全是 `E\d{4}` 形状，成功的 "OK" 一眼就能和它们区分开。
CODE_OK = "OK"

#: 成功响应的默认提示语。中文、完整句、无术语（N3.3）。
DEFAULT_OK_MESSAGE = "操作成功"

#: 兜底的系统错误码。任何**没有被 `MmwfError` 覆盖**的异常都落到它。
#: 用 E9001（未预期错误）而不是 E9999：SAD §9.3 只登记到 E9xxx，
#: 自己发明 E9999 会让码表与文档对不上。
FALLBACK_CODE = "E9001"

#: 每个请求一个追踪号（8 位十六进制）。
#:
#: 为什么是 8 位而不是 UUID4 全串：这个值要**念给用户听**
#: （"请把界面上的编号告诉技术人员"），太长没人念得对。
#: 8 位十六进制 = 32 bit，在"单机单日几百次请求"的量级下
#: 碰撞概率可忽略，够用来在一份日志里唯一定位。
_TRACE_ID_BYTES = 4


def new_trace_id() -> str:
    """生成一个请求追踪号。用 `secrets` 而非 `random`：

    追踪号会出现在响应体里，若可预测则有人能拿它去**伪造**日志条目、
    或反推服务内部状态。本机场景风险低，但保密检查会看这一条。
    """
    return secrets.token_hex(_TRACE_ID_BYTES)


@dataclass
class ApiResponse:
    """统一响应信封（SAD §6.3 的形状，逐字段对齐）。

    字段顺序与文档一致，`asdict` 出来的 JSON 键序也一致 ——
    这样前端快照测试（C13 文案快照）能稳定比对。
    """

    ok: bool
    code: str
    message: str
    data: Any | None = None
    trace_id: str | None = None

    def dict(self) -> dict[str, Any]:
        """序列化成 dict。

        ⚠️ 不用 `dataclasses.asdict` —— 它会**递归**深拷贝，遇到
        非 dataclass 的嵌套对象（比如 ORM 行、Path、datetime）会直接抛错。
        信封的 `data` 是业务数据，形状由各端点自己保证可 JSON 序列化，
        这里只需浅拷贝。
        """
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "data": self.data,
            "trace_id": self.trace_id,
        }


def ok(
    data: Any = None,
    message: str = DEFAULT_OK_MESSAGE,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """成功信封。

    `data=None` 是合法的：`POST /export` 这类"做完即可"的接口
    不一定有返回值，前端只需看 `ok`。
    """
    return ApiResponse(True, CODE_OK, message, data, trace_id).dict()


def fail(
    code: str,
    message: str,
    data: Any = None,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """失败信封。

    `message` 必须是**面向使用者的完整中文句子**（N3.3）。
    技术细节（路径、SQL、堆栈、模型原始输出）一律**不进** `message`，
    它们只该出现在日志里（SAD §9.3 的"严禁"那一条）。
    """
    return ApiResponse(False, code, message, data, trace_id).dict()


def from_error(
    exc: MmwfError,
    *,
    data: Any = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """把业务异常转成失败信封。

    **只取 `user_message`，绝不取 `detail`** —— `MmwfError` 的设计
    就是这两个字段分工：`detail` 给日志，`user_message` 给用户。
    这里再守一道，免得将来有人在端点里图省事直接 `str(exc)`。
    """
    return fail(
        exc.code or FALLBACK_CODE,
        exc.user_message or "操作没有完成，请重试。",
        data,
        trace_id=trace_id,
    )


def from_exception(
    exc: BaseException,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """任意异常 → 失败信封（兜底路径）。

    分三种情况：

      · `MmwfError`   —— 走 `from_error()`，用它的业务码与用户文案；
      · 其他异常       —— 落 `FALLBACK_CODE`（E9001），并**在这里打日志**。

    为什么要在这里打日志而不是交给上层：兜底路径意味着"没人预期的异常"，
    而它的堆栈只此一处可得 —— 用户那边只能看到一句"程序出错了"。
    如果这里不打，异常就彻底消失了（信封会把它吞掉）。
    """
    if isinstance(exc, MmwfError):
        return from_error(exc, trace_id=trace_id)

    logger.exception("未预期异常已转为信封（code=%s）", FALLBACK_CODE)
    spec_message = "程序遇到一个未预期的问题，本次操作没有完成。请重试；若反复出现，请把界面上的编号告诉技术人员。"
    return fail(FALLBACK_CODE, spec_message, trace_id=trace_id)


def is_warning(exc: MmwfError) -> bool:
    """该业务异常是否只是警告级（不阻断）。

    T1-10 的 E4003（待确认项为空）与 T1-09 的 E5002（字体缺失）都是
    警告级：界面应当提示，但流程继续。端点用这个函数决定
    返回 `ok=True` 还是 `ok=False`。
    """
    return exc.severity is Severity.WARNING


__all__ = [
    "ApiResponse",
    "CODE_OK",
    "DEFAULT_OK_MESSAGE",
    "FALLBACK_CODE",
    "fail",
    "from_error",
    "from_exception",
    "is_warning",
    "new_trace_id",
    "ok",
]
