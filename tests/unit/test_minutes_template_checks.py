"""U-MN-07/08/09：确定性规则边界与输入不变。"""

from copy import deepcopy

import pytest

from mmwf.domain.minutes import (
    check_required_elements, check_forbidden_expressions, check_reading_levels, check_template,
)
from mmwf.domain.template import MinutesTemplate, RequiredElement, TemplateFormatError


def tpl(key="attendees", **kw):
    return MinutesTemplate("test", "测试", "test", (RequiredElement(key, "参会人员", **kw),),
                           style={"readings_allowed": ["会议认为", "会议指出", "会议要求", "会议决定", "会议强调", "会议听取"],
                                  "readings_forbidden": ["大家觉得", "差不多", "ＡＢＣ", "abc"]})


@pytest.mark.parametrize("value", [None, "", "  ", [], [""], ["  "], ["待补充"], ["人员待补充"]])
def test_missing_attendees(value):
    issues = check_required_elements({"attendees": value}, tpl(min_count=2))
    assert issues == [{"rule": "missing_required_element", "field": "attendees", "detail": "参会人员缺失"}]


def test_missing_key_and_zero_minimum():
    assert check_required_elements({}, tpl(min_count=0))[0]["rule"] == "missing_required_element"
    assert check_required_elements({"attendees": ["甲"]}, tpl(min_count=0)) == []


@pytest.mark.parametrize("count,expected", [(1, ["required_min_count"]), (2, []), (3, [])])
def test_threshold(count, expected):
    assert [i["rule"] for i in check_required_elements({"attendees": ["甲"] * count}, tpl(min_count=2))] == expected


@pytest.mark.parametrize("key,text_key", [("topics", "name"), ("resolutions", "content"), ("action_items", "content"), ("pending_decisions", "content")])
def test_nested_counts_patterns_and_malformed_items(key, text_key):
    entries = [{text_key: " 合格 "}, {text_key: "待补充"}, {text_key: "错"}, 42, {text_key: 42}]
    minutes = {"topics": entries} if key == "topics" else {"topics": [{key: entries}]}
    issues = check_required_elements(minutes, tpl(key, min_count=3, pattern="^合格$"))
    assert [i["rule"] for i in issues] == ["required_type", "required_type", "required_min_count", "required_pattern"]
    assert issues[-1]["field"].endswith(f"[2].{text_key}")
    assert issues[-2]["actual"] == 2


def test_flat_resolutions_are_not_canonical():
    assert check_required_elements({"resolutions": [{"content": "议定"}]}, tpl("resolutions"))[0]["rule"] == "missing_required_element"


@pytest.mark.parametrize("key", ["title", "meeting_time", "location", "host", "recorder"])
@pytest.mark.parametrize("value,rule", [(None, "missing_required_element"), ("  ", "missing_required_element"), ("时间待补充", "missing_required_element"), (42, "required_type"), ("错", "required_pattern")])
def test_scalar(key, value, rule):
    assert [i["rule"] for i in check_required_elements({key: value}, tpl(key, pattern="^.{2,}$"))] == [rule]


@pytest.mark.parametrize("minutes,key,path", [({"attendees": "甲"}, "attendees", "attendees"), ({"topics": {}}, "resolutions", "topics"), ({"topics": [{"resolutions": "错"}]}, "resolutions", "topics[0].resolutions")])
def test_wrong_container(minutes, key, path):
    assert check_required_elements(minutes, tpl(key)) == [{"rule": "required_type", "field": path, "detail": "参会人员类型不正确"}]


@pytest.mark.parametrize("template", [tpl("unknown"), tpl("title", min_count=2)])
def test_bad_required_config(template):
    with pytest.raises(TemplateFormatError):
        check_required_elements({}, template)


def test_forbidden_normalization_dedup_and_evidence_exclusion():
    minutes = {"topics": [{"discussion": "大家觉得这个方案差 不 多，差不多 ABC ａｂｃ",
                           "source_quote": "大家觉得差不多", "refs": [{"text": "差不多"}]}],
               "sections": [{"items": ["差不多"]}], "issues": [{"detail": "差不多"}]}
    issues = check_forbidden_expressions(minutes, tpl())
    assert [i["matched"] for i in issues] == ["大家觉得", "差不多", "abc"]
    assert {i["field"] for i in issues} == {"topics[0].discussion"}
    assert check_forbidden_expressions({"topics": [{"source_quote": "差不多", "discussion": "差、不、多"}]}, tpl()) == []


@pytest.mark.parametrize("field", ["title", "meeting_time", "location", "host", "recorder", "attendees", "name", "discussion", "readings", "content", "owner", "deadline", "decision_maker"])
def test_canonical_scan_fields(field):
    minutes = {}
    if field in ("title", "meeting_time", "location", "host", "recorder"):
        minutes[field] = "差不多"
    elif field == "attendees":
        minutes[field] = ["差不多"]
    elif field in ("name", "discussion", "readings"):
        minutes["topics"] = [{field: ["差不多"] if field == "readings" else "差不多"}]
    else:
        minutes["topics"] = [{"pending_decisions": [{field: "差不多"}]}]
    assert len(check_forbidden_expressions(minutes, tpl())) == 1


@pytest.mark.parametrize("text,reading,rules", [
    ("请完成材料报送", "会议认为", ["reading_level_mismatch"]),
    ("请完成材料报送", "会议要求", []),
    ("要报送材料", "会议要求", []),
    ("形势总体稳定，必须落实责任", "会议认为", ["reading_level_mismatch"]),
    ("总体形势稳定。必须落实责任。", "会议认为", []),
    ("总体形势稳定。必须落实责任。", "会议要求", ["reading_level_mismatch"]),
    ("会议要求，工作已完成。必须落实责任。", "会议认为", []),
    ("总体形势稳定", "会议听取", ["reading_ambiguous"]),
    ("工作是稳定的", "会议听取", []),
    ("", "会议认为", []),
    ("工作已完成", "会议认为", []),
    ("工作已经推进", "会议认为", []),
    ("任务均已落实", "会议认为", []),
    ("材料已于昨日完成", "会议认为", []),
    ("正在抓紧办理", "会议认为", []),
    ("完成材料报送", "会议认为", ["reading_level_mismatch"]),
    ("重要意义，必须落实", "会议要求", []),
    ("必须落实，同意方案", "会议决定", []),
    ("重要意义", "会议强调", []),
    ("总体稳定", "会议审议通过", ["reading_not_allowed", "reading_level_mismatch"]),
    ("同意方案", "会议审议通过", ["reading_not_allowed"]),
    ("随手记下", "  ", ["reading_not_allowed"]),
])
def test_reading_rules(text, reading, rules):
    issues = check_reading_levels({"topics": [{"discussion": text, "readings": [reading]}]}, tpl())
    assert [i["rule"] for i in issues] == rules
    assert all(i["field"] == "topics[0].readings[0]" for i in issues)


@pytest.mark.parametrize("text,rules", [("必须办理", ["reading_missing"]), ("工作是稳定的", []), ("", [])])
def test_missing_readings(text, rules):
    assert [i["rule"] for i in check_reading_levels({"topics": [{"discussion": text}]}, tpl())] == rules


def test_aggregate_order_purity_and_topic_scope():
    template = tpl()
    minutes = {"topics": [{"discussion": "大家觉得总体形势差不多", "readings": ["会议要求"],
                           "resolutions": [{"content": "决定通过"}]}]}
    original, config = deepcopy(minutes), deepcopy(template)
    issues = check_template(minutes, template)
    assert [i["rule"] for i in issues] == ["missing_required_element", "forbidden_expression", "forbidden_expression", "reading_level_mismatch"]
    assert check_template(minutes, template) == issues
    assert minutes == original and template == config
    assert check_reading_levels({"topics": [{"discussion": "总体稳定", "readings": ["会议认为"], "resolutions": [{"content": "必须办理"}]}]}, template) == []


@pytest.mark.parametrize("text", ["决定", "议定", "审议通过", "同意"])
def test_decision_keywords(text):
    assert check_reading_levels({"topics": [{"discussion": text, "readings": ["会议决定"]}]}, tpl()) == []


@pytest.mark.parametrize("text", ["必须", "务必", "应当", "不得", "严禁", "完成", "推进", "落实", "确保", "抓紧"])
def test_imperative_keywords(text):
    assert check_reading_levels({"topics": [{"discussion": text, "readings": ["会议要求"]}]}, tpl()) == []


@pytest.mark.parametrize("text", ["重要性", "重要意义", "基本原则", "底线意识"])
def test_emphasis_keywords(text):
    assert check_reading_levels({"topics": [{"discussion": text, "readings": ["会议强调"]}]}, tpl()) == []


@pytest.mark.parametrize("text", ["属于", "表明", "说明", "呈现", "总体", "形势", "可行"])
def test_cognitive_keywords(text):
    assert check_reading_levels({"topics": [{"discussion": text, "readings": ["会议指出"]}]}, tpl()) == []


@pytest.mark.parametrize("separator", ["。", "！", "？", "；", "\n"])
def test_first_sentence_only(separator):
    assert check_reading_levels({"topics": [{"discussion": "总体稳定" + separator + "同意方案", "readings": ["会议认为"]}]}, tpl()) == []


def test_prefix_unicode_whitespace_and_four_character_window():
    assert check_reading_levels({"topics": [{"discussion": "会议要求\u3000：工作已完成。必须落实", "readings": ["会议认为"]}]}, tpl()) == []
    # 「已」距动词超过四字，不触发事实排除；规则不扩为语义推断。
    assert check_reading_levels({"topics": [{"discussion": "已于前天下午完成", "readings": ["会议要求"]}]}, tpl()) == []
    assert check_reading_levels({"topics": [{"discussion": "总体稳定", "readings": ["会议认为", "会议指出"]}]}, tpl()) == []


@pytest.mark.parametrize("nested", ["resolutions", "action_items", "pending_decisions"])
def test_scan_nested_fields_only(nested):
    minutes = {"topics": [{nested: [{"content": "差不多", "owner": "差不多", "deadline": "差不多",
                                    "source_quote": "差不多", "source_refs": ["差不多"],
                                    "source_start_ms": "差不多", "source_end_ms": "差不多"}]}]}
    issues = check_forbidden_expressions(minutes, tpl())
    assert [i["field"] for i in issues] == [f"topics[0].{nested}[0].{k}" for k in ("content", "owner", "deadline")]
