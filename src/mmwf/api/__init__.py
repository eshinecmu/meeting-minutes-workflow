"""API 层（T1-11）。

本层是**唯一**允许 import `fastapi` / `uvicorn` 的地方（SAD C01）。
领域层（`domain/`）只认标准库与纯算法依赖 —— 所以本层不能把
FastAPI 的类型（`Request`、`HTTPException`）漏给下层：下层要
报错就抛 `MmwfError`，由 `envelope` 与 `main.py` 的处理器转成信封。

模块分工：

    envelope.py   统一响应信封（SAD §6.3）—— ok/fail/from_exception
    deps.py       会话令牌与启动文件（端口、令牌）
    routes/       路由包：31 个端点，按资源分 7 个模块（详见 routes/__init__.py）
    ../main.py    应用装配、三道门、启动入口
"""

from mmwf.api.envelope import ApiResponse, fail, new_trace_id, ok

__all__ = ["ApiResponse", "fail", "new_trace_id", "ok"]
