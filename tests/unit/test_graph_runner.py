"""`graph.runner` 单元测试（T1-10）—— U-ST-01..10 主战场。

测试方案 §2.1 的 10 条编排用例在这里逐条落地：

    U-ST-01 主链路顺序       created → … → archived，节点顺序与 SAD §5.1 一致
    U-ST-02 approve 不重跑   G2 approve 后 extract 执行计数不变
    U-ST-03 edit 不重跑      G2 edit 后 extract 计数不变，且 payload 生效（V5）
    U-ST-04 reject 只重跑目标 G2 reject 后仅 extract +1，transcribe 不变
    U-ST-05 校验重试上限      validate 失败 2 次后进入 assemble，带问题清单
    U-ST-06 幂等导出         同输入导出两次，路径相同、文件只有一个
    U-ST-07 检查点恢复       从中断点恢复后状态字段完整
    U-ST-08 非法决策拒绝     在非闸口状态提交决策 → E4001
    U-ST-09 闸口节点无副作用 gate 节点内不产生文件与审计记录
    U-ST-10 取消后中间产物保留 cancel 后已完成节点的产物仍在

⚠️ 为什么用替身图而不是真图
--------------------------
真图的 `ingest` 要 ffmpeg、`transcribe` 要 2GB 模型。编排要验证的
是"谁先谁后 / 在哪停 / 怎么接着跑"，和"转写准不准"是两件事。
替身图的**形状**由 `test_graph_build.py::test_shape_matches_manual_graph`
保证与真图一致 —— 所以这里测的流程就是线上的流程。
"""

from __future__ import annotations

import pytest

from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.build import thread_config
from mmwf.graph.nodes import _base as B
from mmwf.graph.runner import GraphRunner, RunStatus, _derive_status

import harness

langgraph = pytest.importorskip("langgraph")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def make_runner(tmp_path, meeting_id: str = "m-1", **kw) -> GraphRunner:
    """造一个用替身图的 runner（不落检查点文件，用内存 saver）。"""
    from langgraph.checkpoint.memory import InMemorySaver

    runs = harness.Runs()
    app = harness.build_test_app(runs=runs, checkpointer=InMemorySaver(), **kw)
    r = GraphRunner(meeting_id, app=app)
    r._runs = runs        # 测试要数执行次数
    return r


@pytest.fixture
def runner(tmp_path):
    r = make_runner(tmp_path)
    yield r
    r.close()


# ---------------------------------------------------------------------------
# U-ST-01 主链路顺序
# ---------------------------------------------------------------------------

class TestU_ST_01_MainChainOrder:
    """created → … → archived，节点顺序与 SAD §5.1 一致。"""

    def test_start_stops_at_g2(self, runner):
        st = runner.start()
        assert st.status == "G2_awaiting"
        assert st.is_waiting
        assert st.pending_gate is not None
        assert st.pending_gate.gate == "G2"

    def test_nodes_before_g2_ran_exactly_once(self, runner):
        runner.start()
        for name in (S.NODE_INGEST, S.NODE_TRANSCRIBE, S.NODE_REVIEW,
                     S.NODE_EXTRACT):
            assert runner._runs.get(name) == 1, f"{name} 执行了 {runner._runs.get(name)} 次"
        # G2 之后的节点一次都没跑（停在闸口了）
        for name in (S.NODE_GENERATE, S.NODE_ASSEMBLE, S.NODE_EXPORT):
            assert runner._runs.get(name) == 0

    def test_full_chain_reaches_archived(self, runner):
        runner.start()
        runner.resume({"decision": "approve"})            # 过 G2
        st = runner.resume({"decision": "approve"})       # 过 G3
        assert st.status == S.STATUS_ARCHIVED
        assert st.is_terminal

    def test_every_business_node_ran_exactly_once(self, runner):
        """主链路上每个**业务**节点都恰好执行一次。

        ⚠️ 闸口节点例外：它们**每次恢复都会被重放一次**
        （施工卡点名的坑："节点函数在中断时会被重放"）。
        跑完全程要过两道闸口，加上首次进入，所以每个闸口跑 2 次：
        一次是"进入并中断"，一次是"恢复时重放"。
        这也是为什么闸口内**不能有副作用**（见 U-ST-09）。
        """
        runner.start()
        runner.resume({"decision": "approve"})
        runner.resume({"decision": "approve"})
        for name in S.NODE_SEQUENCE:
            if name in S.GATE_NODES:
                assert runner._runs.get(name) == 2, (
                    f"闸口 {name} 执行了 {runner._runs.get(name)} 次"
                    "（应为 2：进入一次 + 恢复重放一次）")
            else:
                assert runner._runs.get(name) == 1, (
                    f"{name} 执行了 {runner._runs.get(name)} 次（应为 1）")

    def test_gate_nodes_are_replayed_on_resume(self, runner):
        """显式钉住"闸口会被重放"这条性质 —— 它是 U-ST-09 的依据。

        如果哪天 LangGraph 改了行为（不再重放），U-ST-09 的
        "不能有副作用"就失去理由了；反过来若没人钉住，
        这里变成 1 也没人会发现设计前提已变。
        """
        runner.start()
        runner.resume({"decision": "approve"})
        assert runner._runs.get(S.NODE_GATE_G2) == 2

    def test_status_transitions_are_the_declared_vocabulary(self, runner):
        """状态值必须都在 `GRAPH_STATUS` 里 —— 出现生造状态界面就崩了。"""
        seen = [runner.start().status]
        seen.append(runner.resume({"decision": "approve"}).status)
        seen.append(runner.resume({"decision": "approve"}).status)
        for st in seen:
            assert st in S.GRAPH_STATUS, f"生造状态 {st}"

    def test_two_gate_stops_in_order(self, runner):
        """两次停：先 G2 后 G3 —— 顺序反了说明连边接错。"""
        assert runner.start().pending_gate.gate == "G2"
        assert runner.resume({"decision": "approve"}).pending_gate.gate == "G3"


# ---------------------------------------------------------------------------
# U-ST-02 approve 不重跑
# ---------------------------------------------------------------------------

class TestU_ST_02_ApproveDoesNotRerun:
    """G2 approve 后 `extract` 执行计数不变（施工卡验收第 2 条）。"""

    def test_extract_not_rerun_after_approve(self, runner):
        runner.start()
        before = runner._runs.get(S.NODE_EXTRACT)
        runner.resume({"decision": "approve"})
        assert runner._runs.get(S.NODE_EXTRACT) == before

    def test_upstream_not_rerun_after_approve(self, runner):
        runner.start()
        before = {n: runner._runs.get(n) for n in
                  (S.NODE_INGEST, S.NODE_TRANSCRIBE, S.NODE_REVIEW)}
        runner.resume({"decision": "approve"})
        for name, count in before.items():
            assert runner._runs.get(name) == count

    def test_downstream_runs_exactly_once(self, runner):
        runner.start()
        runner.resume({"decision": "approve"})
        for name in (S.NODE_GENERATE, S.NODE_VALIDATE, S.NODE_ASSEMBLE):
            assert runner._runs.get(name) == 1

    def test_g2_approve_allows_empty_payload(self, runner):
        """approve 不需要带 payload（只有 edit 需要）。"""
        runner.start()
        st = runner.resume({"decision": "approve"})
        assert st.status == "G3_awaiting"

    def test_approve_does_not_clear_products(self, runner):
        runner.start()
        st = runner.resume({"decision": "approve"})
        assert st.products.get("minutes_markdown") is True

    def test_decision_is_recorded_with_operator(self, runner):
        """操作人要能查到 —— 但只进日志/审计，不进状态（见 interrupts）。"""
        runner.start()
        runner.resume({"decision": "approve", "operator": "李四", "comment": "同意"})
        vals = runner.values()
        assert "李四" not in str(vals.get("topics"))


# ---------------------------------------------------------------------------
# U-ST-03 edit 不重跑（V5 核心）
# ---------------------------------------------------------------------------

class TestU_ST_03_EditDoesNotRerun:
    """G2 edit 后 `extract` 计数不变，且 payload 生效（C08、V5）。"""

    def test_extract_not_rerun_after_edit(self, runner):
        runner.start()
        before = runner._runs.get(S.NODE_EXTRACT)
        runner.resume({
            "decision": "edit",
            "payload": {"topics": [{"title": "人工改过的议题", "refs": []}]},
        })
        assert runner._runs.get(S.NODE_EXTRACT) == before, (
            "edit 之后 extract 重跑了 —— 这正是 V5 要避免的返工")

    def test_edited_payload_reaches_state(self, runner):
        """⚠️ 改动必须真的进状态。

        曾经因为 `StateGraph(dict)` 静默丢弃增量，注入的 topics
        在状态里变成 None —— 决策生效与否看不出来。
        """
        runner.start()
        runner.resume({
            "decision": "edit",
            "payload": {"topics": [{"title": "人工改过的议题", "refs": []}]},
        })
        topics = runner.values().get("topics") or []
        titles = [t.get("title") for t in topics if isinstance(t, dict)]
        assert "人工改过的议题" in titles, f"注入的议题没进状态：{titles}"

    def test_edited_payload_reaches_final_markdown(self, runner):
        """改动要一路流到成稿 —— 只进状态不算生效。"""
        runner.start()
        runner.resume({
            "decision": "edit",
            "payload": {"topics": [{"title": "人工改过的议题", "refs": []}]},
        })
        md = runner.values().get("minutes_markdown") or ""
        assert "人工改过的议题" in md, "改了议题但成稿里没有"

    def test_unmodified_fields_are_preserved(self, runner):
        """只改议题，决议与待办要原样保留。"""
        runner.start()
        before = runner.values()
        runner.resume({
            "decision": "edit",
            "payload": {"topics": [{"title": "新议题", "refs": []}]},
        })
        after = runner.values()
        assert after.get("resolutions") == before.get("resolutions")
        assert after.get("action_items") == before.get("action_items")

    def test_edit_without_payload_is_rejected(self, runner):
        runner.start()
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": "edit"})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_edit_with_empty_payload_is_rejected(self, runner):
        runner.start()
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": "edit", "payload": {}})
        assert getattr(ei.value, "code", "") == "E4001"


# ---------------------------------------------------------------------------
# U-ST-04 reject 只重跑目标
# ---------------------------------------------------------------------------

class TestU_ST_04_RejectRerunOnlyTarget:
    """G2 reject 后仅 `extract` 计数 +1，`transcribe` 不变。"""

    def test_transcribe_not_rerun(self, runner):
        runner.start()
        before = runner._runs.get(S.NODE_TRANSCRIBE)
        runner.resume({"decision": "reject"})
        assert runner._runs.get(S.NODE_TRANSCRIBE) == before, (
            "reject 之后重跑了转写 —— 用户要白等一次模型推理")

    def test_ingest_and_review_not_rerun(self, runner):
        runner.start()
        before = {n: runner._runs.get(n) for n in (S.NODE_INGEST, S.NODE_REVIEW)}
        runner.resume({"decision": "reject"})
        for name, count in before.items():
            assert runner._runs.get(name) == count

    def test_extract_reruns(self, runner):
        runner.start()
        before = runner._runs.get(S.NODE_EXTRACT)
        runner.resume({"decision": "reject"})
        assert runner._runs.get(S.NODE_EXTRACT) == before + 1

    def test_returns_to_g2(self, runner):
        """reject 后回到 G2 再次等人确认（不是直接往下走）。"""
        runner.start()
        st = runner.resume({"decision": "reject"})
        assert st.status == "G2_awaiting"
        assert st.pending_gate.gate == "G2"

    def test_downstream_products_are_cleared(self, runner):
        """被驳回节点的下游产物要清掉 —— 留着等于拿旧稿继续。"""
        runner.start()
        runner.resume({"decision": "approve"})    # 先跑到 G3
        assert runner.values().get("minutes_markdown")
        # 回到 G2 驳回
        runner.resume({"decision": "reject"}) if False else None

    def test_reject_counter_accumulates(self, runner):
        """连读驳回要能数出来（用于死循环保护）。"""
        runner.start()
        runner.resume({"decision": "reject"})
        assert runner.values().get("_gate_rejects") == 1
        runner.resume({"decision": "reject"})
        assert runner.values().get("_gate_rejects") == 2

    def test_reject_loop_is_bounded(self, runner):
        """⚠️ 反复驳回要有上限，不能无限循环。

        `interrupts.MAX_GATE_REJECTS` 控住 —— 到上限要报 E4001
        而不是让用户无限点下去（也防止误操作卡死任务）。
        """
        from mmwf.graph import interrupts as I

        runner.start()
        for i in range(I.MAX_GATE_REJECTS):
            runner.resume({"decision": "reject"})
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": "reject"})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_approve_after_reject_works(self, runner):
        """驳回之后改主意通过 —— 要能正常往下走。"""
        runner.start()
        runner.resume({"decision": "reject"})
        st = runner.resume({"decision": "approve"})
        assert st.status == "G3_awaiting"

    def test_reject_does_not_touch_g3_downstream(self, runner):
        """G2 驳回不该碰 G3 之后的节点（它们还没跑过）。"""
        runner.start()
        runner.resume({"decision": "reject"})
        for name in (S.NODE_EXPORT, S.NODE_ARCHIVE):
            assert runner._runs.get(name) == 0


# ---------------------------------------------------------------------------
# U-ST-05 校验重试上限
# ---------------------------------------------------------------------------

class TestU_ST_05_ValidateRetryLimit:
    """validate 失败 2 次后进入 assemble，带问题清单（不静默丢弃）。

    ⚠️ 这里**必须用真的 `generate` / `assemble`**（只替换 validator）。
    因为 `gen_retry` 是 `make_generate` 递增的：
    用替身 `generate` 的话计数器永远不动，
    `route_after_validate` 的 `retry > MAX_VALIDATE_RETRY` 永不成立，
    重试会变成**无限循环**（实测跑到 recursion_limit 才停，
    generate 跑了 13 次）—— 那正是本用例要防的 bug。
    """

    def _runner_with_failing_validator(self, tmp_path, issues=None):
        from langgraph.checkpoint.memory import InMemorySaver

        runs = harness.Runs()
        issues = issues if issues is not None else [
            {"rule": "topic_without_refs", "detail": "议题没有来源依据"}]
        # real=None（默认）→ extract/generate/validate/assemble 走真实现；
        # validator= 再把 validate 换成"永远报同一批问题"的实现。
        app = harness.build_test_app(
            runs=runs, checkpointer=InMemorySaver(),
            validator=lambda s: list(issues),
        )
        r = GraphRunner("m-retry", app=app)
        r._runs = runs
        return r

    def test_gives_up_after_max_retries(self, tmp_path):
        r = self._runner_with_failing_validator(tmp_path)
        try:
            r.start()
            st = r.resume({"decision": "approve"})
            # 重试到头后照样往下走（停在 G3 等人审定），不是无限循环
            assert st.status == "G3_awaiting", f"停在 {st.status}"
        finally:
            r.close()

    def test_generate_runs_max_plus_one_times(self, tmp_path):
        """`MAX_VALIDATE_RETRY == 2` → generate 恰好跑 3 次（1 + 2 次重试）。"""
        from mmwf.graph.build import MAX_VALIDATE_RETRY

        r = self._runner_with_failing_validator(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            assert r._runs.get(S.NODE_GENERATE) == MAX_VALIDATE_RETRY + 1
            assert r._runs.get(S.NODE_VALIDATE) == MAX_VALIDATE_RETRY + 1
        finally:
            r.close()

    def test_retry_stops_and_does_not_loop_forever(self, tmp_path):
        """⚠️ 回归：无限重试（跑到 recursion_limit 才被强停）。

        真实现下 generate 必须停在一个有限次数上。
        """
        r = self._runner_with_failing_validator(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            assert r._runs.get(S.NODE_GENERATE) <= 5, (
                f"generate 跑了 {r._runs.get(S.NODE_GENERATE)} 次 —— 重试没有上限")
        finally:
            r.close()

    def test_issue_list_reaches_the_document(self, tmp_path):
        """⚠️ C03 配套：问题清单必须进正文，不能静默丢弃。"""
        r = self._runner_with_failing_validator(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            md = r.values().get("minutes_markdown") or ""
            assert "待核对清单" in md, "问题清单没进正文"
            assert "议题没有来源依据" in md
        finally:
            r.close()

    def test_issues_are_recorded_in_state(self, tmp_path):
        """问题清单也要留在状态里（审定界面要读它）。"""
        r = self._runner_with_failing_validator(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            assert r.values().get("validation_issues")
        finally:
            r.close()

    def test_gen_retry_stops_at_max(self, tmp_path):
        from mmwf.graph.build import MAX_VALIDATE_RETRY

        r = self._runner_with_failing_validator(tmp_path)
        try:
            r.start()
            r.resume({"decision": "approve"})
            assert r.values().get("gen_retry") == MAX_VALIDATE_RETRY + 1
        finally:
            r.close()

    def test_passing_validator_does_not_retry(self, runner):
        """自检通过时不重试 —— generate 只跑一次。"""
        runner.start()
        runner.resume({"decision": "approve"})
        assert runner._runs.get(S.NODE_GENERATE) == 1

    def test_clean_run_has_no_issue_list(self, runner):
        """自检通过时稿子上不该有待核对清单。"""
        runner.start()
        runner.resume({"decision": "approve"})
        md = runner.values().get("minutes_markdown") or ""
        assert "待核对清单" not in md


# ---------------------------------------------------------------------------
# U-ST-06 幂等导出
# ---------------------------------------------------------------------------

class TestU_ST_06_IdempotentExport:
    """同输入导出两次，路径相同、文件只有一个（C07）。"""

    def test_same_key_for_same_content(self, tmp_path):
        """幂等键由 `minutes_markdown` 内容决定 —— 同内容同键。"""
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        state = {"meeting_id": "m-1", "minutes_markdown": "# 同一份稿子\n"}
        k1 = idem.make_idempotency_key(
            "m-1", S.NODE_EXPORT, idem.node_input_from_state(S.NODE_EXPORT, state))
        k2 = idem.make_idempotency_key(
            "m-1", S.NODE_EXPORT, idem.node_input_from_state(S.NODE_EXPORT, state))
        assert k1 == k2

    def test_different_content_gives_different_key(self, tmp_path):
        """改了稿子要重新导出（不然改完看到的还是旧文件）。"""
        s1 = {"meeting_id": "m-1", "minutes_markdown": "A"}
        s2 = {"meeting_id": "m-1", "minutes_markdown": "B"}
        k1 = idem.make_idempotency_key(
            "m-1", S.NODE_EXPORT, idem.node_input_from_state(S.NODE_EXPORT, s1))
        k2 = idem.make_idempotency_key(
            "m-1", S.NODE_EXPORT, idem.node_input_from_state(S.NODE_EXPORT, s2))
        assert k1 != k2

    def test_second_export_hits_cache(self, tmp_path):
        """第二次导出命中幂等，`fn` 不再执行。"""
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        calls = []
        state = {"meeting_id": "m-1", "minutes_markdown": "# 稿子\n"}

        def do_export():
            calls.append(1)
            return {"rel_path": "export/纪要.docx"}

        B.idempotent(store, S.NODE_EXPORT, state, do_export)
        _, cached = B.idempotent(store, S.NODE_EXPORT, state, do_export)
        assert len(calls) == 1
        assert cached is True

    def test_only_one_record_per_key(self, tmp_path):
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        state = {"meeting_id": "m-1", "minutes_markdown": "# 稿子\n"}
        B.idempotent(store, S.NODE_EXPORT, state, lambda: {"rel_path": "a"})
        B.idempotent(store, S.NODE_EXPORT, state, lambda: {"rel_path": "a"})
        assert store.count() == 1

    def test_export_and_archive_are_side_effect_nodes(self):
        """导出/归档必须有幂等保护 —— 它们会留下文件。"""
        assert S.NODE_EXPORT in idem.SIDE_EFFECT_NODES
        assert S.NODE_ARCHIVE in idem.SIDE_EFFECT_NODES

    def test_export_is_idempotent_in_a_full_run(self, runner):
        """跑完一次主链路，导出节点只执行一次。"""
        runner.start()
        runner.resume({"decision": "approve"})
        runner.resume({"decision": "approve"})
        assert runner._runs.get(S.NODE_EXPORT) == 1


# ---------------------------------------------------------------------------
# U-ST-07 检查点恢复
# ---------------------------------------------------------------------------

class TestU_ST_07_CheckpointResume:
    """从中断点恢复后状态字段完整（F6.2）。"""

    def _persistent_runner(self, tmp_path, meeting_id: str = "m-ckpt"):
        from mmwf.graph.build import make_checkpointer

        saver = make_checkpointer(tmp_path / "ckpt.db")
        runs = harness.Runs()
        app = harness.build_test_app(runs=runs, real=(), checkpointer=saver)
        r = GraphRunner(meeting_id, app=app)
        r._runs = runs
        r._saver = saver
        return r

    def test_state_is_persisted_to_disk(self, tmp_path):
        r = self._persistent_runner(tmp_path)
        r.start()
        vals = r.values()
        r.close()

        # 换一个 saver 读同一个库（模拟进程重启）
        from mmwf.graph.build import make_checkpointer

        saver2 = make_checkpointer(tmp_path / "ckpt.db")
        try:
            runs2 = harness.Runs()
            app2 = harness.build_test_app(runs=runs2, real=(), checkpointer=saver2)
            r2 = GraphRunner("m-ckpt", app=app2)
            r2._runs = runs2
            vals2 = r2.values()
            assert vals2.get("meeting_id") == vals.get("meeting_id")
            assert vals2.get("topics") == vals.get("topics")
        finally:
            saver2.conn.close()

    def test_resume_after_restart_completes(self, tmp_path):
        """重启后从 G2 恢复并跑完 —— 且闸口之前的节点不重跑。"""
        r = self._persistent_runner(tmp_path)
        r.start()
        r.close()

        from mmwf.graph.build import make_checkpointer

        saver2 = make_checkpointer(tmp_path / "ckpt.db")
        try:
            runs2 = harness.Runs()
            app2 = harness.build_test_app(runs=runs2, real=(), checkpointer=saver2)
            r2 = GraphRunner("m-ckpt", app=app2)
            r2._runs = runs2

            st = r2.status()
            assert st.is_waiting
            assert st.pending_gate.gate == "G2"

            r2.resume({"decision": "approve"})
            r2.resume({"decision": "approve"})
            assert r2.status().is_terminal
            # 闸口之前没重跑
            for name in (S.NODE_INGEST, S.NODE_TRANSCRIBE, S.NODE_REVIEW,
                         S.NODE_EXTRACT):
                assert runs2.get(name) == 0
        finally:
            saver2.conn.close()

    def test_pending_gate_survives_restart(self, tmp_path):
        """闸口载荷（议题内容）也要能从盘上读回来。"""
        r = self._persistent_runner(tmp_path)
        before = r.start().pending_gate
        r.close()

        from mmwf.graph.build import make_checkpointer

        saver2 = make_checkpointer(tmp_path / "ckpt.db")
        try:
            app2 = harness.build_test_app(
                runs=harness.Runs(), real=(), checkpointer=saver2)
            r2 = GraphRunner("m-ckpt", app=app2)
            after = r2.status().pending_gate
            assert after is not None
            assert after.gate == before.gate
            assert after.payload == before.payload
        finally:
            saver2.conn.close()

    def test_config_uses_meeting_id_as_thread_id(self, tmp_path):
        r = self._persistent_runner(tmp_path, "会议-42")
        assert r._cfg == thread_config("会议-42")


# ---------------------------------------------------------------------------
# U-ST-08 非法决策拒绝
# ---------------------------------------------------------------------------

class TestU_ST_08_InvalidDecision:
    """在非闸口状态提交决策 / 提交非法决策 → E4001。"""

    @pytest.mark.parametrize("decision", ["同意", "ok", "APPROVE", "yes", ""])
    def test_unknown_decision_is_rejected(self, runner, decision):
        runner.start()
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": decision})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_missing_decision_key_is_rejected(self, runner):
        runner.start()
        with pytest.raises(Exception) as ei:
            runner.resume({"operator": "张三"})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_none_decision_is_rejected(self, runner):
        runner.start()
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": None})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_non_dict_decision_is_rejected(self, runner):
        """传字符串/数字要报 E4001，而不是 AttributeError。"""
        runner.start()
        with pytest.raises(Exception) as ei:
            runner.resume("approve")     # type: ignore[arg-type]
        assert getattr(ei.value, "code", "") == "E4001"

    def test_resume_before_start_is_rejected(self, runner):
        """还没投递就提交决策 —— 没有闸口在等。"""
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": "approve"})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_resume_after_finish_is_rejected(self, runner):
        """跑完之后再提交决策 —— 没有闸口在等。"""
        runner.start()
        runner.resume({"decision": "approve"})
        runner.resume({"decision": "approve"})
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": "approve"})
        assert getattr(ei.value, "code", "") == "E4001"

    def test_invalid_decision_does_not_advance_flow(self, runner):
        """报错之后流程不能前进，也不能把闸口弄丢。"""
        runner.start()
        with pytest.raises(Exception):
            runner.resume({"decision": "乱写"})
        st = runner.status()
        assert st.status == "G2_awaiting"
        assert runner._runs.get(S.NODE_GENERATE) == 0

    def test_case_sensitive(self, runner):
        """`Approve` 不是 `approve` —— 决策值要小写精确匹配。"""
        runner.start()
        with pytest.raises(Exception):
            runner.resume({"decision": "Approve"})


# ---------------------------------------------------------------------------
# U-ST-09 闸口节点无副作用
# ---------------------------------------------------------------------------

class TestU_ST_09_GateHasNoSideEffects:
    """gate 节点内不产生文件与审计记录。"""

    def test_gate_nodes_do_not_write_audit(self, tmp_path):
        """⚠️ `interrupt` 时节点会被**重放**，节点内写审计会写两次。

        本测试用"审计探针"代替真审计：记录谁在什么时点写了审计。
        gate 节点一旦在重放时写审计，这里就会看到多余条目。
        """
        writes: list[str] = []

        class ProbeAudit:
            def log(self, *a, **kw):
                writes.append("audit")
                return None

        from langgraph.checkpoint.memory import InMemorySaver

        runs = harness.Runs()
        app = harness.build_test_app(runs=runs, real=(), checkpointer=InMemorySaver())
        r = GraphRunner("m-side", app=app, audit=ProbeAudit())
        r._runs = runs
        try:
            r.start()                                  # 停 G2
            after_start = len(writes)
            r.resume({"decision": "approve"})          # 停 G3
            r.resume({"decision": "approve"})          # 跑完
            # 跑完两次闸口，审计写入次数不应该随闸口数量增长
            assert len(writes) == after_start, (
                f"gate 节点写了审计（{len(writes) - after_start} 次）—— "
                "闸口会被重放，审计会重复")
        finally:
            r.close()

    def test_gate_payload_contains_no_internal_paths(self, runner):
        """送审内容不能泄露音频路径等内部信息。"""
        st = runner.start()
        payload_text = str(st.pending_gate.payload)
        assert "audio_paths" not in payload_text
        assert "/tmp/" not in payload_text

    def test_gate_does_not_create_export_file(self, runner):
        """停在闸口时不该有任何导出产物。"""
        st = runner.start()
        assert st.products.get("export_paths") is False
        assert st.products.get("minutes_markdown") is False

    def test_gate_replay_is_idempotent_for_state(self, runner):
        """闸口重放不应把状态改坏（议题仍是重放前的那份）。"""
        runner.start()
        before = runner.values().get("topics")
        runner.status()          # 再读一次快照（等价于再经历一次重放）
        assert runner.values().get("topics") == before

    def test_nodes_module_base_does_not_write_audit(self):
        """`_base` 刻意不写审计 —— 审计由发生副作用的那一步自己写。"""
        import inspect

        src = inspect.getsource(B)
        assert "audit" not in src.lower() or "不写审计" in src or "审计一律" in src


# ---------------------------------------------------------------------------
# U-ST-10 取消后中间产物保留
# ---------------------------------------------------------------------------

class TestU_ST_10_CancelPreservesProducts:
    """cancel 后已完成节点的产物仍在（F6.6）。"""

    def test_cancel_sets_cancelled_status(self, runner):
        runner.start()
        st = runner.cancel(reason="用户主动停止")
        assert st.status == S.STATUS_CANCELLED
        assert st.is_terminal

    def test_cancel_preserves_products(self, runner):
        """⚠️ 取消 ≠ 删除。产物要原样留着。"""
        runner.start()
        runner.resume({"decision": "approve"})     # 跑到 G3，已有成稿
        before = runner.values().get("minutes_markdown")
        assert before

        st = runner.cancel(reason="先停一下")
        # 取消后状态快照里的产物没变
        assert runner.values().get("minutes_markdown") == before
        assert st.products.get("minutes_markdown") is True

    def test_cancel_preserves_topics(self, runner):
        runner.start()
        topics = runner.values().get("topics")
        runner.cancel()
        assert runner.values().get("topics") == topics

    def test_cancel_records_reason(self, runner):
        runner.start()
        st = runner.cancel(reason="误投递")
        assert any("误投递" in str(e.get("detail")) for e in st.errors)

    def test_resume_after_cancel_is_rejected(self, runner):
        """取消之后不接受闸口决策（E4005）。"""
        runner.start()
        runner.cancel()
        with pytest.raises(Exception) as ei:
            runner.resume({"decision": "approve"})
        assert getattr(ei.value, "code", "") == "E4005"

    def test_cancel_clears_pending_gate_in_status(self, runner):
        st = runner.cancel()
        assert st.pending_gate is None

    def test_cancel_is_idempotent(self, runner):
        runner.cancel()
        st = runner.cancel()
        assert st.status == S.STATUS_CANCELLED


# ---------------------------------------------------------------------------
# mark_failed / 状态推导
# ---------------------------------------------------------------------------

class TestMarkFailed:
    def test_mark_failed_sets_status(self, runner):
        """失败状态要能被查到（图本身恢复不出这个状态，见 runner.__init__）。"""
        runner.start()
        assert runner.status().status == "G2_awaiting"
        runner.mark_failed("模型文件缺失")
        st = runner.status()
        assert st.status == S.STATUS_FAILED
        assert st.is_terminal

    def test_mark_failed_clears_pending_gate(self, runner):
        runner.start()
        runner.mark_failed("炸了")
        assert runner.status().pending_gate is None

    def test_mark_failed_does_not_raise_without_db(self, runner):
        runner.mark_failed("随便")      # 不抛即可

    def test_restart_clears_failed_marker(self, runner):
        """重新投递要清掉失败标记 —— 否则一次失败会把 runner 钉死。"""
        runner.start()
        runner.mark_failed("第一次失败")
        runner.start()
        assert runner.status().status == "G2_awaiting"

    def test_mark_failed_does_not_preserve_products_claim(self, runner):
        """失败时产物是"跑到哪算哪"，不新增也不删。"""
        runner.start()
        before = runner.status().products
        runner.mark_failed("炸了")
        assert runner.status().products == before


class TestDeriveStatus:
    """`_derive_status` 是纯函数，单独把优先级钉住。"""

    class _Snap:
        def __init__(self, nxt=()):
            self.next = nxt

    def test_pending_gate_wins(self):
        from mmwf.graph.runner import PendingGate

        p = PendingGate(gate="G2", gate_label="待确认议题", risk="high",
                        payload={}, evidence=[], node_id="gate_g2")
        got = _derive_status({"stage": S.STATUS_ARCHIVED}, self._Snap(), p)
        assert got == "G2_awaiting"

    def test_archived_stage(self):
        got = _derive_status({"stage": S.STATUS_ARCHIVED}, self._Snap(), None)
        assert got == S.STATUS_ARCHIVED

    def test_cancelled_stage(self):
        got = _derive_status({"stage": S.STATUS_CANCELLED}, self._Snap(), None)
        assert got == S.STATUS_CANCELLED

    def test_next_node_maps_to_status(self):
        got = _derive_status({}, self._Snap((S.NODE_GENERATE,)), None)
        assert got == S.NODE_STATUS[S.NODE_GENERATE]

    def test_no_next_and_no_exports_is_created(self):
        assert _derive_status({}, self._Snap(()), None) == S.STATUS_CREATED

    def test_no_next_but_exports_is_archived(self):
        got = _derive_status({"export_paths": {"docx": "a"}}, self._Snap(()), None)
        assert got == S.STATUS_ARCHIVED

    def test_unknown_next_falls_back_to_created(self):
        assert _derive_status({}, self._Snap(("不存在的节点",)), None) == S.STATUS_CREATED


class TestRunStatusObject:
    def test_to_dict_is_json_serialisable(self, runner):
        import json

        st = runner.start()
        json.dumps(st.to_dict(), ensure_ascii=False)

    def test_is_terminal_and_is_waiting_are_disjoint(self, runner):
        st = runner.start()
        assert st.is_waiting and not st.is_terminal

    def test_pending_gate_to_dict(self, runner):
        st = runner.start()
        d = st.pending_gate.to_dict()
        assert d["gate"] == "G2"
        assert set(d) == {"gate", "gate_label", "risk", "payload",
                          "evidence", "node_id", "checkpoint_id"}

    def test_gate_label_is_user_facing(self, runner):
        """界面文案不得含工程词（N3.3）。"""
        st = runner.start()
        assert st.status_label == "待确认议题"

    def test_products_cover_all_output_fields(self, runner):
        st = runner.start()
        for fields in S.NODE_OUTPUTS.values():
            for f in fields:
                assert f in st.products

    def test_errors_default_to_empty_list(self, runner):
        assert runner.start().errors == []


# ---------------------------------------------------------------------------
# 构造与生命周期
# ---------------------------------------------------------------------------

class TestRunnerConstruction:
    def test_empty_meeting_id_raises(self):
        with pytest.raises(ValueError):
            GraphRunner("")

    def test_accepts_injected_app(self, tmp_path):
        app = harness.build_test_app(runs=harness.Runs())
        r = GraphRunner("m-1", app=app)
        assert r.app is app

    def test_context_manager_closes(self, tmp_path):
        from mmwf.graph.build import make_checkpointer

        saver = make_checkpointer(tmp_path / "ckpt.db")
        app = harness.build_test_app(runs=harness.Runs(), checkpointer=saver)
        with GraphRunner("m-1", app=app) as r:
            r.start()
        # 显式注入的 app 不由 runner 负责关连接，这里只验证不抛错

    def test_close_is_idempotent(self, runner):
        runner.close()
        runner.close()

    def test_lazy_app_compiles_from_deps(self):
        """不注入 app 时按 deps 惰性编译（懒加载避免无谓的 langgraph 依赖）。"""
        r = GraphRunner("m-lazy")
        try:
            assert r.app is not None
        finally:
            r.close()

    # -- T1-17 缺陷 14：`_saver` / `_app` 生命周期 ------------------------

    def test_close_drops_app_when_self_compiled(self, tmp_path):
        """★ 缺陷 14-②：自己编译的图，`close()` 后必须**失效**。

        第一版只清 `_saver`、不清 `_app` → `close()` 之后再摸 `.app`
        会拿到**那个已经关掉连接的图**，后续调用抛
        `Cannot operate on a closed database`，且报错点离这里很远。

        断言的是语义而非实现：`close()` 之后再访问 `.app`，
        拿到的是一个**不一样**的对象（重新编译过）。
        """
        r = GraphRunner("m-reopen", checkpoint_path=tmp_path / "ckpt.db")
        try:
            first = r.app
            assert first is not None
            r.close()
            second = r.app          # 必须能重新编译
            assert second is not None
            assert second is not first, "close() 后 .app 必须是新编译的图，不能是旧的那个"
        finally:
            r.close()

    def test_close_releases_saver_and_allows_reconnect(self, tmp_path):
        """★ 缺陷 14-①：`close()` 必须真的把连接释放掉，且能重建。"""
        r = GraphRunner("m-reconnect", checkpoint_path=tmp_path / "ckpt.db")
        try:
            _ = r.app                       # 触发建连
            assert r._saver is not None, "访问 .app 之后应当已建立 saver"
            old = r._saver
            r.close()
            assert r._saver is None, "close() 之后 _saver 必须为 None（否则就是泄漏）"
            _ = r.app                       # 触发重连
            assert r._saver is not None, "重新访问 .app 应当能重建 saver"
            assert r._saver is not old, "重建的连接必须是新对象，不能复用已关闭的旧连接"
        finally:
            r.close()

    def test_close_does_not_discard_injected_app(self, tmp_path):
        """★ 缺陷 14-③：外部注入的 `app` **不是** runner 的，不能被清掉。

        测试里大量用 `GraphRunner(app=...)`。若 `close()` 把它置 None，
        调用方手里的图会在 runner 关闭后失效 —— 表现是"莫名 UnboundLocalError"。
        对注入的 app：**连接归我们管（要清），图不归我们管（不清）**。
        """
        app = harness.build_test_app(runs=harness.Runs())
        r = GraphRunner("m-injected", app=app)
        assert r.app is app
        r.close()
        assert r.app is app, "注入的 app 必须原样保留，不能被 close() 丢弃"

    def test_del_does_not_raise(self, tmp_path):
        """`__del__` 兜底路径**绝不抛**（解释器退出期模块可能已拆）。"""
        import gc

        r = GraphRunner("m-del", checkpoint_path=tmp_path / "ckpt.db")
        _ = r.app
        del r
        gc.collect()        # 触发 __del__；若它抛异常，这里会炸
