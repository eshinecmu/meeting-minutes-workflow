"""模板内容绑定、图适配与 U-MN-10 三级编号。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from mmwf.domain.minutes import apply_template, from_graph_payload, render_template_markdown
from mmwf.domain.template import TemplateFormatError
from mmwf.infra.template_store import TemplateStore

CONFIG = Path(__file__).resolve().parents[2] / "config"


def templates():
    loaded, problems = TemplateStore(":memory:", config_dir=CONFIG).load_builtin()
    assert not problems
    return loaded


def minutes():
    return {"title": "项目工作会议", "meeting_time": "周一", "location": "会议室", "host": "甲",
            "attendees": ["甲", "乙"], "recorder": "乙", "topics": [
                {"name": "议题甲", "discussion": "总体形势稳定", "readings": ["会议认为", "会议认为"],
                 "resolutions": [{"content": "同意方案甲", "owner": "暂无", "deadline": None,
                                  "source_quote": "原文", "refs": [{"segment_id": "s1"}]},
                                 {"content": "同意方案乙"}],
                 "action_items": [{"content": "报送材料", "owner": "乙"}],
                 "pending_decisions": [{"content": "经费方案", "decision_maker": "甲"}]},
                {"name": "议题乙", "discussion": "工作已完成", "readings": [],
                 "resolutions": [{"content": "同意方案丙"}]}]}


@pytest.mark.parametrize("template", templates(), ids=lambda t: t.meeting_kind)
def test_four_bindings_and_markdown(template):
    source = minutes()
    original, config = deepcopy(source), deepcopy(template)
    result = apply_template(source, template)
    assert [s["heading"] for s in result["sections"]] == list(template.section_order)
    assert [s["number"] for s in result["sections"]] == ["1", "2", "3", "4"]
    assert result["topics"] == source["topics"]
    assert result["sections"][2]["items"][0]["items"][0]["refs"] == [{"segment_id": "s1"}]
    assert result["sections"][2]["items"][0]["items"][1]["number"] == "3.1.2"
    assert result["sections"][2]["items"][1]["items"][0]["number"] == "3.2.1"
    assert result["sections"][3]["items"][0]["items"][0]["number"] == "4.1.1"
    md = render_template_markdown(result, [{"detail": "参会人员缺失"}])
    assert [line[3:] for line in md.splitlines() if line.startswith("## ")] == [*template.section_order, "待核对清单"]
    assert "会议认为，会议认为" not in md and "会议认为，总体形势稳定" in md
    assert "待决策：经费方案" in md and "决策人：甲" in md
    assert "### 3.1" not in md and "####" not in md
    assert source == original and template == config


def test_empty_sections_and_old_template():
    template = templates()[0]
    result = apply_template({}, template)
    assert len(result["sections"]) == 4
    assert render_template_markdown(result, []).count("待补充") >= 4
    with pytest.raises(TemplateFormatError, match="section_bindings"):
        apply_template({}, replace(template, style={}))


def test_adapter_authoritative_lists_and_no_invented_evidence():
    payload = {"topics": [{"title": "甲", "discussion": "编辑后", "readings": ["会议认为"],
                           "resolutions": [{"content": "旧事项"}]}],
               "resolutions": [{"topic": "甲", "text": "新决议", "refs": [{"segment_id": "s"}]}],
               "action_items": [{"topic": "甲 ", "text": "孤立待办", "quote": "原话"}],
               "minutes": {"title": "旧元信息"}}
    original = deepcopy(payload)
    result = from_graph_payload(payload, meeting_meta={"host": "主持"})
    assert result["title"] == "" and result["host"] == "主持"
    assert result["topics"][0]["discussion"] == "编辑后"
    assert result["topics"][0]["readings"] == ["会议认为"]
    assert [r["content"] for r in result["topics"][0]["resolutions"]] == ["新决议"]
    assert result["topics"][-1]["name"] == "待补充"
    item = result["topics"][-1]["action_items"][0]
    assert item["content"] == "孤立待办" and item["quote"] == "原话"
    assert "source_quote" not in item and "source_start_ms" not in item
    assert payload == original


def test_numbering_example_one_one_two_one():
    template = templates()[0]
    template = replace(template, section_order=("决议",), style={"section_bindings": {"决议": "resolutions"}})
    sections = apply_template(minutes(), template)["sections"]
    assert sections[0]["number"] == "1"
    assert sections[0]["items"][0]["number"] == "1.1"
    assert sections[0]["items"][1]["items"][0]["number"] == "1.2.1"
