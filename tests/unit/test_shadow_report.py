"""SYNTHETIC 报告同源和证据边界。"""
from dataclasses import replace
import html
import json

from mmwf.domain.shadow import aggregate_metrics, project_records, render_report
from mmwf.domain.shadow.models import freeze, thaw
from test_shadow_scoring import vector


def test_report_json_markdown_records_same_source_and_pending_ledger():
    b, s, t = vector(20)
    summary = aggregate_metrics(b, s, t)
    data = thaw(summary.data)
    records = project_records(summary)
    markdown = render_report(summary)
    assert records["records"] == data["records"]
    assert data["quality_pass"] and not data["eligible_for_stage2"]
    assert data["real_meetings_counted"] == 0 and data["real_run_status"] == "PENDING"
    assert "SYNTHETIC / 合成数据，非真实验收结果" in markdown
    embedded = html.unescape(markdown.split("<pre>", 1)[1].split("</pre>", 1)[0])
    assert json.loads(embedded) == data
    for key in ("Q2", "Q3", "Q4", "R-5", "other"):
        assert data["evidence_ledger"][key].startswith("PENDING")
    assert all(f"阶段{i}" in markdown for i in range(4))
    assert data["thresholds"]["agreement"]["confirmed"] is True


def test_table_escapes_any_name_notes_and_full_data_html():
    b, s, t = vector()
    meetings = [thaw(m) for m in b.meetings]
    evil = '|<script>alert("x")</script>\n`[*]\\\r'
    meetings[0].update(name=evil, failure_reason=evil)
    summary = aggregate_metrics(replace(b, meetings=tuple(freeze(m) for m in meetings)), s, t)
    text = render_report(summary)
    assert "<script>" not in text and "&#124;" in text and "<br>" in text
    assert project_records(summary)["records"][0]["meeting_name"] == evil
    assert "&lt;script&gt;" in text


def test_incomplete_failure_diagnostics_and_all_planned_rows():
    b, s, t = vector()
    meetings = [thaw(m) for m in b.meetings]
    meetings[0].update(status="blocked", failure_reason="SYNTHETIC 阻断")
    s["scores"].pop(0)
    summary = aggregate_metrics(replace(b, meetings=tuple(freeze(m) for m in meetings)), s, t)
    data = thaw(summary.data)
    assert data["status"] == "INCOMPLETE"
    assert len(data["records"]) == len(data["meetings"]) == 2
    assert data["records"][0]["topics_matched"] is None
    assert "SYNTHETIC 阻断" in render_report(summary)
    assert not data["quality_pass"]
