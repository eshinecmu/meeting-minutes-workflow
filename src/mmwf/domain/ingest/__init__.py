"""音频接入与切分（T1-06）。

分层位置与本模块的边界
----------------------
对应 SAD §4 的 **M06 `ingest`**。但 M06 被标为"应用层"，
而本包放在 `domain/` —— 这个"不一致"是刻意的，理由如下：

    切分计划（`planner.py`）是**纯业务规则**，不含任何 IO；
    它越界/漏尾会把用户音频悄悄切错，是最需要被单测钉死的部分。
    放领域层 = 可以用最快的方式测它，且 `check_layers.py` 会强制它
    不引入 ffmpeg/subprocess 这类外部依赖。

真正的"外部能力调用"（ffprobe/ffmpeg）在 `mmwf.adapters.audio`；
把两者拼起来、写审计、更新状态的编排在 `mmwf.app.services.ingest`。

模块导航：
    planner      —— 格式白名单、切分计划、重叠归并（纯函数）
    audio_file   —— 音频文件的领域模型与校验结论
"""

from mmwf.domain.ingest.planner import (
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_OVERLAP_SECONDS,
    MAX_FILE_BYTES,
    SUPPORTED_EXTENSIONS,
    TARGET_CHANNELS,
    TARGET_SAMPLE_RATE,
    ChunkPlanItem,
    build_chunk_plan,
    merge_overlapping_texts,
    validate_extension,
)

__all__ = [
    "DEFAULT_CHUNK_SECONDS",
    "DEFAULT_OVERLAP_SECONDS",
    "MAX_FILE_BYTES",
    "SUPPORTED_EXTENSIONS",
    "TARGET_CHANNELS",
    "TARGET_SAMPLE_RATE",
    "ChunkPlanItem",
    "build_chunk_plan",
    "merge_overlapping_texts",
    "validate_extension",
]
