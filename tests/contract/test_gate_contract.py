"""C3 真 HTTP 信封、三态与检查点令牌合同。"""
from copy import deepcopy
import pytest


def decide(e, gate, checkpoint, decision, payload=None, **extra):
    return e.client.post(e.base + f"/gate/{gate}/decide", json={"decision": decision, "checkpoint_id": checkpoint,
                        "payload": payload, "operator": "秘书", **extra}).json()


@pytest.mark.parametrize("gate", ["G2", "G3"])
@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
def test_three_states(gate_env, gate, decision):
    e = gate_env
    pending = e.pending
    if gate == "G3":
        pending = decide(e, "G2", pending["checkpoint_id"], "approve")["data"]["pending_gate"]
    payload = None
    if decision == "edit":
        if gate == "G2":
            items = deepcopy(pending["payload"]["topics"])
            items[0]["title"] = "预算执行安排"
            payload = {"topics": items}
        else:
            payload = {"minutes": {"topics": [{"name": "人工修正预算议题"}, {}]}}
    body = decide(e, gate.lower(), pending["checkpoint_id"], decision, payload)
    assert body["ok"], body
    data = body["data"]
    assert data["from_checkpoint"] == pending["checkpoint_id"]
    assert data["resumed"] is True
    expected = {("G2", False): "generating", ("G2", True): "extracting", ("G3", False): "exporting", ("G3", True): "assembling"}
    assert data["transition_state"] == expected[gate, decision == "reject"]
    assert data["rerun_nodes"] == (["extract" if gate == "G2" else "assemble"] if decision == "reject" else [])
    if gate == "G3" and decision != "reject":
        assert data["next_state"] == "archived" and data["pending_gate"] is None
    else:
        assert data["pending_gate"]["gate"] == (gate if decision == "reject" else "G3")
        assert data["pending_gate"]["checkpoint_id"] != pending["checkpoint_id"]


@pytest.mark.parametrize("gate,checkpoint", [("G2", None), ("G3", "current"), ("G2", "stale")])
def test_invalid_token(gate_env, gate, checkpoint):
    e = gate_env
    ckpt = e.pending["checkpoint_id"] if checkpoint == "current" else checkpoint
    body = decide(e, gate, ckpt, "approve")
    assert body["ok"] is False and body["code"] == "E4001"
    assert e.rt.gate_service().store.registered() == []
    assert e.client.get(e.base).json()["data"]["run"]["pending_gate"] == e.pending


def test_missing_checkpoint_and_bad_inputs(gate_env):
    e = gate_env
    requests = [{"decision": "approve"}, {"decision": []}, {"decision": "approve", "operator": 1},
                {"decision": "approve", "comment": {}}, {"decision": "edit", "payload": {"topics": None}},
                {"decision": "edit", "payload": {"topics": "bad"}},
                {"decision": "edit", "payload": {"topics": [1]}},
                {"decision": "edit", "payload": {"minutes_markdown": "越权"}},
                {"decision": "edit", "payload": {"topics": e.pending["payload"]["topics"], "unknown": 1}}]
    for req in requests:
        if req != {"decision": "approve"}:
            req["checkpoint_id"] = e.pending["checkpoint_id"]
        body = e.client.post(e.base + "/gate/G2/decide", json=req).json()
        assert body["ok"] is False and body["code"] == "E4001", body
    assert e.rt.gate_service().store.registered() == []


def test_non_edit_payload_ignored_and_retry(gate_env):
    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    first = decide(e, "G2", ckpt, "approve", "ignored")
    retry = decide(e, "G2", ckpt, "approve", {"minutes_markdown": "also ignored"})
    assert first["ok"] and retry["ok"]
    assert retry["data"]["resumed"] is False
    assert retry["data"]["pending_gate"] == first["data"]["pending_gate"]
    conflict = decide(e, "G2", ckpt, "reject")
    assert conflict["code"] == "E4001"


def test_invalid_edit_keeps_checkpoint_for_correction(gate_env):
    e = gate_env
    ckpt = e.pending["checkpoint_id"]
    items = deepcopy(e.pending["payload"]["topics"])
    items[0]["refs"] = []
    body = decide(e, "G2", ckpt, "edit", {"topics": items})
    assert body["code"] == "E4001"
    assert decide(e, "G2", ckpt, "edit", {"topics": e.pending["payload"]["topics"]})["ok"]
