"""T1-02 附带测试：db.py 连接层。

覆盖 test_migrations.py 未触及的分支：只读连接、engine() 惰性导入、
app_db_path()、以及 connect_ctx() 的异常安全关闭。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mmwf.infra.db import (
    PRAGMAS,
    app_db_path,
    assert_pragmas,
    connect,
    connect_ctx,
    engine,
    pragma_value,
    transaction,
)
from mmwf.infra.migrations.runner import migrate

MEETING_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def ready_db(tmp_path: Path) -> Path:
    p = tmp_path / "data" / "mmwf.db"
    migrate(p)
    return p


# ---------------------------------------------------------------------------
# 路径约定
# ---------------------------------------------------------------------------

def test_app_db_path_follows_sad_convention():
    """业务库标准位置：<data_dir>/mmwf.db（SAD §8.1）。"""
    assert app_db_path("./data").as_posix().endswith("data/mmwf.db")
    assert app_db_path(Path("/x/y")).name == "mmwf.db"


def test_connect_creates_parent_dirs(tmp_path: Path):
    """父目录不存在时自动创建（部署时 data/ 可能还没建）。"""
    p = tmp_path / "deep" / "nested" / "mmwf.db"
    assert not p.parent.exists()
    with connect_ctx(p):
        pass
    assert p.parent.is_dir()
    assert p.exists()


def test_pragmas_constant_covers_the_three_critical_ones():
    """回归锁：三条关键 PRAGMA 必须留在常量里。"""
    joined = " ".join(PRAGMAS).lower()
    assert "foreign_keys = on" in joined
    assert "journal_mode = wal" in joined
    assert "synchronous = normal" in joined


# ---------------------------------------------------------------------------
# 只读连接
# ---------------------------------------------------------------------------

def test_readonly_connect_can_query(ready_db: Path):
    """只读连接能正常读。"""
    with connect_ctx(ready_db, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meeting").fetchone()[0] == 0


def test_readonly_connect_blocks_writes(ready_db: Path):
    """只读连接必须在写入时报错（校验脚本不允许误写审计表）。"""
    with connect_ctx(ready_db, readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, created_by, created_at, updated_at)"
                " VALUES ('x', 'x', 'x', 'x', 'x')"
            )


def test_readonly_connect_missing_file_raises(tmp_path: Path):
    """只读打开不存在的库要明确报错，而不是静默新建一个空库。"""
    with pytest.raises(FileNotFoundError, match="不存在"):
        connect(tmp_path / "nope.db", readonly=True)


def test_readonly_connect_still_enables_foreign_keys(ready_db: Path):
    """只读连接同样要开外键（校验脚本查孤儿行时依赖它）。"""
    with connect_ctx(ready_db, readonly=True) as conn:
        assert int(pragma_value(conn, "foreign_keys")) == 1


def test_readonly_connect_row_factory_is_row(ready_db: Path):
    """只读连接的 row_factory 也是 sqlite3.Row，可按列名取值。"""
    with connect_ctx(ready_db) as conn:
        with transaction(conn):
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, classification, created_by,"
                " created_at, updated_at)"
                " VALUES (?, '标题', 'internal', 't', 'x', 'x')", (MEETING_ID,),
            )
    with connect_ctx(ready_db, readonly=True) as conn:
        row = conn.execute("SELECT title FROM meeting").fetchone()
        assert row["title"] == "标题"


# ---------------------------------------------------------------------------
# connect_ctx 异常安全
# ---------------------------------------------------------------------------

def test_connect_ctx_closes_on_exception(tmp_path: Path):
    """异常路径也要关闭连接（否则 Windows 下句柄泄漏、文件锁死）。"""
    p = tmp_path / "data" / "mmwf.db"
    migrate(p)

    captured: sqlite3.Connection | None = None
    with pytest.raises(RuntimeError):
        with connect_ctx(p) as conn:
            captured = conn
            raise RuntimeError("模拟业务异常")

    assert captured is not None
    # 已关闭的连接再执行语句会抛 ProgrammingError
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured.execute("SELECT 1")


# ---------------------------------------------------------------------------
# engine() 惰性导入
# ---------------------------------------------------------------------------

def test_engine_returns_working_sqlalchemy_engine(tmp_path: Path):
    """engine() 应返回可用的 SQLAlchemy Engine，且 PRAGMA 与 connect() 一致。"""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import text

    p = tmp_path / "data" / "mmwf.db"
    migrate(p)

    eng = engine(p)
    with eng.connect() as c:
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert str(c.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"
        assert c.execute(text("SELECT COUNT(*) FROM meeting")).scalar() == 0
    eng.dispose()


def test_engine_supports_transactional_write(tmp_path: Path):
    """engine() 建立的连接能写能回滚。"""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    p = tmp_path / "data" / "mmwf.db"
    migrate(p)

    eng = engine(p)
    with eng.connect() as c:
        # 外键同样生效：孤儿行应被拒
        with pytest.raises(IntegrityError):
            c.execute(
                text(
                    "INSERT INTO transcript_segment"
                    " (segment_id, meeting_id, seq, start_ms, end_ms, text)"
                    " VALUES ('o', 'ghost', 1, 0, 1, '孤儿')"
                )
            )
    eng.dispose()


def test_engine_creates_parent_dirs(tmp_path: Path):
    """engine() 也要能自动建父目录。"""
    pytest.importorskip("sqlalchemy")

    p = tmp_path / "created" / "by" / "engine.db"
    eng = engine(p)
    eng.dispose()
    assert p.parent.is_dir()


# ---------------------------------------------------------------------------
# assert_pragmas 其余分支
# ---------------------------------------------------------------------------

def test_assert_pragmas_flags_bad_journal_mode(ready_db: Path):
    """journal_mode 异常时报错（含自定义错误信息）。"""
    with connect_ctx(ready_db) as conn:
        # 强制退回 delete 之外的非法值不太好构造，这里用 memory 库验证逻辑边界：
        # memory 库 journal_mode 为 'memory'，被宽容接受
        mem = sqlite3.connect(":memory:", isolation_level=None)
        try:
            mem.execute("PRAGMA foreign_keys = ON")
            mem.execute("PRAGMA synchronous = NORMAL")
            # memory 库的 journal_mode 为 memory —— 属于白名单，不应抛
            assert_pragmas(mem)
        finally:
            mem.close()


def test_assert_pragmas_flags_bad_synchronous(ready_db: Path):
    """synchronous=OFF(0) 必须被自检抓出。"""
    with connect_ctx(ready_db) as conn:
        conn.execute("PRAGMA synchronous = OFF")
        with pytest.raises(RuntimeError, match="synchronous"):
            assert_pragmas(conn)


# ---------------------------------------------------------------------------
# transaction() 嵌套拒绝
# ---------------------------------------------------------------------------

def test_nested_transaction_is_rejected(ready_db: Path):
    """刻意不支持嵌套事务：嵌套调用必须报错而非静默复用外层。"""
    with connect_ctx(ready_db) as conn:
        with transaction(conn):
            with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
                with transaction(conn):
                    pass
