"""T2-01 分块与记录稿反解契约（U-EX-01～05、08、10）。"""

from pathlib import Path

import pytest

from mmwf.domain.extraction.chunker import chunk_segments
from mmwf.domain.extraction.segmentize import Segment, segmentize

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"


@pytest.mark.parametrize("boundary", ["speaker", "turn", "silence"])
def test_semantic_boundary_requires_target(boundary):
    """U-EX-01～03：先攒够字数，再在语义边界切块。"""
    segments = [Segment(i, str(i) * 300, speaker="张三") for i in range(1, 8)]
    if boundary == "speaker":
        segments[4] = Segment(5, "五" * 300, speaker="李四")
    elif boundary == "turn":
        segments[4] = Segment(5, "下面我们看第二个议题" + "五" * 290, speaker="张三")
    else:
        segments[4] = Segment(5, "五" * 300, speaker="张三", gap_after_ms=1501)
    target = 1500 if boundary == "silence" else 1200
    chunks = chunk_segments(segments, target_chars=target, overlap_chars=0)
    cut = 5 if boundary == "silence" else 4
    assert chunks[0].segments == tuple(segments[:cut])
    assert chunks[1].segments[0] == segments[cut]
    assert len(chunks) == 2


def test_early_boundaries_do_not_fragment_long_fixture():
    """坑 1：两万字夹具只产生 10～20 块，不能遇边界就切。"""
    segments, report = segmentize((FIXTURES / "long_20000.txt").read_text(encoding="utf-8"))
    chunks = chunk_segments(segments)
    assert report.no_timecode == 0
    gaps = [s.gap_after_ms for s in segments if s.gap_after_ms is not None]
    assert report.estimated_gaps == len(gaps)
    assert 0 < sum(g > 1500 for g in gaps) / len(gaps) < 0.1
    assert any(left.segments[-1].speaker != right.segments[right.overlap_lead].speaker
               for left, right in zip(chunks, chunks[1:]))
    assert 10 <= len(chunks) <= 20
    assert all(1000 <= ch.char_count <= 2000 for ch in chunks[:-1])
    assert chunks[-1].char_count <= 2000
    assert {s.seq for ch in chunks for s in ch.segments} == {s.seq for s in segments}


def test_maximum_without_semantic_boundaries():
    """U-EX-04：没有语义边界时仍受累计最大块大小约束。"""
    segments = [Segment(i, "甲" * 300, speaker="张三") for i in range(1, 15)]
    chunks = chunk_segments(segments)
    assert len(chunks) >= 3
    assert all(1000 <= ch.char_count <= 2000 for ch in chunks[:-1])
    assert chunks[-1].char_count <= 2000


@pytest.mark.parametrize("text,first_length", [pytest.param("甲" * 4500, 2000, id="hard"), pytest.param("甲" * 1899 + "。" + "乙" * 2600, 1900, id="punctuation")])
def test_single_oversize_segment_is_split(text, first_length):
    """§2.2：单段超长优先句末切分，无标点硬切并标记告警。"""
    chunks = chunk_segments([Segment(1, text, speaker="张三", start_ms=0, end_ms=9000)], overlap_chars=0)
    assert len(chunks) == 3
    assert len(chunks[0].text) == first_length
    assert all(ch.forced_split for ch in chunks)
    assert all(ch.char_count <= 2000 for ch in chunks)
    assert "".join(s.text for ch in chunks for s in ch.segments) == text


def test_overlap_is_actual_suffix_of_whole_segments():
    """U-EX-05、坑 2：重叠是前块末尾的完整段，宁可略超 200 字。"""
    segments = [Segment(i, str(i) * 120, speaker="张三" if i % 2 else "李四") for i in range(1, 10)]
    chunks = chunk_segments(segments, target_chars=600)
    for left, right in zip(chunks, chunks[1:]):
        count = right.overlap_lead
        assert count > 0
        assert right.segments[:count] == left.segments[-count:]
        assert 200 <= sum(s.char_count for s in right.segments[:count]) < 320
        assert right.segments[count:]


@pytest.mark.parametrize("gap,expected", [(1500, 1), (1501, 2)])
def test_silence_threshold_is_strict(gap, expected):
    """U-EX-03：1500 毫秒不触发，超过才触发。"""
    segments = [Segment(1, "甲" * 1500, gap_after_ms=gap), Segment(2, "乙" * 100)]
    assert len(chunk_segments(segments, overlap_chars=0)) == expected


def test_segmentize_timecodes_gaps_and_loss_report():
    """D1：扣除估计说话时长，缺时间码如实计数。"""
    segments, report = segmentize("# 记录稿\n[01:02] 张三：甲\n[00:01:05] 李四: 乙\n王五：丙")
    assert [s.text for s in segments] == ["甲", "乙", "丙"]
    assert [s.start_ms for s in segments] == [62000, 65000, None]
    assert [s.speaker for s in segments] == ["张三", "李四", "王五"]
    assert [s.gap_after_ms for s in segments] == [2750, None, None]
    assert (report.total, report.with_timecode, report.no_timecode) == (3, 2, 1)


def test_empty_segments():
    """U-EX-10：空文本与空段列表均返回空结果。"""
    assert chunk_segments([]) == []
    segments, report = segmentize("\n# 空记录\n")
    assert segments == []
    assert report.total == 0


def test_turn_word_outside_head_does_not_split():
    """U-EX-02：转折词只在段首十字内有效，正文提及不算转折。"""
    segments = [Segment(1, "甲" * 1500), Segment(2, "乙" * 11 + "下面继续讨论")]
    assert len(chunk_segments(segments, overlap_chars=0)) == 1


def test_chunk_settings_control_pipeline():
    """U-EX-08：实际配置中的分块目标和重叠长度贯通抽取入口。"""
    import json
    from types import SimpleNamespace

    from mmwf.adapters.llm.fake_client import FakeLlmClient
    from mmwf.domain.extraction.extractor import extract_minutes
    from mmwf.settings import ExtractionConfig

    text = "\n".join(f"[{i * 76 // 60:02d}:{i * 76 % 60:02d}] {'张三' if i % 2 else '李四'}：" + "甲" * 300 for i in range(8))
    counts = []
    for target in (600, 1500):
        settings = SimpleNamespace(extraction=ExtractionConfig(chunk={"target_chars": target, "overlap_chars": 0}))
        data = {"title": "纪要", "meeting_time": "", "location": "", "host": "", "attendees": [], "recorder": "", "topics": []}
        llm = FakeLlmClient(script=[json.dumps(data)])
        result = extract_minutes(text, llm=llm, settings=settings, max_workers=1)
        assert len(llm.calls) == result.stats["chunks"]
        counts.append(result.stats["chunks"])
    assert counts == [4, 2]


@pytest.mark.parametrize("next_time,expected_gap,expected_chunks", [(378, 3000, 2), (376, 1000, 1)])
def test_record_timecode_gap_triggers_silence_end_to_end(
    next_time, expected_gap, expected_chunks,
):
    """U-EX-03：真实记录稿反解后，扣除 375 秒说话时长后仅长静音触发分块。"""
    from mmwf.adapters.llm.fake_client import FakeLlmClient
    from mmwf.domain.extraction.extractor import extract_minutes

    # 同一说话人、无转折词、总字数未达最大值，排除其他切块原因。
    text = (
        "[00:00] 张三：" + "甲" * 1500
        + f"\n[{next_time // 60:02d}:{next_time % 60:02d}] 张三：乙"
        + f"\n[{(next_time + 1) // 60:02d}:{(next_time + 1) % 60:02d}] 张三：丙"
    )
    segments, _ = segmentize(text)
    assert [s.gap_after_ms for s in segments] == [
        expected_gap, 750, None,
    ]
    chunks = chunk_segments(segments, overlap_chars=0)
    assert len(chunks) == expected_chunks
    if expected_chunks == 2:
        assert chunks[0].segments == (segments[0],)
        assert chunks[1].segments == tuple(segments[1:])

    llm = FakeLlmClient(script=[
        '{"title":"纪要","meeting_time":"","location":"","host":"",'
        '"attendees":[],"recorder":"","topics":[]}'
    ])
    result = extract_minutes(text, llm=llm, overlap_chars=0, max_workers=1)
    assert result.stats["chunks_ok"] == expected_chunks
    assert len(llm.calls) == expected_chunks
    if expected_chunks == 2:
        assert "甲" * 1500 in llm.calls[0]["prompt"]
        assert "乙" not in llm.calls[0]["prompt"]
        assert "甲" not in llm.calls[1]["prompt"]


@pytest.mark.parametrize("new_size,overlap,dropped", [(1700, 1, False), (1900, 0, True)])
def test_overlap_keeps_last_segment_unless_it_cannot_fit(new_size, overlap, dropped):
    """U-EX-05：挤压时保留末段，连末段都放不下才报告零重叠。"""
    segments = [Segment(i, "甲" * 200) for i in range(1, 4)]
    segments.append(Segment(4, "乙" * new_size))
    chunks = chunk_segments(segments, target_chars=600, overlap_chars=400)
    assert len(chunks) == 2
    assert chunks[1].overlap_lead == overlap
    assert chunks[1].overlap_dropped is dropped
    if overlap:
        assert chunks[1].segments[0] == chunks[0].segments[-1]
    assert all(ch.char_count <= 2000 for ch in chunks)


def test_speech_rate_config_reaches_extraction():
    """语速配置贯通抽取；高语速会留下更多估计静音。"""
    from types import SimpleNamespace

    from mmwf.adapters.llm.fake_client import FakeLlmClient
    from mmwf.domain.extraction.extractor import extract_minutes
    from mmwf.settings import ExtractionConfig

    text = "[00:00] 张三：" + "甲" * 1500 + "\n[06:16] 张三：乙"
    for rate, count in [(4.0, 1), (5.0, 2)]:
        settings = SimpleNamespace(extraction=ExtractionConfig(chunk={"speech_chars_per_sec": rate}))
        result = extract_minutes(text, llm=FakeLlmClient(script=['{"topics":[]}']),
                                 settings=settings, overlap_chars=0, max_workers=1)
        assert result.stats["chunks"] == count
        assert result.stats["estimated_gaps"] == 1


def test_estimated_silence_clamps_to_zero():
    """时间间隔小于说话时长时估计静音不得为负。"""
    segments, report = segmentize("[00:00] 张三：甲乙丙丁戊\n[00:01] 张三：乙")
    assert segments[0].gap_after_ms == 0
    assert report.estimated_gaps == 1


@pytest.mark.parametrize("boundary", ["speaker", "turn", "silence"])
@pytest.mark.parametrize("size,cut", [(999, False), (1000, True)])
def test_semantic_boundary_respects_minimum_size(boundary, size, cut):
    # 裁决：03 的 U-EX-01～03 与 U-EX-04 同时成立；仅在达到块大小目标后必切。
    # 使用允许的最小目标 1000，避免把早期语义边界解释为强制碎片化。
    first = Segment(1, "甲" * size, speaker="张三",
                    gap_after_ms=1501 if boundary == "silence" else None)
    second = Segment(2, "下面我们看第二个议题" if boundary == "turn" else "乙",
                     speaker="李四" if boundary == "speaker" else "张三")
    chunks = chunk_segments([first, second], target_chars=1000, overlap_chars=0)
    assert [c.segments for c in chunks] == ([(first,), (second,)] if cut else [(first, second)])
