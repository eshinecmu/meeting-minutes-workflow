"""Step 1 分块：按语义边界把 segments 切成块（SAD §10.1，F3.2）。

四条边界判据（**优先级从高到低，但触发条件是"或"**）
----------------------------------------------------
| # | 判据 | 判据函数 |
|:--|:---|:---|
| ① | 说话人切换 | `segmentize.has_speaker_change` |
| ② | 段首 10 字含话题转折词 | `segmentize.starts_with_turn_word` |
| ③ | 长静音（`gap_after_ms > 1500`） | 本模块内联 |
| ④ | 字符数达标（`>= target_chars`） | 本模块内联 |

★ **切块条件是 `(④ 攒够字数) AND (①②③ 任一)**，不是 `OR`**
-----------------------------------------------------------
这是本模块最容易写错的一处，写错的后果很具体：

* 写成 `OR`（"遇到边界就切"）→ 说话人一换就切一块。2 万字文稿会碎成
  **几百块**，每块都调一次模型（耗时爆炸），而且块内上下文过短，
  抽取质量反而下降 —— 分块的**目的**（给模型足够上下文）就落空了。
* 写成 `AND` 但把 `④` 判在 `①②③` **之后** → 该切的地方因为字数不够
  一直不切，块会无限膨胀到超出 `max_chars`。

判据：一份正常的 2 万字记录稿，块数应落在 **10—20 块**。
若跑出 100+ 块，就是这个 `AND` 写反了（施工卡 §7 坑 1）。

重叠区（`overlap_chars`，SAD §10.1 / U-EX-05）
---------------------------------------------
相邻块之间保留约 200 字的重叠，**防止议题被切在边界上时两边都抽不全**。

★ **重叠按 segment 边界对齐，不按字符硬切。**
按字符取尾部 200 字会把一句话劈成两半；那句被劈开的文本若成为
`source_quote`，在原文里**精确匹配不到** —— 于是表现为"溯源失败"，
而真因是分块时把句子切坏了（施工卡 §7 坑 2）。
所以这里是"**从尾部往前取整段，直到凑够 overlap_chars**"，
宁可超过 200 字，也不劈句子。

单段超长（U-EX-04 的必要补充）
-------------------------------
若**一个 segment 自身**就超过 `max_chars`（有人一口气说了 3000 字且无标点），
块大小约束会直接失守。必须强制切分该段：切点选最近的句末标点，
找不到就硬切在 `max_chars` 处并记一条告警（`forced_splits`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mmwf.domain.extraction.segmentize import (
    LONG_SILENCE_MS,
    Segment,
    has_speaker_change,
    starts_with_turn_word,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "DEFAULT_TARGET_CHARS",
    "TURN_WORDS",
    "Chunk",
    "chunk_segments",
]

#: 话题转折词（施工卡 T2-01 原文给的表，**不要自行增删**）。
TURN_WORDS: tuple[str, ...] = (
    "那么", "接下来", "第二个议题", "下面", "另外", "关于",
)

DEFAULT_TARGET_CHARS = 1500
DEFAULT_MAX_CHARS = 2000
DEFAULT_OVERLAP_CHARS = 200

#: 强制切分长段时的句末标点（优先在这里下刀）。
_SENTENCE_END = "。！？；;"


@dataclass
class Chunk:
    """一个待抽取的块。

    Attributes:
        index: 从 1 开始。
        segments: 块内 segments（**含重叠部分**）。
        text: 拼好的正文（各段之间换行），喂给模型的就是它。
        char_count: `text` 的长度。
        overlap_lead: 块首有**几段**是从上一块重叠过来的（0 = 无重叠）。
            归并时用它判断"这段内容上一块已经抽过了"，避免重复计数。
        overlap_dropped: 因最后一段重叠与新段仍超上限而放弃重叠。
        forced_split: 是否由"单段超长强制切分"产生（用于告警）。
    """

    index: int
    segments: tuple[Segment, ...]
    text: str
    char_count: int
    overlap_lead: int = 0
    forced_split: bool = False
    overlap_dropped: bool = False


@dataclass
class _Buf:
    """攒块用的可变缓冲。"""

    segments: list[Segment] = field(default_factory=list)
    chars: int = 0

    def add(self, seg: Segment) -> None:
        self.segments.append(seg)
        self.chars += seg.char_count

    def tail_overlap(self, overlap_chars: int) -> "_Buf":
        """从尾部往前取**整段**，凑够 `overlap_chars` 为止。

        ⚠️ 至多取 `len(self.segments) - 1` 段：**绝不把整块都当重叠**，
        否则下一块与上一块完全相同 → 无限膨胀（且会重复抽取整块内容）。
        """
        if overlap_chars <= 0 or len(self.segments) <= 1:
            return _Buf()
        taken: list[Segment] = []
        total = 0
        for seg in reversed(self.segments[1:]):   # 至少留 1 段给上一块
            taken.append(seg)
            total += seg.char_count
            if total >= overlap_chars:
                break
        taken.reverse()
        out = _Buf()
        for s in taken:
            out.add(s)
        return out


def _render(segments: list[Segment]) -> str:
    return "\n".join(s.text for s in segments)


def _split_long_segment(seg: Segment, max_chars: int) -> list[Segment]:
    """把超长单段强制切成若干段（切点优先句末标点）。

    保留原 `seq` 之外的字段（speaker / 时间码）—— 时间码只挂第一片，
    后续片 `start_ms=None`（我们**不知道**它们各自的真实时间，
    编一个出来会让溯源指错地方；宁可为空）。
    """
    text = seg.text
    if len(text) <= max_chars:
        return [seg]

    pieces: list[str] = []
    rest = text
    while len(rest) > max_chars:
        window = rest[:max_chars]
        # 从窗口末尾往前找最近的句末标点
        cut = -1
        for i in range(len(window) - 1, max(0, len(window) - 200), -1):
            if window[i] in _SENTENCE_END:
                cut = i + 1
                break
        if cut <= 0:
            cut = max_chars          # 找不到标点就硬切
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)

    out: list[Segment] = []
    for i, piece in enumerate(pieces):
        out.append(Segment(
            seq=seg.seq,
            text=piece,
            speaker=seg.speaker,
            start_ms=seg.start_ms if i == 0 else None,
            end_ms=seg.end_ms if i == len(pieces) - 1 else None,
            gap_after_ms=seg.gap_after_ms if i == len(pieces) - 1 else None,
        ))
    return out


def _expand_long_segments(
    segments: list[Segment], max_chars: int
) -> tuple[list[Segment], set[int]]:
    """展开超长段。返回 `(展开后的段列表, 被强制切分过的原 seq 集合)`。

    为什么要返回"哪些 seq 被切过"：`forced_split` 若笼统地在"有任意一段超长"
    时就给**所有**块打标，那这个标记就没有信息量了 —— 告警要能指到具体块。
    """
    out: list[Segment] = []
    split_seqs: set[int] = set()
    for s in segments:
        pieces = _split_long_segment(s, max_chars)
        if len(pieces) > 1:
            split_seqs.add(s.seq)
        out.extend(pieces)
    return out, split_seqs



def chunk_segments(
    segments: list[Segment] | tuple[Segment, ...],
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    long_silence_ms: int = LONG_SILENCE_MS,
) -> list[Chunk]:
    """按语义边界切块。

    Args:
        segments: 已 segmentize 的段落。空列表 → 返回 `[]`（U-EX-10，不抛）。
        target_chars: 攒够这么多字之后，遇到边界才切。
        max_chars: 单段超过它会被**强制切分**。
        overlap_chars: 相邻块重叠的目标字数（按 segment 对齐，可能略超）。
        long_silence_ms: 判据 ③ 的阈值。

    Returns:
        块列表，`index` 从 1 开始。空输入返回空列表。
    """
    if not segments:
        return []

    expanded, split_seqs = _expand_long_segments(list(segments), max_chars)

    def _was_forced(segs: list[Segment] | tuple[Segment, ...]) -> bool:
        """这一块里**真的**含有被强制切开的碎片吗（而非全局只要有一段超长）。"""
        return any(s.seq in split_seqs for s in segs)

    chunks: list[Chunk] = []
    buf = _Buf()
    overlap_lead = 0
    prev: Segment | None = None
    overlap_dropped = False

    def emit() -> None:
        """输出当前块，并携带按段对齐的尾部重叠。"""
        nonlocal buf, overlap_lead, overlap_dropped
        chunks.append(Chunk(
            index=len(chunks) + 1,
            segments=tuple(buf.segments),
            text=_render(buf.segments),
            char_count=buf.chars,
            overlap_lead=overlap_lead,
            forced_split=_was_forced(buf.segments),
            overlap_dropped=overlap_dropped,
        ))
        overlap_dropped = False
        buf = buf.tail_overlap(overlap_chars)
        overlap_lead = len(buf.segments)

    for seg in expanded:
        # ①② 是 seg 之前的边界；已有新内容且攒够字数才输出。
        if (len(buf.segments) > overlap_lead and buf.chars >= target_chars
                and (has_speaker_change(prev, seg)
                     or starts_with_turn_word(seg.text, TURN_WORDS))):
            emit()
        # 无语义边界时也不能突破硬上限；超长单段的碎片不能重新拼回去。
        if buf.segments and buf.chars + seg.char_count > max_chars:
            if len(buf.segments) > overlap_lead:
                emit()
            # 完整重叠段与新段放不下时，优先保住新内容和大小上限。
            while len(buf.segments) > 1 and buf.chars + seg.char_count > max_chars:
                buf.chars -= buf.segments.pop(0).char_count
            # 最后一段只在确实放不下时放弃，明确报告这个边界例外。
            if buf.segments and buf.chars + seg.char_count > max_chars:
                buf = _Buf()
                overlap_dropped = True
            overlap_lead = len(buf.segments)
        buf.add(seg)
        # ③ 是 seg 之后的估计或显式静音，加入本段后判断。
        boundary = seg.gap_after_ms is not None and seg.gap_after_ms > long_silence_ms
        prev = seg

        # ★ 这就是那个 AND —— 攒够字数 **且** 遇到语义边界，才切。
        if (buf.chars >= target_chars and boundary) or buf.chars >= max_chars:
            emit()

    if len(buf.segments) > overlap_lead:
        chunks.append(Chunk(
            index=len(chunks) + 1,
            segments=tuple(buf.segments),
            text=_render(buf.segments),
            char_count=buf.chars,
            overlap_lead=overlap_lead,
            forced_split=_was_forced(buf.segments),
            overlap_dropped=overlap_dropped,
        ))
    return chunks
