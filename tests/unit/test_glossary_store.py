"""T1-07 单元测试：术语库仓储 + 应用层（含审计落痕）。

对标施工卡四条验收标准的"库"侧：

    验收1  config/glossary.csv 可被导入，格式 category,canonical,aliases(|分隔),note
    验收4  命中数写入审计日志的 detail（可追溯术语库生效情况）

以及测试方案里跟"持久化/留痕"相关的隐性要求：

    GL-S01  CSV 往返无损（导入再导出，字段一一对应）
    GL-S02  upsert 冲突键是 (category, canonical)，同名不同类别可共存
    GL-S03  导入只做 upsert，**不删**库里已有的条目
    GL-S04  每次写操作都留一条 glossary_change（含 op / hits）
    GL-S05  correct_text 只在真的改了东西时才写审计
    GL-S06  detail 不得含正文原文（部署手册 §6.2 的合规红线）
    GL-S07  未注入 AuditLogger 时打 warning，不静默吞掉
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from mmwf.app.services.glossary import (
    GlossaryService,
    make_glossary_service,
)
from mmwf.domain.glossary.matcher import (
    CSV_HEADER,
    GlossaryEntry,
    GlossaryFormatError,
    parse_csv_rows,
)
from mmwf.infra.audit import (
    FORBIDDEN_DETAIL_KEYS,
    AuditLogger,
    check_detail_safety,
    query,
    verify_chain,
)
from mmwf.infra.glossary_store import (
    GlossaryRepository,
    ImportReport,
    import_csv,
    iter_chunks,
    read_csv,
    write_csv,
)
from mmwf.infra.migrations.runner import migrate

OPERATOR = "zhangsan"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "data" / "mmwf.db"
    migrate(p)
    return p


@pytest.fixture()
def repo(db_path: Path) -> GlossaryRepository:
    return GlossaryRepository(db_path)


@pytest.fixture()
def audit(db_path: Path) -> AuditLogger:
    return AuditLogger(db_path)


@pytest.fixture()
def svc(repo: GlossaryRepository, audit: AuditLogger) -> GlossaryService:
    return GlossaryService(repo, audit=audit, operator=OPERATOR)


def _e(entry_id: str, category: str, canonical: str,
       aliases: tuple[str, ...] = (), note: str = "",
       enabled: bool = True) -> GlossaryEntry:
    return GlossaryEntry(
        entry_id=entry_id, category=category, canonical=canonical,
        aliases=aliases, note=note, enabled=enabled,
    )


SEED = [
    _e("gl-001", "org", "中共中央办公厅", ("中办", "中央办公厅"), "党委口"),
    _e("gl-002", "org", "国家发展和改革委员会", ("发改委", "国家发改委"), ""),
    _e("gl-003", "person", "张建国", ("建国同志", "老张"), "分管"),
    _e("gl-004", "term", "新型基础设施建设", ("新基建",), ""),
    _e("gl-005", "codename", "蓝鲸计划", ("蓝鲸", "蓝鲸工程"), ""),
]


# ---------------------------------------------------------------------------
# CSV 读取（read_csv）
# ---------------------------------------------------------------------------

class TestReadCsv:
    def test_basic_roundtrip(self, tmp_path: Path):
        p = tmp_path / "g.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办|中央办公厅,党委口\n"
            "person,张建国,建国同志,分管\n",
            encoding="utf-8",
        )
        entries, problems = read_csv(p)
        assert problems == []
        assert len(entries) == 2
        assert entries[0].canonical == "中共中央办公厅"
        assert entries[0].aliases == ("中办", "中央办公厅")
        assert entries[0].note == "党委口"
        assert entries[0].category == "org"
        # entry_id 由导入侧生成，必须非空且互不相同
        assert all(e.entry_id for e in entries)
        assert entries[0].entry_id != entries[1].entry_id

    def test_bom_is_stripped(self, tmp_path: Path):
        """Excel 存 UTF-8 CSV 会加 BOM，不剥掉的话首列名变成 \\ufeffcategory。

        后果不是"报个错"那么轻：所有行都会因为"缺 category"被拒，
        用户看到一份格式完全正确的文件被全量打回，无从下手。
        """
        p = tmp_path / "bom.csv"
        p.write_bytes(
            "category,canonical,aliases,note\norg,中共中央办公厅,中办,\n"
            .encode("utf-8-sig")
        )
        entries, problems = read_csv(p)
        assert problems == []
        assert len(entries) == 1
        assert entries[0].category == "org"

    def test_crlf_and_blank_lines_tolerated(self, tmp_path: Path):
        """Windows 记事本/Excel 存出来的是 CRLF，中间常有空行。

        这条以前会在 `csv` 层炸——空行会解析成 `{None: []}` 甚至
        让后续行错位。现在行级剥离先摘掉空行，数据行才交给 csv。
        """
        p = tmp_path / "crlf.csv"
        p.write_bytes(
            (
                "category,canonical,aliases,note\r\n"
                "org,中共中央办公厅,中办,\r\n"
                "\r\n"
                "term,新型基础设施建设,新基建项目,\r\n"
            ).encode("utf-8")
        )
        entries, problems = read_csv(p)
        assert problems == []
        assert [e.canonical for e in entries] == ["中共中央办公厅", "新型基础设施建设"]

    def test_hash_comment_lines_skipped(self, tmp_path: Path):
        p = tmp_path / "c.csv"
        p.write_text(
            "# 这是说明行，不该被当成数据\n"
            "category,canonical,aliases,note\n"
            "# 再插一条注释\n"
            "org,中共中央办公厅,中办,\n",
            encoding="utf-8",
        )
        entries, problems = read_csv(p)
        assert problems == []
        assert len(entries) == 1

    def test_optional_columns_may_be_absent(self, tmp_path: Path):
        p = tmp_path / "min.csv"
        p.write_text(
            "category,canonical\norg,中共中央办公厅\n",
            encoding="utf-8",
        )
        entries, problems = read_csv(p)
        assert problems == []
        assert entries[0].aliases == ()
        assert entries[0].note == ""

    def test_missing_required_column_reports_problem(self, tmp_path: Path):
        p = tmp_path / "bad.csv"
        p.write_text("canonical,aliases\n中共中央办公厅,中办\n", encoding="utf-8")
        with pytest.raises(GlossaryFormatError, match="category"):
            read_csv(p)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            read_csv(tmp_path / "nope.csv")

    def test_bad_category_collects_problem_not_raise(self, tmp_path: Path):
        """一行类别写错不该让整份文件失败——用户要看到全部问题再一起改。"""
        p = tmp_path / "cat.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办,\n"
            "ORG,国家发展和改革委员会,发改委,\n"
            "person,张建国,建国同志,\n",
            encoding="utf-8",
        )
        entries, problems = read_csv(p)
        assert len(entries) == 2
        assert len(problems) == 1
        assert "ORG" in problems[0]

    def test_empty_canonical_collects_problem(self, tmp_path: Path):
        p = tmp_path / "ec.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办,\n"
            "org,,空规范写法,\n",
            encoding="utf-8",
        )
        entries, problems = read_csv(p)
        assert len(entries) == 1
        assert len(problems) == 1

    def test_pipe_aliases_and_json_aliases_both_accepted(self, tmp_path: Path):
        """CSV 列里可能存 `|` 分隔串，也可能存 DB 导出的 JSON 数组。"""
        p = tmp_path / "mix.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            'org,中共中央办公厅,"中办|中央办公厅",\n'
            'term,新型基础设施建设,"[""新基建""]",\n',
            encoding="utf-8",
        )
        entries, problems = read_csv(p)
        assert problems == []
        assert entries[0].aliases == ("中办", "中央办公厅")


# ---------------------------------------------------------------------------
# CSV 写出（write_csv）
# ---------------------------------------------------------------------------

class TestWriteCsv:
    def test_header_is_the_card_format(self, tmp_path: Path):
        """表头前四列必须与施工卡规定的格式一致（顺序不能变）。

        写出时用 `utf-8-sig`，所以字节流以 BOM 开头——这是**故意的**，
        为的是在中文 Windows 上双击用 Excel 打开不乱码。
        断表头前先剥 BOM。
        """
        p = write_csv(tmp_path / "out.csv", SEED)
        raw = p.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "导出 CSV 必须带 BOM，否则 Excel 打开乱码"
        first = raw.decode("utf-8-sig").splitlines()[0]
        assert first.split(",")[:4] == list(CSV_HEADER[:4])
        assert CSV_HEADER[:4] == ("category", "canonical", "aliases", "note")

    def test_roundtrip_is_lossless(self, tmp_path: Path):
        """导出再导入，条目必须一一对应（含 entry_id 与 enabled）。"""
        out = write_csv(tmp_path / "rt.csv", SEED)
        back, problems = read_csv(out)
        assert problems == []
        assert len(back) == len(SEED)
        for src, got in zip(sorted(SEED, key=lambda e: e.entry_id),
                            sorted(back, key=lambda e: e.entry_id)):
            assert got.entry_id == src.entry_id
            assert got.category == src.category
            assert got.canonical == src.canonical
            assert got.aliases == src.aliases
            assert got.note == src.note
            assert got.enabled == src.enabled

    def test_aliases_joined_by_pipe(self, tmp_path: Path):
        p = write_csv(tmp_path / "pipe.csv", [SEED[0]])
        assert "中办|中央办公厅" in p.read_text(encoding="utf-8")

    def test_comma_in_field_is_quoted(self, tmp_path: Path):
        """备注里带逗号很常见，不转义会把一行拆成两列。"""
        p = write_csv(
            tmp_path / "comma.csv",
            [_e("gl-x", "term", "某某术语", ("别名甲",), "备注,含逗号")],
        )
        back, problems = read_csv(p)
        assert problems == []
        assert back[0].note == "备注,含逗号"

    def test_utf8_bom_produced_for_excel(self, tmp_path: Path):
        p = write_csv(tmp_path / "bom_out.csv", [SEED[0]])
        raw = p.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") or not raw.startswith(b"\xef")


# ---------------------------------------------------------------------------
# 仓储（GlossaryRepository）
# ---------------------------------------------------------------------------

class TestRepository:
    def test_upsert_reports_created_then_updated(self, repo: GlossaryRepository):
        assert repo.upsert(SEED[0]) == "created"
        assert repo.upsert(SEED[0]) == "updated"
        assert repo.count() == 1

    def test_conflict_key_is_category_plus_canonical(self, repo: GlossaryRepository):
        """同名但不同类别必须能共存。

        现实里真的有这种数据：某人是人名，同时也是项目代号。
        用 entry_id 做冲突键的话，第二次插入会造出两条同 (category, canonical)
        的记录，撞上 UNIQUE 约束直接崩——或者更糟，被静默吞掉一条。
        """
        repo.upsert(_e("gl-a", "person", "蓝鲸", ("老蓝",)))
        repo.upsert(_e("gl-b", "codename", "蓝鲸", ("蓝鲸工程",)))
        assert repo.count() == 2

    def test_same_category_same_canonical_updates_in_place(self, repo: GlossaryRepository):
        repo.upsert(_e("gl-a", "org", "中共中央办公厅", ("中办",)))
        repo.upsert(_e("gl-b", "org", "中共中央办公厅", ("中办", "中央办公厅")))
        assert repo.count() == 1
        got = repo.list_all()
        assert got[0].aliases == ("中办", "中央办公厅")

    def test_aliases_survive_json_roundtrip(self, repo: GlossaryRepository):
        repo.upsert(SEED[0])
        got = repo.get("gl-001")
        assert got is not None
        assert got.aliases == ("中办", "中央办公厅")

    def test_empty_aliases_roundtrip(self, repo: GlossaryRepository):
        repo.upsert(_e("gl-e", "term", "无别名术语"))
        got = repo.get("gl-e")
        assert got is not None
        assert got.aliases == ()

    def test_list_all_enabled_only_filters(self, repo: GlossaryRepository):
        for e in SEED:
            repo.upsert(e)
        repo.set_enabled("gl-003", False)
        assert len(repo.list_all()) == 5
        assert len(repo.list_all(enabled_only=True)) == 4

    def test_get_unknown_returns_none(self, repo: GlossaryRepository):
        assert repo.get("nope") is None

    def test_set_enabled_returns_false_for_unknown(self, repo: GlossaryRepository):
        assert repo.set_enabled("nope", False) is False

    def test_delete_returns_false_for_unknown(self, repo: GlossaryRepository):
        assert repo.delete("nope") is False

    def test_delete_removes_row(self, repo: GlossaryRepository):
        repo.upsert(SEED[0])
        assert repo.delete("gl-001") is True
        assert repo.get("gl-001") is None

    def test_rows_are_ordered_deterministically(self, repo: GlossaryRepository):
        for e in reversed(SEED):
            repo.upsert(e)
        a = [e.entry_id for e in repo.list_all()]
        b = [e.entry_id for e in repo.list_all()]
        assert a == b

    def test_updated_at_is_stamped(self, repo: GlossaryRepository, db_path: Path):
        repo.upsert(SEED[0])
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT updated_at FROM glossary_entry WHERE entry_id='gl-001'"
            ).fetchone()
        assert row is not None and row[0]

    def test_invalid_category_rejected_by_db_check(self, repo: GlossaryRepository):
        """领域层校验之外，DB 的 CHECK 约束是第二道闸——写库路径可能不止一条。"""
        with pytest.raises((sqlite3.IntegrityError, GlossaryFormatError, ValueError)):
            repo.upsert(_e("gl-z", "not_a_category", "某某"))


# ---------------------------------------------------------------------------
# 批量导入（replace_all / import_csv）
# ---------------------------------------------------------------------------

class TestReplaceAll:
    def test_report_counts(self, repo: GlossaryRepository):
        repo.upsert(SEED[0])
        rep = repo.replace_all([SEED[0], SEED[1], SEED[2]])
        assert isinstance(rep, ImportReport)
        assert rep.created == 2
        assert rep.updated == 1
        assert rep.upserted == 3
        assert rep.ok is True

    def test_import_does_not_delete_existing(self, repo: GlossaryRepository):
        """导入是 upsert，不是同步。

        这条是**故意的**：如果导入按"以文件为准"删掉库里多余条目，
        用户拿一份只含 3 条的补丁文件去导入，就会静默清空术语库。
        删除必须是显式动作，并且留审计。
        """
        for e in SEED:
            repo.upsert(e)
        repo.replace_all([_e("gl-new", "term", "新增术语")])
        assert repo.count() == 6
        assert repo.get("gl-001") is not None

    def test_problems_do_not_abort_import(self, repo: GlossaryRepository, monkeypatch):
        """一条坏数据不该让整批导入失败——已经处理好的 2 条要能留下。

        触发点：`GlossaryEntry.__post_init__` 会在**构造时**拒掉非法类别，
        所以坏数据到不了 `replace_all`。这里用一条 DB 层才会拒的记录
        （category 违反 CHECK 约束）来模拟"导入到一半遇到脏数据"。
        """
        entries = [SEED[0], SEED[1]]
        rep_clean = repo.replace_all(entries)
        assert rep_clean.upserted == 2
        assert rep_clean.ok is True

        # 绕过领域层校验，直接造一条 DB 层会拒绝的条目
        bad = _e("gl-bad", "org", "某某机构", ())
        object.__setattr__(bad, "category", "not_a_category")
        rep2 = repo.replace_all([SEED[2], bad, SEED[3]])
        assert rep2.upserted == 2          # 好的两条照样入库
        assert len(rep2.problems) == 1     # 坏的那条被记下来
        assert rep2.ok is False
        assert repo.get("gl-003") is not None
        assert repo.get("gl-004") is not None
        assert repo.get("gl-bad") is None

    def test_empty_input_is_noop(self, repo: GlossaryRepository):
        rep = repo.replace_all([])
        assert rep.upserted == 0
        assert not rep.problems  # problems 是 tuple，空即无问题

    def test_duplicate_canonical_in_one_batch_is_last_wins(self, repo: GlossaryRepository):
        """同一批里 (category, canonical) 重复不算错误，按后写的生效。"""
        rep = repo.replace_all([
            _e("gl-d1", "term", "某某术语", ("甲",)),
            _e("gl-d2", "term", "某某术语", ("甲", "乙")),
        ])
        assert rep.created + rep.updated == 2
        assert repo.count() == 1


class TestImportCsv:
    def test_end_to_end(self, tmp_path: Path, repo: GlossaryRepository):
        p = tmp_path / "g.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办|中央办公厅,党委口\n"
            "org,国家发展和改革委员会,发改委,\n"
            "term,新型基础设施建设,新基建,\n",
            encoding="utf-8",
        )
        rep = import_csv(repo, p)
        assert rep.ok is True
        assert rep.upserted == 3
        assert repo.count() == 3
        assert rep.source.endswith("g.csv")

    def test_problems_reported_not_raised(self, tmp_path: Path, repo: GlossaryRepository):
        p = tmp_path / "partial.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办,\n"
            "BAD,某某,,\n",
            encoding="utf-8",
        )
        rep = import_csv(repo, p)
        assert rep.upserted == 1
        assert len(rep.problems) == 1
        assert repo.count() == 1

    def test_reimport_is_idempotent(self, tmp_path: Path, repo: GlossaryRepository):
        p = tmp_path / "g.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办,\n",
            encoding="utf-8",
        )
        r1 = import_csv(repo, p)
        r2 = import_csv(repo, p)
        assert r1.created == 1 and r1.updated == 0
        assert r2.created == 0 and r2.updated == 1
        assert repo.count() == 1

    def test_missing_file_raises(self, tmp_path: Path, repo: GlossaryRepository):
        with pytest.raises(FileNotFoundError):
            import_csv(repo, tmp_path / "nope.csv")


def test_iter_chunks_covers_everything_without_overlap():
    assert list(iter_chunks(["a", "b", "c", "d", "e"], 2)) == [
        ["a", "b"], ["c", "d"], ["e"]
    ]
    assert list(iter_chunks([], 3)) == []
    assert list(iter_chunks(["a"], 10)) == [["a"]]


# ---------------------------------------------------------------------------
# 应用层（GlossaryService）— 验收 4：命中数进审计 detail
# ---------------------------------------------------------------------------

class TestServiceCorrectText:
    def test_returns_corrected_text_and_hits(self, svc: GlossaryService):
        for e in SEED:
            svc.upsert(e)
        r = svc.correct_text("中办印发了关于新基建的通知")
        assert r.changed is True
        assert "中共中央办公厅" in r.text
        assert "新型基础设施建设" in r.text
        assert r.hits == 2

    def test_no_change_no_audit(self, svc: GlossaryService, db_path: Path):
        """没改东西就不写审计——每段写一条"改了 0 处"会把链刷爆。"""
        for e in SEED:
            svc.upsert(e)
        before = len(query(db_path))
        r = svc.correct_text("这是一个完全正常的句子，不含任何别名。")
        assert r.hits == 0
        assert r.changed is False
        assert len(query(db_path)) == before

    def test_hit_count_lands_in_audit_detail(self, svc: GlossaryService, db_path: Path):
        for e in SEED:
            svc.upsert(e)
        svc.correct_text("中办印发了关于新基建的通知")
        rows = [r for r in query(db_path) if r.action == "glossary_change"]
        assert rows, "correct_text 命中后必须留一条 glossary_change"
        d = rows[-1].detail
        assert d["op"] == "correct"
        assert d["hits"] == 2
        assert d["entries"] == 2

    def test_audit_detail_is_forensically_useful(self, svc: GlossaryService, db_path: Path):
        """detail 要能回答三个问题：谁、改了几处、涉及哪些别名。"""
        for e in SEED:
            svc.upsert(e)
        svc.correct_text("中办印发了关于新基建的通知")
        d = query(db_path)[-1].detail
        assert {"op", "hits", "aliases", "entries", "chars_before", "chars_after"} <= set(d)
        assert "中办" in d["aliases"] or "新基建" in d["aliases"]
        assert d["chars_after"] > d["chars_before"]  # 规范写法更长

    def test_audit_detail_has_no_verbatim_transcript(self, svc: GlossaryService, db_path: Path):
        """部署手册 §6.2 红线：审计 detail 不得含正文原文。

        这里故意喂一段"有辨识度的整句"，再检查审计里出现的是
        命中计数与**截断过的**别名，而不是整段话。
        """
        for e in SEED:
            svc.upsert(e)
        sentence = "中办印发了关于新基建的通知并要求各单位认真贯彻执行到位"
        svc.correct_text(sentence)
        d = query(db_path)[-1].detail
        blob = json.dumps(d, ensure_ascii=False)
        assert "贯彻执行到位" not in blob
        assert "要求各单位" not in blob
        check_detail_safety(d)  # 抛错即失败
        assert not (FORBIDDEN_DETAIL_KEYS & set(d))

    def test_long_alias_list_in_detail_is_clipped(self, svc: GlossaryService, db_path: Path):
        """别名表很长时，detail 里的 aliases 字段必须被截断，不能无限膨胀。"""
        ents = [
            _e(f"gl-{i:03d}", "term", f"术语{i}", (f"别名{i}",))
            for i in range(40)
        ]
        for e in ents:
            svc.upsert(e)
        svc.correct_text("".join(f"别名{i}" for i in range(40)))
        d = query(db_path)[-1].detail
        assert len(d["aliases"]) <= 60

    def test_target_binding_recorded(self, svc: GlossaryService, db_path: Path):
        for e in SEED:
            svc.upsert(e)
        svc.correct_text("中办发的文件", target_type="meeting", target_id="M-001")
        last = [r for r in query(db_path) if r.action == "glossary_change"][-1]
        assert last.target_type == "meeting"
        assert last.target_id == "M-001"

    def test_write_audit_can_be_disabled_for_preview(self, svc: GlossaryService, db_path: Path):
        """预览（用户在复核页点"先看看会改成什么"）不该留痕。"""
        for e in SEED:
            svc.upsert(e)
        before = len(query(db_path))
        svc.correct_text("中办发的文件", write_audit=False)
        assert len(query(db_path)) == before

    def test_disabled_entry_is_not_applied(self, svc: GlossaryService):
        for e in SEED:
            svc.upsert(e)
        svc.set_enabled("gl-001", False)
        r = svc.correct_text("中办发了文")
        assert "中共中央办公厅" not in r.text

    def test_file_number_survives_service_path(self, svc: GlossaryService):
        for e in SEED:
            svc.upsert(e)
        r = svc.correct_text("中办印发了中办发〔2026〕1 号")
        assert "中办发〔2026〕1 号" in r.text
        assert r.text.startswith("中共中央办公厅印发了")


class TestServiceWritesAudit:
    def test_upsert_logs_created(self, svc: GlossaryService, db_path: Path):
        svc.upsert(SEED[0])
        rows = [r for r in query(db_path) if r.action == "glossary_change"]
        assert len(rows) == 1
        d = rows[0].detail
        # op 直接记仓储返回的 created/updated，而不是笼统的 "upsert"——
        # 事后翻审计要能一眼看出"这条是新加的"还是"这次是改的"
        assert d["op"] == "created"
        assert d["category"] == "org"
        assert d["canonical"] == "中共中央办公厅"
        assert d["alias_count"] == 2
        assert rows[0].target_id == "gl-001"

    def test_upsert_logs_updated_with_alias_delta(self, svc: GlossaryService, db_path: Path):
        svc.upsert(_e("gl-a", "org", "中共中央办公厅", ("中办",)))
        svc.upsert(_e("gl-a", "org", "中共中央办公厅", ("中办", "中央办公厅")))
        d = [r for r in query(db_path) if r.action == "glossary_change"][-1].detail
        assert d["op"] == "updated"
        # 改之前有几个别名 + 改之后有几个，才看得出这次动了什么
        assert d["before_aliases"] == "中办"
        assert d["alias_count"] == 2

    def test_set_enabled_logs(self, svc: GlossaryService, db_path: Path):
        svc.upsert(SEED[0])
        svc.set_enabled("gl-001", False)
        d = [r for r in query(db_path) if r.action == "glossary_change"][-1].detail
        assert d["op"] == "disable"
        assert d["canonical"] == "中共中央办公厅"

    def test_delete_logs(self, svc: GlossaryService, db_path: Path):
        svc.upsert(SEED[0])
        svc.delete("gl-001")
        d = [r for r in query(db_path) if r.action == "glossary_change"][-1].detail
        assert d["op"] == "delete"

    def test_set_enabled_unknown_does_not_log(self, svc: GlossaryService, db_path: Path):
        before = len(query(db_path))
        assert svc.set_enabled("nope", False) is False
        assert len(query(db_path)) == before

    def test_import_file_logs_one_summary_row(self, svc: GlossaryService, tmp_path: Path, db_path: Path):
        p = tmp_path / "g.csv"
        p.write_text(
            "category,canonical,aliases,note\n"
            "org,中共中央办公厅,中办,\n"
            "term,新型基础设施建设,新基建,\n",
            encoding="utf-8",
        )
        svc.import_file(p)
        rows = [r for r in query(db_path) if r.action == "glossary_change"]
        # 逐条留痕会让一次导入刷出几百行；一条汇总足够追溯
        assert len(rows) == 1
        d = rows[-1].detail
        assert d["op"] == "import"
        assert d["created"] == 2

    def test_export_file_logs(self, svc: GlossaryService, tmp_path: Path, db_path: Path):
        svc.upsert(SEED[0])
        svc.export_file(tmp_path / "out.csv")
        d = [r for r in query(db_path) if r.action == "glossary_change"][-1].detail
        assert d["op"] == "export"

    def test_chain_stays_valid_after_many_writes(self, svc: GlossaryService, db_path: Path):
        for e in SEED:
            svc.upsert(e)
        svc.correct_text("中办印发了关于新基建的通知")
        svc.set_enabled("gl-003", False)
        svc.delete("gl-005")
        v = verify_chain(db_path)
        assert v.ok is True, v.problems

    def test_operator_is_recorded(self, repo: GlossaryRepository, audit: AuditLogger, db_path: Path):
        s = GlossaryService(repo, audit=audit, operator="lisi")
        s.upsert(SEED[0])
        assert query(db_path)[-1].operator == "lisi"


class TestServiceWithoutAudit:
    def test_write_logs_warning(self, repo: GlossaryRepository, caplog):
        """未注入审计时必须打 warning。

        "术语库变更没留痕"是部署手册 §6.2 的 8 类核查项之一。
        静默跳过的话，问题要到验收现场才暴露——那时数据已经查不回来了。
        """
        s = GlossaryService(repo, audit=None, operator=OPERATOR)
        with caplog.at_level(logging.WARNING):
            s.upsert(SEED[0])
        assert any("审计" in r.message for r in caplog.records)

    def test_write_still_succeeds(self, repo: GlossaryRepository):
        s = GlossaryService(repo, audit=None)
        s.upsert(SEED[0])
        assert repo.count() == 1


class TestServiceReads:
    def test_conflicts_surface(self, svc: GlossaryService):
        svc.upsert(_e("gl-a", "person", "张建国", ("建军",)))
        svc.upsert(_e("gl-b", "person", "张建军", ("建军",)))
        c = svc.conflicts()
        assert "建军" in c
        assert set(c["建军"]) == {"张建国", "张建军"}

    def test_no_conflicts_on_clean_data(self, svc: GlossaryService):
        for e in SEED:
            svc.upsert(e)
        assert svc.conflicts() == {}

    def test_build_index_only_enabled(self, svc: GlossaryService):
        svc.upsert(SEED[0])
        svc.upsert(_e("gl-off", "term", "已停用术语", ("停用别名",), enabled=False))
        idx = svc.build_index()
        assert "中办" in idx
        assert "停用别名" not in idx

    def test_hotword_bundle_warns_on_truncation(self, repo: GlossaryRepository):
        s = GlossaryService(repo, hotword_limit=3)
        for i in range(5):
            s.upsert(_e(f"gl-{i}", "term", f"术语{i}", (f"别名{i}",)))
        b = s.prepare_hotwords()
        assert b.truncated is True
        assert len(b.words) == 3
        assert b.total == 5
        # 告警必须说清后果，不能只讲"超限"
        assert any("整体不生效" in w for w in b.warnings)

    def test_hotword_bundle_clean_when_fits(self, svc: GlossaryService):
        for e in SEED:
            svc.upsert(e)
        b = svc.prepare_hotwords()
        assert b.truncated is False
        assert b.warnings == []


class TestMakeService:
    def test_works_without_settings(self, db_path: Path):
        s = make_glossary_service(None, db_path=db_path)
        s.upsert(SEED[0])
        assert s.repo.count() == 1

    def test_hotword_limit_from_settings(self, db_path: Path):
        class _Gl:
            hotword_limit = 7

        class _Ex:
            glossary = _Gl()

        class _S:
            extraction = _Ex()

        s = make_glossary_service(_S(), db_path=db_path)
        assert s._hotword_limit == 7  # noqa: SLF001 - 白盒断言默认值来源

    def test_hotword_limit_legacy_flat_layout(self, db_path: Path):
        """兼容旧写法：hotword_limit 直接挂在 extraction 下。"""

        class _Ex:
            hotword_limit = 123

        class _S:
            extraction = _Ex()

        s = make_glossary_service(_S(), db_path=db_path)
        assert s._hotword_limit == 123  # noqa: SLF001

    def test_nested_glossary_wins_over_flat(self, db_path: Path):
        class _Gl:
            hotword_limit = 11

        class _Ex:
            glossary = _Gl()
            hotword_limit = 999

        class _S:
            extraction = _Ex()

        s = make_glossary_service(_S(), db_path=db_path)
        assert s._hotword_limit == 11  # noqa: SLF001

    def test_missing_extraction_falls_back_to_card_default(self, db_path: Path):
        class _S:
            extraction = None

        s = make_glossary_service(_S(), db_path=db_path)
        assert s._hotword_limit == 2000  # noqa: SLF001

    def test_accepts_injected_repo_and_audit(self, repo: GlossaryRepository, audit: AuditLogger):
        s = make_glossary_service(None, repo=repo, audit=audit, operator="wangwu")
        s.upsert(SEED[0])
        assert repo.count() == 1

    def test_settings_without_db_path_resolves_from_data_dir(self, tmp_path: Path):
        """给 settings 不给 db_path 时，按 settings.app.data_dir 推导库位置。

        这是生产入口（`make_glossary_service(settings)`），必须能走通。
        `resolve_path` 是 Settings 的方法（相对路径 -> 项目根下的绝对路径），
        这里用桩替掉，只验"推导逻辑"。注意要先建好表——
        生产上那是迁移器的职责，测试里自己调 migrate。
        """

        class _App:
            data_dir = tmp_path / "data"

        class _S:
            app = _App()
            extraction = None

            @staticmethod
            def resolve_path(p: Path) -> Path:
                return Path(p)

        db = tmp_path / "data" / "mmwf.db"
        migrate(db)
        s = make_glossary_service(_S())
        s.upsert(SEED[0])
        assert s.repo.db_path.name == "mmwf.db"
        assert s.repo.db_path.parent == tmp_path / "data"

    def test_neither_settings_nor_db_path_raises(self):
        with pytest.raises(ValueError, match="至少一个"):
            make_glossary_service(None)

    def test_neither_settings_nor_repo_raises(self):
        """有 settings 但没有 app 段时，无法推导库位置 —— 必须报错。"""

        class _S:
            app = None
            extraction = None

            @staticmethod
            def resolve_path(p):
                return p

        with pytest.raises((ValueError, AttributeError)):
            make_glossary_service(_S())


# ---------------------------------------------------------------------------
# 覆盖补漏：边界与错误分支
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_read_csv_empty_file_raises(self, tmp_path: Path):
        p = tmp_path / "empty.csv"
        p.write_text("", encoding="utf-8")
        with pytest.raises(GlossaryFormatError, match="为空|没有表头"):
            read_csv(p)

    def test_read_csv_only_comments_raises(self, tmp_path: Path):
        p = tmp_path / "onlycomments.csv"
        p.write_text("# 全是注释\n# 没有表头\n", encoding="utf-8")
        with pytest.raises(GlossaryFormatError, match="为空|没有表头"):
            read_csv(p)

    def test_read_csv_header_only_no_rows(self, tmp_path: Path):
        """只有表头、没有数据行：不是错误，得到空库。"""
        p = tmp_path / "headonly.csv"
        p.write_text("category,canonical,aliases,note\n", encoding="utf-8")
        entries, problems = read_csv(p)
        assert entries == []
        assert problems == []

    def test_parse_csv_rows_missing_category_reported(self):
        entries, problems = parse_csv_rows([
            {"canonical": "只有规范写法", "aliases": "", "note": ""},
        ])
        assert entries == []
        assert any("缺 category" in p for p in problems)

    def test_parse_csv_rows_bad_aliases_reported_not_raised(self):
        """别名格式坏掉只报该行，不让整批炸掉。"""
        entries, problems = parse_csv_rows([
            {"category": "term", "canonical": "好条目", "aliases": "", "note": ""},
            {"category": "term", "canonical": "坏条目", "aliases": '{"obj":1}', "note": ""},
        ])
        assert len(entries) == 1
        assert any("JSON" in p for p in problems)

    def test_parse_csv_rows_validate_problems_also_reported(self):
        entries, problems = parse_csv_rows([
            {"category": "term", "canonical": "某术语", "aliases": "某术语", "note": ""},
        ])
        # 条目入册，但问题被报出
        assert len(entries) == 1
        assert len(problems) == 1

    def test_iter_chunks_rejects_nonpositive_size(self):
        with pytest.raises(ValueError, match="size 必须为正"):
            list(iter_chunks(["a"], 0))
        with pytest.raises(ValueError, match="size 必须为正"):
            list(iter_chunks(["a"], -1))

    def test_repo_db_path_property(self, db_path: Path):
        r = GlossaryRepository(db_path)
        assert r.db_path == Path(db_path)

    def test_import_report_to_dict_roundtrip(self, repo: GlossaryRepository):
        rep = repo.replace_all([SEED[0]])
        d = rep.to_dict()
        assert d["upserted"] == 1
        assert d["ok"] is True
        assert isinstance(d["problems"], list)

    def test_prepare_hotwords_conflict_show_limit(self, tmp_path: Path):
        """冲突很多时只展示前 5 个，但计数是全量 —— 告警不能太长。"""
        db = tmp_path / "data" / "mmwf.db"
        migrate(db)
        svc = GlossaryService(GlossaryRepository(db), audit=None)
        # 造 7 组冲突
        for i in range(7):
            svc.upsert(_e(f"a{i}", "person", f"规范甲{i}", (f"冲突别名{i}",)))
            svc.upsert(_e(f"b{i}", "person", f"规范乙{i}", (f"冲突别名{i}",)))
        b = svc.prepare_hotwords()
        assert len(b.conflicts) == 7
        joined = " ".join(b.warnings)
        assert "7 个别名" in joined, f"告警里的计数是全量 7（实际：{joined[:80]}）"

    def test_correct_text_details_serializable(self, svc: GlossaryService):
        for e in SEED:
            svc.upsert(e)
        r = svc.correct_text("中办发了文")
        payload = r.to_dict()
        assert json.dumps(payload, ensure_ascii=False)  # 能被 JSON 序列化（要进审计/接口）
        assert payload["hits"] >= 1
        assert payload["changed"] is True

    def test_set_enabled_returns_false_and_does_not_log(self, svc: GlossaryService, db_path: Path):
        """改不存在的条目：返回 False，且不该留一条"改了啥"的假审计。"""
        before = len(query(db_path))
        assert svc.set_enabled("nope", True) is False
        assert len(query(db_path)) == before

    def test_delete_returns_false_and_does_not_log(self, svc: GlossaryService, db_path: Path):
        before = len(query(db_path))
        assert svc.delete("nope") is False
        assert len(query(db_path)) == before

    def test_read_csv_blank_header_name_raises(self, tmp_path: Path):
        """表头是纯空白（比如 Excel 里误敲了空格）→ 归一化后为空 → 报无表头。"""
        p = tmp_path / "blankheader.csv"
        p.write_text("   \norg,中共中央办公厅,中办,\n", encoding="utf-8")
        with pytest.raises(GlossaryFormatError, match="为空|没有表头|缺少必需列"):
            read_csv(p)

    def test_parse_csv_rows_skips_blank_alias_entries(self):
        """别再让空白别名进库 —— 空串别名会匹配一切（最危险的脏数据）。"""
        entries, problems = parse_csv_rows([
            {"category": "org", "canonical": "某机构", "aliases": "||  ||真别名|", "note": ""},
        ])
        assert len(entries) == 1
        assert entries[0].aliases == ("真别名",)


def test_mask_values_empty_values_is_noop():
    """`_mask_values` 拿到空值表时短路返回，不扫一遍文本。"""
    from mmwf.domain.glossary.matcher import _mask_values

    assert _mask_values("任意文本", []) == ("任意文本", [])
    assert _mask_values("任意文本", [""]) == ("任意文本", [])


def test_iter_alias_pairs_skips_blank_aliases():
    """`iter_alias_pairs` 跳过空白别名。"""
    from mmwf.domain.glossary.matcher import iter_alias_pairs

    pairs = list(iter_alias_pairs([
        GlossaryEntry(entry_id="1", category="org", canonical="某机构",
                      aliases=("", "   ", "真别名")),
    ]))
    assert pairs == [("真别名", "某机构", "1")]
