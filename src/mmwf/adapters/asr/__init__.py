"""转写引擎注册表（T1-05）。

SAD 附录 C 规定的"新增识别引擎的标准步骤"：
    1. 在 `adapters/asr/` 下新建 `<engine>.py`
    2. 实现 `AsrProvider` 的四个方法
    3. **在本模块注册**          ← 本文件
    4. 在 `config/models.yaml` 中把 `asr.provider` 指向新引擎名
    5. 跑 `tests/contract/test_asr_contract.py`
    6. **不得**修改上层任何代码

注册表做成"名字 -> 工厂函数"而不是"名字 -> 类"
-----------------------------------------------
因为契约测试需要对**全部 provider 参数化**（测试方案 §3），而
"构造一个 provider"所需的参数各引擎不同（FunASR 要 model_dir，
未来的引擎可能只要 API 地址）。用工厂函数把差异收在注册表里，
测试只需要 `build(name)` 一行。

另外工厂延迟到调用时才构造，也让"只想知道有哪些引擎"这件事
不必付出实例化代价（FunASR 的构造函数本身很轻，但依赖可能在
`available()` 里才被探测）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mmwf.adapters.asr.base import AsrProvider, AsrProviderError
from mmwf.adapters.asr.funasr_local import PROVIDER_NAME as _FUNASR_NAME
from mmwf.adapters.asr.funasr_local import FunASRLocalProvider

logger = logging.getLogger(__name__)

#: provider 名字 -> 工厂函数。工厂接受 kwargs 并返回 AsrProvider。
_REGISTRY: dict[str, Callable[..., AsrProvider]] = {
    _FUNASR_NAME: FunASRLocalProvider,
}


def register(name: str, factory: Callable[..., AsrProvider], *, override: bool = False) -> None:
    """注册引擎。

    `override=False`（默认）时重复注册会报错 —— 静默覆盖会让
    "两个模块注册了同一个名字"这种问题极难排查（谁生效取决于 import 顺序）。
    """
    if name in _REGISTRY and not override:
        raise AsrProviderError(
            f"转写引擎 {name!r} 已注册（现有：{sorted(_REGISTRY)}）。"
            "若确实要替换，显式传 override=True。"
        )
    _REGISTRY[name] = factory
    logger.info("已注册转写引擎：%s", name)


def unregister(name: str) -> None:
    """移除注册（供测试隔离用）。"""
    _REGISTRY.pop(name, None)


def available_providers() -> list[str]:
    """已注册的引擎名（不实例化）。"""
    return sorted(_REGISTRY)


def build(name: str, **kwargs: Any) -> AsrProvider:
    """按名字构造引擎。

    返回类型标注为 Protocol，但运行时**不做 isinstance 检查**——
    `runtime_checkable` 的 Protocol 只校验方法名存在，不校验签名，
    给出的是"看起来通过"的假安心。真正的接口一致性由
    `tests/contract/test_asr_contract.py` 用行为断言来保证。
    """
    factory = _REGISTRY.get(name)
    if factory is None:
        raise AsrProviderError(
            f"未注册的转写引擎：{name!r}。已注册：{sorted(_REGISTRY)}。"
            f"新增引擎请见 SAD 附录 C。"
        )
    return factory(**kwargs)


def build_from_settings(settings: Any) -> AsrProvider:
    """按 `config/models.yaml` 的 `asr` 段构造（推荐的生产入口）。

    这样上层永远只写 `build_from_settings(get_settings())`，
    **换引擎只改 yaml**，符合"N5.2 配置可外部修改，无需改代码"。
    """
    asr = settings.asr
    return build(
        asr.provider,
        model_name=asr.model,
        model_dir=Path(asr.model_dir),
        device=asr.device,
    )


def health_of_all() -> dict[str, dict[str, Any]]:
    """对全部注册引擎跑一次健康检查（`/system/selfcheck` 用）。

    **单个引擎探测失败不影响其他引擎**：自检的价值在于"报出哪里坏了"，
    如果探测本身抛异常把整个自检搞崩，就失去了意义。
    """
    out: dict[str, dict[str, Any]] = {}
    for name in available_providers():
        try:
            provider = build(name)
            out[name] = provider.health_check()
        except Exception as e:  # noqa: BLE001 - 自检必须容错
            out[name] = {
                "available": False,
                "status": "probe_failed",
                "error": f"{type(e).__name__}: {e}",
            }
    return out


__all__ = [
    "AsrProvider",
    "AsrProviderError",
    "FunASRLocalProvider",
    "available_providers",
    "build",
    "build_from_settings",
    "health_of_all",
    "register",
    "unregister",
]
