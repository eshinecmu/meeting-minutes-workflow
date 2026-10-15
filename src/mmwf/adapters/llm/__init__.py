"""大模型客户端注册表（T2-01）。

与 `adapters/asr/__init__.py` 同构：名字 -> 工厂函数，
延迟构造，重复注册报错（不静默覆盖）。

新增一个 LLM 引擎的步骤（对应 SAD 附录 C 的同款流程）：

    1. 在 `adapters/llm/` 下新建 `<engine>.py`
    2. 实现 `LlmClient` 的三个方法：`available()` / `health_check()` / `chat()`
    3. **在本模块注册**
    4. 在 `config/models.yaml` 中把 `llm.provider` 指向新引擎名
    5. **不得**修改上层任何代码
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from mmwf.adapters.llm.base import LlmClient, LlmError, LlmResponse
from mmwf.adapters.llm.fake_client import FakeLlmClient
from mmwf.adapters.llm.ollama_client import PROVIDER_NAME as _OLLAMA_NAME
from mmwf.adapters.llm.ollama_client import OllamaClient, make_from_settings

logger = logging.getLogger(__name__)

#: provider 名字 -> 工厂函数。
_REGISTRY: dict[str, Callable[..., LlmClient]] = {
    _OLLAMA_NAME: OllamaClient,
    FakeLlmClient.name: FakeLlmClient,
}


def register(name: str, factory: Callable[..., LlmClient], *,
             override: bool = False) -> None:
    """注册客户端。

    `override=False`（默认）时重复注册报错 —— 静默覆盖会让
    "两个模块注册了同一个名字"极难排查（谁生效取决于 import 顺序）。
    """
    if name in _REGISTRY and not override:
        raise LlmError(
            f"大模型客户端 {name!r} 已注册（现有：{sorted(_REGISTRY)}）。"
            "若确实要替换，显式传 override=True。"
        )
    _REGISTRY[name] = factory
    logger.info("已注册大模型客户端：%s", name)


def unregister(name: str) -> None:
    """移除注册（供测试隔离用）。"""
    _REGISTRY.pop(name, None)


def available_providers() -> list[str]:
    """已注册的名字（不实例化）。"""
    return sorted(_REGISTRY)


def build(name: str, **kwargs: Any) -> LlmClient:
    """按名字构造客户端。"""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise LlmError(
            f"未注册的大模型客户端：{name!r}。已注册：{sorted(_REGISTRY)}。"
        )
    return factory(**kwargs)


def build_from_settings(settings: Any) -> LlmClient:
    """按 `config/models.yaml` 的 `llm` 段构造（推荐的生产入口）。

    这样上层只写 `build_from_settings(settings)`，**换引擎只改 yaml**。
    """
    llm = settings.llm
    if llm.provider == _OLLAMA_NAME:
        return make_from_settings(settings)
    return build(llm.provider)


def make_llm_client(settings: Any) -> LlmClient:
    """按配置构造客户端；此时不做健康探测或建立连接。"""
    return build_from_settings(settings)


__all__ = [
    "FakeLlmClient",
    "LlmClient",
    "LlmError",
    "LlmResponse",
    "OllamaClient",
    "available_providers",
    "build",
    "build_from_settings",
    "make_llm_client",
    "register",
    "unregister",
]
