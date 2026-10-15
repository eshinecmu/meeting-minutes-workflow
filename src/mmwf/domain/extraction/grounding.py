"""溯源绑定（SAD §10.3）—— **本卡只做精确匹配的最小版**。

本卡范围与 T2-04 的分工
------------------------
SAD §10.3 定义了三层：

    1. 精确匹配（容忍标点差异）
    2. 精确匹配失败 → 模糊匹配（滑动窗口 + 编辑距离），**confidence 降级并标注**
    3. 完全匹配不到 → 标记 `ungrounded`，**禁止进入最终定稿**

| 层 | 归谁 | 为什么 |
|:--|:---|:---|
| 第 1 层（精确 + 标点归一化） | **T2-01（本模块）** | T2-01 验收明写"quote 能在原文中**精确**匹配" |
| 第 2 层（模糊降级） | **T2-04** | 属 F3.8 后置校验，要和"问题清单"一起设计 |
| 第 3 层（`ungrounded` 硬拦截） | **T2-04** | 硬拦截是**流程决策**，不是领域计算 |

⚠️ **本卡匹配不到时只标记 `matched=None`，不拦截、不降级**。
调用方（`extractor`）把这类条目原样放进结果并记一条 issue，
**由 T2-04 决定怎么处置**。提前拦截会让本卡的验收脚本无法区分
"溯源算法错了"和"拦截策略生效了"。

标点归一化（SAD §10.3 第 1 层的"容忍标点差异"）
------------------------------------------------
中英标点互转 + 去空白 + 统一引号。为什么必须做：
转写引擎输出的标点与模型抄回来的标点**经常不一致**
（全角/半角、`，`/`,`、有无空格），如果只做逐字相等，
大量**实际正确**的引用会被判为"匹配不到"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from mmwf.domain.extraction.segmentize import Segment

__all__ = [
    "MATCH_EXACT",
    "MATCH_NONE",
    "MATCH_NORMALIZED",
    "GroundingResult",
    "bind_grounding_exact",
    "normalize_punct",
]

MATCH_EXACT = "exact"
MATCH_NORMALIZED = "normalized"
MATCH_NONE = None

#: 标点/空白归一化映射。**全角 → 半角**统一，而不是反过来 ——
#: 半角集合更小、更稳定，且不会引入中文字符。
_PUNCT_MAP = {
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "、": ",", "（": "(", "）": ")", "【": "[", "】": "]",
    "《": "<", "》": ">", "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "－": "-", "～": "~", "…": "...",
}

#: 所有空白（含全角空格）。
_WS_RE = re.compile(r"\s+")


def normalize_punct(text: str) -> str:
    """标点归一化：全角→半角、去空白、统一引号。

    只用于**比较**，不用于输出 —— 原文一个字都不能改。
    """
    s = text or ""
    for k, v in _PUNCT_MAP.items():
        s = s.replace(k, v)
    s = _WS_RE.sub("", s)
    return s


@dataclass(frozen=True)
class GroundingResult:
    """一条引用与原文的绑定结果。

    Attributes:
        matched: `"exact"` / `"normalized"` / `None`。
        ref: 命中的 ref 字典（`segment_id` / `start_ms` / `text` / `quote`）；
            未命中为 `None`。
        confidence: 绑定置信度。精确 = 1.0；归一化命中 = 0.95
            （**降级**：标点不同意味着不是逐字相同，SAD §10.3 要求降级标注）；
            未命中 = 0.0。
    """

    matched: str | None
    ref: dict[str, Any] | None
    confidence: float

    @property
    def ok(self) -> bool:
        return self.matched is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "ref": self.ref,
            "confidence": self.confidence,
        }


def _make_ref(seg: Segment, quote: str) -> dict[str, Any]:
    return {
        "segment_id": f"seg-{seg.seq}",
        "start_ms": seg.start_ms,
        "end_ms": seg.end_ms,
        "text": seg.text,
        "quote": quote,
        "speaker": seg.speaker,
    }


def bind_grounding_exact(
    quote: str,
    segments: Sequence[Segment],
) -> GroundingResult:
    """把一条 `source_quote` 绑到原文段落上（**只做精确 + 标点归一化**）。

    先做**逐字**匹配（`matched="exact"`, confidence 1.0），
    失败再退到**标点归一化**后匹配（`matched="normalized"`, confidence 0.95），
    都失败返回 `matched=None`（**不拦截**，见模块 docstring）。

    ⚠️ 逐字匹配优先于归一化匹配，这个顺序不能反：
    归一化会抹掉标点差异，若先做归一化，就永远拿不到 `"exact"` 这个
    **更强**的结论 —— 而"这条引用是逐字抄的"与"标点对不上但内容一致"
    在验收上是两回事（U-GR-01 vs U-GR-02）。
    """
    q = (quote or "").strip()
    if not q or not segments:
        return GroundingResult(matched=MATCH_NONE, ref=None, confidence=0.0)

    # ---- 第 1 层：逐字 ----
    for seg in segments:
        if q in seg.text:
            return GroundingResult(MATCH_EXACT, _make_ref(seg, q), 1.0)

    # ---- 第 2 层：标点归一化 ----
    nq = normalize_punct(q)
    if nq:
        for seg in segments:
            if nq in normalize_punct(seg.text):
                return GroundingResult(MATCH_NORMALIZED, _make_ref(seg, q), 0.95)

    # ---- 都不中：只标记，不拦截（第 2/3 层属 T2-04）----
    return GroundingResult(matched=MATCH_NONE, ref=None, confidence=0.0)
