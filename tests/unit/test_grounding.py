"""T2-04 溯源算法和硬诊断边界；不构图。"""
import socket
from time import monotonic

import pytest

from mmwf.domain.extraction.grounding import bind_grounding_exact
from mmwf.domain.extraction.segmentize import Segment
from mmwf.domain.grounding.binder import bind_grounding, levenshtein, union_refs
from mmwf.domain.minutes.post_validation import check_grounding, validate_hard


def seg(text, seq=1, start=10, end=20):
    return Segment(seq, text, start_ms=start, end_ms=end)


@pytest.mark.parametrize("quote,text,ok,distance", [
    ("甲乙丙丁戊", "甲乙丙丁己", True, 1),
    ("甲乙丙丁戊己庚辛壬癸", "甲乙丙丁未己庚辛壬子", True, 2),
    ("甲乙丙丁戊己庚辛壬癸", "甲乙丙未未己庚辛壬子", False, 3),
    ("甲乙丙丁戊", "甲乙丙己庚", False, 2),
    ("甲乙丙丁", "甲乙丙己", False, 1),
])
def test_fuzzy_boundaries(quote, text, ok, distance):
    result = bind_grounding(quote, [seg(text)])
    assert result.ok is ok
    assert levenshtein(quote, text) == min(distance, 3)
    if ok:
        assert result.matched == "fuzzy"
        assert result.confidence <= .90
        assert result.ref["quote"] == quote
        assert result.ref["matched_text"] == text
        assert not any(i["code"] == "GROUNDING_AMBIGUOUS" for i in result.issues)
        assert result.confidence == .8
        assert any(i["code"] == "GROUNDING_FUZZY" for i in result.issues)
    else:
        assert result.confidence == 0


def test_exact_api_stays_exact():
    assert not bind_grounding_exact("甲乙丙丁戊", [seg("甲乙丙丁己")]).ok
    assert bind_grounding_exact("甲，乙", [seg("甲,乙")]).confidence == .95
    result = bind_grounding("甲，乙", [seg("甲,乙"), seg("甲，乙", 2)])
    assert result.confidence == 1 and result.ref["segment_id"] == "seg-2"


@pytest.mark.parametrize("quote", ["甲乙丙丁戊己", "甲乙丙，丁戊己"])
def test_adjacent_up_to_three(quote):
    source = [seg("甲乙"), seg("丙，丁", 2), seg("戊己", 3)] if "，" in quote else [seg("甲乙"), seg("丙丁", 2), seg("戊己", 3)]
    result = bind_grounding(quote, source)
    assert [r["segment_id"] for r in result.refs] == ["seg-1", "seg-2", "seg-3"]
    assert not bind_grounding("甲乙丙丁戊己庚辛", [seg("甲乙"), seg("丙丁", 2), seg("戊己", 3), seg("庚辛", 4)]).ok


def test_no_cross_segment_fuzzy():
    assert not bind_grounding("甲乙丙丁戊", [seg("甲乙"), seg("丙丁己", 2)]).ok


def test_deterministic_tie_and_time_null(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("禁止出站")
    monkeypatch.setattr(socket, "socket", no_network)
    source = [seg("甲乙丙丁己", 9, None, None), seg("甲乙丙丁己", 2)]
    result = bind_grounding("甲乙丙丁戊", source)
    assert result == bind_grounding("甲乙丙丁戊", source)
    assert result.ref["segment_id"] == "seg-9"
    assert result.ref["start_ms"] is None and result.ref["end_ms"] is None
    assert {i["code"] for i in result.issues} >= {"GROUNDING_AMBIGUOUS", "NO_TIMECODE"}


def minutes(refs):
    return {"topics": [{"name": "导航", "discussion": "", "resolutions": [{"content": "结论", "source_refs": refs}]}]}


def test_ungrounded_rejected():
    assert any(i["blocking"] for i in validate_hard(minutes([]), [seg("真实原文")]))
    assert any(i["code"] == "SOURCE_UNAVAILABLE" for i in validate_hard(minutes([]), []))


@pytest.mark.parametrize("bad", [{"segment_id": "seg-99", "quote": "真实原文"},
                                  {"segment_id": "seg-1", "quote": "完全虚构"}, {},
                                  {"segment_id": "seg-1", "text": "真实原文", "start_ms": 0}])
def test_partial_bad_ref_cannot_hide(bad):
    issues = check_grounding(minutes([{"segment_id": "seg-1", "quote": "真实原文"}, bad]), [seg("真实原文")])
    assert any(i["blocking"] and i["level"] == "error" for i in issues)


def test_wrong_segment_cannot_rebind_elsewhere():
    assert any(i["blocking"] for i in check_grounding(
        minutes([{"segment_id": "seg-1", "quote": "真实原文"}]), [seg("别的段落"), seg("真实原文", 2)]))


def test_union_preserves_different_quotes():
    a = {"segment_id": "seg-1", "quote": "甲", "start_ms": None, "end_ms": None}
    b = {**a, "quote": "乙"}
    assert union_refs([a], [a, b]) == [a, b]


def test_topic_discussion_and_all_item_types():
    data = {"topics": [{"name": "导航", "discussion": "结论", **{
        k: [{"content": "结论", "source_refs": []}] for k in ("resolutions", "action_items", "pending_decisions")}}]}
    assert len([i for i in check_grounding(data, [seg("原文")]) if i["blocking"]]) == 4
    assert any(i["code"] == "SOURCE_TIME_LOSS" for i in check_grounding(minutes([]), record_markdown="真实原文"))


@pytest.mark.parametrize("quote,text", [("甲乙丙丁戊", "甲乙己丁戊"),
                                       ("甲乙丙丁戊", "甲乙己丙丁戊"),
                                       ("甲乙丙丁戊", "甲乙丁戊")])
def test_substitution_insertion_deletion(quote, text):
    result = bind_grounding(quote, [seg(text)])
    assert result.ok and result.confidence < .95
    assert result.ref["matched_text"] == text


def test_normalized_multisegment_real_times_and_quote_validation():
    source = [seg("甲，乙", 1, 100, 350), seg("丙丁", 2, 400, None)]
    result = bind_grounding("甲,乙 丙丁", source)
    assert result.matched == "normalized"
    assert [(r["start_ms"], r["end_ms"]) for r in result.refs] == [(100, 350), (400, None)]
    assert result.refs[0]["matched_text"] == "甲，乙"
    assert any(i["blocking"] for i in check_grounding(minutes([
        {"segment_id": "seg-1", "quote": "甲，乙"}, {"segment_id": "seg-2", "quote": "虚构"}]), source))


def test_single_quote_list_checks_every_element_and_explicit_empty_refs():
    candidate = minutes([{"segment_id": "seg-1", "quote": "真实原文"}])
    entry = candidate["topics"][0]["resolutions"][0]
    entry["source_quote"] = ["真实原文", "虚构"]
    assert any(i["blocking"] for i in check_grounding(candidate, [seg("真实原文")]))
    entry.update(source_refs=[], source_quote="真实原文")
    assert any(i["blocking"] for i in check_grounding(candidate, [seg("真实原文")]))


def test_submitted_quote_and_serialized_multiple_refs_preserved():
    quote = " 甲乙 丙丁 "
    result = bind_grounding(quote, [seg("甲乙"), seg("丙丁", 2)])
    assert result.ref["quote"] == quote
    assert len(result.to_dict()["refs"]) == 2
    assert set(bind_grounding_exact("甲乙", [seg("甲乙")]).to_dict()) == {"matched", "ref", "confidence"}


def test_single_embedded_fuzzy_location_is_not_ambiguous():
    result = bind_grounding("甲乙丙丁戊", [seg("背景甲乙己丁戊结束")])
    assert result.ref["matched_text"] == "甲乙己丁戊"
    assert not any(i["code"] == "GROUNDING_AMBIGUOUS" for i in result.issues)


def test_distinct_locations_in_one_segment_are_ambiguous():
    result = bind_grounding("甲乙丙丁戊", [seg("甲乙己丁戊；甲乙己丁戊")])
    assert result.ref["matched_text"] == "甲乙己丁戊"
    assert any(i["code"] == "GROUNDING_AMBIGUOUS" for i in result.issues)


def test_equal_distance_prefers_quote_length_before_earliest_location():
    result = bind_grounding("甲乙丙丁戊", [seg("甲乙己丙丁戊"), seg("甲乙己丁戊", 2)])
    assert result.ref["segment_id"] == "seg-2"
    assert result.ref["matched_text"] == "甲乙己丁戊"
    assert not any(i["code"] == "GROUNDING_AMBIGUOUS" for i in result.issues)


def test_long_record_fuzzy_smoke():
    # 5 万字、多段、末段才命中；宽松上限用于发现数量级退化，不做微基准。
    source = [seg("背景讨论记录" * 34, seq=i + 1) for i in range(250)]
    source[-1] = seg(source[-1].text + "甲乙己丁戊", seq=250)
    started = monotonic()
    result = bind_grounding("甲乙丙丁戊", source)
    elapsed = monotonic() - started
    assert result.ok and result.ref["segment_id"] == "seg-250"
    assert result.ref["matched_text"] == "甲乙己丁戊"
    assert not any(i["code"] == "GROUNDING_AMBIGUOUS" for i in result.issues)
    assert elapsed < 10, f"5 万字溯源耗时 {elapsed:.2f}s，超过宽松上限"


def test_navigation_and_conclusion_titles_use_presentation_context():
    candidate = {"topics": [{"name": "会议同意预算", "discussion": "", "source_refs": []}]}
    assert not check_grounding(candidate, [seg("原文")])
    issues = validate_hard(candidate, [seg("原文")], conclusion_title_paths={"topics[0]"})
    assert any(i["blocking"] for i in issues)
    candidate["topics"][0]["source_refs"] = [{"segment_id": "seg-1", "quote": "原文"}]
    assert not validate_hard(candidate, [seg("原文")], conclusion_title_paths={"topics[0]"})
