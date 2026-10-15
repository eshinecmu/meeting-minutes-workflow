"""真实四模板与抽取 schema、sections 消费之间的离线合同。"""

from pathlib import Path

from mmwf.domain.extraction.schemas import MinutesSchema
from mmwf.domain.minutes import apply_template, check_template, render_template_markdown
from mmwf.graph.nodes.extraction import naive_assemble
from mmwf.infra.template_store import TemplateStore


def test_real_templates_schema_and_sections_contract():
    config = Path(__file__).resolve().parents[2] / "config"
    templates, problems = TemplateStore(":memory:", config_dir=config).load_builtin()
    assert len(templates) == 4 and not problems
    minutes = MinutesSchema.model_validate({
        "title": "项目工作会议纪要", "meeting_time": "2026-10-14", "location": "会议室",
        "host": "甲", "attendees": ["甲", "乙"], "recorder": "乙",
        "topics": [{"name": "项目方案", "discussion": "总体形势稳定。", "readings": ["会议认为"],
                    "resolutions": [{"content": "同意项目方案", "source_quote": "大家觉得差不多",
                                     "source_start_ms": None, "source_end_ms": None}]}],
    }).model_dump()
    for template in templates:
        assert check_template(minutes, template) == []
        structured = apply_template(minutes, template)
        assert MinutesSchema.model_validate(structured).model_dump() == minutes
        for render in (render_template_markdown, naive_assemble):
            md = render(structured, [])
            assert [line[3:] for line in md.splitlines() if line.startswith("## ")] == list(template.section_order)
        assert set(structured) == set(minutes) | {"sections"}
        assert all({"heading", "items"} <= set(s) for s in structured["sections"])
