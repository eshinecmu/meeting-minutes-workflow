"""docx 领域模块（T1-09，SAD §4 M11）。

职责（SAD §4 原文）：**版式映射规则、样式补丁、编号、字体预检。**
明确不做：完整格式转换（C14）、纪要生成（那是 M08/M09）。

对外只有本文件里的这些名字。包内部分工：

    _docx_backend.py    ★ 唯一允许 import python-docx 的地方
    style_mapper.py     GB/T 9704 版式表 + 中文标题编号 + 字体回退链
    patches.py          OOXML 补丁（中文字体、首行缩进、表格零缩进…）
    text_normalize.py   中英空格、引号转换、空格压缩（F5.6）
    font_check.py       字体预检与回退策略（E5002 / R7）
    writer.py           Markdown 结构 -> docx（内联移植，非黑盒）

用法（供 `app/services/export.py` 调用）::

    from mmwf.domain.docx import render_markdown_to_docx, RenderOptions

    result = render_markdown_to_docx(md, options=RenderOptions(font_policy="warn"))
    result.doc.save("out.docx")
    print(result.warnings)        # 含 font missing 之类的警告

注意 `render_markdown_to_docx` 返回的是**未保存的 Document 对象** ——
落盘由 service 层通过 filestore 负责。领域层不碰文件系统
（`check_layers` 的 C01 约束：领域层不 import sqlite3 等基础设施；
文件 IO 一律由上层做）。
"""

from mmwf.domain.docx.font_check import (
    POLICY_FAIL,
    POLICY_FALLBACK,
    POLICY_WARN,
    FontCheckResult,
    FontReport,
    build_check_result_json,
    check_fonts,
    is_installed,
    list_installed_fonts,
    policy_blocks_export,
    precheck_policy_error,
    required_fonts,
    resolve_font,
)
from mmwf.domain.docx.style_mapper import (
    BODY_FIRST_LINE_INDENT_CHARS,
    BODY_LINE_SPACING_PT,
    FONT_FALLBACK,
    GB_STYLES,
    HEADING_STYLE_KEYS,
    LATIN_FONT,
    GBStyle,
    NumberingState,
    format_heading_number,
    has_heading_number,
    strip_heading_number,
    to_chinese_numeral,
)
from mmwf.domain.docx.text_normalize import (
    add_cjk_latin_spaces,
    collapse_spaces,
    convert_quotes,
    drop_spaces_between_cjk,
    normalize_text,
    to_halfwidth_digits,
)
from mmwf.domain.docx.writer import (
    Block,
    FontMissingError,
    RenderOptions,
    RenderResult,
    parse_markdown,
    render_markdown_to_docx,
)

__all__ = [
    # 版式表与编号
    "BODY_FIRST_LINE_INDENT_CHARS",
    "BODY_LINE_SPACING_PT",
    "FONT_FALLBACK",
    "GB_STYLES",
    "HEADING_STYLE_KEYS",
    "LATIN_FONT",
    "GBStyle",
    "NumberingState",
    "format_heading_number",
    "has_heading_number",
    "strip_heading_number",
    "to_chinese_numeral",
    # 字体预检
    "POLICY_FAIL",
    "POLICY_FALLBACK",
    "POLICY_WARN",
    "FontCheckResult",
    "FontReport",
    "build_check_result_json",
    "check_fonts",
    "is_installed",
    "list_installed_fonts",
    "policy_blocks_export",
    "precheck_policy_error",
    "required_fonts",
    "resolve_font",
    # 文本规范化
    "add_cjk_latin_spaces",
    "collapse_spaces",
    "convert_quotes",
    "drop_spaces_between_cjk",
    "normalize_text",
    "to_halfwidth_digits",
    # 渲染
    "Block",
    "FontMissingError",
    "RenderOptions",
    "RenderResult",
    "parse_markdown",
    "render_markdown_to_docx",
]
