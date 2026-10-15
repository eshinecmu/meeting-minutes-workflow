"""`domain/docx/text_normalize` 单元测试（T1-09，U-DX-06/07/08）。

    U-DX-06  中英空格   「会议讨论 API 接口」正确加空格
    U-DX-07  引号转换   英文 `"x"` -> 中文引号
    U-DX-08  多重空格压缩  连续空格压为一个

另外覆盖 `to_halfwidth_digits`（全角数字转半角）—— 它不在 U-DX 表里，
但 ASR 与上游 LLM 都会偶尔输出全角数字，属于同一个"排版层"的职责，
放在这里一起测比分一个文件更好查。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from lxml import etree

from mmwf.domain.docx import (
    add_cjk_latin_spaces,
    collapse_spaces,
    convert_quotes,
    drop_spaces_between_cjk,
    normalize_text,
    to_halfwidth_digits,
    RenderOptions,
    render_markdown_to_docx,
)


# ---------------------------------------------------------------------------
# U-DX-06 中英空格
# ---------------------------------------------------------------------------

class TestCjkLatinSpaces:
    """U-DX-06：中英文之间加空格。"""

    def test_acceptance_example(self):
        """施工卡原文给的验收例子，一字不差。"""
        assert (
            normalize_text("会议讨论了API 接口，使用JSON格式")
            == "会议讨论了 API 接口，使用 JSON 格式"
        )

    @pytest.mark.parametrize("src,expect", [
        ("中文English", "中文 English"),
        ("English中文", "English 中文"),
        ("讨论了API接口", "讨论了 API 接口"),
        ("第3条", "第 3 条"),
        ("共12人参加", "共 12 人参加"),
        ("用JSON格式", "用 JSON 格式"),
        ("GB/T 标准", "GB/T 标准"),          # 已有空格，不重复加
    ])
    def test_space_inserted_both_sides(self, src, expect):
        assert normalize_text(src) == expect

    def test_no_space_around_chinese_punctuation(self):
        """中文标点与西文之间**不加**空格（加了反而错）。"""
        assert normalize_text("（API）") == "（API）"
        assert normalize_text("。JSON") == "。JSON"
        assert normalize_text("API，接口") == "API，接口"

    def test_idempotent(self):
        """规范化两次结果相同（可重复调用）。"""
        one = normalize_text("会议讨论了API 接口")
        assert normalize_text(one) == one

    def test_does_not_touch_pure_ascii(self):
        assert normalize_text("hello world 123") == "hello world 123"

    def test_does_not_touch_pure_chinese(self):
        assert normalize_text("今天召开会议") == "今天召开会议"

    def test_direct_helper(self):
        assert add_cjk_latin_spaces("中A") == "中 A"
        assert add_cjk_latin_spaces("A中") == "A 中"
        assert add_cjk_latin_spaces("中A中") == "中 A 中"


# ---------------------------------------------------------------------------
# U-DX-07 引号转换
# ---------------------------------------------------------------------------

class TestQuoteConversion:
    """U-DX-07：英文引号 -> 中文引号。"""

    def test_simple_pair(self):
        assert convert_quotes('"test"') == "\u201ctest\u201d"

    def test_alternating_rule(self):
        """四个引号 -> 左右左右（交替规则，行为可预测）。

        ASR 吐的直引号不带语义，无法可靠判断"这是开还是合"，
        所以采用**交替**规则：第 1 个开、第 2 个合、第 3 个开……
        对"正确配对"的输入 100% 正确，且行为完全可预测。
        """
        assert convert_quotes('"甲""乙"') == "\u201c甲\u201d\u201c乙\u201d"

    def test_apostrophe_preserved(self):
        """★ `it's` 里的撇号不能被转成引号（否则是明确的错误）。"""
        assert convert_quotes("meeting's agenda") == "meeting's agenda"
        assert convert_quotes("don't") == "don't"

    def test_lone_quote_opens(self):
        """落单的引号按"开"处理（宁可留个未闭合的左引号）。"""
        assert convert_quotes('他说"好') == "他说\u201c好"

    def test_existing_curly_quotes_untouched(self):
        src = "\u201c已经是中文引号\u201d"
        assert convert_quotes(src) == src

    def test_corner_brackets_untouched(self):
        """直角引号「」原样保留（不强行转成弯引号）。"""
        assert convert_quotes("「直角引号」") == "「直角引号」"

    def test_empty(self):
        assert convert_quotes("") == ""

    def test_multiple_pairs(self):
        assert convert_quotes('"甲"和"乙"') == "\u201c甲\u201d和\u201c乙\u201d"

    def test_single_quote_pair_in_chinese(self):
        """中文语境里的单引号引号（前后不是字母）应转。"""
        assert convert_quotes("他'说'") == "他\u2018说\u2019"

    def test_idempotent(self):
        once = convert_quotes('"test"')
        assert convert_quotes(once) == once

    def test_through_normalize(self):
        assert normalize_text('他说"这个方案"很好') == "他说\u201c这个方案\u201d很好"

    @pytest.mark.parametrize("source,expected", [
        ('他说"方案"与\'预算\'。', "他说“方案”与‘预算’。"),
        ("meeting's agenda and don't", "meeting’s agenda and don’t"),
        ('他说"尚未结束', "他说“尚未结束"),
        ("“已有引号”及「原文」", "“已有引号”及「原文」"),
    ])
    def test_export_has_no_straight_quotes_in_any_text_block(self, source, expected):
        # P19 裁决：按 §03 导出全文无半角引号；所有格用 ’，不当作开引号。
        # convert_quotes 的既有纯文本所有格契约保留；此处检查最终 DOCX。
        markdown = (f"# {source}\n\n## {source}\n\n{source}\n\n"
                    f"- {source}\n\n表 1 {source}\n\n"
                    f"| {source} | 备注 |\n| --- | --- |\n| {source} | 内容 |")
        result = render_markdown_to_docx(markdown, options=RenderOptions(
            installed_fonts={"方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312",
                             "宋体", "Times New Roman"},
        ))
        buf = io.BytesIO()
        result.doc.save(buf)
        with zipfile.ZipFile(buf) as archive:
            root = etree.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        texts = root.xpath("//w:t/text()", namespaces=ns)
        assert sum(expected in text for text in texts) == 7
        assert all('"' not in text and "'" not in text for text in texts)


# ---------------------------------------------------------------------------
# U-DX-08 空格压缩与中文间空格
# ---------------------------------------------------------------------------

class TestSpaceHandling:
    """U-DX-08：多重空格压缩；中文之间不留空格。"""

    def test_multiple_spaces_collapsed(self):
        assert collapse_spaces("你好    世界") == "你好 世界"
        assert collapse_spaces("a\t\tb") == "a b"

    def test_single_space_kept(self):
        assert collapse_spaces("a b") == "a b"

    def test_newline_preserved(self):
        """压缩不能吃掉换行（换行是段落结构）。"""
        assert collapse_spaces("第一段\n\n第二段") == "第一段\n\n第二段"

    def test_spaces_between_cjk_removed(self):
        assert drop_spaces_between_cjk("会 议") == "会议"
        assert drop_spaces_between_cjk("召 开 会 议") == "召开会议"

    def test_cjk_latin_space_preserved(self):
        """中文与西文之间的空格要保留（那是刚加上的）。"""
        assert drop_spaces_between_cjk("讨论 API") == "讨论 API"

    def test_across_lines_not_merged(self):
        """中文换行后的空格不该把两行粘起来。"""
        assert drop_spaces_between_cjk("会\n 议") == "会\n 议"

    def test_ordering_matters(self):
        """★ 顺序：先加中英空格，再压空格 —— 反过来会留下双空格。"""
        src = "讨论了  API  接口"
        assert normalize_text(src) == "讨论了 API 接口"

    def test_realistic_asr_output(self):
        """一段真实的 ASR 风格噪音输入。"""
        src = "今天  我们  讨论了API接口 ，  使用JSON格式"
        out = normalize_text(src)
        assert "API 接口" in out
        assert "JSON 格式" in out
        assert "  " not in out


# ---------------------------------------------------------------------------
# 全角数字（附加）
# ---------------------------------------------------------------------------

class TestHalfwidth:
    def test_fullwidth_digits(self):
        assert to_halfwidth_digits("２０２６") == "2026"

    def test_fullwidth_letters(self):
        assert to_halfwidth_digits("ＡＰＩ") == "API"

    def test_mixed(self):
        assert to_halfwidth_digits("第１２条 ＡＰＩ") == "第12条 API"

    def test_chinese_untouched(self):
        assert to_halfwidth_digits("中文、 ，（）") == "中文、 ，（）"

    def test_fullwidth_punct_untouched(self):
        """全角标点（，。（））**不**转 —— 那是中文正常标点。"""
        assert to_halfwidth_digits("（甲），。") == "（甲），。"


# ---------------------------------------------------------------------------
# 组合与边界
# ---------------------------------------------------------------------------

class TestNormalizeIntegration:
    def test_empty_string(self):
        assert normalize_text("") == ""

    def test_none_like_blank(self):
        assert normalize_text("   ") == " "

    def test_multiline_document(self):
        src = "# 关于API接口的会议纪要\n\n会议讨论了JSON格式"
        out = normalize_text(src)
        assert "# 关于 API 接口的会议纪要" in out
        assert "JSON 格式" in out
        assert out.count("\n") == src.count("\n"), "换行数不变"

    def test_long_sentence_stability(self):
        src = ("会议认为，本次API接口改造涉及12个单位，"
               "要在2026年底前完成JSON格式的统一")
        out = normalize_text(src)
        assert out == ("会议认为，本次 API 接口改造涉及 12 个单位，"
                       "要在 2026 年底前完成 JSON 格式的统一")

    def test_does_not_change_word_content(self):
        """规范化只动排版，不增删任何汉字/字母/数字。"""
        src = "会议讨论了API 接口"
        out = normalize_text(src)
        strip = lambda s: "".join(ch for ch in s if not ch.isspace())
        assert strip(out) == strip(src)
