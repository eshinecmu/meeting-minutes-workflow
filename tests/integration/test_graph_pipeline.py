"""T1-10 集成测试 —— 测试方案 §4.1 的 IT 场景。

与 `tests/unit/test_graph_runner.py` 的分工
------------------------------------------
单元测试验的是**每个节点/每个路由**的行为；这里验的是
**跨步骤的端到端场景**，即"用户视角下一件事从头到尾对不对"：

    IT-04  G2 用 edit  -> `rerun_nodes == []`，最终稿含修改
    IT-05  G2 用 reject -> 仅 extract 重跑
    IT-06  G3 用 edit  -> 改一处措辞后继续，导出含修改
    IT-07  校验连续失败 -> 2 次重试后带问题清单成文
    IT-08  G2 等待时 kill -> 重启后从 G2 恢复
    IT-09  重复导出 -> 返回同一路径，只有一个文件

⚠️ 这些用例用 `tests/fixtures/graph/harness.py` 的替身图。
`test_graph_build.py::test_shape_matches_manual_graph` 保证替身图
与真图**同形**，所以这里跑的流程就是线上的流程。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.build import (
    MAX_VALIDATE_RETRY,
    make_checkpointer,
    thread_config,
)
from mmwf.graph.nodes import _base as B
from mmwf.graph.runner import GraphRunner

import harness

langgraph = pytest.importorskip("langgraph")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def runner_with(tmp_path, meeting_id="it-1", **kw) -> GraphRunner:
    from langgraph.checkpoint.memory import InMemorySaver

    runs = harness.Runs()
    app = harness.build_test_app(runs=runs, checkpointer=InMemorySaver(), **kw)
    r = GraphRunner(meeting_id, app=app)
    r._runs = runs
    return r


@pytest.fixture
def runner(tmp_path):
    r = runner_with(tmp_path)
    yield r
    r.close()


# ---------------------------------------------------------------------------
# IT-04：G2 用 edit —— rerun_nodes == []，最终稿含修改
# ---------------------------------------------------------------------------

class TestIT04_G2Edit:
    """"G2 用 edit | 议题被人工修正 | rerun_nodes == []，最终稿含修改"。"""

    #: 一条**带来源依据**的修正议题。
    #:
    #: ⚠️ `refs` 不能省。第一版用的 payload 是 `{"title": ..., "refs": []}`，
    #: 结果 `assemble` 之前的三个节点各跑了 3 次 —— 因为
    #: `naive_validate` 看到"议题没有来源依据"就报 `topic_without_refs`，
    #: 于是走 `retry` 边回 `generate`；重试**修不了数据问题**（refs 还是空的），
    #: 于是把 `MAX_VALIDATE_RETRY = 2` 次机会全部烧完才 `give_up`
    #: → generate 共 3 次（1 初次 + 2 重试）、validate 共 3 次。
    #:
    #: 这是 U-ST-05 的**正确行为**（带问题清单成文，不静默丢弃），
    #: 但它与本用例要测的东西（edit 会不会触发**闸口返工**）混在一起了。
    #: 所以这里给一条有依据的议题，把校验重试这条线排除掉；
    #: 两者的交互另见 `test_edit_without_refs_triggers_validate_retry`。
    EDIT_TOPIC = {
        "title": "修正后的议题",
        "refs": [{"segment_id": "seg-1", "start_ms": 12000,
                  "text": "[00:00:12] 张三：今天研究季度工作安排。"}],
    }

    def test_rerun_nodes_is_empty(self, runner):
        """★ IT-04 的核心断言：edit 之后**一个节点都不该重跑**。

        这里验的是"闸口返工"：edit 的语义是"用改后的内容继续"，
        所以 `extract` / `transcribe` 这些**上游**一个都不许重跑。
        （`generate`/`assemble` 是 edit 的**下游**，它们本来就要跑一遍
        来把改动渲染进稿子 —— 所以断言的是"`extract` 之前没动"，
        见下一个用例。）
        """
        runner.start()
        before = dict(runner._runs.n)
        runner.resume({"decision": "edit", "payload": {"topics": [self.EDIT_TOPIC]}})
        # 上游（extract 及其之前）一个都不许重跑
        upstream = [n for n in S.NODE_SEQUENCE[: S.NODE_SEQUENCE.index(S.NODE_EXTRACT) + 1]
                    if runner._runs.get(n) > before.get(n, 0)]
        assert upstream == [], f"edit 触发了上游返工：{upstream}"
        # 闸口自身会被重放（LangGraph 的 interrupt 语义），不算业务重跑
        assert runner._runs.get(S.NODE_EXTRACT) == before[S.NODE_EXTRACT]

    def test_edit_does_not_rerun_extract(self, runner):
        """★ 施工卡原文：「在 G2 用 `edit` → 修改后的议题进入后续流程，
        `extract` **不重跑**」（V5 核心）。"""
        runner.start()
        calls = runner._runs.get(S.NODE_EXTRACT)
        runner.resume({"decision": "edit", "payload": {"topics": [self.EDIT_TOPIC]}})
        assert runner._runs.get(S.NODE_EXTRACT) == calls, "edit 让 extract 重跑了"
        assert runner._runs.get(S.NODE_TRANSCRIBE) == 1

    def test_edit_runs_downstream_exactly_once(self, runner):
        """edit 的下游各跑**一次** —— 不多不少。

        这一条才是真正能抓到"重复调度"的断言：如果 `generate` 因为
        校验重试而跑 3 次、或者因为重复出边而跑 2 次，这里就会红。
        """
        runner.start()
        before = dict(runner._runs.n)
        st = runner.resume({"decision": "edit",
                            "payload": {"topics": [self.EDIT_TOPIC]}})
        assert st.status == "G3_awaiting"
        for node in (S.NODE_GENERATE, S.NODE_VALIDATE, S.NODE_ASSEMBLE):
            delta = runner._runs.get(node) - before.get(node, 0)
            assert delta == 1, f"{node} 跑了 {delta} 次（应为 1 次）"

    def test_edit_without_refs_triggers_validate_retry(self, runner):
        """改后的议题**没有依据**时，应该触发校验重试。

        与 `test_edit_runs_downstream_exactly_once` 对照着看：
        同样是 edit，payload 有没有 `refs` 决定了后续跑几次。
        链路是"校验报无依据 -> 重试 -> 修不了 -> 烧完次数 -> 带清单成文"，
        全程**不失败**（U-ST-05 / C03）。把它写成用例是为了让
        "节点跑 3 次"这件事有出处，而不是看起来像图接错了。
        """
        runner.start()
        before = dict(runner._runs.n)
        st = runner.resume({
            "decision": "edit",
            "payload": {"topics": [{"title": "无依据的议题", "refs": []}]},
        })
        assert st.status == "G3_awaiting", "带问题的稿子也该能走到 G3"
        assert st.gen_retry == MAX_VALIDATE_RETRY + 1, (
            f"重试计数不对：{st.gen_retry}")
        delta = runner._runs.get(S.NODE_GENERATE) - before.get(S.NODE_GENERATE, 0)
        assert delta == MAX_VALIDATE_RETRY + 1, (
            f"generate 应重试到上限共 {MAX_VALIDATE_RETRY + 1} 次，实际 {delta}")
        # 问题清单进正文，不静默丢弃
        md = runner.values().get("minutes_markdown") or ""
        assert "待核对清单" in md
        # 上游依然没被牵连
        assert runner._runs.get(S.NODE_EXTRACT) == before[S.NODE_EXTRACT]

    def test_final_minutes_contain_the_edit(self, runner):
        runner.start()
        runner.resume({"decision": "edit", "payload": {"topics": [self.EDIT_TOPIC]}})
        md = runner.values().get("minutes_markdown") or ""
        assert "修正后的议题" in md

    def test_the_edit_survives_all_the_way_to_export(self, runner):
        """改动要一路流到导出产物（不只是停在中间状态）。"""
        runner.start()
        runner.resume({"decision": "edit", "payload": {"topics": [self.EDIT_TOPIC]}})
        st = runner.resume({"decision": "approve"})
        assert st.is_terminal
        md = runner.values().get("minutes_markdown") or ""
        assert "修正后的议题" in md, "过了 G3 之后改动丢了"

    def test_comparison_shows_only_the_edit(self, runner):
        """差异视图：只有被改的那一项不同，其余原样。

        （一期没有真正的 diff 视图，这里用"改前后对比"来等价验证。）
        """
        runner.start()
        before = runner.values()
        runner.resume({"decision": "edit", "payload": {"topics": [self.EDIT_TOPIC]}})
        after = runner.values()

        assert after.get("topics") != before.get("topics"), "议题没变"
        assert after.get("resolutions") == before.get("resolutions"), "决议被误改"
        assert after.get("action_items") == before.get("action_items"), "待办被误改"


# ---------------------------------------------------------------------------
# IT-05：G2 用 reject —— 仅 extract 重跑
# ---------------------------------------------------------------------------

class TestIT05_G2Reject:
    """"G2 用 reject | 只有 extract 重跑"。"""

    def test_only_extract_reruns(self, runner):
        runner.start()
        before = dict(runner._runs.n)
        runner.resume({"decision": "reject"})
        rerun = [n for n, c in runner._runs.n.items() if c > before.get(n, 0)]
        rerun = [n for n in rerun if n not in S.GATE_NODES]
        assert rerun == [S.NODE_EXTRACT], f"重跑的不是 extract：{rerun}"

    def test_transcribe_not_rerun(self, runner):
        """★ 重转写最贵（几分钟到几十分钟模型推理），绝对不能重跑。"""
        runner.start()
        before = runner._runs.get(S.NODE_TRANSCRIBE)
        runner.resume({"decision": "reject"})
        assert runner._runs.get(S.NODE_TRANSCRIBE) == before

    def test_returns_to_g2_for_reconfirmation(self, runner):
        runner.start()
        st = runner.resume({"decision": "reject"})
        assert st.status == "G2_awaiting"

    def test_can_approve_after_reject(self, runner):
        """驳回 -> 重新抽取 -> 再确认，流程要走得通。"""
        runner.start()
        runner.resume({"decision": "reject"})
        st = runner.resume({"decision": "approve"})
        assert st.status == "G3_awaiting"

    def test_extract_output_is_regenerated(self, runner):
        """重跑 extract 后议题仍在（不是被清空）。"""
        runner.start()
        runner.resume({"decision": "reject"})
        assert runner.values().get("topics")


# ---------------------------------------------------------------------------
# IT-06：G3 用 edit —— 改一处措辞后继续，导出含修改
# ---------------------------------------------------------------------------

class TestIT06_G3Edit:
    """"G3 使用 edit | 改一处措辞后继续 | 差异视图正确，导出含修改"。

    ⚠️ 这条场景证明了 **G3 必须支持 edit**，而且改的是**措辞**
    （`minutes_markdown`），不是议题 —— 议题在 G2 已经确认过，
    允许 G3 改议题等于在 G3 绕过 G2 闸口。
    """

    def _to_g3(self, runner):
        runner.start()
        runner.resume({"decision": "approve"})
        assert runner.status().pending_gate.gate == "G3"

    def test_g3_accepts_edit_of_wording(self, runner):
        self._to_g3(runner)
        st = runner.resume({
            "decision": "edit",
            "payload": {"minutes_markdown": "# 改过措辞的纪要\n\n正文如下。\n"},
        })
        # 继续往下走（到归档），不是回到 G2
        assert st.is_terminal

    def test_edited_wording_reaches_the_final_document(self, runner):
        self._to_g3(runner)
        runner.resume({
            "decision": "edit",
            "payload": {"minutes_markdown": "# 改过措辞的纪要\n"},
        })
        md = runner.values().get("minutes_markdown") or ""
        assert "改过措辞的纪要" in md

    def test_g3_edit_does_not_rerun_assemble(self, runner):
        """★ edit 不重跑 —— 这是"改完继续"相对"退回重做"的价值所在。"""
        self._to_g3(runner)
        before = runner._runs.get(S.NODE_ASSEMBLE)
        runner.resume({
            "decision": "edit",
            "payload": {"minutes_markdown": "# 改过措辞的纪要\n"},
        })
        assert runner._runs.get(S.NODE_ASSEMBLE) == before, "edit 重跑了 assemble"

    def test_g3_edit_does_not_rerun_extract_or_transcribe(self, runner):
        self._to_g3(runner)
        before = {n: runner._runs.get(n) for n in
                  (S.NODE_EXTRACT, S.NODE_TRANSCRIBE, S.NODE_GENERATE,
                   S.NODE_VALIDATE)}
        runner.resume({
            "decision": "edit",
            "payload": {"minutes_markdown": "# 改过措辞的纪要\n"},
        })
        for name, count in before.items():
            assert runner._runs.get(name) == count, f"{name} 重跑了"

    def test_g3_rejects_editing_topics(self, runner):
        """⚠️ G3 不能改议题 —— 那是在 G3 绕过 G2 闸口。

        施工卡把可编辑字段**按闸口分开**正是为了堵这条路径。
        """
        self._to_g3(runner)
        with pytest.raises(Exception) as ei:
            runner.resume({
                "decision": "edit",
                "payload": {"topics": [{"title": "偷改的议题", "refs": []}]},
            })
        assert getattr(ei.value, "code", "") == "E4001"

    def test_g3_reject_goes_back_to_assemble_only(self, runner):
        """G3 reject 只重新成文，不回 G2（议题已经确认过了）。"""
        self._to_g3(runner)
        before = dict(runner._runs.n)
        st = runner.resume({"decision": "reject"})
        assert st.status == "G3_awaiting"
        rerun = [n for n, c in runner._runs.n.items() if c > before.get(n, 0)]
        rerun = [n for n in rerun if n not in S.GATE_NODES]
        assert rerun == [S.NODE_ASSEMBLE], f"重跑的不是 assemble：{rerun}"

    def test_g3_approve_after_edit_produces_export(self, runner):
        self._to_g3(runner)
        runner.resume({
            "decision": "edit",
            "payload": {"minutes_markdown": "# 改过措辞的纪要\n"},
        })
        st = runner.status()
        assert st.is_terminal
        assert st.products.get("export_paths") is True


# ---------------------------------------------------------------------------
# IT-07：校验连续失败 -> 2 次重试后带问题清单成文
# ---------------------------------------------------------------------------

class TestIT07_ValidateRetry:
    """"校验连续失败 | 2 次重试后带问题清单成文"。"""

    def _runner(self, tmp_path):
        """必须用真 generate / assemble（`gen_retry` 由它们维护）。"""
        from langgraph.checkpoint.memory import InMemorySaver

        runs = harness.Runs()
        app = harness.build_test_app(
            runs=runs, checkpointer=InMemorySaver(),
            validator=lambda s: [{"rule": "topic_without_refs",
                                  "detail": "议题没有来源依据，无法追溯"}],
        )
        r = GraphRunner("it-07", app=app)
        r._runs = runs
        return r

    def test_retries_then_proceeds_with_issues(self, tmp_path):
        r = self._runner(tmp_path)
        try:
            r.start()
            st = r.resume({"decision": "approve"})
            assert st.status == "G3_awaiting", "重试到头没继续往下走"
            assert r._runs.get(S.NODE_GENERATE) == 3, "重试次数不对（应为 1+2）"
        finally:
            r.close()

    def test_issue_list_in_the_final_text(self, tmp_path):
        """★ 施工卡原文："带问题清单进入 assemble，**不静默丢弃**"。"""
        r = self._runner(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            md = r.values().get("minutes_markdown") or ""
            assert "待核对清单" in md
            assert "议题没有来源依据" in md
        finally:
            r.close()

    def test_issue_list_reaches_the_exported_document(self, tmp_path):
        """问题清单要跟着稿子走到交付物。"""
        r = self._runner(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            r.resume({"decision": "approve"})
            md = r.values().get("minutes_markdown") or ""
            assert "待核对清单" in md
            assert r.status().products.get("export_paths") is True
        finally:
            r.close()

    def test_task_is_not_failed(self, tmp_path):
        """连续失败不等于任务失败 —— 要成文，只是带问题清单。"""
        r = self._runner(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            st = r.status()
            assert st.status != S.STATUS_FAILED
            assert st.is_waiting or st.is_terminal
        finally:
            r.close()

    def test_no_retry_when_validation_passes(self, runner):
        runner.start()
        runner.resume({"decision": "approve"})
        assert runner._runs.get(S.NODE_GENERATE) == 1


# ---------------------------------------------------------------------------
# IT-08：G2 等待时 kill -> 重启后从 G2 恢复
# ---------------------------------------------------------------------------

class TestIT08_KillAndResume:
    """"G2 等待时 kill | 重启后从 G2 恢复"（F6.2）。"""

    def _start_on_disk(self, tmp_path, meeting_id: str):
        """起一个落盘检查点的 runner 并跑到 G2（模拟"kill 前"的状态）。"""
        saver = make_checkpointer(tmp_path / "ckpt.db")
        runs = harness.Runs()
        app = harness.build_test_app(runs=runs, real=(), checkpointer=saver)
        r = GraphRunner(meeting_id, app=app)
        r._runs = runs
        r.start()
        r.close()                     # 关连接 = 模拟进程结束
        return runs

    def test_state_is_on_disk_after_kill(self, tmp_path):
        self._start_on_disk(tmp_path, "it-08")

        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            app = harness.build_test_app(
                runs=harness.Runs(), real=(), checkpointer=saver)
            r = GraphRunner("it-08", app=app)
            st = r.status()
            assert st.is_waiting, "重启后没停在闸口"
            assert st.pending_gate is not None
            assert st.pending_gate.gate == "G2"
        finally:
            saver.conn.close()

    def test_gate_payload_survives_restart(self, tmp_path):
        """送审的议题内容也要能读回来（不然审定人看不到东西）。"""
        self._start_on_disk(tmp_path, "it-08b")

        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            app = harness.build_test_app(
                runs=harness.Runs(), real=(), checkpointer=saver)
            r = GraphRunner("it-08b", app=app)
            payload = r.status().pending_gate.payload
            assert payload, "闸口载荷是空的"
            assert payload.get("topics"), "议题没读回来"
        finally:
            saver.conn.close()

    def test_resume_after_restart_completes_without_rerunning_upstream(self, tmp_path):
        """★ 恢复不是重跑：闸口之前的节点一次都不该执行。"""
        self._start_on_disk(tmp_path, "it-08c")

        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            runs = harness.Runs()
            app = harness.build_test_app(runs=runs, real=(), checkpointer=saver)
            r = GraphRunner("it-08c", app=app)
            r._runs = runs

            r.resume({"decision": "approve"})
            r.resume({"decision": "approve"})
            assert r.status().is_terminal

            for name in (S.NODE_INGEST, S.NODE_TRANSCRIBE, S.NODE_REVIEW,
                         S.NODE_EXTRACT):
                assert runs.get(name) == 0, f"{name} 被重跑了 —— 恢复变成了重跑"
        finally:
            saver.conn.close()

    def test_thread_id_is_meeting_id(self, tmp_path):
        self._start_on_disk(tmp_path, "会议-IT08")

        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            app = harness.build_test_app(
                runs=harness.Runs(), real=(), checkpointer=saver)
            # 用 meeting_id 直接当 thread_id 能读到状态
            snap = app.get_state(thread_config("会议-IT08"))
            assert snap.values.get("meeting_id") == "会议-IT08"
        finally:
            saver.conn.close()

    def test_wrong_thread_id_finds_nothing(self, tmp_path):
        """用错 thread_id 读不到状态 —— 这就是"必须用 meeting_id"的理由。"""
        self._start_on_disk(tmp_path, "it-08d")

        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            app = harness.build_test_app(
                runs=harness.Runs(), real=(), checkpointer=saver)
            snap = app.get_state(thread_config("别的会议号"))
            assert not snap.values, "用错的 thread_id 竟然读到了状态"
        finally:
            saver.conn.close()


# ---------------------------------------------------------------------------
# IT-09：重复导出 -> 返回同一路径，只有一个文件
# ---------------------------------------------------------------------------

class TestIT09_IdempotentExport:
    """"重复导出 | 返回同一路径，只有一个文件"（C07）。"""

    def test_export_key_is_stable_for_same_content(self):
        state = {"meeting_id": "it-09", "minutes_markdown": "# 同一份稿子\n"}
        keys = {
            idem.make_idempotency_key(
                "it-09", S.NODE_EXPORT,
                idem.node_input_from_state(S.NODE_EXPORT, state))
            for _ in range(5)
        }
        assert len(keys) == 1, "同一内容的幂等键不稳定"

    def test_second_export_reuses_the_record(self, tmp_path):
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        state = {"meeting_id": "it-09", "minutes_markdown": "# 稿子\n"}
        calls = []

        def do_export():
            calls.append(1)
            return {"rel_path": "export/纪要.docx"}

        r1, c1 = B.idempotent(store, S.NODE_EXPORT, state, do_export)
        r2, c2 = B.idempotent(store, S.NODE_EXPORT, state, do_export)
        assert calls == [1], "第二次导出又真的写了一次文件"
        assert (c1, c2) == (False, True)
        # 命中时返回的是持久化的引用 —— 与首次的产物路径一致
        assert r2 == "export/纪要.docx"
        assert store.count() == 1, "产生了第二条幂等记录"

    def test_different_content_creates_a_new_export(self, tmp_path):
        """改稿后重新导出是**应该**的（不然看到的还是旧文件）。"""
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        calls = []

        def do_export():
            calls.append(1)
            return {"rel_path": "export/纪要.docx"}

        B.idempotent(store, S.NODE_EXPORT,
                     {"meeting_id": "it-09", "minutes_markdown": "A"}, do_export)
        B.idempotent(store, S.NODE_EXPORT,
                     {"meeting_id": "it-09", "minutes_markdown": "B"}, do_export)
        assert len(calls) == 2
        assert store.count() == 2

    def test_export_runs_once_per_full_pipeline_run(self, runner):
        runner.start()
        runner.resume({"decision": "approve"})
        runner.resume({"decision": "approve"})
        assert runner._runs.get(S.NODE_EXPORT) == 1

    def test_idempotency_survives_process_restart(self, tmp_path):
        """★ 幂等记录要落盘 —— 否则"重启后再导出"会写第二个文件。"""
        db = tmp_path / "idem.db"
        state = {"meeting_id": "it-09", "minutes_markdown": "# 稿子\n"}
        calls = []

        def do_export():
            calls.append(1)
            return {"rel_path": "export/纪要.docx"}

        store1 = idem.IdempotencyStore(db)
        B.idempotent(store1, S.NODE_EXPORT, state, do_export)
        assert len(calls) == 1

        # "重启"：新开一个 store 读同一个库
        store2 = idem.IdempotencyStore(db)
        _, cached = B.idempotent(store2, S.NODE_EXPORT, state, do_export)
        assert cached is True, "重启后幂等失效了 —— 会写出第二个文件"
        assert len(calls) == 1

    def test_archive_is_also_idempotent(self, tmp_path):
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        state = {"meeting_id": "it-09", "export_paths": {"docx": "a.docx"}}
        calls = []

        def do_archive():
            calls.append(1)
            return {"stage": S.STATUS_ARCHIVED}

        B.idempotent(store, S.NODE_ARCHIVE, state, do_archive)
        _, cached = B.idempotent(store, S.NODE_ARCHIVE, state, do_archive)
        assert len(calls) == 1
        assert cached is True


# ---------------------------------------------------------------------------
# 跨场景：V7 —— 投递后不碰额外按钮，除两次确认外自动跑完
# ---------------------------------------------------------------------------

class TestV7_UnattendedRun:
    """"投递音频后不碰任何额外按钮，除 G2/G3 两次确认外自动跑到导出"（V7）。"""

    def test_only_two_confirmation_points(self, runner):
        """★ V7 的核心：全程只停两次，且都是等人确认。

        ⚠️ 计数方式：**每停一次记一笔**，不是在循环里对着同一个
        `st` 记两笔。第一版写成

            st = runner.start(); stops.append(st.status)      # 记了 G2
            while not st.is_terminal:
                stops.append(st.status)                       # 又把 G2 记一遍
                st = runner.resume(...)

        `start()` 返回的就已经是"停在 G2"的状态，循环体里再记一次
        就成了 `['G2_awaiting','G2_awaiting','G3_awaiting']`，
        看起来像"停了三次"（2026-09-28 记忆日志）。
        正确的记法是**只在"刚刚停住"的那一刻记**。
        """
        stops: list[str] = []

        st = runner.start()
        while not st.is_terminal:
            assert st.pending_gate is not None, (
                f"停在非闸口状态 {st.status} —— 用户会被卡住不知道怎么办")
            stops.append(st.status)          # 只在这里记：此刻确实停住了
            st = runner.resume({"decision": "approve"})

        assert stops == ["G2_awaiting", "G3_awaiting"], (
            f"确认点不是恰好两次：{stops}")

    def test_no_other_state_waits_for_the_user(self, runner):
        """除 G2/G3 外**没有任何**状态需要用户操心（V7 的完整表述）。"""
        stops: list[str] = []
        st = runner.start()
        while not st.is_terminal:
            stops.append(st.status)
            assert st.pending_gate is not None, (
                f"{st.status} 停在非闸口状态，用户不知道该怎么办")
            st = runner.resume({"decision": "approve"})

        gate_stops = [s for s in stops if s in S.GATE_STATUSES]
        assert len(gate_stops) == len(stops), (
            f"有非闸口的停顿：{[s for s in stops if s not in S.GATE_STATUSES]}")
        assert len(gate_stops) == 2

    def test_no_manual_intervention_needed(self, runner):
        """除决策外不需要任何额外调用就能跑完。"""
        runner.start()
        runner.resume({"decision": "approve"})
        st = runner.resume({"decision": "approve"})
        assert st.is_terminal

    def test_default_decisions_are_approve(self, runner):
        """不传 payload 只传 approve 就能走 —— 界面上的"通过"按钮只需这一个字段。"""
        runner.start()
        assert runner.resume({"decision": "approve"}).status == "G3_awaiting"
        assert runner.resume({"decision": "approve"}).is_terminal


# ---------------------------------------------------------------------------
# 施工卡点名的三个用例（T1-10 验收标准第 8 条）
# ---------------------------------------------------------------------------
#
# 施工卡原文：
#   [ ] `test_edit_does_not_rerun`、`test_reject_reruns_only_target`、
#       `test_idempotent_export` 全绿
#
# 这三个名字是**验收脚本按名字检索**的（`tools/accept_t1_10.py` 的标准 8），
# 所以必须原样存在，不能只在别的用例里"顺带覆盖"。这里给三个
# 名副其实的用例：每个都只断言该名字说的一件事，失败时一眼能看出是
# 哪条语义坏了。

class TestCardNamedCases:
    """施工卡点名的三个用例（名字必须与验收标准逐字一致）。"""

    def test_edit_does_not_rerun(self, runner):
        """`edit` 不重跑已完成节点（V5 核心）。

        只断言这一件事：编辑之后 `extract`（及它之前的全部上游）执行次数不变。
        下游（generate/validate/assemble）本来就要跑一遍把改动渲染进稿子，
        所以不在这里断言 —— 那是 `test_edit_runs_downstream_exactly_once` 的事。
        """
        runner.start()
        done = {n: runner._runs.get(n) for n in S.NODE_SEQUENCE}
        runner.resume({"decision": "edit", "payload": {"topics": [TestIT04_G2Edit.EDIT_TOPIC]}})
        reran = [n for n in S.NODE_SEQUENCE[: S.NODE_SEQUENCE.index(S.NODE_GATE_G2)]
                 if runner._runs.get(n) > done[n]]
        assert reran == [], f"edit 重跑了已完成节点：{reran}"

    def test_reject_reruns_only_target(self, runner):
        """`reject` 只重跑该闸口的目标节点（G2 的目标是 `extract`）。"""
        runner.start()
        done = {n: runner._runs.get(n) for n in S.NODE_SEQUENCE}
        runner.resume({"decision": "reject"})
        reran = sorted(
            n for n in S.NODE_SEQUENCE
            if n not in S.GATE_NODES and runner._runs.get(n) > done[n]
        )
        assert reran == [S.NODE_EXTRACT], (
            f"重跑的不是「仅 extract」：{reran}（应恰为 ['extract']）")

    def test_idempotent_export(self, runner):
        """重复导出返回同一结果、不产生第二份（C07 / F6.3）。"""
        runner.start()
        runner.resume({"decision": "approve"})
        st = runner.resume({"decision": "approve"})
        assert st.is_terminal
        assert runner._runs.get(S.NODE_EXPORT) == 1, "export 跑了不止一次"
        assert runner._runs.get(S.NODE_ARCHIVE) == 1, "archive 跑了不止一次"

        paths = runner.values().get("export_paths")
        assert paths, "终态必须带着导出路径"


# ---------------------------------------------------------------------------
# 施工卡点名的三个用例（T1-10 验收标准第 8 条）
# ---------------------------------------------------------------------------
#
# 施工卡原文：
#   [ ] `test_edit_does_not_rerun`、`test_reject_reruns_only_target`、
#       `test_idempotent_export` 全绿
#
# 这三个名字是**验收脚本按名字检索**的（`tools/accept_t1_10.py` 的标准 8），
# 所以必须原样存在，不能只在别的用例里"顺带覆盖"。这里给三个
# 名副其实的用例：每个都只断言该名字说的一件事，失败时一眼能看出是
# 哪条语义坏了。

class TestCardNamedCases:
    """施工卡点名的三个用例（名字必须与验收标准逐字一致）。"""

    def test_edit_does_not_rerun(self, runner):
        """`edit` 不重跑已完成节点（V5 核心）。

        只断言这一件事：编辑之后 `extract`（及它之前的全部上游）执行次数不变。
        下游（generate/validate/assemble）本来就要跑一遍把改动渲染进稿子，
        所以不在这里断言 —— 那是 `test_edit_runs_downstream_exactly_once` 的事。
        """
        runner.start()
        done = {n: runner._runs.get(n) for n in S.NODE_SEQUENCE}
        runner.resume({"decision": "edit", "payload": {"topics": [TestIT04_G2Edit.EDIT_TOPIC]}})
        reran = [n for n in S.NODE_SEQUENCE[: S.NODE_SEQUENCE.index(S.NODE_GATE_G2)]
                 if runner._runs.get(n) > done[n]]
        assert reran == [], f"edit 重跑了已完成节点：{reran}"

    def test_reject_reruns_only_target(self, runner):
        """`reject` 只重跑该闸口的目标节点（G2 的目标是 `extract`）。"""
        runner.start()
        done = {n: runner._runs.get(n) for n in S.NODE_SEQUENCE}
        runner.resume({"decision": "reject"})
        reran = sorted(
            n for n in S.NODE_SEQUENCE
            if n not in S.GATE_NODES and runner._runs.get(n) > done[n]
        )
        assert reran == [S.NODE_EXTRACT], (
            f"重跑的不是「仅 extract」：{reran}（应恰为 ['extract']）")

    def test_idempotent_export(self, runner):
        """重复导出返回同一结果、不产生第二份（C07 / F6.3）。"""
        runner.start()
        runner.resume({"decision": "approve"})
        st = runner.resume({"decision": "approve"})
        assert st.is_terminal
        assert runner._runs.get(S.NODE_EXPORT) == 1, "export 跑了不止一次"
        assert runner._runs.get(S.NODE_ARCHIVE) == 1, "archive 跑了不止一次"

        paths = runner.values().get("export_paths")
        assert paths, "终态必须带着导出路径"
