"""T1-02 验收脚本（人可读版）。

施工卡 T1-02 的四条验收标准 + 迁移幂等性（附加），逐条跑一遍并打印结论。
用法：python tools/accept_t1_02.py
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PASS = "✓"
FAIL = "✗"

MEETING_ID = "11111111-1111-4111-8111-111111111111"


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# 标准 1：13 张表齐全
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：全新目录执行建库，13 张表齐全。"""
    banner("标准 1：全新目录建库，13 张表齐全（SAD §8.3）")

    from mmwf.infra.db import connect
    from mmwf.infra.migrations.runner import expected_tables, list_tables, migrate

    db_path = tmp / "fresh" / "data" / "mmwf.db"
    print(f"  目标库：{db_path}（上级目录此前不存在）")

    version = migrate(db_path)
    print(f"  user_version = {version}")

    with connect(db_path) as conn:
        tables = list_tables(conn)
        expected = sorted(expected_tables())

    print(f"  实际建表 {len(tables)} 张：")
    for t in tables:
        mark = PASS if t in expected else FAIL
        print(f"    {mark} {t}")

    missing = sorted(set(expected) - set(tables))
    extra = sorted(set(tables) - set(expected))
    ok = len(tables) == 13 and not missing and not extra
    if missing:
        print(f"  {FAIL} 缺失表：{missing}")
    if extra:
        print(f"  {FAIL} 多余表：{extra}")
    print(f"  {PASS if ok else FAIL} 13 张表齐全")
    return ok


def check_indexes(tmp: Path) -> bool:
    """附加：关键索引（含部分索引）存在。"""
    banner("附加检查：关键索引")

    from mmwf.infra.db import connect
    from mmwf.infra.migrations.runner import migrate

    db_path = tmp / "idx" / "data" / "mmwf.db"
    migrate(db_path)

    with connect(db_path) as conn:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    required = {
        "idx_meeting_status", "idx_meeting_time",
        "idx_seg_meeting_seq", "idx_seg_lowconf",
        "idx_audit_ts", "idx_audit_oper",
    }
    missing = sorted(required - names)
    ok = not missing
    print(f"  已建索引 {len(names)} 个（含 SQLite 自动创建的 UNIQUE 索引）")
    print(f"  {PASS if ok else FAIL} 关键索引齐全" + (f"，缺 {missing}" if missing else ""))
    return ok


# ---------------------------------------------------------------------------
# 标准 2：PRAGMA foreign_keys == 1
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：PRAGMA foreign_keys 返回 1，且三条关键 PRAGMA 生效。"""
    banner("标准 2：PRAGMA foreign_keys = 1（SQLite 默认关闭，必须显式打开）")

    from mmwf.infra.db import connect, pragma_value
    from mmwf.infra.migrations.runner import migrate

    db_path = tmp / "pragma" / "data" / "mmwf.db"
    migrate(db_path)

    ok = True
    with connect(db_path) as conn:
        checks = [
            ("foreign_keys", "1", "外键约束开关，默认 OFF"),
            ("journal_mode", "wal", "预写日志，读写不互锁"),
            ("synchronous", "1", "NORMAL 档位（WAL 推荐）"),
        ]
        for name, want, why in checks:
            got = str(pragma_value(conn, name)).lower()
            good = got == want
            ok &= good
            print(f"  {PASS if good else FAIL} PRAGMA {name} = {got!r}（期望 {want}）— {why}")

    return ok


# ---------------------------------------------------------------------------
# 标准 3：审计日志不可改不可删
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    """标准 3：UPDATE / DELETE 审计表必须抛异常。"""
    banner("标准 3：审计日志只可追加（SAD C12）")

    from mmwf.infra.db import connect, transaction
    from mmwf.infra.migrations.runner import migrate

    db_path = tmp / "audit" / "data" / "mmwf.db"
    migrate(db_path)

    ok = True
    # 注意：Windows 下必须等到连接关闭才能删除库文件，因此这里用
    # try/finally 显式 close，不用 with（with 在本函数中已足够，
    # 但保留显式形式以免后续插入早期 return 时漏关）。
    conn = connect(db_path)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, classification, created_by,"
                " created_at, updated_at)"
                " VALUES (?, '测试会议', 'internal', 'tester',"
                " '2026-09-27T10:00:00', '2026-09-27T10:00:00')",
                (MEETING_ID,),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, operator, action, target_type, target_id,"
                " detail, result, prev_hash, row_hash)"
                " VALUES ('2026-09-27T10:00:00','tester','import','meeting',?,"
                " '{}','success','GENESIS','deadbeef')",
                (MEETING_ID,),
            )
        print(f"  {PASS} 追加写成功（audit_log 行数 = "
              f"{conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]}）")

        for label, sql in [
            ("UPDATE", "UPDATE audit_log SET result='failure'"),
            ("DELETE", "DELETE FROM audit_log"),
        ]:
            try:
                conn.execute(sql)
                print(f"  {FAIL} {label} 竟然成功了 —— 防篡改触发器失效！")
                ok = False
            except sqlite3.IntegrityError as e:
                good = "append-only" in str(e)
                ok &= good
                print(f"  {PASS if good else FAIL} {label} 被拒：{e}")

        # 被拦下后原值必须原封不动
        after = conn.execute("SELECT result FROM audit_log LIMIT 1").fetchone()[0]
        good = after == "success"
        ok &= good
        print(f"  {PASS if good else FAIL} 被拦后原值未被改动（result = {after!r}）")
    finally:
        conn.close()

    return ok


# ---------------------------------------------------------------------------
# 标准 4：外键真实生效
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：插入不存在 meeting_id 的 segment 必须失败。"""
    banner("标准 4：外键约束真实生效（不是摆设）")

    from mmwf.infra.db import connect
    from mmwf.infra.migrations.runner import migrate

    db_path = tmp / "fk" / "data" / "mmwf.db"
    migrate(db_path)

    ok = True
    with connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO transcript_segment"
                " (segment_id, meeting_id, seq, start_ms, end_ms, text)"
                " VALUES ('bad', 'no-such-meeting', 1, 0, 1000, '孤儿段落')"
            )
            print(f"  {FAIL} 孤儿行插入成功 —— 外键形同虚设！")
            ok = False
        except sqlite3.IntegrityError as e:
            good = "FOREIGN KEY" in str(e).upper()
            ok &= good
            print(f"  {PASS if good else FAIL} 孤儿 segment 被拒：{e}")

        # 父行存在时必须放行
        conn.execute(
            "INSERT INTO meeting (meeting_id, title, classification, created_by,"
            " created_at, updated_at)"
            " VALUES (?, '合法会议', 'internal', 'tester',"
            " '2026-09-27T10:00:00', '2026-09-27T10:00:00')",
            (MEETING_ID,),
        )
        try:
            conn.execute(
                "INSERT INTO transcript_segment"
                " (segment_id, meeting_id, seq, start_ms, end_ms, text, confidence)"
                " VALUES ('ok', ?, 1, 0, 1000, '合法段落', 0.95)", (MEETING_ID,),
            )
            print(f"  {PASS} 有父行时正常插入（外键未过度拦截）")
        except sqlite3.IntegrityError as e:
            print(f"  {FAIL} 合法插入被误拒：{e}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# 附加：迁移幂等
# ---------------------------------------------------------------------------

def check_idempotent(tmp: Path) -> bool:
    """附加：重复执行迁移不报错、版本号不倒退、数据不丢。"""
    banner("附加检查：迁移幂等（重复执行安全）")

    from mmwf.infra.db import connect, transaction
    from mmwf.infra.migrations.runner import migrate

    db_path = tmp / "idem" / "data" / "mmwf.db"
    v1 = migrate(db_path)
    with connect(db_path) as conn:
        with transaction(conn):
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, classification, created_by,"
                " created_at, updated_at)"
                " VALUES (?, '幂等测试', 'internal', 'tester',"
                " '2026-09-27T10:00:00', '2026-09-27T10:00:00')",
                (MEETING_ID,),
            )
    v2 = migrate(db_path)
    v3 = migrate(db_path)

    with connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM meeting").fetchone()[0]

    ok = (v1 == v2 == v3 == 1) and rows == 1
    print(f"  三次 migrate() 返回：{v1} / {v2} / {v3}")
    print(f"  既有数据保留：meeting 行数 = {rows}")
    print(f"  {PASS if ok else FAIL} 幂等且不丢数据")
    return ok


# ---------------------------------------------------------------------------
# 附加：全量用例
# ---------------------------------------------------------------------------

def check_tests() -> bool:
    banner("附加检查：T1-02 全量用例")

    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/unit/test_migrations.py", "-q", "--no-header"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    tail = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    last = tail[-1] if tail else "(无输出)"
    ok = result.returncode == 0
    print(f"  {PASS if ok else FAIL} {last}")
    if not ok:
        print(result.stdout[-2000:])
    return ok


def main() -> int:
    print("T1-02 验收：数据模型与建表")
    print(f"项目根：{PROJECT_ROOT}")

    # Windows 下 SQLite 的 -wal / -shm 句柄在解释器回收对象前不会释放，
    # TemporaryDirectory 默认的强删除会抛 WinError 32。
    # 这里用 ignore_cleanup_errors=True 让临时目录清理"尽力而为"：
    # 验收结论不受影响，系统临时目录本来也会被回收。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        results = {
            "标准 1 十三张表齐全": check_standard_1(tmp),
            "标准 2 外键 PRAGMA 开启": check_standard_2(tmp),
            "标准 3 审计日志不可改删": check_standard_3(tmp),
            "标准 4 外键真实生效": check_standard_4(tmp),
            "附加 关键索引齐全": check_indexes(tmp),
            "附加 迁移幂等": check_idempotent(tmp),
            "附加 全量用例通过": check_tests(),
        }

    banner("验收结论")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL} {name}")

    all_ok = all(results.values())
    print(f"\n{'=' * 70}")
    print(f"{PASS} T1-02 验收通过" if all_ok else f"{FAIL} T1-02 验收未通过")
    print(f"{'=' * 70}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
