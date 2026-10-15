"""C3 领域校验：保留证据、规范化与正文隔离的字段摘要。"""
from copy import deepcopy
import pytest

from mmwf.domain.errors import MmwfError
from mmwf.domain.review.gate_edit import validate_edit, gate_content, field_diff, fingerprint, canonical_json


@pytest.fixture
def baseline():
    return {"topics": [{"id": "t1", "title": "预算", "discussion": "讨论", "readings": ["材料"],
                        "refs": [{"segment_id": "s1"}], "confidence": 0.7}],
            "resolutions": [{"text": "通过", "topic": "预算", "owner": "", "deadline": None}],
            "action_items": []}


def test_normalisation_and_title_association(baseline):
    edit = deepcopy(baseline["topics"])
    edit[0]["title"] = "预算安排"
    after = validate_edit("G2", baseline, {"topics": edit})
    assert after["resolutions"][0] == {"text": "通过", "topic": "预算安排", "owner": "暂无", "deadline": None}
    changes = field_diff(gate_content("G2", baseline), after)
    assert list(changes) == ["resolutions[0].topic", "topics[0].title"]
    change = changes["topics[0].title"]
    assert change == {"op": "replace", "old_sha256": fingerprint("预算"), "new_sha256": fingerprint("预算安排"),
                      "old_chars": len(canonical_json("预算")), "new_chars": len(canonical_json("预算安排"))}
    assert "预算" not in canonical_json(changes)
    assert baseline["topics"][0]["title"] == "预算"


@pytest.mark.parametrize("key,value", [("title", " "), ("title", 2), ("discussion", None), ("readings", "x"),
                                       ("readings", [1]), ("refs", []), ("confidence", 1), ("id", "t2"),
                                       ("new_metadata", True)])
def test_bad_fields(baseline, key, value):
    items = deepcopy(baseline["topics"])
    items[0][key] = value
    with pytest.raises(MmwfError) as exc:
        validate_edit("G2", baseline, {"topics": items})
    assert exc.value.code == "E4001"


@pytest.mark.parametrize("payload", [None, {}, [], {"unknown": []}, {"topics": "x"}, {"topics": [1]}, {"topics": None}])
def test_payload_shape(baseline, payload):
    with pytest.raises(MmwfError):
        validate_edit("G2", baseline, payload)


def test_noop_and_empty(baseline):
    before = gate_content("G2", baseline)
    assert field_diff(before, validate_edit("G2", baseline, {"topics": baseline["topics"]})) == {}
    with pytest.raises(MmwfError) as exc:
        validate_edit("G2", baseline, {"topics": [], "resolutions": [], "action_items": []})
    assert exc.value.code == "E4003"
    assert validate_edit("G2", {"topics": [], "resolutions": [{"text": "决议"}], "action_items": []}, {"topics": []})["topics"] == []


@pytest.mark.parametrize("change", ["append", "delete", "reorder", "duplicate", "readonly_delete"])
def test_identity(baseline, change):
    baseline["topics"].append(dict(deepcopy(baseline["topics"][0]), id="t2", title="进度"))
    items = deepcopy(baseline["topics"])
    if change == "append":
        items.append(dict(items[0], id="t3"))
    elif change == "delete":
        items.pop()
    elif change == "reorder":
        items.reverse()
    elif change == "duplicate":
        items[1]["id"] = "t1"
    else:
        del items[0]["refs"]
    with pytest.raises(MmwfError):
        validate_edit("G2", baseline, {"topics": items})


def test_duplicate_title_ambiguous(baseline):
    baseline["topics"].append(dict(deepcopy(baseline["topics"][0]), id="t2"))
    edit = deepcopy(baseline["topics"])
    edit[0]["title"] = "新标题"
    with pytest.raises(MmwfError):
        validate_edit("G2", baseline, {"topics": edit})


@pytest.mark.parametrize("payload", [{"minutes_markdown": ""}, {"minutes_markdown": None}, {"minutes_markdown": 3},
                                      {"minutes_markdown": "正文", "topics": []}])
def test_g3_invalid(payload):
    with pytest.raises(MmwfError):
        validate_edit("G3", {"minutes_markdown": "稿"}, payload)


def test_g3_newlines():
    after = validate_edit("G3", {"minutes_markdown": "甲\n乙"}, {"minutes_markdown": "甲\r\n乙"})
    assert field_diff({"minutes_markdown": "甲\n乙"}, after) == {}


def test_renaming_to_existing_title_is_ambiguous(baseline):
    baseline["topics"].append(dict(deepcopy(baseline["topics"][0]), id="t2", title="进度"))
    edit = deepcopy(baseline["topics"])
    edit[0]["title"] = "进度"
    with pytest.raises(MmwfError):
        validate_edit("G2", baseline, {"topics": edit})


@pytest.mark.parametrize("name", ["resolutions", "action_items"])
def test_deadline_can_be_cleared(baseline, name):
    baseline[name] = [{"text": "提交材料", "deadline": "月底"}]
    items = deepcopy(baseline[name])
    items[0]["deadline"] = None
    after = validate_edit("G2", baseline, {name: items})
    assert after[name][0]["deadline"] is None
    changes = field_diff(gate_content("G2", baseline), after)
    assert list(changes) == [f"{name}[0].deadline"]
    assert changes[f"{name}[0].deadline"]["new_sha256"] == fingerprint(None)
