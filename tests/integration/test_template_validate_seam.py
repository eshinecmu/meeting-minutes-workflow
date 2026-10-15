"""显式节点工厂接缝，不改变生产图装配或状态合同。"""

from pathlib import Path

from mmwf.domain.minutes import apply_template, check_template, from_graph_payload, render_template_markdown
from mmwf.graph import state as S
from mmwf.graph.nodes.extraction import make_generate, make_validate, make_assemble
from mmwf.infra.template_store import TemplateStore


def test_template_generate_validate_assemble(tmp_path):
    config = Path(__file__).resolve().parents[2] / "config"
    template = TemplateStore(":memory:", config_dir=config).load_builtin()[0][0]
    meta = {"title": "测试会议纪要", "meeting_time": "周一", "location": "会议室", "host": "甲"}
    generate = make_generate(generator=lambda payload: apply_template(from_graph_payload(payload, meeting_meta=meta), template))
    validate = make_validate(validator=lambda structured: check_template(structured, template))
    assemble = make_assemble(assembler=render_template_markdown)
    state = {"meeting_id": "test-seam", "_data_dir": str(tmp_path), "_db_path": str(tmp_path / "x.db"),
             "topics": [{"title": "项目方案", "discussion": "大家觉得这个方案差不多可以", "readings": ["会议认为"]}],
             "resolutions": [], "action_items": []}
    generated = generate(state)
    assert set(generated) == {"minutes_structured", "gen_retry", "_gate_route"}
    state.update(generated)
    checked = validate(state)
    assert set(checked) == {"validation_issues"}
    assert any(i["detail"] == "参会人员缺失" for i in checked["validation_issues"])
    assert len([i for i in checked["validation_issues"] if i["rule"] == "forbidden_expression"]) == 2
    state.update(checked)
    assembled = assemble(state)
    assert set(assembled) == {"minutes_markdown", "_gate_route"}
    md = assembled["minutes_markdown"]
    assert "参会人员缺失" in md and "含禁用表述：差不多" in md
    assert md.index("## 待核对清单") > md.index("## " + template.section_order[-1])
    assert state["minutes_structured"]["topics"][0]["readings"] == ["会议认为"]
    assert state["minutes_structured"]["recorder"] == ""
    assert set(generated) <= set(S.PipelineState.__annotations__)
    assert set(checked) <= set(S.PipelineState.__annotations__)
