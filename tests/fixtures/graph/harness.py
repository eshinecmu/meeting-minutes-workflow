"""流水线测试用的确定性替身（T1-10）。

为什么不直接用真节点
--------------------
`ingest` 要 ffmpeg、`transcribe` 要 funasr + 2GB 模型、`export` 要
python-docx。本机没装前两个，装了也不该让**编排测试**依赖它们 ——
编排要验证的是"谁先谁后 / 在哪停 / 怎么接着跑"，
跟"转写准不准"是两件事。

所以这里提供两层替身：

    `STUB_NODES`   —— 5 个外部依赖节点（ingest / transcribe / review /
                      export / archive）换成常量输出（最快、最稳）；
    `STUB_INTERNAL` —— 4 个内部节点（extract / generate / validate /
                      assemble）的常量输出，**只在 `real=()` 时使用**。

`build_test_app` 用真节点搭图，按 `real=` 决定哪些节点走真实现。
**默认**让四个内部节点走真实现：它们的 `naive_*` 是确定性的纯函数、
没有外部依赖，只有用真实现才能测出"闸口决策真的改变了流动的数据"
（比如 edit 注入的议题真的出现在最终 Markdown 里）。
需要"只关心谁先谁后"或"只比对图的形状"时传 `real=()` 全用替身。

执行计数放在图**外**的 `Runs` 里
--------------------------------
不能把计数器放进状态：状态会被检查点持久化并在恢复时重放，
计数器会"恢复后继续累加"，`== 1` 这类断言永远算不准。
（`state.PipelineState` 的文档里也写了这条。）
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from mmwf.graph import interrupts as I
from mmwf.graph import nodes as N
from mmwf.graph import state as S
from mmwf.graph.build import (
    ROUTE_GIVE_UP,
    ROUTE_OK,
    ROUTE_RETRY,
    route_after_validate,
)
from mmwf.graph.nodes import _base as B

#: 一份"能抽出议题 / 决议 / 待办"的记录稿（朴素抽取要能识别）。
#: 每行带时间码，这样 `naive_extract` 能取到来源依据（refs），
#: 让 `validate` 不报"无依据" —— 否则主链路会一直卡在重试。
RECORD_MARKDOWN = """# 会议记录

[00:00:12] 张三：今天研究季度工作安排。
[00:00:30] 李四：决定追加五十万元用于设备采购。
[00:01:05] 王五：请赵六负责落实设备验收工作。
"""

#: 一份"抽不出任何议题"的记录稿（用于校验重试 / give_up 场景）。
EMPTY_RECORD = "# 会议记录\n"


class Runs:
    """各节点执行次数（放在图外，见模块文档）。"""

    def __init__(self) -> None:
        self.n: dict[str, int] = {}

    def bump(self, name: str) -> None:
        self.n[name] = self.n.get(name, 0) + 1

    def get(self, name: str) -> int:
        return self.n.get(name, 0)

    def reset(self) -> None:
        self.n.clear()

    def __repr__(self) -> str:  # pragma: no cover - 断言失败时好看点
        return f"Runs({self.n})"


def stub(name: str, payload: dict[str, Any], runs: Runs) -> Callable:
    """造一个常量输出节点（同时计入执行次数）。"""

    def node(state: dict) -> dict:
        runs.bump(name)
        return dict(payload)

    node.__name__ = name
    return node


def counting(name: str, fn: Callable, runs: Runs) -> Callable:
    """给一个真节点套上计数（真实现 + 可断言执行次数）。"""

    def node(state: dict) -> dict:
        runs.bump(name)
        return fn(state)

    node.__name__ = name
    return node


#: 4 个"内部"节点的替身输出（用 `real=` 点名时才走真实现）。
#:
#: 为什么默认给真实现而不是直接上替身：
#: 编排测试里大部分用例只关心"谁先谁后 / 在哪停"，
#: 但**抽掉真实现会连带抽掉两条关键行为**：
#:
#:   · `generate` 会递增 `gen_retry`。替身返回常量 payload，
#:     计数器永远不动，于是 `route_after_validate` 的
#:     `retry > MAX_VALIDATE_RETRY` **永远不成立** ——
#:     校验重试会变成**无限循环**（实测：跑到 LangGraph 的
#:     recursion_limit 才被强停，中途 generate 跑了 13 次）。
#:     这不是替身的"小失真"，而是把 U-ST-05 要防的那个 bug
#:     亲手制造出来。
#:   · `assemble` 会把问题清单渲染进正文（C03 配套）。
#:     替身不渲染，U-ST-05 的"问题清单要进正文"就没法测。
#:
#: 所以这几份替身**只在明确传 `real=()` 时**才用，且那时不要测
#: "重试上限 / 问题清单"这类依赖增量行为的用例。
STUB_INTERNAL: dict[str, dict[str, Any]] = {
    S.NODE_EXTRACT: {
        "topics": [{"title": "占位议题", "refs": [{"segment_id": "seg-1",
                                                    "start_ms": 0}]}],
        "resolutions": [{"text": "占位决议", "refs": [{"segment_id": "seg-2",
                                                       "start_ms": 1000}]}],
        "action_items": [{"text": "占位待办", "owner": "张三",
                          "refs": [{"segment_id": "seg-3", "start_ms": 2000}]}],
    },
    S.NODE_GENERATE: {"minutes_structured": {"title": "占位纪要", "sections": []}},
    S.NODE_VALIDATE: {"validation_issues": []},
    S.NODE_ASSEMBLE: {"minutes_markdown": "# 占位纪要\n"},
}

#: 5 个外部依赖节点的默认替身输出。
STUB_NODES: dict[str, dict[str, Any]] = {
    S.NODE_INGEST: {},
    S.NODE_TRANSCRIBE: {
        "transcript": [{"seq": 1, "text": "占位", "start_ms": 0, "end_ms": 1000}],
        "transcript_meta": {"engine": "stub"},
    },
    S.NODE_REVIEW: {"record_markdown": RECORD_MARKDOWN},
    S.NODE_EXPORT: {
        "export_paths": {"docx": "export/纪要.docx", "version": 1},
    },
    S.NODE_ARCHIVE: {"stage": S.STATUS_ARCHIVED},
}


def build_test_app(
    *,
    runs: Runs,
    replace: dict[str, dict[str, Any]] | None = None,
    real: tuple[str, ...] | None = None,
    validator: Callable | None = None,
    checkpointer: Any = None,
    **node_kwargs: Any,
):
    """搭一张与 `build.build_pipeline` **形状完全相同**的图。

    刻意重搭而不是 `build_pipeline(Deps(), checkpointer=...)`：
    编排测试需要**观测节点执行次数**，而 `build_pipeline` 装的是
    不带计数的真节点。重搭的代价是"图的形状可能漂移" ——
    这一条由 `test_build.py::test_shape_matches_manual_graph` 兜底。

    Args:
        runs: 执行计数容器。
        replace: 覆盖替身输出的节点（`{node: payload}`）。
        real: 这些节点用**真实现**（`nodes.make_*`）。`None`（默认）
            表示"四个内部节点用真实现"—— 它们的 `naive_*` 是确定性
            纯函数，没有外部依赖，只有用真实现才能测出
            "闸口决策真的改变了流动的数据"（edit 注入的议题真的
            出现在最终 Markdown 里）。传空元组 `()` 则**全部用替身**，
            适合只关心"谁先谁后"或"图的形状"的用例。
        validator: 替换 validate 的实现（测重试上限用）。
        checkpointer: 检查点。`None` 时用 `InMemorySaver`。
        **node_kwargs: 透传给真节点工厂（如 `extractor=`）。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    repl = {**STUB_NODES, **STUB_INTERNAL, **(replace or {})}
    if real is None:
        want_real = set(STUB_INTERNAL)       # 默认：四个内部节点用真实现
    else:
        want_real = set(real)

    def node_for(name: str) -> Callable:
        if name in want_real:
            factory = _FACTORY[name]
            return counting(name, factory(**node_kwargs), runs)
        return stub(name, repl[name], runs)

    def validator_node(state: dict) -> dict:
        """把"返回问题清单"的注入器包成节点。

        ⚠️ 注入器返回的是**清单**（`[issue, ...]`），不是状态增量。
        节点的返回值必须是 `dict`（LangGraph 会报
        `InvalidUpdateError: Expected dict, got [...]`），
        所以这里补一层 `{"validation_issues": ...}`。
        """
        runs.bump(S.NODE_VALIDATE)
        return {"validation_issues": list(validator(state))}

    g = StateGraph(S.PipelineState)
    for name in S.NODE_SEQUENCE:
        if name in (S.NODE_GATE_G2, S.NODE_GATE_G3):
            continue
        if name == S.NODE_VALIDATE and validator is not None:
            g.add_node(name, validator_node)
            continue
        g.add_node(name, node_for(name))
    g.add_node(S.NODE_GATE_G2, counting(S.NODE_GATE_G2, I.make_gate_g2(), runs))
    g.add_node(S.NODE_GATE_G3, counting(S.NODE_GATE_G3, I.make_gate_g3(), runs))

    g.add_edge(START, S.NODE_INGEST)
    g.add_edge(S.NODE_INGEST, S.NODE_TRANSCRIBE)
    g.add_edge(S.NODE_TRANSCRIBE, S.NODE_REVIEW)
    g.add_edge(S.NODE_REVIEW, S.NODE_EXTRACT)
    g.add_edge(S.NODE_EXTRACT, S.NODE_GATE_G2)
    g.add_conditional_edges(
        S.NODE_GATE_G2, I.route_after_g2,
        {"continue": S.NODE_GENERATE, "redo": S.NODE_EXTRACT},
    )
    g.add_edge(S.NODE_GENERATE, S.NODE_VALIDATE)
    g.add_conditional_edges(
        S.NODE_VALIDATE, route_after_validate,
        {ROUTE_OK: S.NODE_ASSEMBLE, ROUTE_RETRY: S.NODE_GENERATE,
         ROUTE_GIVE_UP: S.NODE_ASSEMBLE},
    )
    g.add_edge(S.NODE_ASSEMBLE, S.NODE_GATE_G3)
    g.add_conditional_edges(
        S.NODE_GATE_G3, I.route_after_g3,
        {"continue": S.NODE_EXPORT, "redo": S.NODE_ASSEMBLE},
    )
    g.add_edge(S.NODE_EXPORT, S.NODE_ARCHIVE)
    g.add_edge(S.NODE_ARCHIVE, END)

    return g.compile(checkpointer=checkpointer or InMemorySaver())


#: 真节点工厂表（`real=` 里点名的节点用它构造）。
#:
#: 每个工厂都是 `(**kw) -> 节点函数`，`kw` 只取该节点认得的那几个
#: （比如 `extract` 只吃 `extractor`）。多余的关键字会被丢掉，
#: 这样调用方不用为"这个参数只对某个节点有意义"做分支。
_FACTORY: dict[str, Callable[..., Callable]] = {
    S.NODE_INGEST: lambda **kw: N.make_ingest(None, **kw),
    S.NODE_TRANSCRIBE: lambda **kw: N.make_transcribe(None, **kw),
    S.NODE_REVIEW: lambda **kw: N.make_review(None, **kw),
    S.NODE_EXTRACT: lambda **kw: N.make_extract(None, **_pick(kw, "extractor")),
    S.NODE_GENERATE: lambda **kw: N.make_generate(None, **_pick(kw, "generator")),
    S.NODE_VALIDATE: lambda **kw: N.make_validate(None, **_pick(kw, "validator")),
    S.NODE_ASSEMBLE: lambda **kw: N.make_assemble(None, **_pick(kw, "assembler")),
    S.NODE_EXPORT: lambda **kw: N.make_export(None, **kw),
    S.NODE_ARCHIVE: lambda **kw: N.make_archive(None, **kw),
}


def _pick(kw: dict, key: str) -> dict:
    return {key: kw[key]} if key in kw else {}


def initial_state(meeting_id: str = "m-test", **extra: Any) -> dict:
    """首次 `invoke` 的初始状态（非空，见 `_base.INITIAL_STATE`）。"""
    return B.initial_state(meeting_id, **extra)


__all__ = [
    "EMPTY_RECORD",
    "RECORD_MARKDOWN",
    "STUB_INTERNAL",
    "STUB_NODES",
    "Runs",
    "build_test_app",
    "counting",
    "initial_state",
    "stub",
]
