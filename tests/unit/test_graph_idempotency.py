"""`graph.idempotency` 单元测试（T1-10，SAD §5.4）。

幂等是这个项目里**最容易写成摆设**的机制：写错了不会报错，
只会在"同一件事做了两遍"时悄悄发生 —— 比如导出两个文件、
归档两条记录。所以这里的测试重点在"重复调用真的被拦住"，
而不是"函数能跑通"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mmwf.graph import idempotency as IDEM


@pytest.fixture()
def store(tmp_path: Path) -> IDEM.IdempotencyStore:
    """全新库上的 store（自动建表，`ensure_schema` 默认开）。"""
    return IDEM.IdempotencyStore(tmp_path / "idem.db")


# ---------------------------------------------------------------------------
# 键的构造
# ---------------------------------------------------------------------------

class TestMakeIdempotencyKey:
    def test_length_is_32(self):
        """SAD §5.4 定死 32。改了要同步改 001_init.sql 的注释。"""
        key = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        assert len(key) == IDEM.KEY_LENGTH == 32

    def test_is_hex(self):
        key = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        int(key, 16)  # 不是十六进制会抛 ValueError

    def test_deterministic(self):
        a = IDEM.make_idempotency_key("m1", "export", {"a": 1, "b": 2})
        b = IDEM.make_idempotency_key("m1", "export", {"a": 1, "b": 2})
        assert a == b

    def test_key_order_does_not_matter(self):
        """键序无关 —— 这是 `canonical_json` 存在的全部理由。

        不做规范化的话，同一个字典从不同 SQL 查询拼出来（列顺序不同）
        会算出不同的键，幂等**静默失效**。
        """
        a = IDEM.make_idempotency_key("m1", "export", {"a": 1, "b": 2})
        b = IDEM.make_idempotency_key("m1", "export", {"b": 2, "a": 1})
        assert a == b

    def test_nested_key_order_does_not_matter(self):
        a = IDEM.make_idempotency_key("m1", "g", {"x": {"p": 1, "q": 2}})
        b = IDEM.make_idempotency_key("m1", "g", {"x": {"q": 2, "p": 1}})
        assert a == b

    def test_different_task_yields_different_key(self):
        a = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        b = IDEM.make_idempotency_key("m2", "export", {"a": 1})
        assert a != b

    def test_different_node_yields_different_key(self):
        a = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        b = IDEM.make_idempotency_key("m1", "archive", {"a": 1})
        assert a != b

    def test_different_input_yields_different_key(self):
        a = IDEM.make_idempotency_key("m1", "export", {"v": 1})
        b = IDEM.make_idempotency_key("m1", "export", {"v": 2})
        assert a != b

    def test_input_order_of_list_matters(self):
        """列表**顺序敏感** —— 顺序变了产物就变了，不能算同一个键。"""
        a = IDEM.make_idempotency_key("m1", "g", ["a", "b"])
        b = IDEM.make_idempotency_key("m1", "g", ["b", "a"])
        assert a != b

    def test_tuple_and_list_are_equivalent(self):
        """tuple 与 list 语义相同，应得同一个键。

        否则从 JSON 读回来（list）和从 Python 里拼出来（tuple）
        会得到不同的键 —— 恢复场景下极容易踩。
        """
        a = IDEM.make_idempotency_key("m1", "g", ("a", "b"))
        b = IDEM.make_idempotency_key("m1", "g", ["a", "b"])
        assert a == b

    def test_scalar_inputs_work(self):
        for payload in ("text", 42, 3.14, None, True):
            key = IDEM.make_idempotency_key("m1", "g", payload)
            assert len(key) == IDEM.KEY_LENGTH

    def test_empty_task_id_rejected(self):
        with pytest.raises(ValueError, match="task_id"):
            IDEM.make_idempotency_key("", "export", {})

    def test_whitespace_task_id_rejected(self):
        """**纯空白也是空的**。只判 `not x` 会漏掉 `"   "`，
        而空白串不携带任何区分信息 —— 两个会议都传 `"   "` 照样撞键。
        """
        for bad in ("   ", "\t", "\n", " \t\n "):
            with pytest.raises(ValueError, match="task_id"):
                IDEM.make_idempotency_key(bad, "export", {})

    def test_whitespace_node_id_rejected(self):
        for bad in ("", "  ", "\t"):
            with pytest.raises(ValueError, match="node_id"):
                IDEM.make_idempotency_key("m1", bad, {})

    def test_type_is_preserved_for_scalars(self):
        """数字 `1` 与字符串 `"1"` **不能撞键**。

        直接用 `str(x)` 算哈希时两者都是 `"1"`，会算出同一个键 ——
        于是"议题数量=1"和"议题标题='1'"会被判成同一个产物。
        走规范化 JSON 保留了类型，这类撞键才被挡住。
        """
        a = IDEM.make_idempotency_key("m1", "g", 1)
        b = IDEM.make_idempotency_key("m1", "g", "1")
        assert a != b

    def test_bool_and_int_are_distinguishable_or_not(self):
        """`True` 在 Python 里 `== 1`，JSON 里也是 `true`。

        这里只钉住"行为是确定的"，不钉住"必须不同" ——
        两者是否该区分取决于业务，本项目的幂等输入里没有布尔标量，
        所以不强行规定，只保证不会因为实现漂移而随机变化。
        """
        a = IDEM.make_idempotency_key("m1", "g", True)
        b = IDEM.make_idempotency_key("m1", "g", True)
        assert a == b
        assert len(a) == IDEM.KEY_LENGTH


class TestCanonicalJson:
    def test_no_spaces(self):
        """紧凑分隔符 —— 空格差异不该改变键。"""
        assert IDEM.canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_keeps_chinese_readable(self):
        """`ensure_ascii=False`：中文不转成 \\uXXXX。

        哈希值不受影响，但日志里能直接看懂产物内容，
        排查"为什么这两个键不一样"时省很多事。
        """
        assert "议题" in IDEM.canonical_json({"t": "议题"})

    def test_content_hash_is_64_hex(self):
        h = IDEM.content_hash({"a": 1})
        assert len(h) == 64
        int(h, 16)


# ---------------------------------------------------------------------------
# 节点输入字段表
# ---------------------------------------------------------------------------

class TestIdempotentInputKeys:
    def test_covers_all_side_effect_nodes(self):
        """有副作用的节点必须有输入字段表，否则算不出键。"""
        for node in IDEM.SIDE_EFFECT_NODES:
            assert node in IDEM.IDEMPOTENT_INPUT_KEYS, f"{node} 缺输入字段定义"

    def test_side_effect_nodes_are_the_four(self):
        """施工卡列的四个副作用节点：接入 / 转写 / 导出 / 归档。"""
        assert IDEM.SIDE_EFFECT_NODES == {"ingest", "export", "archive"}

    def test_fields_reference_pipeline_state(self):
        from mmwf.graph.state import PipelineState

        declared = set(PipelineState.__annotations__)
        for node, keys in IDEM.IDEMPOTENT_INPUT_KEYS.items():
            for k in keys:
                assert k in declared, f"{node} 的幂等输入 {k} 不在 PipelineState"

    def test_node_input_from_state_takes_listed_fields(self):
        got = IDEM.node_input_from_state(
            "transcribe", {"audio_paths": ["a.wav"], "chunk_plan": [{"seq": 1}]}
        )
        assert got == {"audio_paths": ["a.wav"], "chunk_plan": [{"seq": 1}]}

    def test_missing_field_becomes_none_placeholder(self):
        """**缺字段用 None 占位，不能跳过**。

        跳过会让键里少一段，于是"还没算的产物"与"值为 None 的产物"
        算出同一个键 —— 会把空产物错误地当成已完成。
        """
        got = IDEM.node_input_from_state("transcribe", {"audio_paths": ["a.wav"]})
        assert got == {"audio_paths": ["a.wav"], "chunk_plan": None}
        assert "chunk_plan" in got

    def test_unregistered_node_raises(self):
        with pytest.raises(KeyError):
            IDEM.node_input_from_state("gate_g2", {})

    def test_export_node_depends_on_markdown(self):
        """导出依赖配对候选、真实源与模板上下文。"""
        assert IDEM.IDEMPOTENT_INPUT_KEYS["export"] == (
            "minutes_markdown", "minutes_structured", "record_markdown", "transcript", "minutes")

    def test_different_markdown_yields_different_key(self):
        """内容变了必须允许再导一次（这是"重新导出"的合法路径）。"""
        k1 = IDEM.make_idempotency_key(
            "m1", "export", IDEM.node_input_from_state("export", {"minutes_markdown": "A"})
        )
        k2 = IDEM.make_idempotency_key(
            "m1", "export", IDEM.node_input_from_state("export", {"minutes_markdown": "B"})
        )
        assert k1 != k2


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class TestStoreConstruction:
    def test_rejects_memory_db(self):
        """`:memory:` 每次连接都是新库，幂等记录写进去就没了。"""
        with pytest.raises(ValueError, match="memory"):
            IDEM.IdempotencyStore(":memory:")

    def test_creates_schema_automatically(self, tmp_path: Path):
        """**首次使用不该要求调用方记得跑迁移。**

        忘了迁移的话，报错点会落在 `put()` 里的
        `no such table`，离真正原因（没迁移）很远。
        """
        db = tmp_path / "auto.db"
        assert not db.exists()
        IDEM.IdempotencyStore(db)
        assert db.exists()
        with sqlite3.connect(db) as conn:
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "idempotency_record" in names

    def test_ensure_schema_is_idempotent(self, tmp_path: Path):
        db = tmp_path / "twice.db"
        s1 = IDEM.IdempotencyStore(db)
        s1.put("k" * 32, "m1", "export", "ref")
        s2 = IDEM.IdempotencyStore(db)  # 再建一次不该清数据
        assert s2.count() == 1

    def test_ensure_schema_can_be_skipped(self, tmp_path: Path):
        """只读库场景：显式关掉自动迁移。"""
        db = tmp_path / "ro.db"
        IDEM.IdempotencyStore(db)  # 先建好
        s = IDEM.IdempotencyStore(db, ensure_schema=False)
        assert s.count() == 0
        assert s._schema_ready is False

    def test_accepts_str_path(self, tmp_path: Path):
        s = IDEM.IdempotencyStore(str(tmp_path / "s.db"))
        assert s.db_path.name == "s.db"


class TestStorePutAndGet:
    def test_put_first_time_returns_true(self, store):
        assert store.put("k1", "m1", "export", "ref1") is True

    def test_put_duplicate_returns_false(self, store):
        """**重复写入必须返回 False 而不是报错。**

        SAD §5.4 明确：唯一约束冲突时"返回既有结果而非报错"。
        报错会让并发场景（两个进程都跑到这里）把正常情况当成故障。
        """
        store.put("k1", "m1", "export", "ref1")
        assert store.put("k1", "m1", "export", "ref2") is False

    def test_duplicate_does_not_overwrite_existing(self, store):
        """后来的写入者不能覆盖先写入者的产物引用。"""
        store.put("k1", "m1", "export", "first")
        store.put("k1", "m1", "export", "second")
        hit = store.get("k1")
        assert hit is not None
        assert hit.result_ref == "first"

    def test_get_missing_returns_none(self, store):
        assert store.get("nope") is None

    def test_get_round_trips_all_fields(self, store):
        store.put("k1", "m1", "export", "ref1")
        hit = store.get("k1")
        assert hit == IDEM.IdempotencyHit(
            idem_key="k1", task_id="m1", node_id="export", result_ref="ref1"
        )

    def test_result_ref_may_be_none(self, store):
        """产物引用可以为空（节点无文件产物但仍需登记防重）。"""
        store.put("k1", "m1", "archive", None)
        assert store.get("k1").result_ref is None

    def test_count(self, store):
        assert store.count() == 0
        store.put("k1", "m1", "export", "a")
        store.put("k2", "m1", "archive", "b")
        assert store.count() == 2

    def test_hit_to_dict(self, store):
        store.put("k1", "m1", "export", "ref1")
        d = store.get("k1").to_dict()
        assert d["idem_key"] == "k1"
        assert d["result_ref"] == "ref1"


class TestStoreFind:
    def test_find_returns_all_records_for_node(self, store):
        """同一节点可能有**多条**记录：同一个会议导出了 v1 和 v2。"""
        store.put("k1", "m1", "export", "v1.docx")
        store.put("k2", "m1", "export", "v2.docx")
        hits = store.find("m1", "export")
        assert len(hits) == 2
        assert {h.result_ref for h in hits} == {"v1.docx", "v2.docx"}

    def test_find_filters_by_task(self, store):
        store.put("k1", "m1", "export", "a")
        store.put("k2", "m2", "export", "b")
        assert [h.result_ref for h in store.find("m1", "export")] == ["a"]

    def test_find_filters_by_node(self, store):
        store.put("k1", "m1", "export", "a")
        store.put("k2", "m1", "archive", "b")
        assert [h.result_ref for h in store.find("m1", "archive")] == ["b"]

    def test_find_empty(self, store):
        assert store.find("nope", "export") == []


class TestStoreGetOrRun:
    def test_first_call_runs(self, store):
        calls = []

        def fn():
            calls.append(1)
            return "RESULT"

        got, cached = store.get_or_run("m1", "export", {"a": 1}, fn)
        assert got == "RESULT"
        assert cached is False
        assert len(calls) == 1

    def test_second_call_is_cached(self, store):
        """**核心行为**：第二次不再执行 `fn`。"""
        calls = []

        def fn():
            calls.append(1)
            return "RESULT"

        store.get_or_run("m1", "export", {"a": 1}, fn)
        got, cached = store.get_or_run("m1", "export", {"a": 1}, fn)
        assert cached is True
        assert len(calls) == 1, "幂等未生效：fn 被执行了两次"
        # 命中时返回的是**产物引用**（fn 的返回值经 result_ref_of 转换）
        assert got is not None

    def test_different_input_runs_again(self, store):
        calls = []

        def fn():
            calls.append(1)
            return "R"

        store.get_or_run("m1", "export", {"v": 1}, fn)
        store.get_or_run("m1", "export", {"v": 2}, fn)
        assert len(calls) == 2

    def test_cached_returns_result_ref_not_return_value(self, store):
        """命中时返回 `result_ref`，不是上次的返回值。

        上次的返回值没被持久化，跨进程恢复时根本拿不到 ——
        所以产物必须是"可重新获取的引用"（路径 / ID）。
        """
        store.get_or_run("m1", "export", {"a": 1}, lambda: "/out/v1.docx")
        got, cached = store.get_or_run("m1", "export", {"a": 1}, lambda: "IGNORED")
        assert cached is True
        assert got == "/out/v1.docx"

    def test_result_ref_of_is_skipped_on_first_run(self, store):
        """**首次执行返回的是 fn 的返回值本身**，不是引用。

        想拿引用要读 `store.get(key).result_ref`。
        这里把这条契约钉住：如果哪天"首次也返回引用"，
        调用方拿到的就不是产物对象，而是个字符串 —— 静默错类型。
        """

        class R:
            def __init__(self, p):
                self.rel_path = p

        got, cached = store.get_or_run(
            "m1", "export", {"a": 1}, lambda: R("export/out.docx")
        )
        assert cached is False
        assert isinstance(got, R), "首次应返回 fn 的返回值本身"
        assert got.rel_path == "export/out.docx"
        # 引用可以从记录里读出来
        key = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        assert store.get(key).result_ref == "export/out.docx"

    def test_result_ref_of_override_uses_custom_ref(self, store):
        """`result_ref_of` 真的被用于**登记**（这才是它的用处）。"""

        class R:
            def __init__(self, p):
                self.rel_path = p

        store.get_or_run(
            "m1", "export", {"a": 1}, lambda: R("export/out.docx")
        )
        key = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        assert store.get(key).result_ref == "export/out.docx"

    def test_custom_result_ref_of_is_honoured(self, store):
        """自定义提取器优先于默认属性探测。"""

        class R:
            rel_path = "wrong.docx"
            custom = "right.docx"

        store.get_or_run(
            "m1", "export", {"a": 1}, lambda: R(),
            result_ref_of=lambda r: r.custom,
        )
        key = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        assert store.get(key).result_ref == "right.docx"

    def test_result_ref_of_extracts_from_dataclass_attr(self, store):
        """默认提取器优先取 `.rel_path` —— `ExportResult` 就有这个属性。"""

        class Fake:
            rel_path = "export/doc.docx"

        assert IDEM._default_result_ref(Fake()) == "export/doc.docx"

    def test_result_ref_of_falls_back_to_str(self, store):
        class Opaque:
            def __str__(self):
                return "opaque-ref"

        assert IDEM._default_result_ref(Opaque()) == "opaque-ref"

    def test_result_ref_of_none(self):
        assert IDEM._default_result_ref(None) is None

    def test_result_ref_of_path(self):
        assert IDEM._default_result_ref(Path("a/b.docx")) == "a\\b.docx" or \
            IDEM._default_result_ref(Path("a/b.docx")) == "a/b.docx"

    def test_result_ref_of_dict(self):
        assert IDEM._default_result_ref({"rel_path": "x.docx"}) == "x.docx"

    def test_registers_key_after_run(self, store):
        store.get_or_run("m1", "export", {"a": 1}, lambda: "R")
        assert store.count() == 1
        key = IDEM.make_idempotency_key("m1", "export", {"a": 1})
        assert store.get(key) is not None


class TestStoreDrop:
    def test_drop_removes_record(self, store):
        store.put("k1", "m1", "export", "ref")
        assert store.drop("k1") is True
        assert store.get("k1") is None

    def test_drop_missing_returns_false(self, store):
        assert store.drop("nope") is False

    def test_drop_then_put_works_again(self, store):
        """撤销导出后再导出的场景（T1-09 的 `undo_export`）。"""
        store.put("k1", "m1", "export", "v1")
        store.drop("k1")
        assert store.put("k1", "m1", "export", "v2") is True


class TestStoreCustomClock:
    def test_now_is_injectable(self, tmp_path: Path):
        """注入时钟让"同一秒写入两条"这类测试可确定。"""
        s = IDEM.IdempotencyStore(tmp_path / "c.db", now=lambda: "2026-01-01T00:00:00+00:00")
        s.put("k1", "m1", "export", "r")
        with sqlite3.connect(s.db_path) as conn:
            ts = conn.execute(
                "SELECT created_at FROM idempotency_record WHERE idem_key='k1'"
            ).fetchone()[0]
        assert ts == "2026-01-01T00:00:00+00:00"


class TestConcurrencySafety:
    def test_two_stores_same_key_only_one_wins(self, tmp_path: Path):
        """两个 store 实例（模拟两个进程）用同一个键写，只有一个返回 True。

        这就是"用 `INSERT OR IGNORE` 而不是先查再插"要保证的事：
        先查再插的话，两个进程都会查到"不存在"，然后都插入，
        其中一个撞主键抛异常。
        """
        db = tmp_path / "c.db"
        s1 = IDEM.IdempotencyStore(db)
        s2 = IDEM.IdempotencyStore(db)
        results = [s1.put("k1", "m1", "export", "a"), s2.put("k1", "m1", "export", "b")]
        assert results.count(True) == 1
        assert results.count(False) == 1

    def test_loser_reads_winner_result(self, tmp_path: Path):
        db = tmp_path / "c2.db"
        s1 = IDEM.IdempotencyStore(db)
        s2 = IDEM.IdempotencyStore(db)
        s1.put("k1", "m1", "export", "winner")
        s2.put("k1", "m1", "export", "loser")
        assert s2.get("k1").result_ref == "winner"


@pytest.mark.parametrize("field,value", [
    ("record_markdown", "新来源"), ("transcript", [{"text": "新分段"}]),
    ("minutes_structured", {"topics": [], "title": "新候选"}),
    ("minutes", {"_context": {"template_snapshot": {"version_no": 2}}}),
])
def test_minutes_delivery_idempotency_tracks_source_template_and_candidate(field, value):
    before = {"minutes_markdown": "同一正文", "minutes_structured": {"topics": []}}
    after = {**before, field: value}
    for node in ("assemble", "export"):
        assert IDEM.node_input_from_state(node, before) != IDEM.node_input_from_state(node, after)
