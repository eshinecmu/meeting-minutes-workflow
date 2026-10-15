"""抽取开关的生产装配与节点集成；全程禁止真实网络连接。"""

from _netguard import install_network_guard
import urllib.request
from pathlib import Path

import pytest

from mmwf.adapters import llm
from mmwf.api.routes._context import Runtime
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes.extraction import make_extract, naive_extract
from mmwf.infra.migrations.runner import migrate
from mmwf.settings import ExtractionConfig, load_settings


RECORD = "[00:00:12] 张三：同意按计划实施。"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """构造与节点执行禁止外部连接或发出 HTTP 请求。"""
    def forbidden(*args, **kwargs):
        raise AssertionError("测试禁止网络调用")

    install_network_guard(monkeypatch.setattr)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


@pytest.fixture
def settings(tmp_path):
    """保留仓库默认配置，数据目录隔离到临时目录。"""
    cfg = load_settings(Path(__file__).resolve().parents[2] / "config")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    migrate(data_dir / "mmwf.db")
    return cfg.model_copy(update={
        "app": cfg.app.model_copy(update={"data_dir": data_dir}),
    })


def test_default_runtime_uses_naive(settings, monkeypatch):
    """默认生产装配不构造客户端，节点结果与既有朴素实现一致。"""
    def forbidden_factory(cfg):
        raise AssertionError("朴素路径不应构造大模型客户端")

    monkeypatch.setattr(llm, "make_llm_client", forbidden_factory)
    monkeypatch.setattr(B, "touch_task_state", lambda *args: None)
    with Runtime(settings).runner("naive-config") as runner:
        assert runner.deps.extractor is None
        result = make_extract(runner.deps)({
            "meeting_id": runner.meeting_id, "record_markdown": RECORD,
        })
    for key, value in naive_extract(RECORD).items():
        assert result[key] == value


def test_llm_runtime_uses_factory_fake(settings, monkeypatch):
    """经生产同一工厂注入 Fake 后，extract 节点返回引擎产物。"""
    cfg = settings.model_copy(update={"extraction": ExtractionConfig(engine="llm")})
    fake = llm.FakeLlmClient(oracle_items=[{
        "quote": "同意按计划实施。",
        "content": "引擎抽取的决议",
        "topic": "引擎识别的议题",
        "owner": "张三",
    }])
    factory_settings = []

    def factory(settings):
        factory_settings.append(settings)
        return fake

    monkeypatch.setattr(llm, "make_llm_client", factory)
    monkeypatch.setattr(B, "touch_task_state", lambda *args: None)
    with Runtime(cfg).runner("llm-config") as runner:
        assert factory_settings == [cfg]
        assert fake.calls == []
        result = make_extract(runner.deps)({
            "meeting_id": runner.meeting_id, "record_markdown": RECORD,
        })
    assert len(fake.calls) == 1
    assert result["topics"][0]["title"] == "引擎识别的议题"
    assert result["resolutions"][0]["text"] == "引擎抽取的决议"
    assert result["resolutions"][0]["owner"] == "张三"
    assert result["resolutions"][0]["refs"]
    assert result["action_items"] == []


def test_llm_runtime_construction_opens_no_connection(settings):
    """真实客户端只在装配时保存配置，不探测服务或调用模型。"""
    cfg = settings.model_copy(update={"extraction": ExtractionConfig(engine="llm")})
    with Runtime(cfg).runner("llm-construction") as runner:
        assert callable(runner.deps.extractor)
