"""旧状态副本升级与丰富原料的边界。"""
from copy import deepcopy

from mmwf.domain.minutes.state_compat import upgrade_legacy_state
from mmwf.domain.minutes.post_validation import validate_hard


def test_upgrade_rebinds_timestamp_and_preserves_snapshot():
    state = {"record_markdown": "[00:12] 张三：决定实施。", "topics": [],
             "resolutions": [{"text": "决定实施。", "refs": [{"start_ms": 12000, "text": "伪造"}]}],
             "action_items": [], "minutes": {"title": "已有标题", "_context": {"template_snapshot": {"a": 1}}}}
    before = deepcopy(state)
    got = upgrade_legacy_state(state, {"meeting_meta": {"title": "会议资料"}})
    assert state == before
    assert got["minutes"]["title"] == "已有标题"
    assert got["minutes"]["_context"]["template_snapshot"] == {"a": 1}
    ref = got["minutes"]["topics"][0]["resolutions"][0]["source_refs"][0]
    assert ref["quote"] == "决定实施。"
    assert ref["end_ms"] is None
    assert not any(i["blocking"] for i in validate_hard(got["minutes"], record_markdown=state["record_markdown"]))


def test_g2_replaces_items_but_keeps_pending_and_reports_unmapped():
    old = {"topics": [{"name": "旧议题", "pending_decisions": [{"content": "待核对"}]}]}
    got = upgrade_legacy_state({"minutes": old, "topics": [{"title": "新议题"}],
                               "resolutions": [{"text": "新决议"}], "action_items": []})
    assert got["minutes"]["topics"][-1]["pending_decisions"] == old["topics"][0]["pending_decisions"]
    assert any(i["code"] == "PENDING_UNMAPPED" for i in got["issues"])
    assert any(i["code"] == "META_MISSING" for i in got["issues"])


def test_sections_missing_source_and_forged_refs_cannot_become_evidence():
    got = upgrade_legacy_state({"minutes_structured": {"sections": [{"heading": "议题", "items": ["决定实施"]}]}})
    assert any(i["code"] == "SOURCE_UNAVAILABLE" for i in validate_hard(got["minutes"]))
    got = upgrade_legacy_state({"topics": [{"title": "决定实施", "refs": [{"start_ms": 42, "text": "决定实施"}]}]})
    assert any(i["blocking"] for i in validate_hard(got["minutes"]))
