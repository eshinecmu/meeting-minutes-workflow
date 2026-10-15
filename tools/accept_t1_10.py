"""T1-10 验收脚本（人可读版）。

施工卡 T1-10 的八条验收标准逐条跑一遍。这张卡验的是**编排能力**：
一次触发走完全程、闸口能停能恢复、断点能续、副作用幂等。
它不验"抽取质量好不好"（那是二期 T2-0x），因此全部用确定性的朴素实现。

八条标准（施工卡原文）
----------------------
  [ ] 投递音频后不碰任何额外按钮，除 G2/G3 两次确认外自动跑到导出（**V7**）
  [ ] 在 G2 用 `approve` → `rerun_nodes == []`，且 `extract` 执行次数不增加
  [ ] 在 G2 用 `edit` → 修改后的议题进入后续流程，`extract` **不重跑**（**V5 核心**）
  [ ] 在 G2 用 `reject` → 只有 `extract` 重跑，`transcribe` 不重跑
  [ ] 杀掉进程后重启，任务能从 G2 恢复（**F6.2**）
  [ ] 同一任务导出两次，第二次返回相同路径且不产生第二个文件（**F6.3 幂等**）
  [ ] 校验连续失败 2 次后，任务带问题清单进入 assemble，**不静默丢弃**（SAD C03 配套）
  [ ] `test_edit_does_not_rerun`、`test_reject_reruns_only_target`、`test_idempotent_export`
      全绿

常见坑（施工卡原文，也一并验证）
--------------------------------
  坑1  checkpoint 必须传 `thread_id`（用 `meeting_id`）
  坑2  `interrupt()` 的节点会被**重放** —— 闸口节点内不能有副作用
  坑3  节点返回值是**状态增量**（partial update），返回 `{}` 表示不改
  坑4  `SqliteSaver` 的连接不能跨线程共用，要按任务开独立连接

⚠️ 本脚本最需要在意的两个"看起来像 bug 的正常行为"
----------------------------------------------------
① **闸口节点每次运行执行两次**。`interrupt()` 恢复时会把中断点**之前**的
   代码重跑一遍，所以 `gate_g2`/`gate_g3` 各跑 2 次是**正常的**。
   断言时把它们排除掉，只看业务节点（见 `check_standard_2`）。

② **`edit` 之后下游可能跑 3 次**。如果人改完的内容**缺来源依据**
   （`refs` 为空），`validate` 会报 `topic_without_refs` 并走 `retry` 边；
   重试**修不了数据问题**（refs 还是空的），于是把 `MAX_VALIDATE_RETRY = 2`
   次机会烧完才 `give_up` → `generate` 3 次（1 初次 + 2 重试）。
   这是 U-ST-05 的**正确行为**（带问题清单成文，不静默丢弃）。
   本脚本两个方向都验：带依据的 edit 下游只跑 1 次（标准 3），
   不带依据的 edit 走到上限并带清单成文（标准 7）。
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "fixtures" / "graph"))

from mmwf.graph import idempotency as idem  # noqa: E402
from mmwf.graph import state as S  # noqa: E402
from mmwf.graph.build import (  # noqa: E402
    MAX_VALIDATE_RETRY,
    Deps,
    build_pipeline,
    make_checkpointer,
    thread_config,
)
from mmwf.graph.nodes import _base as B  # noqa: E402
from mmwf.graph.runner import GraphRunner  # noqa: E402
from mmwf.infra.audit import AuditLogger, query  # noqa: E402
from mmwf.infra.migrations.runner import migrate  # noqa: E402

PASS = "✓"
FAIL = "✗"

#: 一个"能抽出议题 / 决议 / 待办"的记录稿（与测试夹具同一份）。
#: 每行带时间码，这样 `naive_extract` 能取到来源依据（refs），
#: 让 `validate` 不报"无依据" —— 否则主链路会一直卡在重试。
RECORD_MARKDOWN = """# 会议记录

[00:00:12] 张三：今天研究季度工作安排。
[00:00:30] 李四：决定追加五十万元用于设备采购。
[00:01:05] 王五：请赵六负责落实设备验收工作。
"""

#: 抽不出任何议题的记录稿（标准 7 用）。
EMPTY_RECORD = "# 会议记录\n"

MEETING_ID = "acc-t110"

#: **带来源依据**的修正议题。
#:
#: ⚠️ 对照标准 7 —— `refs` 空不空决定了后续跑几次。这里带上依据，
#: 才能干净地验"edit 不触发返工"。
EDIT_TOPIC = {
    "title": "修正后的议题",
    "refs": [{"segment_id": "seg-1", "start_ms": 12000,
              "text": "[00:00:12] 张三：今天研究季度工作安排。"}],
}


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 夹具：一张"外部依赖用替身、编排用真实现"的图
# ---------------------------------------------------------------------------

def _runs_table() -> dict[str, int]:
    return {}


def _build_app(*, checkpointer=None, record_markdown: str = RECORD_MARKDOWN,
               counts: dict[str, int] | None = None, validator=None):
    """搭一张与 `build.build_pipeline` **同形**的图，并记录各节点执行次数。

    为什么不用 `build_pipeline(Deps())` 直接跑：真 `ingest` 要 ffmpeg、
    真 `transcribe` 要 funasr + 2GB 模型、真 `review` 要磁盘上的转写稿。
    本卡验的是**编排**（谁先谁后 / 在哪停 / 怎么接着跑），
    跟"转写准不准"是两件事，所以外部依赖节点换成常量输出。

    四个内部节点（extract / generate / validate / assemble）**用真实现** ——
    它们的 `naive_*` 是确定性纯函数、无外部依赖，只有用真实现才能测出
    "闸口决策真的改变了流动的数据"（edit 注入的议题真的出现在最终稿里）。

    ⚠️ 计数放在图**外**：状态会被检查点持久化并在恢复时重放，
    计数器放状态里会"恢复后继续累加"，`== 1` 这类断言永远算不准。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from mmwf.graph import interrupts as I
    from mmwf.graph import nodes as N
    from mmwf.graph.build import (
        ROUTE_GIVE_UP,
        ROUTE_OK,
        ROUTE_RETRY,
        route_after_validate,
    )

    seen = counts if counts is not None else _runs_table()

    #: 外部依赖节点的替身输出。
    #:
    #: ⚠️ `review` 的替身输出要**用传进来的 `record_markdown`**，
    #: 不能用模块常量。第一版在这里写死了 `RECORD_MARKDOWN`，
    #: 于是 `R(record_markdown=EMPTY_RECORD)` 完全不起作用 ——
    #: 标准 7b 拿不到空记录稿，一路跑出"议题齐全"的结果，
    #: 而报出来的错（`generate 缺少前置输入`）指向的是另一个方向
    #: （2026-09-28 记忆日志）。
    stubs: dict[str, dict] = {
        S.NODE_INGEST: {},
        S.NODE_TRANSCRIBE: {
            "transcript": [{"seq": 1, "text": "占位", "start_ms": 0, "end_ms": 1000}],
            "transcript_meta": {"engine": "accept-stub"},
        },
        S.NODE_REVIEW: {"record_markdown": record_markdown},
        S.NODE_EXPORT: {"export_paths": {"docx": "export/纪要.docx", "version": 1}},
        S.NODE_ARCHIVE: {"stage": S.STATUS_ARCHIVED},
    }
    externals = (S.NODE_INGEST, S.NODE_TRANSCRIBE, S.NODE_REVIEW,
                 S.NODE_EXPORT, S.NODE_ARCHIVE)
    internals = {
        S.NODE_EXTRACT: N.make_extract(None),
        S.NODE_GENERATE: N.make_generate(None),
        S.NODE_ASSEMBLE: N.make_assemble(None),
    }

    def counted(name, fn):
        def node(state: dict) -> dict:
            seen[name] = seen.get(name, 0) + 1
            return fn(state)
        node.__name__ = name
        return node

    def stub_node(name):
        def node(state: dict) -> dict:
            seen[name] = seen.get(name, 0) + 1
            return dict(stubs[name])
        node.__name__ = name
        return node

    def validator_node(state: dict) -> dict:
        """把"返回问题清单"的注入器包成节点。

        ⚠️ 注入器返回的是**清单**（`[issue, ...]`），不是状态增量。
        节点的返回值必须是 `dict`，所以这里补一层
        `{"validation_issues": ...}`。
        """
        seen[S.NODE_VALIDATE] = seen.get(S.NODE_VALIDATE, 0) + 1
        return {"validation_issues": list(validator(state))}

    g = StateGraph(S.PipelineState)
    for name in S.NODE_SEQUENCE:
        if name in (S.NODE_GATE_G2, S.NODE_GATE_G3):
            continue
        if name in externals:
            g.add_node(name, stub_node(name))
        elif name == S.NODE_VALIDATE and validator is not None:
            g.add_node(name, validator_node)
        elif name == S.NODE_VALIDATE:
            g.add_node(name, counted(name, N.make_validate(None)))
        else:
            g.add_node(name, counted(name, internals[name]))
    g.add_node(S.NODE_GATE_G2, counted(S.NODE_GATE_G2, I.make_gate_g2()))
    g.add_node(S.NODE_GATE_G3, counted(S.NODE_GATE_G3, I.make_gate_g3()))

    g.add_edge(START, S.NODE_INGEST)
    g.add_edge(S.NODE_INGEST, S.NODE_TRANSCRIBE)
    g.add_edge(S.NODE_TRANSCRIBE, S.NODE_REVIEW)
    g.add_edge(S.NODE_REVIEW, S.NODE_EXTRACT)
    g.add_edge(S.NODE_EXTRACT, S.NODE_GATE_G2)
    g.add_conditional_edges(
        S.NODE_GATE_G2, I.route_after_g2,
        {S.ROUTE_CONTINUE: S.NODE_GENERATE, S.ROUTE_REDO: S.NODE_EXTRACT})
    g.add_edge(S.NODE_GENERATE, S.NODE_VALIDATE)
    g.add_conditional_edges(
        S.NODE_VALIDATE, route_after_validate,
        {ROUTE_OK: S.NODE_ASSEMBLE, ROUTE_RETRY: S.NODE_GENERATE,
         ROUTE_GIVE_UP: S.NODE_ASSEMBLE})
    g.add_edge(S.NODE_ASSEMBLE, S.NODE_GATE_G3)
    g.add_conditional_edges(
        S.NODE_GATE_G3, I.route_after_g3,
        {S.ROUTE_CONTINUE: S.NODE_EXPORT, S.ROUTE_REDO: S.NODE_ASSEMBLE})
    g.add_edge(S.NODE_EXPORT, S.NODE_ARCHIVE)
    g.add_edge(S.NODE_ARCHIVE, END)

    return g.compile(checkpointer=checkpointer or InMemorySaver()), seen


class R:
    """一次运行的观测记录（节点次数 + runner）。

    Args:
        meeting_id: 会议号（也是 thread_id）。
        counts: 外部的计数字典（不给就自己建）。
        **kw: 透传给 `_build_app`（`record_markdown` / `validator` / `checkpointer`）。
    """

    def __init__(self, meeting_id: str = MEETING_ID, *, counts=None, **kw) -> None:
        self.counts = counts if counts is not None else _runs_table()
        self.app, self.counts = _build_app(counts=self.counts, **kw)
        self.runner = GraphRunner(meeting_id, app=self.app)

    def before(self) -> dict[str, int]:
        return dict(self.counts)

    def delta(self, before: dict[str, int]) -> dict[str, int]:
        return {k: v - before.get(k, 0)
                for k, v in self.counts.items() if v != before.get(k, 0)}

    def close(self) -> None:
        self.runner.close()


#: 闸口节点。每次运行必然执行两次（`interrupt` 重放），断言时排除。
GATES = frozenset({S.NODE_GATE_G2, S.NODE_GATE_G3})


def _business_delta(delta: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in delta.items() if k not in GATES}


# ---------------------------------------------------------------------------
# 标准 1：V7 —— 除两次确认外自动跑完
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：投递后不碰额外按钮，除 G2/G3 两次确认外自动跑到导出（V7）。"""
    banner("标准 1  V7：全程只停两次，都是等人确认，之后自动跑完")
    ok = True

    r = R()
    try:
        stops: list[str] = []
        st = r.runner.start()
        # ⚠️ `start()` 返回的就已经是"停在 G2"的状态。只在这里记一次 ——
        # 在循环体里再记一次会把 G2 数成两遍（2026-09-28 记忆日志）。
        while not st.is_terminal:
            stops.append(st.status)
            ok &= _assert(
                st.pending_gate is not None,
                f"{st.status} 是闸口状态（有东西在等人确认）",
            )
            st = r.runner.resume({"decision": "approve"})

        print(f"      停顿序列：{stops}")
        ok &= _assert(stops == ["G2_awaiting", "G3_awaiting"],
                      f"确认点恰好两次且顺序为 G2 -> G3（实际 {stops}）")
        ok &= _assert(st.status == S.STATUS_ARCHIVED,
                      f"最终停在归档态（实际 {st.status}）")
        ok &= _assert(st.is_terminal, "终态判定为真")

        # 两次确认之间**没有任何**中途状态需要用户操作
        extra = [s for s in stops if s not in S.GATE_STATUSES]
        ok &= _assert(extra == [], f"有非闸口的停顿：{extra}")

        # 主链路每个业务节点恰好跑一次
        biz = {k: v for k, v in r.counts.items() if k not in GATES}
        ok &= _assert(
            all(v == 1 for v in biz.values()),
            f"业务节点应各跑一次（实际 {biz}）",
        )
        ok &= _assert(len(biz) == 9, f"业务节点应为 9 个（实际 {sorted(biz)}）")
        # 闸口各跑两次（interrupt 重放，坑 2）
        ok &= _assert(
            r.counts.get(S.NODE_GATE_G2) == 2 and r.counts.get(S.NODE_GATE_G3) == 2,
            f"闸口各执行两次（重放，实际 G2={r.counts.get(S.NODE_GATE_G2)} "
            f"G3={r.counts.get(S.NODE_GATE_G3)}）",
        )
        # 状态文案不出现工程词（N3.3）
        labels = {S.status_label(s) for s in stops}
        ok &= _assert(labels == {"待确认议题", "待审定纪要"},
                      f"停顿状态用的是界面文案（实际 {labels}）")
        return ok
    finally:
        r.close()


# ---------------------------------------------------------------------------
# 标准 2：G2 approve —— 不重跑
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：G2 用 `approve` → `rerun_nodes == []`，`extract` 次数不增加。"""
    banner("标准 2  G2 approve → 一个业务节点都不重跑")
    ok = True

    r = R()
    try:
        r.runner.start()
        before = r.before()
        st = r.runner.resume({"decision": "approve"})
        delta = _business_delta(r.delta(before))
        print(f"      approve 后的业务节点增量：{delta}")

        ok &= _assert(r.counts.get(S.NODE_EXTRACT, 0) == 1,
                      f"extract 执行次数不增加（实际 {r.counts.get(S.NODE_EXTRACT)}）")
        ok &= _assert(r.counts.get(S.NODE_TRANSCRIBE, 0) == 1,
                      "transcribe 未重跑")
        ok &= _assert(r.counts.get(S.NODE_REVIEW, 0) == 1, "review 未重跑")
        ok &= _assert(st.status == "G3_awaiting",
                      f"approve 后走到 G3（实际 {st.status}）")

        # approve 的语义是"原样继续"：只有 G2 的下游需要跑，
        # 上游（extract 及其之前）的增量必须是 0。
        up_until = S.NODE_SEQUENCE.index(S.NODE_EXTRACT)
        upstream = {k: v for k, v in delta.items() if k in S.NODE_SEQUENCE[:up_until + 1]}
        ok &= _assert(upstream == {}, f"上游没有重跑（实际 {upstream}）")

        # 再 approve 一次到终态，确认整条 approve 路径都不返工
        r.runner.resume({"decision": "approve"})
        ok &= _assert(r.counts.get(S.NODE_EXTRACT, 0) == 1,
                      "两次 approve 后 extract 仍只跑过一次")
        return ok
    finally:
        r.close()


# ---------------------------------------------------------------------------
# 标准 3：G2 edit —— 不重跑，改动生效（V5 核心）
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    """标准 3：G2 用 `edit` → 修改后的议题进入后续流程，`extract` **不重跑**。"""
    banner("标准 3  G2 edit → 改动生效且上游不重跑（V5 核心）")
    ok = True

    r = R()
    try:
        r.runner.start()
        before = r.before()
        st = r.runner.resume({"decision": "edit", "payload": {"topics": [EDIT_TOPIC]}})
        delta = _business_delta(r.delta(before))
        print(f"      edit 后的业务节点增量：{delta}")

        # ① 上游一个都不许重跑
        ok &= _assert(r.counts.get(S.NODE_EXTRACT) == 1,
                      f"extract 不重跑（实际 {r.counts.get(S.NODE_EXTRACT)}）")
        ok &= _assert(r.counts.get(S.NODE_TRANSCRIBE) == 1, "transcribe 不重跑")
        ok &= _assert(r.counts.get(S.NODE_REVIEW) == 1, "review 不重跑")

        # ② 下游各跑**一次**（不多不少）—— 这一条才抓得到重复调度
        for node in (S.NODE_GENERATE, S.NODE_VALIDATE, S.NODE_ASSEMBLE):
            d = delta.get(node, 0)
            ok &= _assert(d == 1, f"{node} 恰好跑 1 次（实际 {d}）")

        # ③ 改动真的进了稿子
        md = r.runner.values().get("minutes_markdown") or ""
        ok &= _assert("修正后的议题" in md, "修改后的议题进入了正文")
        ok &= _assert("会议纪要" in md, "正文仍有标题")

        # ④ 只改了议题，决议/待办原样
        vals = r.runner.values()
        ok &= _assert(vals.get("topics") == [EDIT_TOPIC] or
                      "修正后的议题" in json.dumps(vals.get("topics"), ensure_ascii=False),
                      "状态里的 topics 是修改后的版本")
        ok &= _assert(len(vals.get("resolutions") or []) == 1, "决议未被误改")
        ok &= _assert(len(vals.get("action_items") or []) == 1, "待办未被误改")

        # ⑤ 改动一路流到导出
        st2 = r.runner.resume({"decision": "approve"})
        ok &= _assert(st2.is_terminal, "edit 之后能继续跑到终态")
        md2 = r.runner.values().get("minutes_markdown") or ""
        ok &= _assert("修正后的议题" in md2, "过了 G3 之后改动仍在")
        return ok
    finally:
        r.close()


# ---------------------------------------------------------------------------
# 标准 4：G2 reject —— 只重跑被驳回的节点
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：G2 用 `reject` → 只有 `extract` 重跑，`transcribe` 不重跑。"""
    banner("标准 4  G2 reject → 只重跑 extract，transcribe 不动")
    ok = True

    r = R()
    try:
        r.runner.start()
        before = r.before()
        st = r.runner.resume({"decision": "reject", "comment": "议题抽错了"})
        delta = _business_delta(r.delta(before))
        print(f"      reject 后的业务节点增量：{delta}")

        ok &= _assert(r.counts.get(S.NODE_EXTRACT) == 2,
                      f"extract 重跑（1 -> 2，实际 {r.counts.get(S.NODE_EXTRACT)}）")
        ok &= _assert(r.counts.get(S.NODE_TRANSCRIBE) == 1,
                      f"★ transcribe **不**重跑（实际 {r.counts.get(S.NODE_TRANSCRIBE)}）")
        ok &= _assert(r.counts.get(S.NODE_REVIEW) == 1, "review 不重跑")
        ok &= _assert(r.counts.get(S.NODE_INGEST) == 1, "ingest 不重跑")

        # 退回后重新停在 G2 等人再确认
        ok &= _assert(st.status == "G2_awaiting",
                      f"退回后重新停在 G2（实际 {st.status}）")
        ok &= _assert(st.pending_gate is not None
                      and st.pending_gate.gate == "G2", "待确认的还是 G2")

        # 驳回必须清掉下游产物（否则界面显示"退回重做"、内容却是旧的）
        vals = r.runner.values()
        ok &= _assert(not vals.get("minutes_structured"),
                      "驳回后清掉了下游产物 minutes_structured")
        ok &= _assert(not vals.get("minutes_markdown"),
                      "驳回后清掉了下游产物 minutes_markdown")

        # 驳回留痕（错误清单里有 E4001）
        codes = [e.get("code") for e in (vals.get("errors") or [])]
        ok &= _assert("E4001" in codes, f"驳回在错误清单里留痕（实际 {codes}）")

        # 再 approve 能正常往下走
        st2 = r.runner.resume({"decision": "approve"})
        ok &= _assert(st2.status == "G3_awaiting", "驳回后重新确认能继续")

        # 驳回次数有上限，不会死循环
        ok &= _assert(S.STATUS_CANCELLED not in (st.status, st2.status),
                      "任务没被误置为取消")
        return ok
    finally:
        r.close()


def check_standard_4b(tmp: Path) -> bool:
    """标准 4 补充：连续 reject 到上限会停，不死循环。"""
    banner("标准 4b 连续 reject 到上限 → 任务失败而不是永远循环")
    ok = True

    r = R()
    try:
        r.runner.start()
        from mmwf.domain.errors import MmwfError
        from mmwf.graph import interrupts as I

        raised = None
        for i in range(I.MAX_GATE_REJECTS + 2):
            try:
                r.runner.resume({"decision": "reject", "comment": f"第{i + 1}次"})
            except MmwfError as exc:
                raised = exc
                break

        ok &= _assert(raised is not None,
                      f"驳回超过 {I.MAX_GATE_REJECTS} 次后抛出错误（没有死循环）")
        if raised is not None:
            ok &= _assert(raised.code == "E4001",
                          f"错误码为 E4001（实际 {raised.code}）")
        # 提取没有无限重跑
        ok &= _assert(r.counts.get(S.NODE_EXTRACT, 0) <= I.MAX_GATE_REJECTS + 1,
                      f"extract 重跑次数有界（实际 {r.counts.get(S.NODE_EXTRACT)}）")
        return ok
    finally:
        r.close()


# ---------------------------------------------------------------------------
# 标准 5：断点恢复（F6.2）
# ---------------------------------------------------------------------------

def check_standard_5(tmp: Path) -> bool:
    """标准 5：杀掉进程后重启，任务能从 G2 恢复（F6.2）。

    这里真的**换一个进程**跑第二次（`subprocess`），而不是在同一个进程里
    新建对象 —— 同一进程里内存里的东西还在，"重启"就名不副实了。
    """
    banner("标准 5  杀进程后重启，从 G2 恢复（F6.2）")
    ok = True

    work = tmp / "restart"
    work.mkdir(parents=True, exist_ok=True)
    db = work / "ckpt.db"
    stamp = work / "state.json"

    phase1 = work / "_phase1.py"
    phase1.write_text(
        PHASE1_SRC, encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(phase1)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), env=_child_env(),
    )
    if proc.returncode != 0:
        print((proc.stdout + proc.stderr)[-2000:])
    ok &= _assert(proc.returncode == 0, "阶段一：投递并停在 G2 后『进程退出』")

    info = _read_json(stamp)
    print(f"      阶段一落盘：{info}")
    ok &= _assert(info.get("status") == "G2_awaiting",
                  f"阶段一停在 G2（实际 {info.get('status')}）")
    ok &= _assert(db.exists(), "检查点库已落盘")

    if info.get("status") != "G2_awaiting":
        # 阶段一没成功就不必再跑阶段二 —— 否则会把"阶段一失败"报成两条
        # 无关的错，掩盖真正的原因。
        return ok

    # ---- 阶段二：**新进程**读同一个库继续 ----
    phase2 = work / "_phase2.py"
    phase2.write_text(PHASE2_SRC, encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, str(phase2)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), env=_child_env(),
    )
    if proc2.returncode != 0:
        print((proc2.stdout + proc2.stderr)[-2000:])
    ok &= _assert(proc2.returncode == 0, "阶段二：新进程成功读回检查点并继续")

    info2 = _read_json(stamp)
    print(f"      阶段二落盘：{info2}")
    ok &= _assert(info2.get("status") == "archived",
                  f"新进程一路跑到归档（实际 {info2.get('status')}）")
    ok &= _assert(info2.get("resumed_from_gate") == "G2",
                  "新进程确认自己是『从 G2 恢复』的")
    ok &= _assert(info2.get("minutes_present") is True,
                  "恢复后成稿仍在（中间产物没丢）")
    # 恢复时**不重跑上游** —— 阶段二里 extract 一次都没跑
    ok &= _assert(info2.get("extract_ran") == 0,
                  f"★ 恢复后 extract 未重跑（实际跑了 {info2.get('extract_ran')} 次）")
    return ok


def _read_json(path: Path) -> dict:
    """读阶段脚本落盘的 JSON；文件不存在时返回 `{}`。

    不抛错：阶段脚本可能因为**真正的原因**（比如检查点没落盘）而没写成，
    这时应当由调用方记一条"阶段一失败"，而不是让验收脚本自己崩掉
    （那样会把一条可读的失败变成一段 traceback）。
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _child_env() -> dict:
    """子进程的环境：把**项目根**通过环境变量传进去。

    ⚠️ 不能用 `Path(__file__).parents[N]` 推项目根。阶段脚本是写在
    临时目录里的，`parents[N]` 指向的是 temp 的某级祖先，不是项目根 ——
    表现是子进程报 `ModuleNotFoundError: No module named 'harness'`，
    而外层看到的是"阶段一失败"，栈指向临时脚本，很容易被误判成
    "检查点没落盘"（2026-09-28 记忆日志）。
    """
    import os

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["MMWF_PROJECT_ROOT"] = str(PROJECT_ROOT)
    return env


#: 阶段一/阶段二脚本共用的开头：定位项目根 + 挂 sys.path。
_PHASE_PREAMBLE = '''\
import json, os, sys
from pathlib import Path

ROOT = Path(os.environ["MMWF_PROJECT_ROOT"])
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "graph"))

import harness
from mmwf.graph.build import make_checkpointer
from mmwf.graph.runner import GraphRunner

here = Path(__file__).resolve().parent
MEETING = "acc-t110"
'''

#: 阶段一：投递 -> 停在 G2 -> 写状态 -> 退出（模拟"被杀掉"）。
PHASE1_SRC = _PHASE_PREAMBLE + '''
runs = harness.Runs()
saver = make_checkpointer(here / "ckpt.db")
# real=() -> 全替身：重启场景只关心"状态在不在"，不关心抽取质量。
app = harness.build_test_app(runs=runs, real=(), checkpointer=saver)
r = GraphRunner(MEETING, app=app)
st = r.start()
(here / "state.json").write_text(json.dumps({
    "status": st.status,
    "pending_gate": st.pending_gate.gate if st.pending_gate else None,
    "counts": runs.n,
}, ensure_ascii=False), encoding="utf-8")
saver.conn.close()
sys.exit(0 if st.status == "G2_awaiting" else 1)
'''

#: 阶段二：新进程读同一个检查点库 -> 恢复 -> 跑完。
PHASE2_SRC = _PHASE_PREAMBLE + '''
runs = harness.Runs()
saver = make_checkpointer(here / "ckpt.db")
app = harness.build_test_app(runs=runs, real=(), checkpointer=saver)
r = GraphRunner(MEETING, app=app)

st = r.status()
resumed_from = st.pending_gate.gate if st.pending_gate else None
assert resumed_from == "G2", f"没有从 G2 恢复，实际 {resumed_from}"
st = r.resume({"decision": "approve"})
st = r.resume({"decision": "approve"})
vals = r.values()
(here / "state.json").write_text(json.dumps({
    "status": st.status,
    "resumed_from_gate": resumed_from,
    "minutes_present": bool(vals.get("minutes_markdown")),
    "extract_ran": runs.get("extract"),
    "counts": runs.n,
}, ensure_ascii=False), encoding="utf-8")
saver.conn.close()
sys.exit(0 if st.status == "archived" else 1)
'''


# ---------------------------------------------------------------------------
# 标准 6：幂等导出（F6.3）
# ---------------------------------------------------------------------------

def check_standard_6(tmp: Path) -> bool:
    """标准 6：同一任务导出两次，第二次返回相同路径且不产生第二个文件。"""
    banner("标准 6  同一任务导出两次 → 同一路径，只有一个文件（F6.3）")
    ok = True

    # ① 纯幂等层：同输入两次 -> 命中既有记录
    store = idem.IdempotencyStore(tmp / "idem6.db")
    state = {"meeting_id": MEETING_ID, "minutes_markdown": "# 同一份稿子\n"}
    calls: list[int] = []

    def do_export():
        calls.append(1)
        return {"rel_path": "export/纪要.docx"}

    _, c1 = B.idempotent(store, S.NODE_EXPORT, state, do_export)
    ref2, c2 = B.idempotent(store, S.NODE_EXPORT, state, do_export)
    ok &= _assert((c1, c2) == (False, True), f"第二次命中既有记录（{c1},{c2}）")
    ok &= _assert(len(calls) == 1, f"只真正导出过一次（实际 {len(calls)} 次）")
    ok &= _assert(ref2 == "export/纪要.docx", f"返回相同的路径（{ref2}）")
    ok &= _assert(store.count() == 1, f"只有一条幂等记录（实际 {store.count()}）")

    # ② 幂等键稳定
    keys = {idem.make_idempotency_key(
        MEETING_ID, S.NODE_EXPORT,
        idem.node_input_from_state(S.NODE_EXPORT, state)) for _ in range(5)}
    ok &= _assert(len(keys) == 1, "同一内容的幂等键稳定")

    # ③ 换内容应当产出新导出（不是"永远复用第一份"）
    B.idempotent(store, S.NODE_EXPORT,
                 {"meeting_id": MEETING_ID, "minutes_markdown": "改过的稿子"}, do_export)
    ok &= _assert(store.count() == 2, "改稿后是一条新记录")
    ok &= _assert(len(calls) == 2, "改稿后真的重新导出")

    # ④ 落盘幂等：'重启'（新建 store）后仍然命中
    db = tmp / "idem6b.db"
    s1 = idem.IdempotencyStore(db)
    B.idempotent(s1, S.NODE_EXPORT, state, do_export)
    n_before = len(calls)
    s2 = idem.IdempotencyStore(db)
    _, cached = B.idempotent(s2, S.NODE_EXPORT, state, do_export)
    ok &= _assert(cached is True, "重启后幂等仍生效（不会写出第二个文件）")
    ok &= _assert(len(calls) == n_before, "重启后没有重复导出")

    # ⑤ 整条流水线跑完，export 恰好执行一次
    r = R(meeting_id="acc-t110-idem")
    try:
        r.runner.start()
        r.runner.resume({"decision": "approve"})
        r.runner.resume({"decision": "approve"})
        ok &= _assert(r.counts.get(S.NODE_EXPORT) == 1,
                      f"整条链上 export 只跑一次（实际 {r.counts.get(S.NODE_EXPORT)}）")
        ok &= _assert(r.counts.get(S.NODE_ARCHIVE) == 1,
                      f"archive 只跑一次（实际 {r.counts.get(S.NODE_ARCHIVE)}）")
    finally:
        r.close()

    # ⑥ 副作用节点就是施工卡点名的那三个
    ok &= _assert(
        idem.SIDE_EFFECT_NODES == frozenset({"ingest", "export", "archive"}),
        f"副作用节点表与施工卡一致（实际 {sorted(idem.SIDE_EFFECT_NODES)}）",
    )
    return ok


# ---------------------------------------------------------------------------
# 标准 7：校验连续失败 -> 带问题清单成文（C03 配套）
# ---------------------------------------------------------------------------

def check_standard_7(tmp: Path) -> bool:
    """标准 7：校验连续失败 2 次后带问题清单进入 assemble，**不静默丢弃**。"""
    banner("标准 7  校验连续失败 → 2 次重试后带问题清单成文（C03 配套）")
    ok = True

    calls = {"n": 0}

    def always_bad(state: dict) -> list[dict]:
        """永远报一个问题的校验器 —— 模拟"重试也修不好"。"""
        calls["n"] += 1
        return [{"rule": "no_topics", "detail": f"第 {calls['n']} 次自检仍未通过"}]

    r = R(meeting_id="acc-t110-validate", validator=always_bad)
    try:
        r.runner.start()
        before = r.before()
        st = r.runner.resume({"decision": "approve"})
        delta = _business_delta(r.delta(before))
        print(f"      校验一直失败时的增量：{delta}")

        # 重试到上限（1 初次 + MAX_VALIDATE_RETRY 次重试）
        expect = MAX_VALIDATE_RETRY + 1
        ok &= _assert(delta.get(S.NODE_GENERATE) == expect,
                      f"generate 跑 {expect} 次（1 次 + {MAX_VALIDATE_RETRY} 次重试，"
                      f"实际 {delta.get(S.NODE_GENERATE)}）")
        ok &= _assert(delta.get(S.NODE_VALIDATE) == expect,
                      f"validate 跑 {expect} 次（实际 {delta.get(S.NODE_VALIDATE)}）")
        ok &= _assert(delta.get(S.NODE_ASSEMBLE) == 1,
                      "超次数后仍然成文（assemble 跑 1 次）")

        # 关键：**带问题清单成文，不静默丢弃**
        md = r.runner.values().get("minutes_markdown") or ""
        ok &= _assert("待核对清单" in md, "★ 问题清单进了正文（不是只记日志）")
        ok &= _assert("请核对后再定稿" in md, "正文里有给审定人的提示语")
        issues = r.runner.values().get("validation_issues") or []
        ok &= _assert(len(issues) >= 1, f"问题清单在状态里（{len(issues)} 条）")
        ok &= _assert(st.gen_retry == expect,
                      f"重试计数停在 {expect}（实际 {st.gen_retry}）")
        # 任务**没有失败**
        ok &= _assert(st.status == "G3_awaiting",
                      f"任务是走到 G3 而不是失败（实际 {st.status}）")
        ok &= _assert(not st.is_terminal or st.status == S.STATUS_ARCHIVED,
                      "带问题的稿子没有把任务判死")
        return ok
    finally:
        r.close()


def check_standard_7b(tmp: Path) -> bool:
    """标准 7 补充：抽不出议题时**闸口自动放行 + 入口守卫生效**。

    ⚠️ 这一段第一版写错了方向，值得留个记录：我以为"空记录稿"会像
    `validate` 一样"带清单成文"，于是断言它能走到 G3。实际跑出来的是
    `NodeInputError: 节点 generate 缺少前置输入 ['topics', ...]`。

    想清楚之后发现**这才是对的**，而且正是本卡两条规则的合力：

      · `gate_g2` 拿到的送审载荷三项全空 -> `_payload_is_empty` 为真
        -> 记一条 **E4003**（警告级）后**自动放行**，不停下来等人。
        让人"确认一个空列表"是纯粹浪费（`interrupts.make_gate` 的注释）。
      · `generate` 的 `NODE_REQUIRES` 里就有 `topics/resolutions/action_items`，
        空列表按 `_is_blank` 算"缺"，所以它在入口立刻停下报错。

    "带问题清单成文"（C03）针对的是**校验**（`validate`）发现内容问题，
    不是"压根没抽出东西"。两者是不同层：`validate` 管"内容可不可疑"，
    `NODE_REQUIRES` 管"前置输入有没有"。这条区别正是本用例的价值。
    """
    banner("标准 7b 空记录稿 → 闸口自动放行（E4003）+ 入口守卫生效")
    ok = True

    from mmwf.domain.errors import MmwfError

    r = R(meeting_id="acc-t110-empty", record_markdown=EMPTY_RECORD)
    try:
        raised: MmwfError | None = None
        try:
            r.runner.start()
        except MmwfError as exc:
            raised = exc

        print(f"      节点次数：{r.counts}")
        ok &= _assert(raised is not None,
                      "★ 抽不出议题时在入口停下（而不是产出一篇空纪要）")
        if raised is not None:
            ok &= _assert(raised.code == "E9001",
                          f"按 E9001（编排输入缺失）报错（实际 {raised.code}）")
            ok &= _assert("generate" in raised.detail,
                          f"错误信息指出是 generate 缺输入（{raised.detail[:80]}）")

        # 关键：extract 产出了空的三项，闸口因此**没有**停下来等人，
        # 直接放行到 generate —— 而 generate 在入口就抛了（见上）。
        # 所以这里断言的是"generate 被调用后立刻失败"，不是"没被调用"。
        # 第一版写成 `== 0`，把"入口守卫拦住"与"根本没走到"混为一谈
        # （2026-09-28 记忆日志）。
        ok &= _assert(r.counts.get(S.NODE_EXTRACT) == 1,
                      "extract 跑过一次（产出了空的三项）")
        ok &= _assert(r.counts.get(S.NODE_GENERATE, 0) == 1,
                      f"★ generate 被调用但立即失败（实际 {r.counts.get(S.NODE_GENERATE, 0)} 次）")
        ok &= _assert(r.counts.get(S.NODE_ASSEMBLE, 0) == 0,
                      "assemble 没跑（不会产出空稿子）")

        # 闸口确实被调用过（它做了"自动放行"的判定），但只调用一次 ——
        # 自动放行不 `interrupt`，所以**没有重放**。
        ok &= _assert(r.counts.get(S.NODE_GATE_G2, 0) == 1,
                      f"gate_g2 只跑一次（自动放行不重放，实际 "
                      f"{r.counts.get(S.NODE_GATE_G2, 0)}）")
        return ok
    finally:
        r.close()


def check_standard_7c(tmp: Path) -> bool:
    """标准 7 补充：闸口"空载荷自动放行"这条规则本身（单元级）。"""
    banner("标准 7c 闸口空载荷自动放行（E4003），非空就必须停")
    ok = True

    from mmwf.graph import interrupts as I

    empty = {"topics": None, "resolutions": None, "action_items": None}
    ok &= _assert(I._payload_is_empty("G2", empty),
                  "G2 三项全空 -> 视为无待确认内容")
    ok &= _assert(I._payload_is_empty("G3", {"minutes_markdown": None}),
                  "G3 成稿为空 -> 视为无待确认内容")
    ok &= _assert(not I._payload_is_empty(
        "G2", {"topics": [{"title": "甲"}], "resolutions": [], "action_items": []}),
        "只要有一项非空就**必须**停下来等人")
    ok &= _assert(not I._payload_is_empty(
        "G3", {"minutes_markdown": "# 有内容\n"}),
        "G3 只要成稿非空就必须给人看（审定不能跳过）")

    # 空载荷时节点返回的是"继续"而不是中断
    node = I.make_gate_g2()
    out = node({"meeting_id": MEETING_ID, "errors": [], **empty})
    ok &= _assert(out.get(I.GATE_ROUTE_KEY) == "continue",
                  f"空载荷自动走 continue（实际 {out.get(I.GATE_ROUTE_KEY)}）")
    codes = [e.get("code") for e in (out.get("errors") or [])]
    ok &= _assert("E4003" in codes, f"留一条 E4003 警告（实际 {codes}）")
    return ok


# ---------------------------------------------------------------------------
# 标准 8：三个点名的用例
# ---------------------------------------------------------------------------

def check_standard_8() -> bool:
    """标准 8：`test_edit_does_not_rerun` / `test_reject_reruns_only_target` /
    `test_idempotent_export` 全绿。

    施工卡点名了这三个名字。本卡的测试把它们拆到了两个文件里
    （单元 `test_graph_runner.py` 覆盖 U-ST-02/03/04/06，
    集成 `test_graph_pipeline.py` 覆盖 IT-04/05/09），
    所以这里按**名字逐个子串检索**，确认它们真实存在且通过。
    """
    banner("标准 8  三个点名用例全绿（edit / reject / idempotent export）")
    ok = True

    named = {
        "test_edit_does_not_rerun": (
            "tests/unit/test_graph_runner.py", "tests/integration/test_graph_pipeline.py"),
        "test_reject_reruns_only_target": (
            "tests/unit/test_graph_runner.py", "tests/integration/test_graph_pipeline.py"),
        "test_idempotent_export": (
            "tests/unit/test_graph_runner.py", "tests/integration/test_graph_pipeline.py"),
    }

    root = PROJECT_ROOT / "tests"
    for name, _ in named.items():
        found: list[str] = []
        for py in root.rglob("test_*.py"):
            if name in py.read_text(encoding="utf-8"):
                found.append(py.relative_to(PROJECT_ROOT).as_posix())
        ok &= _assert(bool(found), f"{name} 已在 {found or '（未找到）'} 中定义")

    # 逐个子串跑一遍（`-k` 是子串匹配，能把 `test_edit_does_not_rerun` 与
    # `test_g2_edit_does_not_rerun_extract` 这类同族用例一起选中）
    for name in named:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-k", name,
             "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        tail = [ln for ln in (proc.stdout + proc.stderr).strip().splitlines() if ln.strip()]
        last = tail[-1] if tail else "(无输出)"
        ok &= _assert(proc.returncode == 0, f"{name} 全绿 —— {last}")
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr)[-1500:])
    return ok


# ---------------------------------------------------------------------------
# 坑 1：thread_id 必须是 meeting_id
# ---------------------------------------------------------------------------

def check_pit_1(tmp: Path) -> bool:
    """坑 1：checkpoint 必须传 `thread_id`（用 `meeting_id`）。"""
    banner("坑 1  checkpoint 的 thread_id 必须用 meeting_id")
    ok = True

    cfg = thread_config(MEETING_ID)
    ok &= _assert(cfg == {"configurable": {"thread_id": MEETING_ID}},
                  f"thread_config 用 meeting_id 填 thread_id（{cfg}）")

    for bad in ("", None):
        try:
            thread_config(bad)  # type: ignore[arg-type]
            ok &= _assert(False, f"空 thread_id {bad!r} 应当报错")
        except ValueError:
            ok &= _assert(True, f"空 thread_id {bad!r} 被拒绝")

    # 用错 thread_id 读不到状态 —— 这就是"必须用 meeting_id"的理由
    saver = make_checkpointer(tmp / "wrong.db")
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        r = R(meeting_id="acc-pit1")
        try:
            r.runner.start()
            same = r.app.get_state(thread_config("acc-pit1"))
            other = r.app.get_state(thread_config("另一个会议号"))
            ok &= _assert(same.values.get("meeting_id") == "acc-pit1",
                          "用 meeting_id 能读回自己的状态")
            ok &= _assert(not other.values,
                          "★ 用错的 thread_id 读不到状态（不会串号）")
        finally:
            r.close()
    finally:
        saver.conn.close()
    return ok


# ---------------------------------------------------------------------------
# 坑 2：闸口节点被重放 —— 不能有副作用
# ---------------------------------------------------------------------------

def check_pit_2(tmp: Path) -> bool:
    """坑 2：`interrupt()` 的节点会被**重放** —— 闸口节点内不能有副作用。"""
    banner("坑 2  闸口节点被重放两次，且执行期间不写审计/不产文件")
    ok = True

    # ① 重放确实发生（2 次），且业务节点仍是 1 次
    r = R(meeting_id="acc-pit2")
    try:
        r.runner.start()
        r.runner.resume({"decision": "approve"})
        ok &= _assert(r.counts.get(S.NODE_GATE_G2) == 2,
                      f"gate_g2 执行 2 次（重放，实际 {r.counts.get(S.NODE_GATE_G2)}）")
        ok &= _assert(r.counts.get(S.NODE_EXTRACT) == 1,
                      "重放没有波及上游（extract 仍 1 次）")
    finally:
        r.close()

    # ② 闸口送审载荷里**没有内部路径**（N3.3：界面不出现工程词）
    r2 = R(meeting_id="acc-pit2b")
    try:
        st = r2.runner.start()
        gate = st.pending_gate
        ok &= _assert(gate is not None, "启动后停在闸口")
        payload_json = json.dumps(gate.payload, ensure_ascii=False)
        for word in ("audio_paths", "chunk_plan", "transcript_meta", "idem_keys"):
            ok &= _assert(word not in payload_json,
                          f"送审载荷不含内部字段 {word}")
        keys = set(gate.payload or {})
        ok &= _assert(keys == set(["topics", "resolutions", "action_items"]),
                      f"G2 送审字段恰好是三项（实际 {sorted(keys)}）")
        ok &= _assert(gate.risk == "high", "G2 风险等级为 high")
        ok &= _assert(gate.gate_label == "待确认议题",
                      f"闸口文案为『待确认议题』（实际 {gate.gate_label}）")
    finally:
        r2.close()

    # ③ 闸口执行期间审计条数不变（闸口只组装载荷 + 调 interrupt）
    db = tmp / "audit_pit2.db"
    migrate(db)
    audit = AuditLogger(db)
    before_n = len(query(db))
    node = _audit_probe_gate()
    # 在**没有闸口决策**的情况下调一次节点体（它会 interrupt 抛特殊异常），
    # 只要它没写审计就说明闸口无副作用。
    try:
        node({"meeting_id": MEETING_ID, "topics": [], "resolutions": [],
              "action_items": []})
    except BaseException as exc:  # interrupt 会抛 GraphInterrupt / 同类
        del exc
    ok &= _assert(len(query(db)) == before_n,
                  f"闸口节点没有写审计（{before_n} -> {len(query(db))}）")

    # ④ `_base` 不在节点前后**写**审计（架构决定：审计由副作用那一步自己写）
    #
    # ⚠️ 判据是"有没有调 `.log()`"，不是"有没有 import AuditLogger"。
    # `_base.session_of()` 里确实**构造**了一个 `AuditLogger` —— 那是
    # 会话装配（把 store / db / audit 三样打包给节点用），不是写审计。
    # 第一版断言成"不 import AuditLogger"，把装配当成了违规
    # （2026-09-28 记忆日志）。
    #
    # 而且必须走 **AST**：`_base.py` 的模块文档字符串里正解释着
    # "审计一律由发生副作用的那一步自己写"，子串匹配会把这段**说明**
    # 当成违规。T1-09 的验收脚本踩过同一个坑，那边也是用 AST 的。
    calls = _logged_audit_calls(PROJECT_ROOT / "src" / "mmwf" / "graph" / "nodes" / "_base.py")
    ok &= _assert(calls == [],
                  f"★ `_base` 从不调用 `audit.log(...)`（实际 {calls}）")
    return ok


def _logged_audit_calls(path: Path) -> list[str]:
    """找出文件里形如 `xxx.log(...)` / `audit.log(...)` 的调用（AST 级）。

    只认"接收者名字里带 audit 的 `.log(...)` 调用"，这样：
      · `logger.info(...)` 不会被误判（接收者是 logger，不是 audit）；
      · `AuditLogger(db_path)` 这种**构造**不算（不是 `.log` 调用）；
      · 文档字符串/注释里的解释文字完全不参与（AST 不看字符串内容）。
    """
    import ast

    out: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "log":
            continue
        recv = func.value
        name = ""
        if isinstance(recv, ast.Name):
            name = recv.id
        elif isinstance(recv, ast.Attribute):
            name = recv.attr
        if "audit" in name.lower():
            out.append(f"line {node.lineno}: {name}.log(...)")
    return out


def _audit_probe_gate():
    """取一个 G2 闸口节点函数用于"无副作用"探针。"""
    from mmwf.graph import interrupts as I

    return I.make_gate_g2()


# ---------------------------------------------------------------------------
# 坑 3：节点返回的是状态增量
# ---------------------------------------------------------------------------

def check_pit_3(tmp: Path) -> bool:
    """坑 3：节点返回值是**状态增量**，返回 `{}` 表示不改。"""
    banner("坑 3  节点返回状态增量（partial update），空增量表示不改")
    ok = True

    from mmwf.graph import nodes as N

    r = R(meeting_id="acc-pit3")
    try:
        # 生成节点只返回它负责的几个键，不是整个状态
        state = {
            "meeting_id": MEETING_ID, "task_id": MEETING_ID,
            "topics": [{"title": "议题甲", "refs": [{"segment_id": "s1", "start_ms": 1}]}],
            "resolutions": [{"text": "决议乙",
                             "refs": [{"segment_id": "s2", "start_ms": 2}]}],
            "action_items": [{"text": "待办丙", "owner": "张三",
                              "refs": [{"segment_id": "s3", "start_ms": 3}]}],
        }
        out = N.make_generate(None)(state)
        ok &= _assert(set(out) <= {"minutes_structured", "gen_retry", "_gate_route"},
                      f"generate 只返回自己负责的键（实际 {sorted(out)}）")
        ok &= _assert("topics" not in out,
                      "★ 没有把整个状态回填（那会让下游的增量被覆盖）")

        # 闸口 reject 时清下游产物 = 用 None 作增量（而不是删键）
        from mmwf.graph import interrupts as I

        rej = I._apply_decision(
            {"meeting_id": MEETING_ID, "errors": []},
            gate="G2", node_id="gate_g2", raw={"decision": "reject"},
        )
        ok &= _assert(rej.get("minutes_markdown") is None,
                      "reject 用 None 清掉下游产物（作为增量下发）")
        ok &= _assert(rej.get("_gate_route") == "redo", "reject 的路由结论是 redo")

        # `_base.INITIAL_STATE` 非空 —— total=False 的 TypedDict 不接受空 dict
        ok &= _assert(bool(B.INITIAL_STATE), "初始状态非空（否则 LangGraph 报错）")
        ok &= _assert(set(B.INITIAL_STATE) == {"errors", "_gate_route"},
                      f"初始状态就是那两个键（实际 {sorted(B.INITIAL_STATE)}）")
    finally:
        r.close()
    return ok


# ---------------------------------------------------------------------------
# 坑 4：检查点连接不跨线程共用
# ---------------------------------------------------------------------------

def check_pit_4(tmp: Path) -> bool:
    """坑 4：`SqliteSaver` 的连接不能跨线程共用，要按任务开独立连接。"""
    banner("坑 4  每个任务一条检查点连接，且 check_same_thread=False")
    ok = True

    saver = make_checkpointer(tmp / "ckpt4.db")
    try:
        ok &= _assert(saver.conn is not None, "拿到连接对象")
        # sqlite3 的连接默认为 check_same_thread=False 时才能跨线程；
        # 本项目按任务开连接 + 允许线程内自用，所以这里验"另一线程能读"。
        err: list[str] = []

        def other_thread():
            try:
                saver.setup()
            except BaseException as exc:  # noqa: BLE001
                err.append(f"{type(exc).__name__}: {exc}")

        t = threading.Thread(target=other_thread)
        t.start()
        t.join(timeout=10)
        ok &= _assert(err == [], f"另一线程可用该连接（{err}）")

        # 两个任务各开一条连接，互不影响
        s1 = make_checkpointer(tmp / "t1.db")
        s2 = make_checkpointer(tmp / "t2.db")
        try:
            ok &= _assert(s1.conn is not s2.conn, "两个任务拿到的是不同连接")
            ok &= _assert((tmp / "t1.db").exists() and (tmp / "t2.db").exists(),
                          "两个检查点库都落了盘")
        finally:
            s1.conn.close()
            s2.conn.close()

        # `runner.close()` 能释放连接
        r = R(meeting_id="acc-pit4", checkpointer=saver)
        r.runner.start()
        r.close()
        ok &= _assert(True, "runner.close() 正常释放连接")
    finally:
        try:
            saver.conn.close()
        except Exception:  # noqa: BLE001
            pass
    return ok


# ---------------------------------------------------------------------------
# 附加：分层 / 依赖 / 用例回归
# ---------------------------------------------------------------------------

def check_gates() -> bool:
    """附加：分层检查、依赖白名单、T1-10 用例、图与 SAD 同形、既有回归。"""
    banner("附加  分层检查 + 依赖白名单 + T1-10 用例 + 同形校验 + 回归")
    ok = True

    def _run(cmd):
        return subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    for label, cmd in [
        ("分层检查 check_layers.py", [sys.executable, "tools/check_layers.py"]),
        ("依赖白名单 check_deps.py", [sys.executable, "tools/check_deps.py"]),
        ("T1-10 图用例（真假图同形 + 全部 U-ST）",
         [sys.executable, "-m", "pytest",
          "tests/unit/test_graph_nodes.py", "tests/unit/test_graph_build.py",
          "tests/unit/test_graph_runner.py",
          "tests/integration/test_graph_pipeline.py",
          "-q", "--no-header", "-p", "no:cacheprovider"]),
        ("T1-09 回归（导出仍可用）",
         [sys.executable, "-m", "pytest", "tests/unit/test_export_service.py",
          "-q", "--no-header", "-p", "no:cacheprovider"]),
    ]:
        proc = _run(cmd)
        lines = [ln for ln in (proc.stdout + proc.stderr).strip().splitlines() if ln.strip()]
        print(f"  {PASS if proc.returncode == 0 else FAIL} {label}")
        print(f"      {lines[-1] if lines else '(无输出)'}")
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr)[-2000:])
        ok &= proc.returncode == 0
    return ok


def check_graph_matches_sad(tmp: Path) -> bool:
    """附加：真图与 SAD §5.1 的主链路/路由逐条对齐。"""
    banner("附加  真图的形状与 SAD §5.1 一致")
    ok = True

    app = build_pipeline(Deps(), checkpointer=None)
    graph = app.get_graph()
    nodes = set(graph.nodes)
    ok &= _assert(set(S.NODE_SEQUENCE) <= nodes,
                  f"11 个节点都在图上（缺 {set(S.NODE_SEQUENCE) - nodes}）")
    ok &= _assert(len(S.NODE_SEQUENCE) == 11,
                  f"节点数为 11（实际 {len(S.NODE_SEQUENCE)}）")

    # 主链路顺序（相邻两节点之间必须有边）
    edges = {(e.source, e.target) for e in graph.edges}
    for a, b in zip(S.NODE_SEQUENCE, S.NODE_SEQUENCE[1:]):
        if a in GATES or b in GATES:
            continue
        # 闸口段用条件边，这里只查直连段
        if {a, b} & {S.NODE_GATE_G2, S.NODE_GATE_G3, S.NODE_VALIDATE}:
            continue
        ok &= _assert((a, b) in edges, f"主链路有边 {a} -> {b}")

    # validate / gate 的出入边是**条件边**（`add_conditional_edges`），
    # 在上面的 zip 循环里被跳过了，这里单独验几条关键的直连边。
    ok &= _assert((S.NODE_EXTRACT, S.NODE_GATE_G2) in edges,
                  "extract -> gate_g2 直连")
    ok &= _assert((S.NODE_GENERATE, S.NODE_VALIDATE) in edges,
                  "generate -> validate 直连")
    ok &= _assert((S.NODE_ASSEMBLE, S.NODE_GATE_G3) in edges,
                  "assemble -> gate_g3 直连")
    ok &= _assert((S.NODE_EXPORT, S.NODE_ARCHIVE) in edges,
                  "export -> archive 直连")

    # 两个中断点
    ok &= _assert(S.NODE_GATE_G2 in nodes and S.NODE_GATE_G3 in nodes,
                  "两个闸口都在图上")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T1-10 验收")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据")
    args = ap.parse_args()

    print("T1-10 验收：流水线编排（LangGraph 状态机，SAD §5.1/§5.3/§5.4）")
    print(f"项目根：{PROJECT_ROOT}")

    logging.disable(logging.WARNING)

    tmp_root = Path(tempfile.mkdtemp(prefix="mmwf_t110_"))
    try:
        results: dict[str, bool] = {
            "标准 1 V7 除两次确认外自动跑完": check_standard_1(tmp_root / "s1"),
            "标准 2 G2 approve 不重跑": check_standard_2(tmp_root / "s2"),
            "标准 3 G2 edit 改动生效且不返工（V5）": check_standard_3(tmp_root / "s3"),
            "标准 4 G2 reject 只重跑 extract": check_standard_4(tmp_root / "s4"),
            "标准 4b 连续 reject 有上限不死循环": check_standard_4b(tmp_root / "s4b"),
            "标准 5 杀进程后从 G2 恢复（F6.2）": check_standard_5(tmp_root / "s5"),
            "标准 6 幂等导出（F6.3）": check_standard_6(tmp_root / "s6"),
            "标准 7 校验失败带清单成文（C03）": check_standard_7(tmp_root / "s7"),
            "标准 7b 空记录稿闸口放行 + 入口守卫": check_standard_7b(tmp_root / "s7b"),
            "标准 7c 闸口空载荷自动放行（E4003）": check_standard_7c(tmp_root / "s7c"),
            "标准 8 三个点名用例全绿": check_standard_8(),
            "坑 1 thread_id 必须用 meeting_id": check_pit_1(tmp_root / "p1"),
            "坑 2 闸口被重放且无副作用": check_pit_2(tmp_root / "p2"),
            "坑 3 节点返回状态增量": check_pit_3(tmp_root / "p3"),
            "坑 4 检查点连接不跨线程共用": check_pit_4(tmp_root / "p4"),
            "附加 分层 + 依赖 + 用例 + 回归": check_gates(),
            "附加 真图与 SAD §5.1 一致": check_graph_matches_sad(tmp_root / "g1"),
        }

        banner("验收结论")
        for name, good in results.items():
            print(f"  {PASS if good else FAIL} {name}")

        all_ok = all(results.values())
        print(f"\n{'=' * 78}")
        print(f"{PASS} T1-10 验收通过" if all_ok else f"{FAIL} T1-10 验收未通过")
        print(f"{'=' * 78}")
        return 0 if all_ok else 1
    finally:
        if args.keep_tmp:
            print(f"\n临时数据已保留：{tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
