"""生产纪要节点失败时保留工作稿，前置校验先于状态打点。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes import minutes_context as M
from mmwf.infra.template_store import TemplateStore


@pytest.mark.parametrize("name", ["generate", "validate"])
def test_missing_inputs_leave_state_and_task_status_unchanged(name, monkeypatch):
    state = {"meeting_id": "missing", "minutes": {"title": "保留原料"}}
    if name == "generate":
        state["minutes_structured"] = {"title": "保留旧工作稿"}
    before = deepcopy(state)
    touch = Mock()
    context = Mock(side_effect=AssertionError("缺输入不应选模"))
    monkeypatch.setattr(B, "touch_task_state", touch)
    node = M.production_node(name, None, context, deps=SimpleNamespace(settings=object()))

    with pytest.raises(B.NodeInputError) as caught:
        node(state)

    assert caught.value.context["node"] == name
    assert state == before
    touch.assert_not_called()
    context.assert_not_called()


@pytest.mark.parametrize("name", ["generate", "validate"])
def test_domain_failure_keeps_previous_structured_minutes(name, tmp_path, monkeypatch):
    template = TemplateStore(":memory:", config_dir=Path(__file__).resolve().parents[2] / "config").select()
    context = lambda state: {"template_snapshot": template.to_dict()}
    settings = SimpleNamespace(app=SimpleNamespace(data_dir=tmp_path), resolve_path=lambda path: path)
    state = {"meeting_id": "failed", "topics": [{"title": "议题"}],
             "resolutions": [], "action_items": [],
             "minutes_structured": {"title": "旧工作稿", "topics": [{"name": "旧议题", "discussion": "原稿"}]}}
    before = deepcopy(state)
    touch = Mock()
    monkeypatch.setattr(B, "touch_task_state", touch)

    def broken(candidate, *args, **kwargs):
        candidate["topics"][0]["discussion"] = "失败计算的临时修改"
        raise RuntimeError("领域计算失败")

    monkeypatch.setattr(M, "apply_template" if name == "generate" else "post_validate", broken)
    node = M.production_node(name, None, context, deps=SimpleNamespace(settings=settings))
    with pytest.raises(RuntimeError, match="领域计算失败"):
        node(state)

    assert state == before
    touch.assert_called_once_with(tmp_path / "mmwf.db", "failed", name)
