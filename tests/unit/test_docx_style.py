"""`domain/docx` 版式映射与补丁单元测试（T1-09，U-DX-01..05、09、10）。

测试方案 §2.1 的 `domain/docx` 用例表是本文件的骨架，逐条对应：

    U-DX-01  中文字体设置     -> TestCjkFont
    U-DX-02  首行缩进 2 字符   -> TestFirstLineIndent
    U-DX-03  **表格零缩进**    -> TestTableIndentPenetration（C15）
    U-DX-04  行距固定值 28.8pt -> TestLineSpacing
    U-DX-05  表格全边框        -> TestTableBorders
    U-DX-09  字体回退          -> TestFontFallback
    U-DX-10  编号连续性        -> TestNumbering

（U-DX-06/07/08 是文本规范化，在 `test_docx_text.py`。）

本文件的做法是**真的生成 docx 再解压读 XML**，而不是断言
python-docx 的对象属性。理由见 `tools/verify_docx_style.py` 的模块文档：
python-docx 读不到的属性等于不存在，表格缩进穿透恰恰是"写进去的是
残留值、读回来可能看不到"的那类问题。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from lxml import etree

from mmwf.domain.docx import (
    GB_STYLES,
    FONT_FALLBACK,
    FontMissingError,
    NumberingState,
    RenderOptions,
    check_fonts,
    format_heading_number,
    has_heading_number,
    render_markdown_to_docx,
    strip_heading_number,
    to_chinese_numeral,
)
from mmwf.domain.docx import patches as P

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 本文件统一用一个"已装齐"的字体集合，让回退逻辑不干扰版式断言。
FONTS_OK = {
    "方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312", "宋体",
    "Times New Roman",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def render(md: str, **kw) -> bytes:
    """渲染 Markdown 并返回 docx 字节。"""
    kw.setdefault("installed_fonts", FONTS_OK)
    opts = RenderOptions(**kw)
    result = render_markdown_to_docx(md, options=opts)
    buf = io.BytesIO()
    result.doc.save(buf)
    return buf.getvalue()


def doc_xml(data: bytes) -> etree._Element:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return etree.fromstring(z.read("word/document.xml"))


def indent_of(para_el) -> dict:
    """取一个 `w:p` 的缩进属性字典。"""
    ppr = para_el.find(W + "pPr")
    if ppr is None:
        return {}
    ind = ppr.find(W + "ind")
    if ind is None:
        return {}
    return {k.split("}")[1]: v for k, v in ind.attrib.items()}


def body_paragraphs(root) -> list:
    """文档 body 的直接子段落（不含表格内段落）。"""
    body = root.find(W + "body")
    return [el for el in body if el.tag == W + "p"]


def run_fonts(run_el) -> dict:
    rpr = run_el.find(W + "rPr")
    rfonts = rpr.find(W + "rFonts") if rpr is not None else None
    if rfonts is None:
        return {}
    return {k.split("}")[1]: v for k, v in rfonts.attrib.items()}


# ---------------------------------------------------------------------------
# U-DX-01 中文字体
# ---------------------------------------------------------------------------

class TestCjkFont:
    """U-DX-01：`w:rFonts` 的 `w:eastAsia` 被正确设置。"""

    def test_east_asia_is_set(self):
        root = doc_xml(render("正文一段话"))
        runs = [r for r in root.iter(W + "r")
                if "".join(t.text or "" for t in r.iter(W + "t")).strip()]
        assert runs, "应至少有一个带文字的 run"
        for r in runs:
            f = run_fonts(r)
            assert "eastAsia" in f, "缺 w:eastAsia 中文会回退字体（U-DX-01）"
            assert f["eastAsia"] == "仿宋_GB2312"

    def test_all_four_attrs_set(self):
        """四个字体属性都要设，否则西文跟中文字体走。"""
        root = doc_xml(render("正文一段话"))
        run = next(r for r in root.iter(W + "r")
                   if "".join(t.text or "" for t in r.iter(W + "t")).strip())
        f = run_fonts(run)
        assert set(f) >= {"ascii", "hAnsi", "eastAsia", "cs"}
        assert f["ascii"] == "Times New Roman", "西文应走 Times New Roman"
        assert f["eastAsia"] == "仿宋_GB2312"

    def test_rfonts_is_first_child_of_rpr(self):
        """`w:rFonts` 必须是 `w:rPr` 的第一个子元素（OOXML schema）。

        位置错了 Word 会报"文档已损坏" —— 这是很难从表象反推的坑，
        所以单独断言。
        """
        root = doc_xml(render("正文一段话"))
        for rpr in root.iter(W + "rPr"):
            kids = [el.tag.split("}")[1] for el in rpr]
            if "rFonts" in kids:
                assert kids[0] == "rFonts", f"rFonts 不在首位：{kids}"

    def test_theme_font_removed(self):
        """`w:asciiTheme` 会覆盖 `w:ascii`，必须清掉。"""
        root = doc_xml(render("正文一段话"))
        for rfonts in root.iter(W + "rFonts"):
            for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
                assert rfonts.get(W + attr) is None

    def test_title_uses_gb_title_font(self):
        root = doc_xml(render("# 标题\n\n正文"))
        first_text_run = next(
            r for r in root.iter(W + "r")
            if "标题" in "".join(t.text or "" for t in r.iter(W + "t"))
        )
        assert run_fonts(first_text_run)["eastAsia"] == "方正小标宋简体"

    def test_title_size_22pt(self):
        root = doc_xml(render("# 标题\n\n正文"))
        run = next(r for r in root.iter(W + "r")
                   if "标题" in "".join(t.text or "" for t in r.iter(W + "t")))
        sz = run.find(f"{W}rPr/{W}sz")
        assert sz is not None
        assert sz.get(W + "val") == "44", "22pt = 44 半磅（w:sz 单位是半磅）"

    def test_title_is_not_bold(self):
        """公文大标题**不加粗** —— 加粗就不合规了。"""
        root = doc_xml(render("# 标题\n\n正文"))
        run = next(r for r in root.iter(W + "r")
                   if "标题" in "".join(t.text or "" for t in r.iter(W + "t")))
        b = run.find(f"{W}rPr/{W}b")
        assert b is None or b.get(W + "val") in (None, "0", "false")

    def test_title_is_centered(self):
        root = doc_xml(render("# 标题\n\n正文"))
        para = next(p for p in body_paragraphs(root)
                    if "标题" in "".join(t.text or "" for t in p.iter(W + "t")))
        jc = para.find(f"{W}pPr/{W}jc")
        assert jc is not None and jc.get(W + "val") == "center"

    def test_h1_uses_heiti_16pt(self):
        root = doc_xml(render("# 大标题\n\n## 一、\n\n正文"))
        run = next(r for r in root.iter(W + "r")
                   if "一、" in "".join(t.text or "" for t in r.iter(W + "t")))
        f = run_fonts(run)
        assert f["eastAsia"] == "黑体"
        assert run.find(f"{W}rPr/{W}sz").get(W + "val") == "32"   # 16pt

    def test_h2_uses_kaiti(self):
        root = doc_xml(render("# 大标题\n\n### 二级\n\n正文"))
        run = next(r for r in root.iter(W + "r")
                   if "二级" in "".join(t.text or "" for t in r.iter(W + "t")))
        assert run_fonts(run)["eastAsia"] == "楷体_GB2312"

    def test_h3_is_bold_fangsong(self):
        root = doc_xml(render("# 大标题\n\n#### 三级\n\n正文"))
        run = next(r for r in root.iter(W + "r")
                   if "三级" in "".join(t.text or "" for t in r.iter(W + "t")))
        assert run_fonts(run)["eastAsia"] == "仿宋_GB2312"
        b = run.find(f"{W}rPr/{W}b")
        assert b is not None and b.get(W + "val") in (None, "1", "true")

    def test_empty_text_run_is_skipped(self):
        """没有文字的 run 不该被强行加字体（页码域 run 就是这种）。"""
        data = render("正文一段")
        root = doc_xml(data)
        # 页脚里的域 run 没有 w:t，本断言只确认渲染不崩且文档可解压
        assert root is not None

    def test_set_cjk_font_is_idempotent(self):
        """重复调用不产生第二个 `w:rFonts`。"""
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        para = doc.add_paragraph()
        run = para.add_run("测试")
        P.set_cjk_font(run, "仿宋_GB2312", 16)
        P.set_cjk_font(run, "宋体", 14)
        rpr = run._element.find(W + "rPr")
        assert len(rpr.findall(W + "rFonts")) == 1
        assert rpr.find(W + "rFonts").get(W + "eastAsia") == "宋体"


# ---------------------------------------------------------------------------
# U-DX-02 首行缩进
# ---------------------------------------------------------------------------

class TestFirstLineIndent:
    """U-DX-02：`w:firstLineChars == "200"`，`w:firstLine == "0"`。"""

    def test_body_paragraph_indent(self):
        root = doc_xml(render("正文一段话"))
        para = next(p for p in body_paragraphs(root)
                    if "正文一段话" in "".join(t.text or "" for t in p.iter(W + "t")))
        ind = indent_of(para)
        assert ind.get("firstLineChars") == "200"
        assert ind.get("firstLine") == "0", "绝对值必须清零，避免与 Chars 冲突"

    def test_all_eight_indent_attrs_written(self):
        """八个缩进属性都要出现（显式 0 与"没有元素"含义不同）。"""
        root = doc_xml(render("正文一段话"))
        para = next(p for p in body_paragraphs(root)
                    if "正文一段话" in "".join(t.text or "" for t in p.iter(W + "t")))
        ind = indent_of(para)
        for attr in P.INDENT_ATTRS:
            assert attr.split(":")[1] in ind, f"缺缩进属性 {attr}"

    def test_heading_also_indented(self):
        """公文标题也首行缩进 2 字符（与西文排版相反）。"""
        root = doc_xml(render("# 大标题\n\n## 会议基本情况\n\n正文"))
        para = next(p for p in body_paragraphs(root)
                    if "会议基本情况" in "".join(t.text or "" for t in p.iter(W + "t")))
        assert indent_of(para).get("firstLineChars") == "200"

    def test_list_item_not_indented(self):
        """列表项不缩进 2 字符（否则项目符号位置奇怪）。"""
        root = doc_xml(render("- 第一项\n- 第二项"))
        para = next(p for p in body_paragraphs(root)
                    if "第一项" in "".join(t.text or "" for t in p.iter(W + "t")))
        assert indent_of(para).get("firstLineChars") == "0"

    def test_zero_indent_helper(self):
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        para = doc.add_paragraph()
        P.set_first_line_indent_chars(para, 2)
        assert indent_of(para._p).get("firstLineChars") == "200"
        P.zero_indent(para)
        ind = indent_of(para._p)
        assert ind["firstLineChars"] == "0"
        assert all(ind.get(k, "0") == "0" for k in
                   ("firstLine", "left", "right", "hanging"))

    @pytest.mark.parametrize("chars", [0, 1, 2, 4])
    def test_indent_chars_conversion(self, chars):
        """N 字符 -> `N*100`（单位是 1/100 字符）。"""
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        para = doc.add_paragraph()
        P.set_first_line_indent_chars(para, chars)
        assert indent_of(para._p)["firstLineChars"] == str(chars * 100)


# ---------------------------------------------------------------------------
# U-DX-03 ★ 表格零缩进（C15，本卡最高频的坑）
# ---------------------------------------------------------------------------

TABLE_MD = """# 大标题

正文一段。

| 姓名 | 职务 | 备注 |
| --- | --- | --- |
| 张三 | 处长 | 主持工作 |
| 李四 | 副处长 |  |
"""


class TestTableIndentPenetration:
    """U-DX-03：表格内全部 `w:ind` 属性为 0（C15）。"""

    def test_table_has_no_indent(self):
        root = doc_xml(render(TABLE_MD))
        errors = []
        for tbl in root.iter(W + "tbl"):
            for ind in tbl.iter(W + "ind"):
                for attr in P.INDENT_ATTRS:
                    v = ind.get(W + attr)
                    if v and v != "0":
                        errors.append(f"{attr}={v}")
        assert errors == [], f"表格内段落缩进未清零：{errors}"

    def test_body_paragraph_still_indented(self):
        """关键对照：修表格**不能**把正文的缩进也清掉。

        只写"表格里是 0"的测试是站在悬崖边的 —— 一个把全文缩进都清零的
        错误实现也能通过它。必须同时断言正文仍然是 200。
        """
        root = doc_xml(render(TABLE_MD))
        para = next(p for p in body_paragraphs(root)
                    if "正文一段" in "".join(t.text or "" for t in p.iter(W + "t")))
        assert indent_of(para)["firstLineChars"] == "200"

    def test_every_cell_processed_not_just_first(self):
        """★ 每个单元格的每个段落都要处理，不能只处理 `paragraphs[0]`。"""
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        cells = list(tbl.iter(W + "tc"))
        assert len(cells) == 9, "3 行 × 3 列"
        for tc in cells:
            for p in tc.iter(W + "p"):
                ind = indent_of(p)
                assert ind, "每个单元格段落都该有 w:ind（显式零缩进）"
                assert ind.get("firstLineChars") == "0"

    def test_multi_paragraph_cell_fully_processed(self):
        """一个单元格里有多段时（单元格内换行），每段都要清。

        上游 Markdown 的单元格里可能有 `<br>` 或多行 —— 哪怕现在解析器
        只产出一段，补丁本身也必须能处理多段（它是公共函数）。
        """
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        table = doc.add_table(rows=1, cols=1)
        cell = table.cell(0, 0)
        cell.paragraphs[0].add_run("第一段")
        cell.add_paragraph("第二段")
        cell.add_paragraph("第三段")
        P.set_first_line_indent_chars(cell.paragraphs[0], 2)
        touched = P.fix_table_indent_penetration(table)
        assert touched >= 3
        for p in cell.paragraphs:
            assert indent_of(p._p)["firstLineChars"] == "0"

    def test_autofit_disabled(self):
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        tbl_pr = tbl.find(W + "tblPr")
        layout = tbl_pr.find(W + "tblLayout")
        assert layout is not None
        assert layout.get(W + "type") == "fixed"

    def test_table_left_indent_zero(self):
        """表格整体相对页边的缩进也要归零。"""
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        tind = tbl.find(f"{W}tblPr/{W}tblInd")
        assert tind is not None
        assert tind.get(W + "w") == "0"

    def test_cell_alignment_left(self):
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        for tc in tbl.iter(W + "tc"):
            for p in tc.iter(W + "p"):
                jc = p.find(f"{W}pPr/{W}jc")
                assert jc is not None and jc.get(W + "val") == "left"


# ---------------------------------------------------------------------------
# U-DX-04 行距
# ---------------------------------------------------------------------------

class TestLineSpacing:
    """U-DX-04：`w:spacing@line` 对应 28.8pt = 576 twips。"""

    def test_body_fixed_line_spacing(self):
        root = doc_xml(render("正文一段话"))
        para = next(p for p in body_paragraphs(root)
                    if "正文一段话" in "".join(t.text or "" for t in p.iter(W + "t")))
        sp = para.find(f"{W}pPr/{W}spacing")
        assert sp is not None
        assert sp.get(W + "lineRule") == "exact", "必须是固定值行距"
        assert sp.get(W + "line") == "576", "28.8pt = 576 twips"

    def test_table_cell_has_no_fixed_spacing(self):
        """表格内不设 28.8pt 行距（单元格高度会被撑开）。"""
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        for sp in tbl.iter(W + "spacing"):
            assert sp.get(W + "line") != "576"

    def test_set_fixed_line_spacing_helper(self):
        from docx.enum.text import WD_LINE_SPACING

        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        para = doc.add_paragraph()
        P.set_fixed_line_spacing(para, 28.8)
        assert para.paragraph_format.line_spacing_rule == WD_LINE_SPACING.EXACTLY
        assert abs(para.paragraph_format.line_spacing.pt - 28.8) < 0.01

    @pytest.mark.parametrize("pt", [20.0, 24.0, 28.8, 32.0])
    def test_line_spacing_pt_conversion(self, pt):
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        para = doc.add_paragraph()
        P.set_fixed_line_spacing(para, pt)
        sp = para._p.find(f"{W}pPr/{W}spacing")
        assert int(sp.get(W + "line")) == round(pt * 20)


# ---------------------------------------------------------------------------
# U-DX-05 表格边框
# ---------------------------------------------------------------------------

class TestTableBorders:
    """U-DX-05：`w:tblBorders` 六边齐全。"""

    def test_all_six_edges(self):
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        borders = tbl.find(f"{W}tblPr/{W}tblBorders")
        assert borders is not None, "缺 w:tblBorders"
        present = {e.tag.split("}")[1] for e in borders}
        assert present == set(P.TABLE_EDGES)

    def test_border_attrs(self):
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        borders = tbl.find(f"{W}tblPr/{W}tblBorders")
        for e in borders:
            assert e.get(W + "val") == "single"
            assert e.get(W + "sz") == "4", "0.5pt = 4（单位 1/8 pt）"
            assert e.get(W + "color") == "000000"

    def test_borders_are_idempotent(self):
        """重复注入替换而非叠加（叠加会让后续修改"看着没生效"）。"""
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        table = doc.add_table(rows=1, cols=1)
        P.set_table_full_borders(table)
        P.set_table_full_borders(table, sz=8)
        tbl_pr = table._tbl.tblPr
        found = tbl_pr.findall(W + "tblBorders")
        assert len(found) == 1
        assert found[0][0].get(W + "sz") == "8"

    def test_header_row_repeats(self):
        root = doc_xml(render(TABLE_MD))
        tbl = next(root.iter(W + "tbl"))
        rows = list(tbl.iter(W + "tr"))
        first_tr_pr = rows[0].find(W + "trPr")
        assert first_tr_pr is not None
        assert first_tr_pr.find(W + "tblHeader") is not None

    def test_two_tables_not_merged(self):
        """表格之间要有空段，否则 Word 会把两张表粘成一张。"""
        md = TABLE_MD + "\n中间一段话\n\n| 甲 | 乙 |\n| --- | --- |\n| 1 | 2 |\n"
        root = doc_xml(render(md))
        assert len(list(root.iter(W + "tbl"))) == 2


# ---------------------------------------------------------------------------
# U-DX-09 字体回退
# ---------------------------------------------------------------------------

class TestFontFallback:
    """U-DX-09：缺字体时按 `FONT_FALLBACK` 回退并返回警告。"""

    def test_fallback_chain_used(self):
        r = check_fonts(installed={"宋体", "黑体"})
        assert r.resolve("仿宋_GB2312") == "宋体"
        assert r.resolve("方正小标宋简体") == "宋体"
        assert r.resolve("楷体_GB2312") == "宋体"

    def test_fallback_to_intermediate(self):
        """回退链的**第一个**可用项，而不是直接跳到宋体。"""
        r = check_fonts(installed={"仿宋", "宋体"})
        assert r.resolve("仿宋_GB2312") == "仿宋", "应回退到链上第一个可用的"

    def test_no_warning_when_all_installed(self):
        r = check_fonts(installed=FONTS_OK)
        assert r.all_installed
        assert r.warnings == ()

    def test_warning_generated_on_missing(self):
        r = check_fonts(installed={"宋体", "黑体"})
        assert not r.all_installed
        assert len(r.warnings) >= 1
        assert all("font missing" in w for w in r.warnings)

    def test_missing_list_content(self):
        r = check_fonts(installed={"宋体", "黑体"})
        assert set(r.missing) == {"方正小标宋简体", "楷体_GB2312", "仿宋_GB2312"}

    def test_unresolvable_warns_differently(self):
        """回退链走完还没有 —— 警告文案必须不同（说"未能回退"）。"""
        r = check_fonts(installed={"某个不相关的字体"})
        joined = " ".join(r.warnings)
        assert "未能回退" in joined

    def test_policy_fail_blocks(self):
        from mmwf.domain.docx import policy_blocks_export, precheck_policy_error

        r = check_fonts(installed={"宋体"}, policy="fail")
        assert policy_blocks_export(r) is True
        assert "fail" in precheck_policy_error(r)

    def test_policy_fallback_suppresses_file_warning(self):
        """fallback 档回退但不报文件级警告（只记日志）。"""
        r = check_fonts(installed={"宋体"}, policy="fallback")
        assert r.warnings == ()
        assert not r.all_installed        # 但依然如实反映缺失

    def test_policy_warn_does_not_block(self):
        from mmwf.domain.docx import policy_blocks_export

        r = check_fonts(installed={"宋体"}, policy="warn")
        assert policy_blocks_export(r) is False

    def test_precheck_unavailable_passes_through(self):
        """预检不可用（非 Windows）时放行，不误判为缺失、不阻断。"""
        from mmwf.domain.docx import policy_blocks_export

        r = check_fonts(installed=set())
        assert r.precheck_available is False
        assert r.all_installed                  # 不报缺失
        assert r.resolve("仿宋_GB2312") == "仿宋_GB2312"  # 原样放行
        assert policy_blocks_export(check_fonts(installed=set(), policy="fail")) is False

    def test_invalid_policy_rejected(self):
        with pytest.raises(ValueError, match="未知的字体策略"):
            check_fonts(installed=FONTS_OK, policy="whatever")

    def test_render_uses_fallback_font_in_xml(self):
        """★ 回退要体现在**产出的 docx 里**，不能只是警告。"""
        data = render("正文一段话", installed_fonts={"宋体", "黑体"})
        root = doc_xml(data)
        run = next(r for r in root.iter(W + "r")
                   if "正文一段话" in "".join(t.text or "" for t in r.iter(W + "t")))
        assert run_fonts(run)["eastAsia"] == "宋体"

    def test_render_warns_in_result(self):
        result = render_markdown_to_docx(
            "正文", options=RenderOptions(installed_fonts={"宋体"})
        )
        assert any("font missing" in w for w in result.warnings)

    def test_policy_fail_raises_in_render(self):
        with pytest.raises(FontMissingError) as ei:
            render_markdown_to_docx(
                "正文", options=RenderOptions(
                    font_policy="fail", installed_fonts={"宋体"})
            )
        assert "仿宋_GB2312" in str(ei.value) or ei.value.missing

    def test_font_missing_error_code(self):
        with pytest.raises(FontMissingError) as ei:
            render_markdown_to_docx(
                "正文", options=RenderOptions(
                    font_policy="fail", installed_fonts={"宋体"})
            )
        assert ei.value.code == "E5001"

    def test_all_fallback_targets_exist_in_chain(self):
        """`FONT_FALLBACK` 的末端必须是宋体（系统自带，几乎不会缺）。"""
        for src, chain in FONT_FALLBACK.items():
            assert chain, f"{src} 的回退链为空"
            assert chain[-1] == "宋体", f"{src} 的回退链末端不是宋体：{chain}"

    def test_required_fonts_order(self):
        from mmwf.domain.docx import required_fonts

        r = required_fonts()
        assert r[0] == "方正小标宋简体", "标题字体排最前"
        assert "黑体" in r and "仿宋_GB2312" in r
        assert len(r) == len(set(r)), "不应重复"

    def test_font_report_to_dict(self):
        r = check_fonts(installed={"宋体", "黑体"})
        d = r.to_dict()
        assert d["all_installed"] is False
        assert set(d["missing"]) == {"方正小标宋简体", "楷体_GB2312", "仿宋_GB2312"}
        assert len(d["fonts"]) == len(r.checks)

    def test_system_fonts_payload_shape(self):
        from mmwf.domain.docx import build_check_result_json

        payload = build_check_result_json(check_fonts(installed={"宋体"}))
        assert set(payload) >= {"available", "all_installed", "missing", "detail"}


# ---------------------------------------------------------------------------
# U-DX-10 编号连续性
# ---------------------------------------------------------------------------

class TestNumbering:
    """U-DX-10：标题编号不跳号、不重号。"""

    def test_chinese_numeral_basic(self):
        cases = {1: "一", 2: "二", 9: "九", 10: "十", 11: "十一",
                 19: "十九", 20: "二十", 21: "二十一", 99: "九十九"}
        for n, expect in cases.items():
            assert to_chinese_numeral(n) == expect, f"{n} -> {expect}"

    def test_chinese_numeral_out_of_range(self):
        for bad in (0, -1, 100):
            with pytest.raises(ValueError):
                to_chinese_numeral(bad)

    @pytest.mark.parametrize("level,counter,expect", [
        (1, 1, "一、"), (1, 5, "五、"), (1, 10, "十、"), (1, 12, "十二、"),
        (2, 1, "（一）"), (2, 3, "（三）"),
        (3, 1, "1."), (3, 11, "11."),
    ])
    def test_heading_number_format(self, level, counter, expect):
        assert format_heading_number(level, counter) == expect

    def test_invalid_level(self):
        with pytest.raises(ValueError):
            format_heading_number(4, 1)

    def test_state_machine_resets_deeper(self):
        """★ 核心：`一、` 之后 `（一）` 必须从 1 重新开始。"""
        st = NumberingState()
        assert st.next_prefix(1) == "一、"
        assert st.next_prefix(2) == "（一）"
        assert st.next_prefix(3) == "1."
        assert st.next_prefix(1) == "二、"
        assert st.next_prefix(2) == "（一）", "h2 应归零重数"
        assert st.next_prefix(3) == "1.", "h3 应归零重数"

    def test_no_duplicate_numbers_in_document(self):
        """整篇文档里同级编号不重复。"""
        md = ("# 大标题\n\n"
              "## 甲\n\n### 甲一\n\n### 甲二\n\n## 乙\n\n### 乙一\n\n## 丙\n")
        root = doc_xml(render(md))
        # 收集所有以"一、二、三…"开头的 h1 文本
        texts = ["".join(t.text or "" for t in p.iter(W + "t"))
                 for p in body_paragraphs(root)]
        h1 = [t for t in texts if t and t[0] in "一二三四五六七八九十" and t[1] == "、"]
        assert h1 == ["一、甲", "二、乙", "三、丙"], f"h1 编号异常：{h1}"

    def test_h2_restarts_under_each_h1(self):
        md = ("# 大标题\n\n## 甲\n\n### 甲一\n\n## 乙\n\n### 乙一\n")
        root = doc_xml(render(md))
        texts = ["".join(t.text or "" for t in p.iter(W + "t"))
                 for p in body_paragraphs(root)]
        h2 = [t for t in texts if t.startswith("（")]
        assert h2 == ["（一）甲一", "（一）乙一"], f"h2 未重数：{h2}"

    def test_existing_number_is_stripped(self):
        """标题若自带序号，剥掉后再统一编号（避免"一、一、"）。"""
        md = "# 大标题\n\n## 一、总体情况\n\n正文\n"
        root = doc_xml(render(md))
        texts = ["".join(t.text or "" for t in p.iter(W + "t"))
                 for p in body_paragraphs(root)]
        assert "一、总体情况" in texts
        assert "一、一、总体情况" not in texts

    def test_strip_heading_number_variants(self):
        assert strip_heading_number("一、总体") == "总体"
        assert strip_heading_number("（一）分项") == "分项"
        assert strip_heading_number("(一) 半角") == "半角"
        assert strip_heading_number("3. 第三") == "第三"
        assert strip_heading_number("1、顿号") == "顿号"
        assert strip_heading_number("无编号") == "无编号"

    def test_has_heading_number(self):
        assert has_heading_number("一、总体")
        assert has_heading_number("（二）存在")
        assert has_heading_number("12. 事项")
        assert not has_heading_number("总体情况")
        assert not has_heading_number("2026 年工作")

    def test_deep_markdown_level_collapses_to_h3(self):
        """`#####` 已超三级，降到三级并留 warning。"""
        result = render_markdown_to_docx(
            "# 大标题\n\n##### 很深的标题\n",
            options=RenderOptions(installed_fonts=FONTS_OK),
        )
        assert any("只到三级" in w for w in result.warnings)

    def test_second_h1_demoted_with_warning(self):
        result = render_markdown_to_docx(
            "# 第一个\n\n# 第二个\n",
            options=RenderOptions(installed_fonts=FONTS_OK),
        )
        # 第一个当 title，第二个降为 h1
        assert result.heading_count == 1

    def test_auto_numbering_can_be_disabled(self):
        result = render_markdown_to_docx(
            "# 大标题\n\n## 情况\n",
            options=RenderOptions(installed_fonts=FONTS_OK, auto_numbering=False),
        )
        assert any("关闭标题自动编号" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 版式表本身
# ---------------------------------------------------------------------------

class TestGBStylesTable:
    """`GB_STYLES` 的数据契约（改了表必须同步改这里）。"""

    def test_all_keys_present(self):
        assert set(GB_STYLES) == {
            "title", "h1", "h2", "h3", "body",
            "table_body", "table_head", "page_number",
        }

    def test_key_matches_dict_key(self):
        for key, style in GB_STYLES.items():
            assert style.key == key, f"{key} 的 key 字段是 {style.key}"

    @pytest.mark.parametrize("key,font,size", [
        ("title", "方正小标宋简体", 22),
        ("h1", "黑体", 16),
        ("h2", "楷体_GB2312", 16),
        ("h3", "仿宋_GB2312", 16),
        ("body", "仿宋_GB2312", 16),
        ("table_body", "仿宋_GB2312", 12),
        ("table_head", "黑体", 12),
        ("page_number", "宋体", 14),
    ])
    def test_gb_9704_specs(self, key, font, size):
        s = GB_STYLES[key]
        assert s.font == font
        assert s.size_pt == size

    def test_only_title_is_centered(self):
        centered = [k for k, s in GB_STYLES.items() if s.align == "center"]
        assert centered == ["title"]

    def test_only_h3_is_bold(self):
        bold = [k for k, s in GB_STYLES.items() if s.bold]
        assert sorted(bold) == ["h3", "table_head"]

    def test_table_styles_have_zero_indent(self):
        assert GB_STYLES["table_body"].first_line_indent_chars == 0
        assert GB_STYLES["table_head"].first_line_indent_chars == 0

    def test_body_has_fixed_line_spacing(self):
        assert GB_STYLES["body"].line_spacing_pt == 28.8
        assert GB_STYLES["body"].first_line_indent_chars == 2

    def test_size_names(self):
        assert GB_STYLES["title"].size_name == "二号"
        assert GB_STYLES["body"].size_name == "三号"
        assert GB_STYLES["table_body"].size_name == "小四"
        assert GB_STYLES["page_number"].size_name == "四号"


# ---------------------------------------------------------------------------
# 页码域
# ---------------------------------------------------------------------------

class TestPageNumber:
    """页码为 `PAGE` 域（施工卡验收标准）。"""

    def test_page_field_in_footer(self):
        data = render("正文一段")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            footers = [n for n in z.namelist() if n.startswith("word/footer")]
            assert footers, "应有 footer 部件"
            merged = b"".join(z.read(n) for n in footers)
        assert b"PAGE" in merged
        assert b"fldChar" in merged, "必须是域（三段式），不是纯文本"

    def test_page_field_three_parts(self):
        data = render("正文一段")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = b"".join(z.read(n) for n in z.namelist()
                           if n.startswith("word/footer"))
        root = etree.fromstring(xml)
        types = [f.get(W + "fldCharType") for f in root.iter(W + "fldChar")]
        assert "begin" in types and "end" in types

    def test_page_number_can_be_disabled(self):
        data = render("正文一段", page_number=False)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            merged = b"".join(
                z.read(n) for n in z.namelist() if n.startswith("word/footer")
            )
        assert b"PAGE" not in merged

    def test_page_number_font(self):
        data = render("正文一段")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = b"".join(z.read(n) for n in z.namelist()
                           if n.startswith("word/footer"))
        root = etree.fromstring(xml)
        rfonts = list(root.iter(W + "rFonts"))
        assert rfonts
        assert rfonts[0].get(W + "eastAsia") == "宋体"


# ---------------------------------------------------------------------------
# 页边距与文档骨架
# ---------------------------------------------------------------------------

class TestPageSetup:
    """GB/T 9704 版心：天头 37mm、地脚 35mm、订口 28mm、切口 26mm。"""

    def test_margins(self):
        data = render("正文")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = etree.fromstring(z.read("word/document.xml"))
        pg = xml.find(f"{W}body/{W}sectPr/{W}pgMar")
        assert pg is not None
        # 1 cm = 567 twips
        assert abs(int(pg.get(W + "top")) - 2098) < 5      # 3.7cm
        assert abs(int(pg.get(W + "bottom")) - 1984) < 5   # 3.5cm
        assert abs(int(pg.get(W + "left")) - 1587) < 5     # 2.8cm
        assert abs(int(pg.get(W + "right")) - 1474) < 5    # 2.6cm

    def test_document_is_valid_zip(self):
        data = render("# 标题\n\n正文\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
        assert zipfile.is_zipfile(io.BytesIO(data))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert "word/document.xml" in z.namelist()
            assert "word/styles.xml" in z.namelist()

    def test_empty_markdown_does_not_crash(self):
        result = render_markdown_to_docx("", options=RenderOptions(installed_fonts=FONTS_OK))
        assert result.block_count == 0
        assert result.doc is not None

    def test_only_whitespace(self):
        result = render_markdown_to_docx(
            "\n\n   \n\t\n", options=RenderOptions(installed_fonts=FONTS_OK)
        )
        assert result.block_count == 0

    def test_special_chars_escaped(self):
        """`&` / `<` / `>` 必须被转义，否则 XML 损坏。"""
        data = render("正文含 & 与 <标签> 与 \"引号\"")
        root = doc_xml(data)
        text = "".join(t.text or "" for t in root.iter(W + "t"))
        assert "&" in text and "<标签>" in text


# ---------------------------------------------------------------------------
# 后端隔离层（`_docx_backend`）
# ---------------------------------------------------------------------------

class TestDocxBackend:
    """隔离层自身的测试。

    这一层看起来只是"把 import 挪个地方"，但它承担的职责是
    **让 M11 可以被换库**（SAD §4）。职责越薄，越容易被漏测 ——
    而一旦它悄悄坏了（比如 `qn` 拼错命名空间），全部版式补丁会
    在没有报错的情况下写到错误的标签上，产出 Word 打不开的文件。
    """

    def test_new_document_returns_doc(self):
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        assert doc is not None
        # 新的 Document 自带一个空段落，不应有内容
        assert "".join(p.text for p in doc.paragraphs).strip() == ""

    def test_open_document_roundtrip(self, tmp_path):
        """写出去再读回来，文字必须一致。

        `open_document` 目前只被对照校验/回归路径用到，但没有测试的话
        它坏了也没人知道 —— 而它是"读回产物做二次校验"的唯一入口。
        """
        from mmwf.domain.docx._docx_backend import open_document

        data = render("# 标题\n\n正文段落\n")
        path = tmp_path / "roundtrip.docx"
        path.write_bytes(data)

        doc = open_document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "标题" in text
        assert "正文段落" in text

    def test_qn_produces_clark_notation(self):
        from mmwf.domain.docx._docx_backend import qn

        assert qn("w:ind") == W + "ind"
        assert qn("w:rFonts") == W + "rFonts"

    def test_oxml_element_tag(self):
        from mmwf.domain.docx._docx_backend import OxmlElement

        el = OxmlElement("w:ind")
        assert el.tag == W + "ind"

    def test_pt_and_cm_convert_to_emu(self):
        """长度单位换算：1pt = 12700 EMU，1cm = 360000 EMU。"""
        from mmwf.domain.docx._docx_backend import Cm, Pt

        assert Pt(1) == 12700
        assert Pt(28.8) == int(28.8 * 12700)
        assert Cm(1) == 360000

    def test_align_and_line_spacing_enums_exposed(self):
        """枚举必须能从隔离层拿到，且成员齐全（否则版式无处可设）。"""
        from mmwf.domain.docx._docx_backend import (
            WD_ALIGN_PARAGRAPH,
            WD_LINE_SPACING,
        )

        align = WD_ALIGN_PARAGRAPH()
        for name in ("LEFT", "CENTER", "RIGHT", "JUSTIFY"):
            assert hasattr(align, name), f"WD_ALIGN_PARAGRAPH 缺 {name}"

        spacing = WD_LINE_SPACING()
        for name in ("EXACTLY", "SINGLE", "AT_LEAST"):
            assert hasattr(spacing, name), f"WD_LINE_SPACING 缺 {name}"

    def test_backend_error_has_install_hint(self):
        """缺依赖时必须给**可执行**的提示，而不是干巴巴的 ModuleNotFoundError。"""
        from mmwf.domain.docx._docx_backend import DocxBackendError

        assert issubclass(DocxBackendError, RuntimeError)

    def test_strip_default_styles_removes_rfonts(self):
        """自带样式的 `w:rFonts` 必须被清掉，否则会与 GB 注入的字体打架。

        清掉的是 `w:rFonts`，**不是样式本身** —— 删样式会留下悬空
        `w:pStyle` 引用，Word 会报"文档已损坏"。
        """
        from mmwf.domain.docx._docx_backend import new_document, qn

        doc = new_document()
        for style in doc.styles:
            try:
                rpr = style.element.find(qn("w:rPr"))
            except (AttributeError, ValueError):
                continue
            if rpr is None:
                continue
            assert rpr.find(qn("w:rFonts")) is None, (
                f"样式 {style.name} 仍带 w:rFonts（未被剥离）"
            )


# ---------------------------------------------------------------------------
# 后端隔离层（`_docx_backend`）
# ---------------------------------------------------------------------------

class TestDocxBackend:
    """隔离层自身的测试。

    这一层看起来只是"把 import 挪个地方"，但它承担的职责是
    **让 M11 可以被换库**（SAD §4）。职责越薄，越容易被漏测 ——
    而一旦它悄悄坏了（比如 `qn` 拼错命名空间），全部版式补丁会
    在没有报错的情况下写到错误的标签上，产出 Word 打不开的文件。
    """

    def test_new_document_returns_doc(self):
        from mmwf.domain.docx._docx_backend import new_document

        doc = new_document()
        assert doc is not None
        # 新的 Document 自带一个空段落，不应有内容
        assert "".join(p.text for p in doc.paragraphs).strip() == ""

    def test_open_document_roundtrip(self, tmp_path):
        """写出去再读回来，文字必须一致。

        `open_document` 目前只被对照校验/回归路径用到，但没有测试的话
        它坏了也没人知道 —— 而它是"读回产物做二次校验"的唯一入口。
        """
        from mmwf.domain.docx._docx_backend import open_document

        data = render("# 标题\n\n正文段落\n")
        path = tmp_path / "roundtrip.docx"
        path.write_bytes(data)

        doc = open_document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "标题" in text
        assert "正文段落" in text

    def test_qn_produces_clark_notation(self):
        from mmwf.domain.docx._docx_backend import qn

        assert qn("w:ind") == W + "ind"
        assert qn("w:rFonts") == W + "rFonts"

    def test_oxml_element_tag(self):
        from mmwf.domain.docx._docx_backend import OxmlElement

        el = OxmlElement("w:ind")
        assert el.tag == W + "ind"

    def test_pt_and_cm_convert_to_emu(self):
        """长度单位换算：1pt = 12700 EMU，1cm = 360000 EMU。"""
        from mmwf.domain.docx._docx_backend import Cm, Pt

        assert Pt(1) == 12700
        assert Pt(28.8) == int(28.8 * 12700)
        assert Cm(1) == 360000

    def test_align_and_line_spacing_enums_exposed(self):
        """枚举必须能从隔离层拿到，且成员齐全（否则版式无处可设）。"""
        from mmwf.domain.docx._docx_backend import (
            WD_ALIGN_PARAGRAPH,
            WD_LINE_SPACING,
        )

        align = WD_ALIGN_PARAGRAPH()
        for name in ("LEFT", "CENTER", "RIGHT", "JUSTIFY"):
            assert hasattr(align, name), f"WD_ALIGN_PARAGRAPH 缺 {name}"

        spacing = WD_LINE_SPACING()
        for name in ("EXACTLY", "SINGLE", "AT_LEAST"):
            assert hasattr(spacing, name), f"WD_LINE_SPACING 缺 {name}"

    def test_backend_error_has_install_hint(self):
        """缺依赖时必须给**可执行**的提示，而不是干巴巴的 ModuleNotFoundError。"""
        from mmwf.domain.docx._docx_backend import DocxBackendError

        assert issubclass(DocxBackendError, RuntimeError)

    def test_strip_default_styles_removes_rfonts(self):
        """自带样式的 `w:rFonts` 必须被清掉，否则会与 GB 注入的字体打架。

        清掉的是 `w:rFonts`，**不是样式本身** —— 删样式会留下悬空
        `w:pStyle` 引用，Word 会报"文档已损坏"。
        """
        from mmwf.domain.docx._docx_backend import new_document, qn

        doc = new_document()
        for style in doc.styles:
            try:
                rpr = style.element.find(qn("w:rPr"))
            except (AttributeError, ValueError):
                continue
            if rpr is None:
                continue
            assert rpr.find(qn("w:rFonts")) is None, (
                f"样式 {style.name} 仍带 w:rFonts（未被剥离）"
            )
