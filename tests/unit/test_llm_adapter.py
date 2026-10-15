"""T2-01 本地大模型适配器契约；全部传输均由内存替身接管。"""

import io
import json
from _netguard import install_network_guard
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from mmwf.adapters.llm.base import LlmError
from mmwf.adapters.llm.fake_client import FakeLlmClient
from mmwf.adapters.llm.ollama_client import OllamaClient, make_from_settings
from mmwf.domain.extraction.schemas import minutes_json_schema
from mmwf.graph.nodes.extraction import make_llm_extractor
from mmwf.settings import LlmConfig


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """即使忘记注入传输替身，也立即失败，非回环连接立即失败。"""
    def forbidden(*args, **kwargs):
        raise AssertionError("测试禁止网络调用")
    install_network_guard(monkeypatch.setattr)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


class FakeTransport:
    """记录真实 Request 对象，返回内存字节流或抛指定异常。"""

    def __init__(self, raw=None, error=None):
        self.raw = raw if raw is not None else json.dumps({
            "message": {"content": '{"topics":[]}'},
            "done": True,
            "done_reason": "stop",
        }).encode()
        self.error = error
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return io.BytesIO(self.raw)


def test_chat_request_body_uses_config(monkeypatch):
    """请求发往 /api/chat，携带 JSON Schema 与配置中的推理参数。"""
    settings = SimpleNamespace(llm=LlmConfig(
        base_url="http://127.0.0.1:12345/",
        model="测试模型",
        temperature=0.37,
        num_ctx=4096,
        timeout_seconds=23,
    ))
    transport = FakeTransport()
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    schema = minutes_json_schema()
    response = make_from_settings(settings).chat(
        "预算{调整}方案{}", system="只抽不编", json_schema=schema,
    )
    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    assert request.full_url == "http://127.0.0.1:12345/api/chat"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 23
    assert json.loads(request.data) == {
        "model": "测试模型",
        "messages": [
            {"role": "system", "content": "只抽不编"},
            {"role": "user", "content": "预算{调整}方案{}"},
        ],
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.37, "num_ctx": 4096},
    }
    assert response.text == '{"topics":[]}'
    assert not response.truncated
    assert response.elapsed_ms >= 0


def test_per_call_overrides(monkeypatch):
    """单次温度及超时覆盖默认值，温度零也有效。"""
    transport = FakeTransport()
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    OllamaClient().chat("测试", temperature=0, timeout_seconds=7)
    request, timeout = transport.calls[0]
    assert json.loads(request.data)["options"]["temperature"] == 0
    assert "format" not in json.loads(request.data)
    assert timeout == 7


@pytest.mark.parametrize("kind,match", [
    ("timeout", "超时"),
    ("connection", "无法连接"),
    ("http", "HTTP 503"),
    ("json", "不是合法 JSON"),
    ("empty", "空内容"),
])
def test_transport_errors_map_to_llm_error(monkeypatch, kind, match):
    """超时、连接、HTTP 和返回内容错误统一映射，适配器不自行重试。"""
    errors = {
        "timeout": TimeoutError("测试超时"),
        "connection": urllib.error.URLError("测试连接拒绝"),
        "http": urllib.error.HTTPError(
            "http://127.0.0.1/api/chat", 503, "测试失败", {},
            io.BytesIO("服务不可用".encode()),
        ),
    }
    raw = b"invalid" if kind == "json" else b'{"message":{"content":""}}'
    transport = FakeTransport(raw=raw, error=errors.get(kind))
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    with pytest.raises(LlmError, match=match):
        OllamaClient().chat("测试")
    assert len(transport.calls) == 1


def test_health_probe_uses_tags_and_short_timeout(monkeypatch):
    """健康探测只列模型，不能触发推理。"""
    transport = FakeTransport(raw=b'{"models":[{"name":"local:small"}]}')
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    health = OllamaClient(model="local:small").health_check()
    request, timeout = transport.calls[0]
    assert request.full_url.endswith("/api/tags")
    assert request.get_method() == "GET"
    assert request.data is None
    assert timeout == 5
    assert health["available"] and health["model_present"]


def test_health_probe_failure_is_reported(monkeypatch):
    """探测失败返回结构化信息，不向调用方抛异常。"""
    monkeypatch.setattr(
        urllib.request, "urlopen", FakeTransport(error=TimeoutError("测试超时")),
    )
    health = OllamaClient().health_check()
    assert health["available"] is False
    assert "error" in health


@pytest.mark.parametrize("text", ["", "[00:00] 张三：同意预算。"])
def test_graph_factory_returns_only_contract_keys(text):
    """D4 工厂返回五个图契约键，丰富原料与问题不得丢失。"""
    data = {
        "title": "会议纪要",
        "meeting_time": "",
        "location": "",
        "host": "",
        "attendees": [],
        "recorder": "",
        "topics": [{
            "name": "预算",
            "discussion": "同意预算。",
            "resolutions": [{
                "content": "同意预算",
                "source_quote": "同意预算。",
                "source_start_ms": 0,
                "source_end_ms": 1000,
            }],
        }],
    }
    llm = FakeLlmClient(script=[json.dumps(data)])
    result = make_llm_extractor(llm, max_workers=1)(text)
    assert set(result) == {"topics", "resolutions", "action_items", "minutes", "issues"}
    if text:
        assert result["topics"][0]["title"] == "预算"
        assert result["resolutions"][0]["text"] == "同意预算"
        assert result["resolutions"][0]["refs"][0]["start_ms"] == 0
    else:
        assert result == {"topics": [], "resolutions": [], "action_items": [],
                          "minutes": {}, "issues": []}
        assert llm.calls == []
