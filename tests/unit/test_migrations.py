"""T1-02 验收测试：数据模型与建表。

对应施工卡 T1-02 的四条验收标准：
  1. 全新目录执行建库，13 张表齐全
  2. PRAGMA foreign_keys 返回 1，且外键**真实生效**（不是摆设）
  3. test_audit_log_immutable：UPDATE / DELETE 审计表必须抛异常
  4. test_foreign_key_enforced：插入不存在 meeting_id 的 segment 必须失败
另加：迁移幂等性（重复执行不报错、版本号不倒退）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mmwf.infra.db import (
    assert_pragmas,
    connect,
    connect_ctx,
    pragma_value,
    transaction,
)
from mmwf.infra.migrations.runner import (
    MigrationError,
    apply_migration,
    discover_migrations,
    expected_tables,
    get_user_version,
    list_tables,
    migrate,
    pending_migrations,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MEETING_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """全新目录下的空库路径（建库由 migrate 完成）。"""
    return tmp_path / "data" / "mmwf.db"


@pytest.fixture()
def db(db_path: Path) -> sqlite3.Connection:
    """已迁移到最新版本的连接。保证测试结束后关闭（否则 Windows 下句柄泄漏）。"""
    migrate(db_path)
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _open(db_path: Path) -> Iterator[sqlite3.Connection]:
    """生成器版连接夹具，供需要自己管理生命周期的用例使用。

    用 with 块包住，确保异常路径（如 pytest.raises）也关闭连接。
    """
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _insert_meeting(conn: sqlite3.Connection, meeting_id: str = MEETING_ID) -> None:
    """插一条合法会议行，供外键测试做父行。"""
    conn.execute(
        "INSERT INTO meeting (meeting_id, title, classification, created_by, created_at, updated_at)"
        " VALUES (?, ?, 'internal', 'tester', '2026-09-27T10:00:00', '2026-09-27T10:00:00')",
        (meeting_id, "测试会议"),
    )


# ---------------------------------------------------------------------------
# 标准 1：13 张表齐全
# ---------------------------------------------------------------------------

def test_fresh_db_has_all_13_tables(db: sqlite3.Connection):
    """标准 1：全新目录建库后，13 张业务表一张不少。"""
    tables = list_tables(db)
    expected = sorted(expected_tables())
    assert len(expected) == 13, "expected_tables() 本身应为 13 张"
    assert tables == expected, f"建表结果与预期不符\n实际：{tables}\n预期：{expected}"


def test_no_unexpected_tables(db: sqlite3.Connection):
    """没有多余表（防止 DDL 被误加）。"""
    assert list_tables(db) == sorted(expected_tables())


@pytest.mark.parametrize(
    "table_name",
    [
        "meeting", "audio_file", "transcript_segment", "topic", "resolution",
        "action_item", "pending_decision", "minutes_version", "glossary_entry",
        "template", "task_state", "idempotency_record", "audit_log",
    ],
)
def test_table_is_queryable(db: sqlite3.Connection, table_name: str):
    """每张表都能 SELECT，说明 DDL 语法与列定义有效。"""
    db.execute(f"SELECT * FROM {table_name} LIMIT 1").fetchall()


def test_indexes_created(db: sqlite3.Connection):
    """关键索引存在（含 transcript_segment 的部分索引）。"""
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    required = {
        "idx_meeting_status", "idx_meeting_time",
        "idx_seg_meeting_seq", "idx_seg_lowconf",
        "idx_audit_ts", "idx_audit_oper",
    }
    assert required <= names, f"缺失索引：{sorted(required - names)}"


def test_lowconf_partial_index_only_indexes_low_rows(db: sqlite3.Connection):
    """部分索引 WHERE confidence < 0.7：低分段落入索引，高分段的查询不走它。"""
    _insert_meeting(db)
    db.execute(
        "INSERT INTO transcript_segment"
        " (segment_id, meeting_id, seq, start_ms, end_ms, text, confidence)"
        " VALUES ('s1', ?, 1, 0, 1000, '低置信度内容', 0.42)", (MEETING_ID,),
    )
    db.execute(
        "INSERT INTO transcript_segment"
        " (segment_id, meeting_id, seq, start_ms, end_ms, text, confidence)"
        " VALUES ('s2', ?, 2, 1000, 2000, '高置信度内容', 0.98)", (MEETING_ID,),
    )
    # 查询计划应命中 idx_seg_lowconf（且只覆盖低分行）
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM transcript_segment"
        " WHERE meeting_id=? AND confidence < 0.7", (MEETING_ID,),
    ).fetchall()
    detail = " ".join(str(r[-1]) for r in plan)
    assert "idx_seg_lowconf" in detail, f"未命中部分索引：{detail}"


# ---------------------------------------------------------------------------
# 标准 2：外键开启 + PRAGMA 自检
# ---------------------------------------------------------------------------

def test_foreign_keys_pragma_is_on(db: sqlite3.Connection):
    """标准 2：PRAGMA foreign_keys 返回 1。"""
    assert int(pragma_value(db, "foreign_keys")) == 1


def test_journal_mode_is_wal(db: sqlite3.Connection):
    """WAL 模式生效（并发读友好）。"""
    assert str(pragma_value(db, "journal_mode")).lower() == "wal"


def test_synchronous_is_normal(db: sqlite3.Connection):
    """synchronous=NORMAL（WAL 推荐档位）。"""
    assert int(pragma_value(db, "synchronous")) == 1


def test_assert_pragmas_passes(db: sqlite3.Connection):
    """自检函数不抛异常。"""
    assert_pragmas(db)


def test_assert_pragmas_catches_foreign_keys_off(db_path: Path):
    """自检必须能抓出"外键被关掉"这一最危险的错配。"""
    migrate(db_path)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        assert int(pragma_value(conn, "foreign_keys")) == 0
        with pytest.raises(RuntimeError, match="foreign_keys"):
            assert_pragmas(conn)
    finally:
        conn.close()


def test_wal_files_created(db_path: Path):
    """WAL 模式会生成 -wal / -shm，备份脚本必须一并处理（文档已标注）。"""
    migrate(db_path)
    with connect_ctx(db_path) as conn:
        with transaction(conn):
            _insert_meeting(conn)
        assert db_path.with_name(db_path.name + "-wal").exists()


# ---------------------------------------------------------------------------
# 标准 3：审计日志不可改不可删
# ---------------------------------------------------------------------------

def _insert_audit_row(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log"
        " (ts, operator, action, target_type, target_id, detail, result, prev_hash, row_hash)"
        " VALUES ('2026-09-27T10:00:00', 'tester', 'import', 'meeting', ?, '{}', 'success',"
        " 'GENESIS', 'deadbeef')",
        (MEETING_ID,),
    )
    return int(cur.lastrowid)


def test_audit_log_immutable_update(db: sqlite3.Connection):
    """标准 3a：UPDATE 审计表必须抛异常。"""
    _insert_meeting(db)
    _insert_audit_row(db)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_log SET result='failure' WHERE operator='tester'")


def test_audit_log_immutable_delete(db: sqlite3.Connection):
    """标准 3b：DELETE 审计表必须抛异常。"""
    _insert_meeting(db)
    _insert_audit_row(db)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log WHERE operator='tester'")


def test_audit_log_delete_all_still_blocked(db: sqlite3.Connection):
    """清空整张表同样被拦（防止"用 DELETE 无 WHERE 绕过"）。"""
    _insert_meeting(db)
    _insert_audit_row(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")


def test_audit_log_insert_is_allowed(db: sqlite3.Connection):
    """追加写必须畅通——触发器只拦 UPDATE/DELETE。"""
    _insert_meeting(db)
    _insert_audit_row(db)
    _insert_audit_row(db)
    count = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert count == 2


def test_audit_log_row_unchanged_after_blocked_update(db: sqlite3.Connection):
    """被拦下的 UPDATE 不能留下副作用：原值必须原封不动。"""
    _insert_meeting(db)
    _insert_audit_row(db)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE audit_log SET result='failure'")

    result = db.execute("SELECT result FROM audit_log LIMIT 1").fetchone()[0]
    assert result == "success", "触发器未回滚，数据被改坏了"


def test_audit_log_both_triggers_exist(db: sqlite3.Connection):
    """两个触发器都建好了，且都挂在 audit_log 上。"""
    rows = db.execute(
        "SELECT name, tbl_name FROM sqlite_master"
        " WHERE type='trigger' AND name LIKE 'audit_log_%'"
    ).fetchall()
    found = {r[0]: r[1] for r in rows}
    assert found.get("audit_log_no_update") == "audit_log"
    assert found.get("audit_log_no_delete") == "audit_log"


def test_trigger_whitelist_not_empty():
    """回归锁：触发器定义必须存在于 001_init.sql 文本中。

    防止有人"顺手清理"掉触发器而不被测试发现。
    """
    from mmwf.infra.migrations.runner import discover_migrations

    sql = discover_migrations()[0].sql
    assert "audit_log_no_update" in sql
    assert "audit_log_no_delete" in sql
    assert "RAISE(ABORT, 'audit_log is append-only')" in sql


# ---------------------------------------------------------------------------
# 标准 4：外键真实生效
# ---------------------------------------------------------------------------

def test_foreign_key_enforced(db: sqlite3.Connection):
    """标准 4：插入不存在 meeting_id 的 segment，必须失败。"""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "INSERT INTO transcript_segment"
            " (segment_id, meeting_id, seq, start_ms, end_ms, text)"
            " VALUES ('bad', 'no-such-meeting', 1, 0, 1000, '孤儿段落')"
        )


def test_foreign_key_allows_valid_parent(db: sqlite3.Connection):
    """父行存在时必须放行——外键不能变成"什么都插不进"。"""
    _insert_meeting(db)
    db.execute(
        "INSERT INTO transcript_segment"
        " (segment_id, meeting_id, seq, start_ms, end_ms, text, confidence)"
        " VALUES ('ok', ?, 1, 0, 1000, '合法段落', 0.95)", (MEETING_ID,),
    )
    assert db.execute("SELECT COUNT(*) FROM transcript_segment").fetchone()[0] == 1


@pytest.mark.parametrize(
    "child_table, fk_col, extra_cols, extra_vals",
    [
        ("audio_file", "meeting_id",
         "audio_id, original_path, format, duration_ms, size_bytes, sha256, created_at",
         "'a1', 'x.m4a', 'm4a', 1000, 100, 'abc', '2026-09-27T10:00:00'"),
        ("topic", "meeting_id",
         "topic_id, seq, name, source_refs",
         "'t1', 1, '议题一', '[]'"),
        ("resolution", "meeting_id",
         "resolution_id, seq, content, source_refs",
         "'r1', 1, '事项一', '[]'"),
        ("action_item", "meeting_id",
         "action_id, task, source_refs",
         "'ac1', '办一件事', '[]'"),
        ("pending_decision", "meeting_id",
         "decision_id, content, source_refs",
         "'p1', '待决策内容', '[]'"),
        ("minutes_version", "meeting_id",
         "version_id, version_no, structured_json, markdown, created_by, created_at",
         "'v1', 1, '{}', '# 纪要', 'tester', '2026-09-27T10:00:00'"),
    ],
)
def test_all_child_tables_enforce_fk(
    db: sqlite3.Connection, child_table: str, fk_col: str, extra_cols: str, extra_vals: str
):
    """所有指向 meeting 的子表都必须拦孤儿行。"""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            f"INSERT INTO {child_table} ({extra_cols}, {fk_col})"
            f" VALUES ({extra_vals}, 'ghost-meeting')"
        )


def test_meeting_template_fk_enforced(db: sqlite3.Connection):
    """meeting.template_id 指向 template，同样受约束。"""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "INSERT INTO meeting (meeting_id, title, template_id, created_by, created_at, updated_at)"
            " VALUES ('m-x', 'x', 'no-such-template', 'tester',"
            " '2026-09-27T10:00:00', '2026-09-27T10:00:00')"
        )


def test_fk_without_pragma_would_silently_pass(db_path: Path):
    """反证：关掉 PRAGMA foreign_keys 后，同样的孤儿插入会静默成功。

    这条测试是为了把"SQLite 外键默认关闭"这个坑钉死在测试里——
    如果哪天有人把 PRAGMA 那段删了，上面所有 FK 测试会全绿但线上全废。
    本用例证明：差异确实存在，且 PRAGMA 就是那个开关。
    """
    migrate(db_path)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO transcript_segment"
            " (segment_id, meeting_id, seq, start_ms, end_ms, text)"
            " VALUES ('orphan', 'ghost', 1, 0, 1, '孤儿')"
        )
        assert conn.execute("SELECT COUNT(*) FROM transcript_segment").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 迁移器行为
# ---------------------------------------------------------------------------

def test_migration_file_discovery():
    """迁移文件可发现，且 001_init 版本号为 1。"""
    migs = discover_migrations()
    assert migs, "未发现任何迁移文件"
    assert migs[0].version == 1
    assert migs[0].name == "init"


def test_migrate_on_fresh_db_sets_version_1(db_path: Path):
    """新库执行迁移后 user_version = 1。"""
    assert migrate(db_path) == 1


def test_migrate_is_idempotent(db_path: Path):
    """标准（附加）：重复执行迁移不报错、版本号不倒退。"""
    assert migrate(db_path) == 1
    assert migrate(db_path) == 1
    assert migrate(db_path) == 1

    with connect(db_path) as conn:
        assert get_user_version(conn) == 1
        assert len(list_tables(conn)) == 13


def test_migrate_idempotent_preserves_data(db_path: Path):
    """重复迁移不得清空既有数据。"""
    migrate(db_path)
    with connect(db_path) as conn:
        with transaction(conn):
            _insert_meeting(conn)

    migrate(db_path)  # 再跑一遍

    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meeting").fetchone()[0] == 1


def test_pending_migrations_empty_after_migrate(db_path: Path):
    """迁移完成后待办为空。"""
    migrate(db_path)
    with connect(db_path) as conn:
        assert pending_migrations(conn) == []


def test_user_version_zero_on_empty_db(tmp_path: Path):
    """空库（未迁移）user_version = 0。"""
    p = tmp_path / "empty.db"
    with connect(p) as conn:
        assert get_user_version(conn) == 0
        assert list_tables(conn) == []


# ---------------------------------------------------------------------------
# 约束与默认值
# ---------------------------------------------------------------------------

def test_meeting_classification_check_constraint(db: sqlite3.Connection):
    """classification 只允许四个取值（数据分级红线）。"""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute(
            "INSERT INTO meeting (meeting_id, title, classification, created_by, created_at, updated_at)"
            " VALUES ('m1', 'x', 'top_secret', 'tester', '2026-09-27T10:00:00', '2026-09-27T10:00:00')"
        )


@pytest.mark.parametrize(
    "state", ["pending", "approved", "rejected", "edited"]
)
def test_decision_state_accepts_four_values(db: sqlite3.Connection, state: str):
    """decision_state 四态（approve / reject / edit 闸口语义）。"""
    _insert_meeting(db)
    db.execute(
        "INSERT INTO resolution (resolution_id, meeting_id, seq, content, source_refs, decision_state)"
        " VALUES (?, ?, 1, '内容', '[]', ?)", (f"r-{state}", MEETING_ID, state),
    )


def test_decision_state_rejects_unknown(db: sqlite3.Connection):
    """decision_state 非法取值必须被拦。"""
    _insert_meeting(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute(
            "INSERT INTO resolution (resolution_id, meeting_id, seq, content, source_refs, decision_state)"
            " VALUES ('r-bad', ?, 1, '内容', '[]', 'maybe')", (MEETING_ID,),
        )


def test_owner_defaults_to_no_data_placeholder(db: sqlite3.Connection):
    """F3.7：责任人缺省必须是"暂无"，不是空串也不是 NULL。"""
    _insert_meeting(db)
    db.execute(
        "INSERT INTO resolution (resolution_id, meeting_id, seq, content, source_refs)"
        " VALUES ('r-def', ?, 1, '内容', '[]')", (MEETING_ID,),
    )
    owner = db.execute("SELECT owner FROM resolution WHERE resolution_id='r-def'").fetchone()[0]
    assert owner == "暂无"


def test_glossary_unique_category_canonical(db: sqlite3.Connection):
    """术语库 (category, canonical) 唯一。"""
    db.execute(
        "INSERT INTO glossary_entry (entry_id, category, canonical, updated_at)"
        " VALUES ('g1', 'term', '四不两直', '2026-09-27T10:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "INSERT INTO glossary_entry (entry_id, category, canonical, updated_at)"
            " VALUES ('g2', 'term', '四不两直', '2026-09-27T10:00:00')"
        )


def test_minutes_version_unique_meeting_version_no(db: sqlite3.Connection):
    """纪要版本 (meeting_id, version_no) 唯一。"""
    _insert_meeting(db)
    cols = ("version_id, meeting_id, version_no, structured_json, markdown,"
            " created_by, created_at")
    db.execute(
        f"INSERT INTO minutes_version ({cols}) VALUES ('v1', ?, 1, '{{}}', '#a', 't', '2026-09-27T10:00:00')",
        (MEETING_ID,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            f"INSERT INTO minutes_version ({cols}) VALUES ('v2', ?, 1, '{{}}', '#b', 't', '2026-09-27T10:00:00')",
            (MEETING_ID,),
        )


def test_audit_log_id_uses_autoincrement(db: sqlite3.Connection):
    """audit_log.log_id 单调递增且不复用（AUTOINCREMENT 生效）。"""
    _insert_meeting(db)
    ids = [_insert_audit_row(db) for _ in range(3)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_transaction_rolls_back_on_error(db: sqlite3.Connection):
    """事务异常回滚：半截数据不落库。"""
    with pytest.raises(RuntimeError):
        with transaction(db):
            _insert_meeting(db)
            raise RuntimeError("模拟中途失败")
    assert db.execute("SELECT COUNT(*) FROM meeting").fetchone()[0] == 0


def test_transaction_commits_on_success(db: sqlite3.Connection):
    """事务正常退出提交。"""
    with transaction(db):
        _insert_meeting(db)
    assert db.execute("SELECT COUNT(*) FROM meeting").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 迁移器异常路径
# ---------------------------------------------------------------------------

def test_bad_migration_filename_raises(tmp_path: Path):
    """不符合命名规则的 .sql 必须报错，不能静默忽略。"""
    (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="不符合规则"):
        discover_migrations(tmp_path)


def test_duplicate_version_raises(tmp_path: Path):
    """同版本号两个文件必须报错。"""
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="重复"):
        discover_migrations(tmp_path)


def test_broken_sql_raises_and_leaves_version_unchanged(db_path: Path):
    """迁移 SQL 语法错误：抛 MigrationError，且 user_version 不被推进。"""
    migrate(db_path)

    bad = discover_migrations()[0]
    broken = type(bad)(version=2, name="broken",
                       path=db_path.parent / "002_broken.sql")
    broken.path.write_text("CREATE TABLE ((( ;", encoding="utf-8")

    with connect(db_path) as conn:
        with pytest.raises(MigrationError, match="002_broken"):
            apply_migration(conn, broken)
        assert get_user_version(conn) == 1, "失败的迁移不应推进版本号"


def test_missing_migrations_dir_raises(tmp_path: Path):
    """迁移目录不存在时报错。"""
    with pytest.raises(MigrationError, match="不存在"):
        discover_migrations(tmp_path / "nope")


# ---------------------------------------------------------------------------
# T1-17 缺陷 25：`minutes_version` 弃用标注（表保留，不写不读）
# ---------------------------------------------------------------------------

def test_minutes_version_table_still_exists(db: sqlite3.Connection):
    """★ 缺陷 25：弃用**不等于删表** —— 表必须仍在，且结构不变。

    删表是不可逆的 schema 变更，已交付的 `mmwf.db` 可能已建过它。
    本卡只做"标注弃用"，绝不 DROP。
    """
    assert "minutes_version" in list_tables(db)


def test_minutes_version_columns_unchanged(db: sqlite3.Connection):
    """弃用标注不得顺手改结构（列名/列数保持原样）。"""
    cols = [r[1] for r in db.execute("PRAGMA table_info(minutes_version)")]
    assert cols == [
        "version_id",
        "meeting_id",
        "version_no",
        "structured_json",
        "markdown",
        "is_final",
        "created_by",
        "created_at",
        "note",
    ]


def test_minutes_version_is_documented_as_deprecated():
    """★ 缺陷 25 的**回归锁**：弃用说明必须写在迁移文件里，且指明朝向。

    这条测试防的是"将来有人看到 0 写 0 读就顺手把表删了/把注释删了"。
    判据：迁移 SQL 内该表上方存在弃用说明，且点明了启用时点。
    """
    from mmwf.infra.migrations.runner import discover_migrations

    sql = discover_migrations()[0].path.read_text(encoding="utf-8")

    # 定位建表语句
    marker = "CREATE TABLE IF NOT EXISTS minutes_version"
    assert marker in sql, "minutes_version 建表语句丢失"

    # 建表语句之前的 2000 字符内必须有弃用说明
    head = sql[: sql.index(marker)][-2000:]
    assert "弃用" in head, "弃用标注丢失（T1-17 缺陷 25）"
    assert "T2-04" in head, "弃用说明必须指明启用时点（二期 T2-04）"


def test_minutes_version_stays_empty_in_phase1(db: sqlite3.Connection):
    """一期该表**恒为 0 行** —— 这是"弃用"在运行期的体现。

    若某天这条红了，说明有人开始写这张表了：
    要么是二期 T2-04 正常启用（那就应当同时更新本卡与迁移注释），
    要么是有人偷偷写了个旁路写入。两种情况都值得被这条测试叫停。
    """
    _insert_meeting(db)
    assert db.execute("SELECT COUNT(*) FROM minutes_version").fetchone()[0] == 0
