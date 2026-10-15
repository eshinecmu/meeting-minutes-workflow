"""G2 批次 1：真实节点/存储/自检，模型与操作系统故障均确定性注入。"""
from io import BytesIO
from pathlib import Path
import socket
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from mmwf.adapters.llm import FakeLlmClient
from mmwf.api import system
from mmwf.api.routes._context import Runtime
from mmwf.api.routes._support import _load_recent_segments
from mmwf.app.services.export import ExportService
from mmwf.domain.asr_types import TranscriptResult, TranscriptSegmentResult, TranscriptWord
from mmwf.domain.docx import font_check
from mmwf.domain.errors import MmwfError, Severity
from mmwf.graph import nodes as N
from mmwf.graph.nodes.transcribe import make_transcribe
from mmwf.graph.runner import GraphRunner
from mmwf.infra.audit import verify_chain
from mmwf.infra.db import connect_ctx
from mmwf.infra.filestore import make_filestore
from mmwf.infra.migrations.runner import migrate
from mmwf.settings import ExtractionConfig, load_settings


RECORD = "[00:00:12] 张三：研究预算执行。"
FONTS = {"方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312", "宋体", "Times New Roman"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("DR 测试禁止网络")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = load_settings(Path(__file__).resolve().parents[2] / "config")
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={"data_dir": tmp_path})})
    db = tmp_path / "mmwf.db"
    migrate(db)
    with connect_ctx(db) as conn:
        conn.execute("INSERT INTO meeting (meeting_id,title,status,created_by,created_at,updated_at) VALUES ('dr','故障演练','record_ready','tester','now','now')")
    store = make_filestore("dr", tmp_path, encrypt_enabled=False)
    store.write_text("record/v1.md", RECORD, stage="record")
    return SimpleNamespace(cfg=cfg, db=db, store=store, root=tmp_path)


def selfcheck(env, monkeypatch):
    monkeypatch.setattr(system, "probe_network", lambda: system.CheckResult(
        name="network_probe", verdict="pass", detail={}))
    return system.run_selfcheck(db_path=env.db, env={},
        segment_loader=lambda: _load_recent_segments(env.db),
        font_report=font_check.check_fonts(installed=FONTS))


@pytest.mark.parametrize("fault", ["empty", "timestamps", "hotwords"])
def test_asr_fault_reaches_system_selfcheck(env, monkeypatch, caplog, fault):
    # 裁决 DR-01：§03 的 E2002 与 §01 §9.3 冲突；空结果取较窄的
    # E2003 静默失效（沿用 TranscriptResult.validated），E2002 仅超时。
    if fault == "empty":
        result = TranscriptResult((), "fake", "offline")
    else:
        words = (TranscriptWord("研", 0, 100),) if fault == "timestamps" else ()
        result = TranscriptResult((TranscriptSegmentResult(
            1, "研究预算执行工作安排", 0, 1000, .99, words=words),), "fake", "offline")
    received = []
    class Engine:
        name = "fake"
        def available(self):
            return True
        def transcribe(self, path, *, hotwords):
            received.append((path, hotwords))
            return result
    glossary = SimpleNamespace(
        prepare_hotwords=lambda: SimpleNamespace(words=["张三"], warnings=[]),
        correct_text=lambda text, **kwargs: SimpleNamespace(text=text, hits=0))
    node = make_transcribe(settings=env.cfg, provider_factory=lambda cfg: Engine(),
        glossary_factory=lambda cfg, mid: glossary)
    # 真图保留 transcribe 及其后续全部节点，只省掉音频解码。
    monkeypatch.setattr(N, "make_ingest", lambda deps: lambda state: {})
    monkeypatch.setattr(N, "make_transcribe", lambda deps: node)
    with Runtime(env.cfg).runner("dr") as runner:
        if fault == "hotwords":
            runner.start(audio_paths=["offline.wav"])
            assert any(e["code"] == "E2004" for e in runner.values()["errors"])
            code = "E2004"
        else:
            with pytest.raises(MmwfError) as caught:
                runner.start(audio_paths=["offline.wav"])
            assert caught.value.code == "E2003"
            assert caught.value.severity == Severity.FATAL
            assert "已暂停" in caught.value.user_message
            assert not runner.values().get("minutes_markdown")
            assert not runner.values().get("export_paths")
            assert "review" not in runner._runs
            assert "E2003" in caplog.text
            code = "E2003"
    assert received == [("offline.wav", ["张三"])]
    report = selfcheck(env, monkeypatch)
    assert report.overall == "fail"
    assert report.asr_quality.verdict == "fail"
    assert code in report.asr_quality.detail["alert_codes"]
    assert ("热词零命中" if fault == "hotwords" else "转写结果出现异常") in report.asr_quality.note
    assert verify_chain(env.db).ok
    assert not list(env.store.resolve("export").glob("*.docx"))
    with connect_ctx(env.db) as conn:
        assert conn.execute("SELECT count(*) FROM minutes_version").fetchone()[0] == 0
    # 故障修复后的真实转写完成记录必须清除旧告警，不能永久误报。
    result = TranscriptResult((TranscriptSegmentResult(
        1, "张三研究预算执行", 0, 1000, .99),), "fake", "offline", hotwords_applied=True)
    with Runtime(env.cfg).runner("dr") as recovered:
        recovered.start(audio_paths=["recovered.wav"])
    quality = selfcheck(env, monkeypatch).asr_quality
    # 本夹具未向分段库导入置信度，允许 unsupported，但旧故障不得残留。
    assert quality.verdict == "unsupported"
    assert quality.detail["anomaly"] is None
    assert not quality.detail.get("alert_codes")


def test_bad_schema_retries_twice_and_renders_issue_list(env, monkeypatch):
    # DR-04/IT-07：两次指 schema 修复重试；不另加两轮无效的成文调用。
    from mmwf.adapters import llm
    fake = FakeLlmClient(script=['{"topics": "invalid-list"}'])
    monkeypatch.setattr(llm, "make_llm_client", lambda cfg: fake)
    monkeypatch.setattr(N, "make_ingest", lambda deps: lambda state: {})
    monkeypatch.setattr(N, "make_transcribe", lambda deps: lambda state: {"transcript": []})
    monkeypatch.setattr(N, "make_review", lambda deps: lambda state: {"record_markdown": RECORD})
    cfg = env.cfg.model_copy(update={"extraction": ExtractionConfig(engine="llm")})
    rt = Runtime(cfg)
    with rt.runner("dr") as runner:
        status = runner.start()
        assert len(fake.calls) == 3  # 初次 + 2 次，真实 schema 校验与重试提示
        assert fake.calls[0]["prompt"] != fake.calls[1]["prompt"]
        assert all(c["has_schema"] for c in fake.calls)
        # 全部块失败，没有议题候选；G2 按既有空项规则跳过，停在 G3 审稿。
        assert status.status == "G3_awaiting"
        state = runner.values()
        failures = [i for i in state["issues"] if i["code"] == "E3002"]
        assert len(failures) == 1
        assert failures[0]["blocking"] is False
        assert "待核对清单" in state["minutes_markdown"]
        assert failures[0]["message"] in state["minutes_markdown"]
        assert "invalid-list" not in state["minutes_markdown"]
        assert len(fake.calls) == 3
    with rt.runner("dr") as restored:
        assert failures[0] in restored.values()["issues"]
        assert failures[0]["message"] in restored.values()["minutes_markdown"]


@pytest.mark.parametrize("existing", [False, True], ids=["first-write", "overwrite"])
def test_word_lock_reports_e5004_and_succeeds_after_release(env, monkeypatch, existing):
    service = ExportService(env.store, db_path=env.db)
    # Word locks the destination, not the staging file. An older export must
    # survive a failed atomic replacement unchanged, including its manifest.
    if existing:
        old = service.export_docx(installed_fonts=FONTS)
        env.store.write_text("record/v1.md", RECORD + "\n补充议题。", stage="record")
        old_bytes = env.store.resolve(old.rel_path).read_bytes()
    manifest = env.store.manifest_path.read_bytes()
    replace = Path.replace
    attempted = []
    def locked(path, target):
        if Path(target).suffix == ".docx":
            attempted.append(Path(target))
            raise PermissionError("injected Word lock")
        return replace(path, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", locked)
        with pytest.raises(MmwfError) as caught:
            service.export_docx(installed_fonts=FONTS)
    assert caught.value.code == "E5004"
    assert "关闭" in caught.value.user_message and "重试" in caught.value.user_message
    assert len(attempted) == 1  # no redundant restore against the locked target
    if existing:
        assert env.store.resolve(old.rel_path).read_bytes() == old_bytes
    else:
        assert not list(env.store.resolve("export").glob("*.docx"))
    assert env.store.manifest_path.read_bytes() == manifest
    assert env.store.verify_manifest() == []
    assert not list(env.store.store_root.rglob("*.tmp"))
    result = service.export_docx(installed_fonts=FONTS)
    assert env.store.exists(result.rel_path)
    assert service.export_docx(installed_fonts=FONTS).reused
    assert len(list(env.store.resolve("export").glob("*.docx"))) == 1


def test_deleted_font_warns_e5002_and_actual_docx_uses_fallback(env, monkeypatch):
    # 字体清单替身只报告仍存在的临时字体文件；实际 unlink 后走真实预检/渲染。
    font_dir = env.root / "fonts"
    font_dir.mkdir()
    for name in FONTS:
        (font_dir / (name + ".ttf")).write_bytes(b"font inventory fixture")
    monkeypatch.setattr(font_check, "list_installed_fonts",
        lambda: {p.stem for p in font_dir.glob("*.ttf")})
    assert font_check.check_fonts().all_installed
    (font_dir / "仿宋_GB2312.ttf").unlink()
    result = ExportService(env.store, db_path=env.db).export_docx()
    assert result.font_report.resolve("仿宋_GB2312") == "宋体"
    assert result.to_dict()["warning_codes"] == ["E5002"]
    assert any("仿宋_GB2312" in w for w in result.font_warnings)
    assert result.to_dict()["font_warnings"]
    with ZipFile(BytesIO(env.store.resolve(result.rel_path).read_bytes())) as archive:
        xml = archive.read("word/document.xml").decode()
    assert 'w:eastAsia="宋体"' in xml
    assert 'w:eastAsia="仿宋_GB2312"' not in xml


@pytest.mark.parametrize("operation", ["status", "start"])
def test_corrupt_checkpoint_reports_e4002_with_upstream_rerun(env, operation):
    checkpoint = env.root / "damaged-checkpoint.db"
    checkpoint.write_bytes(b"not a SQLite database\x00fault injection")
    before = checkpoint.read_bytes()
    with GraphRunner("dr", checkpoint_path=checkpoint) as runner:
        with pytest.raises(MmwfError) as caught:
            getattr(runner, operation)()
    assert caught.value.code == "E4002"
    assert "上游" in caught.value.user_message and "重跑" in caught.value.user_message
    assert checkpoint.read_bytes() == before
    assert env.store.read_text("record/v1.md") == RECORD
    assert not list(env.store.resolve("export").glob("*.docx"))
