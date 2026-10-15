"""抽取与溯源共用的领域段落类型；不依赖领域子包。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    """分块用的最小单位（**领域层自己的类型**，不是 `infra` 的 `SegmentRow`）。

    为什么另立一个而不复用 `SegmentRow`：领域层不 import `infra`
    （`tools/check_layers.py` 的 C01 约束，全库 `domain/` 无一处 import `infra`）。
    而且这里只需要四个字段，`SegmentRow` 的 `confidence` / `flags` /
    `reviewed_*` 分块时一个都用不到。

    Attributes:
        seq: 从 1 开始的序号（**从 1 开始**，与 `SegmentRow.seq` 一致）。
        text: 正文，**已剥掉** `[时间码]` 与 `说话人：` 前缀。
        speaker: 说话人，抽不到为 `None`。
        start_ms: 起始毫秒；该行没有时间码时为 `None`。
        end_ms: 结束毫秒；**由下一段的 `start_ms` 推得**，末段为 `None`。
        gap_after_ms: 与**下一段**之间的静音时长（Markdown 输入为语速估计值）；末段或算不出时为 `None`。
    """

    seq: int
    text: str
    speaker: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    gap_after_ms: int | None = None

    @property
    def char_count(self) -> int:
        return len(self.text)
