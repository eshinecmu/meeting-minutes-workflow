"""大模型客户端接口契约（T2-01）。

与 `adapters/asr/base.py` 同构：**Protocol 而非 ABC**。
理由一样 —— 换引擎/换实现不该产生 import 耦合，
测试替身（`FakeLlmClient`）也不需要 import 本模块。

三个方法的语义边界
------------------
`available()`    —— **只探测、不加载**。查进程在不在、端口通不通。
                    绝不在这里触发一次真实推理：它会被自检频繁调用，
                    而一次 14B 模型的推理要几十秒。
`health_check()` —— 返回**结构化**健康状况，供 `/system/selfcheck` 直接嵌入。
`chat()`         —— 真正干活。

★ `chat()` 的 `json_schema` 参数是本项目的关键
----------------------------------------------
SAD §10.2 明写：

    Ollama 侧使用 `format` 参数传入 JSON Schema
    （本地方案中这是唯一能保证结构合规又完全离线的路径）

所以"约束解码"必须由**适配器**承担（它才知道怎么把 schema 传给具体引擎），
而**业务层的 `MinutesSchema.model_validate()` 校验层照样要过** ——
约束解码保证模式合规，**不保证业务逻辑正确**（SAD §10.2 原文）。
两层都在，缺一不可。

★ 为什么用 `urllib.request` 而不是 `httpx` / `requests`
-------------------------------------------------------
本项目**离线优先**：依赖全部离线 wheel，`pyproject.toml` 只有
pydantic / pyyaml / cryptography 三个直接依赖。
`httpx` 虽然在本机装着，但它是**测试工具链的传递依赖**，不是项目依赖 ——
为了一个本机回环 HTTP 调用把它提升为直接依赖，要多 vendoring 一个 wheel，
不值得。`urllib.request` 是标准库，零成本。

代价：没有连接池、没有自动重试。对"本机回环 + 单任务"的场景够用；
真要换 httpx，只需改 `ollama_client.py` 一个文件（协议不变）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mmwf.domain.errors import MmwfError

__all__ = ["LlmClient", "LlmError", "LlmResponse"]


class LlmError(MmwfError, RuntimeError):
    """携带 §9.3 业务码的适配器错误；detail 仅供日志，用户文案取自码表。

    保留 RuntimeError 兼容现有适配器调用方。
    """

    def __init__(self, detail: str = "", *, code: str = "E3004") -> None:
        super().__init__(code=code, detail=detail)


class LlmResponse:
    """一次推理的返回。

    为什么不直接返回 `str`：调用方需要知道**用了多久**（N1.2 的耗时门槛）
    和**是不是被截断了**（`done_reason != "stop"` 时输出不完整，
    拿去解析 schema 会得到一个莫名其妙的校验失败 —— 而真因是截断）。
    把这两件事随结果一起返回，比让调用方另开接口去问要可靠。
    """

    __slots__ = ("text", "elapsed_ms", "done_reason", "raw")

    def __init__(self, text: str, *, elapsed_ms: int = 0,
                 done_reason: str = "stop", raw: Any = None) -> None:
        self.text = text
        self.elapsed_ms = elapsed_ms
        self.done_reason = done_reason
        self.raw = raw

    @property
    def truncated(self) -> bool:
        """输出是否被截断（长度上限/超时）。截断的输出**不要**拿去解析。"""
        return self.done_reason not in ("stop", "")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"LlmResponse(len={len(self.text)}, "
                f"{self.elapsed_ms}ms, done={self.done_reason!r})")


@runtime_checkable
class LlmClient(Protocol):
    """大模型客户端接口。实现类只需要"长得像"。

    最小实现示例::

        class MyClient:
            name = "my_llm"

            def available(self) -> bool: ...
            def health_check(self) -> dict: ...
            def chat(self, prompt, *, system=None, json_schema=None,
                     temperature=None) -> LlmResponse: ...
    """

    #: 标识，与 `config/models.yaml` 的 `llm.provider` 取值一致。
    name: str

    def available(self) -> bool:
        """服务是否可用。**不得触发真实推理。**"""

    def health_check(self) -> dict[str, Any]:
        """结构化健康状况（`/system/selfcheck` 用）。"""

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        timeout_seconds: int | None = None,
    ) -> LlmResponse:
        """单轮对话。

        Args:
            prompt: 用户消息。
            system: 系统消息（约束"只抽不编"）。
            json_schema: ★ 传入时要求引擎**约束解码**（Ollama 的 `format`）。
            temperature: 覆盖默认温度。
            timeout_seconds: 覆盖默认超时。
        """
