"""分块分层抽取领域模块（T2-01，SAD §10.1 / §10.2）。

本模块对外只暴露这一层 —— `graph/` 等编排层**只准 import 本包**，
不准 import `mmwf.domain.extraction.chunker` 这类内部模块
（SAD C01 的组合根规则，见 `tools/check_layers.py`）。

能力一览
--------
| 函数 | 职责 | SAD |
|:---|:---|:---|
| `extract_minutes` | **主入口**：分块 → 并行抽取 → 归并 → 补全 → 溯源 | §10.1 |
| `chunk_segments` | 只做分块（测试/调试单用） | §10.1 Step 1 |
| `segmentize` | 记录稿 Markdown → segments | 本卡裁决 D1 |
| `merge_chunk_results` | 只做归并（测试单用） | §10.1 Step 3 |
| `validate_minutes` | 模式校验层 | §10.2 |
| `bind_grounding_exact` | 精确溯源（最小版） | §10.3 第 1 层 |

边界（本卡不做，见施工卡 §1.2）
-------------------------------
模糊匹配降级、`ungrounded` 硬拦截、完整粒度控制、语体对齐 ——
分别属 T2-04 / T2-02。
"""

from __future__ import annotations

from mmwf.domain.extraction.chunker import Chunk, chunk_segments
from mmwf.domain.extraction.completeness import (
    MISSING_MARKER,
    REQUIRED_ELEMENTS,
    complete_missing,
)
from mmwf.domain.extraction.extractor import (
    DEFAULT_MAX_WORKERS,
    ExtractResult,
    extract_minutes,
)
from mmwf.domain.extraction.grounding import (
    GroundingResult,
    bind_grounding_exact,
    normalize_punct,
)
from mmwf.domain.extraction.merge import (
    MergeResult,
    MergeStats,
    merge_chunk_results,
    similarity,
)
from mmwf.domain.extraction.schemas import (
    SCHEMA_VERSION,
    ActionItemSchema,
    MinutesSchema,
    PendingDecisionSchema,
    ResolutionSchema,
    SchemaIssue,
    TopicSchema,
    minutes_json_schema,
    validate_minutes,
)
from mmwf.domain.extraction.segmentize import (
    Segment,
    SegmentizeReport,
    segmentize,
)

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "MISSING_MARKER",
    "REQUIRED_ELEMENTS",
    "SCHEMA_VERSION",
    "ActionItemSchema",
    "Chunk",
    "ExtractResult",
    "GroundingResult",
    "MergeResult",
    "MergeStats",
    "MinutesSchema",
    "PendingDecisionSchema",
    "ResolutionSchema",
    "SchemaIssue",
    "Segment",
    "SegmentizeReport",
    "TopicSchema",
    "bind_grounding_exact",
    "chunk_segments",
    "complete_missing",
    "extract_minutes",
    "merge_chunk_results",
    "minutes_json_schema",
    "normalize_punct",
    "segmentize",
    "similarity",
    "validate_minutes",
]
