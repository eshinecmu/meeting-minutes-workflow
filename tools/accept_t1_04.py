"""T1-04 验收脚本（人可读版）。

施工卡 T1-04 的四条验收标准，逐条跑一遍并打印结论。
用法：python tools/accept_t1_04.py

四条标准（施工卡原文）：
  [ ] 一串操作后，tools/verify_audit_chain.py 校验通过
  [ ] 手工 UPDATE audit_log SET action='x' WHERE log_id=1 必须被触发器拒绝
  [ ] 若绕过触发器直接改文件（模拟篡改），校验脚本能定位到出错行
  [ ] test_audit_records_gate_decision：闸口批准/驳回/编辑三个动作都留痕，
      且 detail 里能看出改了什么
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.infra.db import connect_ctx  # noqa: E402
from mmwf.infra.migrations.runner import migrate  # noqa: E402

PASS = "✓"
FAIL = "✗"

MEETING_ID = "b7c1e2a0-3f4d-4e5b-8a6c-9d0e1f2a3b4c"
VERIFY_CLI = PROJECT_ROOT / "tools" / "verify_audit_chain.py"

# 由 main() 填入实际临时根目录，供 _sub() 使用
_TMP_BASE: list[Path] = [Path(".")]


def banner(text: str) -> None:
    print(f"\n{'=' * 74}\n{text}\n{'=' * 74}")


def _sub(name: str) -> Path:
    """每个检查用独立的子目录，避免互相污染。"""
    d = _TMP_BASE[0] / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fresh_db(root: Path) -> Path:
    db = root / "data" / "mmwf.db"
    migrate(db)
    return db


def _run_verify(db: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """调用真正的命令行工具（而不是 import 它的函数）——验收必须验"交付物本身"。"""
    return subprocess.run(
        [sys.executable, str(VERIFY_CLI), "--db", str(db), *extra],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 标准 1：一串操作后校验通过
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：一串操作后，tools/verify_audit_chain.py 校验通过。"""
    banner("标准 1：一串操作后，verify_audit_chain.py 校验通过")

    from mmwf.infra.audit import AuditLogger, query, verify_chain

    db = _fresh_db(tmp)
    audit = AuditLogger(db)

    # 模拟一次完整任务的审计流（覆盖 AUD-04 的关键动作）
    ops = [
        ("zhangsan", "login", None, None, {"client": "desktop"}),
        ("zhangsan", "import_audio", "meeting", MEETING_ID,
         {"filename": "党组会.m4a", "size_bytes": 52_428_800}),
        ("zhangsan", "import_text", "meeting", MEETING_ID, {"filename": "上会议题.docx"}),
        ("zhangsan", "transcribe_start", "meeting", MEETING_ID,
         {"engine": "funasr", "model": "paraformer-zh"}),
        ("zhangsan", "transcribe_done", "meeting", MEETING_ID,
         {"segment_count": 428, "elapsed_ms": 610_000}),
        ("wangwu", "segment_edit", "segment", "seg-0017",
         {"fields": ["text"], "before_len": 52, "after_len": 58}),
        ("wangwu", "gate_decision", "meeting", MEETING_ID,
         {"decision": "approve", "gate": "G1"}),
        ("zhangsan", "glossary_change", "glossary", "g-0007",
         {"change": "update", "canonical": "某某办公室"}),
        ("zhangsan", "export", "meeting", MEETING_ID,
         {"files": ["export/某某会议纪要.docx"], "classification": "内部"}),
        ("zhangsan", "selfcheck", "system", "run-01", {"outbound_connections": 0}),
        ("zhangsan", "logout", None, None, {}),
    ]
    for operator, action, tt, tid, detail in ops:
        audit.log(operator, action, tt, tid, detail)

    print(f"  库文件：{db}")
    print(f"  写入了 {audit.count()} 条审计记录：")
    for r in query(db):
        print(f"    #{r.log_id:<3} {r.ts}  {r.operator:<9} {r.action:<17} "
              f"{r.result:<8} {r.row_hash[:16]}…")

    result = verify_chain(db)
    print()
    print(f"  链式校验：{result.summary()}")
    ok = result.ok
    print(f"  {PASS if ok else FAIL} import 级校验通过")

    # 关键是**命令行工具**也要通过（验收标准写的是运行这个脚本）
    proc = _run_verify(db)
    cli_ok = proc.returncode == 0 and "审计链校验通过" in proc.stdout
    ok &= cli_ok
    print(f"  {PASS if cli_ok else FAIL} 命令行工具退出码 {proc.returncode}"
          f"（期望 0），输出含「审计链校验通过」")
    if not cli_ok:
        print(proc.stdout[-2000:])
        print(proc.stderr[-1000:])

    # 顺带展示退出码语义（0 完好 / 1 篡改 / 2 环境错误）
    print(f"      退出码约定：0=完好，1=发现篡改，2=用法或环境错误")
    proc_missing = _run_verify(tmp / "no_such.db")
    miss_ok = proc_missing.returncode == 2
    ok &= miss_ok
    print(f"  {PASS if miss_ok else FAIL} 库不存在时退出码 {proc_missing.returncode}"
          f"（期望 2，与「篡改」区分开）")

    return ok


# ---------------------------------------------------------------------------
# 标准 2：手工 UPDATE 被触发器拒绝
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：手工 UPDATE audit_log SET action='x' WHERE log_id=1 被拒绝。"""
    banner("标准 2：手工 UPDATE audit_log 被触发器拒绝（AUD-01 / AUD-02）")

    import sqlite3

    from mmwf.infra.audit import AuditLogger, query

    db = _fresh_db(tmp)
    audit = AuditLogger(db)
    for i in range(3):
        audit.log("zhangsan", "selfcheck", "system", f"run-{i}")

    before = query(db)[0]
    print(f"  改前：#1  action={before.action!r}  row_hash={before.row_hash[:20]}…")
    print()

    # —— 施工卡原文的那一条 SQL ——
    stmt = "UPDATE audit_log SET action='x' WHERE log_id=1"
    print(f"  执行：{stmt}")
    try:
        with connect_ctx(db) as conn:
            conn.execute(stmt)
        print(f"  {FAIL} **竟然成功了** —— 触发器失守，这是一个严重缺陷")
        ok = False
    except sqlite3.IntegrityError as e:
        print(f"  {PASS} 被拒绝：sqlite3.IntegrityError: {e}")
        ok = True

    after = query(db)[0]
    unchanged = after.action == before.action and after.row_hash == before.row_hash
    ok &= unchanged
    print(f"  {PASS if unchanged else FAIL} 原值未被改动"
          f"（action={after.action!r}，row_hash 一致={after.row_hash == before.row_hash}）")

    # DELETE 同样要拦
    print()
    print("  执行：DELETE FROM audit_log WHERE log_id=1")
    try:
        with connect_ctx(db) as conn:
            conn.execute("DELETE FROM audit_log WHERE log_id=1")
        print(f"  {FAIL} **竟然成功了**")
        ok = False
    except sqlite3.IntegrityError as e:
        print(f"  {PASS} 被拒绝：sqlite3.IntegrityError: {e}")
        ok &= True

    count_ok = audit.count() == 3
    ok &= count_ok
    print(f"  {PASS if count_ok else FAIL} 记录数仍为 3（被拒的 DELETE 未生效）")

    # 全表操作也拦
    print()
    print("  执行：UPDATE audit_log SET operator='attacker'  （不带 WHERE 的全表改）")
    try:
        with connect_ctx(db) as conn:
            conn.execute("UPDATE audit_log SET operator='attacker'")
        print(f"  {FAIL} **竟然成功了**")
        ok = False
    except sqlite3.IntegrityError as e:
        print(f"  {PASS} 被拒绝：{e}")

    # 触发器确实存在于 schema
    with connect_ctx(db) as conn:
        trigs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'"
        )]
    trig_ok = sorted(trigs) == ["audit_log_no_delete", "audit_log_no_update"]
    ok &= trig_ok
    print(f"  {PASS if trig_ok else FAIL} 库里存在 2 个触发器：{sorted(trigs)}")

    # 表结构层面的已知缺口（诚实标注）
    print()
    print("  已知缺口（诚实记录，不隐瞒）：")
    print("    · `DROP TABLE audit_log` 是 DDL，**不触发**行级 DELETE 触发器，")
    print("      因此能绕过 SQL 层防护。但整表消失是极明显的痕迹，")
    print("      校验脚本会立刻以 no such table 报错（退出码 2）。")
    print("    · 该缺口已写入单元测试 test_drop_table_is_NOT_blocked_documented_gap")

    return ok


# ---------------------------------------------------------------------------
# 标准 3：绕过触发器改文件，校验脚本定位到出错行
# ---------------------------------------------------------------------------

def _forge_row(db: Path, log_id: int, **fields: object) -> None:
    """摘掉触发器 -> 改行 -> 装回触发器。模拟"用编辑器改 .db 文件字节"。

    为什么可以这样模拟：校验脚本读的是表里的**字段值**，不看是谁改的、
    也不看当下触发器在不在。所以"改完再装回触发器"的库状态，
    与"用十六进制编辑器改过字节、触发器从未被动过"的库状态，
    在校验脚本眼里完全等价。
    """
    with connect_ctx(db) as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE audit_log SET {sets} WHERE log_id=?",
                     (*fields.values(), log_id))
    with connect_ctx(db) as conn2:
        conn2.execute(
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        )


def check_standard_3(tmp: Path) -> bool:
    """标准 3：绕过触发器直接改文件，校验脚本能定位到出错行。"""
    banner("标准 3：绕过触发器改文件后，校验脚本定位到出错行（AUD-03）")

    from mmwf.infra.audit import AuditLogger

    db = _fresh_db(tmp)
    audit = AuditLogger(db)
    for i in range(6):
        audit.log("zhangsan", "selfcheck", "system", f"run-{i}")

    proc = _run_verify(db)
    ok = proc.returncode == 0
    print(f"  篡改前：退出码 {proc.returncode}（期望 0），链完好")
    print(f"  {PASS if ok else FAIL} 篡改前校验通过")

    # —— 模拟篡改：把第 3 条记录的动作从 selfcheck 改成 export ——
    print()
    print("  模拟篡改：把 log_id=3 的动作 selfcheck -> export")
    _forge_row(db, 3, action="export")

    proc = _run_verify(db, "--verbose")
    print(f"  篡改后：退出码 {proc.returncode}（期望 1 = 发现篡改）")
    print()
    print("  ── 校验脚本输出 ──")
    for line in proc.stdout.splitlines():
        print(f"  │ {line}")
    print("  ── 输出结束 ──")
    print()

    tamper_detected = proc.returncode == 1
    ok &= tamper_detected
    print(f"  {PASS if tamper_detected else FAIL} 退出码 1，篡改被发现")

    # 必须精确指到第 3 行
    locates = "log_id=3" in proc.stdout
    ok &= locates
    print(f"  {PASS if locates else FAIL} 精确指出出错行 log_id=3")

    # 必须给出处置指引（不能只是报错让人猜）
    guides = "不得自行修复" in proc.stdout
    ok &= guides
    print(f"  {PASS if guides else FAIL} 输出了处置指引（部署手册 §7.7）")

    # 只报一行，不牵连后面无辜的行
    only_one = proc.stdout.count("row_hash_mismatch") == 1
    ok &= only_one
    print(f"  {PASS if only_one else FAIL} 只报被改的那一行，未牵连后续行")

    # 再验一种更阴险的篡改：整行替换 + 重算 row_hash（单行自洽）
    print()
    print("  模拟更隐蔽的篡改：把 log_id=2 整行内容换成 log_id=1 的（含其 row_hash）")
    db2 = _fresh_db(_sub("s3b"))
    a2 = AuditLogger(db2)
    for i in range(4):
        a2.log("zhangsan", "selfcheck", "system", f"run-{i}")
    with connect_ctx(db2) as conn:
        row1 = dict(conn.execute("SELECT * FROM audit_log WHERE log_id=1").fetchone())
    with connect_ctx(db2) as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute(
            "UPDATE audit_log SET ts=?, operator=?, action=?, target_type=?,"
            " target_id=?, detail=?, result=?, prev_hash=?, row_hash=? WHERE log_id=2",
            (row1["ts"], row1["operator"], row1["action"], row1["target_type"],
             row1["target_id"], row1["detail"], row1["result"],
             row1["prev_hash"], row1["row_hash"]),
        )
    proc2 = _run_verify(db2)
    caught_by_prev = "prev_hash_mismatch" in proc2.stdout
    ok &= caught_by_prev
    print(f"  {PASS if caught_by_prev else FAIL} 被 prev_hash 比对抓到"
          f"（单行哈希自洽也逃不掉 —— 这正是链式哈希的存在理由）")
    print(f"  该场景下退出码 {proc2.returncode}（期望 1）")
    ok &= proc2.returncode == 1

    # 校验必须是只读的：跑两次不改库
    from mmwf.infra.audit import query
    h_before = [r.row_hash for r in query(db)]
    _run_verify(db)
    _run_verify(db)
    readonly_ok = [r.row_hash for r in query(db)] == h_before
    ok &= readonly_ok
    print(f"  {PASS if readonly_ok else FAIL} 校验跑两次，库内容零变化（只读打开）")

    return ok


# ---------------------------------------------------------------------------
# 标准 4：test_audit_records_gate_decision
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：闸口批准/驳回/编辑三动作都留痕，detail 能看出改了什么。"""
    banner("标准 4：test_audit_records_gate_decision（F4.5 / 验收标准 4）")

    from mmwf.infra.audit import AuditLogger, AuditQuery, query, verify_chain

    db = _fresh_db(tmp)
    audit = AuditLogger(db)

    # —— 批准 ——
    r_approve = audit.gate_decision(
        "wangwu", MEETING_ID, "approve",
        gate="G2", from_node="awaiting_confirm", to_node="extracting",
    )
    # —— 驳回 ——
    r_reject = audit.gate_decision(
        "wangwu", MEETING_ID, "reject",
        gate="G2", from_node="awaiting_confirm", to_node="extracting",
        comment="第 3 项议题依据不足，退回补充",
    )
    # —— 编辑 ——
    r_edit = audit.gate_decision(
        "wangwu", MEETING_ID, "edit",
        gate="G2", from_node="awaiting_confirm", to_node="extracting",
        changed={
            "title": {"old": "（草）关于某工作的通知", "new": "关于某工作的通知"},
            "owner": {"old": "暂无", "new": "办公室"},
        },
        comment="仅改表述，不需重跑",
    )
    # —— 超时自动拒绝（F4.6 安全默认，必须可区分于人工决策）——
    r_auto = audit.gate_decision(
        "system", MEETING_ID, "reject", gate="G2", auto=True,
        comment="超过 24 小时未确认，已自动退回",
    )

    records = query(db, AuditQuery(action="gate_decision"))
    print(f"  写入 {len(records)} 条闸口决策记录：")
    for r in records:
        print(f"    #{r.log_id}  operator={r.operator:<9} "
              f"decision={r.detail['decision']:<8} "
              f"auto={r.detail.get('auto', False)}")
        print(f"        detail = {json.dumps(r.detail, ensure_ascii=False)}")
    print()

    ok = len(records) == 4
    decisions = [r.detail["decision"] for r in records]
    three_ok = decisions[:3] == ["approve", "reject", "edit"]
    ok &= three_ok
    print(f"  {PASS if three_ok else FAIL} 批准 / 驳回 / 编辑 三个动作都留痕"
          f"（{decisions[:3]}）")

    # detail 能看出"改了什么"
    changed = r_edit.detail.get("changed", {})
    keys_ok = r_edit.detail.get("changed_keys") == ["owner", "title"]
    ok &= keys_ok
    print(f"  {PASS if keys_ok else FAIL} edit 场景 detail.changed_keys = "
          f"{r_edit.detail.get('changed_keys')}（看得出改了哪几个字段）")
    detail_ok = (changed.get("owner", {}).get("new") == "办公室"
                 and changed.get("title", {}).get("new") == "关于某工作的通知")
    ok &= detail_ok
    print(f"  {PASS if detail_ok else FAIL} changed 含新旧值，能看出具体改成了什么")

    # 审批人 / 闸口 / 流转方向
    flow_ok = (r_approve.operator == "wangwu"
               and r_approve.detail["gate"] == "G2"
               and r_approve.detail["from_node"] == "awaiting_confirm"
               and r_approve.detail["to_node"] == "extracting")
    ok &= flow_ok
    print(f"  {PASS if flow_ok else FAIL} 记录了审批人 / 闸口 / 流转方向"
          f"（{r_approve.detail['from_node']} -> {r_approve.detail['to_node']}）")

    # 超时自动拒绝可区分
    auto_ok = r_auto.detail.get("auto") is True and r_auto.operator == "system"
    ok &= auto_ok
    print(f"  {PASS if auto_ok else FAIL} 超时自动拒绝带 auto=true、operator=system"
          f"（可与人工决策区分）")

    # 驳回理由留痕
    reject_ok = "依据不足" in r_reject.detail["comment"]
    ok &= reject_ok
    print(f"  {PASS if reject_ok else FAIL} 驳回理由已留痕")

    # 四条记录链式完好
    chain_ok = verify_chain(db).ok
    ok &= chain_ok
    print(f"  {PASS if chain_ok else FAIL} 四条记录链式哈希完好")

    # 对照跑一次真正的单元测试
    print()
    rc = _run_unit_test("-k", "test_audit_records_gate_decision")
    ok &= rc
    print(f"  {PASS if rc else FAIL} pytest -k test_audit_records_gate_decision 通过")

    return ok


# ---------------------------------------------------------------------------
# 附加：检索 / 导出报告 / 8 类核查项
# ---------------------------------------------------------------------------

def check_search_and_report(tmp: Path) -> bool:
    """附加：可检索（AUD-07）与可导出（AUD-06）与 8 类核查项（AUD-08）。"""
    banner("附加检查：可检索 / 可导出 / 8 类最小核查项覆盖")

    from mmwf.infra.audit import (
        AuditLogger,
        AuditQuery,
        audit_minimum_coverage,
        build_report,
        query,
    )

    db = _fresh_db(tmp)
    audit = AuditLogger(db)

    audit.log("zhangsan", "login", detail={"client": "desktop"})
    audit.log("zhangsan", "import_audio", "meeting", MEETING_ID,
              detail={"filename": "党组会.m4a"})
    audit.log("zhangsan", "import_text", "meeting", MEETING_ID,
              detail={"filename": "上会议题.docx"})
    audit.log("zhangsan", "transcribe_start", "meeting", MEETING_ID,
              detail={"engine": "funasr"})
    audit.log("zhangsan", "transcribe_done", "meeting", MEETING_ID,
              detail={"segment_count": 428})
    audit.segment_edit("zhangsan", MEETING_ID, "seg-0017", fields=["text"],
                       before_len=52, after_len=58)
    audit.gate_decision("wangwu", MEETING_ID, "approve", gate="G1")
    audit.log("zhangsan", "glossary_change", "glossary", "g-0007",
              detail={"change": "update"})
    audit.log("zhangsan", "template_change", "template", "t-01",
              detail={"change": "update"})
    audit.export_event("zhangsan", MEETING_ID, files=["export/纪要.docx"],
                       classification="内部")
    audit.log("zhangsan", "logout")
    audit.log("zhangsan", "selfcheck", "system", "run-01",
              detail={"outbound_connections": 0})

    ok = True

    # —— AUD-07 检索 ——
    print("  AUD-07 检索：")
    cases = [
        ("按人员 zhangsan", AuditQuery(operator="zhangsan")),
        ("按人员 wangwu", AuditQuery(operator="wangwu")),
        ("按类型 gate_decision", AuditQuery(action="gate_decision")),
        ("按目标 meeting", AuditQuery(target_type="meeting", target_id=MEETING_ID)),
        ("按动作 export", AuditQuery(action="export")),
    ]
    for label, q in cases:
        rows = query(db, q)
        good = len(rows) > 0 and all(
            (not q.operator or r.operator == q.operator)
            and (not q.action or r.action == q.action)
            and (not q.target_id or r.target_id == q.target_id)
            for r in rows
        )
        ok &= good
        print(f"    {PASS if good else FAIL} {label}：{len(rows)} 条")

    # 时间区间过滤：全部记录在同一秒内写入，所以用"起点=最后一秒"会全部命中。
    # 要验证的是**区间语义**，因此给前 4 条显式指定较早的时间戳。
    from mmwf.infra.db import connect_ctx as _ctx

    db2 = _fresh_db(_sub("s5b"))
    audit2 = AuditLogger(db2)
    early = "2026-09-01T09:00:00Z"
    late = "2026-09-27T09:00:00Z"
    for i in range(4):
        audit2.log("zhangsan", "selfcheck", "system", f"early-{i}", ts=early)
    for i in range(3):
        audit2.log("zhangsan", "selfcheck", "system", f"late-{i}", ts=late)

    rows = query(db2, AuditQuery(from_ts=late))
    time_ok = len(rows) == 3
    ok &= time_ok
    print(f"  {PASS if time_ok else FAIL} 按时间起点 {late} 过滤："
          f"{len(rows)} 条（期望 3 条 late-*）")

    rows = query(db2, AuditQuery(from_ts=early, to_ts=early))
    range_ok = len(rows) == 4
    ok &= range_ok
    print(f"  {PASS if range_ok else FAIL} 闭区间 [early, early]："
          f"{len(rows)} 条（期望 4 条 early-*）")

    rows = query(db2, AuditQuery(to_ts=early))
    to_ok = len(rows) == 4
    ok &= to_ok
    print(f"  {PASS if to_ok else FAIL} 按时间终点 {early} 过滤："
          f"{len(rows)} 条（期望 4 条）")

    # 定长性：字典序 == 时间序（检索的正确性依赖这一点）
    all_ts = [r.ts for r in query(db2)]
    sorted_ok = all_ts == sorted(all_ts) and len({len(t) for t in all_ts}) == 1
    ok &= sorted_ok
    print(f"  {PASS if sorted_ok else FAIL} ts 定长 UTC，字典序 == 时间序")

    # 只读打开的回归（校验脚本本身不得有写权限）
    with _ctx(db2, readonly=True) as c:
        ro = True
        try:
            c.execute("INSERT INTO audit_log (ts,operator,action,result,row_hash)"
                      " VALUES ('T','x','login','success','h')")
            ro = False
        except Exception:
            pass
    ok &= ro
    print(f"  {PASS if ro else FAIL} 只读连接无法写入（校验脚本不可被利用为篡改入口）")

    # —— AUD-08 8 类核查项 ——
    print()
    print("  AUD-08 8 类最小核查项覆盖（部署手册 §6.2）：")
    cov = audit_minimum_coverage(db)
    for item, state in cov.items():
        mark = {True: PASS, False: FAIL}.get(state == "ok")
        note = {"ok": "已有记录", "empty": "动作齐备、尚无记录",
                "missing": "**动作未登记**"}[state]
        print(f"    {mark if state == 'ok' else '·'} {item:<16} {note}")
    cov_ok = all(v == "ok" for v in cov.values())
    ok &= cov_ok
    print(f"  {PASS if cov_ok else FAIL} 8 类核查项全部有记录")

    # —— AUD-06 导出报告 ——
    print()
    report = build_report(db, generated_at="2026-09-27T12:00:00Z")
    print("  AUD-06 审计报告（供保密检查）：")
    print(f"    完整性结论：integrity.result = {report['integrity']['result']!r}"
          f"（checked {report['integrity']['checked_rows']} 行）")
    print(f"    时间段：{report['period']}")
    print(f"    操作人汇总：{report['summary']['operators']}")
    print(f"    动作分布：{report['summary']['actions']}")
    print(f"    明细条数：{len(report['records'])}")
    integ_ok = report["integrity"]["result"] == "pass"
    ok &= integ_ok
    print(f"  {PASS if integ_ok else FAIL} 报告含完整性校验结论")

    # 报告必须 JSON 可序列化（要能走 HTTP / 写文件）
    try:
        blob = json.dumps(report, ensure_ascii=False)
        json.loads(blob)
        json_ok = True
    except (TypeError, ValueError) as e:
        json_ok = False
        print(f"    序列化失败：{e}")
    ok &= json_ok
    print(f"  {PASS if json_ok else FAIL} 报告 JSON 可序列化（{len(blob):,} 字符）")

    # 篡改后报告必须转 fail 并带处置指引
    _forge_row(db, 3, action="export")
    bad = build_report(db)
    fail_ok = (bad["integrity"]["result"] == "fail"
               and "action_required" in bad
               and "不得自行修复" in bad["action_required"])
    ok &= fail_ok
    print(f"  {PASS if fail_ok else FAIL} 篡改后报告转 fail 并给出 action_required")

    # —— AUD-05 detail 无正文 ——
    print()
    print("  AUD-05 detail 不含转写稿原文：")
    bad_ok = True
    for r in query(db):
        leaked = {"text", "content", "full_text", "原文", "正文"} & set(r.detail)
        if leaked:
            bad_ok = False
            print(f"    {FAIL} log_id={r.log_id} 的 detail 含 {leaked}")
    ok &= bad_ok
    print(f"  {PASS if bad_ok else FAIL} 全部记录的 detail 均无正文键名")

    # 主动拦截：试图写正文必须报错
    try:
        audit.log("zhangsan", "segment_edit", "segment", "s1",
                  detail={"text": "同志们，今天我们召开党组会…"})
        print(f"  {FAIL} 含正文的写入竟然成功了")
        ok = False
    except Exception as e:
        blocked = "禁止的键名" in str(e)
        ok &= blocked
        print(f"  {PASS if blocked else FAIL} 含正文的写入被主动拦截（{type(e).__name__}）")

    return ok


# ---------------------------------------------------------------------------
# 附加：全量用例
# ---------------------------------------------------------------------------

def _run_unit_test(*args: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_audit.py", *args,
         "-q", "--no-header"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    # 用 stdout + stderr 合并判定：pytest 的 "-q" 在选中用例少时可能只输出到 stderr
    lines = [ln for ln in (result.stdout + result.stderr).strip().splitlines() if ln.strip()]
    print(f"      {lines[-1] if lines else '(无输出)'}")
    if result.returncode != 0:
        print((result.stdout + result.stderr)[-3000:])
    return result.returncode == 0


def check_tests() -> bool:
    banner("附加检查：T1-04 全量用例")
    return _run_unit_test()


# ---------------------------------------------------------------------------
# 附加：命令行工具在默认库路径上可用
# ---------------------------------------------------------------------------

def check_cli_help() -> bool:
    banner("附加检查：verify_audit_chain.py 命令行可用性")

    ok = True
    proc = subprocess.run(
        [sys.executable, str(VERIFY_CLI), "--help"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    help_ok = proc.returncode == 0 and "--verbose" in proc.stdout and "退出码" in proc.stdout
    ok &= help_ok
    print(f"  {PASS if help_ok else FAIL} --help 正常，且说明退出码语义")

    # 部署手册 §5.2 要求：备份完成后校验备份库
    # -> 必须支持 --db 指向任意路径
    supports_db = "--db" in proc.stdout
    ok &= supports_db
    print(f"  {PASS if supports_db else FAIL} 支持 --db（部署手册 §5.2 校验备份用）")

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print("T1-04 验收：审计日志")
    print(f"项目根：{PROJECT_ROOT}")
    print(f"校验工具：{VERIFY_CLI}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _TMP_BASE[0] = Path(td)
        results = {
            "标准 1 一串操作后校验通过": check_standard_1(_sub("s1")),
            "标准 2 UPDATE 被触发器拒绝": check_standard_2(_sub("s2")),
            "标准 3 改文件能定位出错行": check_standard_3(_sub("s3")),
            "标准 4 闸口决策留痕": check_standard_4(_sub("s4")),
            "附加 可检索/可导出/8类核查项": check_search_and_report(_sub("s5")),
            "附加 命令行工具可用": check_cli_help(),
            "附加 全量用例通过": check_tests(),
        }

    banner("验收结论")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL} {name}")

    all_ok = all(results.values())
    print(f"\n{'=' * 74}")
    print(f"{PASS} T1-04 验收通过" if all_ok else f"{FAIL} T1-04 验收未通过")
    print(f"{'=' * 74}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
