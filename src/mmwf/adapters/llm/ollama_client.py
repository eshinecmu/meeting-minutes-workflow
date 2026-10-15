"""Ollama 本地大模型客户端（T2-01）。

对应 `config/models.yaml` 的 `llm.provider = "ollama_local"`。

接口用到的两个端点
------------------
| 方法 | 路径 | 用途 |
|:---|:---|:---|
| GET  | `/api/tags` | 列已加载模型 —— **健康检查用，不触发推理** |
| POST | `/api/chat` | 单轮对话；`format` 传 JSON Schema 走**约束解码** |

★ 为什么 `format` 是本项目的关键（SAD §10.2）
---------------------------------------------
本地离线方案里，`format` 是**唯一**能既保证结构合规又完全离线的路径。
云侧的 function-calling 不能用（红线：不出网），自己写解析器又不保证合规。
所以 `json_schema` 不是可选优化，是 F3.4 的实现手段。

★ 超时的处置
------------
`timeout_seconds` 默认取 `config/models.yaml` 的 600（10 分钟）。
超时抛 `LlmError` —— **由调用方决定是重试还是跳过该块**。
适配器**不自己重试**：块级重试策略属于编排层（`extractor`），
适配器重试会让"单块耗时"不可控（N1.2 要 ≤10 分钟总耗时）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from mmwf.adapters.llm.base import LlmError, LlmResponse

logger = logging.getLogger(__name__)

PROVIDER_NAME = "ollama_local"

#: 默认超时（秒）。与 `config/models.yaml` 的 `llm.timeout_seconds` 对齐。
DEFAULT_TIMEOUT_SECONDS = 600

#: 健康探测的超时。**必须远小于推理超时** —— 探测是"问一下在不在"，
#: 用 600 秒去问会让自检卡十分钟。
PROBE_TIMEOUT_SECONDS = 5


class OllamaClient:
    """Ollama HTTP 客户端（标准库实现，无第三方依赖）。"""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:14b-instruct-q5_K_M",
        temperature: float = 0.2,
        num_ctx: int = 8192,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # ★ 防御性校验：必须是本机地址。
        # `settings.LlmConfig` 已经有一道同样的校验，这里再来一道 ——
        # 因为客户端可以被**绕过配置**直接构造（测试、脚本）。
        # 离线的红线不该只靠"配置层记得校验"来守。
        if not any(h in base_url for h in ("127.0.0.1", "localhost", "::1")):
            raise LlmError(
                f"大模型地址必须是本机回环地址，当前为 {base_url!r}"
                "（本项目离线运行，不允许指向外部地址）"
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """服务在不在（**不触发推理**）。"""
        return bool(self.health_check().get("available"))

    def health_check(self) -> dict[str, Any]:
        """结构化健康状况（`/system/selfcheck` 直接嵌入）。

        任何异常都收敛成 `available=False` + `error` —— 自检的价值是
        "报出哪里坏了"，探测本身抛异常把自检搞崩就失去意义
        （与 `adapters/asr/__init__.py::health_of_all` 同一条原则）。
        """
        out: dict[str, Any] = {
            "available": False,
            "provider": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "models_loaded": [],
        }
        try:
            data = self._request_json(
                "GET", "/api/tags", None, timeout=PROBE_TIMEOUT_SECONDS
            )
            names = [m.get("name") for m in (data.get("models") or [])
                     if isinstance(m, dict)]
            out["models_loaded"] = names
            out["available"] = True
            out["model_present"] = any(
                str(n).split(":")[0] == self.model.split(":")[0] for n in names
            )
        except Exception as e:  # noqa: BLE001 - 自检必须容错
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        timeout_seconds: int | None = None,
    ) -> LlmResponse:
        """单轮对话。`json_schema` 非空时走约束解码。"""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        options: dict[str, Any] = {
            "temperature": self.temperature if temperature is None else temperature,
            "num_ctx": self.num_ctx,
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if json_schema is not None:
            # ★ SAD §10.2：这是保证结构合规的**唯一离线路径**
            body["format"] = json_schema

        t0 = time.monotonic()
        data = self._request_json(
            "POST", "/api/chat", body,
            timeout=timeout_seconds or self.timeout_seconds,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        msg = data.get("message") or {}
        content = str(msg.get("content") or "")
        done_reason = str(data.get("done_reason") or ("stop" if data.get("done") else ""))
        if not content:
            raise LlmError(
                f"Ollama 返回了空内容（model={self.model}, "
                f"done_reason={done_reason!r}）"
            )
        return LlmResponse(
            content, elapsed_ms=elapsed_ms, done_reason=done_reason, raw=data
        )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        timeout: int,
    ) -> dict[str, Any]:
        """发请求并解析 JSON。所有异常翻译成 `LlmError`。"""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise LlmError(
                f"Ollama 返回 HTTP {e.code}（{url}）：{detail}"
            ) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise LlmError("调用本地大模型超时", code="E3003") from e
            # 最常见的真因：Ollama 没启动 / 端口不对。
            # 把"连不上"翻译成人话，别让用户看到 `ConnectionRefusedError`。
            raise LlmError(
                f"无法连接本地大模型服务 {url}（{e.reason}）。"
                "请确认 Ollama 已启动且监听该端口。"
            ) from e
        except TimeoutError as e:
            raise LlmError(
                f"调用本地大模型超时（{timeout} 秒，{url}）。"
                "可调大 config/models.yaml 的 llm.timeout_seconds。", code="E3003"
            ) from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LlmError(f"Ollama 返回的不是合法 JSON：{raw[:200]!r}") from e


def make_from_settings(settings: Any) -> OllamaClient:
    """按 `config/models.yaml` 的 `llm` 段构造。"""
    llm = settings.llm
    return OllamaClient(
        base_url=llm.base_url,
        model=llm.model,
        temperature=llm.temperature,
        num_ctx=llm.num_ctx,
        timeout_seconds=llm.timeout_seconds,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "PROVIDER_NAME",
    "OllamaClient",
    "make_from_settings",
]
