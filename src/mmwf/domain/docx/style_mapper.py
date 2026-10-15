"""公文版式映射规则（T1-09，GB/T 9704—2012；SAD §4 M11）。

这张卡最容易被做偏的地方，是把它当成"Markdown 转 Word"。
它不是。它是**把一份 Markdown 映射到 GB/T 9704—2012 规定的版式上**，
标准里写死的每一个数字都是验收对象。

GB/T 9704—2012 里与本模块相关的硬性规定
---------------------------------------
    ┌─────────┬──────────────────┬────────┬────────────────────────┐
    │ 位置    │ 字体             │ 字号   │ 其他                   │
    ├─────────┼──────────────────┼────────┼────────────────────────┤
    │ 标题    │ 方正小标宋简体   │ 二号   │ 居中，不加粗           │
    │ 一级标题│ 黑体             │ 三号   │ 首行缩进 2 字符        │
    │ 二级标题│ 楷体_GB2312      │ 三号   │ 首行缩进 2 字符        │
    │ 三级标题│ 仿宋_GB2312 加粗 │ 三号   │ 首行缩进 2 字符        │
    │ 正文    │ 仿宋_GB2312      │ 三号   │ 首行缩进 2 字符        │
    │ 表格正文│ 仿宋_GB2312      │ 小四   │ **零缩进**             │
    │ 表头    │ 黑体             │ 小四   │ **零缩进**，加粗       │
    └─────────┴──────────────────┴────────┴────────────────────────┘

字号换算（本项目统一用磅，不用"号"）
------------------------------------
Word 里的"号"是一套中文印刷字号，换算关系到 pt：

    二号   = 22 pt        （标题）
    三号   = 16 pt        （正文、各级标题）
    小四   = 12 pt        （表格内）
    四号   = 14 pt        （页码）

施工卡的 `GB_STYLES` 骨架直接给了磅值，本模块照用 —— 但**同时保留
"二号/三号"的中文名**（见 `SIZE_NAMES`），因为验收时要跟标准原文对照，
人看着"三号"比对"16 pt"更不容易错。

行距为什么是 28.8 磅
--------------------
GB/T 9704—2012 规定正文一般每页 22 行。A4 纸去掉天头地脚后的版心高度
约 22 × 28.8 = 633.6 pt ≈ 22.35 cm，与标准规定的版心高度吻合。
所以 28.8 pt 是**从"每页 22 行"反推出来的**，不是随手取整；
它必须是**固定值**（`w:lineRule="exact"`），不能是"多倍行距"——
多倍行距随字号变，字号一改行数就不是 22 了。

字体为什么要有回退表
--------------------
方正小标宋简体、仿宋_GB2312、楷体_GB2312 都是**商业字体**，受版权保护，
本项目不得随包分发（部署手册 §2.1 第三段、§2.2）。模板只引用字体名。
目标机（党政机关办公机）通常已装齐，但开发机、CI、容器里几乎一定没有。
所以必须有回退链，且**回退时要留痕**（E5002，警告级，不中断导出）：
一份"仿宋变成了宋体"的公文，字形已不合规，使用者有权知道。

"回退" ≠ "静默替换"
-------------------
`FONT_FALLBACK` 的语义是：**按顺序找第一个装了的**。
如果连最后一个（宋体）都没有，那说明目标机连系统字体都不全，
属于环境异常 —— 此时仍导出（不能因为字体问题交付失败），
但警告里要写清楚"未能回退到任何替代字体"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

# ---------------------------------------------------------------------------
# 字号换算
# ---------------------------------------------------------------------------

#: 中文印刷字号 -> 磅。用于验收时与标准原文对照。
SIZE_NAMES: Mapping[float, str] = {
    26.0: "一号",
    24.0: "小一",
    22.0: "二号",
    18.0: "小二",
    16.0: "三号",
    15.0: "小三",
    14.0: "四号",
    12.0: "小四",
    10.5: "五号",
}

#: GB/T 9704—2012 正文固定行距（磅）。来源见模块文档。
BODY_LINE_SPACING_PT = 28.8

#: 正文首行缩进字符数（中文公文一律缩进 2 字）。
BODY_FIRST_LINE_INDENT_CHARS = 2


# ---------------------------------------------------------------------------
# 版式表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GBStyle:
    """一个版式位置的完整规格。

    用 dataclass 而不是施工卡骨架里的裸 dict，理由和 T1-08 的
    `METADATA_FIELDS` 一样：**这些字段是契约**。裸 dict 里把
    `first_line_indent_chars` 拼成 `firstline_indent_chars`，
    运行时只会静默地不缩进；dataclass 会在构造时报错。
    """

    key: str
    label: str
    font: str
    size_pt: float
    bold: bool = False
    align: str = "left"          # left | center | right | justify
    first_line_indent_chars: int = 0
    line_spacing_pt: float | None = None

    @property
    def size_name(self) -> str:
        """中文字号名（"三号"），取不到时回落到磅值字符串。"""
        return SIZE_NAMES.get(self.size_pt, f"{self.size_pt}pt")


#: GB/T 9704—2012 核心版式表（施工卡骨架逐项落地，并补上对齐与行距）。
GB_STYLES: Mapping[str, GBStyle] = {
    # 标题：二号方正小标宋简体，居中。**不加粗** —— 这是公文与
    # 一般文档最直观的区别，很多人会习惯性加粗，加上去就不合规了。
    "title": GBStyle(
        key="title", label="标题",
        font="方正小标宋简体", size_pt=22, bold=False, align="center",
    ),
    # 一级标题：三号黑体，首行缩进 2 字符（公文的标题也缩进，与西文排版相反）
    "h1": GBStyle(
        key="h1", label="一级标题",
        font="黑体", size_pt=16, bold=False, align="left",
        first_line_indent_chars=BODY_FIRST_LINE_INDENT_CHARS,
    ),
    # 二级标题：三号楷体_GB2312，首行缩进 2 字符
    "h2": GBStyle(
        key="h2", label="二级标题",
        font="楷体_GB2312", size_pt=16, bold=False, align="left",
        first_line_indent_chars=BODY_FIRST_LINE_INDENT_CHARS,
    ),
    # 三级标题：三号仿宋_GB2312 **加粗**（靠字重区分层级，不靠字体）
    "h3": GBStyle(
        key="h3", label="三级标题",
        font="仿宋_GB2312", size_pt=16, bold=True, align="left",
        first_line_indent_chars=BODY_FIRST_LINE_INDENT_CHARS,
    ),
    # 正文：三号仿宋_GB2312，固定行距 28.8 磅，首行缩进 2 字符
    "body": GBStyle(
        key="body", label="正文",
        font="仿宋_GB2312", size_pt=16, bold=False, align="justify",
        first_line_indent_chars=BODY_FIRST_LINE_INDENT_CHARS,
        line_spacing_pt=BODY_LINE_SPACING_PT,
    ),
    # ★ 表格正文：**零缩进**。C15 那个坑的正面规定就在这一行。
    "table_body": GBStyle(
        key="table_body", label="表格正文",
        font="仿宋_GB2312", size_pt=12, bold=False, align="left",
        first_line_indent_chars=0,
    ),
    # ★ 表头：黑体小四、加粗、零缩进
    "table_head": GBStyle(
        key="table_head", label="表头",
        font="黑体", size_pt=12, bold=True, align="left",
        first_line_indent_chars=0,
    ),
    # 页码：四号宋体，居外侧。公文的页码在版心外，奇偶页分左右。
    "page_number": GBStyle(
        key="page_number", label="页码",
        font="宋体", size_pt=14, bold=False, align="outside",
    ),
}

#: 字体回退链（施工卡骨架）。**顺序即优先级**。
FONT_FALLBACK: Mapping[str, tuple[str, ...]] = {
    "方正小标宋简体": ("方正小标宋_GBK", "宋体"),
    "仿宋_GB2312": ("仿宋", "宋体"),
    "楷体_GB2312": ("楷体", "宋体"),
}


# ---------------------------------------------------------------------------
# 中英文字体分设
# ---------------------------------------------------------------------------
#
# 公文正文里的西文与数字走 Times New Roman，这是 GB/T 9704 的惯例。
# 如果只设 `w:eastAsia`，西文会跟着中文字体走 —— 仿宋_GB2312 里的
# 拉丁字形又窄又挤，"2026""API"这种词会明显不协调。
#
# 注意这不是"回退"：中文字体和西文字体是**两套并列的设定**，
# 同时写进同一个 `w:rFonts`。回退是"这个字体没有时用那个"。
LATIN_FONT = "Times New Roman"


# ---------------------------------------------------------------------------
# 中文标题编号（U-DX-10）
# ---------------------------------------------------------------------------
#
# GB/T 9704—2012 的层级序号是**写死的三套符号**，不是无限嵌套的：
#
#     一、       （一级：汉字数字 + 顿号）
#     （一）     （二级：全角括号 + 汉字数字）
#     1.        （三级：阿拉伯数字 + 半角句点）
#
# 为什么必须自己编号、不能靠 Word 的自动编号
# ------------------------------------------
#   1. Word 的自动编号存在 `w:numPr` 里，依赖 numbering.xml 与样式表，
#      **解压 docx 读 document.xml 看不到任何编号**。本卡验收全部是
#      "解压读 XML 断言"，用自动编号就等于放弃了可校验性。
#   2. 自动编号会随"复制粘贴段落"漂移，而公文是**打印归档**的，
#      序号跳号/重号是低级但显眼的错误（U-DX-10 专测这个）。
#   3. 中文公文的层级就三层，写死比配置化更清楚。

#: 汉字数字（1..99 够用；公文标题不会超过这个层级）。
_CN_DIGITS = "零一二三四五六七八九"


def to_chinese_numeral(n: int) -> str:
    """阿拉伯数字 -> 汉字数字（公文序号的字形）。

    只支持 1..99：一级标题出现"一百"的公文不存在，
    真出现说明上游 Markdown 结构已经坏了，`raise` 比编造一个
    "一百" 更好排查。**不做"两"vs"二"的分支** ——
    公文序号一律用"二"（"二、"而不是"两、"）。
    """
    if n < 1 or n > 99:
        raise ValueError(f"中文序号只支持 1..99，收到 {n}")
    if n < 10:
        return _CN_DIGITS[n]
    tens, ones = divmod(n, 10)
    text = (_CN_DIGITS[tens] if tens > 1 else "") + "十"
    if ones:
        text += _CN_DIGITS[ones]
    return text


#: 各级标题的编号格式。`{n}` 是计数，`{cn}` 是汉字数字。
NUMBER_FORMATS: Mapping[int, str] = {
    1: "{cn}、",
    2: "（{cn}）",
    3: "{n}.",
}


def format_heading_number(level: int, counter: int) -> str:
    """生成第 `level` 级、序号为 `counter` 的标题前缀。

    >>> format_heading_number(1, 3)
    '三、'
    >>> format_heading_number(2, 2)
    '（二）'
    >>> format_heading_number(3, 11)
    '11.'
    """
    fmt = NUMBER_FORMATS.get(level)
    if fmt is None:
        raise ValueError(f"只支持 1..3 级标题编号，收到 level={level}")
    return fmt.format(n=counter, cn=to_chinese_numeral(counter))


#: 识别"这行标题已经带了编号"的正则（避免重复编号）。
#: 上游纪要正文常常**自带**"一、总体情况"这样的序号，再加一遍就成
#: "一、一、总体情况"。这个判断必须在编号前做。
_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十]+、|"          # 一、
    r"（[一二三四五六七八九十]+）|"        # （一）
    r"\(\s*[一二三四五六七八九十]+\s*\)|"  # (一)  半角括号（Markdown 里很常见）
    r"\d+\s*[.、]"                        # 1.  1、
    r")\s*"
)


def has_heading_number(text: str) -> bool:
    """标题是否已经自带序号。"""
    return bool(_PREFIX_RE.match(text))


def strip_heading_number(text: str) -> str:
    """剥掉标题自带的序号（编号由我们统一生成，保证不跳号重号）。"""
    return _PREFIX_RE.sub("", text, count=1).strip()


#: 标题层级 -> 版式 key 的映射。
#: Markdown 里 `#` 是 1 级、`##` 是 2 级…… 但公文的"标题"(`title`)
#: 与"一级标题"(`h1`) 是两回事：`title` 是文档大标题，`h1` 是正文里的
#: "一、"。映射规则见 `writer.py`：**第一个 `#` 当 title，其余降一级**。
HEADING_STYLE_KEYS: Mapping[int, str] = {1: "h1", 2: "h2", 3: "h3"}


@dataclass
class NumberingState:
    """标题编号的状态机。

    为什么需要状态：`<h1>` 之后 `h1` 计数 +1、`h2` 与 `h3` 计数**归零**。
    不归零就会出现"一、（一）、（二）、二、（三）"这种跳号 ——
    U-DX-10 要抓的就是它。这段逻辑很小，但它跨越多个段落，
    所以必须是个有状态的对象，不能是纯函数。
    """

    counters: list[int] = field(default_factory=lambda: [0, 0, 0])

    def next_prefix(self, level: int, *, decimal: bool = False) -> str:
        """推进到该级的下一号并返回前缀，同时清空更深层级。"""
        if level < 1 or level > 3:
            raise ValueError(f"标题层级只支持 1..3，收到 {level}")
        self.counters[level - 1] += 1
        for deeper in range(level, 3):
            self.counters[deeper] = 0
        if decimal:
            return ".".join(str(n) for n in self.counters[:level]) + " "
        return format_heading_number(level, self.counters[level - 1])

    def reset(self) -> None:
        self.counters = [0, 0, 0]


__all__ = [
    "BODY_FIRST_LINE_INDENT_CHARS",
    "BODY_LINE_SPACING_PT",
    "FONT_FALLBACK",
    "GB_STYLES",
    "HEADING_STYLE_KEYS",
    "LATIN_FONT",
    "NUMBER_FORMATS",
    "SIZE_NAMES",
    "GBStyle",
    "NumberingState",
    "format_heading_number",
    "has_heading_number",
    "strip_heading_number",
    "to_chinese_numeral",
]
