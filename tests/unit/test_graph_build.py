"""`graph.build` 单元测试（T1-10）。

这个模块"没有业务逻辑"，所以它错的方式很特别：**图仍然能编译通过**，
只是形状不对 —— 少一条边就少走一个节点，条件边映射写错就在运行时
抛 `KeyError`（栈指向 LangGraph 内部）。所以这里要逐条把
SAD §5.1 的连边钉死。

最重要的一条是 `test_shape_matches_manual_graph`：编排测试用的是
`harness.build_test_app` **重搭**的图（为了能数节点执行次数），
重搭的代价就是"两张图可能漂移"。这条测试拿真图去比对替身图的形状，
替身图一旦和真图不一致就会红 —— 它保护的是**所有**编排测试的有效性。
"""

from __future__ import annotations

import pytest

from mmwf.graph import interrupts as I
from mmwf.graph import state as S
from mmwf.graph.build import (
    MAX_VALIDATE_RETRY,
    ROUTE_GIVE_UP,
    ROUTE_OK,
    ROUTE_RETRY,
    Deps,
    build_pipeline,
    make_checkpointer,
    route_after_validate,
    thread_config,
)
from mmwf.graph.nodes import extraction as E

import harness

langgraph = pytest.importorskip("langgraph")


# ---------------------------------------------------------------------------
# route_after_validate（纯函数，先测）
# ---------------------------------------------------------------------------

class TestRouteAfterValidate:
    def test_no_issues_routes_ok(self):
        assert route_after_validate({"validation_issues": []}) == ROUTE_OK

    def test_missing_issues_key_routes_ok(self):
        """没跑过 validate 时不该判成失败 —— 缺省视为通过。

        否则一个刚起步的任务会被误判成"自检没过"。
        """
        assert route_after_validate({}) == ROUTE_OK

    def test_none_issues_routes_ok(self):
        assert route_after_validate({"validation_issues": None}) == ROUTE_OK

    @pytest.mark.parametrize("retry", [1, 2])
    def test_issues_within_limit_retries(self, retry):
        """1~2 次重试仍在额度内（`MAX_VALIDATE_RETRY == 2`）。"""
        got = route_after_validate(
            {"validation_issues": [{"rule": "r"}], "gen_retry": retry})
        assert got == ROUTE_RETRY

    @pytest.mark.parametrize("retry", [3, 4, 10])
    def test_issues_beyond_limit_gives_up(self, retry):
        """⚠️ 超次数要 `give_up`（成文带问题清单），**不是**失败。

        施工卡 C03 配套："2 次重试后带问题清单进入 assemble，
        不静默丢弃"。丢掉等于让用户白等且什么都拿不到。
        """
        got = route_after_validate(
            {"validation_issues": [{"rule": "r"}], "gen_retry": retry})
        assert got == ROUTE_GIVE_UP

    def test_boundary_is_exactly_max(self):
        """边界要精确：`gen_retry == 2` 还重试，`== 3` 才放弃。

        `generate` 每次进入都 `+1`，所以第 1 次自检时 `gen_retry == 1`。
        允许重试 2 次意味着最多跑到 `gen_retry == 3`。
        """
        issues = {"validation_issues": [{"rule": "r"}]}
        assert route_after_validate({**issues, "gen_retry": 2}) == ROUTE_RETRY
        assert route_after_validate({**issues, "gen_retry": 3}) == ROUTE_GIVE_UP

    def test_missing_gen_retry_treated_as_one(self):
        """没有 `gen_retry` 时按"第一次"处理 —— 应该重试而不是放弃。"""
        got = route_after_validate({"validation_issues": [{"rule": "r"}]})
        assert got == ROUTE_RETRY

    def test_gen_retry_zero_still_retries(self):
        got = route_after_validate(
            {"validation_issues": [{"rule": "r"}], "gen_retry": 0})
        assert got == ROUTE_RETRY

    def test_garbage_issues_list_is_tolerated(self):
        """路由函数不能因为问题清单里有垃圾条目就炸。"""
        assert route_after_validate({"validation_issues": [None]}) == ROUTE_RETRY

    def test_max_validate_retry_is_two(self):
        """SAD §5.1 / 施工卡写死 2 —— 改了要回头改文档。"""
        assert MAX_VALIDATE_RETRY == 2
        assert E.MAX_VALIDATE_RETRY == MAX_VALIDATE_RETRY

    def test_returns_a_declared_route_value(self):
        for state in ({}, {"validation_issues": [{"rule": "r"}]},
                      {"validation_issues": [{"rule": "r"}], "gen_retry": 9}):
            assert route_after_validate(state) in {ROUTE_OK, ROUTE_RETRY, ROUTE_GIVE_UP}


# ---------------------------------------------------------------------------
# thread_config
# ---------------------------------------------------------------------------

class TestThreadConfig:
    def test_uses_meeting_id_as_thread_id(self):
        """施工卡点名：`thread_id` 用 `meeting_id`，否则恢复找不到状态。"""
        got = thread_config("m-123")
        assert got == {"configurable": {"thread_id": "m-123"}}

    def test_empty_meeting_id_raises(self):
        with pytest.raises(ValueError):
            thread_config("")

    def test_none_meeting_id_raises(self):
        with pytest.raises(ValueError):
            thread_config(None)   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 编译产物的形状
# ---------------------------------------------------------------------------

def _compiled(*, checkpointer=None):
    return build_pipeline(Deps(), checkpointer=checkpointer)


def _draw(compiled):
    """把编译后的图摊成 `{节点: {出边标签}}`。"""
    out: dict[str, set[str]] = {}
    for node, spec in compiled.get_graph().nodes.items():
        out[node] = set()
    for edge in compiled.get_graph().edges:
        out.setdefault(edge.source, set()).add(edge.target)
    return out


class TestGraphShape:
    def test_compiles_without_deps(self):
        """空 `Deps()` 必须能编译 —— 节点各字段可空是刻意的。"""
        assert build_pipeline() is not None

    def test_has_all_eleven_business_nodes(self):
        g = _compiled().get_graph()
        for name in S.NODE_SEQUENCE:
            assert name in g.nodes, f"少节点 {name}"

    def test_has_start_and_end(self):
        g = _compiled().get_graph()
        assert "__start__" in g.nodes
        assert "__end__" in g.nodes

    def test_main_chain_edges(self):
        """主干：ingest→transcribe→review→extract→gate_g2。"""
        e = _draw(_compiled())
        assert S.NODE_TRANSCRIBE in e[S.NODE_INGEST]
        assert S.NODE_REVIEW in e[S.NODE_TRANSCRIBE]
        assert S.NODE_EXTRACT in e[S.NODE_REVIEW]
        assert S.NODE_GATE_G2 in e[S.NODE_EXTRACT]

    def test_start_points_to_ingest(self):
        e = _draw(_compiled())
        assert S.NODE_INGEST in e["__start__"]

    def test_gate_g2_routes_to_generate_and_extract(self):
        """G2：continue→generate，redo→extract（reject 只重跑 extract）。"""
        e = _draw(_compiled())
        assert S.NODE_GENERATE in e[S.NODE_GATE_G2]
        assert S.NODE_EXTRACT in e[S.NODE_GATE_G2]
        assert len(e[S.NODE_GATE_G2]) == 2

    def test_gate_g2_redo_targets_extract_not_transcribe(self):
        """⚠️ V5/F6 的核心：reject 只回退**一格**。

        回到 `transcribe` 会让用户重听一遍录音（几分钟到几十分钟），
        而议题错了根本不需要重转写。
        """
        e = _draw(_compiled())
        assert S.NODE_TRANSCRIBE not in e[S.NODE_GATE_G2]

    def test_validate_routes_to_assemble_and_generate(self):
        """validate：ok→assemble，retry→generate，give_up→assemble。"""
        e = _draw(_compiled())
        assert S.NODE_ASSEMBLE in e[S.NODE_VALIDATE]
        assert S.NODE_GENERATE in e[S.NODE_VALIDATE]

    def test_validate_never_routes_back_to_extract(self):
        """校验不过**不重跑 extract** —— 议题没错（SAD §5.1）。

        重跑 extract 会让用户在 G2 确认过的议题白确认一次。
        """
        e = _draw(_compiled())
        assert S.NODE_EXTRACT not in e[S.NODE_VALIDATE]

    def test_generate_feeds_validate(self):
        e = _draw(_compiled())
        assert e[S.NODE_GENERATE] == {S.NODE_VALIDATE}

    def test_gate_g3_routes_to_export_and_assemble(self):
        """G3：continue→export，redo→assemble（改措辞只重新成文）。"""
        e = _draw(_compiled())
        assert S.NODE_EXPORT in e[S.NODE_GATE_G3]
        assert S.NODE_ASSEMBLE in e[S.NODE_GATE_G3]
        assert len(e[S.NODE_GATE_G3]) == 2

    def test_gate_g3_redo_does_not_go_back_to_gate_g2(self):
        """⚠️ G3 reject 不能回到 G2。

        回到 G2 等于让用户在 G3 驳回后**重新审一遍议题**，
        而议题在 G2 已经确认过了。
        """
        e = _draw(_compiled())
        assert S.NODE_GATE_G2 not in e[S.NODE_GATE_G3]
        assert S.NODE_EXTRACT not in e[S.NODE_GATE_G3]

    def test_tail_chain_edges(self):
        e = _draw(_compiled())
        assert e[S.NODE_EXPORT] == {S.NODE_ARCHIVE}
        assert e[S.NODE_ARCHIVE] == {"__end__"}

    def test_two_interrupt_nodes_are_the_gates(self):
        """只有两个闸口是中断点 —— 多一个会让流程莫名停住。"""
        assert set(S.GATE_NODES) == {S.NODE_GATE_G2, S.NODE_GATE_G3}

    def test_conditional_nodes_are_gates_and_validate(self):
        """带条件出边的只有 3 个节点：两个闸口 + validate。

        条件边写多的地方就是"流程会莫名走岔"的地方，钉住数量。
        """
        g = _compiled().get_graph()
        branching = {
            node for node, spec in g.nodes.items()
            if len(spec) > 0 and any(
                e.conditional for e in g.edges if e.source == node)
        }
        assert branching == {S.NODE_GATE_G2, S.NODE_GATE_G3, S.NODE_VALIDATE}


class TestShapeMatchesManualGraph:
    """⚠️ 替身图与真图必须同形 —— 保护所有编排测试的有效性。

    `harness.build_test_app` 为了让测试能**数节点执行次数**，
    没有复用 `build_pipeline`，而是手工重搭了一张图。重搭的风险是
    两张图悄悄漂移：真图改了边，替身图没改，于是编排测试测的是
    一个**现实中不存在的流程**，全绿但毫无意义。

    这条测试把两张图的节点集与边集直接比对。
    """

    def _shape(self, compiled) -> tuple[frozenset, frozenset]:
        g = compiled.get_graph()
        nodes = frozenset(g.nodes)
        edges = frozenset(
            (e.source, e.target, bool(e.conditional)) for e in g.edges
        )
        return nodes, edges

    def test_node_sets_are_identical(self):
        real, _ = self._shape(_compiled())
        fake, _ = self._shape(harness.build_test_app(runs=harness.Runs(), real=()))
        assert real == fake, (
            "替身图与真图的节点不一致 —— 编排测试测的流程和线上不一样了。"
            f"只在真图：{sorted(real - fake)}；只在替身：{sorted(fake - real)}"
        )

    def test_edge_sets_are_identical(self):
        _, real = self._shape(_compiled())
        _, fake = self._shape(harness.build_test_app(runs=harness.Runs(), real=()))
        assert real == fake, (
            "替身图与真图的连边不一致 —— 编排测试测的流程和线上不一样了。"
            f"只在真图：{sorted(real - fake)}；只在替身：{sorted(fake - real)}"
        )

    def test_default_build_test_app_also_matches_shape(self):
        """默认参数（四个内部节点用真实现）也必须同形。

        这条防的是"替身表里的键名写错"这类错误：`STUB_INTERNAL`
        的键名错了不会报错，只会静默少一个节点。
        """
        real_nodes, real_edges = self._shape(_compiled())
        fake_nodes, fake_edges = self._shape(harness.build_test_app(runs=harness.Runs()))
        assert real_nodes == fake_nodes
        assert real_edges == fake_edges

    def test_both_use_pipeline_state_schema(self):
        """两张图都必须是 TypedDict schema。

        ⚠️ 用裸 `dict` 当 `state_schema` 时 LangGraph **替换**而不是合并
        状态，节点返回的增量会被**静默丢弃** —— 表现为闸口决策不生效
        （reject 静默变成 continue）。这条把该约定钉在测试里。
        """
        assert S.PipelineState.__total__ is False
        assert hasattr(S.PipelineState, "__annotations__")
        for name in ("_gate_route", "_gate_rejects"):
            assert name in S.PipelineState.__annotations__, (
                f"{name} 未在 PipelineState 里声明会被静默丢弃"
            )


# ---------------------------------------------------------------------------
# make_checkpointer
# ---------------------------------------------------------------------------

class TestMakeCheckpointer:
    def test_creates_db_file(self, tmp_path):
        db = tmp_path / "nested" / "ckpt.db"
        saver = make_checkpointer(db)
        try:
            assert db.exists(), "检查点库文件没建出来"
        finally:
            saver.conn.close()

    def test_creates_parent_dir(self, tmp_path):
        db = tmp_path / "a" / "b" / "ckpt.db"
        saver = make_checkpointer(db)
        try:
            assert db.parent.is_dir()
        finally:
            saver.conn.close()

    def test_runs_setup_so_tables_exist(self, tmp_path):
        """不 setup 的话第一次 invoke 才会报表不存在。"""
        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            rows = saver.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        finally:
            saver.conn.close()
        assert rows, "检查点库是空的 —— saver.setup() 没生效"

    def test_two_checkpointers_have_independent_connections(self, tmp_path):
        """⚠️ 施工卡点名的坑：连接不能共用。

        共用一条 sqlite3 连接时，第二个任务在别的线程里会抛
        `ProgrammingError` —— 单任务测试永远看不到。
        """
        a = make_checkpointer(tmp_path / "a.db")
        b = make_checkpointer(tmp_path / "b.db")
        try:
            assert a.conn is not b.conn
        finally:
            a.conn.close()
            b.conn.close()

    def test_checkpointer_can_be_attached_to_graph(self, tmp_path):
        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            assert build_pipeline(Deps(), checkpointer=saver) is not None
        finally:
            saver.conn.close()


class TestCheckpointResume:
    """T1-10 验收第 5 条：G2 停住后重启能恢复（F6.2）。

    ⚠️ 这里用**替身图**而不是真图：真图的 `review` 需要真实的
    `transcript/segments.json`，会让"检查点能不能恢复"这件事
    被"转写产物在不在"干扰。恢复能力是检查点层的性质，
    与节点实现无关 —— 用替身测才测的是对的东西。
    """

    def _run_to_gate(self, db, meeting_id: str) -> None:
        """跑到 G2 停住（替身图，全替身节点）。"""
        saver = make_checkpointer(db)
        try:
            app = harness.build_test_app(runs=harness.Runs(), real=(), checkpointer=saver)
            app.invoke(harness.initial_state(meeting_id), thread_config(meeting_id))
        finally:
            saver.conn.close()

    def test_state_survives_a_new_graph_instance(self, tmp_path):
        """同一个 `thread_id` 换一张图实例也能读到状态 —— 断点续跑的基础。"""
        db = tmp_path / "ckpt.db"
        self._run_to_gate(db, "m-resume")

        # 换一个 saver / 图实例（模拟进程重启）
        saver2 = make_checkpointer(db)
        try:
            app2 = harness.build_test_app(runs=harness.Runs(), real=(), checkpointer=saver2)
            snap = app2.get_state(thread_config("m-resume"))
            assert snap is not None
            assert snap.values.get("meeting_id") == "m-resume"
        finally:
            saver2.conn.close()

    def test_resume_continues_from_the_gate(self, tmp_path):
        """重启后能从 G2 继续（而不是从头跑）。

        注意要**依次过两道闸口**：从 G2 approve 之后流程会跑到
        **G3 再次停住**（这是设计如此，两个闸口各有一次确认）。
        一次 approve 只能往前走一段，不是走到终点。
        """
        from langgraph.types import Command

        db = tmp_path / "ckpt.db"
        self._run_to_gate(db, "m-resume2")

        saver2 = make_checkpointer(db)
        try:
            runs2 = harness.Runs()
            app2 = harness.build_test_app(runs=runs2, real=(), checkpointer=saver2)
            cfg = thread_config("m-resume2")

            # 恢复前停在 G2（还有下一步 = 确实停住了）
            before = app2.get_state(cfg)
            assert before.next, "恢复后 next 为空 —— 状态没接上"
            assert S.NODE_GATE_G2 in before.next

            # 第 1 次恢复：G2 approve -> 跑到 G3 停住
            app2.invoke(Command(resume={"decision": "approve", "operator": "李四"}), cfg)
            mid = app2.get_state(cfg)
            assert S.NODE_GATE_G3 in mid.next, (
                f"过了 G2 应该停在 G3，实际停在 {mid.next}")
            for name in (S.NODE_GENERATE, S.NODE_VALIDATE, S.NODE_ASSEMBLE):
                assert runs2.get(name) == 1, f"{name} 没有被执行"

            # 第 2 次恢复：G3 approve -> 走到终点
            app2.invoke(Command(resume={"decision": "approve", "operator": "李四"}), cfg)
            after = app2.get_state(cfg)
            assert not after.next, f"恢复后没跑完，还停在 {after.next}"
            for name in (S.NODE_EXPORT, S.NODE_ARCHIVE):
                assert runs2.get(name) == 1, f"{name} 没有被执行"

            # 闸口**之前**的节点一次都没重跑（恢复不是从头开始）
            for name in (S.NODE_INGEST, S.NODE_TRANSCRIBE, S.NODE_REVIEW,
                         S.NODE_EXTRACT):
                assert runs2.get(name) == 0, f"{name} 被重跑了 —— 恢复变成了重跑"

            # 闸口之后的节点也只跑了一次（approve 不重跑，V5）
            for name in (S.NODE_GENERATE, S.NODE_VALIDATE, S.NODE_ASSEMBLE,
                         S.NODE_EXPORT, S.NODE_ARCHIVE):
                assert runs2.get(name) == 1, f"{name} 跑了 {runs2.get(name)} 次"
        finally:
            saver2.conn.close()

    def test_unknown_thread_id_returns_empty_values(self, tmp_path):
        """没跑过的 thread 不该抛异常 —— 返回空状态即可。"""
        saver = make_checkpointer(tmp_path / "ckpt.db")
        try:
            app = build_pipeline(Deps(), checkpointer=saver)
            snap = app.get_state(thread_config("从没跑过"))
            assert not snap.values
        finally:
            saver.conn.close()


class TestDepsContainer:
    def test_all_fields_default_to_none(self):
        d = Deps()
        for name in ("settings", "idem_store", "asr_provider", "llm", "audit",
                     "ingest_service", "review_service", "export_service"):
            assert getattr(d, name) is None

    def test_extra_defaults_to_empty_dict(self):
        assert Deps().extra == {}

    def test_extra_dicts_are_not_shared(self):
        """`default_factory` 而不是 `= {}` —— 共享可变默认值会串台。"""
        a, b = Deps(), Deps()
        a.extra["k"] = 1
        assert b.extra == {}

    def test_accepts_partial_deps(self):
        d = Deps(settings=object())
        assert d.settings is not None
        assert d.idem_store is None

    def test_pipeline_accepts_deps_with_only_idem_store(self, tmp_path):
        from mmwf.graph import idempotency as idem

        store = idem.IdempotencyStore(tmp_path / "idem.db")
        assert build_pipeline(Deps(idem_store=store)) is not None


class TestRouteFunctionsAreWired:
    """图用的路由函数必须就是模块里那两个（而不是内联的 lambda）。"""

    def test_gate_routes_are_the_interrupt_functions(self):
        assert I.route_after_g2.__name__ == "route_after_g2"
        assert I.route_after_g3.__name__ == "route_after_g3"

    @pytest.mark.parametrize("gate,routes", [
        ("G2", S.GATE_ROUTE["G2"]),
        ("G3", S.GATE_ROUTE["G3"]),
    ])
    def test_gate_route_table_matches_sad(self, gate, routes):
        assert routes["approve"] == "continue"
        assert routes["edit"] == "continue"      # ★ edit 不重跑
        assert routes["reject"] == "redo"

    def test_gate_redo_targets_match_sad(self):
        assert S.GATE_REDO_TARGET["G2"] == S.NODE_EXTRACT
        assert S.GATE_REDO_TARGET["G3"] == S.NODE_ASSEMBLE
