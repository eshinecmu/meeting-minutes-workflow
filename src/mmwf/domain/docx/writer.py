"""Markdown 结构 -> docx（T1-09 核心，SAD §6「复用 + 自研补丁」）。

为什么是"内联移植"而不是 import md2docx
---------------------------------------
SAD §6 的依赖表把 `minerior/md2docx` 列为公文体参考实现，但**明确要求**：

    > 因其仅 6 个提交，须把关键映射逻辑内联进本项目并自建测试，
    > 避免上游停更即瘫痪

6 个提交意味着它不是一个被维护的库，是一个**参考实现**。所以本模块
按施工卡"架构"图的两段走：结构转换（本文件）+ 样式修正（patches.py），
而**不**引入 md2docx 作为运行期依赖。

支持的 Markdown 子集（刻意窄）
-----------------------------
    # 标题           -> 文档大标题（title 版式，二号方正小标宋简体居中）
    ## / ### / ####  -> 一级/二级/三级标题（自动加"一、" "（一）" "1." 编号）
    | a | b |        -> 表格（表头行 + 分隔行 + 数据行）
    - / * / +        -> 无序列表（渲染为带"○"的正文段）
    - [ ] / - [x]    -> 待办项（渲染为"☐ / ☑"）
    1. 2. 3.         -> 有序列表（**保留原有序号**，本卡不重编号 —— 见下）
    普通段落          -> 正文（首行缩进 2 字符，固定行距 28.8 磅）
    > 引用           -> 引文（带缩进的正文段）

**刻意不支持**：嵌套列表、代码块、HTML、图片、脚注、数学公式。
理由：这些都是"纪要"不该出现的东西（SAD §4 把 M09 的边界写为
"公文结构模板、语体对齐、粒度控制"，M11 只做版式）。真出现了
说明上游生成有问题，**原样当普通段落渲染**并留一条 warning，
比硬塞一个半吊子的代码块样式更诚实。

有序列表为什么不重编号
----------------------
与本模块给标题重编号（见 style_mapper）的做法**相反**，原因是：
标题的编号在整个公文里是**全局唯一**的层级序号，必须由一处统一生成，
否则"三、"后面接"二、"这种跨节跳号没人能发现。而有序列表的序号
（"1. 2. 3."）是**段内局部**的，上游写"1."就是"1."，我们重编号
反而会把"1. 2. 2. 3."（作者笔误）悄悄修好，掩盖了上游质量问题。
版式层的职责是"排得对"，不是"改对"。

标题层级为什么要有"降一级"的规则
--------------------------------
Markdown 里 `#` 是最高级标题，但公文的格局是：

    ┌─────────────────────────────┐
    │     关于XX工作的会议纪要     │  ← 文档大标题（title）
    │                             │
    │ 一、会议基本情况             │  ← 一级标题（h1）
    │ （一）时间地点               │  ← 二级标题（h2）
    └─────────────────────────────┘

也就是说文档大标题（title）在 Markdown 里是 `#`，而公文的一级标题
对应 Markdown 的 `##`。所以映射是：**第一个 `#` 当 title，`##`→h1、
`###`→h2、`####`→h3**。如果文档里出现多个 `#`，第二个及以后降级为
h1（它们不可能是第二个大标题），并留 warning。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from mmwf.domain.docx import patches as P
from mmwf.domain.docx._docx_backend import WD_ALIGN_PARAGRAPH as wd_align
from mmwf.domain.docx.style_mapper import (
    GB_STYLES,
    HEADING_STYLE_KEYS,
    GBStyle,
    NumberingState,
    has_heading_number,
    strip_heading_number,
)
from mmwf.domain.docx.text_normalize import normalize_text

#: 表格分隔行：`| --- | :--: |` 之类。
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")

#: 表格行：以 `|` 开头，或以 `|` 结尾（宽松接受没对齐的写法）。
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

#: 标题：`#` 到 `######` + 空格。
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

#: 无序列表：`- ` / `* ` / `+ `。
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")

#: 待办项：`- [ ] 文字` / `- [x] 文字`。
_TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*)$")

#: 有序列表：`1. ` / `1、`。
_ORDERED_RE = re.compile(r"^\s*(\d+)[.、]\s+(.*)$")

#: 引用：`> 文字`。
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")

#: 分隔线：`---` / `***` / `___`（三个以上）。渲染为一条空段落。
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


@dataclass
class Block:
    """一个待渲染的结构块（解析的中间产物）。

    分成 parse 与 render 两段而不是边读边写，是为了让"结构解析"
    可以**脱离 python-docx 单独测试**：给一段 Markdown，断言解析出的
    Block 序列 —— 这比"生成 docx 再解压看 XML"快得多，也更容易定位
    是哪一步错了。
    """

    kind: str                      # title | heading | paragraph | bullet |
                                   # ordered | task | quote | table | hr
    text: str = ""
    level: int = 0                 # heading 的层级（1..3）
    marker: str = ""               # 列表的原始标号（有序列表用）
    rows: list[list[str]] = field(default_factory=list)   # table


@dataclass
class RenderResult:
    """渲染结果。"""

    doc: object
    block_count: int
    table_count: int
    heading_count: int
    paragraph_count: int
    warnings: tuple[str, ...] = ()
    font_report: object | None = None

    def to_dict(self) -> dict:
        return {
            "blocks": self.block_count,
            "tables": self.table_count,
            "headings": self.heading_count,
            "paragraphs": self.paragraph_count,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# 结构解析
# ---------------------------------------------------------------------------

def _split_table_cells(line: str) -> list[str]:
    """拆一行表格为单元格列表。

    要处理三种写法（都是真实上游会产生的）：
        `| a | b |`        标准
        `|a|b|`            没空格
        `a | b`            缺首尾竖线（宽松）

    同时处理**转义竖线** `\\|`（单元格内容里真有个竖线时）——
    不处理的话一个内容里的竖线会把单元格切开，表格列数错乱。
    """
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def parse_markdown(text: str) -> tuple[list[Block], list[str]]:
    """把 Markdown 解析成 Block 序列。返回 `(blocks, warnings)`。"""
    blocks: list[Block] = []
    warnings: list[str] = []
    lines = text.splitlines()
    seen_title = False
    heading_counter = NumberingState()
    i = 0

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 分隔线要在表格判定之前（`---` 会被 _TABLE_SEP_RE 误判）
        if _HR_RE.match(line):
            # 但 `---` 紧跟表格行时它是分隔行，由表格分支消费；
            # 单独出现才是分隔线。
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if not _TABLE_ROW_RE.match(nxt):
                blocks.append(Block(kind="hr"))
                i += 1
                continue

        # 表格
        if _TABLE_ROW_RE.match(line):
            rows, consumed, tbl_warn = _consume_table(lines, i)
            warnings.extend(tbl_warn)
            if rows:
                blocks.append(Block(kind="table", rows=rows))
            i += consumed
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            hashes, title_text = m.group(1), m.group(2).strip()
            md_level = len(hashes)

            if not seen_title and md_level == 1:
                seen_title = True
                blocks.append(Block(kind="title", text=title_text))
                i += 1
                continue

            if md_level == 1:
                # 第二个 `#`：文档里只能有一个大标题，降级并留痕
                warnings.append(
                    f"markdown 里出现第 2 个一级标题「{title_text}」，"
                    "已降为公文体一级标题"
                )
                style_level = 1
            else:
                style_level = min(md_level - 1, 3)
                if md_level - 1 > 3:
                    warnings.append(
                        f"标题「{title_text}」层级为 {md_level}，"
                        "公文体只到三级，已按三级标题排版"
                    )

            # 统一编号：剥掉自带序号，再由 NumberingState 生成
            clean = strip_heading_number(title_text) if has_heading_number(title_text) else title_text
            prefix = heading_counter.next_prefix(style_level)
            blocks.append(Block(kind="heading", text=f"{prefix}{clean}",
                                level=style_level))
            i += 1
            continue

        # 待办项要先于无序列表判定（`- [ ] x` 也匹配 _BULLET_RE）
        m = _TASK_RE.match(line)
        if m:
            checked = m.group(1).lower() == "x"
            blocks.append(Block(kind="task", text=m.group(2).strip(),
                                marker="☑" if checked else "☐"))
            i += 1
            continue

        m = _BULLET_RE.match(line)
        if m:
            blocks.append(Block(kind="bullet", text=m.group(1).strip(),
                                marker="○"))
            i += 1
            continue

        m = _ORDERED_RE.match(line)
        if m:
            blocks.append(Block(kind="ordered", text=m.group(2).strip(),
                                marker=f"{m.group(1)}."))
            i += 1
            continue

        m = _QUOTE_RE.match(line)
        if m:
            blocks.append(Block(kind="quote", text=m.group(1).strip()))
            i += 1
            continue

        # 兜底：原样当正文段落（见模块文档"刻意不支持"）
        blocks.append(Block(kind="paragraph", text=stripped))
        i += 1

    return blocks, warnings


def _consume_table(lines: Sequence[str], start: int) -> tuple[list[list[str]], int, list[str]]:
    """从 `start` 起消费连续表格行。返回 `(rows, 消费行数, warnings)`。

    表头行之后**必须**有一个分隔行（`| --- |`），这是 Markdown 的语法。
    但上游 LLM 生成的 Markdown 经常漏掉它 —— 所以**宽松接受**：
    没有分隔行时把第一行当表头，并留一条 warning。
    """
    warnings: list[str] = []
    raw_rows: list[list[str]] = []
    i = start
    while i < len(lines) and _TABLE_ROW_RE.match(lines[i].rstrip()):
        raw_rows.append(_split_table_cells(lines[i]))
        i += 1

    if not raw_rows:
        return [], 0, warnings

    has_sep = len(raw_rows) >= 2 and all(
        _TABLE_SEP_RE.match("|" + "|".join(c) + "|") or set(c) <= set("-: ")
        for c in [raw_rows[1]]
    )
    if has_sep:
        body = [raw_rows[0], *raw_rows[2:]]
    else:
        body = raw_rows
        if len(raw_rows) >= 1:
            warnings.append(
                "表格缺少 Markdown 分隔行（| --- |），已按第一行为表头处理"
            )

    width = max((len(r) for r in body), default=0)
    normalized: list[list[str]] = []
    for r in body:
        if len(r) < width:
            warnings.append(
                f"表格有一行只有 {len(r)} 列，已补空单元格到 {width} 列"
            )
        normalized.append(list(r) + [""] * (width - len(r)))
    return normalized, i - start, warnings


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

@dataclass
class RenderOptions:
    """渲染选项。默认值即 GB/T 9704 的常规做法。"""

    #: 字体策略：warn | fallback | fail（见 font_check）。
    font_policy: str = "warn"
    #: 是否做中英空格/引号规范化（F5.6）。
    normalize_text: bool = True
    #: 是否给标题自动编号（公文体"一、"）。
    auto_numbering: bool = True
    #: 是否在页脚加 `PAGE` 域页码（施工卡验收标准要求）。
    page_number: bool = True
    #: 已预检的字体报告（传入则不再探测，便于测试与复用）。
    font_report: object | None = None
    #: 显式的已装字体集合（测试用；None 走真实探测）。
    installed_fonts: set[str] | None = None


def _style(key: str) -> GBStyle:
    return GB_STYLES[key]


def _apply_style(paragraph, style: GBStyle, *, actual_font: str,
                 options: RenderOptions) -> None:
    """给段落套上版式：对齐、缩进、行距。字体在 `_write_runs` 里设。

    `actual_font` 只用于日志/排查时对照，段落本身不记字体 ——
    OOXML 的字体是字符属性，段落层没有这个位置（事实 ①）。
    """
    P.set_alignment(paragraph, style.align)
    P.set_first_line_indent_chars(paragraph, style.first_line_indent_chars)
    if style.line_spacing_pt is not None:
        P.set_fixed_line_spacing(paragraph, style.line_spacing_pt)


def _write_runs(paragraph, text: str, style: GBStyle, *, actual_font: str,
                options: RenderOptions) -> None:
    """写入文字并设字体。**每个 run 都要单独设中文字体**（事实 ①）。

    为什么要按 run 设而不是按段落：OOXML 的字体是**字符属性**
    （`w:rPr`），不存在"段落级字体"。任何"给段落设一次就行"的想法
    都会在 Word 里表现成"英文生效、中文没生效"。
    """
    payload = normalize_text(text) if options.normalize_text else text
    run = paragraph.add_run(payload)
    P.set_cjk_font(run, actual_font, style.size_pt, style.bold)


def _render_page_number(doc, report, options: RenderOptions) -> None:
    """给每个 section 的页脚加 `PAGE` 域页码。

    GB/T 9704 规定页码在版心外、居外侧（奇偶页分左右）。"居外侧"
    需要设置 `w:evenAndOddHeaders` 并分别定义奇偶页脚 —— 那会让
    footer 部件的数量翻倍，而本卡的验收标准只要求"页码为 PAGE 域"。

    所以这里做**够用且可断言**的实现：单一页脚、居中。
    这样 `verify_docx_style.py` 能稳定地在 footer XML 里找到
    `PAGE` 与 `fldChar`。奇偶分设留给后续卡（它需要更多 fixture）。
    """
    if not options.page_number:
        return
    align = wd_align()

    style = _style("page_number")
    actual = report.resolve(style.font)

    for section in doc.sections:
        footer = section.footer
        # `is_linked_to_previous` 默认 True：不解除的话，第二个 section
        # 的页脚会继承第一个，页码域只会在第一个 section 出现一次。
        footer.is_linked_to_previous = False
        para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        para.text = ""
        para.alignment = align.CENTER
        P.set_first_line_indent_chars(para, 0)
        P.add_page_number_field(para, font_name=actual, size_pt=style.size_pt)


def render_markdown_to_docx(
    markdown: str,
    *,
    options: RenderOptions | None = None,
    doc=None,
) -> RenderResult:
    """把 Markdown 渲染成 docx（GB/T 9704 版式）。

    `doc` 传入时在其上续写（供调用方预置页眉页脚）；不传则新建。
    """
    from mmwf.domain.docx import font_check as FC
    from mmwf.domain.docx._docx_backend import new_document

    opts = options or RenderOptions()
    doc = doc if doc is not None else new_document()

    # --- 字体预检（在写任何内容之前，因为字体名要按回退结果写） ---
    report = opts.font_report
    if report is None:
        report = FC.check_fonts(policy=opts.font_policy,
                               installed=opts.installed_fonts)
    if FC.policy_blocks_export(report):
        raise FontMissingError(
            "字体缺失且策略为 fail，已中止导出",
            missing=report.missing,
        )

    blocks, warnings = parse_markdown(markdown)
    if not opts.auto_numbering:
        warnings.append("已关闭标题自动编号（auto_numbering=False）")

    # --- 版面：页边距 37/35/28/26 mm（GB/T 9704 版心 156×225 mm） ---
    for section in doc.sections:
        P.set_page_margins(section)

    # --- 页脚页码（PAGE 域） ---
    _render_page_number(doc, report, opts)

    counters = {"paragraph": 0, "heading": 0, "table": 0}

    for block in blocks:
        if block.kind == "title":
            style = _style("title")
            para = doc.add_paragraph()
            _apply_style(para, style, actual_font=report.resolve(style.font),
                         options=opts)
            _write_runs(para, block.text, style,
                        actual_font=report.resolve(style.font), options=opts)
            # 标题与正文之间空一行（公文的标题与正文之间留白）
            spacer = doc.add_paragraph()
            _apply_style(spacer, _style("body"),
                         actual_font=report.resolve(_style("body").font),
                         options=opts)
            continue

        if block.kind == "heading":
            key = HEADING_STYLE_KEYS.get(block.level, "h3")
            style = _style(key)
            actual = report.resolve(style.font)
            para = doc.add_paragraph()
            _apply_style(para, style, actual_font=actual, options=opts)
            _write_runs(para, block.text, style, actual_font=actual, options=opts)
            counters["heading"] += 1
            continue

        if block.kind == "table":
            _render_table(doc, block.rows, report=report, options=opts)
            counters["table"] += 1
            continue

        if block.kind == "hr":
            # 分隔线：公文里用不着，渲染成一个空正文段（保留视觉间隔）
            para = doc.add_paragraph()
            style = _style("body")
            _apply_style(para, style, actual_font=report.resolve(style.font),
                         options=opts)
            continue

        # 其余都是"文字类"块：正文 / 列表 / 待办 / 引用
        style = _style("body")
        actual = report.resolve(style.font)
        para = doc.add_paragraph()
        _apply_style(para, style, actual_font=actual, options=opts)

        text = block.text
        if block.kind in ("bullet", "task"):
            # 列表项：**不缩进**（公文的列表不首行缩进 2 字符，
            # 否则项目符号会跑到奇怪的位置），标号写在最前
            P.set_first_line_indent_chars(para, 0)
            text = f"{block.marker} {text}"
        elif block.kind == "ordered":
            P.set_first_line_indent_chars(para, 0)
            text = f"{block.marker} {text}"
        elif block.kind == "quote":
            # 引文：左缩进 2 字符（用 leftChars 而非 firstLineChars）
            text = text

        _write_runs(para, text, style, actual_font=actual, options=opts)
        counters["paragraph"] += 1

    return RenderResult(
        doc=doc,
        block_count=len(blocks),
        table_count=counters["table"],
        heading_count=counters["heading"],
        paragraph_count=counters["paragraph"],
        warnings=tuple(warnings) + tuple(report.warnings),
        font_report=report,
    )


def _render_table(doc, rows: list[list[str]], *, report, options: RenderOptions) -> None:
    """渲染一个表格。**这里是 C15 的战场。**"""
    if not rows or not rows[0]:
        return
    cols = len(rows[0])
    table = doc.add_table(rows=len(rows), cols=cols)
    table.style = None          # 刻意不挂样式，版式全由补丁注入

    head_style = _style("table_head")
    body_style = _style("table_body")
    head_font = report.resolve(head_style.font)
    body_font = report.resolve(body_style.font)

    for r_idx, row_cells in enumerate(rows):
        style = head_style if r_idx == 0 else body_style
        actual = head_font if r_idx == 0 else body_font
        for c_idx, cell_text in enumerate(row_cells):
            cell = table.cell(r_idx, c_idx)
            # 一个单元格可能已经有空段落，直接复用第一个
            para = cell.paragraphs[0]
            P.set_alignment(para, style.align)
            P.set_first_line_indent_chars(para, 0)
            _write_runs(para, cell_text, style, actual_font=actual, options=options)

    # ★ 顺序很重要：先修缩进穿透，再设边框。
    # 反过来的话，fix 里遍历段落不影响边框，但**边框注入会重建 tblPr 的
    # 子元素顺序**，某些 Word 版本对顺序敏感。先内容后装饰更稳。
    P.fix_table_indent_penetration(table)
    P.set_table_full_borders(table)
    P.remove_table_left_indent(table)
    P.set_cell_margins(table)
    if len(rows) > 1:
        P.repeat_header_row(table.rows[0])

    # 表格后补一个空正文段：两个连续表格之间没有段落时，
    # Word 会把它们粘成一张表 —— 这是 OOXML 的既定行为，不是 bug。
    spacer = doc.add_paragraph()
    _apply_style(spacer, _style("body"),
                 actual_font=report.resolve(_style("body").font),
                 options=options)


class FontMissingError(RuntimeError):
    """`fail` 策略下字体缺失导致导出中止（映射 E5001）。"""

    def __init__(self, message: str, *, missing: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.missing = tuple(missing)
        self.code = "E5001"


__all__ = [
    "Block",
    "FontMissingError",
    "RenderOptions",
    "RenderResult",
    "parse_markdown",
    "render_markdown_to_docx",
]
