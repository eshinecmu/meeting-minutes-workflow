"""记录稿渲染（T1-08，SAD §11 F2.3）。

产出物是「会议**记录**」，不是「会议**纪要**」
------------------------------------------
这两个词在公文语境里差得很远，混了会被内行一眼看穿：

        记录                          纪要
    ────────────────          ────────────────
    客观、按时间、流水          主观提炼、按议题、结论化
    "张局长说……"              "会议认为……"
    谁说的、什么时候说的都要有    谁说的不重要，结论重要

F2.3 要的是前者：**逐段带时间码的原始记录**，加上元信息表与待核对清单。
纪要由 T1-09 之后的 `minutes`/`extraction` 生成（SAD §4 把 M07 的边界
明写为"不做纪要生成"）。所以本模块**不做任何提炼、归纳、润色** ——
正文就是分段的原样拼接，一个字不改。任何"顺手把口语整理一下"的念头
都会让这份记录稿在溯源时对不上原始音频，那是本项目的红线（F4 溯源）。

八项元信息，缺一不可（验收标准第 2 条）
--------------------------------------
    会议名称 / 会议时间 / 会议地点 / 主持人
    参会人员 / 列席人员 / 记录人 / 记录整理时间

前七项来自 `meeting` 表（`title` / `meeting_time` / `location` / `host` /
`attendees` / `absentees` / `recorder`），第八项是**生成本文件的时刻**。

缺项填「待补充」而不是留空（施工卡「常见坑」第二条）
--------------------------------------------------
留空是"沉默的谎言"：一份元信息表里空着格子的记录稿，看起来像
"这场会没有主持人"，而不是"我们没填"。归档三个月后没人分得清。
填「待补充」是**主动声明这里有缺口**，使用者一眼可见，
也便于搜索（`grep 待补充` 就能列出所有待补的记录稿）。

为什么参会人员要用顿号连接（验收标准第 2 条）
--------------------------------------------
`meeting.attendees` 存的是 JSON 数组（001_init.sql），
直接 `str(list)` 会渲染成 `['张三', '李四']` —— 在公文里这是**错的**。
中文公文并列人名一律用顿号「、」。这条看着是小事，
但记录稿是要打印、要归档、要给领导看的，`['张三', '李四']` 会直接暴露
"这是机器生成的"。所以 `join_names()` 单独成函数、单独测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from mmwf.domain.review.split import (
    format_duration_ms,
    review_reason,
)

#: 元信息缺项时的占位符（施工卡明写）。
MISSING_METADATA_PLACEHOLDER = "待补充"

#: 渲染数据来源标记，写进 manifest 的 detail，便于回溯"这份记录稿怎么来的"。
RENDER_SOURCE = "review.render_record"

#: 正文小节的标题（**唯一真相**）。两处必须用它，不能各写一遍字符串：
#:   · `render_record()`           —— 产出记录稿时作为小标题；
#:   · `ReviewService.extract_body()` —— 下游取正文时按它定位。
#: 一旦两处漂移，"取正文"就会静默退回全文 —— 元信息表被当成议题，
#: 每篇纪要凭空多出十几条误报（见 `ReviewService.read_record_body` 的说明）。
RECORD_BODY_HEADING = "二、会议记录正文"

#: 八项元信息的 (键, 中文标签)，**顺序即渲染顺序**（施工卡模板的表行顺序）。
#:
#: 做成有序元组而不是 dict：dict 在 3.7+ 保序，但"顺序是契约"这件事
#: 值得用类型表达出来 —— 有人改成 set 就会被测试立刻抓住。
METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "会议名称"),
    ("meeting_time", "会议时间"),
    ("location", "会议地点"),
    ("host", "主持人"),
    ("attendees", "参会人员"),
    ("absentees", "列席人员"),
    ("recorder", "记录人"),
    ("generated_at", "记录整理时间"),
)

#: 顿号。中文公文的并列分隔符。
_IDEOGRAPHIC_COMMA = "、"

#: 视作"人名列表"结束的标点（用于从一整个字符串里拆人名）。
_NAME_SPLIT_RE = re.compile(r"[,，;；、\s]+")


class RecordRenderError(RuntimeError):
    """记录稿渲染失败（目前只在"分段没有时间戳"这类结构性错误时抛）。"""


@dataclass(frozen=True)
class RecordMetadata:
    """记录稿的八项元信息。

    刻意用 dataclass 而不是裸 dict：这样"哪八项"从类型上就是封闭的，
    少一项会在 `render_record` 的循环里被 `getattr` 抓到（渲染成「待补充」），
    而不是静默地少一行。

    值允许是 `None` / 空串 / 空列表 —— 三种都渲染成「待补充」。
    传入值一律先 `strip()`（"  " 和 "" 同样没有信息量）。
    """

    title: str | None = None
    meeting_time: str | None = None
    location: str | None = None
    host: str | None = None
    attendees: Sequence[str] | str | None = None
    absentees: Sequence[str] | str | None = None
    recorder: str | None = None
    generated_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, generated_at: str) -> "RecordMetadata":
        """从 `meeting` 表的一行（`sqlite3.Row` / dict）构造。

        `attendees` / `absentees` 在库里是 **JSON 文本**（001_init.sql：
        `attendees TEXT, -- JSON 数组`）。这里做一次宽容解析：
        是合法 JSON 数组就展开，不是就当"一整串人名"用顿号拆开 ——
        因为手工在数据库里填过的人（真实会发生）常常直接写
        `"张三、李四"` 而不是 `'["张三","李四"]'`。
        硬按 JSON 解析会抛错，让一条本来能用的记录无法生成。
        """
        return cls(
            title=_as_text(row.get("title")),
            meeting_time=_as_text(row.get("meeting_time")),
            location=_as_text(row.get("location")),
            host=_as_text(row.get("host")),
            attendees=_parse_name_field(row.get("attendees")),
            absentees=_parse_name_field(row.get("absentees")),
            recorder=_as_text(row.get("recorder")),
            generated_at=generated_at,
        )


@dataclass(frozen=True)
class ReviewItem:
    """一条待核对事项（记录稿第三节的列表项）。"""

    seq: int
    start_ms: int
    reason: str
    text: str

    @property
    def timestamp(self) -> str:
        return format_timestamp(self.start_ms)

    def to_line(self) -> str:
        """渲染成一行 Markdown。

        原文用 `>` 引用块而不是行内代码：原文里可能有反引号
        （人名/术语里出现反引号不罕见），行内代码会被它截断。
        引用块对内容里的任何字符都安全，且视觉上"这是一段引文/待核对的原文"
        的语义更强。
        """
        head = f"- **{self.timestamp}**（{self.reason}）"
        body = self.text.strip() or "（空）"
        return f"{head}\n  > {body}"


@dataclass
class RecordRender:
    """渲染结果（正文 + 若干统计，供 service 写审计与 manifest）。"""

    markdown: str
    metadata_filled: tuple[str, ...]
    metadata_missing: tuple[str, ...]
    body_segment_count: int
    review_items: tuple[ReviewItem, ...]
    auto_pass_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def metadata_complete(self) -> bool:
        return not self.metadata_missing

    @property
    def review_count(self) -> int:
        return len(self.review_items)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def format_timestamp(ms: int) -> str:
    """`[HH:MM:SS]` 形状的时间码（**带方括号**，验收标准第 3 条）。

    施工卡模板里正文段落写的是"每段带时间码，格式 `[00:12:34]`" ——
    方括号是格式的一部分。复核页与导出都按这个形状做正则抓取
    （`\\[(\\d{2}:\\d{2}:\\d{2})\\]`），少一个括号就对不上。
    """
    return f"[{format_duration_ms(ms)}]"


def join_names(names: Iterable[str] | str | None) -> str:
    """把人名列表渲染成顿号分隔的中文写法；空则返回「待补充」。

        ["张三", "李四"]  ->  "张三、李四"
        "张三、李四"      ->  "张三、李四"      （已经是字符串就拆了重拼，统一分隔符）
        [] / None         ->  "待补充"

    为什么字符串也要拆了重拼：库里可能存着 `"张三, 李四"`（英文逗号加空格），
    直接返回会输出 `张三, 李四` —— 在公文里是全角/半角混用的硬伤。
    重拼一遍把分隔符统一成顿号，顺带去掉多余空白。
    """
    if names is None:
        return MISSING_METADATA_PLACEHOLDER
    if isinstance(names, str):
        items = [p for p in _NAME_SPLIT_RE.split(names) if p]
    else:
        # 元素里可能还夹着分隔符（"张三、李四" 作为一个元素传进来），再拆一遍
        items = []
        for n in names:
            items.extend(p for p in _NAME_SPLIT_RE.split(str(n)) if p)
    if not items:
        return MISSING_METADATA_PLACEHOLDER
    return _IDEOGRAPHIC_COMMA.join(items)


def split_for_review(
    segments: Sequence[Any],
    thresholds: dict[str, Any],
) -> tuple[list[Any], list[Any]]:
    """转发到 `split.needs_review` 的两堆切分。

    这里刻意**再导出一次**：`__init__.py` 的对外 API 里
    `split_for_review` 与 `render_record` 是并列的两个主入口，
    调用方（service 层）通常两个一起用。放在 record 模块里只是
    为了 import 路径短一点，实现仍然只有一处（在 split.py）。
    """
    from mmwf.domain.review.split import split_for_review as _split

    return _split(segments, thresholds)


def summarize_review(items: Sequence[ReviewItem], total: int) -> str:
    """一句人话总结复核量（写进审计 detail 与验收输出）。"""
    if total <= 0:
        return "无分段，无需复核"
    n = len(items)
    return (
        f"{n}/{total} 段待核对（{n / total:.1%}）"
        if n
        else f"{total} 段全部通过，无需人工复核"
    )


# ---------------------------------------------------------------------------
# 主渲染
# ---------------------------------------------------------------------------

def render_record(
    segments: Sequence[Any],
    metadata: RecordMetadata | Mapping[str, Any],
    thresholds: dict[str, Any],
    *,
    meeting_id: str | None = None,
    version: int | None = None,
    extra_warnings: Sequence[str] = (),
) -> RecordRender:
    """渲染「会议记录.md」（施工卡 F2.3 模板，逐字对齐）。

    结构::

        # 会议记录

        ## 一、会议基本信息
        | 项目 | 内容 |
        ...8 行...

        ## 二、会议记录正文
        [00:00:12] 正文…
        [00:00:31] 正文…

        ## 三、待核对事项
        - **[00:12:34]**（识别置信度偏低（0.52 < 0.70））
          > 原文…

    正文段落**全部**保留（包括待复核的那些）—— 记录稿是"会议的完整记录"，
    少一段就是漏记。待复核只是在第三节额外列出来提醒人去核，
    而不是把正文里的那一行抽走。如果抽走，正文时间码就会出现空洞，
    使用者会以为"那段时间没人说话"。

    Args:
        segments: 分段序列（按时间升序）。每个需有
            `text` / `start_ms` / `confidence`（`TranscriptSegmentResult` 或替身）。
        metadata: `RecordMetadata` 或等价字典。缺失项渲染成「待补充」。
        thresholds: 分流阈值（传给 `review_reason`）。
        meeting_id: 仅用于错误信息与渲染统计，不写进正文
            （正文里出现内部 ID 会破坏公文观感）。
        version: 版本号，写进"记录整理时间"前的注释行便于与 manifest 对照。
        extra_warnings: 上游告警（如"引擎未提供置信度"）转成本文件末尾的提示。

    Returns:
        `RecordRender`：`markdown` 是最终文本，其余字段供 service 写审计。

    Raises:
        RecordRenderError: 分段缺 `start_ms`（那会导致时间码错位，
            而这种错误在成品里看不出来 —— 必须当场停）。
    """
    if isinstance(metadata, Mapping):
        meta = RecordMetadata(**dict(metadata))  # type: ignore[arg-type]
    else:
        meta = metadata

    # ① 元信息表。逐项按 METADATA_FIELDS 的顺序取，缺项填「待补充」。
    rows: list[tuple[str, str]] = []
    filled: list[str] = []
    missing: list[str] = []
    for key, label in METADATA_FIELDS:
        raw = getattr(meta, key, None)
        value = join_names(raw) if key in ("attendees", "absentees") else _as_text(raw)
        if value is None:
            value = MISSING_METADATA_PLACEHOLDER
        rows.append((label, value))
        (missing if value == MISSING_METADATA_PLACEHOLDER else filled).append(label)

    # ② 正文 + 待核对清单
    body_lines: list[str] = []
    review_items: list[ReviewItem] = []
    auto_pass = 0
    for i, seg in enumerate(segments, start=1):
        if not hasattr(seg, "start_ms"):
            raise RecordRenderError(
                f"分段缺少 start_ms，无法生成时间码（第 {i} 段，"
                f"meeting_id={meeting_id or '(未提供)'}）。"
                "记录稿的正文必须每段带时间码，缺时间戳会让溯源全部错位。"
            )
        start_ms = int(getattr(seg, "start_ms"))
        text = (getattr(seg, "text", "") or "").strip()
        body_lines.append(f"{format_timestamp(start_ms)} {text or '（空）'}")

        reason = review_reason(seg, thresholds)
        if reason:
            review_items.append(
                ReviewItem(
                    seq=int(getattr(seg, "seq", i)),
                    start_ms=start_ms,
                    reason=reason,
                    text=text,
                )
            )
        else:
            auto_pass += 1

    warnings: list[str] = list(extra_warnings)
    if missing:
        warnings.append(
            "元信息缺项已填「待补充」："
            + "、".join(missing)
            + "（请补齐后重新生成记录稿）"
        )
    if not segments:
        warnings.append("本次会议没有任何转写分段，记录稿正文为空。")

    # ③ 拼装
    out: list[str] = ["# 会议记录", ""]
    if version is not None:
        # 版本行放在标题之后、元信息之前：它是文件的"版次"，属于抬头信息。
        out += [f"> 记录稿版本：v{version}", ""]

    out += ["## 一、会议基本信息", "", "| 项目 | 内容 |", "|---|---|"]
    out += [f"| {label} | {value} |" for label, value in rows]
    out += ["", f"## {RECORD_BODY_HEADING}", ""]
    out += body_lines if body_lines else ["（无正文）"]
    out += ["", "## 三、待核对事项", ""]
    if review_items:
        out += [item.to_line() for item in review_items]
    else:
        # 空清单也要有一行说明，不能留空小节 ——
        # 空的"待核对事项"小节会让人以为渲染坏了，而不是"确实没有"。
        out += ["（本次会议无需人工核对的段落，全部段落置信度正常）"]

    if warnings:
        out += ["", "## 四、生成提示", ""]
        out += [f"- {w}" for w in warnings]

    markdown = "\n".join(out).rstrip() + "\n"

    return RecordRender(
        markdown=markdown,
        metadata_filled=tuple(filled),
        metadata_missing=tuple(missing),
        body_segment_count=len(body_lines),
        review_items=tuple(review_items),
        auto_pass_count=auto_pass,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _as_text(v: Any) -> str | None:
    """归一化成"有内容的字符串"或 None。

    `0` / `False` 这类"有值但假值"会被 `if not v` 误判成空，
    所以判空用**显式的 None 与 strip 后为空**，不用真值测试。
    实际字段都是文本，但把规则写对不用额外成本。
    """
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_name_field(v: Any) -> list[str] | None:
    """解析 `meeting` 表里的 JSON 文本人名列（容错，见 `from_row` 说明）。"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    s = str(v).strip()
    if not s:
        return None
    if s.startswith("["):
        try:
            import json

            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            # 落回"当普通字符串拆"，不抛错（见 from_row 的理由）
            pass
    return [s]
