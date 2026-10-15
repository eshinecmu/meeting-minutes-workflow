"""纪要后置校验与版本差异（T1-16 落 `diff_versions`）。

本模块目前的职责
----------------
施工文档 §T2-03 定义了「差异对比视图（F4.3）」，要求：

    「按**段落粒度**，且**只显示变化段落**」

这里落的就是那个 `diff_versions()`。它是**纯函数**：
两份 Markdown 文本进来，一组差异段出去。不读盘、不碰库。

为什么是"段落"而不是"行"（本卡裁决 D2，与施工文档参考实现**有意不同**）
----------------------------------------------------------------------
施工文档给了一段参考实现，用的是 `splitlines()`（行级）。
但**同一节的目标**又写着"按段落粒度"。两者矛盾，本模块选**段落**：

    记录稿 v1:                    只改一个字后 v2:
    ┌────────────────────┐        ┌────────────────────┐
    │ 会议认为，三季度    │        │ 会议认为，四季度    │
    │ 预算方案总体可行。  │        │ 预算方案总体可行。  │
    └────────────────────┘        └────────────────────┘

* **行级**（`splitlines()`）：改一个字 → **两行**都成 `changed`
  （第一行变、第二行没变但被 SequenceMatcher 当 replace 一起配对）。
  界面上看到"两行标红，其中一行的内容其实一模一样"。
* **段落级**（按空行切）：改一个字 → **一段** changed。

用户要的是"我改的是哪一条"，不是"我的键盘动了几次"。
所以本模块按**空行**切段落。

⚠️ 但这里有个真实的取舍要记下来：Markdown 里"一段"= "一个空行分隔的块"。
记录稿是 `review` 节点生成的，段落之间有**空行**（T1-08 的格式约定），
所以段落切分是可靠的。**如果哪天记录稿改成"段间不空行"，
这个方法会退化成一个超大的 `unchanged` 段** —— 那时要改用
"按标题行（`#`/`##`）切"。这个前提条件写在这里，别当它永远成立。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "DIFF_TYPES",
    "MAX_UNCHANGED_LINES",
    "DiffBlock",
    "diff_versions",
]

#: 差异块的四种类型 —— **与施工文档 §T2-03 的 `diff_versions()` 逐字对齐**。
#: 前端将来直接按这四个值渲染，**不要自创第五种**。
DIFF_TYPES = ("unchanged", "changed", "removed", "added")

#: 连续未变段落超过这个**行数**就折叠（本卡裁决 D3）。
#: 40 的来源：一屏大约能放下 40 行，再多用户也不会细看，
#: 而一份 500 段的记录稿不折叠会让响应体 99% 是没变的内容。
MAX_UNCHANGED_LINES = 40

#: 折叠时**保留**的头尾行数（各保留这么多，中间用 `elided` 计数）。
_KEEP_HEAD = 20
_KEEP_TAIL = 20


@dataclass
class DiffBlock:
    """一个差异块。`type` 取 `DIFF_TYPES` 之一。"""

    type: str
    lines: list[str] = field(default_factory=list)
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    #: 折叠掉了多少行（仅 `unchanged` 会有）。
    elided: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.type == "unchanged":
            out["lines"] = list(self.lines)
            if self.elided:
                out["elided"] = self.elided
        elif self.type == "changed":
            out["old"] = list(self.old)
            out["new"] = list(self.new)
        elif self.type == "removed":
            out["old"] = list(self.old)
        elif self.type == "added":
            out["new"] = list(self.new)
        return out


def _split_paragraphs(text: str) -> list[str]:
    """按**空行**切段，段内保留原有换行。

    ⚠️ 三个细节都必须对，否则会出现"空段落"这种噪声：

    ① 统一换行符：`\\r\\n` → `\\n`。Windows 上写的文件读出来带 `\\r`，
       不统一会让 `"段落\r"` 与 `"段落"` **判为不同** —— 明明没改，
       diff 却报 changed。**这是最阴的一类假差异。**
    ② 去掉每段**尾部**空白行，但不 strip 段内的行 —— 段内缩进是内容。
    ③ 段首尾的空行会让切出空串，要过滤（空串进 diff 会变成"一个空段变了"）。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = normalized.split("\n\n")
    out: list[str] = []
    for chunk in raw:
        # 只去掉段两端的空白行，段内不动
        stripped = chunk.strip("\n")
        if stripped.strip():
            out.append(stripped)
    return out


def _fold_unchanged(lines: Sequence[str]) -> tuple[list[str], int]:
    """折叠超长的未变区间。返回 `(保留的行, 被省掉的行数)`。"""
    if len(lines) <= MAX_UNCHANGED_LINES:
        return list(lines), 0
    head = list(lines[:_KEEP_HEAD])
    tail = list(lines[-_KEEP_TAIL:])
    elided = len(lines) - len(head) - len(tail)
    return head + [f"……（此处省略 {elided} 段未变内容）……"] + tail, elided


def diff_versions(approved: str, edited: str) -> list[dict[str, Any]]:
    """对比两份记录稿，返回**段落级**差异。

    参数名沿用施工文档（`approved` / `edited`），但语义是
    "基准版" 与 "对比版" —— 不限于"已批准"与"已编辑"。

    返回值的 `type` 只取 `DIFF_TYPES` 四值。
    """
    a = _split_paragraphs(approved)
    b = _split_paragraphs(edited)

    matcher = difflib.SequenceMatcher(None, a, b)
    blocks: list[DiffBlock] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            kept, elided = _fold_unchanged(a[i1:i2])
            blocks.append(DiffBlock(type="unchanged", lines=kept, elided=elided))
        elif tag == "replace":
            blocks.append(
                DiffBlock(type="changed", old=list(a[i1:i2]), new=list(b[j1:j2]))
            )
        elif tag == "delete":
            blocks.append(DiffBlock(type="removed", old=list(a[i1:i2])))
        elif tag == "insert":
            blocks.append(DiffBlock(type="added", new=list(b[j1:j2])))
    return [blk.to_dict() for blk in blocks]


def summarize_blocks(blocks: Iterable[dict[str, Any]]) -> dict[str, int]:
    """数一数每类各有多少块。四种 type 的计数**四个键都在**（值为 0 也要有）。

    为什么 0 也要有：前端如果靠 `Object.keys` 判断"有哪些变化"，
    缺键会让它少渲染一类；而"某类为 0"和"没这类数据"在界面上不一样。
    """
    out = {t: 0 for t in DIFF_TYPES}
    for blk in blocks:
        t = blk.get("type")
        if t in out:
            out[t] += 1
    return out
