"""T1-04 单元测试：审计日志。

对标施工卡四条验收标准 + 测试方案 §7.2 的 AUD-01~AUD-08：

    验收1  一串操作后 verify_audit_chain.py 校验通过
    验收2  手工 UPDATE audit_log 被触发器拒绝
    验收3  绕过触发器改文件，校验脚本能定位出错行
    验收4  test_audit_records_gate_decision：批准/驳回/编辑三动作都留痕，detail 能看出改了什么

    AUD-01  UPDATE 被拒                -> test_update_rejected_by_trigger
    AUD-02  DELETE 被拒                -> test_delete_rejected_by_trigger
    AUD-03  绕过触发器改文件能定位      -> test_file_level_tamper_is_located
    AUD-04  完整任务关键动作均有记录    -> test_full_task_records_all_key_actions
    AUD-05  detail 不含转写稿原文       -> test_detail_rejects_transcript_text
    AUD-06  导出报告含完整性结论        -> test_build_report_contains_integrity
    AUD-07  按时间/人员/类型检索准确    -> test_query_by_*
    AUD-08  8 类必需动作覆盖            -> test_minimum_check_items_covered
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mmwf.infra.audit import (
    ACTIONS,
    GENESIS_HASH,
    MAX_DETAIL_STRING_LEN,
    MINIMUM_CHECK_ITEM_ACTIONS,
    AuditError,
    AuditLogger,
    AuditQuery,
    audit_minimum_coverage,
    build_report,
    check_detail_safety,
    compute_row_hash,
    detail_from_json,
    detail_to_json,
    prune_expired,
    query,
    verify_chain,
    verify_chain_rows,
)
from mmwf.infra.db import connect, connect_ctx
from mmwf.infra.migrations.runner import migrate

MEETING_ID = "33333333-3333-4333-8333-333333333333"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "data" / "mmwf.db"
    migrate(p)
    return p


@pytest.fixture()
def audit(db_path: Path) -> AuditLogger:
    return AuditLogger(db_path)


def _seed(a: AuditLogger, n: int = 5) -> None:
    """写下 n 条可区分时序的记录（ts 固定递增，便于断言）。"""
    base = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        ts = (base + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
        a.log("zhangsan", "selfcheck", "system", f"run-{i}",
              detail={"seq": i}, ts=ts)


# ---------------------------------------------------------------------------
# 哈希计算（纯函数）
# ---------------------------------------------------------------------------

def test_compute_row_hash_is_stable_and_hex64():
    h1 = compute_row_hash(GENESIS_HASH, "2026-09-27T10:00:00Z", "a", "login",
                          None, None, "{}", "success")
    h2 = compute_row_hash(GENESIS_HASH, "2026-09-27T10:00:00Z", "a", "login",
                          None, None, "{}", "success")
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_compute_row_hash_none_renders_as_empty_not_literal_none():
    """None 必须渲染成空串，而不是字符串 'None'。

    若渲染成 'None'，`target_id=None` 与 `target_id='None'` 会算出同一个
    哈希 —— 审计里"没有目标"和"目标名叫 None"混为一谈。
    """
    with_none = compute_row_hash(GENESIS_HASH, "T", "op", "export", None, None, "{}", "success")
    with_literal = compute_row_hash(GENESIS_HASH, "T", "op", "export", "None", "None", "{}", "success")
    assert with_none != with_literal

    # 与"显式空串"相等（这才是正确的语义：都表示无）
    with_empty = compute_row_hash(GENESIS_HASH, "T", "op", "export", "", "", "{}", "success")
    assert with_none == with_empty


def test_pipe_separator_prevents_field_splicing_collision():
    """分隔符的核心价值：('ab','c') 与 ('a','bc') 不能撞车。"""
    h1 = compute_row_hash("p", "T", "ab", "c", None, None, "{}", "success")
    h2 = compute_row_hash("p", "T", "a", "bc", None, None, "{}", "success")
    assert h1 != h2


def test_each_field_participates_in_hash():
    """逐字段改动都必须改变哈希——否则某个字段就是"审计上不可见"的。"""
    base = dict(prev_hash="p", ts="T", operator="op", action="export",
                target_type="meeting", target_id="m1", detail_json="{}", result="success")
    baseline = compute_row_hash(**base)
    for field_name, new_value in [
        ("prev_hash", "q"),
        ("ts", "T2"),
        ("operator", "op2"),
        ("action", "export_undo"),
        ("target_type", "segment"),
        ("target_id", "m2"),
        ("detail_json", '{"a":1}'),
        ("result", "failure"),
    ]:
        mutated = {**base, field_name: new_value}
        assert compute_row_hash(**mutated) != baseline, f"{field_name} 未参与哈希计算"


# ---------------------------------------------------------------------------
# detail 序列化
# ---------------------------------------------------------------------------

def test_detail_json_sort_keys_makes_hash_order_independent():
    """字典插入顺序不影响哈希——否则"同一条记录重算"会误报篡改。"""
    a = detail_to_json({"b": 2, "a": 1})
    b = detail_to_json({"a": 1, "b": 2})
    assert a == b


def test_detail_json_keeps_chinese_readable():
    """ensure_ascii=False：审计报告要给人看，不能是 \\uXXXX。"""
    s = detail_to_json({"note": "闸口批准"})
    assert "闸口批准" in s
    assert "\\u" not in s


def test_detail_from_json_tolerates_corrupt_data():
    """一行坏 detail 不能让整个检索功能挂掉。"""
    assert detail_from_json("") == {}
    assert detail_from_json(None) == {}
    assert detail_from_json("{不是json")["_corrupt"] == "{不是json"
    assert detail_from_json("[1,2]") == {"_raw": [1, 2]}


# ---------------------------------------------------------------------------
# AUD-05：detail 不得含转写稿原文
# ---------------------------------------------------------------------------

def test_detail_rejects_transcript_text():
    """AUD-05：含正文的 detail 必须被拒绝写入。"""
    with pytest.raises(AuditError, match="禁止的键名"):
        check_detail_safety({"segment_id": "s1", "text": "同志们，今天我们召开…"})

    with pytest.raises(AuditError, match="禁止的键名"):
        check_detail_safety({"转写稿": "同志们"})

    # 嵌套也要能扫到
    with pytest.raises(AuditError, match="禁止的键名"):
        check_detail_safety({"outer": {"inner": {"full_text": "很长的正文"}}})

    with pytest.raises(AuditError, match="禁止的键名"):
        check_detail_safety({"items": [{"content": "正文"}]})


def test_detail_rejects_container_key_holding_a_list():
    """容器类键名装了列表 —— 键名本身合法，判定靠"键名 + 值类型"。"""
    with pytest.raises(AuditError, match="容器类键名"):
        check_detail_safety({"segments": [{"id": "s1", "text": "正文一"},
                                          {"id": "s2", "text": "正文二"}]})

    with pytest.raises(AuditError, match="容器类键名"):
        check_detail_safety({"topics": {"t1": "议题内容"}})

    with pytest.raises(AuditError, match="容器类键名"):
        check_detail_safety({"分段": [{"x": 1}]})


def test_container_key_with_scalar_value_is_allowed():
    """**关键的正向用例**：`segments=428` 是正常审计，不能被误拦。

    最初把 `segments` 放进"禁止键名"黑名单，结果正常的
    "转写产出 428 条分段"直接被拒 —— 过度封禁会逼调用方改键名绕过去，
    反而降低审计可读性。所以改成"键名 + 值类型"双条件。
    """
    check_detail_safety({"segments": 428, "elapsed_ms": 610_000})
    check_detail_safety({"segment_count": 428})
    check_detail_safety({"topics": 6})
    check_detail_safety({"lines": 1200})


def test_container_key_even_when_empty_is_rejected():
    """空列表也拒 —— 守住"这个键永远不装容器"这条不变量。

    为什么不为空列表开个口子：值为空时确实不含正文，但
    "先允许写空、以后自然就有人往里塞东西"是典型的滑坡。
    调用方要记数量，有标量形式可用（`segments=0`）；
    要记标识符列表，键名不在容器黑名单里（如 `files`）就不会被拦。
    """
    with pytest.raises(AuditError, match="容器类键名"):
        check_detail_safety({"segments": []})


def test_detail_rejects_long_string_even_without_forbidden_key():
    """键名不在黑名单里，但值是长文本 —— 一样要拦。

    黑名单挡不住"换个键名继续塞正文"，长度上限是补上的第二道闸。
    """
    with pytest.raises(AuditError, match="疑似正文"):
        check_detail_safety({"note": "字" * (MAX_DETAIL_STRING_LEN + 1)})

    # 恰好等于上限则放行（边界：不误杀正常的长路径/长错误信息）
    check_detail_safety({"note": "字" * MAX_DETAIL_STRING_LEN})


def test_detail_rejects_over_nested_payload():
    """深度炸弹：detail 只该放标识符，不该放数据体。"""
    payload: dict = {"k": "v"}
    for _ in range(1200):
        payload = {"nested": payload}
    with pytest.raises(AuditError, match="结构过大"):
        check_detail_safety(payload)


def test_log_rejects_unsafe_detail_before_writing(audit: AuditLogger, db_path: Path):
    """拦在写之前 —— 审计落盘就删不掉，写进去就永久在里面了。"""
    with pytest.raises(AuditError):
        audit.log("zhangsan", "segment_edit", "segment", "s1",
                  detail={"text": "同志们，今天我们召开党组会…"})
    assert audit.count() == 0, "被拒绝的写入不得留下任何记录"


def test_segment_edit_records_only_ids_and_lengths(audit: AuditLogger):
    """AUD-05 正向验证：segment_edit 的 detail 只含 id 与长度。"""
    rec = audit.segment_edit("zhangsan", MEETING_ID, "seg-0042",
                             fields=["text"], before_len=88, after_len=96,
                             reason="修正同音字")
    assert rec.detail["segment_id"] == "seg-0042"
    assert rec.detail["before_len"] == 88
    assert rec.detail["after_len"] == 96
    # 全文里不应出现任何像正文的东西
    assert "同志们" not in rec.detail_raw
    assert len(rec.detail_raw) < MAX_DETAIL_STRING_LEN * 3


# ---------------------------------------------------------------------------
# 动作登记
# ---------------------------------------------------------------------------

def test_action_whitelist_exactly_matches_construction_card():
    """回归锁：动作白名单一个不多一个不少。

    施工卡原文（T1-04）给的是 13 个。T1-08 新增了 `record_generate`
    —— 记录稿生成是 SAD §5.1 状态机 `reviewing -> record_ready` 的产物，
    它不给部署手册 §6.2 的最小核查项集新增**类别**
    （记录稿属"文稿"，与 `segment_edit` 同族），但必须单独留痕。

    T1-16 新增 `export_download` —— **这条与 `export` 是不同的动作**，
    所以不能并入：

      · `export`（T1-04 就有）记的是"**生成了**导出文件"；
      · `export_download` 记的是"**有人把导出文件取走了**"。

    为什么这个区分是必要的：部署手册 §6.2 的「导出记录」核查项，
    要回答的是"这份纪要**流向了谁**"。只记"生成过"答不了这个问题 ——
    文件静静躺在磁盘上和被人下载到 U 盘上，在审计上是**两件事**
    （这正是保密检查关心的那件）。所以 `export_download` 被加进
    `MINIMUM_CHECK_ITEM_ACTIONS["导出记录"]`。

    白名单的**纪律**没变：新增动作必须在 `infra/audit.py` 里显式登记、
    并同步更新本测试与文档 —— 这正是这条测试要守住的东西。
    （**本卡实测**：改 `ACTIONS` 时忘了同步这里，这条测试立刻红并
    指名报出 `export_download`。纪律有效。）
    """
    card_actions = {
        "login", "logout", "import_audio", "import_text",
        "transcribe_start", "transcribe_done",
        "segment_edit", "gate_decision", "export", "export_undo",
        "glossary_change", "template_change", "selfcheck",
    }
    #: T1-08 起新增的、经登记的扩展动作（每加一个都要在这里写明理由）
    sanctioned_extensions = {
        # T1-08：记录稿生成（SAD §5.1 `reviewing -> record_ready` 的产物）
        "record_generate",
        # T1-16：导出文件被**下载**（与 `export` 不同 —— 见 docstring；
        # 部署手册 §6.2「导出记录」要能回答"流向了谁"）
        "export_download",
    }

    assert ACTIONS == card_actions | sanctioned_extensions
    assert len(card_actions) == 13


def test_every_minimum_check_item_action_is_registered():
    """AUD-08 前置：8 类核查项引用的动作必须全部登记在 ACTIONS 里。"""
    for item, actions in MINIMUM_CHECK_ITEM_ACTIONS.items():
        assert actions & ACTIONS, f"核查项「{item}」引用了未登记的动作：{actions - ACTIONS}"


def test_unregistered_action_rejected(audit: AuditLogger):
    with pytest.raises(AuditError, match="未登记的审计动作"):
        audit.log("zhangsan", "随便编的动作")  # type: ignore[arg-type]
    with pytest.raises(AuditError, match="未登记的审计动作"):
        audit.log("zhangsan", "LOGIN")   # 大小写敏感，不能靠"看起来差不多"蒙过去


def test_invalid_result_rejected(audit: AuditLogger):
    with pytest.raises(AuditError, match="result 只能是"):
        audit.log("zhangsan", "login", result="ok")
    with pytest.raises(AuditError, match="result 只能是"):
        audit.log("zhangsan", "login", result="SUCCESS")


def test_empty_operator_rejected(audit: AuditLogger):
    """审计的核心就是"谁做的"，operator 为空等于记录失效。"""
    with pytest.raises(AuditError, match="operator 不能为空"):
        audit.log("", "login")


def test_missing_table_gives_actionable_error(tmp_path: Path):
    """没建库时给可执行的提示，而不是裸的 sqlite3.OperationalError。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    db = empty / "no_such.db"
    # 建一个没有 audit_log 表的库
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE dummy(x)")
    conn.commit()
    conn.close()

    with pytest.raises(AuditError, match="python tools/migrate.py"):
        AuditLogger(db)


# ---------------------------------------------------------------------------
# 链的构造
# ---------------------------------------------------------------------------

def test_first_row_uses_genesis_hash(audit: AuditLogger):
    rec = audit.log("zhangsan", "login")
    assert rec.prev_hash == GENESIS_HASH
    assert rec.log_id == 1


def test_chain_links_consecutive_rows(audit: AuditLogger):
    """prev_hash 必须是**上一条的 row_hash**（施工卡「常见坑」第一条）。"""
    _seed(audit, 5)
    records = query(audit.db_path)
    assert len(records) == 5
    assert records[0].prev_hash == GENESIS_HASH
    for prev, cur in zip(records, records[1:]):
        assert cur.prev_hash == prev.row_hash, f"log_id={cur.log_id} 链断"

    assert verify_chain(audit.db_path).ok


def test_last_hash_advances_with_each_write(audit: AuditLogger):
    a = audit.log("zhangsan", "login")
    assert audit.last_hash() == a.row_hash
    b = audit.log("zhangsan", "logout")
    assert audit.last_hash() == b.row_hash
    assert b.prev_hash == a.row_hash


def test_last_hash_on_empty_table_is_genesis(audit: AuditLogger):
    assert audit.last_hash() == GENESIS_HASH
    assert audit.last_row() is None
    assert audit.count() == 0


def test_log_returns_record_with_queryable_fields(audit: AuditLogger):
    rec = audit.log("lisi", "import_audio", "meeting", MEETING_ID,
                    detail={"filename": "会议.m4a", "size_bytes": 1024})
    assert rec.log_id == 1
    assert rec.operator == "lisi"
    assert rec.action == "import_audio"
    assert rec.target_type == "meeting"
    assert rec.target_id == MEETING_ID
    assert rec.detail["filename"] == "会议.m4a"
    assert rec.result == "success"


def test_ts_is_utc_iso8601_second_precision(audit: AuditLogger):
    """定长 UTC ISO8601：字典序 == 时间序（检索依赖这一点）。"""
    rec = audit.log("zhangsan", "login")
    assert rec.ts.endswith("Z")
    assert len(rec.ts) == len("2026-09-27T10:00:00Z") == 20
    assert "." not in rec.ts, "微秒会破坏定长性，导致字符串比较排序出错"
    assert datetime.fromisoformat(rec.ts.replace("Z", "+00:00"))


def test_ts_is_monotonic_across_rapid_writes(audit: AuditLogger):
    """同一秒内连写多条：ts 可以相同，但链必须仍然严格递进。"""
    for _ in range(20):
        audit.log("zhangsan", "selfcheck")
    assert verify_chain(audit.db_path).ok
    records = query(audit.db_path)
    assert [r.log_id for r in records] == list(range(1, 21))


# ---------------------------------------------------------------------------
# AUD-01 / AUD-02：触发器拦 UPDATE / DELETE
# ---------------------------------------------------------------------------

def test_update_rejected_by_trigger(audit: AuditLogger, db_path: Path):
    """AUD-01 / 验收标准 2：`UPDATE audit_log SET action='x' WHERE log_id=1` 必须被拒。"""
    _seed(audit, 3)
    before = query(db_path)[0]

    with connect_ctx(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="audit_log is append-only"):
            conn.execute("UPDATE audit_log SET action='x' WHERE log_id=1")

    # 原值必须没变（"报错了但其实改了"是最坏的情况）
    after = query(db_path)[0]
    assert after.action == before.action == "selfcheck"
    assert after.row_hash == before.row_hash


def test_update_any_column_rejected(audit: AuditLogger, db_path: Path):
    """触发器是 BEFORE UPDATE ON audit_log，不区分列——逐列验证一遍。"""
    _seed(audit, 2)
    stmts = [
        "UPDATE audit_log SET ts='2000-01-01T00:00:00Z' WHERE log_id=1",
        "UPDATE audit_log SET operator='attacker' WHERE log_id=1",
        "UPDATE audit_log SET target_id='x' WHERE log_id=1",
        "UPDATE audit_log SET detail='{}' WHERE log_id=1",
        "UPDATE audit_log SET result='failure' WHERE log_id=1",
        "UPDATE audit_log SET prev_hash='0' WHERE log_id=2",
        "UPDATE audit_log SET row_hash='deadbeef' WHERE log_id=2",
    ]
    with connect_ctx(db_path) as conn:
        for stmt in stmts:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(stmt)
    assert verify_chain(db_path).ok


def test_bulk_update_rejected(audit: AuditLogger, db_path: Path):
    """不带 WHERE 的全表 UPDATE 也要被拦（触发器是逐行的）。"""
    _seed(audit, 4)
    with connect_ctx(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET operator='attacker'")
    assert audit.count() == 4
    assert verify_chain(db_path).ok


def test_delete_rejected_by_trigger(audit: AuditLogger, db_path: Path):
    """AUD-02：DELETE 必须被拒。"""
    _seed(audit, 3)
    with connect_ctx(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="audit_log is append-only"):
            conn.execute("DELETE FROM audit_log WHERE log_id=1")

    with connect_ctx(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_log")

    assert audit.count() == 3, "被拒绝的 DELETE 不得真的删掉数据"


def test_drop_table_is_NOT_blocked_documented_gap(audit: AuditLogger, db_path: Path):
    """**记录一个真实的防护缺口：`DROP TABLE` 绕过了 DELETE 触发器。**

    直觉上以为"删表也会走 DELETE 触发器"，实测**不会**——
    SQLite 的 `BEFORE DELETE` 触发器只在行级 DELETE 语句上触发，
    `DROP TABLE` 是 DDL，直接销毁表结构，不逐行删除。

    这条测的是"已知缺口存在"，不是"防护有效"。把它写进测试而不是
    悄悄删掉，是为了让缺口**留在代码库里可见**：
      · 它不改变一期结论——本系统的威胁模型是"绕过应用改 .db 文件字节"，
        而 DROP TABLE 留下的痕迹极其明显（整个表没了），
        `verify_chain` 会立刻以 "no such table" / 0 行 + 表结构缺失报错；
      · 但如果将来要做"无人值守运行期巡检"，就需要额外一条
        "audit_log 表与两个触发器是否存在"的结构性检查。
    """
    _seed(audit, 2)
    with connect_ctx(db_path) as conn:
        conn.execute("DROP TABLE audit_log")     # 不被触发器拦（已知缺口）

    # 表没了 => 后续任何审计写入必须显式失败，而不是静默丢失
    with pytest.raises(AuditError, match="python tools/migrate.py"):
        AuditLogger(db_path)

    # 且校验脚本必须报错（而不是"0 行，一切正常"这种危险的假阳性）
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        verify_chain(db_path)


def test_missing_table_is_detected_by_structure_check(db_path: Path):
    """补偿性检查：表结构被毁时，探测表与触发器是否还在。

    这是将来巡检脚本要用的形状——DROP TABLE 挡不住，就**事后能发现**。
    """
    _seed(AuditLogger(db_path), 2)

    def structure(db: Path) -> dict[str, list[str]]:
        with connect_ctx(db, readonly=True) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
            )]
            triggers = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'"
            )]
        return {"tables": tables, "triggers": triggers}

    before = structure(db_path)
    assert before["tables"] == ["audit_log"]
    assert sorted(before["triggers"]) == ["audit_log_no_delete", "audit_log_no_update"]

    with connect_ctx(db_path) as conn:
        conn.execute("DROP TABLE audit_log")

    after = structure(db_path)
    assert after["tables"] == []
    assert after["triggers"] == []


def test_insert_still_allowed_after_rejections(audit: AuditLogger, db_path: Path):
    """被拒的操作不应破坏后续写入（RAISE(ABORT) 只回滚该语句）。"""
    _seed(audit, 2)
    with connect_ctx(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET action='x' WHERE log_id=1")
    rec = audit.log("zhangsan", "logout")
    assert rec.log_id == 3
    assert verify_chain(db_path).ok


def test_triggers_exist_in_schema(db_path: Path):
    """两个触发器必须真的建出来了（部署手册 §5.1 检查项 5）。"""
    with connect_ctx(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='audit_log' ORDER BY name"
        ).fetchall()
    assert [r[0] for r in rows] == ["audit_log_no_delete", "audit_log_no_update"]


# ---------------------------------------------------------------------------
# AUD-03 / 验收标准 3：绕过触发器直接改文件
# ---------------------------------------------------------------------------

def _forge_row(db_path: Path, log_id: int, **fields: object) -> None:
    """模拟"绕过应用直接改 db 文件"的攻击。

    真实攻击是十六进制编辑器改文件；这里用 SQL 达到**同样的库状态**：
    先把触发器摘掉、改完、再把触发器装回去（模拟"触发器从未存在过"）。
    改完后的库与"用编辑器改过字节的库"在校验脚本眼里没有区别——
    因为校验脚本读的就是表里的字段值，不看是谁改的。
    """
    with connect_ctx(db_path) as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE audit_log SET {sets} WHERE log_id=?",
                     (*fields.values(), log_id))
    # 装回触发器，模拟"攻击者改完后原样恢复现场"
    with connect_ctx(db_path) as conn2:
        conn2.execute(
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        )


def test_file_level_tamper_is_located(audit: AuditLogger, db_path: Path):
    """AUD-03 / 验收标准 3：改文件后校验脚本必须定位到具体出错行。"""
    _seed(audit, 6)
    assert verify_chain(db_path).ok, "前置：原始链必须是好的"

    _forge_row(db_path, 3, action="export")     # 把第 3 行的动作改掉

    result = verify_chain(db_path)
    assert not result.ok
    assert result.tampered
    # 必须精确指到第 3 行
    assert result.first_bad_log_id == 3
    kinds = {p.kind for p in result.problems if p.log_id == 3}
    assert "row_hash_mismatch" in kinds
    assert len(result.problems) == 1, (
        "只应报被改的那一行；后续行的 prev_hash 指向的是当时真实存在的第 3 行，"
        f"不该被牵连（实际报了 {[(p.log_id, p.kind) for p in result.problems]}）"
    )


def test_tamper_of_detail_field_is_detected(audit: AuditLogger, db_path: Path):
    _seed(audit, 3)
    _forge_row(db_path, 2, detail='{"seq":999}')
    result = verify_chain(db_path)
    assert not result.ok
    assert result.first_bad_log_id == 2


def test_tamper_of_ts_is_detected(audit: AuditLogger, db_path: Path):
    """改时间戳（"把操作时间挪到下班后"是典型的掩盖手法）。"""
    _seed(audit, 3)
    _forge_row(db_path, 1, ts="2020-01-01T00:00:00Z")
    result = verify_chain(db_path)
    assert not result.ok
    assert result.first_bad_log_id == 1


def test_tamper_of_operator_is_detected(audit: AuditLogger, db_path: Path):
    """改操作人（"不是我干的"）。"""
    _seed(audit, 3)
    _forge_row(db_path, 2, operator="lisi")
    assert not verify_chain(db_path).ok


def test_row_substitution_detected_by_prev_hash(audit: AuditLogger, db_path: Path):
    """整行替换 + 重算 row_hash：本行自洽，但链断在 prev_hash 上。

    这是**只做单行哈希、不做链式哈希**会漏掉的攻击——本用例专门证明
    为什么必须有 prev_hash 这一环。
    """
    _seed(audit, 4)
    # 取第 1 行的内容，整体搬去覆盖第 2 行（连 row_hash 一起搬）
    # 这样第 2 行"自己跟自己自洽"，但它的 prev_hash 指向的是 GENESIS，
    # 与第 1 行的 row_hash 对不上。
    with connect_ctx(db_path) as conn:
        row1 = dict(conn.execute(
            "SELECT * FROM audit_log WHERE log_id=1").fetchone())

    with connect_ctx(db_path) as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute(
            "UPDATE audit_log SET ts=?, operator=?, action=?, target_type=?,"
            " target_id=?, detail=?, result=?, prev_hash=?, row_hash=?"
            " WHERE log_id=?",
            (row1["ts"], row1["operator"], row1["action"], row1["target_type"],
             row1["target_id"], row1["detail"], row1["result"],
             row1["prev_hash"], row1["row_hash"], 2),
        )
    with connect_ctx(db_path) as conn2:
        conn2.execute(
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        )

    result = verify_chain(db_path)
    assert not result.ok
    kinds = {p.kind for p in result.problems}
    assert "prev_hash_mismatch" in kinds, (
        "整行替换必须被 prev_hash 比对抓到 —— 这正是链式哈希的存在理由"
    )


def test_tamper_is_not_self_healing(audit: AuditLogger, db_path: Path):
    """改完再写新日志，坏行依然存在（新写入不会"冲掉"证据）。"""
    _seed(audit, 4)
    _forge_row(db_path, 2, action="export")
    audit.log("zhangsan", "logout")
    result = verify_chain(db_path)
    assert not result.ok
    assert result.first_bad_log_id == 2


def test_append_only_property_preserved_after_tamper_detection(audit: AuditLogger, db_path: Path):
    """校验是只读的：跑几次校验都不改变库状态。"""
    _seed(audit, 3)
    _forge_row(db_path, 2, action="export")
    h_before = [r.row_hash for r in query(db_path)]
    verify_chain(db_path)
    verify_chain(db_path)
    assert [r.row_hash for r in query(db_path)] == h_before


def test_verify_chain_missing_db_reports_problem(tmp_path: Path):
    result = verify_chain(tmp_path / "nope.db")
    assert not result.ok
    assert result.problems[0].kind == "db_missing"
    assert result.total_rows == 0


def test_verify_chain_empty_table_is_ok(audit: AuditLogger):
    """空库（还没用过）链是"完好"的，不是"损坏"的。"""
    result = verify_chain(audit.db_path)
    assert result.ok
    assert result.total_rows == 0
    assert result.checked_rows == 0
    assert result.head_hash is None


def test_verify_chain_rows_accepts_plain_dicts():
    """纯函数可脱离数据库测试（便于将来做流式校验大库）。"""
    rows = [
        {"log_id": 1, "ts": "T1", "operator": "a", "action": "login",
         "target_type": None, "target_id": None, "detail": "{}",
         "result": "success", "prev_hash": GENESIS_HASH,
         "row_hash": compute_row_hash(GENESIS_HASH, "T1", "a", "login", None, None, "{}", "success")},
    ]
    assert verify_chain_rows(rows) == []

    rows[0]["row_hash"] = "0" * 64
    problems = verify_chain_rows(rows)
    assert len(problems) == 1
    assert problems[0].kind == "row_hash_mismatch"


# ---------------------------------------------------------------------------
# 验收标准 4 / AUD-04：闸口决策留痕
# ---------------------------------------------------------------------------

def test_audit_records_gate_decision(audit: AuditLogger, db_path: Path):
    """**验收标准 4**：批准/驳回/编辑三个动作都留痕，detail 能看出改了什么。"""
    audit.gate_decision("wangwu", MEETING_ID, "approve",
                        gate="G2", from_node="awaiting_confirm", to_node="extracting")
    audit.gate_decision("wangwu", MEETING_ID, "reject",
                        gate="G2", from_node="awaiting_confirm", to_node="extracting",
                        comment="第 3 项议题依据不足，退回补充")
    audit.gate_decision("wangwu", MEETING_ID, "edit",
                        gate="G2", from_node="awaiting_confirm", to_node="extracting",
                        changed={"title": {"old": "（草）关于…", "new": "关于…"},
                                 "owner": {"old": "暂无", "new": "办公室"}},
                        comment="仅改表述，不需重跑")

    records = query(db_path, AuditQuery(action="gate_decision"))
    assert len(records) == 3
    decisions = [r.detail["decision"] for r in records]
    assert decisions == ["approve", "reject", "edit"]

    # detail 里能看出每个动作干了什么
    assert records[0].detail["gate"] == "G2"
    assert records[0].detail["to_node"] == "extracting"
    assert "依据不足" in records[1].detail["comment"]

    # edit 场景：detail 里能看到"改了哪几个字段" + 新旧值
    assert records[2].detail["changed_keys"] == ["owner", "title"]
    assert records[2].detail["changed"]["owner"]["new"] == "办公室"
    assert records[2].detail["changed"]["title"]["old"] == "（草）关于…"

    # 三条都必须链式完好
    assert verify_chain(db_path).ok


def test_gate_decision_timeout_records_auto_flag(audit: AuditLogger):
    """F4.6 超时自动拒绝必须可区分于人工决策（安全默认的审计依据）。"""
    rec = audit.gate_decision("system", MEETING_ID, "reject",
                              gate="G2", auto=True,
                              comment="超过 24 小时未确认，已自动退回")
    assert rec.detail["auto"] is True
    assert rec.detail["decision"] == "reject"
    assert rec.operator == "system"


def test_gate_decision_rejects_long_comment_regression(audit: AuditLogger):
    """注释过长会被拦——防止有人把整段纪要塞进 comment 绕过正文检查。"""
    with pytest.raises(AuditError, match="疑似正文"):
        audit.gate_decision("wangwu", MEETING_ID, "reject",
                            comment="同志们，" + "这次会议主要研究了三个方面的工作。" * 20)


def test_full_task_records_all_key_actions(audit: AuditLogger, db_path: Path):
    """AUD-04：完整跑一个任务的每个关键动作都要有记录。"""
    audit.log("zhangsan", "login", detail={"client": "desktop"})
    audit.log("zhangsan", "import_audio", "meeting", MEETING_ID,
              detail={"filename": "党组会.m4a", "size_bytes": 52_428_800})
    audit.log("zhangsan", "import_text", "meeting", MEETING_ID,
              detail={"filename": "纪要模板.docx"})
    audit.log("zhangsan", "transcribe_start", "meeting", MEETING_ID,
              detail={"engine": "funasr", "model": "paraformer-zh"})
    audit.log("zhangsan", "transcribe_done", "meeting", MEETING_ID,
              detail={"segment_count": 428, "elapsed_ms": 610_000})
    audit.segment_edit("zhangsan", MEETING_ID, "seg-0017",
                       fields=["text"], before_len=52, after_len=58)
    audit.gate_decision("wangwu", MEETING_ID, "approve", gate="G1")
    audit.export_event("zhangsan", MEETING_ID,
                       files=["export/会议纪要_v1.docx"], classification="内部")
    audit.log("zhangsan", "logout")

    actions = [r.action for r in query(db_path)]
    assert actions == [
        "login", "import_audio", "import_text",
        "transcribe_start", "transcribe_done", "segment_edit",
        "gate_decision", "export", "logout",
    ]
    assert verify_chain(db_path).ok


def test_export_undo_is_distinct_action(audit: AuditLogger):
    normal = audit.export_event("zhangsan", MEETING_ID, files=["a.docx"])
    undo = audit.export_event("zhangsan", MEETING_ID, files=["a.docx"], undo=True)
    assert normal.action == "export"
    assert undo.action == "export_undo"


def test_failure_result_is_recorded(audit: AuditLogger):
    """失败也要留痕——审计只记成功等于掩盖问题。"""
    rec = audit.log("zhangsan", "transcribe_start", "meeting", MEETING_ID,
                    detail={"engine": "funasr"}, result="failure")
    assert rec.result == "failure"
    assert query(audit.db_path, AuditQuery(result="failure"))[0].log_id == rec.log_id


# ---------------------------------------------------------------------------
# AUD-07：检索
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded(audit: AuditLogger) -> AuditLogger:
    base = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)

    def ts(sec: int) -> str:
        return (base + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")

    audit.log("zhangsan", "login", ts=ts(0))
    audit.log("zhangsan", "import_audio", "meeting", "m1", ts=ts(10))
    audit.log("lisi", "import_audio", "meeting", "m2", ts=ts(20))
    audit.log("lisi", "gate_decision", "meeting", "m1",
              detail={"decision": "approve"}, ts=ts(30))
    audit.log("wangwu", "export", "meeting", "m1", result="failure", ts=ts(40))
    return audit


def test_query_by_operator(seeded: AuditLogger):
    rows = query(seeded.db_path, AuditQuery(operator="lisi"))
    assert [r.action for r in rows] == ["import_audio", "gate_decision"]


def test_query_by_action(seeded: AuditLogger):
    rows = query(seeded.db_path, AuditQuery(action="import_audio"))
    assert [r.operator for r in rows] == ["zhangsan", "lisi"]


def test_query_by_target(seeded: AuditLogger):
    rows = query(seeded.db_path, AuditQuery(target_type="meeting", target_id="m1"))
    assert len(rows) == 3
    assert all(r.target_id == "m1" for r in rows)


def test_query_by_time_range_is_inclusive(seeded: AuditLogger):
    base = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)

    def ts(sec: int) -> str:
        return (base + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")

    rows = query(seeded.db_path, AuditQuery(from_ts=ts(10), to_ts=ts(30)))
    assert [r.log_id for r in rows] == [2, 3, 4]   # 闭区间


def test_query_by_result(seeded: AuditLogger):
    rows = query(seeded.db_path, AuditQuery(result="failure"))
    assert [r.action for r in rows] == ["export"]


def test_query_combines_conditions_with_and(seeded: AuditLogger):
    rows = query(seeded.db_path, AuditQuery(operator="lisi", action="import_audio"))
    assert len(rows) == 1
    assert rows[0].target_id == "m2"


def test_query_limit_offset(seeded: AuditLogger):
    all_rows = query(seeded.db_path)
    assert len(all_rows) == 5
    assert [r.log_id for r in query(seeded.db_path, AuditQuery(limit=2))] == [1, 2]
    assert [r.log_id for r in query(seeded.db_path, AuditQuery(limit=2, offset=2))] == [3, 4]
    # 只给 offset 也要工作（LIMIT -1 OFFSET n）
    assert [r.log_id for r in query(seeded.db_path, AuditQuery(offset=3))] == [4, 5]


def test_query_empty_result_is_empty_list_not_error(audit: AuditLogger):
    assert query(audit.db_path, AuditQuery(operator="nobody")) == []


def test_query_detail_is_parsed_to_dict(seeded: AuditLogger):
    rows = query(seeded.db_path, AuditQuery(action="gate_decision"))
    assert rows[0].detail == {"decision": "approve"}


def test_time_string_ordering_equals_time_ordering(seeded: AuditLogger):
    """回归锁：ts 定长是实现依赖，一旦有微秒就会破坏字符串比较。"""
    rows = query(seeded.db_path)
    ts_list = [r.ts for r in rows]
    assert ts_list == sorted(ts_list)
    assert len({len(t) for t in ts_list}) == 1


# ---------------------------------------------------------------------------
# AUD-06：导出报告
# ---------------------------------------------------------------------------

def test_build_report_contains_integrity(seeded: AuditLogger):
    """AUD-06：报告必须含完整性校验结论。"""
    base = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)

    def ts(sec: int) -> str:
        return (base + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")

    report = build_report(seeded.db_path, from_ts=ts(10), to_ts=ts(30))

    assert report["integrity"]["result"] == "pass"
    assert report["integrity"]["checked_rows"] == 5       # 完整性校验是全库的
    assert report["integrity"]["head_hash"]
    assert report["integrity"]["problems"] == []
    assert "action_required" not in report

    assert report["period"] == {"from": ts(10), "to": ts(30)}
    assert report["summary"]["total"] == 3                # 明细受时间过滤
    assert report["summary"]["operators"] == {"zhangsan": 1, "lisi": 2}
    assert report["summary"]["actions"]["import_audio"] == 2
    assert len(report["records"]) == 3
    assert report["records"][0]["log_id"] == 2


def test_build_report_flags_tampered_chain(seeded: AuditLogger):
    """完整性 fail 时报告必须带 action_required，把处置动作写进数据里。"""
    _forge_row(seeded.db_path, 3, action="export")
    report = build_report(seeded.db_path)
    assert report["integrity"]["result"] == "fail"
    assert report["integrity"]["problems"][0]["log_id"] == 3
    assert "不得自行修复" in report["action_required"]


def test_build_report_includes_minimum_check_items(seeded: AuditLogger):
    report = build_report(seeded.db_path)
    cov = report["minimum_check_items"]
    assert cov["登录记录"] == "ok"
    assert cov["音频导入记录"] == "ok"
    assert cov["闸口审批记录"] == "ok"
    assert cov["导出记录"] == "ok"
    assert cov["术语库/模板变更"] == "empty"     # 动作登记了但还没用过
    assert set(cov) == set(MINIMUM_CHECK_ITEM_ACTIONS)


def test_build_report_is_json_serializable(seeded: AuditLogger):
    """报告要能直接写文件/走 HTTP，必须 JSON 可序列化。"""
    report = build_report(seeded.db_path)
    text = json.dumps(report, ensure_ascii=False)
    assert json.loads(text) == report


def test_build_report_records_contain_no_transcript_text(seeded: AuditLogger):
    """AUD-05 的导出面验证：报告里不能出现正文。"""
    report = build_report(seeded.db_path)
    for rec in report["records"]:
        assert not ({"text", "content", "full_text"} & set(rec["detail"]))


def test_audit_minimum_coverage_states(seeded: AuditLogger):
    cov = audit_minimum_coverage(seeded.db_path)
    assert cov["登录记录"] == "ok"
    assert cov["转写记录"] == "empty"
    assert "missing" not in cov.values(), "本项目 8 类核查项的动作都应已登记"


def test_minimum_check_items_covered_after_full_run(seeded: AuditLogger, db_path: Path):
    """AUD-08：8 类必需动作全部覆盖后，状态应全为 ok。"""
    covered = [
        ("logout", None, None),
        ("import_text", "meeting", "m1"),
        ("transcribe_start", "meeting", "m1"),
        ("transcribe_done", "meeting", "m1"),
        ("segment_edit", "segment", "s1"),
        ("export_undo", "meeting", "m1"),
        ("glossary_change", "glossary", "g1"),
        ("template_change", "template", "t1"),
        ("selfcheck", "system", "run"),
    ]
    for action, tt, tid in covered:
        seeded.log("zhangsan", action, tt, tid)

    cov = audit_minimum_coverage(db_path)
    assert all(v == "ok" for v in cov.values()), cov


# ---------------------------------------------------------------------------
# 锁与并发
# ---------------------------------------------------------------------------

def test_concurrent_writes_preserve_chain(audit: AuditLogger, db_path: Path):
    """多线程并发写不得产生分叉链（施工卡：单机单任务用 threading.Lock）。"""
    import threading

    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(8):
                audit.log(f"user{tid}", "selfcheck", detail={"tid": tid, "i": i})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入报错：{errors}"
    assert audit.count() == 48
    result = verify_chain(db_path)
    assert result.ok, result.summary()

    # prev_hash 必须构成 1 -> 2 -> ... -> 48 的单链，不能有重号
    records = query(db_path)
    prevs = [r.prev_hash for r in records]
    assert prevs[0] == GENESIS_HASH
    assert len(set(prevs)) == len(prevs), "出现两条记录共用同一 prev_hash = 链分叉"


def test_separate_logger_instances_share_chain(db_path: Path):
    """两个 AuditLogger 实例（模拟 API 进程 + 后台巡检）写同一库，链仍连续。"""
    a1 = AuditLogger(db_path)
    a2 = AuditLogger(db_path)
    r1 = a1.log("zhangsan", "login")
    r2 = a2.log("zhangsan", "selfcheck")
    r3 = a1.log("zhangsan", "logout")
    assert r2.prev_hash == r1.row_hash
    assert r3.prev_hash == r2.row_hash
    assert verify_chain(db_path).ok


# ---------------------------------------------------------------------------
# 批量写入
# ---------------------------------------------------------------------------

def test_log_many_is_atomic_and_chained(audit: AuditLogger, db_path: Path):
    recs = audit.log_many([
        {"operator": "wangwu", "action": "gate_decision", "target_type": "meeting",
         "target_id": MEETING_ID, "detail": {"decision": "approve"}},
        {"operator": "wangwu", "action": "export", "target_type": "meeting",
         "target_id": MEETING_ID, "detail": {"files": ["a.docx"]}},
    ])
    assert [r.log_id for r in recs] == [1, 2]
    assert recs[1].prev_hash == recs[0].row_hash
    assert verify_chain(db_path).ok


def test_log_many_validates_all_before_writing(audit: AuditLogger):
    """批量写入先全校验 —— 不能"写了一半才发现第三条动作非法"。"""
    with pytest.raises(AuditError, match="未登记的审计动作"):
        audit.log_many([
            {"operator": "a", "action": "login"},
            {"operator": "a", "action": "编造的"},
        ])
    assert audit.count() == 0


def test_log_many_rejects_unsafe_detail_atomically(audit: AuditLogger):
    with pytest.raises(AuditError, match="禁止的键名"):
        audit.log_many([
            {"operator": "a", "action": "login"},
            {"operator": "a", "action": "segment_edit", "detail": {"text": "正文"}},
        ])
    assert audit.count() == 0


def test_log_many_empty_is_noop(audit: AuditLogger):
    assert audit.log_many([]) == []
    assert audit.count() == 0


# ---------------------------------------------------------------------------
# 保留期
# ---------------------------------------------------------------------------

def test_prune_expired_lists_only_and_never_deletes(audit: AuditLogger, db_path: Path):
    """保留期清理只列不删——删除必须走独立归档流程（触发器在那儿拦着）。"""
    old = (datetime.now(timezone.utc) - timedelta(days=2000)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    new = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    audit.log("zhangsan", "login", ts=old)
    audit.log("zhangsan", "logout", ts=new)

    expired = prune_expired(db_path, retention_days=1095)
    assert expired == [1]
    assert audit.count() == 2, "dry-run 不得删除任何记录"
    assert verify_chain(db_path).ok


def test_prune_expired_requires_positive_days(audit: AuditLogger):
    with pytest.raises(AuditError, match="必须 > 0"):
        prune_expired(audit.db_path, retention_days=0)
    with pytest.raises(AuditError, match="必须 > 0"):
        prune_expired(audit.db_path, retention_days=-1)


def test_prune_expired_nothing_to_do(audit: AuditLogger):
    audit.log("zhangsan", "login")
    assert prune_expired(audit.db_path, retention_days=1095) == []


# ---------------------------------------------------------------------------
# 只读校验不写库
# ---------------------------------------------------------------------------

def test_verification_uses_readonly_connection(audit: AuditLogger, db_path: Path):
    """校验程序不得有写权限（否则它自己成了篡改入口）。"""
    _seed(audit, 3)
    mtime_before = db_path.stat().st_mtime_ns
    verify_chain(db_path)
    build_report(db_path)
    audit_minimum_coverage(db_path)
    assert db_path.stat().st_mtime_ns == mtime_before


def test_readonly_connection_cannot_write(db_path: Path):
    _seed(AuditLogger(db_path), 1)
    with connect_ctx(db_path, readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO audit_log (ts, operator, action, result, row_hash)"
                         " VALUES ('T','x','login','success','h')")


def test_connection_keeps_foreign_keys_on_for_audit_writes(db_path: Path):
    """回归：审计写入走的仍是 db.connect()，PRAGMA 不能被绕过。"""
    with connect_ctx(db_path) as conn:
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_audit_writes_do_not_break_wal_checkpoint(db_path: Path):
    """写入后库文件仍是 WAL 模式（备份脚本依赖这一点，见部署手册 §5.2）。"""
    a = AuditLogger(db_path)
    _seed(a, 3)
    with connect(db_path) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
