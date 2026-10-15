"""T2-01 精确溯源契约；硬拦截留给 T2-04。"""

import pytest

from mmwf.domain.extraction.grounding import bind_grounding_exact
from mmwf.domain.extraction.segmentize import Segment


@pytest.mark.parametrize("quote,matched", [("同意预算，月底完成。", "exact"), ("同意预算, 月底完成.", "normalized")])
def test_grounding_exact_and_normalized(quote, matched):
    """U-GR-01、02：精确优先，标点与空白差异可归一化匹配。"""
    segment = Segment(3, "会议同意预算，月底完成。", speaker="张三", start_ms=1000, end_ms=2000)
    result = bind_grounding_exact(quote, [segment])
    assert result.ok
    assert result.matched == matched
    assert result.ref == {"segment_id": "seg-3", "start_ms": 1000, "end_ms": 2000, "text": segment.text, "quote": quote, "speaker": "张三"}


@pytest.mark.parametrize("quote", ["未讨论的事项", "同意预案，月底完成。", ""])
def test_not_found_is_nonblocking(quote):
    """未命中只返回标记，不抛异常，也不提前做模糊匹配。"""
    result = bind_grounding_exact(quote, [Segment(1, "同意预算，月底完成。")])
    assert result.matched is None
    assert result.ref is None
    assert not result.ok


def test_exact_match_has_priority_across_segments():
    """后段逐字命中优先于前段仅标点归一化命中。"""
    result = bind_grounding_exact("预算，完成。", [Segment(1, "预算,完成."), Segment(2, "预算，完成。")])
    assert result.matched == "exact"
    assert result.ref["segment_id"] == "seg-2"
