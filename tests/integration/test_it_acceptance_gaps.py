"""G4 batch 3: real HTTP/graph/storage/DOCX, deterministic audio and ASR only.

03 §4.1 is the oracle; E1001 agrees with 01 §9.3. IT-24 real recall is
intentionally outside this batch: the ASR fixture measures plumbing, not recall.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import socket
import subprocess

import pytest
from fastapi.testclient import TestClient

from mmwf.api.deps import create_session_token
from mmwf.app.services.ingest import make_ingest_service
from mmwf.app.services.minutes import MinutesService
from mmwf.domain.asr_types import TranscriptResult, TranscriptSegmentResult
from mmwf.graph.runner import GraphRunner
from mmwf.infra.db import connect_ctx
from mmwf.infra.migrations.runner import migrate
from mmwf.main import create_app
from mmwf.settings import load_settings

API = "/api/v1/meetings"
RECORD = "[00:00:12] 张三：研究季度预算执行。\n[00:00:30] 李四：决定本周完成预算核对。"
TEMPLATES = [
    ("tpl-regular-meeting-v1", ["会议基本情况", "工作汇报与讨论", "议定事项", "下阶段工作安排"]),
    ("tpl-party-group-v1", ["会议基本情况", "会议听取和审议的事项", "会议决定的事项", "会议要求"]),
    ("tpl-special-topic-v1", ["会议基本情况", "专题研究事项", "议定事项", "工作要求"]),
    ("tpl-coordination-v1", ["会议基本情况", "协调事项及各方意见", "议定事项", "责任分工与时限"]),
]


class AudioStub:
    """Small on-disk chunk tokens carry duration, avoiding hours of PCM/ffmpeg."""
    def __init__(self):
        self.durations = {}
        self.chunks = {}

    def probe_duration_ms(self, path):
        return self.durations[Path(path)]

    def cut_chunk(self, src, dst, start_ms, end_ms):
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"deterministic audio token")
        self.chunks[dst] = (start_ms, end_ms)


class AsrStub:
    name = "it-asr"

    def __init__(self, audio):
        self.audio = audio
        self.calls = []

    def available(self):
        return True

    def transcribe(self, path, *, hotwords):
        path = Path(path)
        assert path.is_file()
        self.calls.append(path)
        start, end = self.audio.chunks[path]
        # Two independent sentinels per block prove both ends survive concatenation.
        texts = [f"研究第{start}毫秒预算安排。", f"决定本周完成第{end}毫秒预算核对。"]
        span = end - start
        return TranscriptResult(segments=(
            TranscriptSegmentResult(1, texts[0], 0, 1000, 0.99),
            TranscriptSegmentResult(2, texts[1], span - 1000, span, 0.99),
        ), engine=self.name, model="deterministic", avg_confidence=0.99)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("acceptance tests must not invoke network or subprocesses")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = load_settings(Path(__file__).resolve().parents[2] / "config")
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={
        "data_dir": tmp_path / "data", "log_dir": tmp_path / "logs"})})
    migrate(cfg.app.data_dir / "mmwf.db")
    audio = AudioStub()
    asr = AsrStub(audio)
    # Replace adapters, leaving ingest planning, nodes, routes and exporters real.
    monkeypatch.setattr("mmwf.app.services.ingest.make_ingest_service",
                        lambda settings, **kw: make_ingest_service(settings, adapter=audio, **kw))
    monkeypatch.setattr("mmwf.adapters.asr.build_from_settings", lambda settings: asr)
    calls = []
    original = GraphRunner._bump_node

    def counted(self, name):
        calls.append((self.meeting_id, name))
        original(self, name)

    monkeypatch.setattr(GraphRunner, "_bump_node", counted)
    app = create_app(cfg)
    # No lifespan worker: every action and gate transition is driven synchronously.
    client = TestClient(app)
    client.headers["X-MMWF-Token"] = create_session_token()
    # Register the four definitions in the catalogue referenced by meeting.template_id.
    store = app.state.runtime.template_store()
    for template_id, _ in TEMPLATES:
        store.create(store.select(template_id), template_id=template_id)
    try:
        yield SimpleNamespace(client=client, rt=app.state.runtime, audio=audio,
                              asr=asr, calls=calls, root=tmp_path)
    finally:
        client.close()


def ok(response):
    assert response.status_code == 200
    body = response.json()
    assert body["ok"], body
    return body["data"]


def create(flow, **inputs):
    return ok(flow.client.post(API, json={
        "title": "季度预算工作会议", "classification": "internal",
        "template_id": "tpl-regular-meeting-v1", "meeting_time": "2026-10-15 09:00",
        "location": "第一会议室", "host": "张三", "attendees": ["张三", "李四"],
        "recorder": "李四", **inputs}))


def start(flow, mid):
    status = ok(flow.client.post(f"{API}/{mid}/start"))
    assert status["status"] == "G2_awaiting"
    assert status["pending_gate"]["gate"] == "G2"
    return status["pending_gate"]


def approve(flow, mid, pending):
    return ok(flow.client.post(f"{API}/{mid}/gate/{pending['gate']}/decide", json={
        "decision": "approve", "checkpoint_id": pending["checkpoint_id"], "operator": "验收员"}))


def finish(flow, mid, pending):
    g3 = approve(flow, mid, pending)
    assert g3["next_state"] == "G3_awaiting"
    assert g3["pending_gate"]["gate"] == "G3"
    final = approve(flow, mid, g3["pending_gate"])
    assert final["next_state"] == "archived"
    with flow.rt.runner(mid) as runner:
        assert runner.status().status == "archived"
        state = runner.values()
    with connect_ctx(flow.rt.db_path) as conn:
        assert conn.execute("SELECT status FROM meeting WHERE meeting_id=?", (mid,)).fetchone()[0] == "archived"
        assert conn.execute("SELECT status FROM task_state WHERE task_id=?", (mid,)).fetchone()[0] == "archived"
    fs = flow.rt.filestore(mid)
    path = fs.resolve(state["export_paths"]["docx"])
    assert path.is_file() and path.stat().st_size > 0
    assert fs.read_json("archive/manifest.json")["export_paths"] == state["export_paths"]
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = ["".join(t.text or "" for t in p.findall(".//w:t", ns))
                  for p in root.findall(".//w:body/w:p", ns)]
    final_version = MinutesService(flow.rt.cfg).store().get(mid)
    assert final_version["is_final"] == 1
    assert final_version["structured_json"] == state["minutes_structured"]
    assert final_version["markdown"] == state["minutes_markdown"]
    return state, paragraphs


@pytest.mark.parametrize("duration_ms,blocks", [(300_000, 1), (9_000_000, 6)],
                         ids=["IT-01-five-minutes", "IT-02-two-and-half-hours"])
def test_audio_two_approvals_archive_without_chunk_loss(flow, duration_ms, blocks):
    source = flow.root / "meeting.wav"
    source.write_bytes(b"audio fixture token")
    flow.audio.durations[source] = duration_ms
    created = create(flow, audio_paths=[str(source)])
    assert created["succeeded"] == 1 and created["failed"] == 0
    assert created["chunk_count"] == blocks
    mid = created["meeting_id"]
    pending = start(flow, mid)
    with flow.rt.runner(mid) as runner:
        at_g2 = runner.values()
    plan = at_g2["chunk_plan"]
    assert len(plan) == blocks
    assert plan[0]["start_ms"] == 0 and plan[-1]["end_ms"] == duration_ms
    assert all(b["start_ms"] <= a["end_ms"] for a, b in zip(plan, plan[1:]))
    expected_paths = [Path(p) for item in plan for p in item["chunk_files"]]
    assert len(expected_paths) == len(set(expected_paths)) == blocks
    assert flow.asr.calls == expected_paths
    expected_texts = [text for item in plan for text in (
        f"研究第{item['start_ms']}毫秒预算安排。", f"决定本周完成第{item['end_ms']}毫秒预算核对。")]
    transcript = at_g2["transcript"]
    assert [s["text"] for s in transcript] == expected_texts
    assert [s["seq"] for s in transcript] == list(range(1, 2 * blocks + 1))
    stored = flow.rt.filestore(mid).read_json("transcript/segments.json")
    assert stored["segments"] == transcript
    state, paragraphs = finish(flow, mid, pending)
    for text in expected_texts:
        assert text in state["record_markdown"]
        assert text in state["minutes_markdown"]
        # The DOCX typography pass inserts spaces between Chinese and digits.
        assert any(text in paragraph.replace(" ", "") for paragraph in paragraphs)
    assert flow.asr.calls == expected_paths  # Neither approval repeats ASR.


def test_it03_text_import_enters_review_without_transcribe(flow):
    mid = create(flow, record_markdown=RECORD)["meeting_id"]
    pending = start(flow, mid)
    assert flow.calls[0] == (mid, "review")
    assert not any(node in {"ingest", "transcribe"} for _, node in flow.calls)
    assert flow.asr.calls == []
    state, paragraphs = finish(flow, mid, pending)
    assert "研究季度预算执行" in state["minutes_markdown"]
    assert any("研究季度预算执行" in p for p in paragraphs)
    assert flow.asr.calls == []


def test_it13_empty_owner_defaults_through_final_docx(flow):
    mid = create(flow, record_markdown=RECORD)["meeting_id"]
    pending = start(flow, mid)
    with flow.rt.runner(mid) as runner:
        assert runner.values()["resolutions"]
        assert all(not item.get("owner") for item in runner.values()["resolutions"])
    state, paragraphs = finish(flow, mid, pending)
    items = [r for t in state["minutes_structured"]["topics"] for r in t["resolutions"]]
    assert items and all(r["owner"] == "暂无" for r in items)
    assert "责任人：暂无" in state["minutes_markdown"]
    assert any("责任人：暂无" in p for p in paragraphs)


def test_it14_three_submitted_tasks_processed_in_order_with_visible_status(flow):
    mids = [create(flow, title=f"第{i}次预算工作会议", record_markdown=RECORD)["meeting_id"]
            for i in range(1, 4)]

    def visible(expected):
        listed = {row["meeting_id"]: row["status"] for row in ok(flow.client.get(API))}
        assert {mid: listed[mid] for mid in mids} == dict(zip(mids, expected))
        for mid, status in zip(mids, expected):
            detail = ok(flow.client.get(f"{API}/{mid}"))
            assert detail["status"] == status
            if status != "created":
                assert detail["run"]["status"] == status

    visible(["created"] * 3)
    for index, mid in enumerate(mids):
        pending = start(flow, mid)
        visible(["archived"] * index + ["G2_awaiting"] + ["created"] * (2 - index))
        g3 = approve(flow, mid, pending)
        assert g3["next_state"] == "G3_awaiting"
        visible(["archived"] * index + ["G3_awaiting"] + ["created"] * (2 - index))
        assert approve(flow, mid, g3["pending_gate"])["next_state"] == "archived"
        visible(["archived"] * (index + 1) + ["created"] * (2 - index))
    assert [mid for mid, node in flow.calls if node == "review"] == mids
    assert [mid for mid, node in flow.calls if node == "archive"] == mids
    assert [mid for mid, _ in flow.calls] == sorted(
        [mid for mid, _ in flow.calls], key=mids.index)


def test_it14_audio_starts_queue_in_submission_order(flow, monkeypatch):
    # 03 的窄义：自动处理段串行，人工闸口等待不占队列；不要求后台调度进程。
    mids = []
    for index in range(3):
        source = flow.root / f"queued-{index}.wav"
        source.write_bytes(b"audio token")
        flow.audio.durations[source] = 300_000
        mids.append(create(flow, audio_paths=[str(source)])["meeting_id"])
    entered = [Event() for _ in mids]
    release = [Event() for _ in mids]
    original = flow.asr.transcribe

    def blocked(path, *, hotwords):
        index = mids.index(Path(path).parents[2].name)
        entered[index].set()
        assert release[index].wait(10), "test failed to release ASR"
        return original(path, hotwords=hotwords)

    monkeypatch.setattr(flow.asr, "transcribe", blocked)
    futures = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            for index, mid in enumerate(mids):
                futures.append(pool.submit(start, flow, mid))
                # Real queue admission, observed under its own condition; no sleeps.
                with flow.rt._processing_condition:
                    assert flow.rt._processing_condition.wait_for(
                        lambda: flow.rt._processing_next == index + 1, timeout=10)
                if index == 0:
                    assert entered[0].wait(10)
            assert not entered[1].is_set() and not entered[2].is_set()
            for index in range(3):
                assert entered[index].wait(10)
                # Later queued tasks remain individually visible and unstarted.
                statuses = {row["meeting_id"]: row["status"]
                            for row in ok(flow.client.get(API))}
                for later in mids[index + 1:]:
                    assert statuses[later] == "created"
                    assert not any(mid == later for mid, _ in flow.calls)
                release[index].set()
                pending = futures[index].result(timeout=10)
                assert pending["gate"] == "G2"
                assert ok(flow.client.get(f"{API}/{mids[index]}"))["run"]["status"] == "G2_awaiting"
            assert [mid for mid, node in flow.calls if node == "transcribe"] == mids
        finally:
            for event in release:
                event.set()
    for mid, future in zip(mids, futures):
        finish(flow, mid, future.result())


def test_it22_txt_audio_reports_e1001_in_chinese(flow):
    source = flow.root / "not-audio.txt"
    source.write_text("this is not audio", encoding="utf-8")
    result = create(flow, audio_paths=[str(source)])
    assert result["succeeded"] == 0 and result["failed"] == 1
    failure, = result["audio"]
    assert failure["code"] == "E1001"
    assert "不支持" in failure["message"] and "格式" in failure["message"]
    assert failure["message"] in result["warning"]
    assert flow.audio.chunks == {} and flow.asr.calls == []
    assert flow.client.post(f"{API}/{result['meeting_id']}/start").json()["code"] == "E1004"


def test_status_sync_preserves_last_error_after_invocation(flow, monkeypatch):
    mid = create(flow, record_markdown=RECORD)["meeting_id"]
    start(flow, mid)
    with connect_ctx(flow.rt.db_path) as conn:
        conn.execute("UPDATE task_state SET last_error=? WHERE task_id=?",
                     ("保留此前诊断", mid))
    with flow.rt.runner(mid) as runner:
        # Isolate the post-invoke status publication from business-node writes.
        monkeypatch.setattr(runner.app, "invoke", lambda *args, **kwargs: None)
        runner._invoke(None)
    with connect_ctx(flow.rt.db_path) as conn:
        row = conn.execute("SELECT status,last_error FROM task_state WHERE task_id=?", (mid,)).fetchone()
        assert tuple(row) == ("G2_awaiting", "保留此前诊断")


@pytest.mark.parametrize("status", ["cancelled", "unknown-status"])
def test_status_sync_does_not_publish_cancelled_or_unknown_meeting_status(flow, status):
    mid = create(flow, record_markdown=RECORD)["meeting_id"]
    start(flow, mid)
    with connect_ctx(flow.rt.db_path) as conn:
        conn.execute("UPDATE task_state SET last_error=? WHERE task_id=?", ("保留此前诊断", mid))
    with flow.rt.runner(mid) as runner:
        if status == "cancelled":
            assert runner.cancel(reason="用户取消").status == "cancelled"
        else:
            runner._touch(status, last_error="无效状态")
    with connect_ctx(flow.rt.db_path) as conn:
        assert conn.execute("SELECT status FROM meeting WHERE meeting_id=?", (mid,)).fetchone()[0] == "G2_awaiting"
        row = conn.execute("SELECT status,last_error FROM task_state WHERE task_id=?", (mid,)).fetchone()
        assert tuple(row) == ("cancelled" if status == "cancelled" else "G2_awaiting", "保留此前诊断")


@pytest.mark.parametrize("template_id,headings", TEMPLATES, ids=[t[0] for t in TEMPLATES])
def test_it25_template_structure_reaches_final_docx(flow, template_id, headings):
    mid = create(flow, template_id=template_id, record_markdown=RECORD)["meeting_id"]
    template = flow.rt.template_store().select(template_id)
    assert list(template.section_order) == headings
    state, paragraphs = finish(flow, mid, start(flow, mid))
    sections = state["minutes_structured"]["sections"]
    assert [s["heading"] for s in sections] == headings
    assert [s["number"] for s in sections] == ["1", "2", "3", "4"]
    # Verify each binding's content, not just four nonempty headings.
    assert sections[0]["items"] == ["时间：2026-10-15 09:00", "地点：第一会议室",
        "主持：张三", "参会人员：张三、李四", "记录人：李四"]
    assert any("研究季度预算执行" in item["text"] for item in sections[1]["items"])
    assert any("决定本周完成预算核对" in entry["text"]
               for item in sections[2]["items"] for entry in item["items"])
    assert all(item["text"] == "待补充" for item in sections[3]["items"])
    assert [line[3:] for line in state["minutes_markdown"].splitlines()
            if line.startswith("## ") and line[3:] != "待核对清单"] == headings
    docx_headings = [f"{number}、{heading}" for number, heading in zip("一二三四", headings)]
    positions = [next(i for i, p in enumerate(paragraphs) if p == h) for h in docx_headings]
    assert positions == sorted(positions)
    for section, begin, end in zip(sections, positions, positions[1:] + [len(paragraphs)]):
        region = "\n".join(paragraphs[begin:end])
        for item in section["items"]:
            if isinstance(item, str):
                assert item in region
            else:
                assert item["heading"] in region
                assert item["text"] in region
                for entry in item["items"]:
                    assert entry["text"] in region
