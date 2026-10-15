"""T2-01 模式约束、重试和逐块容错契约。"""

import json
from pathlib import Path

import pytest

from mmwf.adapters.llm.fake_client import FakeLlmClient
from mmwf.domain.extraction.extractor import extract_minutes
from mmwf.domain.extraction.schemas import (ActionItemSchema, MinutesSchema, PendingDecisionSchema, ResolutionSchema, minutes_json_schema, validate_minutes)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"


def payload():
    """提供完整的 SAD §10.2 输入形状。"""
    return {"title": "会议纪要", "meeting_time": "今天", "location": "会议室", "host": "张三", "attendees": ["张三"], "recorder": "李四", "topics": [{"name": "预算", "discussion": "同意预算。", "resolutions": [{"content": "同意预算", "source_quote": "同意预算。", "source_start_ms": 0, "source_end_ms": 1000}]}]}


def test_sad_shape_and_schema_export():
    """SAD §10.2：顶层、议题及三类事项的字段名保持一致。"""
    obj, issues = validate_minutes(payload())
    assert issues == []
    data = obj.model_dump()
    assert set(data) == {"title", "meeting_time", "location", "host", "attendees", "recorder", "topics", "schema_version"}
    assert data["schema_version"] == "1.0"
    assert set(data["topics"][0]) == {"name", "discussion", "readings", "resolutions", "action_items", "pending_decisions"}
    assert set(data["topics"][0]["resolutions"][0]) == {"content", "owner", "deadline", "source_quote", "source_start_ms", "source_end_ms"}
    schema = minutes_json_schema()
    assert schema["properties"]["topics"]["items"]["$ref"] == "#/$defs/TopicSchema"
    assert schema["$defs"]["ResolutionSchema"]["properties"]["content"]["minLength"] == 1


@pytest.mark.parametrize("cls", [ResolutionSchema, ActionItemSchema, PendingDecisionSchema])
@pytest.mark.parametrize("owner", [None, "", "  ", "张三"])
def test_owner_is_always_serialized_with_placeholder(cls, owner):
    """C05：责任人缺失或空白填「暂无」，序列化必须保留键。"""
    data = dict(content="同意预算", source_quote="同意预算。", source_start_ms=0, source_end_ms=1000)
    if owner is not None:
        data["owner"] = owner
    obj = cls.model_validate(data)
    assert json.loads(obj.model_dump_json())["owner"] == ("张三" if owner == "张三" else "暂无")


@pytest.mark.parametrize("invalid", ["content", "source_quote", "source_start_ms", "source_end_ms", "discussion", "title", "schema_version", "name"])
def test_schema_rejects_contract_violations(invalid):
    """必填字段、内容长度、题名长度与版本号必须校验。"""
    data = payload()
    topic = data["topics"][0]
    if invalid == "content":
        topic["resolutions"][0][invalid] = ""
    elif invalid.startswith("source_"):
        del topic["resolutions"][0][invalid]
    elif invalid == "discussion":
        del topic[invalid]
    elif invalid == "title":
        del data[invalid]
    elif invalid == "schema_version":
        data[invalid] = "2.0"
    else:
        topic["name"] = "甲" * 31
    obj, issues = validate_minutes(data)
    assert obj is None
    assert issues
    assert issues[0].loc


@pytest.mark.parametrize("bad", ['{"topics": "错误"}', '不是 JSON'])
def test_schema_retry_is_limited_and_feedback_is_injected(bad):
    """SAD §10.2：初次加两次重试，失败落 issue，重试回注具体错误。"""
    llm = FakeLlmClient(script=[bad])
    result = extract_minutes("[00:00] 张三：预算{调整}方案与附件{}。", llm=llm)
    assert len(llm.calls) == 3
    assert all(c["has_schema"] for c in llm.calls)
    assert all("【校验报错】" in c["prompt"] for c in llm.calls[1:])
    assert all("预算{调整}方案与附件{}。" in c["prompt"] for c in llm.calls)
    assert any(i["rule"] in {"chunk_schema_failed", "chunk_bad_json"} for i in result.issues)


def test_retry_can_recover_and_preserves_braces():
    """坑 8：正文花括号原样进入初次及重试提示词，修正后正常抽取。"""
    llm = FakeLlmClient(script=['{"topics":"错误"}', json.dumps(payload())])
    result = extract_minutes("[00:00] 张三：同意预算。预算{调整}方案及{}。\n[00:01] 李四：收到。", llm=llm)
    assert len(llm.calls) == 2
    assert "topics" in llm.calls[1]["prompt"]
    assert all("预算{调整}方案及{}。" in c["prompt"] for c in llm.calls)
    assert len(result.resolutions) == 1
    assert result.resolutions[0]["owner"] == "暂无"
    MinutesSchema.model_validate(result.minutes)


@pytest.mark.parametrize("text", ["", " \n", "# 空记录\n"])
def test_empty_input_never_calls_llm(text):
    """U-EX-10：空输入返回空数组且完全不调用模型。"""
    llm = FakeLlmClient(script=["不得调用"])
    result = extract_minutes(text, llm=llm)
    assert result.to_graph_dict() == {"topics": [], "resolutions": [], "action_items": []}
    assert not llm.calls


def test_chitchat_all_six_dimensions_are_empty():
    """U-EX-09：空信息响应经管道后，六维信息均为空，不凭空补造。"""
    data = payload()
    data.update(attendees=[], topics=[])
    llm = FakeLlmClient(script=[json.dumps(data)])
    result = extract_minutes((FIXTURES / "chitchat.txt").read_text(encoding="utf-8"), llm=llm)
    assert len(llm.calls) == 1
    # 发言人由 attendees 表达；结论性意见、议定、待办、待决策均在议题内。
    assert result.minutes["attendees"] == []
    assert result.minutes["topics"] == []
    assert result.to_graph_dict() == {"topics": [], "resolutions": [], "action_items": []}


@pytest.mark.parametrize("workers", [1, 2])
def test_one_failed_chunk_does_not_fail_extraction(workers):
    """坑 6：串行和并行路径都应保留成功块并报告失败块。"""
    def respond(prompt):
        if "失败块" in prompt:
            raise TimeoutError("测试注入超时")
        return json.dumps(payload())
    llm = FakeLlmClient(fn=respond)
    result = extract_minutes("[00:00] 张三：开始。\n[00:01] 李四：失败块。\n[00:02] 王五：同意预算。", llm=llm, target_chars=5, overlap_chars=0, max_workers=workers)
    assert result.stats["chunks"] == 2
    assert result.stats["chunks_ok"] == 1
    assert len(result.resolutions) == 1
    assert any(i["rule"] == "chunk_llm_failed" and i["chunk"] == 1 for i in result.issues)


def test_missing_timecodes_are_reported():
    """D1：缺失时间码显式告警，不让分块退化静默发生。"""
    data = payload()
    data["topics"] = []
    result = extract_minutes("张三：早上好。\n[00:01] 李四：你好。", llm=FakeLlmClient(script=[json.dumps(data)]))
    issue = next(i for i in result.issues if i["rule"] == "no_timecode_segments")
    assert "1/2" in issue["detail"]
    assert result.stats["no_timecode"] == 1


def test_long_fixture_oracle_preserves_all_25_resolutions():
    """施工卡 §4.1：全知假模型验证管道无损，不代表真模型召回率。"""
    items = json.loads((FIXTURES / "long_20000_expected.json").read_text(encoding="utf-8"))
    assert len(items) == 25
    llm = FakeLlmClient(oracle_items=items)
    result = extract_minutes((FIXTURES / "long_20000.txt").read_text(encoding="utf-8"), llm=llm, max_workers=1)
    assert {r["text"] for r in result.resolutions} == {i["content"] for i in items}
    assert len(result.resolutions) == 25
    assert all(r["refs"] for r in result.resolutions)
    assert result.stats["chunks_ok"] == result.stats["chunks"]
    MinutesSchema.model_validate(result.minutes)


@pytest.mark.parametrize("retries", [0, 1, 2, 5])
def test_configured_retries_respect_two_retry_limit(retries, caplog):
    """配置可减少重试次数，但不能突破施工卡规定的两次上限。"""
    from types import SimpleNamespace

    from mmwf.settings import ExtractionConfig

    settings = SimpleNamespace(extraction=ExtractionConfig(retry={"max_schema_retries": retries}))
    llm = FakeLlmClient(script=['{"topics":"错误"}'])
    result = extract_minutes("[00:00] 张三：同意预算。", llm=llm, settings=settings)
    assert len(llm.calls) == min(retries, 2) + 1
    if retries > 2:
        assert "max_schema_retries=5" in caplog.text
        assert "采用 2" in caplog.text
        assert "SAD §10.2" in caplog.text
    else:
        assert "max_schema_retries" not in caplog.text
    assert any(i["rule"] == "chunk_schema_failed" for i in result.issues)


def test_unmatched_quote_does_not_block_result():
    """T2-01 未命中只留空依据，事项照常返回，硬拦截属于 T2-04。"""
    result = extract_minutes("[00:00] 张三：讨论其他工作。", llm=FakeLlmClient(script=[json.dumps(payload())]))
    assert len(result.resolutions) == 1
    assert result.resolutions[0]["refs"] == []
