"""路由表装配（T1-11 建立，T1-16 补到 30 个端点）。

★ **为什么拆包**：本文件原来叫 `api/routes.py`，单文件 **2495 行**，
其中 `build_router` 一个函数就占 1865 行、挂 30 个端点。

根子上的原因不是"写的人懒"，而是**闭包**：`cfg` / `db_path` / 八个服务
工厂 / 会议锁全靠 `build_router` 的闭包捕获，端点一旦搬出这个函数就
什么都访问不到 —— 所以它们只能挤在一起。那个文件的头注释里写着
"等剩余端点补齐后再按资源拆"，到 T1-16 端点补齐了，时机也就到了。

拆法（`_context.Runtime` 是关键一步）：

    _support.py    常量 + 无状态工具（不依赖 Runtime）
    _context.py    Runtime：把闭包捕获物显式化成实例属性与方法
    selfcheck.py   握手 / 自检 / 字体              3 个端点
    meetings.py    会议生命周期（主链路）           9 个端点
    transcript.py  转写稿 读/改/批量接受            3 个端点
    export.py      导出 / 撤销 / 差异 / 下载        5 个端点
    templates.py   模板 列表/新建/修改              3 个端点
    glossary.py    术语库                           6 个端点
    audit.py       审计 检索/报告                   2 个端点

**全部挂 `require_token`**。令牌校验写成**路由器级依赖**
（`APIRouter(dependencies=[...])`），不是每个函数再写一遍 ——
后者一定会漏，而漏掉的那个端点就是安全缺口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from mmwf.api.deps import verify_token
from mmwf.settings import Settings

from . import audit, export, glossary, meetings, selfcheck, templates, transcript
from ._context import Runtime
from ._support import (
    ALLOWED_DECISIONS,
    AUDIT_MAX_LIMIT,
    CLASSIFICATIONS,
    DEFAULT_OPERATOR,
    GATE_NODE_MAP,
    TRANSCRIPT_DEFAULT_LIMIT,
    TRANSCRIPT_MAX_LIMIT,
    _decision_message,
    _now,
)


def build_router(cfg: Settings) -> APIRouter:
    """构造路由表。`cfg` 用于解析数据目录与库路径。"""

    def require_token(
        x_mmwf_token: str | None = Header(default=None, alias="X-MMWF-Token"),
    ) -> str:
        """令牌校验（E6001）。

        用**声明式依赖**而不是中间件：中间件要自己判断"哪些路径豁免"
        （handshake 之外的都要查），凭豁免名单很容易漏；
        而挂成路由依赖时，新增端点**默认就是受保护的** ——
        安全的默认值比方便更重要。
        """
        return verify_token(x_mmwf_token)

    #: 整个路由器都要求令牌 —— 见上方 `require_token` 的说明。
    router = APIRouter(dependencies=[Depends(require_token)])
    rt = Runtime(cfg)

    # 注册顺序：只有 `export` 内部有约束（`export/undo` 必须在
    # `export/{file}` 之前，见 `export.py` 模块 docstring）。
    # 其余模块之间路径不冲突，这里的顺序只为"从握手到交付"的阅读顺序。
    selfcheck.register(router, rt)
    meetings.register(router, rt)
    transcript.register(router, rt)
    export.register(router, rt)
    templates.register(router, rt)
    glossary.register(router, rt)
    audit.register(router, rt)
    return router


__all__ = [
    "ALLOWED_DECISIONS",
    "AUDIT_MAX_LIMIT",
    "CLASSIFICATIONS",
    "DEFAULT_OPERATOR",
    "GATE_NODE_MAP",
    "TRANSCRIPT_DEFAULT_LIMIT",
    "TRANSCRIPT_MAX_LIMIT",
    "Runtime",
    "build_router",
]
