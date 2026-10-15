"""最终粒度规则：边界、证据保留与重复执行。"""
from copy import deepcopy

import pytest

from mmwf.domain.minutes.granularity import normalize_granularity
from mmwf.domain.extraction.merge import similarity


def data(kind="resolutions", *items):
    return {"topics": [{kind: list(items)}]}


def entries(result, kind="resolutions"):
    return result.minutes["topics"][0][kind]


@pytest.mark.parametrize("kind", ["resolutions", "action_items", "pending_decisions"])
@pytest.mark.parametrize("owner", ["missing", None, "  "])
def test_owner_all_types(kind, owner):
    item = {"content": "完成报告", "deadline": "下周"}
    if owner != "missing":
        item["owner"] = owner
    original = data(kind, item)
    before = deepcopy(original)
    result = normalize_granularity(original)
    assert entries(result, kind)[0]["owner"] == "暂无"
    assert any(i["code"] == "OWNER_DEFAULTED" and i["level"] == "info" for i in result.issues)
    assert original == before
    second = normalize_granularity(result.minutes)
    assert second.minutes == result.minutes
    assert second.issues == result.issues and not second.changes


@pytest.mark.parametrize("word", ["并", "以及", "同时"])
@pytest.mark.parametrize("length", [60, 61])
def test_split_unicode_boundary(word, length):
    content = "甲" * 30 + word + "乙" * (length - 30 - len(word))
    refs = [{"segment_id": "seg-1", "quote": "原文"}]
    result = normalize_granularity(data("resolutions", {"id": "r", "content": content, "owner": "王五", "deadline": "下周", "source_refs": refs}))
    items = entries(result)
    assert len(items) == (2 if length == 61 else 1)
    if length == 61:
        assert [i["id"] for i in items] == ["r~1", "r~2"]
        assert all(i["source_refs"] == refs and i["deadline"] == "下周" for i in items)
        assert all(m["original_content"] == content for m in result.minutes["_context"]["granularity_items"].values())
        assert all("_granularity" not in i for i in items)
    again = normalize_granularity(result.minutes)
    assert again.minutes == result.minutes and not again.changes


@pytest.mark.parametrize("content,deadline,downgrade", [
    ("预算事项", "下周", True), ("完成报告", None, True),
    ("完成报告", "下周", False), ("完成前景报告", "于", True),
    ("完成报告", "尽快", False), ("完成报告", "2026-10-14", False),
    ("完成报告", "三天", False), ("完成报告", "年内", False),
])
def test_action_or_condition(content, deadline, downgrade):
    result = normalize_granularity(data("action_items", {"content": content, "deadline": deadline}))
    assert bool(entries(result, "pending_decisions")) is downgrade
    if downgrade:
        assert entries(result, "pending_decisions")[0]["decision_maker"] == "暂无"
        assert any(i["code"] == "ACTION_DOWNGRADED" and not i["blocking"] for i in result.issues)
    if deadline == "尽快":
        assert any(i["message"] == "时限待明确" for i in result.issues)
    assert normalize_granularity(result.minutes).minutes == result.minutes


def test_merge_union_and_no_overlap_needed():
    a = {"content": "完成报告", "owner": "暂无", "source_refs": [{"segment_id": "seg-1", "quote": "甲"}]}
    b = {"content": "完成报告", "owner": "王五", "source_refs": [{"segment_id": "seg-1", "quote": "乙"}]}
    result = normalize_granularity(data("resolutions", a, b))
    assert len(entries(result)) == 1
    assert entries(result)[0]["owner"] == "王五"
    assert len(entries(result)[0]["source_refs"]) == 2
    assert not normalize_granularity(result.minutes).changes


def test_strict_point_nine():
    a, b = "甲乙丙丁戊己庚辛壬癸", "甲乙丙丁戊己庚辛壬癸子"
    assert similarity(a, b) == .9
    assert len(entries(normalize_granularity(data("resolutions", {"content": a}, {"content": b})))) == 2


@pytest.mark.parametrize("field,one,two", [("owner", "张三", "李四"), ("deadline", "下周", "本周"),
                                           ("content", "甲" * 30 + "预算30万元", "甲" * 30 + "预算40万元"),
                                           ("content", "甲" * 30 + "请张三同志负责", "甲" * 30 + "请李四同志负责")])
def test_conflicts_retained(field, one, two):
    a = {"content": "甲" * 30 + "报告", field: one}
    b = {"content": "甲" * 30 + "报告", field: two}
    # 正文冲突也需要满足相似度前置条件。
    if field == "content":
        prefix = "".join(chr(0x4e00 + i) for i in range(200))
        a[field], b[field] = prefix + one[30:], prefix + two[30:]
    result = normalize_granularity(data("resolutions", a, b))
    assert len(entries(result)) == 2
    assert any(i["code"] == "DUPLICATE_CONFLICT" for i in result.issues)


def test_empty_content_and_different_topics_do_not_merge():
    assert len(entries(normalize_granularity(data("resolutions", {"content": ""}, {"content": ""})))) == 2
    original = {"topics": [{"resolutions": [{"content": "通过"}]}] * 2}
    assert len(normalize_granularity(original).minutes["topics"]) == 2


def test_group_first_not_transitive_and_idempotent_longest_content():
    a = "".join(chr(0x5100 + i) for i in range(100))
    b, c = a + "甲乙", a + "甲乙丙丁戊己庚辛壬癸子"
    assert similarity(a, b) > .9 and similarity(b, c) > .9 and similarity(a, c) <= .9
    result = normalize_granularity(data("resolutions", *[{"content": text} for text in (a, b, c)]))
    assert [i["content"] for i in entries(result)] == [b, c]
    again = normalize_granularity(result.minutes)
    assert again.minutes == result.minutes and again.issues == result.issues and not again.changes


def test_split_all_three_connectors_left_to_right():
    text = "甲" * 61 + "以及，乙同时丙并丁"
    result = normalize_granularity(data("resolutions", {"id": "x", "content": text}))
    assert [i["content"] for i in entries(result)] == ["甲" * 61, "乙", "丙", "丁"]
    assert [i["id"] for i in entries(result)] == ["x~1", "x~2", "x~3", "x~4"]
    assert normalize_granularity(result.minutes).minutes == result.minutes


def test_correction_target_after_move_keeps_original_path():
    result = normalize_granularity(data("action_items", {"content": "没有时限"}))
    owner = next(i for i in result.issues if i["code"] == "OWNER_DEFAULTED")
    assert owner["target"] == "topics[0].pending_decisions[0]"
    assert owner["evidence"]["original_path"] == "topics[0].action_items[0]"
    assert normalize_granularity(result.minutes).issues == result.issues


def test_split_downgrade_targets_individual_children():
    result = normalize_granularity(data("action_items", {"content": "甲" * 61 + "并乙事项"}))
    issues = [i for i in result.issues if i["code"] == "ACTION_DOWNGRADED"]
    assert {i["target"] for i in issues} == {"topics[0].pending_decisions[0]", "topics[0].pending_decisions[1]"}
    assert normalize_granularity(result.minutes).issues == result.issues


@pytest.mark.parametrize("one,two,merge", [("约30万", "约30万", True),
                                         ("约30万", "约40万", False),
                                         ("约30万", "预算事项", True)])
def test_unsupported_numbers_conflict_only_when_both_differ(one, two, merge):
    prefix = "".join(chr(0x5300 + i) for i in range(200))
    result = normalize_granularity(data("resolutions", {"content": prefix + one}, {"content": prefix + two}))
    assert len(entries(result)) == (1 if merge else 2)
    assert any(i["code"] == "DUPLICATE_CONFLICT" for i in result.issues) is not merge
    again = normalize_granularity(result.minutes)
    assert again.minutes == result.minutes and again.issues == result.issues and not again.changes


def test_bookkeeping_stays_in_context_without_history_growth_or_render_leak():
    from mmwf.domain.minutes import apply_template, render_template_markdown
    from mmwf.domain.template import MinutesTemplate

    original = data("resolutions", {"content": "甲" * 61 + "以及乙事项"})
    result = normalize_granularity(original)
    for _ in range(10):
        again = normalize_granularity(result.minutes)
        assert again.minutes == result.minutes and again.issues == result.issues and not again.changes
    for item in entries(result):
        assert not set(item) & {"_granularity", "dedup_anchor", "parent_path", "original_content", "split_child"}
    assert result.minutes["_context"]["granularity_items"]
    assert result.minutes["_context"]["granularity_changes"]
    template = MinutesTemplate("test", "测试", "test", (), section_order=("决议",),
                               style={"section_bindings": {"决议": "resolutions"}})
    rich = apply_template(result.minutes, template)
    canonical = deepcopy(result.minutes)
    del canonical["_context"]
    # 现有渲染器按业务字段输出；上下文有无不影响最终正文。
    assert render_template_markdown(rich, result.issues) == render_template_markdown(
        apply_template(canonical, template), result.issues)


def test_action_without_verb_and_deadline_is_downgraded():
    refs = [{"segment_id": "seg-1", "quote": "预算事项"}]
    result = normalize_granularity(data("action_items", {
        "content": "预算事项", "owner": "王五", "source_refs": refs,
    }))
    assert entries(result, "action_items") == []
    pending = entries(result, "pending_decisions")
    assert len(pending) == 1
    assert pending[0]["content"] == "预算事项"
    assert pending[0]["source_refs"] == refs
    assert any(i["code"] == "ACTION_DOWNGRADED" for i in result.issues)


def test_vague_deadline_preserves_original_text():
    result = normalize_granularity(data("action_items", {
        "content": "完成报告", "owner": "王五", "deadline": "尽快",
    }))
    assert entries(result, "action_items")[0]["deadline"] == "尽快"
    assert entries(result, "pending_decisions") == []
    assert any(i["message"] == "时限待明确" for i in result.issues)
