"""D4 前 PipelineState 的冻结副本；禁止跟随生产状态定义修改。"""
from typing import TypedDict

NODE_SEQUENCE: tuple[str, ...] = (
    "ingest",
    "transcribe",
    "review",
    "extract",
    "gate_g2",
    "generate",
    "validate",
    "assemble",
    "gate_g3",
    "export",
    "archive",
)

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
