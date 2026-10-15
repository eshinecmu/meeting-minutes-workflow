"""状态机构建（T1-10，SAD §5.1）。

这个模块只做一件事：**把 11 个节点和它们的连边组装成一张图**。
业务逻辑一行都没有 —— 这正是它应该的样子。图的形状（谁接谁、
在哪停、走哪条边）单独可读，改顺序不用翻任何节点实现。

图的形状（与 SAD §5.1 逐条对应）
--------------------------------

    START -> ingest -> transcribe -> review -> extract -> gate_g2
                                                              │
                              ┌─────────────── continue ──────┤
                              ↓                               │ redo
                           generate <── (approve / edit)      │
                              ↓                               │
                           validate                           │
                              │                               │
                    ┌── ok ──┼── give_up ──┐                  │
                    ↓        └── retry ────┘(回到 generate)   │
                 assemble <────────────────────────── extract ┘
                    ↓
                 gate_g3
                    │
        ┌── continue ──┐        ┌── redo ──→ assemble
        ↓              │        │
      export ←─────────┘        │
        ↓                       │
      archive -> END

⚠️ 本模块最容易被改错的一处
---------------------------
`route_after_g2` / `route_after_g3` 返回的是 **`continue` / `redo`**
字符，而不是节点名。`add_conditional_edges` 的第二个参数是
**映射表**（`{"continue": "generate", "redo": "extract"}`）——
把映射表写成节点名的位置、或者漏一个键，会在运行时抛
`KeyError`，但那时栈指向 LangGraph 内部，很难看出是映射表的问题。

⚠️ 检查点：`thread_id` 必须传
-----------------------------
`SqliteSaver` 靠 `thread_id` 找状态。施工卡点名："不传 `thread_id`，
恢复时找不到状态"。本模块的 `compile_pipeline` **不自带** thread_id
（那是运行时的事），但 `runner.py` 会强制用 `meeting_id` 填进去。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mmwf.graph import idempotency as idem
from mmwf.graph import interrupts as I
from mmwf.graph import nodes as N
from mmwf.graph import state as S

logger = logging.getLogger(__name__)

#: 自检不通过时的重试上限（SAD §5.1 的 `retry` 边）。
MAX_VALIDATE_RETRY = N.MAX_VALIDATE_RETRY

#: `validate` 的路由值（SAD §5.1：ok / retry / give_up）。
ROUTE_OK = "ok"
ROUTE_RETRY = "retry"
ROUTE_GIVE_UP = "give_up"


@dataclass
class Deps:
    """流水线依赖容器。

    节点都从 `deps` 取"外部世界"。**全部字段可空**，缺的节点自己
    按配置构造 —— 这样单测可以只填它关心的那一两个字段。
    """

    settings: Any = None
    idem_store: idem.IdempotencyStore | None = None
    asr_provider: Any = None
    llm: Any = None
    audit: Any = None
    ingest_service: Any = None
    review_service: Any = None
    export_service: Any = None
    #: ★ T2-01 新增：`extract` 节点的抽取实现。
    #:
    #: `None` 时 `make_extract` 回落到朴素实现（`naive_extract`）——
    #: 这是**刻意的默认**：一期已验收的编排测试全部靠它，
    #: 本卡只加能力、不改既有语义。要真引擎就显式注入
    #: （`Deps(extractor=make_llm_extractor(llm, settings))`）。
    extractor: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def route_after_validate(state: dict) -> str:
    """`validate` 之后走哪条边（SAD §5.1）。

        ok       -> assemble   自检通过
        retry    -> generate   没过，重生成（最多 `MAX_VALIDATE_RETRY` 次）
        give_up  -> assemble   **超次数也成文，但带问题清单**

    `give_up` 是关键：施工卡要求"2 次重试后带问题清单成文，
    **不静默丢弃**"（C03 的配套）。把失败的任务直接丢掉，
    等于让用户白等两小时且什么也拿不到 —— 带问题的稿子至少能人工改。
    """
    issues = state.get("validation_issues") or []
    if not issues:
        return ROUTE_OK
    retry = int(state.get("gen_retry") or 0)
    if retry > MAX_VALIDATE_RETRY:
        logger.info("自检连续未通过 %d 次，带 %d 条问题成文（不再重试）",
                    retry, len(issues))
        return ROUTE_GIVE_UP
    return ROUTE_RETRY


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------

def build_pipeline(deps: Deps | None = None, *, checkpointer: Any = None,
                   on_node: Any = None):
    """构造并编译流水线图。

    Args:
        deps: 依赖容器。`None` 时会构造一个空容器（各节点自己按配置取）。
        checkpointer: 检查点存储。`None` 时**不挂**（内存态，仅测试用）——
            生产必须传 `SqliteSaver`，否则断点续跑无从谈起。
        on_node: 可选的节点执行回调 `fn(node_name: str) -> None`，
            每进入一个节点调一次（含被 `interrupt` 重放的那次）。
            API 层用它算 `rerun_nodes`（V5 验收项）；`None` 时不加包装。
    """
    from langgraph.graph import END, START, StateGraph

    d = deps or Deps()
    g = StateGraph(S.PipelineState)

    def _wrap(name: str, fn):
        """给节点套一层"执行即回调"，用于统计各节点跑了几次（T1-11）。

        为什么需要这层：`state.py` 明确要求**不要把执行次数放进状态**
        （状态会被检查点持久化并在恢复时重放，计数器会跟着重放一起累加，
        `rerun_nodes == []` 这类断言就算不准）。所以计数必须挂在
        **图外面** —— 这里就是图外的钩子。

        为什么放在 `add_node` 而不是在 runner 里事后比对：
        只有在这一层才能保证"每次真正执行"都被记到。事后比对
        只能看到状态变化，而一个节点可能跑了但没改状态（幂等命中）。

        `on_node` 为 None 时（生产默认）**不加包装**，保持零开销。
        """
        if on_node is None:
            return fn

        def _wrapped(state, *args, **kwargs):
            on_node(name)
            return fn(state, *args, **kwargs)

        _wrapped.__name__ = getattr(fn, "__name__", name)
        return _wrapped

    # ---- 节点 ----
    g.add_node(S.NODE_INGEST, _wrap(S.NODE_INGEST, N.make_ingest(d)))
    g.add_node(S.NODE_TRANSCRIBE, _wrap(S.NODE_TRANSCRIBE, N.make_transcribe(d)))
    g.add_node(S.NODE_REVIEW, _wrap(S.NODE_REVIEW, N.make_review(d)))
    g.add_node(S.NODE_EXTRACT, _wrap(S.NODE_EXTRACT, N.make_extract(d)))
    g.add_node(S.NODE_GATE_G2, _wrap(S.NODE_GATE_G2, N.make_gate_g2(d)))       # ★ 中断点
    g.add_node(S.NODE_GENERATE, _wrap(S.NODE_GENERATE, N.make_generate(d)))
    g.add_node(S.NODE_VALIDATE, _wrap(S.NODE_VALIDATE, N.make_validate(d)))
    g.add_node(S.NODE_ASSEMBLE, _wrap(S.NODE_ASSEMBLE, N.make_assemble(d)))
    g.add_node(S.NODE_GATE_G3, _wrap(S.NODE_GATE_G3, N.make_gate_g3(d)))       # ★ 中断点
    g.add_node(S.NODE_EXPORT, _wrap(S.NODE_EXPORT, N.make_export(d)))
    g.add_node(S.NODE_ARCHIVE, _wrap(S.NODE_ARCHIVE, N.make_archive(d)))

    # ---- 主干 ----
    g.add_edge(START, S.NODE_INGEST)
    g.add_edge(S.NODE_INGEST, S.NODE_TRANSCRIBE)
    g.add_edge(S.NODE_TRANSCRIBE, S.NODE_REVIEW)
    g.add_edge(S.NODE_REVIEW, S.NODE_EXTRACT)
    g.add_edge(S.NODE_EXTRACT, S.NODE_GATE_G2)

    # ---- G2 闸口：continue / redo ----
    g.add_conditional_edges(
        S.NODE_GATE_G2,
        I.route_after_g2,
        {
            S.ROUTE_CONTINUE: S.NODE_GENERATE,
            S.ROUTE_REDO: S.NODE_EXTRACT,     # reject -> 只重跑 extract
        },
    )
    g.add_edge(S.NODE_GENERATE, S.NODE_VALIDATE)

    # ---- 自检：ok / retry / give_up ----
    g.add_conditional_edges(
        S.NODE_VALIDATE,
        route_after_validate,
        {
            ROUTE_OK: S.NODE_ASSEMBLE,
            ROUTE_RETRY: S.NODE_GENERATE,     # 校验不过 -> 重生成，最多 N 次
            ROUTE_GIVE_UP: S.NODE_ASSEMBLE,   # 超次数 -> 带问题清单成文
        },
    )
    g.add_edge(S.NODE_ASSEMBLE, S.NODE_GATE_G3)

    # ---- G3 闸口：continue / redo ----
    g.add_conditional_edges(
        S.NODE_GATE_G3,
        I.route_after_g3,
        {
            S.ROUTE_CONTINUE: S.NODE_EXPORT,
            S.ROUTE_REDO: S.NODE_ASSEMBLE,    # 成稿审定驳回 -> 只重新成文
        },
    )
    g.add_edge(S.NODE_EXPORT, S.NODE_ARCHIVE)
    g.add_edge(S.NODE_ARCHIVE, END)

    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 检查点
# ---------------------------------------------------------------------------

def make_checkpointer(db_path: Path | str):
    """按任务开一个 `SqliteSaver` 连接。

    ⚠️ **每个任务一条连接，不要共用**（施工卡点名的坑）。

    为什么：`SqliteSaver` 的底层是 `sqlite3.Connection`，
    而 sqlite3 的连接默认**不允许跨线程**使用（`check_same_thread`）。
    多任务并行时如果共用一个 saver，第二个任务会拿到
    `ProgrammingError: SQLite objects created in a thread can only be used
    in that same thread` —— 这个错在单任务测试里永远看不到。

    返回的 saver 是 `contextlib.AbstractContextManager`，
    **调用方负责 `with` 或显式 `close()`**（见 runner 的用法）。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def thread_config(meeting_id: str) -> dict:
    """构造调用配置。`thread_id` **必须**用 `meeting_id`。

    用别的值（比如自增序号）会让"同一个会议换个 id 恢复"找不到状态，
    而且检查点表里会出现同一个会议的多条并行线程，肉眼看不出来。
    """
    if not meeting_id:
        raise ValueError("thread_id 不能为空（一期用 meeting_id）")
    return {"configurable": {"thread_id": meeting_id}}


__all__ = [
    "MAX_VALIDATE_RETRY",
    "ROUTE_GIVE_UP",
    "ROUTE_OK",
    "ROUTE_RETRY",
    "Deps",
    "build_pipeline",
    "make_checkpointer",
    "route_after_validate",
    "thread_config",
]
