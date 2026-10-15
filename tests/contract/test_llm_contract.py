"""03 §3 六例裁决：探测、响应、参数、坏模式重试、超时、不可用。

所有注册 provider 共用；Ollama 在 urlopen 边界注入，禁止真实网络/推理。
坏模式由业务校验负责，错误码采用 01 §9.3 E3002/3/4。
"""
import io
import json
import urllib.error

import pytest

from mmwf.adapters.llm import _REGISTRY, build
from mmwf.adapters.llm.base import LlmClient, LlmError, LlmResponse
from mmwf.domain.errors import MmwfError
from mmwf.domain.extraction.extractor import extract_minutes


@pytest.fixture(params=sorted(_REGISTRY))
def provider(request, monkeypatch):
    state = {"text": '{"topics":[]}', "fault": None, "requests": []}

    def content():
        if state["fault"]:
            raise state["fault"]
        return state["text"]

    if request.param == "fake":
        client = build(request.param, fn=lambda prompt: content())
    else:
        # 新 provider 必须补上其传输替身；未知后端不静默 skip。
        assert request.param == "ollama_local"

        def urlopen(req, *, timeout):
            state["requests"].append((req, timeout))
            text = content()
            body = ({"models": [{"name": "qwen2.5:test"}]} if req.method == "GET"
                    else {"message": {"content": text}, "done": True, "done_reason": "stop"})
            return io.BytesIO(json.dumps(body).encode())

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        client = build(request.param)
    return client, state


def test_probe_contract(provider):
    client, state = provider
    assert isinstance(client, LlmClient)
    assert client.available() is True
    report = client.health_check()
    assert report["provider"] == client.name
    assert report["available"] is True
    assert isinstance(report["models_loaded"], list)
    assert all(req.method == "GET" for req, _ in state["requests"])
    if hasattr(client, "calls"):
        assert client.calls == []


def test_response_contract(provider):
    client, state = provider
    response = client.chat("只返回议题")
    assert isinstance(response, LlmResponse)
    assert response.text == state["text"]
    assert isinstance(response.elapsed_ms, int) and response.elapsed_ms >= 0
    assert response.done_reason == "stop"
    assert response.truncated is False


def test_request_options_contract(provider):
    client, state = provider
    schema = {"type": "object", "properties": {"topics": {"type": "array"}}}
    client.chat("正文", system="只抽不编", json_schema=schema, temperature=0.0, timeout_seconds=7)
    if state["requests"]:
        req, timeout = state["requests"][-1]
        body = json.loads(req.data)
        assert body["format"] == schema
        assert body["options"]["temperature"] == 0.0
        assert body["messages"] == [{"role": "system", "content": "只抽不编"},
                                     {"role": "user", "content": "正文"}]
        assert body["stream"] is False
        assert timeout == 7
    else:
        call = client.calls[-1]
        assert call["prompt"] == "正文" and call["system"] == "只抽不编"
        assert call["has_schema"] is True and call["temperature"] == 0.0


def test_bad_schema_retries_and_reports_e3002(provider):
    client, state = provider
    state["text"] = '{"topics":"错误的类型"}'
    result = extract_minutes("[00:00] 张三：讨论预算。", llm=client, max_workers=1)
    assert result.topics == [] and result.resolutions == []
    issues = [i for i in result.issues if i.get("code") == "E3002"]
    assert len(issues) == 1 and "重试" in issues[0]["message"]
    calls = len(state["requests"]) if state["requests"] else len(client.calls)
    assert calls == 3


@pytest.mark.parametrize("wrapped", [False, True])
def test_timeout_is_e3003(provider, wrapped):
    client, state = provider
    state["fault"] = urllib.error.URLError(TimeoutError("private upstream")) if wrapped else TimeoutError("private upstream")
    with pytest.raises(MmwfError) as caught:
        client.chat("正文")
    assert isinstance(caught.value, LlmError)
    assert caught.value.code == "E3003"
    assert "重试" in caught.value.user_message
    assert "private upstream" not in json.dumps(caught.value.to_dict())


def test_unavailable_is_e3004(provider):
    client, state = provider
    state["fault"] = urllib.error.URLError(ConnectionRefusedError("private upstream"))
    with pytest.raises(MmwfError) as caught:
        client.chat("正文")
    assert isinstance(caught.value, LlmError)
    assert caught.value.code == "E3004"
    assert caught.value.user_message
    assert "private upstream" not in json.dumps(caught.value.to_dict())
