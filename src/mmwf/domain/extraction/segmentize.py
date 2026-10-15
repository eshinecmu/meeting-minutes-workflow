"""把记录稿 Markdown 反解成**分块用的 segments**（T2-01 裁决 D1）。

为什么需要这一步
----------------
SAD §10.1 的分块算法是**按 segments** 写的：

    boundary = (seg.speaker != prev.speaker) or ... or (seg.gap_after_ms > 1500)

但图上 `extract` 节点的输入契约是 `record_markdown`（**一个字符串**，
见 `graph/state.py::INPUT_FIELDS[NODE_EXTRACT]`）。两者对不上。

本模块就是那座桥：`record_markdown -> list[Segment]`。

为什么不让图改契约、直接传 segments
------------------------------------
T1-10 立下过"二期把领域实现插进来，**图不用改一行**"的承诺，注入点签名
就是 `Callable[[str], dict]`。改成传 segments 要动 `state.py` 的
`INPUT_FIELDS`，那会波及检查点兼容性，以及一期**已经验收过**的一批
编排测试（"approve 后 extract 不重跑"这类断言全靠状态字段定义稳定）。
**收益是省一次解析，代价是动已验收的契约** —— 不值。

> 所以本模块的解析与 `graph/nodes/extraction.py::_split_sentences`
> 是**同源**的（时间码 + 说话人前缀）。差别只在粒度：
> 那边切**句子**（朴素抽取按句分类），这边切**行/段**（分块按段攒字数）。
> 两处都认同一套记录稿格式，格式变了要一起改。

记录稿长什么样（`review` 节点产出，T1-08 约定）
-----------------------------------------------
    # 会议记录稿

    [00:00:00] 主持人：今天讨论三季度预算。
    [00:00:32] 张三：我认为总体可行。
    下面看第二个议题。

- 带时间码的行 → 一个 segment，`start_ms` 能解出来。
- 不带时间码的行 → 也是一个 segment，但 `start_ms=None`
  （**不是 0**：0 是合法时间点，用它冒充"没有时间码"会让两者无法区分）。
- 标题行（`#`）与空行 → 跳过，不产生 segment。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from mmwf.domain.segments import Segment

__all__ = [
    "LONG_SILENCE_MS",
    "Segment",
    "SegmentizeReport",
    "segmentize",
]


#: 判定"长静音"的阈值（毫秒）。SAD §10.1 Step 1 原文给的 1500。
LONG_SILENCE_MS = 1500


@dataclass(frozen=True)
class SegmentizeReport:
    """解析的**体检报告** —— 用来暴露 D1 有损反解的代价。

    `no_timecode` 是本模块最该被看见的数字：它 > 0 说明记录稿里有行没有
    时间码，那些段落的 `start_ms` 是 `None`，`gap_after_ms` 也算不出来，
    于是**长静音这条分块边界对它们失效**。这不是 bug（记录稿本身信息不全），
    但如果比例很高，分块会退化成"只按字符数切"，抽取质量会下降。
    把这个数字报出来，让人能看见退化，而不是默默变差。
    """

    # 有相邻时间码的 gap 均为语速估计值，不能作为实测静音。
    total: int
    with_timecode: int
    no_timecode: int
    speakers_found: int
    estimated_gaps: int = 0

    @property
    def timecode_ratio(self) -> float:
        return self.with_timecode / self.total if self.total else 1.0

    @property
    def degraded(self) -> bool:
        """时间码覆盖率 < 50% 即视为**分块能力退化**（长静音边界基本失效）。"""
        return self.total > 0 and self.timecode_ratio < 0.5


#: `[01:02:03]` / `[02:03]` 两种时间码写法。
_TIMECODE_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")

#: 行首的时间码（**必须锚定行首**，避免把正文里的方括号当时间码）。
_LEADING_TIMECODE_RE = re.compile(r"^\s*\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*")

#: 说话人前缀：行首 2—4 个汉字后跟中文/英文冒号。
#:
#: ⚠️ 冒号前**允许空白**（`\s*`）。记录稿经人工编辑后常见 `王五 ： 有空`
#: 这种带空格写法，正则若要求冒号紧贴名字就剥不掉，说话人会留在正文里。
_SPEAKER_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4})\s*[：:]\s*")


def _parse_timecode(m: re.Match[str]) -> int:
    """`[01:02:03]` -> 3723000；`[02:03]` -> 123000（毫秒）。"""
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:                       # [02:03] 解释为 分:秒
        return (int(a) * 60 + int(b)) * 1000
    return ((int(a) * 60 + int(b)) * 60 + int(c)) * 1000


def segmentize(record_markdown: str, *, speech_chars_per_sec: float = 4.0) -> tuple[list[Segment], SegmentizeReport]:
    """把记录稿反解成 segments，并给出一份体检报告。

    Returns:
        `(segments, report)`。空输入返回 `([], report)` —— **不抛异常**：
        空记录稿是合法输入（U-EX-10），比如会议只开了 5 秒。
    """
    if speech_chars_per_sec <= 0:
        raise ValueError("speech_chars_per_sec 必须大于 0")
    raw: list[tuple[str, str | None, int | None]] = []
    # (正文, 说话人, start_ms)
    for line in (record_markdown or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        start_ms: int | None = None
        m = _LEADING_TIMECODE_RE.match(line)
        if m:
            start_ms = _parse_timecode(m)
            line = line[m.end():].strip()

        speaker: str | None = None
        sm = _SPEAKER_RE.match(line)
        if sm:
            speaker = sm.group(1)
            line = line[sm.end():].strip()

        if not line:
            # 整行只有时间码、或只有"说话人："而没有正文 —— 不成段。
            # ⚠️ 这里**直接丢弃**，不产生空段：空段会进分块缓冲，
            # 白白占字符数预算（`char_count=0` 但它仍占一个 segment），
            # 还可能让 `has_speaker_change` 在空段上误判。
            continue
        raw.append((line, speaker, start_ms))

    segments: list[Segment] = []
    for i, (text, speaker, start_ms) in enumerate(raw):
        next_start = raw[i + 1][2] if i + 1 < len(raw) else None
        # Markdown 没有真实结束时间；扣除按语速估计的说话时长，得到估计静音。
        # end_ms 仍沿用下一段起点作溯源区间上界，不冒充真实发言结束时间。
        end_ms = next_start
        gap = (max(0, next_start - start_ms
                   - round(len(text) / speech_chars_per_sec * 1000))
               if next_start is not None and start_ms is not None else None)
        segments.append(Segment(
            seq=i + 1,
            text=text,
            speaker=speaker,
            start_ms=start_ms,
            end_ms=end_ms,
            gap_after_ms=gap,
        ))

    with_tc = sum(1 for s in segments if s.start_ms is not None)
    report = SegmentizeReport(
        total=len(segments),
        with_timecode=with_tc,
        no_timecode=len(segments) - with_tc,
        estimated_gaps=sum(s.gap_after_ms is not None for s in segments),
        speakers_found=len({s.speaker for s in segments if s.speaker}),
    )
    return segments, report


def has_speaker_change(prev: Segment | None, cur: Segment) -> bool:
    """是否发生说话人切换（分块边界判据 ①）。

    ⚠️ **两边都非空才比**。记录稿里大量行没有说话人（`None`），
    若把 `None -> None` 也算"切换"，那几乎每行都是边界，块会碎成一句一块。
    同理 `张三 -> None` 也不算切换 —— `None` 是"没写说话人"，不是"另一个人"。
    """
    if prev is None:
        return False
    return (prev.speaker is not None
            and cur.speaker is not None
            and prev.speaker != cur.speaker)


def starts_with_turn_word(text: str, words: Sequence[str],
                          *, head_chars: int = 10) -> bool:
    """段首 `head_chars` 字内是否含话题转折词（分块边界判据 ②）。

    施工卡给的是"`seg.text[:10]` 里含转折词"，本函数与之一致。
    只在**段首**找而不是全段找：转折词出现在句子中间（"关于这一点我同意"）
    不是新议题的开始。
    """
    head = (text or "")[:head_chars]
    return any(w in head for w in words)
