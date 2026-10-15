"""记录稿正文 -> 转写分段（T1-11，纯函数）。

本模块存在的唯一理由：**填上"文字稿导入"这条路上缺的一环**
------------------------------------------------------------

上游有两条进料口（SAD §11 F1.1 / F1.6）：

    ① 音频投递  ->  `transcribe` 节点跑 ASR -> 落 `transcript/segments.json`
    ② 文字稿导入 -> 用户直接给 Markdown   -> **没人落 segments.json**

第 ② 条路上，`review` 节点的输入契约（读 `transcript/segments.json`）
根本没人满足 —— 于是 `ReviewService.load_segments()` 抛
`ReviewServiceError`，被 `_base.translate_exception` 包成 **E9001**，
用户看到"程序遇到一个未预期的问题"。而真相是"这次的输入形式没有被接住"，
属于**输入问题**，指成系统错误会让排查整个跑偏。

为什么补在**领域层**而不是 `review` 节点里
-----------------------------------------
"Markdown 怎么切段"是一条**纯规则** —— 同一个问题在别处也会遇到
（复核页要按段展示、溯源要给每段算 id）。写成纯函数才能真正单测，
也才不用为了测它去建库、起服务、造密钥。

为什么不复用 `extraction._split_sentences()`
-------------------------------------------
那个函数服务于**议题抽取**：它按句末标点继续切，好处是议题粒度细，
代价是"一句就是一个段"。但本模块产出的东西要冒充**转写分段**，
下游 `review` 会按它渲染"逐段带时间码的记录稿" —— 一段对应原稿一行
才符合直觉。两处目标不同，共用会让任一处改动都波及另一处。

免责声明（重要）
----------------
本模块产出的是**行级近似**，不是真实 ASR 结果：
  · `confidence` 一律为 `None`（"没有置信度"是事实，编一个 0.9 是撒谎），
    于是 `review` 的分流规则会**整篇标为待核对** —— 这恰恰是对的行为，
    因为文字稿没有 ASR 置信度、没人替它做过质量判据。
  · `words` 一律为空（没有逐字时间戳）。
所以落盘时 `engine` 必须写成 `text_import` 之类的**非引擎名**，
让人一眼看出这份段落是从文字稿反推的，而不是某个引擎的产物
（`review` 会把它渲染进记录稿的元信息）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

#: 时间码：`[01:02:03]` 或 `[02:03]`（时:分:秒 / 分:秒）。
TIMECODE_RE = re.compile(r"\[(\d{1,3}):([0-5]?\d)(?::([0-5]?\d))?\]")

#: 说话人前缀：行首 2-4 个汉字 + 中文/英文冒号。
#: 冒号前允许空白 —— 人工整理的稿子常见 `王五 ： 有空`。
SPEAKER_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4})\s*[：:]\s*")

#: Markdown 结构行（标题 / 引用 / 分隔线 / 表格）。它们**不是正文**，
#: 当成分段会让记录稿里冒出 `# 会议记录` 这种"发言人说的话"。
_STRUCTURE_RE = re.compile(r"^(#{1,6}\s|>\s|[-*_]{3,}\s*$|\|)")

#: Markdown 列表符号（`- ` / `1. ` / `* `）。列表**是**正文
#: （用户可能把发言整理成列表），只把符号剥掉、段落保留。
_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}[.)]\s+)")

#: 一段的最长时间跨度（毫秒）。相邻两段若都没时间码，按它递推出一个
#: 单调的占位时间轴；有真实时间码的行永远优先。
DEFAULT_SPAN_MS = 30_000


@dataclass(frozen=True)
class ParsedLine:
    """从一行 Markdown 解析出的（可选）说话人与正文。"""

    text: str
    speaker: str = ""
    start_ms: int | None = None
    end_ms: int | None = None


def parse_timecode(raw: str) -> int | None:
    """`[00:01:23]` -> 83000；`[02:03]` -> 123000（一律按毫秒）。

    解析不出返回 `None` 而不是 `0`：`0` 是**合法时间点**（会议刚开始），
    用它代替"没有时间码"会让两种情况在后端无法区分。
    """
    m = TIMECODE_RE.search(raw)
    if not m:
        return None
    a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
    if c is None:                     # [02:03] -> 分:秒
        return (a * 60 + b) * 1000
    return ((a * 60 + b) * 60 + int(c)) * 1000   # [01:02:03] -> 时:分:秒


def strip_speaker(line: str) -> tuple[str, str]:
    """剥掉行首"说话人："前缀，返回 `(正文, 说话人)`。

    说话人**返回而不是丢弃**：记录稿渲染时要用它（`张三：……`），
    只做剥除会把信息弄丢。
    """
    m = SPEAKER_RE.match(line)
    if not m:
        return line.strip(), ""
    return line[m.end():].strip(), m.group(1)


def is_content_line(line: str) -> bool:
    """这一行算不算"正文"（值得作为一个分段）。"""
    s = line.strip()
    if not s:
        return False
    if _STRUCTURE_RE.match(s):
        return False
    return True


def extract_lines(markdown: str) -> list[ParsedLine]:
    """把 Markdown 记录稿解析成行级分段列表（**保持原顺序**）。

    `start_ms` 的取法（三层）：
      ① 行内有 `[时间码]`  -> 用它；
      ② 上一行有时间码、本行没有 -> 顺延 `DEFAULT_SPAN_MS`
         （人工整理的稿子常只给每个话题一个时间码，
         后面几行没有 —— 顺延出来的时间轴虽然不精确，但**单调**，
         而单调是下游（按时间排序、溯源定位）唯一的硬要求）；
      ③ 整篇都没有时间码 -> 从这个值开始按 `DEFAULT_SPAN_MS` 递推。

    `end_ms` = 本行 `start_ms` + 估算时长，且**不越过下一行的开始**，
    免得出现"上一段还没说完，下一段已经开始"的倒挂时间轴。
    """
    rows: list[ParsedLine] = []
    for raw in markdown.splitlines():
        if not is_content_line(raw):
            continue
        line = _BULLET_RE.sub("", raw.strip())

        start: int | None = None
        if line.startswith("["):
            head, sep, rest = line.partition("]")
            # ⚠️ `partition("]")` 的 head 不含右括号，而时间码正则要求
            # 成对的中括号 —— 必须补回 `sep` 才能解析出来（同类坑见
            # `extraction._split_sentences` 的注释）。
            start = parse_timecode(head + sep)
            line = rest.strip()

        body, speaker = strip_speaker(line)
        if not body:
            continue
        rows.append(ParsedLine(text=body, speaker=speaker, start_ms=start))

    if not rows:
        return []

    # --- 时间轴第一步：把缺的 start_ms 补成一个**单调递增**的序列 ---
    #
    # 只有单调一条硬要求，精确度是次要的：下游按时间排序、按时间定位，
    # 时间轴倒挂会让"这段在第几分钟"整个失效；而"少了 30 秒"只是不精确。
    cursor = 0
    stamped: list[ParsedLine] = []
    for row in rows:
        if row.start_ms is not None:
            cursor = max(row.start_ms, cursor)
        else:
            cursor += DEFAULT_SPAN_MS
        stamped.append(ParsedLine(
            text=row.text, speaker=row.speaker,
            start_ms=cursor, end_ms=None,
        ))

    # --- 时间轴第二步：算 end_ms，且不越过下一段的开始 ---
    out: list[ParsedLine] = []
    for i, row in enumerate(stamped):
        start = int(row.start_ms or 0)
        end = start + DEFAULT_SPAN_MS
        if i + 1 < len(stamped):
            end = min(end, int(stamped[i + 1].start_ms or start))
        out.append(ParsedLine(
            text=row.text, speaker=row.speaker,
            start_ms=start, end_ms=max(end, start),
        ))
    return out


def to_raw_segments(markdown: str) -> list[dict[str, Any]]:
    """Markdown 记录稿 -> `TranscriptResult.from_raw_segments` 能吃的字典列表。

    `confidence` 刻意是 `None`、`words` 刻意是空表 —— 见模块 docstring
    的"免责声明"。**不要**为了让下游"看着正常"而编造置信度。
    """
    return [
        {
            "text": (f"{p.speaker}：{p.text}" if p.speaker else p.text),
            "start_ms": p.start_ms,
            "end_ms": p.end_ms,
            "confidence": None,
            "words": [],
            "flags": [],
        }
        for p in extract_lines(markdown)
    ]


def segments_payload(
    markdown: str,
    *,
    engine: str = "text_import",
    model: str = "record_markdown",
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """组装成 `transcript/segments.json` 的完整形状。

    形状与 `graph/nodes/transcribe._write_segments()` **逐字段一致** ——
    两份产出必须能被同一个 `ReviewService.load_segments()` 读懂，
    否则"文字稿导入"和"音频投递"两条路会在下游分叉。
    """
    return {
        "engine": engine,
        "model": model,
        "hotwords_applied": False,
        "hotword_count": 0,
        "warnings": list(warnings),
        "segments": to_raw_segments(markdown),
    }


__all__ = [
    "DEFAULT_SPAN_MS",
    "ParsedLine",
    "SPEAKER_RE",
    "TIMECODE_RE",
    "extract_lines",
    "is_content_line",
    "parse_timecode",
    "segments_payload",
    "strip_speaker",
    "to_raw_segments",
]
