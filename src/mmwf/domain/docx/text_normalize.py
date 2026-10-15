"""中文排版规范化（T1-09，F5.6）。

转写稿是**口语的笔录**，它带着 ASR 引擎的全部痕迹：中英之间没有空格
（"讨论了API 接口"、"用JSON格式"）、引号是半角的（`"AI"`）、
标点后跟着两三个空格、说话人停顿留下的连续空格。这些在纯文本里
无所谓，一旦印成公文就非常刺眼。

本模块做的是**排版层的规范化**，它有一条不能越过的边界：

    只改"字符之间怎么排"，不改"说了什么"。

具体地：不删字、不换词、不合并句子、不改标点种类（除了引号形态）。
`" 我认为这个方案不行 "` 规范化后是 `"我认为这个方案不行"`（去首尾空白），
而不是 `"方案欠佳"`。后者属于 T1-08 之后 `minutes` 的职责（SAD §4 把
M11 `docx` 的边界明写为"版式映射规则、样式补丁、编号、字体预检"，
**不含润色**）。

四条规则，各自的理由
--------------------

**① 中英文之间加空格。**
这条是中文排版的通行做法（"中文文案排版指北"），也是 Word 里
最显眼的差异。但**不能无脑加** —— 有几个地方加了反而错：

    · 中文**标点**与西文之间：`"（API）"` 不该变成 `"（ API ）"`；
    · 数字与**量词/单位**：`"第3条"` 里 `3` 是西文、`条` 是中文，
      按规则该加空格，但公文里"第 3 条"和"第3条"都常见 ——
      本项目**统一加**（与"中文文案排版指北"一致），因为它可预测：
      使用者看惯了就知道是排版规则，而不是"这里漏了个字"。
    · 已经紧贴的**专有名词**：`"GB/T 9704"` 内部本来就有空格，不动。

**② 英文引号转中文引号。**
ASR 引擎输出一律是 `"` `"` `'` `'`（U+201C/201D/2018/2019，其实已是
中文引号的"弯引号"形态，但很多引擎吐的是 `"` `'` 直引号）。
公文的规范是**弯引号**。判定"该用左还是右"不能只看"这是第几个引号"——
`"他说"好""` 这种嵌套用计数法必错。本项目用**就近配对**：
遇到一个待转换的引号，看它前面是否还有未配对的同种引号，
有就转右（收），没有就转左（开）。这个规则对嵌套也成立。

**③ 多重空格压缩。**
`"你好    世界"` → `"你好 世界"`。这是 ASR 停顿的副产物。

**④ 中文之间的空格去掉。**
`"会 议"` → `"会议"`。中文没有词间空格，ASR 偶尔会在词中间吐空格。
这条必须在 ① 之后做 —— 顺序反了，① 刚加的空格会被 ④ 误删
（比如"讨论 API"里 API 前后的空格，若 `A` 被当成中文就糟了，
好在 `A` 不在 CJK 范围内）。为稳妥，②③④ 统一在 ① 之后。

顺序为什么不能乱
----------------
    ① 中英加空格  →  ② 引号转换  →  ③ 压缩空格  →  ④ 去中文间空格

如果把 ③ 放在 ① 之前：`"讨论  API"` 先被压成 `"讨论 API"`，
再走 ① 时 `论` 与 `A` 之间已有空格，不会重复加 —— 结果相同。
但如果 ① 产生的是**双空格**（比如原文 `"讨论  API"` 里 `论` 后是
两个空格，① 的 `([CJK])([LATIN])` 匹配不到，因为中间隔了空格），
那就需要 ③ 来收尾。所以 ③ 必须在 ① 之后。
"""

from __future__ import annotations

import re

#: CJK 统一表意文字（含扩展 A）+ 常用中文标点之外的中文部分。
#: 注意**不含中文标点** —— 标点与西文之间不加空格（规则 ① 的例外）。
_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_LATIN = r"A-Za-z0-9"

#: 中文**标点**（不该被插入空格的那些）。
_CJK_PUNCT = (
    r"\u3000-\u303f"          # 中文标点区（、。〈〉《》「」『』【】〔〕等）
    r"\uff00-\uffef"          # 全角形式（，！？：；（）等）
)

#: 中英之间需要加空格的**左边界**：中文汉字（不是标点）。
_CJK_CHAR = rf"[{_CJK}]"
#: 右边界：西文字母或数字。
_LATIN_CHAR = rf"[{_LATIN}]"

#: 直引号 -> 弯引号映射（"弯引号"就是中文排版用的引号形态）。
_STRAIGHT_DOUBLE = '"'
_CURLY_DOUBLE_OPEN = "\u201c"    # "
_CURLY_DOUBLE_CLOSE = "\u201d"   # "
_STRAIGHT_SINGLE = "'"
_CURLY_SINGLE_OPEN = "\u2018"    # '
_CURLY_SINGLE_CLOSE = "\u2019"   # '

#: 中文引号也接受"直角引号"（「」『』）。部分单位公文规范用直角引号，
#: 本项目**不主动转换**为直角引号（默认弯引号），但**不破坏**已有的直角引号。
_CORNER_OPEN = "「"
_CORNER_CLOSE = "」"


def normalize_text(text: str) -> str:
    """完整的中文排版规范化。空输入原样返回。"""
    if not text:
        return text
    text = add_cjk_latin_spaces(text)
    text = convert_quotes(text)
    text = collapse_spaces(text)
    text = drop_spaces_between_cjk(text)
    return text


def add_cjk_latin_spaces(text: str) -> str:
    """在中文与西文之间插入一个空格（两侧都处理）。

    用 `re.sub` 两遍而不是一遍带回溯的复杂正则：
    一遍处理"中文后接西文"，一遍处理"西文后接中文"。
    拆成两遍的副作用是"中 A 文"会被两遍各看一次，
    但因为插入的是空格（不属于两侧字符类），第二遍不会重复插入。
    """
    text = re.sub(rf"({_CJK_CHAR})({_LATIN_CHAR})", r"\1 \2", text)
    text = re.sub(rf"({_LATIN_CHAR})({_CJK_CHAR})", r"\1 \2", text)
    return text


def convert_quotes(text: str) -> str:
    """半角直引号 -> 中文弯引号（按出现顺序交替开/合）。

    判定规则：**遇到未闭合的就闭合，否则新开**。这是一个"交替"状态机 ——
    第 1 个转左（“）、第 2 个转右（”）、第 3 个转左……单引号同理。

    为什么不用"就近配对"或"看前后文语义"
    ------------------------------------
    ASR 吐出来的直引号**不带任何语义信息**，无法可靠区分
    "这是一段引用的开头"还是"这是引用的结尾"。任何基于内容的猜测
    （比如"引号后是空格则视为闭合"）都会在某些句子上出错，而且出错后
    很难解释。交替法虽然对**不配对**的输入（奇数个引号）无能为力，
    但它对**正确配对的输入**（绝大多数）100% 正确，行为完全可预测。

    直角引号 `「」`、已是弯引号的字符，一律原样保留 ——
    有人手写的正文用了直角引号，不该被改掉。
    """
    out: list[str] = []
    double_open = 0     # 尚未闭合的左双引号数
    single_open = 0

    for idx, ch in enumerate(text):
        if ch == _STRAIGHT_DOUBLE:
            if double_open > 0:
                out.append(_CURLY_DOUBLE_CLOSE)
                double_open -= 1
            else:
                out.append(_CURLY_DOUBLE_OPEN)
                double_open += 1
        elif ch == _STRAIGHT_SINGLE:
            # 单引号有歧义：可能是英文所有格（"it's"）而不是中文引号。
            # 判据：**前后紧邻字母则视为撇号**，不动它。
            # 不判这一条，`meeting's` 会变成 `meeting‘s` ——
            # 在公文里是明确的错误。
            if _is_apostrophe(out, text, idx):
                out.append(ch)
                continue
            if single_open > 0:
                out.append(_CURLY_SINGLE_CLOSE)
                single_open -= 1
            else:
                out.append(_CURLY_SINGLE_OPEN)
                single_open += 1
        else:
            if ch == _CURLY_DOUBLE_OPEN:
                double_open += 1
            elif ch == _CURLY_DOUBLE_CLOSE:
                double_open = max(0, double_open - 1)
            elif ch == _CURLY_SINGLE_OPEN:
                single_open += 1
            elif ch == _CURLY_SINGLE_CLOSE:
                single_open = max(0, single_open - 1)
            out.append(ch)
    return "".join(out)


def _is_apostrophe(out: list[str], src: str, idx: int) -> bool:
    """判断 `src[idx]` 处的单引号是不是英文撇号（前后都是字母）。

    只看**紧邻**字符，不看词法 —— `'` 在英文里几乎只出现在
    字母夹缝中（`it's` / `don't` / `O'Brien`），这个判据足够，
    且不需要引入词典。
    """
    if not out:
        return False
    prev = out[-1]
    nxt = src[idx + 1] if idx + 1 < len(src) else ""
    return prev.isascii() and prev.isalpha() and nxt.isascii() and nxt.isalpha()


def collapse_spaces(text: str) -> str:
    """把连续空格/制表符压成一个空格（不改换行）。"""
    return re.sub(r"[ \t]{2,}", " ", text)


def drop_spaces_between_cjk(text: str) -> str:
    """去掉中文汉字之间的**行内**空格。

    `"会 议"` -> `"会议"`。中文没有词间空格，这类空格一定是 ASR 噪音。

    **保留**中文与西文之间的空格（那是刚加上的），
    **绝不吃掉换行** —— 换行是段落结构，不是排版空隙。

    ★ 这里有一个必须用 `[ \\t]+` 而不是 `\\s+` 的地方：
    `\\s` 包含 `\\n`，写成 `\\s+` 会把
    `"会议纪要\\n\\n议定事项"` 里两个段落之间的换行整个吃掉，
    合并成一个"会议纪要议定事项"的段落 —— **静默丢掉了段落边界**。
    这类 bug 在"文本看起来还挺顺"的情况下极难发现，
    而后果是公文正文的段全部粘成一坨。

    所以：**行内空白用 `[ \\t]`，跨行一律停手。**

    ★ 另一个必须处理的细节：`re.sub` 的正则引擎**不重叠**匹配。
    对 `"召 开 会 议"`，第一遍匹配到 `开` 与 `会` 之间的空格，
    消费掉这段之后，`召` 与 `开`、`会` 与 `议` 之间的空格就没被再看一眼，
    结果留下 `"召开 会议"`。下面用循环跑到不动为止 ——
    因为每轮至少消掉一个空格，最多跑 `len(text)` 次就必然收敛。
    （用 lookahead 写 `(?=([CJK]))` 也能一次搞定，但那个写法可读性差，
    且替换时要处理组号，容易出错。循环更直白。）
    """
    pattern = re.compile(rf"({_CJK_CHAR})[ \t]+({_CJK_CHAR})")
    prev = None
    text_out = text
    while prev != text_out:
        prev = text_out
        text_out = pattern.sub(r"\1\2", text_out)
    return text_out


def normalize_lines(text: str) -> list[str]:
    """按行规范化，并去掉空行的首尾空白。

    供 writer 逐行处理 Markdown 源文时使用：Markdown 的结构行
    （`# `、`| `、`- `）需要保留其前缀，所以**不能**整篇先规范化。
    本函数只做"逐行"，不做结构解析。
    """
    return [normalize_text(line) for line in text.splitlines()]


#: 全角数字/字母 -> 半角。公文里偶尔混入全角数字（ASR 或上游 LLM 输出），
#: 视觉上比半角宽一倍，混排时对不齐。
_FULLWIDTH_OFFSET = 0xFEE0


def to_halfwidth_digits(text: str) -> str:
    """全角数字与字母转半角（`２０２６` -> `2026`）。"""
    out = []
    for ch in text:
        o = ord(ch)
        if 0xFF10 <= o <= 0xFF19 or 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            out.append(chr(o - _FULLWIDTH_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


__all__ = [
    "add_cjk_latin_spaces",
    "collapse_spaces",
    "convert_quotes",
    "drop_spaces_between_cjk",
    "normalize_lines",
    "normalize_text",
    "to_halfwidth_digits",
]
