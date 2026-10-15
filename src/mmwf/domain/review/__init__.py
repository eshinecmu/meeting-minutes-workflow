"""转写稿复核与记录稿生成（T1-08，M07 `review`）。

对外只暴露三样东西，因为这一层是**纯函数**，没有状态、没有 IO：

    needs_review(seg, thresholds)      单个分段要不要送人工复核
    split_for_review(segments, ...)    把整篇分成「待复核」与「可直接用」两堆
    render_record(...)                 渲染结构化「会议记录.md」

为什么不在这里碰数据库/文件
--------------------------
SAD §4 的模块表把 M07 标为**应用层**，但那一栏指的是"低置信分流、逐段校对、
记录稿生成"这三件事**整体的职责归属**。真正落到代码上，其中"哪些段落要人看"
和"记录稿长什么样"是纯粹的判断与渲染 —— 它们是领域规则：

  · 换一个界面（Electron → 别的壳）不改变"低于 0.7 要复核"；
  · 换一个存储不改变"记录稿有八项元信息"。

把它们做成纯函数，才能在没有数据库、没有密钥、没有文件系统的情况下逐条单测
（本卡验收标准第 4 条就要求这么测）。凡是要读写的地方，都交给
`app/services/review.py`，那里才是本模块的"接线员"。
"""

from __future__ import annotations

from mmwf.domain.review.record import (
    METADATA_FIELDS,
    MISSING_METADATA_PLACEHOLDER,
    RENDER_SOURCE,
    RecordMetadata,
    RecordRenderError,
    ReviewItem,
    format_timestamp,
    join_names,
    render_record,
    split_for_review,
    summarize_review,
)
from mmwf.domain.review.split import (
    REVIEW_FLAGS,
    ReviewDecision,
    format_duration_ms,
    needs_review,
    plan_review,
    review_ratio,
    review_reason,
)

__all__ = [
    # split
    "REVIEW_FLAGS",
    "ReviewDecision",
    "format_duration_ms",
    "needs_review",
    "plan_review",
    "review_ratio",
    "review_reason",
    # record
    "METADATA_FIELDS",
    "MISSING_METADATA_PLACEHOLDER",
    "RENDER_SOURCE",
    "RecordMetadata",
    "RecordRenderError",
    "ReviewItem",
    "format_timestamp",
    "join_names",
    "render_record",
    "split_for_review",
    "summarize_review",
]
