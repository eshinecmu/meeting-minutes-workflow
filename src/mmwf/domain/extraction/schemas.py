"""模式约束输出：`MinutesSchema` 全套（SAD §10.2，F3.4）。

为什么"约束解码"之外**还要**一层 pydantic 校验
------------------------------------------------
SAD §10.2 原文：

    生成后**必须**再过 `MinutesSchema.model_validate()` 校验层 ——
    因为约束解码保证模式合规，**不保证业务逻辑正确**。

这两件事是**正交**的：JSON Schema 能保证"`owner` 是个字符串"，
但保证不了"`owner` 不是空串"、"`source_quote` 真的在原文里"、
"`topics` 不是空数组"。所以校验层不是冗余，是补上约束解码管不到的那半边。

★ 裁决 D2：`source_start_ms` / `source_end_ms` 类型改为 `int | None`
-------------------------------------------------------------------
SAD §10.2 原文写的是 `source_start_ms: int`（**必填 int**）。本卡**有意偏离**，
理由：

* 记录稿里**可能没有时间码**（`segmentize` 的 `SegmentizeReport.no_timecode`
  就是在数这个）。源头没有时间信息时，要求模型必须给一个 int，
  等于**逼它编一个** —— 而"无依据编造"是本项目明列的红旗项。
* 编出来的 `start_ms` 更坏：它会让溯源**指到一个错误的位置**，
  比"没有时间戳"危险得多（用户点溯源跳过去，看到的是别的内容）。

所以这里类型放宽为 `int | None = None`，并在 JSON Schema 里允许 `null`。
**字段名与 SAD 一致**（`source_start_ms` / `source_end_ms`），只放宽可空性 ——
将来上游若明确要求必填，改这一处即可。

★ 裁决 D3：`ActionItemSchema` / `PendingDecisionSchema` 由本卡补充
-----------------------------------------------------------------
SAD §10.2 的 `TopicSchema` **引用了** `ActionItemSchema` 与
`PendingDecisionSchema`，但**全文没有给出这两个类的定义** —— 属于上游文档缺口。
本卡按 §10.1 Step 2 的六维度补齐，字段与 `ResolutionSchema` 对齐
（`content` / `owner` / `deadline` / `source_quote` / `source_start_ms` /
`source_end_ms`），`PendingDecisionSchema` 另加 `decision_maker`（需谁决策）。

**这两处是"本卡补充"，不是上游原文** —— 将来上游若给出定义，以原文为准。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

__all__ = [
    "SCHEMA_VERSION",
    "ActionItemSchema",
    "MinutesSchema",
    "PendingDecisionSchema",
    "ResolutionSchema",
    "SchemaIssue",
    "TopicSchema",
    "minutes_json_schema",
    "validate_minutes",
]

#: SAD §10.2 原文即为 `Literal["1.0"]`。**不要自增** —— 版本号是下游
#: 解析器的分支依据，改了要有迁移方案。
SCHEMA_VERSION = "1.0"


class _Grounded(BaseModel):
    """带原文依据的条目（议题下的三类事项共用）。

    抽出来做基类是为了**保证三类事项的溯源字段完全一致** ——
    否则很容易出现"议定事项有时间戳、待办没有"这种不对称，
    而下游（T2-04 的溯源完整性校验）要统一处理它们。
    """

    content: str = Field(..., min_length=1, description="事项内容")
    #: ★ 责任人。**确无责任人时必须填「暂无」，不得留空、不得省略该字段**
    #: （SAD §10.2 严格要求 3 / §10.4 规则 1 / F3.7）。
    #: 默认值给 `"暂无"` 而不是 `""`：模型漏了这个字段时，
    #: pydantic 会填默认值，从而**自动**满足"缺失率为 0"。
    owner: str = Field(default="暂无", description="责任人；无则填『暂无』")
    deadline: str | None = Field(default=None, description="时限，无则 null")
    #: ★ 必须**逐字摘自**原文（SAD §10.2 严格要求 2）。
    source_quote: str = Field(default="", description="原文片段，须逐字摘自转写稿")
    #: 见裁决 D2：可空，因为记录稿可能没有时间码。
    source_start_ms: int | None = None
    source_end_ms: int | None = None

    model_config = {"extra": "ignore"}


class ResolutionSchema(_Grounded):
    """议定事项（SAD §10.2 原文定义，字段名逐字一致）。"""


class ActionItemSchema(_Grounded):
    """待办事项（★ 本卡补充，见裁决 D3）。

    与议定事项的差别只在语义：议定是"已决定"，待办是"要去做"。
    字段刻意保持完全一致，方便下游统一做溯源校验。
    """


class PendingDecisionSchema(_Grounded):
    """需领导决策事项（★ 本卡补充，见裁决 D3）。

    多一个 `decision_maker`：这类事项的特征是**卡在某人那里等拍板**，
    所以"等谁决策"是必要信息；没有它，这条事项在纪要里等于没头没尾。
    """

    decision_maker: str = Field(default="暂无", description="需谁决策；不明则『暂无』")


class TopicSchema(BaseModel):
    """议题（SAD §10.2 原文定义）。"""

    name: str = Field(..., max_length=30)
    discussion: str = Field(default="")
    readings: list[str] = Field(default_factory=list)   # 公文语体标签（T2-02）
    resolutions: list[ResolutionSchema] = Field(default_factory=list)
    action_items: list[ActionItemSchema] = Field(default_factory=list)
    pending_decisions: list[PendingDecisionSchema] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class MinutesSchema(BaseModel):
    """纪要结构（SAD §10.2 原文定义）。"""

    title: str = Field(default="")
    meeting_time: str = Field(default="")
    location: str = Field(default="")
    host: str = Field(default="")
    attendees: list[str] = Field(default_factory=list)
    recorder: str = Field(default="")
    topics: list[TopicSchema] = Field(default_factory=list)
    schema_version: Literal["1.0"] = "1.0"

    model_config = {"extra": "ignore"}


class SchemaIssue(BaseModel):
    """一条模式校验问题（回注给模型重试用）。"""

    loc: str
    message: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"loc": self.loc, "message": self.message, "kind": self.kind}


def minutes_json_schema() -> dict[str, Any]:
    """导出 JSON Schema，供 Ollama 的 `format` 参数使用。

    ⚠️ 这里**不改** `$defs` / `$ref` 结构 —— Ollama（llama.cpp 侧）接受
    标准 JSON Schema。自作聪明地"拍平"`$ref` 会让两个定义相同但名字不同的
    类型被合并，反而破坏约束。
    """
    return MinutesSchema.model_json_schema()


def validate_minutes(payload: Any) -> tuple[MinutesSchema | None, list[SchemaIssue]]:
    """过一遍 `MinutesSchema.model_validate()`（SAD §10.2 的"校验层"）。

    Returns:
        `(实例, 问题列表)`。通过时问题为空；不通过时实例为 `None`
        且问题列表**非空**（用于回注给模型重试，最多 2 次）。

    ★ 为什么返回问题列表而不是直接抛：SAD §10.2 明写"校验不通过的处理：
    **不是简单重试**，而是把 pydantic 的报错信息**回注给模型**，最多重试 2 次；
    仍失败则落到 `validating` 状态并输出问题清单送人工（**不静默降级**）"。
    所以这里必须把结构化的问题交出去，调用方才有东西可回注。
    """
    try:
        return MinutesSchema.model_validate(payload), []
    except ValidationError as exc:
        issues: list[SchemaIssue] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            issues.append(SchemaIssue(
                loc=loc or "<root>",
                message=str(err.get("msg") or ""),
                kind=str(err.get("type") or ""),
            ))
        return None, issues
