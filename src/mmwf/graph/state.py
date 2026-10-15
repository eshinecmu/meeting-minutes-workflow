"""流水线状态定义与节点标识（T1-10，SAD §5.1/§5.2）。

这个模块是**整个编排层的词汇表**：状态名、节点名、界面文案、闸口语义
全在这里定义一次，`build.py` / `nodes/` / `runner.py` / API 层都从这里取。
不这样做的话，"G2_awaiting" 这个字符串会在五六个文件里各写一遍，
改一处忘一处，最后界面上显示一个内部状态名给用户看（N3.3 明令禁止）。

三层命名，别混
--------------
本模块同时维护**三套**命名，它们的职责完全不同（这是最容易搞错的地方）：

    ================== ============================== ==================
    用途                示例                            谁在用
    ================== ============================== ==================
    节点名（node）       "extract" / "gate_g2"            LangGraph 图结构
    状态名（status）     "extracting" / "G2_awaiting"    meeting.status 落库
    界面文案（label）    "正在梳理议题" / "待确认议题"     前端展示
    ================== ============================== ==================

节点名是**动作**（动词），状态名是**进行时**（-ing）。一个节点执行时
把状态置为对应的进行时状态，执行完置为产物就绪状态。闸口节点是个例外：
它的状态名 `G2_awaiting` 没有 -ing，因为它表示"停在这里等人"，不是"正在做"。

为什么"界面文案"要定义在领域无关的编排层
--------------------------------------
N3.3 要求界面不出现"检查点""schema""幂等""HITL"等工程词。
把文案与状态名放在**同一张表**里，是为了让"新增一个状态但忘了写文案"
在开发时就暴露（表里少一列），而不是等用户看到 `G3_awaiting` 才发现。
`GRAPH_STATUS_LABEL` 与 `GRAPH_STATUS` 的键必须完全一致，有测试兜底。
"""

from __future__ import annotations

from typing import Literal, TypedDict

# ---------------------------------------------------------------------------
# 一、状态名（SAD §5.2 状态定义表，逐行对应）
# ---------------------------------------------------------------------------

#: 已接收（用户投递音频 / 导入文字稿）
STATUS_CREATED = "created"
#: 正在检查音频（格式校验、时长探测、切分计划）
STATUS_INGEST = "ingest_pending"
#: 正在转写
STATUS_TRANSCRIBING = "transcribing"
#: 转写完成
STATUS_TRANSCRIPT_READY = "transcript_ready"
#: 待复核（低置信分流）
STATUS_REVIEWING = "reviewing"
#: 记录稿已生成
STATUS_RECORD_READY = "record_ready"
#: 正在梳理议题
STATUS_EXTRACTING = "extracting"
#: ★ 待确认议题（G2 闸口）
STATUS_G2_AWAITING = "G2_awaiting"
#: 正在起草纪要
STATUS_GENERATING = "generating"
#: 正在自检
STATUS_VALIDATING = "validating"
#: 正在成文
STATUS_ASSEMBLING = "assembling"
#: ★ 待审定纪要（G3 闸口）
STATUS_G3_AWAITING = "G3_awaiting"
#: 正在导出
STATUS_EXPORTING = "exporting"
#: 已完成
STATUS_ARCHIVED = "archived"
#: 处理失败
STATUS_FAILED = "failed"
#: 已取消
STATUS_CANCELLED = "cancelled"

#: 全部合法状态（SAD §5.2 的 16 个）。
GRAPH_STATUS: tuple[str, ...] = (
    STATUS_CREATED,
    STATUS_INGEST,
    STATUS_TRANSCRIBING,
    STATUS_TRANSCRIPT_READY,
    STATUS_REVIEWING,
    STATUS_RECORD_READY,
    STATUS_EXTRACTING,
    STATUS_G2_AWAITING,
    STATUS_GENERATING,
    STATUS_VALIDATING,
    STATUS_ASSEMBLING,
    STATUS_G3_AWAITING,
    STATUS_EXPORTING,
    STATUS_ARCHIVED,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

#: 终态（SAD §5.2「是否终态」列）。到终态后不再有 next_node。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_ARCHIVED, STATUS_FAILED, STATUS_CANCELLED}
)

#: 闸口状态。停在闸口时任务在等人决策，`awaiting_actor` 有值。
GATE_STATUSES: frozenset[str] = frozenset(
    {STATUS_G2_AWAITING, STATUS_G3_AWAITING}
)

#: 界面文案（N3.3：界面不得出现工程词，中间产物统一叫「文稿」）。
GRAPH_STATUS_LABEL: dict[str, str] = {
    STATUS_CREATED: "已接收",
    STATUS_INGEST: "正在检查音频",
    STATUS_TRANSCRIBING: "正在转写",
    STATUS_TRANSCRIPT_READY: "文稿待核对",
    STATUS_REVIEWING: "文稿待核对",
    STATUS_RECORD_READY: "记录稿已生成",
    STATUS_EXTRACTING: "正在梳理议题",
    STATUS_G2_AWAITING: "待确认议题",
    STATUS_GENERATING: "正在起草纪要",
    STATUS_VALIDATING: "正在自检",
    STATUS_ASSEMBLING: "正在成文",
    STATUS_G3_AWAITING: "待审定纪要",
    STATUS_EXPORTING: "正在导出",
    STATUS_ARCHIVED: "已完成",
    STATUS_FAILED: "处理失败",
    STATUS_CANCELLED: "已取消",
}


def status_label(status: str) -> str:
    """状态 -> 界面文案。未知状态**原样返回**而不是报错。

    未知状态说明代码有 bug，但那是日志该记的事 —— 在界面上把用户的
    任务显示成"未知状态"比显示成内部字符串更糟（后者至少能被搜索到）。
    """
    return GRAPH_STATUS_LABEL.get(status, status)


# ---------------------------------------------------------------------------
# 二、节点名（SAD §5.1 状态图的 11 个节点）
# ---------------------------------------------------------------------------

NODE_INGEST = "ingest"
NODE_TRANSCRIBE = "transcribe"
NODE_REVIEW = "review"
NODE_EXTRACT = "extract"
NODE_GATE_G2 = "gate_g2"
NODE_GENERATE = "generate"
NODE_VALIDATE = "validate"
NODE_ASSEMBLE = "assemble"
NODE_GATE_G3 = "gate_g3"
NODE_EXPORT = "export"
NODE_ARCHIVE = "archive"

#: 图的主链路顺序（与 SAD §5.1 一致）。用于断言"节点顺序没被改乱"
#: 以及 `rerun_nodes` 的差集计算。
NODE_SEQUENCE: tuple[str, ...] = (
    NODE_INGEST,
    NODE_TRANSCRIBE,
    NODE_REVIEW,
    NODE_EXTRACT,
    NODE_GATE_G2,
    NODE_GENERATE,
    NODE_VALIDATE,
    NODE_ASSEMBLE,
    NODE_GATE_G3,
    NODE_EXPORT,
    NODE_ARCHIVE,
)

#: 节点 -> 该节点开始执行时任务处于的状态（用于落库与界面显示）。
NODE_STATUS: dict[str, str] = {
    NODE_INGEST: STATUS_INGEST,
    NODE_TRANSCRIBE: STATUS_TRANSCRIBING,
    NODE_REVIEW: STATUS_REVIEWING,
    NODE_EXTRACT: STATUS_EXTRACTING,
    NODE_GATE_G2: STATUS_G2_AWAITING,
    NODE_GENERATE: STATUS_GENERATING,
    NODE_VALIDATE: STATUS_VALIDATING,
    NODE_ASSEMBLE: STATUS_ASSEMBLING,
    NODE_GATE_G3: STATUS_G3_AWAITING,
    NODE_EXPORT: STATUS_EXPORTING,
    NODE_ARCHIVE: STATUS_ARCHIVED,
}

#: 闸口节点 -> 闸口标识。`gate_g2` 的闸口是 "G2"。
GATE_NODES: dict[str, str] = {
    NODE_GATE_G2: "G2",
    NODE_GATE_G3: "G3",
}

#: 闸口标识 -> 界面上的送审名称（N3.3 文案）。
GATE_LABEL: dict[str, str] = {
    "G2": "待确认议题",
    "G3": "待审定纪要",
}

#: 闸口 -> 风险等级（SAD §5.3 `InterruptPoint.risk`）。
#: G2 是最高优先级闸口（议题错了后面全错），G3 是成稿审定。
GATE_RISK: dict[str, str] = {
    "G2": "high",
    "G3": "medium",
}


# ---------------------------------------------------------------------------
# 三、流水线状态（LangGraph 的 State）
# ---------------------------------------------------------------------------

class PipelineState(TypedDict, total=False):
    """流水线状态（施工卡给定，字段与 SAD §5.1 各节点产物对应）。

    `total=False` 是**必须**的：LangGraph 的节点返回的是**状态增量**
    （partial update），只有被返回的键才会被合并。写成 `total=True`
    会要求每个节点返回全部字段，实际上不可能（`ingest` 节点执行时
    还没有 `transcript`）。

    **注意**：不要把"节点执行次数"这类**观测数据**塞进状态。
    状态会被检查点持久化并在恢复时重放，把计数器放进去会让
    "恢复之后计数继续累加"，`rerun_nodes == []` 这类断言就永远算不准。
    执行次数记在 `GraphRunner` 的内存/审计里（见 runner.py）。
    """

    # --- 身份 ---
    task_id: str
    meeting_id: str
    stage: str

    # --- ingest ---
    audio_paths: list[str]
    chunk_plan: list[dict]

    # --- transcribe ---
    transcript: list[dict]
    transcript_meta: dict

    # --- review ---
    review_result: dict
    record_markdown: str

    # --- extract（G2 的送审内容就是这三项）---
    topics: list[dict]
    resolutions: list[dict]
    action_items: list[dict]

    # 丰富原料与全阶段问题；可选键，无 append reducer。
    minutes: dict
    issues: list[dict]

    # --- 闸口 ---
    pending_decisions: list[dict]

    # --- generate ---
    minutes_structured: dict

    # --- assemble ---
    minutes_markdown: str

    # --- validate ---
    validation_issues: list[dict]
    gen_retry: int

    # --- export / archive ---
    export_paths: dict
    idem_keys: dict

    # --- 通用 ---
    errors: list[dict]
    operator: str

    # --- 编排内部字段（下划线前缀；业务代码不该读写）------------------
    #
    # ⚠️ 这两个字段**必须在 TypedDict 里显式声明**，否则会被 LangGraph
    #    静默丢弃 —— 实测确认：`StateGraph(dict)` 或者只声明了业务字段的
    #    TypedDict，都会让节点返回的 `{"_gate_route": "redo"}` 在
    #    `get_state().values` 里变成 `None`。
    #
    #    丢掉的后果不是报错，而是**静默走错分支**：条件边读不到
    #    `_gate_route` 就按 `continue` 处理，于是"退回重做"变成了
    #    "继续往下走" —— 错误决策被静默接受，恰好是闸口设计要避免的反面。
    #
    #    名字用下划线开头是刻意的：它不参与业务语义，只服务于
    #    "闸口决策 -> 条件边"这一次跳转。

    #: 本次闸口决策落到哪条边（`continue` / `redo`）。
    #: 由闸口节点写、由 `route_after_g2/g3` 读、由下游第一个节点清。
    _gate_route: str
    #: 该闸口累计驳回次数（死循环保护，见 `interrupts.MAX_GATE_REJECTS`）。
    _gate_rejects: int


#: 节点 -> 该节点产出（声明式）。用于：
#:   ① `runner.report()` 说明"当前已有哪些产物"；
#:   ② `reject` 时判定"要清掉哪些下游产物"；
#:   ③ 测试断言"某个节点没重跑"时，可以直接看产物有没有被覆盖。
NODE_OUTPUTS: dict[str, tuple[str, ...]] = {
    NODE_INGEST: ("audio_paths", "chunk_plan"),
    NODE_TRANSCRIBE: ("transcript", "transcript_meta"),
    NODE_REVIEW: ("review_result", "record_markdown"),
    NODE_EXTRACT: ("topics", "resolutions", "action_items", "minutes", "issues"),
    NODE_GATE_G2: (),
    NODE_GENERATE: ("minutes_structured", "gen_retry"),
    NODE_VALIDATE: ("validation_issues", "issues", "minutes_structured"),
    NODE_ASSEMBLE: ("minutes_markdown",),
    NODE_GATE_G3: (),
    NODE_EXPORT: ("export_paths", "idem_keys"),
    NODE_ARCHIVE: (),
}

#: 节点 -> 该节点**要求**已经存在的输入键。缺了就是编排 bug，
#: 应该在节点入口立刻报错，而不是等 KeyError 飘到一半。
NODE_REQUIRES: dict[str, tuple[str, ...]] = {
    NODE_INGEST: (),
    NODE_TRANSCRIBE: ("audio_paths",),
    NODE_REVIEW: ("transcript",),
    NODE_EXTRACT: ("record_markdown",),
    NODE_GATE_G2: ("topics", "resolutions", "action_items"),
    NODE_GENERATE: ("topics", "resolutions", "action_items"),
    NODE_VALIDATE: ("minutes_structured",),
    NODE_ASSEMBLE: ("minutes_structured",),
    NODE_GATE_G3: ("minutes_markdown",),
    NODE_EXPORT: ("minutes_markdown",),
    NODE_ARCHIVE: ("export_paths",),
}


# ---------------------------------------------------------------------------
# 四、闸口决策（SAD §5.3）
# ---------------------------------------------------------------------------

#: 三种决策。SAD §5.3 的 `Decision` Literal。
Decision = Literal["approve", "reject", "edit"]

#: 合法决策集合，供节点做入参校验（非法决策要报 E4001，不能静默当 approve）。
VALID_DECISIONS: frozenset[str] = frozenset({"approve", "reject", "edit"})

#: 决策 -> 路由目标（`add_conditional_edges` 的键）。
#: `route_after_g2` / `route_after_g3` 返回这些值。
ROUTE_CONTINUE = "continue"
ROUTE_REDO = "redo"

#: 闸口 -> 决策落到哪个路由。
#: approve / edit 都走 `continue`（**都不重跑已完成节点**，这是降返工的关键）；
#: 只有 reject 走 `redo`。
GATE_ROUTE: dict[str, dict[str, str]] = {
    "G2": {
        "approve": ROUTE_CONTINUE,
        "edit": ROUTE_CONTINUE,
        "reject": ROUTE_REDO,
    },
    "G3": {
        "approve": ROUTE_CONTINUE,
        "edit": ROUTE_CONTINUE,
        # G3 驳回回 assemble（不是回 G2）—— 成稿审定驳回只需重新成文，
        # 议题本身已经确认过了。
        "reject": ROUTE_REDO,
    },
}

#: 驳回后回到哪个节点（SAD §5.3「退回上游重规划节点」）。
GATE_REDO_TARGET: dict[str, str] = {
    "G2": NODE_EXTRACT,
    "G3": NODE_ASSEMBLE,
}

#: 决策 -> 界面按钮文案（N3.3）。
#: 场景文档特意点名：`reject` 显示「退回重做」、`edit` 显示「改完继续」，
#: 必须让使用者直观感知"改完继续"不用整篇重来。
DECISION_LABEL: dict[str, str] = {
    "approve": "通过",
    "reject": "退回重做",
    "edit": "改完继续",
}


__all__ = [
    "DECISION_LABEL",
    "Decision",
    "GATE_LABEL",
    "GATE_NODES",
    "GATE_REDO_TARGET",
    "GATE_RISK",
    "GATE_ROUTE",
    "GATE_STATUSES",
    "GRAPH_STATUS",
    "GRAPH_STATUS_LABEL",
    "NODE_ARCHIVE",
    "NODE_ASSEMBLE",
    "NODE_EXPORT",
    "NODE_EXTRACT",
    "NODE_GATE_G2",
    "NODE_GATE_G3",
    "NODE_GENERATE",
    "NODE_INGEST",
    "NODE_OUTPUTS",
    "NODE_REQUIRES",
    "NODE_REVIEW",
    "NODE_SEQUENCE",
    "NODE_STATUS",
    "NODE_TRANSCRIBE",
    "NODE_VALIDATE",
    "PipelineState",
    "ROUTE_CONTINUE",
    "ROUTE_REDO",
    "STATUS_ARCHIVED",
    "STATUS_ASSEMBLING",
    "STATUS_CANCELLED",
    "STATUS_CREATED",
    "STATUS_EXPORTING",
    "STATUS_EXTRACTING",
    "STATUS_FAILED",
    "STATUS_G2_AWAITING",
    "STATUS_G3_AWAITING",
    "STATUS_GENERATING",
    "STATUS_INGEST",
    "STATUS_RECORD_READY",
    "STATUS_REVIEWING",
    "STATUS_TRANSCRIBING",
    "STATUS_TRANSCRIPT_READY",
    "STATUS_VALIDATING",
    "TERMINAL_STATUSES",
    "VALID_DECISIONS",
    "status_label",
]
