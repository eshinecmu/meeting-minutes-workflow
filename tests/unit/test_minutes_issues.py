"""统一问题清单的结构合同与两种表示。"""
from mmwf.domain.minutes.issues import (
    aggregate_issues, issues_payload, make_issue, normalize_issue, render_issues_text, replace_issues,
)
from mmwf.domain.minutes.post_validation import post_validate


def issue(source="consistency", code="NUMERIC_MISMATCH", **kw):
    return make_issue(code, "topics[0].resolutions[0].content", "请核对", source=source, **kw)


def test_three_sources_normalized_sorted_and_deduplicated():
    extraction = {"rule": "chunk_meta_missing", "detail": "缺元信息", "chunk": 1, "keys": ["host"]}
    template = {"rule": "required_pattern", "field": "host", "detail": "必填", "source": "template"}
    hard = issue("grounding", "UNGROUNDED", level="error", blocking=True)
    items = aggregate_issues([extraction], [template], [issue(), hard, issue()])
    assert len(items) == 4 and items[0] == hard
    assert {i["source"] for i in items} == {"extraction", "template", "consistency", "grounding"}
    for i in items:
        assert set(i) == {"code", "level", "blocking", "source", "target", "message", "evidence", "suggestion", "rule", "detail"}
        assert i["rule"] == i["code"].lower() and i["detail"] == i["message"]
    assert next(i for i in items if i["source"] == "extraction")["evidence"] == {"chunk": 1, "keys": ["host"]}


def test_dedup_structural_not_message():
    a, b = issue(level="info", suggestion="建议一"), issue(level="warning", suggestion="建议二")
    b["message"] = b["detail"] = "不同文案"
    items = aggregate_issues([a, b])
    assert len(items) == 1 and items[0]["level"] == "warning"
    assert items[0]["suggestion"] == "建议一；建议二"
    assert len(aggregate_issues([a, issue(evidence={"actual": 30})])) == 2
    assert not issue(level="error", blocking=True)["blocking"]
    assert not issue("grounding", "GROUNDING_FUZZY", level="error", blocking=True)["blocking"]


def test_replace_fixed_diagnostics_and_replay():
    old = [issue(), issue("extraction", "CHUNK_META_MISSING")]
    current = replace_issues(old, [], source="consistency")
    assert len(current) == 1 and current[0]["source"] == "extraction"
    assert replace_issues(current, [], source="consistency") == current
    assert replace_issues(old, [], source="consistency", targets=["host"]) == aggregate_issues(old)


def test_text_and_json_one_to_one_complete_false():
    items = [issue(), issue("granularity", "OWNER_DEFAULTED", level="info")]
    payload = issues_payload("meeting-1", items, complete=False, source_fingerprint="abc")
    text = render_issues_text("meeting-1", items, complete=False, source_fingerprint="abc", source_version=2)
    assert text == render_issues_text("meeting-1", items, complete=False, source_fingerprint="abc", source_version=2)
    assert "\r" not in text and text.endswith("\n") and text.encode("utf-8").decode("utf-8") == text
    assert "历史检查未完成" in text and "abc" in text
    assert payload["counts"] == {"error": 0, "warning": 1, "info": 1}
    assert len([line for line in text.splitlines() if line.startswith("[")]) == len(payload["issues"])
    assert "当前清单无问题" in render_issues_text("m", [])
    assert "历史检查未完成" in render_issues_text("m", [], complete=False)


def test_no_raw_model_output_or_traceback_in_message():
    normalized = normalize_issue({"rule": "chunk_failed", "detail": "secret", "traceback": "secret stack", "raw_output": "secret model"})
    assert "secret" not in render_issues_text("m", [normalized])


def test_post_validate_recomputes_instead_of_accumulating():
    minutes = {"topics": [{"discussion": "", "resolutions": [{"content": "通过", "source_quote": "通过"}]}]}
    kwargs = {"record_markdown": "通过", "extraction_issues": [{"rule": "chunk_meta_missing", "keys": ["host"]}]}
    assert post_validate(minutes, **kwargs) == post_validate(minutes, **kwargs)


def test_all_template_checks_aggregated_with_extraction_and_grounding():
    from mmwf.domain.template import MinutesTemplate, RequiredElement
    template = MinutesTemplate("t", "测试", "test", (RequiredElement("host", "主持人"),),
                               style={"readings_allowed": ["会议认为", "会议决定"], "readings_forbidden": ["差不多"]})
    minutes = {"host": "", "topics": [{"name": "导航", "discussion": "同意差不多通过", "readings": ["会议认为"], "source_refs": []}]}
    items = post_validate(minutes, template=template, record_markdown="同意差不多通过",
                          extraction_issues=[{"rule": "chunk_meta_missing", "keys": ["host"]}])
    assert {i["source"] for i in items} >= {"extraction", "template", "grounding"}
    assert {i["code"] for i in items} >= {"MISSING_REQUIRED_ELEMENT", "FORBIDDEN_EXPRESSION", "READING_LEVEL_MISMATCH", "UNGROUNDED"}
    assert all(i["level"] == "warning" and not i["blocking"] for i in items if i["source"] == "template")


def test_legacy_exception_details_are_not_user_messages():
    for code in ("chunk_llm_failed", "chunk_bad_json", "chunk_schema_failed"):
        result = normalize_issue({"rule": code, "chunk": 1, "detail": "secret model body"})
        assert "secret" not in render_issues_text("m", [result])
        assert result["evidence"] == {"chunk": 1}
