"""OOXML 样式补丁（T1-09，SAD C15）。

本模块的每个函数都对应一条"只靠 python-docx 的高层 API 做不到"的事。
核心原因只有一个：**python-docx 的高层 API 覆盖不了中文排版需要的
OOXML 属性**。`run.font.name = "仿宋_GB2312"` 看着像设了字体，其实
只写了 `w:rFonts/@w:ascii` 和 `@w:hAnsi`，中文（`@w:eastAsia`）没被写 ——
Word 于是回退到默认中文字体。这是部署手册 §7.5 排障表的第一行症状，
也是本卡"常见坑"里的第一条。

四条必须记牢的 OOXML 事实
-------------------------

**① `w:rFonts` 有四个属性，中文必须逐个设。**
`@w:ascii`（西文）、`@w:hAnsi`（西文扩展）、`@w:eastAsia`（东亚）、
`@w:cs`（复杂文种）。少设 `@w:eastAsia` 中文就不生效；
少设 `@w:ascii` 则西文会跟着中文字体走，数字和字母字形难看。
四个一起设，是唯一稳妥的写法。

**② 缩进有两套属性，同时存在时以 Chars 为准但兼容性有坑。**
`w:ind/@w:firstLine`（绝对值，单位 twips=1/20 pt）与
`w:ind/@w:firstLineChars`（单位 1/100 字符，随字号自适应）。
GB/T 9704 要求"首行缩进 2 字符"，指的是**字符**，不是固定磅值 ——
字号从三号改到小四时，2 字符的物理宽度应当跟着变。所以用 Chars。
但两套属性同时存在时，不同版本 Word 的处理不一致（有的以 Chars 为准，
有的相加）。本项目的统一做法：**写 Chars，同时把绝对值显式清零**，
让文档里只有一个真相。

**③ `w:ind` 的八个属性要全清，漏一个就会"穿透"。**
`firstLineChars` / `firstLine` / `leftChars` / `left` /
`rightChars` / `right` / `hangingChars` / `hanging`。
表格缩进穿透（C15）的根因是**表格继承了正文的段落样式**，
只清 `firstLine*` 而留着 `left*`，表格依然会整体右移。

**④ 表格边框必须写进 `w:tblBorders`，不能靠 `style`。**
`table.style = "Table Grid"` 在自带模板里有效，但我们的
`new_document()` 刻意不用自带模板（见 `_docx_backend`），
而且部分模板里该样式的边框定义已被剥掉。直接注入六个边最稳，
且能被 `tools/verify_docx_style.py` 直接断言。

单位换算备忘
------------
    twips  = pt × 20          （`w:firstLine` / `w:spacing/@w:line`）
    pt     = size_pt × 12700  （python-docx 的 EMU 用 `Pt()` 即可）
    1/100 字符 = chars × 100  （`w:firstLineChars`）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from mmwf.domain.docx._docx_backend import (
        _Document, _Element, _Paragraph, _Row, _Run, _Section, _Table,
    )

from mmwf.domain.docx._docx_backend import (
    Cm,
    OxmlElement,
    Pt,
    WD_ALIGN_PARAGRAPH,
    WD_LINE_SPACING,
    qn,
)

#: `w:ind` 上所有与"缩进"有关的属性。清缩进时**必须整组清**（事实 ③）。
INDENT_ATTRS: tuple[str, ...] = (
    "w:firstLineChars",
    "w:firstLine",
    "w:leftChars",
    "w:left",
    "w:rightChars",
    "w:right",
    "w:hangingChars",
    "w:hanging",
)

#: `w:rFonts` 上的四个字体属性（事实 ①）。
FONT_ATTRS: tuple[str, ...] = ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs")

#: `w:tblBorders` 的六个边（事实 ④）。
TABLE_EDGES: tuple[str, ...] = ("top", "left", "bottom", "right", "insideH", "insideV")


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

def set_cjk_font(
    run: _Run,
    font_name: str,
    size_pt: float | None = None,
    bold: bool | None = None,
    *,
    latin_font: str | None = None,
) -> None:
    """设置中文字体。**必须同时设 `w:eastAsia`**，否则中文回退到默认字体。

    `latin_font` 默认取 `style_mapper.LATIN_FONT`（Times New Roman）：
    公文正文的西文与数字走西文字体，中英混排才协调（见 style_mapper）。
    传 `latin_font=""` 可让西文也跟随中文字体。
    """
    if latin_font is None:
        from mmwf.domain.docx.style_mapper import LATIN_FONT

        latin_font = LATIN_FONT
    latin_font = latin_font or font_name

    run.font.name = font_name              # 高层 API：只写 ascii/hAnsi
    if size_pt is not None:
        run.font.size = Pt(size_pt)
    if bold is not None:
        run.font.bold = bold

    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        # 必须插在 rPr 的**首位**：OOXML 的 schema 规定 rFonts 是
        # rPr 的第一个子元素，位置错了 Word 会报"文档已损坏"。
        rpr.insert(0, rfonts)

    rfonts.set(qn("w:ascii"), latin_font)
    rfonts.set(qn("w:hAnsi"), latin_font)
    rfonts.set(qn("w:eastAsia"), font_name)     # ★ 中文生效的关键
    rfonts.set(qn("w:cs"), latin_font)

    # 附带清掉"主题字体"引用。模板里若写了 `w:asciiTheme`，
    # 它会**覆盖**我们刚设的 `w:ascii`（主题优先级更高）。
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rfonts.get(qn(attr)) is not None:
            del rfonts.attrib[qn(attr)]


# ---------------------------------------------------------------------------
# 缩进
# ---------------------------------------------------------------------------

def _get_or_add_ind(paragraph: _Paragraph) -> _Element:
    """取（或新建）该段落的 `w:ind` 元素。"""
    ppr = paragraph._p.get_or_add_pPr()
    ind = ppr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        ppr.append(ind)
    return ind


def set_first_line_indent_chars(paragraph: _Paragraph, chars: int) -> None:
    """首行缩进 N 字符（用 `w:firstLineChars`，随字号自适应）。

    `chars=0` 时**不删** `w:ind`，而是把八个属性全写 0 ——
    显式的 0 与"没有这个元素"在 Word 里含义不同：前者是
    "我明确要求不缩进"，后者是"继承上一级的设定"。表格要靠前者（C15）。
    """
    ind = _get_or_add_ind(paragraph)
    # 先把整套缩进清零，再按需设首行 —— 避免残留的 hanging/left 干扰
    for attr in INDENT_ATTRS:
        ind.set(qn(attr), "0")
    if chars:
        ind.set(qn("w:firstLineChars"), str(chars * 100))   # 1/100 字符
    # `w:firstLine` 保持 "0"：两套属性同时在时以 Chars 为准（事实 ②）


def zero_indent(paragraph: _Paragraph) -> None:
    """段落缩进八项全清零。表格内段落与表头专用（事实 ③、C15）。"""
    set_first_line_indent_chars(paragraph, 0)


# ---------------------------------------------------------------------------
# ★ 表格缩进穿透修复（C15，本卡最高频的坑）
# ---------------------------------------------------------------------------

def fix_table_indent_penetration(table: _Table) -> int:
    """修复表格段落被正文缩进穿透。返回处理过的段落数。

    **根因**：段落缩进是**段落属性**（`w:pPr/w:ind`），而 Word 里
    表格单元格内的段落默认会继承它所在位置的段落样式。我们把正文
    设成"首行缩进 2 字符"之后，表格单元格里的每个字都会跟着缩进 ——
    单元格本来就窄，缩进 2 字符会让文字挤出边框或被迫换行，
    表格看起来"破了个洞"。这就是 F5.3 说的"穿透"。

    **本函数做三件事，缺一不可**：

      1. 遍历**每一个**单元格的**每一个**段落（不只是第一个）——
         一个单元格里可能有多个段落（多行文字），只处理 `cell.paragraphs[0]`
         是极常见的漏改写法；
      2. 把 `w:ind` 八个属性全部写 0（事实 ③），而不只是首行那两项；
      3. 对齐设为左对齐 —— 正文若被设成两端对齐（`justify`），
         单元格里的短文本会被拉出难看的字间距。

    **为什么还要 `table.autofit = False`**：自适应表格在 Word 里会
    按内容重算列宽，正文缩进残留时列宽会被算歪。关掉自适应交给
    我们设定的列宽，输出才可复现。

    另注：本函数只清"单元格内段落"的缩进，**不动 `w:tblInd`**
    （表格整体相对页边的缩进）。那个是另一回事，公文表格通常顶格。
    """
    align = WD_ALIGN_PARAGRAPH()

    touched = 0
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                zero_indent(paragraph)
                paragraph.alignment = align.LEFT
                touched += 1

    # 关掉自适应：避免 Word 重算列宽时把残留缩进算进去
    table.autofit = False
    return touched


def set_table_full_borders(table: _Table, *, sz: int = 4, color: str = "000000") -> None:
    """表格全边框（六个边齐全）。

    `sz` 单位是 1/8 pt，所以 4 = 0.5 pt。GB/T 9704 对表格线宽没有
    硬性规定，0.5 pt 是公文表格的常规做法（够清楚，打印不糊）。

    **幂等**：重复调用会替换已有的 `w:tblBorders`，不会叠加出
    "两套边框"。OOXML 允许同一元素出现多次时以先出现的为准，
    残留旧边框会让后来的修改"看着没生效"。
    """
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblBorders"))
    if existing is not None:
        tbl_pr.remove(existing)

    borders = OxmlElement("w:tblBorders")
    for edge in TABLE_EDGES:
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tbl_pr.append(borders)


def repeat_header_row(row: _Row) -> None:
    """把某一行设为"跨页时重复的表头行"。

    公文表格跨页是常态（参会人员表、议定事项表都可能超过一页）。
    不设这个属性，第二页的表头会消失，读者得翻回去看哪列是什么。
    """
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


# ---------------------------------------------------------------------------
# 行距与对齐
# ---------------------------------------------------------------------------

def set_fixed_line_spacing(paragraph: _Paragraph, pt: float) -> None:
    """固定值行距（公文体常用 28.8 磅）。

    必须是**固定值**（`w:lineRule="exact"`）而不能是"多倍行距"：
    多倍行距随字号变，正文里插一个 22pt 的标题就会把那一行的行高
    撑开，每页行数不再是 22 —— GB/T 9704 的"每页 22 行"就废了。
    """
    pf = paragraph.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING().EXACTLY
    pf.line_spacing = Pt(pt)


def set_alignment(paragraph: _Paragraph, align: str) -> None:
    """按 `GBStyle.align` 的字符串设对齐。

    映射写成显式字典而不是 `getattr(WD_ALIGN_PARAGRAPH, align.upper())`：
    后者在拼错时抛 AttributeError（还行），但 `"outside"`（页码用）
    在 python-docx 里**没有**对应常量，会直接炸掉整次导出。
    页码的"外侧"由页脚奇偶设置处理，这里按左对齐占位。
    """
    align_enum = WD_ALIGN_PARAGRAPH()

    mapping = {
        "left": align_enum.LEFT,
        "center": align_enum.CENTER,
        "right": align_enum.RIGHT,
        "justify": align_enum.JUSTIFY,
        "outside": align_enum.LEFT,   # 占位，见 docstring
    }
    paragraph.alignment = mapping.get(align, align_enum.LEFT)


# ---------------------------------------------------------------------------
# 分栏 / 表格整体属性（备用，供后续卡使用）
# ---------------------------------------------------------------------------

def remove_table_left_indent(table: _Table) -> None:
    """清掉表格整体相对页边的缩进（`w:tblInd`）。

    与"单元格内段落缩进"是两回事，但症状相似（表格看起来没对齐）。
    公文表格一律顶格，所以这里固定写 0。
    """
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblInd"))
    if existing is not None:
        tbl_pr.remove(existing)
    ind = OxmlElement("w:tblInd")
    ind.set(qn("w:w"), "0")
    ind.set(qn("w:type"), "dxa")
    tbl_pr.append(ind)


def set_cell_margins(table: _Table, *, top: int = 28, start: int = 85,
                     bottom: int = 28, end: int = 85) -> None:
    """设置单元格内边距（单位 twips）。

    `28 twips ≈ 0.05 cm`（上下）、`85 twips ≈ 0.15 cm`（左右）——
    是 Word 默认值的十分之一左右，公文表格排得紧，默认内边距会
    让单元格显得空。这个值偏主观，做成参数方便各单位按习惯调。
    """
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblCellMar"))
    if existing is not None:
        tbl_pr.remove(existing)

    mar = OxmlElement("w:tblCellMar")
    for tag, val in (("top", top), ("start", start),
                     ("bottom", bottom), ("end", end)):
        el = OxmlElement(f"w:{tag}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tbl_pr.append(mar)


def set_page_margins(section: _Section, *, top_cm: float = 3.7, bottom_cm: float = 3.5,
                     left_cm: float = 2.8, right_cm: float = 2.6) -> None:
    """GB/T 9704 版心：A4 纸天头 37mm、地脚 35mm、订口 28mm、切口 26mm。

    这些数字不是随便取的 —— 标准规定的版心尺寸是 156mm × 225mm，
    与 A4（210 × 297）相减再分配，就得到上面四个值。
    """
    section.top_margin = Cm(top_cm)
    section.bottom_margin = Cm(bottom_cm)
    section.left_margin = Cm(left_cm)
    section.right_margin = Cm(right_cm)


def add_page_number_field(paragraph: _Paragraph, *, font_name: str = "宋体",
                          size_pt: float = 14) -> None:
    """在段落里插入 `PAGE` 域（页码）。

    python-docx **没有**页码的 API —— 这是它最常被抱怨的缺口之一。
    页码是一个"域"（field），OOXML 里要用三段式来写：

        <w:fldChar w:fldCharType="begin"/>   域开始
        <w:instrText>PAGE</w:instrText>      域指令
        <w:fldChar w:fldCharType="end"/>     域结束

    三段缺一不可：只写 instrText 是纯文本（不会自动更新），
    只写 begin/end 是空域（显示空白）。
    """
    run = paragraph.add_run()
    for ftype in ("begin", "separate", "end"):
        if ftype == "separate":
            # separate 与 end 之间是域的"显示内容"，留空让 Word 自己填
            pass
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), ftype)
        run._element.append(fld)
        if ftype == "begin":
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = "PAGE"
            run._element.append(instr)

    set_cjk_font(run, font_name, size_pt, latin_font=font_name)


def iter_paragraphs_in_tables(doc: _Document) -> Iterable[_Paragraph]:
    """遍历文档中所有表格里的所有段落（供 verify 脚本与测试用）。"""
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


__all__ = [
    "FONT_ATTRS",
    "INDENT_ATTRS",
    "TABLE_EDGES",
    "add_page_number_field",
    "fix_table_indent_penetration",
    "iter_paragraphs_in_tables",
    "remove_table_left_indent",
    "repeat_header_row",
    "set_alignment",
    "set_cell_margins",
    "set_cjk_font",
    "set_first_line_indent_chars",
    "set_fixed_line_spacing",
    "set_page_margins",
    "set_table_full_borders",
    "zero_indent",
]
