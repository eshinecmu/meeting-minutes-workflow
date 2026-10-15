"""`graph.interrupts` 单元测试（T1-10，SAD §5.3）。

这个模块实现的是**闸口语义**，所以测试分两层：

    ① 纯逻辑层 —— `ResumeDecision.validate` / `route_after_*` /
       `build_gate_payload` / `collect_evidence` / 驳回计数上限。
       这层不需要 langgraph。

    ② 真跑图 —— 用 `InMemorySaver` + 真实 `interrupt()` 搭一个最小图，
       验证 approve / edit / reject 三种决策**实际走出的分支**。
       这层必须有 langgraph（缺了就 skip，不 fail）。

第 ② 层是本文件存在的核心理由。纯逻辑层测不出"节点返回的状态增量
被 LangGraph 丢掉"这类问题 —— 那正是本卡实测踩到的坑
（见 `state.PipelineState` 里 `_gate_route` 的注释）。
"""

from __future__ import annotations

import pytest

from mmwf.domain.errors import MmwfError
from mmwf.graph import interrupts as I
from mmwf.graph import state as S

#: 全部业务字段都在的"完整状态"，用于构造测试输入。
FULL_G2_STATE: dict = {
    "topics": [{"title": "议题A"}],
    "resolutions": [{"text": "决议A"}],
    "action_items": [{"text": "待办A"}],
}


# ---------------------------------------------------------------------------
# ① 纯逻辑层
# ---------------------------------------------------------------------------

class TestResumeDecisionValidate:
    def test_approve_ok(self):
        I.ResumeDecision(decision="approve").validate(gate="G2")

    def test_reject_ok(self):
        I.ResumeDecision(decision="reject").validate(gate="G2")

    def test_edit_with_known_payload_ok(self):
        I.ResumeDecision(decision="edit", payload={"topics": []}).validate(gate="G2")

    @pytest.mark.parametrize(
        "bad", ["ok", "yes", "APPROVE", "", None, 123, "通过", "skip"]
    )
    def test_invalid_decision_raises_e4001(self, bad):
        """非法决策报 E4001（施工卡 U-ST-08）。

        **绝不做"默认当 approve"的兜底**：把一个拼错的决策当成"通过"，
        会让错误决策静默生效 —— 而闸口存在的意义就是拦人。
        注意 `None` 也在拦截范围（前端漏传字段是很常见的 bug）。
        """
        with pytest.raises(MmwfError) as ei:
            I.ResumeDecision(decision=bad).validate(gate="G2")
        assert ei.value.code == "E4001"

    def test_case_sensitive(self):
        """`"APPROVE"` 不算合法 —— 大小写不敏感会掩盖前端 bug。"""
        with pytest.raises(MmwfError):
            I.ResumeDecision(decision="APPROVE").validate(gate="G2")

    def test_edit_without_payload_raises(self):
        """点了"改完继续"却没带修改内容，多半是前端 bug，明确报错。"""
        with pytest.raises(MmwfError) as ei:
            I.ResumeDecision(decision="edit", payload=None).validate(gate="G2")
        assert ei.value.code == "E4001"

    def test_edit_with_empty_payload_raises(self):
        with pytest.raises(MmwfError) as ei:
            I.ResumeDecision(decision="edit", payload={}).validate(gate="G2")
        assert ei.value.code == "E4001"

    def test_edit_with_unknown_fields_raises(self):
        """只带未知字段 = 没有任何东西被改，等于假装 edit。"""
        with pytest.raises(MmwfError) as ei:
            I.ResumeDecision(decision="edit", payload={"foo": 1}).validate(gate="G2")
        assert ei.value.code == "E4001"

    def test_edit_payload_with_extra_unknown_fields_ok(self):
        """带了一个已知字段 + 若干未知字段是**允许**的（未知的会被过滤）。

        前端常把整个表单回传，里面混着无关字段，为此报错会让用户
        看到一个莫名其妙的中断。
        """
        I.ResumeDecision(
            decision="edit",
            payload={"topics": [], "irrelevant": 1},
        ).validate(gate="G2")

    def test_approve_ignores_payload(self):
        """approve 带 payload 不报错（前端常回传上一次的 payload）。"""
        I.ResumeDecision(decision="approve", payload={"topics": []}).validate(gate="G2")

    def test_g3_edit_accepts_markdown(self):
        I.ResumeDecision(
            decision="edit", payload={"minutes_markdown": "# 新稿"}
        ).validate(gate="G3")

    def test_g3_edit_rejects_g2_only_field(self):
        """G3 **不允许**改议题 —— 议题已经在 G2 确认过了。

        允许改的话，G3 就成了一条**绕过 G2 闸口**直接改议题的通道，
        闸口的分段防护就漏了。

        注意 IT-06 要求 G3 能 edit，但改的是**措辞**（`minutes_markdown`），
        不是议题 —— 这两件事必须分得开。
        """
        with pytest.raises(MmwfError) as ei:
            I.ResumeDecision(
                decision="edit", payload={"topics": []}
            ).validate(gate="G3")
        assert ei.value.code == "E4001"

    def test_g2_edit_rejects_g3_only_field(self):
        """反向也要拦：G2 审议题，不该放行成稿 Markdown。"""
        with pytest.raises(MmwfError):
            I.ResumeDecision(
                decision="edit", payload={"minutes_markdown": "# 稿"}
            ).validate(gate="G2")

    def test_editable_fields_are_per_gate(self):
        """可编辑字段**按闸口分开**，不是一个共用的大集合。

        共用一个集合时 G3 能改议题 —— 那个漏洞不会报错，
        只会让"G2 已确认"这件事失去意义。
        """
        assert I.GATE_EDITABLE_FIELDS["G2"] == I.G2_PAYLOAD_KEYS
        assert I.GATE_EDITABLE_FIELDS["G3"] == ("minutes_markdown",)
        assert "topics" not in I.GATE_EDITABLE_FIELDS["G3"]
        assert "minutes_markdown" not in I.GATE_EDITABLE_FIELDS["G2"]

    def test_editable_fields_cover_both_gates(self):
        assert set(I.GATE_EDITABLE_FIELDS) == {"G2", "G3"}

    def test_editable_fields_reference_pipeline_state(self):
        ann = S.PipelineState.__annotations__
        for gate, fields in I.GATE_EDITABLE_FIELDS.items():
            for f in fields:
                assert f in ann, f"{gate} 的可编辑字段 {f} 不在 PipelineState"


class TestInterruptPoint:
    def test_gate_from_node_id(self):
        p = I.InterruptPoint("gate_g2", "cp1", {}, "high")
        assert p.gate == "G2"
        p3 = I.InterruptPoint("gate_g3", "cp2", {}, "medium")
        assert p3.gate == "G3"

    def test_gate_label(self):
        assert I.InterruptPoint("gate_g2", "c", {}, "high").gate_label == "待确认议题"
        assert I.InterruptPoint("gate_g3", "c", {}, "medium").gate_label == "待审定纪要"

    def test_to_dict_shape(self):
        d = I.InterruptPoint("gate_g2", "cp1", {"t": 1}, "high", [{"x": 1}]).to_dict()
        assert set(d) == {
            "gate", "gate_label", "node_id", "checkpoint_id",
            "payload", "risk", "evidence",
        }
        assert d["gate"] == "G2"
        assert d["evidence"] == [{"x": 1}]

    def test_is_frozen(self):
        p = I.InterruptPoint("gate_g2", "c", {}, "high")
        with pytest.raises(Exception):
            p.node_id = "other"

    def test_evidence_defaults_empty(self):
        assert I.InterruptPoint("gate_g2", "c", {}, "high").evidence == []


class TestBuildGatePayload:
    def test_g2_payload_keys(self):
        got = I.build_gate_payload("G2", FULL_G2_STATE)
        assert set(got) == set(I.G2_PAYLOAD_KEYS)

    def test_g3_payload_keys(self):
        got = I.build_gate_payload("G3", {"minutes_markdown": "x"})
        assert set(got) == set(I.G3_PAYLOAD_KEYS)

    def test_missing_keys_become_none(self):
        """缺字段用 None 占位（与幂等输入同一套约定）。"""
        got = I.build_gate_payload("G2", {})
        assert got == {"topics": None, "resolutions": None, "action_items": None}

    def test_does_not_leak_internal_paths(self):
        """送审载荷**不能**把 `audio_paths` 这类内部路径带出去。

        载荷会经 API 到前端，而 N3.3 要求界面不出现工程词 ——
        带上路径迟早会漏到界面上。
        """
        got = I.build_gate_payload("G2", {**FULL_G2_STATE, "audio_paths": ["/secret/a.wav"]})
        assert "audio_paths" not in got
        assert "/secret" not in str(got)

    def test_unknown_gate_raises(self):
        with pytest.raises(KeyError):
            I.build_gate_payload("G9", {})


class TestCollectEvidence:
    def test_collects_from_topics_and_resolutions(self):
        got = I.collect_evidence({
            "topics": [{"refs": [{"segment_id": "s1", "start_ms": 100, "text": "原话"}]}],
            "resolutions": [{"refs": [{"segment_id": "s2", "start_ms": 200}]}],
        })
        assert len(got) == 2
        assert got[0]["segment_id"] == "s1"
        assert got[1]["segment_id"] == "s2"

    def test_dedupes_same_segment_and_time(self):
        """同一条原话被两条议题引用时只出现一次。"""
        ref = {"segment_id": "s1", "start_ms": 100, "text": "原话"}
        got = I.collect_evidence({
            "topics": [{"refs": [ref]}, {"refs": [ref]}],
            "resolutions": [],
        })
        assert len(got) == 1

    def test_keeps_same_segment_different_time(self):
        got = I.collect_evidence({
            "topics": [{"refs": [
                {"segment_id": "s1", "start_ms": 100},
                {"segment_id": "s1", "start_ms": 200},
            ]}],
        })
        assert len(got) == 2

    def test_empty_state_yields_empty_list(self):
        """**没有依据不报错** —— "没有依据"是内容问题，不是编排错误。"""
        assert I.collect_evidence({}) == []

    def test_tolerates_non_dict_items(self):
        """状态被外部污染时不该崩 —— 编排层要能扛住脏数据。"""
        assert I.collect_evidence({"topics": ["垃圾", None, 42]}) == []

    def test_tolerates_non_dict_refs(self):
        assert I.collect_evidence({"topics": [{"refs": ["垃圾", None]}]}) == []


class TestRouteFunctions:
    def test_route_after_g2_reads_marker(self):
        assert I.route_after_g2({I.GATE_ROUTE_KEY: S.ROUTE_REDO}) == S.ROUTE_REDO
        assert I.route_after_g2({I.GATE_ROUTE_KEY: S.ROUTE_CONTINUE}) == S.ROUTE_CONTINUE

    def test_route_after_g3_reads_marker(self):
        assert I.route_after_g3({I.GATE_ROUTE_KEY: S.ROUTE_REDO}) == S.ROUTE_REDO

    def test_missing_marker_defaults_to_continue(self):
        """缺键默认 `continue` —— 缺键说明此路由被图之外的人直接调用了。

        默认往下走比抛异常更好用（测试会这么干）。
        **但节点的 `validate()` 是严格的** —— 这个宽容只在这里。
        """
        assert I.route_after_g2({}) == S.ROUTE_CONTINUE
        assert I.route_after_g3({}) == S.ROUTE_CONTINUE

    def test_garbage_marker_defaults_to_continue(self):
        assert I.route_after_g2({I.GATE_ROUTE_KEY: "乱七八糟"}) == S.ROUTE_CONTINUE

    def test_none_marker_defaults_to_continue(self):
        """`clear_route_marker` 写的就是 None，必须被容忍。"""
        assert I.route_after_g2({I.GATE_ROUTE_KEY: None}) == S.ROUTE_CONTINUE

    def test_clear_route_marker(self):
        assert I.clear_route_marker({}) == {I.GATE_ROUTE_KEY: None}


class TestDecisionButtons:
    def test_matches_state_table(self):
        """按钮文案的**唯一来源**是 `state.DECISION_LABEL`，不许各写一份。"""
        assert I.DECISION_BUTTONS == S.DECISION_LABEL

    def test_reject_edit_read_differently(self):
        assert I.DECISION_BUTTONS["reject"] != I.DECISION_BUTTONS["edit"]


class TestConstants:
    def test_max_gate_rejects_is_three(self):
        assert I.MAX_GATE_REJECTS == 3

    def test_route_keys_are_private_prefixed(self):
        """内部字段用下划线前缀，避免与业务字段撞名。"""
        assert I.GATE_ROUTE_KEY.startswith("_")
        assert I.GATE_REJECT_KEY.startswith("_")

    def test_route_keys_are_declared_in_pipeline_state(self):
        """⚠️ 未在 TypedDict 里声明的键会被 LangGraph **静默丢弃**。

        这是本卡实测的坑：丢了之后条件边读到 None → 兜底 `continue`
        → `reject` 不退回而是静默往下走。
        """
        ann = S.PipelineState.__annotations__
        assert I.GATE_ROUTE_KEY in ann
        assert I.GATE_REJECT_KEY in ann


class TestDownstreamOutputs:
    def test_g2_downstream_includes_topics(self):
        """G2 驳回回 extract，所以 extract 自己的产物也要清（要重抽）。"""
        outs = I._downstream_outputs("G2")
        assert "topics" in outs
        assert "resolutions" in outs
        assert "action_items" in outs
        assert "minutes_structured" in outs
        assert "minutes_markdown" in outs

    def test_g2_downstream_excludes_upstream_products(self):
        """转写稿与记录稿**不该**被清 —— 它们在 extract 上游，驳回不重跑。"""
        outs = I._downstream_outputs("G2")
        for keep in ("transcript", "transcript_meta", "record_markdown", "audio_paths"):
            assert keep not in outs, f"{keep} 在 G2 上游，不应被清"

    def test_g3_downstream_excludes_topics(self):
        """G3 驳回只重成文 —— 已确认的议题必须保住。"""
        outs = I._downstream_outputs("G3")
        for keep in ("topics", "resolutions", "action_items", "minutes_structured"):
            assert keep not in outs, f"{keep} 不该被 G3 驳回清掉"

    def test_g3_downstream_includes_markdown(self):
        outs = I._downstream_outputs("G3")
        assert "minutes_markdown" in outs

    def test_no_duplicates(self):
        outs = I._downstream_outputs("G2")
        assert len(outs) == len(set(outs))


class TestPayloadIsEmpty:
    def test_g2_empty_when_all_three_missing(self):
        assert I._payload_is_empty("G2", {}) is True

    def test_g2_empty_when_all_three_empty_lists(self):
        assert I._payload_is_empty(
            "G2", {"topics": [], "resolutions": [], "action_items": []}
        ) is True

    def test_g2_not_empty_with_any_one(self):
        assert I._payload_is_empty("G2", {"topics": [{"t": 1}]}) is False
        assert I._payload_is_empty("G2", {"resolutions": [{"t": 1}]}) is False
        assert I._payload_is_empty("G2", {"action_items": [{"t": 1}]}) is False

    def test_g3_empty_without_markdown(self):
        assert I._payload_is_empty("G3", {}) is True

    def test_g3_not_empty_with_markdown(self):
        """G3 **只要**有成稿就必须给人看，哪怕问题清单是空的。

        成稿审定不能跳过 —— 那是"机器产出直接交付"的最后一关。
        """
        assert I._payload_is_empty("G3", {"minutes_markdown": "# 稿"}) is False

    def test_g3_ignores_issues_alone(self):
        assert I._payload_is_empty("G3", {"validation_issues": [{"x": 1}]}) is True


class TestMakeGateFactory:
    def test_g2_matches_state_table(self):
        assert I.make_gate_g2().__name__ == "gate_g2"

    def test_g3_matches_state_table(self):
        assert I.make_gate_g3().__name__ == "gate_g3"

    def test_unknown_gate_raises(self):
        with pytest.raises(KeyError):
            I.make_gate("G9")

    def test_gate_nodes_have_docstrings(self):
        assert "待确认议题" in I.make_gate_g2().__doc__
        assert "待审定纪要" in I.make_gate_g3().__doc__


class TestMergeEditPayload:
    def test_filters_to_editable_fields(self):
        got = I._merge_edit_payload(
            {}, gate="G2", payload={"topics": [], "audio_paths": ["/x"]}
        )
        assert got == {"topics": []}

    def test_raises_when_nothing_editable(self):
        with pytest.raises(MmwfError) as ei:
            I._merge_edit_payload({}, gate="G2", payload={"foo": 1})
        assert ei.value.code == "E4001"

    def test_cannot_edit_upstream_products(self):
        """`edit` **不是**绕过闸口直接改状态的通道。"""
        with pytest.raises(MmwfError):
            I._merge_edit_payload({}, gate="G2", payload={"transcript": []})

    def test_g3_cannot_edit_topics(self):
        """G3 的 payload 里混进 topics 会被**过滤掉**而不是报错。

        前端把整张表单回传是常态，为此报错会让用户看到一个
        莫名其妙的中断。但过滤后一个字段都不剩时要报错（见下一条）。
        """
        got = I._merge_edit_payload(
            {}, gate="G3", payload={"minutes_markdown": "# 稿", "topics": []}
        )
        assert got == {"minutes_markdown": "# 稿"}
        assert "topics" not in got

    def test_g3_with_only_topics_raises(self):
        with pytest.raises(MmwfError) as ei:
            I._merge_edit_payload({}, gate="G3", payload={"topics": []})
        assert ei.value.code == "E4001"

    def test_g2_cannot_edit_markdown(self):
        got = I._merge_edit_payload(
            {}, gate="G2", payload={"minutes_markdown": "# 稿", "resolutions": []}
        )
        assert got == {"resolutions": []}


class TestApplyDecisionPure:
    """`_apply_decision` 的纯逻辑分支（不打图）。"""

    def test_non_dict_raw_raises_e4001(self):
        """恢复数据形状不对时给明确错误，而不是在下一行 KeyError。"""
        for bad in ("approve", ["approve"], 42, None):
            with pytest.raises(MmwfError) as ei:
                I._apply_decision({}, gate="G2", node_id="gate_g2", raw=bad)
            assert ei.value.code == "E4001"

    def test_approve_returns_continue_route(self):
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "approve"},
        )
        assert got == {I.GATE_ROUTE_KEY: S.ROUTE_CONTINUE}

    def test_approve_does_not_touch_payload(self):
        """approve **原样继续** —— 不该返回任何业务字段的增量。"""
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "approve"},
        )
        for field in I.G2_PAYLOAD_KEYS:
            assert field not in got

    def test_edit_returns_merged_payload_and_continue(self):
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "edit", "payload": {"topics": [{"title": "改过"}]}},
        )
        assert got["topics"] == [{"title": "改过"}]
        assert got[I.GATE_ROUTE_KEY] == S.ROUTE_CONTINUE
        # 没改的字段不该出现在增量里（原值由 LangGraph 合并保留）
        assert "resolutions" not in got

    def test_edit_does_not_return_redo(self):
        """**降返工的核心**：edit 绝不走 redo。"""
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "edit", "payload": {"topics": []}},
        )
        assert got[I.GATE_ROUTE_KEY] != S.ROUTE_REDO

    def test_reject_returns_redo_and_counter(self):
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "reject", "comment": "议题不对"},
        )
        assert got[I.GATE_ROUTE_KEY] == S.ROUTE_REDO
        assert got[I.GATE_REJECT_KEY] == 1

    def test_reject_nulls_downstream_products(self):
        """驳回必须清下游产物。

        不清的话，上游重跑后下游若因幂等命中而复用旧产物，
        用户看到的内容还是被驳回的那一版 —— 状态说"退回重做"，
        内容却是旧的，这是驳回最容易出的错。
        """
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "reject"},
        )
        assert got["topics"] is None
        assert got["minutes_markdown"] is None

    def test_reject_appends_error_entry(self):
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "reject", "comment": "议题不对"},
        )
        assert got["errors"][0]["code"] == "E4001"
        assert "议题不对" in got["errors"][0]["detail"]

    def test_reject_accumulates_error_entries(self):
        """`errors` 是累积字段 —— 覆盖会让前一个节点的错被吃掉。"""
        prev = {"errors": [{"code": "E1001", "node": "ingest", "detail": "x"}]}
        got = I._apply_decision(
            {**FULL_G2_STATE, **prev}, gate="G2", node_id="gate_g2",
            raw={"decision": "reject"},
        )
        assert len(got["errors"]) == 2
        assert got["errors"][0]["code"] == "E1001"

    def test_reject_counter_increments(self):
        state = {**FULL_G2_STATE, I.GATE_REJECT_KEY: 2}
        got = I._apply_decision(
            state, gate="G2", node_id="gate_g2", raw={"decision": "reject"},
        )
        assert got[I.GATE_REJECT_KEY] == 3

    def test_reject_beyond_max_raises(self):
        """超过上限**停止退回**（否则任务永远出不去）。

        连着驳到上限仍要退回，说明人机之间对不上（比如上游根本
        抽不出议题）。这时不能继续循环 —— 让任务落到 failed 交人工。
        """
        state = {**FULL_G2_STATE, I.GATE_REJECT_KEY: I.MAX_GATE_REJECTS}
        with pytest.raises(MmwfError) as ei:
            I._apply_decision(
                state, gate="G2", node_id="gate_g2", raw={"decision": "reject"},
            )
        assert ei.value.code == "E4001"
        assert ei.value.context["rejects"] == I.MAX_GATE_REJECTS

    def test_reject_loop_guard_triggers_exactly_at_limit_plus_one(self):
        for n in range(I.MAX_GATE_REJECTS):
            state = {**FULL_G2_STATE, I.GATE_REJECT_KEY: n}
            I._apply_decision(
                state, gate="G2", node_id="gate_g2", raw={"decision": "reject"},
            )  # 前 3 次都该通过
        state = {**FULL_G2_STATE, I.GATE_REJECT_KEY: I.MAX_GATE_REJECTS}
        with pytest.raises(MmwfError):
            I._apply_decision(
                state, gate="G2", node_id="gate_g2", raw={"decision": "reject"},
            )

    def test_operator_is_recorded_in_log_only(self):
        """操作人只进日志，**不进状态** —— 状态里放操作人会让
        "同一份内容换个操作人"被算成不同产物。"""
        got = I._apply_decision(
            FULL_G2_STATE, gate="G2", node_id="gate_g2",
            raw={"decision": "approve", "operator": "张三"},
        )
        assert "operator" not in got

    def test_invalid_decision_propagates_e4001(self):
        with pytest.raises(MmwfError) as ei:
            I._apply_decision(
                FULL_G2_STATE, gate="G2", node_id="gate_g2",
                raw={"decision": "nope"},
            )
        assert ei.value.code == "E4001"

    def test_g3_reject_targets_assemble_not_extract(self):
        got = I._apply_decision(
            {"minutes_markdown": "稿"}, gate="G3", node_id="gate_g3",
            raw={"decision": "reject"},
        )
        assert got[I.GATE_ROUTE_KEY] == S.ROUTE_REDO
        assert got["minutes_markdown"] is None
        # 议题不该被 G3 驳回清掉
        assert "topics" not in got


# ---------------------------------------------------------------------------
# ② 真跑图（需要 langgraph）
# ---------------------------------------------------------------------------

langgraph = pytest.importorskip("langgraph", reason="需要 langgraph 才能打图")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402


class _Runs:
    """记录各节点执行次数的可观测替身。

    **不能把计数器放进流水线状态**：状态会被检查点持久化并在恢复时
    重放，计数器放进去会"恢复之后继续累加"，`== 1` 这类断言永远算不准。
    所以用图外对象记（这也正是 runner.py 的做法）。
    """

    def __init__(self) -> None:
        self.n: dict[str, int] = {}

    def bump(self, name: str) -> None:
        self.n[name] = self.n.get(name, 0) + 1

    def get(self, name: str) -> int:
        return self.n.get(name, 0)


def _build_g2_app(runs: _Runs):
    """G2 的最小复现图：extract -> gate_g2 -{continue,redo}-> terminal。"""

    def extract(state):
        runs.bump("extract")
        return {
            "topics": [{"title": "议题A", "refs": [
                {"segment_id": "s1", "start_ms": 1000, "text": "原话"}]}],
            "resolutions": [{"text": "决议A"}],
            "action_items": [{"text": "待办A"}],
        }

    def down(state):
        runs.bump("down")
        # 返回一个已声明的业务字段，满足"至少要写一个键"
        return {"minutes_structured": {"ok": True}}

    g = StateGraph(S.PipelineState)
    g.add_node("extract", extract)
    g.add_node("gate_g2", I.make_gate_g2())
    g.add_node("down", down)
    g.add_edge(START, "extract")
    g.add_edge("extract", "gate_g2")
    g.add_conditional_edges(
        "gate_g2", I.route_after_g2, {"continue": "down", "redo": "extract"}
    )
    g.add_edge("down", END)
    return g.compile(checkpointer=InMemorySaver())


def _initial() -> dict:
    """首次 invoke 的初始状态。

    ⚠️ `TypedDict` + `total=False` 时**空 dict 会被拒**：
    `InvalidUpdateError: Must write to at least one of [...]`。
    必须给一个带初值的 dict。
    """
    return {I.GATE_ROUTE_KEY: None, "errors": []}


def _interrupt_value(app, cfg) -> dict:
    """取出当前挂起的中断载荷。

    用 `get_state(cfg).tasks[*].interrupts` 而不是 `invoke()` 的返回值 ——
    实测：首次 `invoke` 返回值里 `__interrupt__` 是 `None`，
    只有 `get_state` 能可靠拿到。
    """
    st = app.get_state(cfg)
    for task in st.tasks:
        for intr in task.interrupts or ():
            return intr.value
    raise AssertionError("当前没有挂起的中断")


@pytest.fixture()
def g2(tmp_path):
    runs = _Runs()
    app = _build_g2_app(runs)
    return app, runs


class TestGateRealGraph:
    """真跑图的闸口语义（U-ST-02/03/04/08）。"""

    def test_stops_at_gate_with_payload(self, g2):
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t1"}}
        app.invoke(_initial(), cfg)
        v = _interrupt_value(app, cfg)
        assert v["gate"] == "G2"
        assert v["gate_label"] == "待确认议题"
        assert v["risk"] == "high"
        assert v["payload"]["topics"][0]["title"] == "议题A"
        assert v["evidence"][0]["segment_id"] == "s1"
        assert runs.get("extract") == 1
        assert runs.get("down") == 0, "停在闸口时下游不该被执行"

    def test_approve_continues_without_rerunning(self, g2):
        """U-ST-02：approve **不重跑**已完成节点。"""
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t2"}}
        app.invoke(_initial(), cfg)
        app.invoke(Command(resume={"decision": "approve", "operator": "张三"}), cfg)
        assert runs.get("extract") == 1, "approve 不该重跑 extract"
        assert runs.get("down") == 1
        st = app.get_state(cfg)
        assert st.values[I.GATE_ROUTE_KEY] == S.ROUTE_CONTINUE
        assert st.next == ()

    def test_edit_injects_payload_without_rerunning(self, g2):
        """U-ST-03：edit 不重跑，且修改后的 payload 真的生效。

        这是**本卡最大的收益点**（降返工）。
        """
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t3"}}
        app.invoke(_initial(), cfg)
        app.invoke(
            Command(resume={
                "decision": "edit",
                "payload": {"topics": [{"title": "改过的议题"}]},
                "operator": "李四",
            }),
            cfg,
        )
        assert runs.get("extract") == 1, "edit 不该重跑 extract（降返工的关键）"
        assert runs.get("down") == 1
        st = app.get_state(cfg)
        assert st.values["topics"] == [{"title": "改过的议题"}], "edit 的 payload 未生效"
        assert st.values[I.GATE_ROUTE_KEY] == S.ROUTE_CONTINUE

    def test_edit_preserves_unmodified_fields(self, g2):
        """只改了 topics，resolutions/action_items 应保持原值。"""
        app, _ = g2
        cfg = {"configurable": {"thread_id": "t3b"}}
        app.invoke(_initial(), cfg)
        app.invoke(
            Command(resume={"decision": "edit", "payload": {"topics": [{"title": "改"}]}}),
            cfg,
        )
        st = app.get_state(cfg)
        assert st.values["resolutions"] == [{"text": "决议A"}]
        assert st.values["action_items"] == [{"text": "待办A"}]

    def test_reject_reruns_and_returns_to_gate(self, g2):
        """U-ST-04：reject **只重跑目标节点**，然后重新停下等确认。

        注意 reject 之后是"回到闸口再问一次"，不是"一路往下走" ——
        所以 extract 会被重跑，然后再次中断。
        """
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t4"}}
        app.invoke(_initial(), cfg)
        app.invoke(Command(resume={"decision": "reject", "comment": "议题不对"}), cfg)
        assert runs.get("extract") == 2, "reject 应该重跑 extract"
        assert runs.get("down") == 0, "reject 后不该往下走"
        st = app.get_state(cfg)
        assert st.values[I.GATE_REJECT_KEY] == 1
        assert st.next == ("gate_g2",), "驳回后应再次停在闸口"
        # 又挂起了（等人重新确认）
        v = _interrupt_value(app, cfg)
        assert v["gate"] == "G2"
        # 被驳回的产物已重建（extract 重跑写入了新值）
        assert st.values["topics"][0]["title"] == "议题A"

    def test_reject_clears_downstream_products(self, g2):
        """驳回清掉了下游产物（`down` 还没跑，所以 minutes_structured 仍是空）。"""
        app, _ = g2
        cfg = {"configurable": {"thread_id": "t4b"}}
        app.invoke(_initial(), cfg)
        app.invoke(Command(resume={"decision": "reject"}), cfg)
        st = app.get_state(cfg)
        assert st.values.get("minutes_structured") is None

    def test_approve_after_reject_works(self, g2):
        """驳回 → 再次确认通过，能正常走完（证明 reject 没把状态搞坏）。"""
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t5"}}
        app.invoke(_initial(), cfg)
        app.invoke(Command(resume={"decision": "reject"}), cfg)
        app.invoke(Command(resume={"decision": "approve"}), cfg)
        assert runs.get("extract") == 2
        assert runs.get("down") == 1
        assert app.get_state(cfg).next == ()

    def test_invalid_decision_rejected_at_gate(self, g2):
        """U-ST-08：非法决策拒绝（E4001）。"""
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t6"}}
        app.invoke(_initial(), cfg)
        with pytest.raises(MmwfError) as ei:
            app.invoke(Command(resume={"decision": "maybe"}), cfg)
        assert ei.value.code == "E4001"
        assert runs.get("down") == 0

    def test_edit_without_payload_rejected(self, g2):
        app, runs = g2
        cfg = {"configurable": {"thread_id": "t7"}}
        app.invoke(_initial(), cfg)
        with pytest.raises(MmwfError) as ei:
            app.invoke(Command(resume={"decision": "edit"}), cfg)
        assert ei.value.code == "E4001"
        assert runs.get("down") == 0

    def test_resume_needs_thread_id(self, g2):
        """不给 `thread_id` 就找不到状态 —— 确认这个约束是真的。"""
        app, _ = g2
        app.invoke(_initial(), {"configurable": {"thread_id": "t8"}})
        with pytest.raises(Exception):
            # 换一个 thread_id 恢复：找不到挂起的中断
            app.invoke(Command(resume={"decision": "approve"}),
                       {"configurable": {"thread_id": "another"}})

    def test_gate_node_has_no_side_effects(self, tmp_path):
        """U-ST-09：闸口节点**无副作用**。

        做法：给闸口节点套一个"审计写入"探针，让真正的写盘发生在
        下游节点。断言"闸口执行前后，探针写入次数不变"。

        为什么必须这样：`interrupt()` 恢复时**节点体会被重放**，
        节点里写审计会写两次。
        """
        writes: list[str] = []

        def audit_write(tag: str) -> None:
            writes.append(tag)

        def extract(state):
            return {"topics": [{"title": "A"}], "resolutions": [], "action_items": []}

        def gate(state):
            # 闸口：只组装载荷 + interrupt + 转状态增量（零副作用）
            node = I.make_gate_g2()
            return node(state)

        def down(state):
            # 审计写在**下游**（恢复之后），所以只发生一次
            audit_write("down")
            return {"minutes_structured": {}}

        g = StateGraph(S.PipelineState)
        g.add_node("extract", extract)
        g.add_node("gate_g2", gate)
        g.add_node("down", down)
        g.add_edge(START, "extract")
        g.add_edge("extract", "gate_g2")
        g.add_conditional_edges(
            "gate_g2", I.route_after_g2, {"continue": "down", "redo": "extract"}
        )
        g.add_edge("down", END)
        app = g.compile(checkpointer=InMemorySaver())

        cfg = {"configurable": {"thread_id": "side-effect"}}
        app.invoke(_initial(), cfg)
        assert writes == [], "停在闸口时不该有任何副作用"
        app.invoke(Command(resume={"decision": "approve"}), cfg)
        assert writes == ["down"], f"副作用次数应为 1，实际 {writes}"

    def test_gate_autopasses_when_payload_empty(self):
        """空载荷自动放行（E4003），不该让人确认一个空列表。"""
        runs = _Runs()

        def probe(state):
            runs.bump("probe")
            return {"topics": [], "resolutions": [], "action_items": []}

        def down(state):
            runs.bump("down")
            return {"minutes_structured": {}}

        g = StateGraph(S.PipelineState)
        g.add_node("probe", probe)
        g.add_node("gate_g2", I.make_gate_g2())
        g.add_node("down", down)
        g.add_edge(START, "probe")
        g.add_edge("probe", "gate_g2")
        g.add_conditional_edges(
            "gate_g2", I.route_after_g2, {"continue": "down", "redo": "probe"}
        )
        g.add_edge("down", END)
        app = g.compile(checkpointer=InMemorySaver())

        cfg = {"configurable": {"thread_id": "autopass"}}
        app.invoke(_initial(), cfg)
        st = app.get_state(cfg)
        assert st.next == (), "空载荷不该停在闸口"
        assert runs.get("down") == 1
        assert any(e["code"] == "E4003" for e in st.values.get("errors") or [])

    def test_reject_loop_guard_stops_infinite_confirm(self):
        """连续驳回超过上限后任务不再退回，而是报错（防死循环）。

        没有这道闸：reject -> extract -> gate_g2 -> reject -> …… 永动机。
        """
        runs = _Runs()

        def extract(state):
            runs.bump("extract")
            return {"topics": [{"title": "A"}], "resolutions": [], "action_items": []}

        g = StateGraph(S.PipelineState)
        g.add_node("extract", extract)
        g.add_node("gate_g2", I.make_gate_g2())

        def down(state):
            return {"minutes_structured": {}}

        g.add_node("down", down)
        g.add_edge(START, "extract")
        g.add_edge("extract", "gate_g2")
        g.add_conditional_edges(
            "gate_g2", I.route_after_g2, {"continue": "down", "redo": "extract"}
        )
        g.add_edge("down", END)
        app = g.compile(checkpointer=InMemorySaver())

        cfg = {"configurable": {"thread_id": "loop"}}
        app.invoke(_initial(), cfg)
        for _ in range(I.MAX_GATE_REJECTS):
            app.invoke(Command(resume={"decision": "reject"}), cfg)
        with pytest.raises(MmwfError) as ei:
            app.invoke(Command(resume={"decision": "reject"}), cfg)
        assert ei.value.code == "E4001"
        assert runs.get("extract") == I.MAX_GATE_REJECTS + 1

    def test_state_merging_keeps_partial_updates(self):
        """⚠️ 回归测试：状态**必须按键合并**，不能整体替换。

        这是本卡最贵的那个坑的守卫：`StateGraph(dict)` 会让节点返回的
        增量被丢掉，于是 `_gate_route` 读不到 → 兜底 `continue`
        → reject 静默变成"继续往下走"。这个 bug **不报错**，
        只是行为悄悄错了。
        """
        runs = _Runs()
        app = _build_g2_app(runs)
        cfg = {"configurable": {"thread_id": "merge"}}
        app.invoke(_initial(), cfg)
        app.invoke(Command(resume={"decision": "reject"}), cfg)
        st = app.get_state(cfg)
        assert st.values[I.GATE_ROUTE_KEY] == S.ROUTE_REDO, (
            "状态增量被丢弃了：检查 StateGraph 的 state_schema 是否为 TypedDict，"
            "以及 PipelineState 是否声明了 _gate_route"
        )
        assert st.values[I.GATE_REJECT_KEY] == 1

    def test_pipeline_state_covers_resume_initial_keys(self):
        """`_initial()` 里写的键必须都在 `PipelineState` 里，否则被丢。"""
        ann = S.PipelineState.__annotations__
        for k in _initial():
            assert k in ann, f"初始状态的键 {k} 未在 PipelineState 声明"
