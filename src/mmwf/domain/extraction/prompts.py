"""抽取提示词模板（施工卡 T2-01 原文，**六维固定，不得自由发挥**）。

★ 为什么模板要"固定六维"
------------------------
F3.3（固定抽取维度）是**强制约束**，写进 SAD §14。自由发挥的提示词
每次跑出来的维度都不一样，下游（模板套用、语体对齐、校验）就无从适配。
六个维度（SAD §10.1 Step 2）：

    发言人 / 议题 / 结论性意见 / 议定事项(含责任人、时限) / 待办事项 / 需领导决策事项

★ 坑 8：**不要用 `str.format()` 注入正文**
-----------------------------------------
会议记录里出现花括号是常事（"预算{调整}方案"、"见附件{}"）。
`TEMPLATE.format(chunk_text=text)` 遇到正文里的 `{}` 会抛
`KeyError` / `IndexError`，而且是**偶发**的 —— 大部分会议没事，
某一次会议就炸，排查起来很费劲。

所以这里用 `.replace()` 做占位替换（见 `render_prompt`），
占位符用 `{{CHUNK_TEXT}}` 这种**双花括号**形式，与正文里可能出现的
单花括号区分开。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PLACEHOLDER",
    "RETRY_PREFIX",
    "SYSTEM_PROMPT",
    "EXTRACTION_TEMPLATE",
    "render_prompt",
    "render_retry_prompt",
]

#: 占位符。用双花括号，和正文里可能出现的单花括号区分开（见模块 docstring 坑 8）。
PLACEHOLDER = "{{CHUNK_TEXT}}"

#: 系统提示词。约束"只抽不编"是本项目红线项 5（无依据编造）的第一道防线。
SYSTEM_PROMPT = (
    "你是会议记录结构化抽取器。"
    "你只做信息抽取，不做总结、不做推断、不做美化。"
)

#: 抽取模板（施工卡 T2-01 原文，逐字保留五条严格要求）。
EXTRACTION_TEMPLATE = """你是会议记录结构化抽取器。请从下面这段会议转写文本中抽取信息。

【严格要求】
1. 只抽取文本中实际出现的信息，不得推断、不得补充、不得美化。
2. 每条结论必须附 source_quote，且 quote 必须逐字摘自原文。
3. 议定事项若无明确责任人，owner 字段必须填「暂无」，不得留空、不得省略该字段。
4. 若本段没有某类信息，对应字段返回空数组，不要编造。
5. 输出必须严格符合给定 JSON Schema。

【本段文本】
{{CHUNK_TEXT}}"""

#: 重试时追加的前缀（SAD §10.2：把校验报错**回注**给模型，而不是简单重试）。
RETRY_PREFIX = """上一次的输出未通过结构校验。请**只修正下面列出的问题**，
其余内容保持不变，重新输出完整的 JSON。

【校验报错】
{errors}
"""


def render_prompt(chunk_text: str) -> str:
    """把块正文填进模板。

    ★ 用 `replace` 而不是 `format` —— 见模块 docstring 坑 8。
    正文里的 `{}` 原样保留，不会触发格式化。
    """
    return EXTRACTION_TEMPLATE.replace(PLACEHOLDER, chunk_text or "")


def render_retry_prompt(chunk_text: str, issues: list[Any]) -> str:
    """带校验报错的重试提示词（SAD §10.2 的"回注"）。

    Args:
        chunk_text: 块正文。
        issues: `SchemaIssue` 或 `{"loc","message"}` 形态的问题列表。
    """
    lines: list[str] = []
    for it in issues or []:
        loc = getattr(it, "loc", None)
        msg = getattr(it, "message", None)
        if loc is None and isinstance(it, dict):
            loc, msg = it.get("loc"), it.get("message")
        lines.append(f"- {loc or '<root>'}: {msg or ''}")
    errors = "\n".join(lines) if lines else "- （未提供具体报错）"
    # 同样用 replace：`errors` 里可能含正文片段（带花括号）
    head = RETRY_PREFIX.replace("{errors}", errors)
    return f"{head}\n{render_prompt(chunk_text)}"
