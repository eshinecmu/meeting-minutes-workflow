"""Electron 只做源码契约检查；不启动 Node、浏览器或声称视觉通过。"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2] / 'src/desktop/renderer'


def test_edit_and_checkpoint_wiring():
    js = (ROOT / 'pages/gate.js').read_text()
    assert '/gate/${level}/decide' in js
    assert 'api.gateRequest(roundToken, {' in js
    client = (ROOT / 'api-client.js').read_text()
    assert r"check\u0070oint_id" in client
    assert '[GATE_ROUND_KEY]: roundToken' in client
    assert "decision === 'edit' ? { payload: draft }" in js
    assert 'pending.payload.topics' in js
    assert "['title', 'discussion', 'readings']" in js
    assert "['text', 'owner', 'deadline']" in js
    assert 'draft = { minutes: editableMinutes' in js
    assert 'currentCheckpoint()' in js and '旧草稿已作废' in js
    assert 'draftReviewed.clear()' in js and 'draft = {}' in js
    assert 'submitting = true' in js and 'el.disabled = true' in js


def test_four_factors_and_draft_filter():
    js = (ROOT / 'pages/gate.js').read_text()
    for name in ('raw', 'extracted', 'risk', 'basis'):
        assert f'factorItem(FACTOR_LABELS.{name}' in js
    assert '!(serverConfirmed || draftReviewed)' in js
    assert 't.confirmed === true' in js and 'draftReviewed.has(index)' in js
    assert 'checked = true' in js and "renderItems(e.target.checked)" in js
    assert '已核对（未提交）' in js and '刷新后需重新核对' in js
    assert '来源转写置信' in js and '未提供' in js and 't.low_confidence' in js
    assert 'timecode(source.start_ms)' in js and 'raw.scrollIntoView' in js
    assert 'ms / 1000' in js


def test_server_diff_and_styles():
    js = (ROOT / 'pages/gate.js').read_text()
    css = (ROOT / 'styles/app.css').read_text()
    assert "api.call('POST', `/meetings/${meeting.meeting_id}/gate/${level}/diff`, api.gateRequest(roundToken, { payload: draft }))" in js
    assert 'renderDiff(diffWrap, data.segments)' in js
    assert '系统生成版' in js and '人工修改版' in js
    assert 'diff-${segment.type}' in js
    assert "segment.type === 'unchanged'" in js and 'pre.hidden = true' in js
    assert 'segment.elided' in js
    for name in ('changed', 'added', 'removed', 'unchanged'):
        assert re.search(r'\.diff-' + name + r'\s*\{[^}]*color:', css)


def test_structured_preview_issues_and_download():
    js = (ROOT / 'pages/gate.js').read_text()
    client = (ROOT / 'api-client.js').read_text()
    for text in ('data.minutes_markdown', 'data.issues', 'source_refs', 'issue-${issue.level}', 'FUZZY', '原文有字词差异，待核对', 'api.downloadIssues', 'pending_decisions'):
        assert text in js
    assert '/issues?format=text' in client
    assert "'X-MMWF-Token': TOKEN" in client
    assert 'URL.revokeObjectURL' in client


def test_legacy_canonical_lists_and_readonly_sources_are_optional():
    js = (ROOT / 'pages/gate.js').read_text()
    assert 'editableMinutes(data.minutes_structured || {})' in js
    assert '(value.topics || []).map' in js
    assert '(topic[kind] || []).map' in js
    assert 'data.minutes_structured?.topics?.[index] || {}' in js
    assert 'sourceText(original[kind]?.[itemIndex])' in js
    assert 'function sourceText(item = {})' in js
    assert "return '暂无时间信息'" in js  # 施工卡 §2.3 固定缺时间文案。
