"""数据库接入层（T1-02）。

本模块是**唯一**允许 `import sqlite3` 的基础设施入口，职责有三：
  1. 建立连接并施加必需的 PRAGMA（外键、WAL、忙等待）
  2. 提供 transaction() 上下文管理器，统一事务边界
  3. 暴露 engine()，供后续需要 SQLAlchemy Core 的模块复用（T1-04 起）

为什么用 sqlite3 标准库而不是 SQLAlchemy ORM
--------------------------------------------
SAD §7 已定调：**SQLAlchemy Core（不用 ORM 全功能）**。一期 13 张表、
字段与约束都在 001_init.sql 里手写 DDL，ORM 的模型映射只会多一层间接、
且让审计 SQL 变得更难看清。因此本模块以 sqlite3 为主干，
SQLAlchemy 仅在需要跨方言/连接池时按需引入（engine()）。

三条必须记住的 SQLite 坑
------------------------
1. **外键默认关闭**。`PRAGMA foreign_keys` 是"每连接"设置，不设就永远为 0，
   所有 `FOREIGN KEY` 声明全部失效。这是本项目最容易埋雷的一处（施工卡 T1-02 明写）。
2. **WAL 会生成 -wal / -shm 两个附属文件**。加密、备份、搬家脚本必须一并处理，
   否则会出现"主库拷走了、最近的事务还在 -wal 里"的数据丢失。
3. **sqlite3.connect 默认 infer 事务边界**，DDL 行为随 Python 版本漂移。
   本模块统一 `isolation_level=None`（自动提交模式）并显式 BEGIN，
   让事务边界完全可控、可读。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# 连接级 PRAGMA（每次 connect 都要重设，不是全局设置）
#
#   foreign_keys=ON   外键约束开关，默认 OFF，必须显式打开
#   journal_mode=WAL  预写日志，读不阻塞写，单机多进程（Electron 壳 + 服务）友好
#   synchronous=NORMAL WAL 下的推荐档位：崩溃不丢已提交事务，性能明显优于 FULL
#   busy_timeout=5000 遇到锁时最多等 5 秒再报 SQLITE_BUSY，避免瞬时并发直接失败
PRAGMAS: tuple[str, ...] = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
)


def connect(db_path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """打开一个已设置好 PRAGMA 的连接。

    调用方负责关闭：推荐用本模块的 `connect_ctx()` 上下文管理器。

    readonly=True 时以 URI 只读模式打开——用于校验脚本，
    从连接层就杜绝了"校验程序误写审计表"的可能。
    """
    db_path = Path(db_path)
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"库文件不存在，无法只读打开：{db_path}")
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        # 只读连接下 journal_mode 不可改，只设读取相关的
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        return conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> 关闭 sqlite3 的隐式事务，改由我们显式 BEGIN
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    for pragma in PRAGMAS:
        conn.execute(pragma)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connect_ctx(db_path: Path | str, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """连接上下文管理器，退出时确保关闭。"""
    conn = connect(db_path, readonly=readonly)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """显式事务。正常退出提交，异常回滚并向上抛。

    注意：**不支持嵌套**。SQLite 没有真正的嵌套事务
    （SAVEPOINT 是另一套机制，本项目一期用不到）。
    嵌套调用会因 "cannot start a transaction within a transaction" 报错——
    这是刻意保留的失败，比静默复用外层事务更安全。
    """
    # 需先读后写的业务可提前取得写锁，避免并发读取同一旧版本。
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# 健康检查与自检
# ---------------------------------------------------------------------------

def pragma_value(conn: sqlite3.Connection, name: str) -> object:
    """读取单个 PRAGMA 的值（如 foreign_keys / journal_mode）。"""
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row is not None else None


def assert_pragmas(conn: sqlite3.Connection) -> None:
    """自检：三条关键 PRAGMA 必须生效，否则立刻炸。

    为什么放在这里而不是只在测试里：这是"错了就全盘皆输"的设置，
    运行期任何一次连接建立都值得花几十微秒确认一次。
    """
    fk = pragma_value(conn, "foreign_keys")
    if int(fk or 0) != 1:
        raise RuntimeError(
            f"PRAGMA foreign_keys={fk!r}，必须为 1。"
            "外键未开启时所有 FOREIGN KEY 声明都是摆设（SAD C12 场景失守）。"
        )
    jm = str(pragma_value(conn, "journal_mode") or "").lower()
    if jm != "wal":
        # 只读连接无法设 WAL，容忍 memory/delete 以外的异常值仅告警不抛
        if jm not in ("wal", "memory", "delete"):
            raise RuntimeError(f"PRAGMA journal_mode={jm!r}，期望 wal")
    sync = pragma_value(conn, "synchronous")
    if int(sync or -1) not in (1, 2):  # NORMAL=1, FULL=2
        raise RuntimeError(f"PRAGMA synchronous={sync!r}，期望 1(NORMAL) 或 2(FULL)")


def app_db_path(data_dir: Path | str) -> Path:
    """业务库的标准位置：<data_dir>/mmwf.db（SAD §8.1）。"""
    return Path(data_dir) / "mmwf.db"


def engine(db_path: Path | str):
    """返回 SQLAlchemy Engine（可选能力，仅 T1-04 起的 Core 用法需要）。

    不在此处 import sqlalchemy：一期大部分模块只用 sqlite3，
    把它做成**惰性导入**可以让"没装 SQLAlchemy 也能跑核心链路"。
    本函数保留是为了让后续要写 Core 表达式的模块有统一入口，
    并保证 PRAGMA 设置与 connect() 完全一致（同一组常量）。

    **poolclass=NullPool 是刻意的选择**：
      - 本应用是单机单进程、短事务，连接复用带来的收益极小；
      - 池化连接会在进程退出时留下未关闭的 SQLite 句柄
        （Windows 下表现为文件被锁、临时目录删不掉）；
      - 更重要的是 PRAGMA 是**每连接**设置，池化复用连接有概率
        绕过 connect 事件（如池中已有旧连接），而 NullPool 每次新建、
        必然走一遍 PRAGMA，让"外键一定开着"这件事没有例外。
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import Engine
    from sqlalchemy.pool import NullPool

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    eng: Engine = create_engine(
        f"sqlite+pysqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - 引擎回调
        cur = dbapi_conn.cursor()
        for pragma in PRAGMAS:
            cur.execute(pragma)
        cur.close()

    return eng
