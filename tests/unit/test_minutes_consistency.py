"""数字、人名只报问题；不猜金额对应关系或参会事实。"""
import pytest

from mmwf.domain.extraction.segmentize import Segment
from mmwf.domain.minutes.consistency import check_numbers, check_person_names, numeric_tokens


def data(content, quote=None, **fields):
    return {"topics": [{"resolutions": [{"content": content, "source_quote": quote or content, **fields}]}]}


def codes(issues):
    assert not any(i["blocking"] for i in issues)
    return {i["code"] for i in issues}


@pytest.mark.parametrize("actual,source,mismatch", [
    ("300 万元", "三十万", True), ("30万元", "三十万元", False),
    ("3,000,000元", "三百万元", False), ("三点五万元", "35000元", False),
    ("2026-10-14", "2026年10月14日", False), ("10月14日", "2026年10月14日", True),
    ("三十名", "30人", False), ("30%", "30元", False), ("３０万元", "三十万元", False),
    ("二〇二六年十月十四日", "2026-10-14", False),
])
def test_numbers(actual, source, mismatch):
    issues = check_numbers(data(actual, source), [Segment(1, source)])
    assert ("NUMERIC_MISMATCH" in codes(issues)) is mismatch
    if actual == "30%":
        assert "NUMERIC_UNSUPPORTED" in codes(issues)


def test_normalized_amount_values():
    assert numeric_tokens("300 万元")[0][0].value == 3000000
    assert numeric_tokens("三十万")[0][0].value == 300000


@pytest.mark.parametrize("expression", ["约30万元", "三十至五十万元", "三分之一", "30余元"])
def test_unsupported_numeric_expression(expression):
    assert "NUMERIC_UNPARSED" in codes(check_numbers(data(expression), [Segment(1, expression)]))


def test_fallback_and_metadata_exclusion():
    assert "NUMERIC_SCOPE_FALLBACK" in codes(check_numbers(data("30万元", "伪造"), [Segment(1, "30万元")]))
    assert not check_numbers({"topics": [{"name": "导航", "resolutions": [{"content": "通过", "id": "300万元", "source_quote": "通过", "source_refs": [{"quote": "通过", "start_ms": 300}]}]}]}, [Segment(1, "通过")])


def test_local_refs_and_fuzzy_do_not_exempt_numbers():
    source = [Segment(1, "预算为三十万元"), Segment(2, "300万元")]
    assert "NUMERIC_MISMATCH" in codes(check_numbers(data("300万元", "预算为三十万元"), source))
    assert "NUMERIC_MISMATCH" in codes(check_numbers(data("300万元", "预算为三十万余"), [source[0]]))


PERSON = [{"category": "person", "canonical": "张三", "aliases": ["老张"], "enabled": True}]


def test_unknown_and_terms_are_not_attendance_evidence():
    source = [Segment(1, "提交报告")]
    assert "PERSON_UNSUPPORTED" in codes(check_person_names({"host": "张三"}, source))
    assert "PERSON_UNSUPPORTED" in codes(check_person_names({"host": "张三"}, source, terms=PERSON))


def test_source_person_without_term_and_alias():
    source = [Segment(1, "请张三同志提交报告", speaker="张三")]
    assert not check_person_names({"host": "张三"}, source)
    assert "PERSON_ALIAS" in codes(check_person_names({"host": "老张"}, source, terms=PERSON))
    assert "PERSON_ALIAS" in codes(check_person_names({"host": "张三"}, [Segment(1, "参会人员：老张")], terms=PERSON))


def test_ambiguous_alias_and_org_excluded():
    terms = PERSON + [{"category": "person", "canonical": "张四", "aliases": ["老张"]},
                      {"category": "org", "canonical": "专项团队", "aliases": ["协作组"]}]
    assert "PERSON_AMBIGUOUS" in codes(check_person_names({"host": "老张"}, [Segment(1, "老张")], terms=terms))
    assert not check_person_names({"host": "办公室", "recorder": "暂无", "attendees": ["协作组"]}, [], terms=terms)


def test_names_in_bounded_pattern_and_local_owner():
    source = [Segment(1, "请李四同志完成报告"), Segment(2, "请张三同志完成报告")]
    assert "PERSON_UNSUPPORTED" in codes(check_person_names(data("请张三负责报告", "请李四同志完成报告", owner="张三"), source))


@pytest.mark.parametrize("expression", ["30万元至50万元", "30万元左右", "数十万元"])
def test_ranges_and_approximation_not_silently_matched(expression):
    tokens, unsupported = numeric_tokens(expression)
    assert unsupported and not tokens


@pytest.mark.parametrize("expression", ["2026-10-14", "二〇二六年十月十四日", "百分之三十", "3,000,000元"])
def test_supported_forms_not_unparsed(expression):
    tokens, unsupported = numeric_tokens(expression)
    assert len(tokens) == 1 and not unsupported


def test_percent_equivalent_and_disabled_person_term():
    assert not check_numbers(data("30%", "百分之三十"), [Segment(1, "百分之三十")])
    disabled = [{**PERSON[0], "enabled": False}]
    assert "PERSON_UNSUPPORTED" in codes(check_person_names({"host": "老张"}, [Segment(1, "请张三同志参加")], terms=disabled))


def test_malformed_numeric_expression_reported_not_crashed():
    assert "NUMERIC_UNPARSED" in codes(check_numbers(data("预算1.2.3元"), [Segment(1, "预算1.2.3元")]))
