"""`graph.state` 单元测试（T1-10）。

这个模块是"词汇表"，所以测试的重点不是算法，而是**一致性**：
三张表的键必须互相对齐，少一行就会在运行时表现为"界面显示内部状态名"
或者"节点没有对应状态"，这类问题在人工抽查里极难发现。
"""

from __future__ import annotations

import pytest

from mmwf.graph import state as S


class TestStatusVocabulary:
    def test_status_count_is_sixteen(self):
        """SAD §5.2 定死 16 个状态。数量变了必须回头改文档。"""
        assert len(S.GRAPH_STATUS) == 16

    def test_statuses_are_unique(self):
        assert len(set(S.GRAPH_STATUS)) == len(S.GRAPH_STATUS)

    def test_every_status_has_a_label(self):
        """状态与文案表必须一一对应。

        新增状态忘了写文案，界面上就会直接显示 `G3_awaiting`
        这种内部字符串 —— N3.3 明令禁止。
        """
        assert set(S.GRAPH_STATUS_LABEL) == set(S.GRAPH_STATUS)

    def test_labels_are_non_empty_and_have_no_engineering_words(self):
        """文案不得含工程词（N3.3）。

        逐个查了几个本项目里实际会漏出去的词。这些词一旦出现在界面上，
        非技术用户不知道是什么意思。
        """
        banned = ("checkpoint", "schema", "幂等", "HITL", "G2", "G3",
                  "node", "status", "state", "_")
        for status, label in S.GRAPH_STATUS_LABEL.items():
            assert label, f"{status} 的文案为空"
            low = label.lower()
            for word in banned:
                assert word.lower() not in low, (
                    f"文案 {label!r}（{status}）含工程词 {word!r}"
                )

    def test_terminal_statuses_are_subset_of_all(self):
        assert S.TERMINAL_STATUSES <= set(S.GRAPH_STATUS)

    def test_terminal_statuses_are_exactly_three(self):
        assert S.TERMINAL_STATUSES == {
            S.STATUS_ARCHIVED, S.STATUS_FAILED, S.STATUS_CANCELLED,
        }

    def test_gate_statuses_are_subset_of_all(self):
        assert S.GATE_STATUSES <= set(S.GRAPH_STATUS)

    def test_gate_and_terminal_do_not_overlap(self):
        """停在闸口等人 ≠ 终态。重叠了会让"任务已结束"的判定出错。"""
        assert not (S.GATE_STATUSES & S.TERMINAL_STATUSES)

    @pytest.mark.parametrize("status", ["G2_awaiting", "G3_awaiting"])
    def test_gate_status_says_awaiting_not_doing(self, status):
        """闸口状态用 `awaiting` 而不是动作的 -ing 形式。

        命名语义是"停在这里等人"，不是"正在做某事"。这条断言看起来
        很学究，但它保护的是**读代码时的判断**：看到 `generating`
        知道是机器在跑，看到 `G2_awaiting` 知道是人在拖 ——
        排查"任务为什么卡住"时这两者的处理方式完全不同。
        """
        assert status.endswith("_awaiting")
        assert not status.startswith("gate")

    def test_only_gate_statuses_use_awaiting(self):
        """`awaiting` 是闸口专用词。别处用了会把"等人"和"等机器"混淆。"""
        using_awaiting = {
            s for s in S.GRAPH_STATUS if "awaiting" in s
        }
        assert using_awaiting == S.GATE_STATUSES

    def test_non_gate_statuses_never_use_awaiting(self):
        for s in S.GRAPH_STATUS:
            if s not in S.GATE_STATUSES:
                assert "awaiting" not in s

    def test_status_label_known(self):
        assert S.status_label(S.STATUS_G2_AWAITING) == "待确认议题"

    def test_status_label_unknown_passes_through(self):
        """未知状态原样返回 —— 报错会让界面更难看，且不可搜索。"""
        assert S.status_label("no_such_status") == "no_such_status"


class TestNodeVocabulary:
    def test_node_sequence_is_eleven(self):
        """SAD §5.1 的主链路是 11 个节点。"""
        assert len(S.NODE_SEQUENCE) == 11

    def test_node_sequence_order_matches_sad(self):
        """顺序是 SAD §5.1 逐字对应的，改动必须同步改文档。"""
        assert S.NODE_SEQUENCE == (
            "ingest", "transcribe", "review", "extract", "gate_g2",
            "generate", "validate", "assemble", "gate_g3", "export", "archive",
        )

    def test_nodes_are_unique(self):
        assert len(set(S.NODE_SEQUENCE)) == len(S.NODE_SEQUENCE)

    def test_every_node_has_a_status(self):
        assert set(S.NODE_STATUS) == set(S.NODE_SEQUENCE)

    def test_every_node_status_is_declared(self):
        for node, status in S.NODE_STATUS.items():
            assert status in S.GRAPH_STATUS, f"{node} 的状态 {status} 不在状态表里"

    def test_every_node_has_outputs_entry(self):
        """`NODE_OUTPUTS` 少一个节点，`reject` 时就漏清一个产物。"""
        assert set(S.NODE_OUTPUTS) == set(S.NODE_SEQUENCE)

    def test_every_node_has_requires_entry(self):
        assert set(S.NODE_REQUIRES) == set(S.NODE_SEQUENCE)

    def test_gate_nodes_have_no_outputs(self):
        """闸口节点**无产物**：它只做决策，不生产内容。

        一旦给闸口声明了产物，`reject` 的"清下游产物"就会把闸口
        自己的东西也清掉，而紧跟着还要再进一次闸口 —— 逻辑上自相矛盾。
        """
        for gate_node in S.GATE_NODES:
            assert S.NODE_OUTPUTS[gate_node] == ()

    def test_gate_nodes_require_gate_payload(self):
        """闸口必须声明它要审的东西，否则会成为"无条件放行"。"""
        assert S.NODE_REQUIRES["gate_g2"] == (
            "topics", "resolutions", "action_items",
        )
        assert S.NODE_REQUIRES["gate_g3"] == ("minutes_markdown",)

    def test_ingest_requires_nothing(self):
        """首节点没有前置输入 —— 它的输入是用户投递的文件，不在状态里。"""
        assert S.NODE_REQUIRES["ingest"] == ()

    def test_gate_nodes_mapping(self):
        assert S.GATE_NODES == {"gate_g2": "G2", "gate_g3": "G3"}

    def test_gate_labels_cover_all_gates(self):
        assert set(S.GATE_LABEL) == set(S.GATE_NODES.values())

    def test_gate_risk_cover_all_gates(self):
        assert set(S.GATE_RISK) == set(S.GATE_NODES.values())

    def test_g2_is_high_risk(self):
        """G2 是最高优先级闸口（议题错了后面全错）。"""
        assert S.GATE_RISK["G2"] == "high"
        assert S.GATE_RISK["G3"] == "medium"

    def test_outputs_reference_declared_state_fields(self):
        """节点声明的产物必须真的在 `PipelineState` 里。"""
        declared = set(S.PipelineState.__annotations__)
        for node, outs in S.NODE_OUTPUTS.items():
            for field in outs:
                assert field in declared, f"{node} 声明的产物 {field} 不在 PipelineState"

    def test_requires_reference_declared_state_fields(self):
        declared = set(S.PipelineState.__annotations__)
        for node, reqs in S.NODE_REQUIRES.items():
            for field in reqs:
                assert field in declared, f"{node} 要求的输入 {field} 不在 PipelineState"


class TestPipelineStateDeclaresInternalFields:
    """⚠️ 内部分支字段必须显式声明，否则 LangGraph 静默丢弃。

    这是本卡实测踩到的坑，详见 state.py 里的注释与
    2026-09-27 的记忆日志。没有这个声明，`reject` 不会退回，
    而是静默往下走 —— 错误决策被接受。
    """

    def test_gate_route_is_declared(self):
        assert "_gate_route" in S.PipelineState.__annotations__

    def test_gate_rejects_is_declared(self):
        assert "_gate_rejects" in S.PipelineState.__annotations__

    def test_state_is_total_false(self):
        """必须是 `total=False`，否则节点得返回全部字段（不可能）。"""
        assert S.PipelineState.__total__ is False

    def test_state_has_identity_fields(self):
        ann = S.PipelineState.__annotations__
        for f in ("task_id", "meeting_id", "stage"):
            assert f in ann


class TestGateRoutingTables:
    def test_valid_decisions_exactly_three(self):
        assert S.VALID_DECISIONS == {"approve", "reject", "edit"}

    def test_every_gate_covers_every_decision(self):
        """漏一个决策，`GATE_ROUTE[gate][decision]` 就会 KeyError。"""
        for gate, table in S.GATE_ROUTE.items():
            assert set(table) == S.VALID_DECISIONS, f"{gate} 的决策覆盖不全"

    def test_approve_and_edit_both_continue(self):
        """**这是降返工的核心不变量**：approve 与 edit 都不重跑上游。

        两者在路由上完全等价，差别只在 edit 会注入修改后的 payload。
        如果哪天有人给 edit 改成 `redo`，返工时间会立刻回到"整篇重来"，
        而路由表这个改动在 review 时看起来很无害。
        """
        for gate, table in S.GATE_ROUTE.items():
            assert table["approve"] == S.ROUTE_CONTINUE
            assert table["edit"] == S.ROUTE_CONTINUE
            assert table["reject"] == S.ROUTE_REDO

    def test_routes_are_known_values(self):
        for table in S.GATE_ROUTE.values():
            for route in table.values():
                assert route in (S.ROUTE_CONTINUE, S.ROUTE_REDO)

    def test_redo_targets_are_declared_nodes(self):
        for target in S.GATE_REDO_TARGET.values():
            assert target in S.NODE_SEQUENCE

    def test_redo_targets_are_upstream_of_the_gate(self):
        """退回目标必须在闸口**上游**，否则"退回"会退到未来。"""
        for gate, target in S.GATE_REDO_TARGET.items():
            gate_node = f"gate_{gate.lower()}"
            assert S.NODE_SEQUENCE.index(target) < S.NODE_SEQUENCE.index(gate_node), (
                f"{gate} 的退回目标 {target} 不在其上游"
            )

    def test_g2_redo_targets_extract(self):
        """G2 驳回重抽议题（SAD §5.3）。"""
        assert S.GATE_REDO_TARGET["G2"] == S.NODE_EXTRACT

    def test_g3_redo_targets_assemble(self):
        """G3 驳回只重新成文 —— 议题本身已经确认过了。"""
        assert S.GATE_REDO_TARGET["G3"] == S.NODE_ASSEMBLE

    def test_g3_redo_does_not_go_back_to_g2(self):
        """G3 驳回**不能**退回 G2：那等于让已确认的议题重来一遍。"""
        assert S.GATE_REDO_TARGET["G3"] != S.NODE_EXTRACT
        assert S.NODE_SEQUENCE.index(S.GATE_REDO_TARGET["G3"]) > \
            S.NODE_SEQUENCE.index(S.NODE_GATE_G2)


class TestDecisionLabels:
    def test_labels_cover_all_decisions(self):
        assert set(S.DECISION_LABEL) == S.VALID_DECISIONS

    def test_reject_and_edit_read_differently(self):
        """场景文档点名的文案要求（N3.3）。

        `reject` 必须让用户看出"要重做"，`edit` 必须让用户看出
        "不用重来"。两者文案如果一样，用户就不会去用 edit，
        本卡最大的收益（降返工）直接归零。
        """
        assert S.DECISION_LABEL["reject"] == "退回重做"
        assert S.DECISION_LABEL["edit"] == "改完继续"
        assert S.DECISION_LABEL["reject"] != S.DECISION_LABEL["edit"]

    def test_labels_have_no_engineering_words(self):
        for decision, label in S.DECISION_LABEL.items():
            assert label.strip() == label
            assert "approve" not in label.lower()
