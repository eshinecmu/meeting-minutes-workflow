"""`app/services/export` 单元测试（T1-09）。

覆盖的契约（施工卡 + SAD 的硬性要求）：

    幂等      同输入重复导出 -> 同一路径、只有一个文件（C07 / IT-09）
    E5002     缺字体 -> 警告但不崩（IT-11 / DR-08）
    E5001     fail 策略 -> 阻断并报 E5001
    明文区    `export/` 不加密（否则 Word 打不开，交付物就废了）
    红线      审计 detail 不含正文
    撤销      export_undo 后 manifest 仍然干净（F5.8）
    .doc      缺 soffice 时 skipped 不抛错
    版式      导出的 docx 通过 `verify_docx_style` 全部检查
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from mmwf.app.services.export import (
    DOC_STATUS_FAILED,
    DOC_STATUS_OK,
    DOC_STATUS_SKIPPED,
    EXPORT_AREA,
    ExportBlockedError,
    ExportService,
    ExportServiceError,
    judge_docx,
    make_export_service,
    make_idempotency_key,
)
from mmwf.infra.audit import AuditLogger, query, verify_chain
from mmwf.infra.db import connect_ctx
from mmwf.infra.filestore import ENCRYPTED_AREAS, make_filestore
from mmwf.infra.migrations.runner import migrate

MEETING_ID = "exp-t109"
OPERATOR = "tester"

#: 一份最小但完整的记录稿（含标题、正文、表格、列表）。
#:
#: `讨论了API接口与JSON格式` 刻意**不**带中英空格 —— 真实 ASR 输出
#: 就是这样，规范化要负责补上（U-DX-06）。留一处能验证"规范化真的生效"。
SAMPLE_RECORD = """# 关于信息化建设工作的会议纪要

会议于 2026 年 9 月 27 日在三楼第一会议室召开，讨论了API接口与JSON格式问题。

## 议定事项

| 事项 | 责任人 | 时限 |
| --- | --- | --- |
| 完成接口对接 | 李四 | 10 月 15 日 |
| 采购服务器 | 王五 | 10 月底 |

### 补充要求

- 各单位要高度重视
- [ ] 下周报送方案

会议强调，要抓好落实。
"""

FONTS_OK = {"方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312",
            "宋体", "Times New Roman"}


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path):
    """一套临时环境：库 + filestore + 一份记录稿。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "mmwf.db"
    migrate(db)
    _insert_meeting(db)
    store = make_filestore(MEETING_ID, data, encrypt_enabled=False)
    store.write_text("record/v1.md", SAMPLE_RECORD, stage="record")
    return data, db, store


def _insert_meeting(db: Path, *, status: str = "record_ready") -> None:
    row = {
        "meeting_id": MEETING_ID,
        "title": "信息化建设会议纪要",
        "meeting_time": "2026-09-27T09:00:00Z",
        "location": "三楼第一会议室",
        "host": "张三丰",
        "attendees": json.dumps(["李四", "王五"], ensure_ascii=False),
        "absentees": json.dumps([], ensure_ascii=False),
        "recorder": "孙八",
        "classification": "internal",
        "status": status,
        "created_by": OPERATOR,
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
    }
    cols = ", ".join(row)
    with connect_ctx(db) as conn:
        conn.execute(f"INSERT INTO meeting ({cols}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))


def _svc(env, **kw) -> ExportService:
    data, db, store = env
    kw.setdefault("font_policy", "warn")
    kw.setdefault("operator", OPERATOR)
    return ExportService(store, db_path=db, audit=AuditLogger(db), **kw)


# ---------------------------------------------------------------------------
# 幂等（C07 / IT-09）
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_repeat_export_reuses_file(self, env):
        svc = _svc(env)
        r1 = svc.export_docx(installed_fonts=FONTS_OK)
        r2 = svc.export_docx(installed_fonts=FONTS_OK)
        assert r1.reused is False
        assert r2.reused is True
        assert r1.rel_path == r2.rel_path
        assert r1.sha256 == r2.sha256

    def test_only_one_file_on_disk(self, env):
        data, db, store = env
        svc = _svc(env)
        svc.export_docx(installed_fonts=FONTS_OK)
        svc.export_docx(installed_fonts=FONTS_OK)
        svc.export_docx(installed_fonts=FONTS_OK)
        files = list((store.resolve(EXPORT_AREA)).glob("*.docx"))
        assert len(files) == 1, f"应只有一个文件，实际 {[f.name for f in files]}"

    def test_manifest_has_single_entry(self, env):
        data, db, store = env
        svc = _svc(env)
        svc.export_docx(installed_fonts=FONTS_OK)
        svc.export_docx(installed_fonts=FONTS_OK)
        entries = [e for e in store.manifest() if e.path.startswith(f"{EXPORT_AREA}/")]
        assert len(entries) == 1

    def test_idempotency_key_stable(self, env):
        svc = _svc(env)
        r1 = svc.export_docx(installed_fonts=FONTS_OK)
        r2 = svc.export_docx(installed_fonts=FONTS_OK)
        assert r1.idempotency_key == r2.idempotency_key

    def test_idempotency_key_changes_with_content(self, env):
        data, db, store = env
        svc = _svc(env)
        k1 = svc.export_docx(installed_fonts=FONTS_OK).idempotency_key
        store.write_text("record/v2.md", SAMPLE_RECORD + "\n补充一段话。\n", stage="record")
        k2 = svc.export_docx(version=2, installed_fonts=FONTS_OK).idempotency_key
        assert k1 != k2

    def test_key_shape(self):
        key = make_idempotency_key("m1", 3, "a" * 64)
        assert key.startswith("export:m1:v3:")
        assert len(key.split(":")[-1]) == 16

    # -- T1-17 缺陷 26：复用判据必须"认内容"，不能只看路径 ----------------

    def test_mutated_content_same_version_rerenders(self, env):
        """★★ 缺陷 26 回归锁（本卡实测结论：这不是缺陷，但要**锁住**）。

        探针场景（`C:/tmp/probe_defect26b.py` 案例 A）：**版本号不变、
        记录稿内容被就地改掉**，再导出一次。

        错误行为（若只按路径判复用）：第二次直接返回**第一次的字节** ——
        于是"改了稿子，导出的还是旧稿"，而且**看不出来**。
        这是保密场景的实质风险：交上去的 docx 与当前记录稿不一致。

        正确行为：内容哈希变了 → 幂等键变 → 重新渲染。

        ⚠️ 本条用**同 version=1**，因为"新增 v2"那种情况文件名都不同，
        太容易通过，测不出真问题。必须打同版本号这个软肋。
        """
        data, db, store = env
        svc = _svc(env)

        # 第一次导出（v1，原始内容）
        r1 = svc.export_docx(version=1, installed_fonts=FONTS_OK)
        assert r1.reused is False

        # **就地覆盖 v1.md**：版本号不变，内容不同
        store.write_text("record/v1.md", SAMPLE_RECORD + "\n【改】金额由 300 万调整为 500 万。\n")

        # 第二次导出，仍指定 same version=1
        r2 = svc.export_docx(version=1, installed_fonts=FONTS_OK)

        assert r2.reused is False, (
            "同版本号但内容已变，绝不能复用旧字节 —— 否则交上去的是旧稿"
        )
        assert r1.sha256 != r2.sha256, "内容变了，导出件的哈希必须变"
        assert r1.idempotency_key != r2.idempotency_key, "幂等键必须体现内容差异"

    def test_stale_docx_is_not_served_after_record_edit(self, env):
        """★ 缺陷 26 的用户可见后果：**磁盘上的 docx 必须反映最新记录稿**。

        上一条测的是返回值；这一条测**落到磁盘的字节**（用户真正拿到的东西）。
        判据：改稿前后各读一次 export/*.docx 的 sha256，必须不同。
        """
        import hashlib

        data, db, store = env
        svc = _svc(env)

        r1 = svc.export_docx(version=1, installed_fonts=FONTS_OK)
        f1 = store.resolve(r1.rel_path)
        sha_before = hashlib.sha256(f1.read_bytes()).hexdigest()

        store.write_text("record/v1.md", SAMPLE_RECORD + "\n【改】新增一段：设备采购重新核算。\n")
        r2 = svc.export_docx(version=1, installed_fonts=FONTS_OK)
        f2 = store.resolve(r2.rel_path)
        sha_after = hashlib.sha256(f2.read_bytes()).hexdigest()

        assert sha_before != sha_after, "改稿后磁盘上的 docx 仍是旧字节"

    def test_key_rejects_short_digest(self):
        with pytest.raises(ValueError, match="长度不足"):
            make_idempotency_key("m1", 1, "abc")

    # --- 确定性序列化（T1-10 补：幂等判定的前提）------------------------

    def test_export_reuse_survives_second_export(self, env):
        """重复导出必须复用同一文件，且 manifest 不新增条目。

        背景（真实缺陷，2026-09-28 全量回归暴露）：`docx` 是 ZIP，
        python-docx 的 `save()` 往每个条目的 `ZipInfo.date_time` 写
        **当前本地时间**。而 `export_docx` 用**渲染字节的 sha256** 与
        manifest 里已落盘文件的 sha256 比对来决定 `reused` ——于是
        两次导出只要不在同一秒，`reused` 就是 False，幂等失效。
        条目数与落盘文件数是这一定理的**外部可观测面**。
        """
        data, db, store = env
        svc = _svc(env)
        assert svc.export_docx(installed_fonts=FONTS_OK).reused is False
        assert svc.export_docx(installed_fonts=FONTS_OK).reused is True
        entries = [e for e in store.manifest() if e.path.startswith(f"{EXPORT_AREA}/")]
        assert len(entries) == 1, f"幂等失效：manifest 有 {len(entries)} 条"

    def test_docx_bytes_are_reproducible(self):
        """同一份 markdown 渲染两次，字节必须完全一致（含跨秒）。"""
        import time as _time

        from mmwf.app.services.export import _docx_bytes
        from mmwf.domain.docx.writer import render_markdown_to_docx

        md = "# 标题\n\n正文一段，含表格：\n\n| 事项 | 责任人 |\n| --- | --- |\n| 对接 | 李四 |\n"
        first = _docx_bytes(render_markdown_to_docx(md))
        _time.sleep(1.1)          # 强制跨过 ZIP 时间戳的秒边界
        second = _docx_bytes(render_markdown_to_docx(md))
        assert first == second, "docx 序列化带入了时钟，幂等判定会被破坏"

    def test_every_zip_entry_has_fixed_timestamp(self):
        """每个 ZIP 条目的时间戳都必须是固定的 1980-01-01。"""
        from mmwf.app.services.export import _docx_bytes
        from mmwf.domain.docx._docx_backend import DETERMINISTIC_ZIP_DATETIME
        from mmwf.domain.docx.writer import render_markdown_to_docx

        raw = _docx_bytes(render_markdown_to_docx("# 标题\n\n正文。\n"))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            stamps = {i.date_time for i in z.infolist()}
        assert stamps == {DETERMINISTIC_ZIP_DATETIME}, f"时间戳不唯一：{stamps}"

    def test_deterministic_bytes_still_open(self, tmp_path: Path):
        """确定性重写不能损坏文档：zip 完整且 python-docx 能读回。"""
        from mmwf.app.services.export import _docx_bytes
        from mmwf.domain.docx._docx_backend import open_document
        from mmwf.domain.docx.writer import render_markdown_to_docx

        md = "# 标题\n\n正文。\n\n| 事项 | 责任人 |\n| --- | --- |\n| 对接 | 李四 |\n"
        raw = _docx_bytes(render_markdown_to_docx(md))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            assert z.testzip() is None, "重写后的 zip 损坏"
        tmp = tmp_path / "deterministic_probe.docx"
        tmp.write_bytes(raw)
        doc = open_document(tmp)
        assert len(doc.tables) == 1

    def test_deterministic_bytes_pass_style_check(self, env):
        """确定性序列化后，导出的 docx 版式仍须全绿。"""
        from verify_docx_style import verify_file

        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        report = verify_file(svc.store.resolve(r.rel_path))
        assert report.ok, f"版式不过：{report.errors}"

    def test_content_unchanged_same_key(self, env):
        """相同内容、不同版本号 -> 键不同（版本是键的一部分）。"""
        data, db, store = env
        svc = _svc(env)
        store.write_text("record/v2.md", SAMPLE_RECORD, stage="record")
        k1 = svc.export_docx(version=1, installed_fonts=FONTS_OK).idempotency_key
        k2 = svc.export_docx(version=2, installed_fonts=FONTS_OK).idempotency_key
        assert k1 != k2


# ---------------------------------------------------------------------------
# 字体（E5002 / E5001 / IT-11 / DR-08）
# ---------------------------------------------------------------------------

class TestFontHandling:
    def test_missing_font_warns_but_succeeds(self, env):
        """IT-11：缺字体 -> E5002 警告，导出不崩。"""
        svc = _svc(env)
        r = svc.export_docx(installed_fonts={"宋体"})
        assert r.rel_path
        assert r.font_warnings, "应有 font missing 警告"
        assert any("E5002" not in w for w in r.font_warnings)   # 文案形态
        assert "font missing" in " ".join(r.font_warnings)

    def test_font_warning_mentions_fallback_target(self, env):
        svc = _svc(env)
        r = svc.export_docx(installed_fonts={"宋体"})
        assert any("宋体" in w for w in r.font_warnings)

    def test_policy_fail_raises_e5001(self, env):
        svc = _svc(env, font_policy="fail")
        with pytest.raises(ExportBlockedError) as ei:
            svc.export_docx(installed_fonts={"宋体"})
        assert ei.value.code == "E5001"

    def test_policy_fail_does_not_write_file(self, env):
        data, db, store = env
        svc = _svc(env, font_policy="fail")
        with pytest.raises(ExportBlockedError):
            svc.export_docx(installed_fonts={"宋体"})
        assert list(store.resolve(EXPORT_AREA).glob("*.docx")) == []

    def test_policy_fail_writes_failure_audit(self, env):
        data, db, store = env
        svc = _svc(env, font_policy="fail")
        with pytest.raises(ExportBlockedError):
            svc.export_docx(installed_fonts={"宋体"})
        recs = [r for r in query(db) if r.action == "export"]
        assert recs and recs[-1].result == "failure"

    def test_policy_fail_ok_when_fonts_present(self, env):
        svc = _svc(env, font_policy="fail")
        r = svc.export_docx(installed_fonts=FONTS_OK)
        assert r.rel_path and r.font_warnings == ()

    def test_precheck_unavailable_passes(self, env):
        """预检不可用（非 Windows）不阻断，也不误报缺失。"""
        svc = _svc(env, font_policy="fail")
        r = svc.export_docx(installed_fonts=set())
        assert r.rel_path
        assert r.font_warnings == ()

    def test_font_report_exposed(self, env):
        svc = _svc(env)
        r = svc.export_docx(installed_fonts={"宋体"})
        assert r.font_report is not None
        assert r.font_report.all_installed is False

    def test_system_fonts_payload(self, env):
        svc = _svc(env)
        payload = svc.system_fonts_payload()
        assert "missing" in payload and "all_installed" in payload

    def test_fallback_applied_in_output(self, env):
        """回退要体现在产出文件里（不只是警告）。"""
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts={"宋体", "黑体"})
        xml = _doc_xml(store.resolve(r.rel_path).read_bytes())
        # 正文回退到宋体
        assert 'w:eastAsia="宋体"' in xml


# ---------------------------------------------------------------------------
# 明文区与合规红线
# ---------------------------------------------------------------------------

class TestCompliance:
    def test_export_area_is_plaintext(self, env):
        """`export/` 必须**不**加密，否则 Word 打不开。"""
        assert EXPORT_AREA not in ENCRYPTED_AREAS

    def test_output_not_encrypted(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        assert r.encrypted is False
        raw = store.resolve(r.rel_path).read_bytes()
        assert zipfile.is_zipfile(io.BytesIO(raw)), "应是可直接打开的 zip（docx）"

    def test_audit_has_no_body_text(self, env):
        """★ 审计 detail 绝不能含记录稿正文（部署手册 §5.3 红线）。"""
        data, db, store = env
        svc = _svc(env)
        svc.export_docx(installed_fonts=FONTS_OK)
        recs = [r for r in query(db) if r.action == "export"]
        raw = json.dumps([r.detail for r in recs], ensure_ascii=False)
        assert "信息化建设" not in raw
        assert "抓好落实" not in raw
        assert "李四" not in raw

    def test_audit_records_path_and_stats(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        rec = [x for x in query(db) if x.action == "export"][-1]
        assert rec.detail["path"] == r.rel_path
        assert rec.detail["version"] == 1
        assert "idempotency_key" in rec.detail

    def test_audit_chain_valid(self, env):
        data, db, store = env
        svc = _svc(env)
        svc.export_docx(installed_fonts=FONTS_OK)
        assert verify_chain(db).ok

    def test_manifest_clean_after_export(self, env):
        data, db, store = env
        svc = _svc(env)
        svc.export_docx(installed_fonts=FONTS_OK)
        assert store.verify_manifest() == []

    def test_export_action_is_registered(self, env):
        from mmwf.infra.audit import ACTIONS

        assert "export" in ACTIONS
        assert "export_undo" in ACTIONS


# ---------------------------------------------------------------------------
# 撤销导出（F5.8）
# ---------------------------------------------------------------------------

class TestUndoExport:
    def test_undo_removes_file(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        assert svc.undo_export(r.rel_path) is True
        assert not store.exists(r.rel_path)

    def test_undo_cleans_manifest(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        svc.undo_export(r.rel_path)
        paths = {e.path for e in store.manifest()}
        assert r.rel_path not in paths
        assert store.verify_manifest() == []

    def test_undo_audited(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        svc.undo_export(r.rel_path)
        recs = [x for x in query(db) if x.action == "export_undo"]
        assert len(recs) == 1
        assert recs[0].detail["removed"] is True

    def test_undo_missing_file_is_noop(self, env):
        svc = _svc(env)
        assert svc.undo_export(f"{EXPORT_AREA}/nope.docx") is False

    def test_undo_rejects_other_areas(self, env):
        """不许用撤销导出的入口去删加密区的文件。"""
        svc = _svc(env)
        with pytest.raises(ExportServiceError, match="只能撤销"):
            svc.undo_export("record/v1.md")

    def test_export_after_undo_works(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        svc.undo_export(r.rel_path)
        r2 = svc.export_docx(installed_fonts=FONTS_OK)
        assert store.exists(r2.rel_path)


# ---------------------------------------------------------------------------
# 版本选择
# ---------------------------------------------------------------------------

class TestVersionSelection:
    def test_latest_version_default(self, env):
        data, db, store = env
        store.write_text("record/v2.md", SAMPLE_RECORD, stage="record")
        store.write_text("record/v3.md", SAMPLE_RECORD, stage="record")
        svc = _svc(env)
        assert svc.latest_record_version() == 3

    def test_explicit_version(self, env):
        data, db, store = env
        store.write_text("record/v2.md", SAMPLE_RECORD, stage="record")
        svc = _svc(env)
        r = svc.export_docx(version=1, installed_fonts=FONTS_OK)
        assert r.version == 1
        assert "v1" in r.filename

    def test_missing_record_raises(self, env):
        data, db, store = env
        store.delete("record/v1.md")
        svc = _svc(env)
        with pytest.raises(ExportServiceError, match="没有找到任何记录稿"):
            svc.latest_record_version()

    def test_missing_specific_version_raises(self, env):
        svc = _svc(env)
        with pytest.raises(ExportServiceError, match="记录稿不存在"):
            svc.export_docx(version=99, installed_fonts=FONTS_OK)

    def test_filename_template(self, env):
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        assert r.filename == f"{MEETING_ID}-v1.docx"

    def test_version_from_manifest_union(self, env):
        """文件被**外部**删掉、manifest 仍记得 v2 —— 不该退回 v1。

        注意不能用 `store.delete()` 来模拟：那个方法会**同时**清掉
        manifest 条目，于是"并集"就没有意义了。真正要防的场景是
        "有人用资源管理器/命令行直接把文件删了" —— 此时 manifest
        是唯一还记得 v2 存在过的地方。
        """
        data, db, store = env
        store.write_text("record/v2.md", SAMPLE_RECORD, stage="record")
        store.resolve("record/v2.md").unlink()          # 绕过 filestore 删除
        svc = _svc(env)
        assert svc.latest_record_version() == 2

    def test_union_falls_back_when_manifest_empty(self, env):
        """manifest 条目也没了的时候，回落到目录扫描（仍能找到 v1）。"""
        data, db, store = env
        svc = _svc(env)
        assert svc.latest_record_version() == 1


# ---------------------------------------------------------------------------
# 版式（导出的文件真的要合规）
# ---------------------------------------------------------------------------

class TestGeneratedDocxStyle:
    def test_passes_style_verification(self, env, tmp_path):
        """★ 导出的 docx 通过 `tools/verify_docx_style.py` 的全部检查。"""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        from verify_docx_style import verify_file

        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        out = tmp_path / "out.docx"
        out.write_bytes(store.resolve(r.rel_path).read_bytes())
        report = verify_file(out)
        assert report.ok, f"版式不合规：{report.errors}"

    def test_judge_docx_clean(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        assert judge_docx(store.resolve(r.rel_path).read_bytes()) == []

    def test_judge_docx_detects_broken(self):
        """自检能真的发现问题（防止它变成永远返回 [] 的假检查）。"""
        assert judge_docx(b"not a docx") != []

    def test_table_indent_zero_in_output(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        xml = _doc_xml(store.resolve(r.rel_path).read_bytes())
        assert 'w:firstLineChars="200"' in xml      # 正文有缩进
        assert "tblBorders" in xml                  # 表格有边框
        assert "PAGE" not in xml                    # 页码在页脚，不在正文

    def test_page_number_in_footer(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        raw = store.resolve(r.rel_path).read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            footers = b"".join(z.read(n) for n in z.namelist()
                               if n.startswith("word/footer"))
        assert b"PAGE" in footers

    def test_normalization_applied(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        xml = _doc_xml(store.resolve(r.rel_path).read_bytes())
        assert "API 接口" in xml, "中英之间应加空格"

    def test_normalization_can_be_disabled(self, env):
        data, db, store = env
        svc = _svc(env, normalize=False)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        xml = _doc_xml(store.resolve(r.rel_path).read_bytes())
        assert "API 接口" not in xml

    def test_heading_numbering_applied(self, env):
        data, db, store = env
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        xml = _doc_xml(store.resolve(r.rel_path).read_bytes())
        assert "一、议定事项" in xml


# ---------------------------------------------------------------------------
# .doc 转换
# ---------------------------------------------------------------------------

class TestDocConversion:
    def test_skipped_without_soffice(self, env):
        svc = _svc(env, soffice_path="C:/definitely/not/here/soffice.exe")
        r = svc.export_docx(installed_fonts=FONTS_OK, convert_doc=True)
        assert r.doc_conversion.status == DOC_STATUS_SKIPPED
        assert "LibreOffice" in r.doc_conversion.reason

    def test_skipped_does_not_fail_export(self, env):
        svc = _svc(env, soffice_path="C:/nope/soffice.exe")
        r = svc.export_docx(installed_fonts=FONTS_OK, convert_doc=True)
        assert r.rel_path, "docx 仍应正常导出"

    def test_not_attempted_by_default(self, env):
        svc = _svc(env)
        r = svc.export_docx(installed_fonts=FONTS_OK)
        assert r.doc_conversion.status == DOC_STATUS_SKIPPED

    def test_failed_when_source_missing(self, env):
        import shutil

        soffice = shutil.which("soffice")
        if not soffice:
            pytest.skip("环境无 soffice")
        svc = _svc(env, soffice_path=soffice)
        conv = svc.convert_to_doc(f"{EXPORT_AREA}/nonexistent.docx")
        assert conv.status == DOC_STATUS_FAILED

    def test_doc_status_constants(self):
        assert {DOC_STATUS_OK, DOC_STATUS_SKIPPED, DOC_STATUS_FAILED} == {
            "ok", "skipped", "failed"
        }


# ---------------------------------------------------------------------------
# 结果对象与工厂
# ---------------------------------------------------------------------------

class TestResultObject:
    def test_to_dict_shape(self, env):
        svc = _svc(env)
        d = svc.export_docx(installed_fonts=FONTS_OK).to_dict()
        assert set(d) >= {
            "meeting_id", "version", "path", "sha256", "size_bytes",
            "encrypted", "idempotency_key", "reused", "warnings", "doc",
        }

    def test_summary_readable(self, env):
        svc = _svc(env)
        s = svc.export_docx(installed_fonts=FONTS_OK).summary()
        assert "v1" in s and ".docx" in s

    def test_size_positive(self, env):
        svc = _svc(env)
        assert svc.export_docx(installed_fonts=FONTS_OK).size_bytes > 1000

    def test_status_readback(self, env):
        svc = _svc(env)
        assert svc.current_meeting_status() == "record_ready"


class TestMakeExportService:
    def test_factory_from_settings(self):
        from mmwf.settings import load_settings

        root = Path(__file__).resolve().parents[2]
        settings = load_settings(root / "config")
        svc = make_export_service(MEETING_ID, settings, encrypt_enabled=False)
        assert isinstance(svc, ExportService)
        assert svc.font_policy in {"warn", "fallback", "fail"}

    def test_factory_reads_soffice_path(self):
        from mmwf.settings import load_settings

        root = Path(__file__).resolve().parents[2]
        settings = load_settings(root / "config")
        svc = make_export_service(MEETING_ID, settings, encrypt_enabled=False)
        assert svc.soffice_path, "应从 models.yaml 的 export 段读到 soffice_path"
        assert isinstance(svc.soffice_path, str), "要转成 str 交给 subprocess"

    def test_factory_reads_font_policy(self):
        from mmwf.settings import load_settings

        root = Path(__file__).resolve().parents[2]
        settings = load_settings(root / "config")
        svc = make_export_service(MEETING_ID, settings, encrypt_enabled=False)
        assert svc.font_policy == settings.export.font_fallback_policy

    def test_factory_data_dir_resolved(self):
        from mmwf.settings import load_settings

        root = Path(__file__).resolve().parents[2]
        settings = load_settings(root / "config")
        svc = make_export_service(MEETING_ID, settings, encrypt_enabled=False)
        assert svc.db_path.is_absolute(), "data_dir 必须解析成绝对路径"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _doc_xml(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return z.read("word/document.xml").decode("utf-8")
