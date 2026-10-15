"""转写引擎接口契约（T1-05）。

这是一个**协议**（`typing.Protocol`），不是基类。
为什么用 Protocol 而不是 ABC：

    SAD 附录 C 要求"新增识别引擎 = 新建一个文件 + 实现四个方法 + 注册"，
    **不得改动上层任何代码**。若用 ABC，新引擎必须 import 基类并继承，
    于是"接口定义"和"实现"就产生了 import 耦合；
    用 Protocol 则只要求**结构匹配**——任何有这四个方法的对象都算数，
    连测试替身（stub）都不需要 import 本模块。

四个方法的语义边界
------------------
`available()`    —— **只探测、不加载**。用于启动自检与界面提示。
                    绝不允许在这里加载模型：它会被 `/system/selfcheck`
                    频繁调用，而加载 220M 模型要几秒到几十秒。
`warmup()`       —— 显式加载。由编排层在"投递音频后、开始转写前"调用一次，
                    让用户看到"正在准备识别引擎"的进度，而不是在整个
                    转写过程中一直看到 0%。
`transcribe()`   —— 真正干活。**纯函数语义**：同样输入同样输出，
                    不依赖除 `audio_path`/`hotwords` 之外的任何隐藏状态。
                    这样重跑（幂等重试、断点恢复）才不会产生不同结果。
`health_check()` —— 返回**结构化**健康状况，供 `/system/selfcheck` 直接嵌入
                    响应（SAD §9.2 的 `asr_quality` 字段就是它的输出）。

`transcribe()` 为什么不接受"进度回调"
-------------------------------------
因为一期单机单任务、用户等待的是"分钟级"的转写。
进度条由**分块**（T1-06）而不是引擎内部回调来驱动——
块数已知，进度天然可算，且不必让每个引擎都实现回调协议。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mmwf.domain.asr_types import TranscriptResult


class AsrProviderError(RuntimeError):
    """适配器层的内部错误。

    ⚠️ 这不是给用户看的错误。适配器应当把引擎异常**翻译**成
    `mmwf.domain.errors` 里的 E2xxx 业务异常再抛出（见各 provider 实现）。
    本类只在"适配器自身写错了"（比如注册表里缺条目）时使用。
    """


@runtime_checkable
class AsrProvider(Protocol):
    """转写引擎接口。实现类只需要"长得像"，不需要继承任何东西。

    最小实现示例::

        class MyProvider:
            name = "my_engine"

            def available(self) -> bool: ...
            def warmup(self) -> None: ...
            def transcribe(self, audio_path, hotwords=None) -> TranscriptResult: ...
            def health_check(self) -> dict: ...
    """

    #: 引擎标识，与 `config/models.yaml` 的 `asr.provider` 取值一致。
    #: 定义为类属性而不是方法：注册表要能在**不实例化**的情况下读它。
    name: str

    def available(self) -> bool:
        """引擎是否可用（依赖已安装 + 模型文件存在）。**不得加载模型。**"""
        ...

    def warmup(self) -> None:
        """显式加载模型。可重复调用，第二次应快速返回。"""
        ...

    def transcribe(
        self,
        audio_path: Path | str,
        hotwords: list[str] | None = None,
    ) -> TranscriptResult:
        """转写音频。

        契约要求（`tests/contract/test_asr_contract.py` 自动验证）：
          · 返回 `TranscriptResult`
          · `segments` 非空
          · 每段 `start_ms <= end_ms`
          · `seq` 严格从 1 递增
          · 段起点单调不减
          · 失败时抛 `mmwf.domain.errors` 中的 E1xxx/E2xxx 业务异常

        `hotwords` 传**词表**而不是拼接好的字符串：不同引擎的参数格式
        不同（空格分隔 / JSON / 文件路径），拼接是引擎特有的知识，
        不该泄漏到调用方。
        """
        ...

    def health_check(self) -> dict[str, Any]:
        """结构化健康状况。必须含以下键（契约测试断言）：

            available          bool   依赖是否就绪
            model              str    模型名
            device             str    实际使用的设备（cpu / cuda:0）
            hotword_supported  bool   该版本是否支持热词
        """
        ...
