"""T2-01 块间归并与配置贯通契约（U-EX-06～08）。"""

import json
from types import SimpleNamespace

import pytest

from mmwf.adapters.llm.fake_client import FakeLlmClient
from mmwf.domain.extraction.extractor import extract_minutes
from mmwf.domain.extraction.merge import merge_chunk_results, similarity
from mmwf.settings import ExtractionConfig


def test_topic_refs_are_union_without_mutating_inputs():
    """U-EX-06、坑 3：跨块同题保留全部依据，并去掉重叠副本。"""
    a, b, c = [{"segment_id": f"seg-{i}"} for i in range(1, 4)]
    chunks = [
        {"topics": [{"title": "预算", "discussion": "讨论预算", "refs": [a, b]}]},
        {"topics": [{"title": "预算", "discussion": "讨论预算", "refs": [b, c]}]},
    ]
    original = json.dumps(chunks)
    result = merge_chunk_results(chunks)
    assert len(result.topics) == 1
    assert result.topics[0]["refs"] == [a, b, c]
    assert json.dumps(chunks) == original


@pytest.mark.parametrize("overlap,expected", [(True, 1), (False, 2)])
def test_duplicate_resolution_requires_overlap(overlap, expected):
    """U-EX-07：高相似度且区间重叠才合并，并保留信息更完整者。"""
    text = "由办公室负责组织预算方案的审核落实并在本月底前提交全部工作成果"
    other = text + "。"
    assert similarity(text, other) > 0.9
    a = {
        "text": text,
        "owner": "暂无",
        "refs": [{"segment_id": "seg-1", "start_ms": 0, "end_ms": 1000}],
    }
    b = {
        "text": other,
        "owner": "张三",
        "deadline": "月底",
        "refs": [{
            "segment_id": "seg-2",
            "start_ms": 500 if overlap else 2000,
            "end_ms": 3000,
        }],
    }
    result = merge_chunk_results([{"resolutions": [a]}, {"resolutions": [b]}])
    assert len(result.resolutions) == expected
    if overlap:
        assert result.resolutions[0]["text"] == other
        assert result.resolutions[0]["owner"] == "张三"
        assert result.resolutions[0]["refs"] == a["refs"] + b["refs"]


def test_unrelated_items_in_same_interval_are_kept():
    """相同区间不是充分条件，不能误合并不同事项。"""
    ref = {"segment_id": "seg-1"}
    result = merge_chunk_results([{"resolutions": [
        {"text": "批准预算", "refs": [ref]},
        {"text": "开展安全检查", "refs": [ref]},
    ]}])
    assert len(result.resolutions) == 2


@pytest.fixture
def chunk_response():
    """构造独立的逐块响应，阈值用例无需复制或改写配置目录。"""
    def response(name, content):
        return json.dumps({
            "title": "会议纪要",
            "meeting_time": "",
            "location": "",
            "host": "",
            "attendees": [],
            "recorder": "",
            "topics": [{
                "name": name,
                "discussion": "",
                "resolutions": [{
                    "content": content,
                    "source_quote": "批准预算方案。",
                    "source_start_ms": 0,
                    "source_end_ms": 1000,
                }],
            }],
        })
    return response


@pytest.mark.parametrize("kind", ["topic", "resolution"])
@pytest.mark.parametrize("threshold,expected_count", [(0.2, 1), (0.99, 2)])
def test_thresholds_from_settings_change_extraction(
    chunk_response, kind, threshold, expected_count,
):
    """U-EX-08、坑 4：内存配置改变后，端到端归并行为随之改变。"""
    a, b = "预算方案审核落实", "预算方案审核落实工作"
    assert 0.2 < similarity(a, b) < 0.99
    quote = "批准预算方案。"
    if kind == "topic":
        script = [chunk_response(a, quote), chunk_response(b, quote)]
        field = "topic_similarity_threshold"
    else:
        script = [chunk_response("预算", a), chunk_response("预算", b)]
        field = "resolution_dedup_threshold"
    settings = SimpleNamespace(
        extraction=ExtractionConfig(merge={field: threshold}),
    )
    # 两块都包含同一原段，确保议定去重的区间条件成立。
    text = (
        "[00:00] 张三：开始讨论。\n"
        "[00:01] 李四：批准预算方案。\n"
        "[00:02] 王五：继续讨论。"
    )
    result = extract_minutes(
        text,
        llm=FakeLlmClient(script=script),
        settings=settings,
        target_chars=8,
        overlap_chars=6,
        max_workers=1,
    )
    assert result.stats["chunks_ok"] == 2
    items = result.topics if kind == "topic" else result.resolutions
    assert len(items) == expected_count


@pytest.mark.parametrize("suffix,count", [("子", 2), ("", 1)])
def test_resolution_similarity_is_strictly_above_point_nine(suffix, count):
    # 裁决：03 U-EX-07 的 >0.9 严格执行；保留引用区间重叠这一更窄的合并前提。
    text = "甲乙丙丁戊己庚辛壬癸"
    assert similarity(text, text + suffix) == (0.9 if suffix else 1.0)
    result = merge_chunk_results([{"resolutions": [
        {"text": text, "refs": [{"segment_id": "seg-1"}]},
        {"text": text + suffix, "refs": [{"segment_id": "seg-1"}]},
    ]}])
    assert len(result.resolutions) == count


def test_yaml_reload_changes_point_eight_two_topic_threshold(tmp_path, chunk_response):
    """U-EX-08：实际修改临时 extraction.yaml 并重新加载，而非内存替换。"""
    import shutil
    from pathlib import Path
    import yaml
    from mmwf.settings import reload_settings

    source = Path(__file__).resolve().parents[2] / "config"
    for name in ("app.yaml", "models.yaml", "thresholds.yaml", "extraction.yaml"):
        shutil.copyfile(source / name, tmp_path / name)
    path = tmp_path / "extraction.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    a, b = "预算方案审核落实", "预算方案审核落实好"
    assert 0.82 < similarity(a, b) < 0.99
    counts = []
    for threshold in (0.82, 0.99, 0.82):
        doc["extraction"]["merge"]["topic_similarity_threshold"] = threshold
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        settings = reload_settings(tmp_path)
        assert settings.extraction.merge.topic_similarity_threshold == threshold
        result = extract_minutes(
            "[00:00] 张三：开始讨论。\n[00:01] 李四：批准预算方案。\n[00:02] 王五：继续讨论。",
            llm=FakeLlmClient(script=[chunk_response(a, "批准预算方案。"),
                                      chunk_response(b, "批准预算方案。")]),
            settings=settings, target_chars=8, overlap_chars=6, max_workers=1,
        )
        assert result.stats["chunks_ok"] == 2
        counts.append(len(result.topics))
    assert counts == [1, 2, 1]
