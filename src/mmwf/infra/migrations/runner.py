"""迁移执行器（T1-02）。

设计要点
--------
1. **版本推进靠 SQLite 内建的 `user_version`**，不额外建"迁移记录表"。
   为什么：迁移记录表本身也需要建表，会陷入"先有鸡还是先有蛋"；
   `user_version` 是数据库文件头里的一个 32 位整数，零成本、原子随事务提交。

2. **一个文件 = 一个版本号**：`001_init.sql` -> user_version 1，
   `002_xxx.sql` -> user_version 2。文件名必须是 `<3位数字>_<描述>.sql`。

3. **每个迁移单独一个事务**。SQLite 的 DDL 是事务性的，因此
   "建到一半崩了"不会留下半张表的库——要么这个版本全成功，要么全回滚。

4. **SQL 文件内不写 BEGIN/COMMIT**，事务边界由本模块控制
   （脚本里写 BEGIN 会让 sqlite3 执行器报 "cannot start a transaction
   within a transaction"）。

5. **不支持降级（downgrade）**。理由：这是本机单机工具，回滚靠
   "删库重来 + 从备份恢复"，写一套没人验证过的 downgrade 脚本反而是风险。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mmwf.infra.db import connect, transaction

MIGRATIONS_DIR = Path(__file__).resolve().parent

# 文件名规则：001_init.sql / 002_add_xxx.sql
_FILENAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """迁移过程中的不可恢复错误。"""


@dataclass(frozen=True)
class Migration:
    """一个迁移文件。"""

    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """扫描迁移目录，按版本号升序返回。

    遇到不符合命名规则的文件直接报错——静默忽略是灾难，
    会让"写了迁移但没生效"这种问题拖到生产才暴露。
    """
    d = migrations_dir or MIGRATIONS_DIR
    if not d.is_dir():
        raise MigrationError(f"迁移目录不存在：{d}")

    found: list[Migration] = []
    for p in sorted(d.glob("*.sql")):
        m = _FILENAME_RE.match(p.name)
        if m is None:
            raise MigrationError(
                f"迁移文件名不符合规则 <3位数字>_<小写描述>.sql：{p.name}"
            )
        found.append(Migration(version=int(m.group(1)), name=m.group(2), path=p))

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        dupes = sorted({v for v in versions if versions.count(v) > 1})
        raise MigrationError(f"迁移版本号重复：{dupes}")

    return sorted(found, key=lambda m: m.version)


def get_user_version(conn: sqlite3.Connection) -> int:
    """读取当前库版本。新库（空文件）返回 0。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    """已应用的版本号列表（=[1..user_version]）。

    user_version 语义约定为"已成功应用的最高版本号"，因此 1..N 全部视为已应用。
    """
    cur = get_user_version(conn)
    return list(range(1, cur + 1))


def pending_migrations(
    conn: sqlite3.Connection, migrations_dir: Path | None = None
) -> list[Migration]:
    """待应用的迁移（版本号 > 当前 user_version）。"""
    cur = get_user_version(conn)
    return [m for m in discover_migrations(migrations_dir) if m.version > cur]


def split_statements(sql: str) -> list[str]:
    """把 SQL 脚本切分成单条可执行语句。

    **为什么必须自己切而不能用 `conn.executescript()`**：
    `executescript()` 会先隐式 COMMIT 掉外层事务再执行——它自带的事务语义
    与我们的 `transaction()` 上下文管理器冲突，结果就是
    "cannot commit - no transaction is active"（T1-02 实测踩到）。

    **为什么不能用朴素的 `sql.split(";")`**：
    `CREATE TRIGGER ... BEGIN ... END;` 的正文里也有分号。
    朴素切分会把触发器体劈成两半，execute 时报 "incomplete input"
    （T1-02 第二次实测踩到）。本例正是本项目的审计防篡改触发器，
    属于"一定会踩"的坑，不是边角情形。

    正确做法：用 `sqlite3.complete_statement()` —— 这是 SQLite C API
    `sqlite3_complete()` 的 Python 绑定，它**认识 BEGIN...END 触发器体**，
    逐字符累加直到语句在语法上完整为止。这才是权威切分器。
    """
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    # 文件末尾若有无分号结尾的残余语句，也一并执行（宽松处理）
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


def apply_migration(conn: sqlite3.Connection, mig: Migration) -> None:
    """在**单一事务**内应用一个迁移，并把 user_version 推到 mig.version。

    关键点：
      - 逐条 execute，不用 executescript（见 split_statements 的说明）。
      - `PRAGMA user_version = N` 不能用参数绑定（SQLite 不支持），
        所以这里用 f-string 拼整数。version 来自文件名正则的 `\\d{3}`，
        已保证是纯数字，不存在注入面。
      - **PRAGMA 在事务内的行为**：`user_version` 是数据库文件头字段，
        其赋值在事务内同样可回滚，因此"建表 + 记版本"是原子的——
        要么全成功，要么这个版本一行不留。
    """
    try:
        with transaction(conn):
            for stmt in split_statements(mig.sql):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {mig.version:d}")
    except sqlite3.Error as e:
        raise MigrationError(
            f"迁移 {mig.version:03d}_{mig.name} 执行失败：{e}"
        ) from e


def migrate(
    db_path: Path,
    migrations_dir: Path | None = None,
    *,
    verbose: bool = False,
) -> int:
    """把库推进到最新版本，返回最终 user_version。

    幂等：已应用的版本不会重跑，重复调用返回同一个版本号。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with connect(db_path) as conn:
        todo = pending_migrations(conn, migrations_dir)
        if verbose:
            print(f"[migrate] {db_path} 当前 user_version={get_user_version(conn)}，"
                  f"待应用 {len(todo)} 个迁移")
        for mig in todo:
            apply_migration(conn, mig)
            if verbose:
                print(f"[migrate]   已应用 {mig.version:03d}_{mig.name}")
        final = get_user_version(conn)
        if verbose:
            print(f"[migrate] 完成，user_version={final}")
        return final


def expected_tables() -> list[str]:
    """本版本应存在的业务表（供验收脚本核对）。"""
    return [
        "meeting", "audio_file", "transcript_segment", "topic", "resolution",
        "action_item", "pending_decision", "minutes_version", "glossary_entry",
        "template", "template_revision", "task_state", "idempotency_record", "audit_log",
    ]


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """列出库中的业务表（排除 SQLite 内部表）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]
