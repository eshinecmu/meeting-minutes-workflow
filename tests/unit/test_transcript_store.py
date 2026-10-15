"""`infra/transcript_store.py` 单测（T1-14）。

本卡只铺 C 档地基、不接端点，所以这里测的是**仓储语义**，
不是 HTTP 契约。四类：

  ① 读写往返（含 `confidence=None` 与 `flags` 的保真）
  ② 就地编辑（版本递增、空文本拒绝、无变化仍留痕）
  ③ 批量接受（**两条判据**：分数 + flags —— 这是最容易写错的一处）
  ④ 审计留痕（`segment_edit` 必须出现，且 detail 不含正文）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mmwf.infra.audit import AuditLogger, AuditQuery, query
from mmwf.infra.migrations.runner import migrate
from mmwf.infra.transcript_store import (
    ACCEPT_MIN_CONFIDENCE,
    DEFAULT_LOW_CONFIDENCE,
    SegmentRow,
    TranscriptStore,
    make_transcript_store,
    _dump_flags,
    _parse_flags,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "mmwf.db"
    migrate(p)
    _insert_meeting(p, "m-1")
    return p


def _insert_meeting(db: Path, meeting_id: str, title: str = "测试会议") -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO meeting (meeting_id, title, status, classification,"
            " created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (meeting_id, title, "created", "internal", "sys",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def store(db: Path) -> TranscriptStore:
    return make_transcript_store("m-1", db, audit=AuditLogger(db))


def _mk(store: TranscriptStore, **overrides):
    """建一段并返回它。默认给一个高置信、无 flag 的段。"""
    base = {
        "seq": 1, "start_ms": 0, "end_ms": 1000,
        "text": "会议认为", "confidence": 0.95,
    }
    base.update(overrides)
    store.replace_segments([base])
    return store.list_segments()[0]


# ---------------------------------------------------------------------------
# ① flags 序列化
# ---------------------------------------------------------------------------

class TestFlagsSerialization:
    def test_none_and_empty_become_null(self):
        assert _dump_flags(None) is None
        assert _dump_flags([]) is None
        # 全是空白项也当空 —— 空白 flag 会匹配一切，是最危险的脏数据
        assert _dump_flags(["", "   "]) is None

    def test_chinese_flags_are_not_escaped(self):
        """`ensure_ascii=True` 会把中文写成 \\uXXXX，界面直接乱码。"""
        raw = _dump_flags(["静音"])
        assert raw is not None
        assert "静音" in raw
        assert "\\u" not in raw

    def test_parse_roundtrip(self):
        assert _parse_flags(_dump_flags(["a", "b"])) == ("a", "b")

    def test_parse_tolerates_bare_string(self):
        """有人手工把 `["low_conf"]` 存成了 `"low_conf"`。"""
        assert _parse_flags('"low_conf"') == ("low_conf",)

    @pytest.mark.parametrize("bad", ["{bad json", "[1,", "not json at all"])
    def test_parse_bad_json_returns_empty_not_raise(self, bad):
        """一行坏数据不该让整个复核页打不开。"""
        assert _parse_flags(bad) == ()

    def test_parse_non_array_returns_empty(self):
        assert _parse_flags('{"a": 1}') == ()
        assert _parse_flags("42") == ()

    def test_parse_empty_returns_empty(self):
        assert _parse_flags(None) == ()
        assert _parse_flags("") == ()


# ---------------------------------------------------------------------------
# ② 读写往返
# ---------------------------------------------------------------------------

class TestReadWrite:
    def test_replace_and_list(self, store: TranscriptStore):
        n = store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 1000, "text": "甲", "confidence": 0.9},
            {"seq": 2, "start_ms": 1000, "end_ms": 2000, "text": "乙", "confidence": 0.5},
        ])
        assert n == 2
        assert store.count() == 2
        rows = store.list_segments()
        assert [r.seq for r in rows] == [1, 2]
        assert [r.text for r in rows] == ["甲", "乙"]

    def test_replace_is_full_swap_not_append(self, store: TranscriptStore):
        """重跑转写必须是替换 —— 追加会让新旧段 seq 撞车。"""
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "旧", "confidence": 0.9},
        ])
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "新", "confidence": 0.9},
        ])
        assert store.count() == 1
        assert store.list_segments()[0].text == "新"

    def test_segment_id_is_deterministic(self, store: TranscriptStore):
        """同一 seq 重跑后 id 不变，指向它的引用不会悬空。"""
        store.replace_segments([
            {"seq": 7, "start_ms": 0, "end_ms": 100, "text": "x", "confidence": 0.9},
        ])
        first = store.list_segments()[0].segment_id
        store.replace_segments([
            {"seq": 7, "start_ms": 0, "end_ms": 100, "text": "y", "confidence": 0.9},
        ])
        assert store.list_segments()[0].segment_id == first

    def test_confidence_none_is_preserved(self, store: TranscriptStore):
        """`None` 表示引擎没给，**不能**被填成 0。"""
        row = _mk(store, confidence=None)
        assert row.confidence is None

    def test_confidence_zero_is_not_none(self, store: TranscriptStore):
        """0.0 与 None 是两件事，别用 `or` 把 0 吞成 None。"""
        row = _mk(store, confidence=0.0)
        assert row.confidence == 0.0
        assert row.confidence is not None

    def test_no_confidence_gets_no_low_conf_flag(self, store: TranscriptStore):
        """没给置信度 ≠ 低置信。混为一谈会让整篇被标红、失去提示意义。"""
        row = _mk(store, confidence=None)
        assert "low_conf" not in row.review_flags

    def test_flags_roundtrip(self, store: TranscriptStore):
        row = _mk(store, flags=["overlap", "silence"])
        assert row.flags == ("overlap", "silence")

    def test_duration_ms(self, store: TranscriptStore):
        row = _mk(store, start_ms=1000, end_ms=3500)
        assert row.duration_ms == 2500

    def test_get_segment(self, store: TranscriptStore):
        row = _mk(store)
        assert store.get_segment(row.segment_id) is not None
        assert store.get_segment("nope") is None

    def test_get_segment_scoped_to_meeting(self, db: Path):
        """⚠️ 必须校验 meeting_id —— 否则换个会议 id 能读到别人的分段。"""
        _insert_meeting(db, "m-2")
        store_a = make_transcript_store("m-1", db)
        store_b = make_transcript_store("m-2", db)
        row = _mk(store_a)
        assert store_a.get_segment(row.segment_id) is not None
        assert store_b.get_segment(row.segment_id) is None

    def test_list_is_scoped_to_meeting(self, db: Path):
        _insert_meeting(db, "m-2")
        store_a = make_transcript_store("m-1", db)
        store_b = make_transcript_store("m-2", db)
        store_a.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "甲", "confidence": 0.9},
        ])
        assert store_a.count() == 1
        assert store_b.count() == 0
        assert store_b.list_segments() == []

    def test_list_limit_offset(self, store: TranscriptStore):
        store.replace_segments([
            {"seq": i, "start_ms": i * 100, "end_ms": i * 100 + 50,
             "text": f"第{i}段", "confidence": 0.9}
            for i in range(1, 6)
        ])
        assert [r.seq for r in store.list_segments(limit=2)] == [1, 2]
        assert [r.seq for r in store.list_segments(limit=2, offset=2)] == [3, 4]

    def test_list_seq_range(self, store: TranscriptStore):
        store.replace_segments([
            {"seq": i, "start_ms": i * 100, "end_ms": i * 100 + 50,
             "text": f"第{i}段", "confidence": 0.9}
            for i in range(1, 6)
        ])
        got = [r.seq for r in store.list_segments(min_seq=2, max_seq=4)]
        assert got == [2, 3, 4]

    def test_limit_zero_is_clamped_to_one(self, store: TranscriptStore):
        """`limit=0` 会静默返回空 —— 夹到 1，避免"明明有数据却空"的困惑。"""
        _mk(store)
        assert len(store.list_segments(limit=0)) == 1


# ---------------------------------------------------------------------------
# ③ 复核统计
# ---------------------------------------------------------------------------

class TestCountsByReviewState:
    def test_empty(self, store: TranscriptStore):
        c = store.counts_by_review_state()
        assert c == {"total": 0, "reviewed": 0, "pending": 0,
                     "low_confidence": 0, "no_confidence": 0}

    def test_partitions_correctly(self, store: TranscriptStore):
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "高", "confidence": 0.95},
            {"seq": 2, "start_ms": 100, "end_ms": 200, "text": "低", "confidence": 0.30},
            {"seq": 3, "start_ms": 200, "end_ms": 300, "text": "无", "confidence": None},
        ])
        c = store.counts_by_review_state()
        assert c["total"] == 3
        assert c["low_confidence"] == 1
        assert c["no_confidence"] == 1
        assert c["pending"] == 3

    def test_threshold_boundary_is_exclusive(self, store: TranscriptStore):
        """`confidence < 0.7` 才算低置信 —— 恰好 0.7 不算。"""
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "边界",
             "confidence": DEFAULT_LOW_CONFIDENCE},
        ])
        assert store.counts_by_review_state()["low_confidence"] == 0


# ---------------------------------------------------------------------------
# ④ 就地编辑
# ---------------------------------------------------------------------------

class TestEditSegment:
    def test_edit_changes_text_and_bumps_version(self, store: TranscriptStore):
        row = _mk(store, text="大家觉得")
        assert row.version == 1
        res = store.edit_segment(row.segment_id, "会议认为", operator="刘秘书")
        assert res.changed is True
        assert res.version == 2
        assert res.segment.text == "会议认为"
        assert res.segment.reviewed is True
        assert res.segment.reviewed_by == "刘秘书"
        assert res.before_text == "大家觉得"

    def test_edit_persists(self, store: TranscriptStore):
        row = _mk(store, text="大家觉得")
        store.edit_segment(row.segment_id, "会议认为", operator="刘秘书")
        again = store.get_segment(row.segment_id)
        assert again is not None
        assert again.text == "会议认为"
        assert again.version == 2
        assert again.reviewed is True

    def test_edit_same_text_still_bumps_version_and_marks_reviewed(
        self, store: TranscriptStore
    ):
        """看一眼没改就保存是常见操作，**不该报错**，但要告知用户。"""
        row = _mk(store, text="会议认为")
        res = store.edit_segment(row.segment_id, "会议认为", operator="刘秘书")
        assert res.changed is False
        assert res.version == 2
        assert res.segment.reviewed is True
        assert res.warnings  # 明确告诉调用方"仍然记了一次"

    @pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
    def test_empty_text_rejected(self, store: TranscriptStore, bad: str):
        """逐段校对是改错字，不是删段落。"""
        row = _mk(store)
        with pytest.raises(ValueError, match="不能为空"):
            store.edit_segment(row.segment_id, bad)

    def test_unknown_segment_raises_keyerror(self, store: TranscriptStore):
        with pytest.raises(KeyError):
            store.edit_segment("no-such-id", "新文本")

    def test_cannot_edit_other_meetings_segment(self, db: Path):
        """⚠️ 安全边界：跨会议编辑必须失败。"""
        _insert_meeting(db, "m-2")
        store_a = make_transcript_store("m-1", db, audit=AuditLogger(db))
        store_b = make_transcript_store("m-2", db, audit=AuditLogger(db))
        row = _mk(store_a, text="原文")
        with pytest.raises(KeyError):
            store_b.edit_segment(row.segment_id, "篡改")
        assert store_a.get_segment(row.segment_id).text == "原文"

    def test_edit_can_replace_flags(self, store: TranscriptStore):
        row = _mk(store, flags=["low_conf"])
        res = store.edit_segment(row.segment_id, "改好了", flags=[])
        assert res.segment.flags == ()

    def test_edit_without_flags_arg_keeps_them(self, store: TranscriptStore):
        row = _mk(store, flags=["overlap"])
        res = store.edit_segment(row.segment_id, "改好了")
        assert res.segment.flags == ("overlap",)

    def test_edit_marks_reviewed_even_when_flags_remain(self, store: TranscriptStore):
        """人工看过就算看过，即使标记还在（标记由引擎给，不进人工流程）。"""
        row = _mk(store, flags=["overlap"])
        res = store.edit_segment(row.segment_id, "改好了")
        assert res.segment.reviewed is True


# ---------------------------------------------------------------------------
# ⑤ 批量接受（两条判据 —— 本模块最容易写错的地方）
# ---------------------------------------------------------------------------

class TestAcceptHighConfidence:
    def _seed(self, store: TranscriptStore) -> None:
        store.replace_segments([
            # seq 1：高分 + 无标记 → 应接受
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "好的",
             "confidence": 0.95},
            # seq 2：低分 → 应跳过
            {"seq": 2, "start_ms": 100, "end_ms": 200, "text": "大家觉得",
             "confidence": 0.40},
            # seq 3：★ 高分但有 overlap 标记 → 必须跳过
            {"seq": 3, "start_ms": 200, "end_ms": 300, "text": "抢话",
             "confidence": 0.93, "flags": ["overlap"]},
            # seq 4：无置信度 → 应跳过
            {"seq": 4, "start_ms": 300, "end_ms": 400, "text": "没给分",
             "confidence": None},
        ])

    def test_accepts_only_high_confidence_without_flags(self, store: TranscriptStore):
        """核心断言：`overlap` 那条**分数很高也必须跳过**。

        只看分数的实现会把 seq 3 接受 —— 那是错的：引擎对两人抢话
        往往给出很高置信度（它很确定自己听见了字），但内容是叠在一起的
        废数据。这正是 `flags` 存在的理由。
        """
        self._seed(store)
        res = store.accept_high_confidence(operator="刘秘书")
        assert res.accepted == 1
        assert res.segment_ids == ["m-1:00001"]
        # 三条被跳过：seq2 低分、seq3 有 overlap 标记、seq4 没给置信度。
        # 注意 seq4：`confidence IS NULL` 归入"不能自动接受"（无法用分数分流），
        # 混进 low_confidence 计数是刻意的简化——两类都要人工过。
        assert res.skipped_low_confidence == 3
        assert res.skipped_reviewed == 0

    def test_accept_marks_reviewed(self, store: TranscriptStore):
        self._seed(store)
        store.accept_high_confidence(operator="刘秘书")
        row = store.get_segment("m-1:00001")
        assert row is not None
        assert row.reviewed is True
        assert row.reviewed_by == "刘秘书"

    def test_already_reviewed_are_skipped_not_recounted(self, store: TranscriptStore):
        self._seed(store)
        first = store.accept_high_confidence(operator="刘秘书")
        assert first.accepted == 1
        second = store.accept_high_confidence(operator="刘秘书")
        assert second.accepted == 0
        assert second.skipped_reviewed == 1

    def test_manually_edited_segment_counts_as_reviewed(self, store: TranscriptStore):
        self._seed(store)
        store.edit_segment("m-1:00002", "会议认为", operator="刘秘书")
        res = store.accept_high_confidence(operator="刘秘书")
        assert res.skipped_reviewed == 1
        assert "m-1:00002" not in res.segment_ids

    def test_threshold_is_configurable(self, store: TranscriptStore):
        self._seed(store)
        # 放到 0.35：seq2（0.40）也够格了
        res = store.accept_high_confidence(operator="刘秘书", threshold=0.35)
        assert "m-1:00002" in res.segment_ids
        assert res.accepted == 2

    def test_threshold_boundary_inclusive(self, store: TranscriptStore):
        """`>= threshold`：恰好等于阈值要接受。"""
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "边界",
             "confidence": ACCEPT_MIN_CONFIDENCE},
        ])
        res = store.accept_high_confidence()
        assert res.accepted == 1

    def test_default_threshold_is_documented_value(self):
        assert ACCEPT_MIN_CONFIDENCE == 0.85

    def test_limit_to_specific_ids(self, store: TranscriptStore):
        self._seed(store)
        res = store.accept_high_confidence(segment_ids=["m-1:00001"])
        assert res.accepted == 1
        assert res.segment_ids == ["m-1:00001"]

    def test_empty_id_list_accepts_nothing(self, store: TranscriptStore):
        self._seed(store)
        res = store.accept_high_confidence(segment_ids=[])
        assert res.accepted == 0
        assert store.counts_by_review_state()["reviewed"] == 0

    def test_unknown_id_in_list_is_ignored(self, store: TranscriptStore):
        self._seed(store)
        res = store.accept_high_confidence(segment_ids=["m-1:00001", "ghost"])
        assert res.accepted == 1

    def test_empty_store(self, store: TranscriptStore):
        res = store.accept_high_confidence()
        assert res.accepted == 0
        assert res.to_dict()["segment_ids"] == []


# ---------------------------------------------------------------------------
# ⑥ 审计留痕（AUD-05 / AUD-08）
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_edit_writes_segment_edit_action(self, store: TranscriptStore, db: Path):
        row = _mk(store, text="大家觉得")
        store.edit_segment(row.segment_id, "会议认为", operator="刘秘书")
        records = query(db, AuditQuery(action="segment_edit"))
        assert len(records) == 1
        r = records[0]
        assert r.operator == "刘秘书"
        assert r.target_type == "segment"
        assert r.target_id == row.segment_id

    def test_audit_detail_has_no_transcript_text(self, store: TranscriptStore, db: Path):
        """★ AUD-05：detail **不含转写稿原文**，只含 id 与长度。"""
        secret = "本次会议讨论了涉密事项的具体内容"
        row = _mk(store, text="原")
        store.edit_segment(row.segment_id, secret, operator="刘秘书")
        records = query(db, AuditQuery(action="segment_edit"))
        raw = str(records[0].detail)
        assert secret not in raw
        assert records[0].detail.get("before_len") == 1
        assert records[0].detail.get("after_len") == len(secret)

    def test_unchanged_edit_still_writes_audit(self, store: TranscriptStore, db: Path):
        """★ 无改动**也**留痕，但用 `reason=no_text_change` 与"改了字"区分开。

        ⚠️ 本用例原名 `test_unchanged_edit_writes_no_audit`，断言"没改就不留痕"，
        理由是 docstring 里那句真话：「否则用户反复点保存会把审计冲垮」。

        T1-15 复核时判定 **这条断言错了、那个顾虑对**，两件事分开处理：

        * **断言错了**：`edit_segment` 无论正文变不变，都真的改了
          `version += 1` / `reviewed = 1` / `reviewed_by/at` —— 它**就是一次
          真实写操作**。而且"用户点了『这段没问题』"在复核流程里比改错字
          更需要留痕（AUD-08 关心的是"逐段校对有没有被复核"；若只有改动才留痕，
          "全部复核完但一字未改"的会议在审计里会**看起来像没复核过**）。
          旧断言的后果是：端点返回的 warning 说「仍记了一次校订」，
          审计里却**什么都没有** —— 文案与事实自相矛盾（缺陷 22）。
        * **顾虑对**：反复保存确实会把 `segment_edit` 冲淡。解法不是"不记"，
          而是**记的时候带上可区分的标记**，让查阅者能一眼看出
          "这次只是又确认了一遍，不是又改了一处"——即下面的 `reason`。
        """
        row = _mk(store, text="会议认为")
        res = store.edit_segment(row.segment_id, "会议认为", operator="刘秘书")

        assert res.changed is False, "changed 字段如实反映『正文没变』，与审计无关"

        records = query(db, AuditQuery(action="segment_edit"))
        assert len(records) == 1, "无改动也留痕 —— 这是一次真实的写操作（version/reviewed 都动了）"
        assert records[0].detail.get("reason") == "no_text_change", (
            "★ 用 reason 把『只是确认了一遍』与『真的改了字』分开，"
            "以免反复保存把逐段改动冲淡"
        )
        assert records[0].detail.get("before_len") == records[0].detail.get("after_len")

    def test_changed_edit_has_no_reason_marker(self, store: TranscriptStore, db: Path):
        """真改了字的那次，`reason` 键**根本不出现** —— 与上一条互为对照。

        `audit.py:740` 写的是 `if reason: detail["reason"] = reason`，
        所以 `reason=None` 的后果是**键缺失**而不是"键在但值是 null"。
        断言写成 `is None` 会同时接受两种情况，验不出这个细节，故用 `not in`。
        """
        row = _mk(store, text="会议认为")
        store.edit_segment(row.segment_id, "会议认为必须执行", operator="刘秘书")
        records = query(db, AuditQuery(action="segment_edit"))
        assert len(records) == 1
        assert "reason" not in records[0].detail

    def test_accept_writes_single_audit_for_the_batch(self, store: TranscriptStore, db: Path):
        """★ 批量接受 300 段写 1 条，不是 300 条 —— 否则淹没逐段改动。"""
        store.replace_segments([
            {"seq": i, "start_ms": i * 100, "end_ms": i * 100 + 50,
             "text": f"第{i}段", "confidence": 0.95}
            for i in range(1, 6)
        ])
        res = store.accept_high_confidence(operator="刘秘书")
        assert res.accepted == 5
        records = query(db, AuditQuery(action="segment_edit"))
        assert len(records) == 1
        assert records[0].detail.get("op") == "accept_high_confidence"
        assert records[0].detail.get("accepted") == 5

    def test_accept_with_zero_accepted_writes_no_audit(self, store: TranscriptStore, db: Path):
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100, "text": "低", "confidence": 0.1},
        ])
        store.accept_high_confidence()
        assert query(db, AuditQuery(action="segment_edit")) == []

    def test_missing_audit_logger_warns_but_does_not_crash(self, db: Path, caplog):
        """没注入审计器时**要打 warning**，不能静默放过。"""
        store = make_transcript_store("m-1", db)  # 无 audit
        row = _mk(store, text="大家觉得")
        with caplog.at_level("WARNING"):
            res = store.edit_segment(row.segment_id, "会议认为", operator="刘秘书")
        assert res.changed is True
        assert any("未留痕" in m for m in caplog.messages)

    def test_accept_audit_has_no_text(self, store: TranscriptStore, db: Path):
        store.replace_segments([
            {"seq": 1, "start_ms": 0, "end_ms": 100,
             "text": "涉密的具体表述", "confidence": 0.95},
        ])
        store.accept_high_confidence()
        raw = str(query(db, AuditQuery(action="segment_edit"))[0].detail)
        assert "涉密的具体表述" not in raw

    def test_source_never_logs_raw_text(self):
        """静态兜底：审计调用里不得出现正文变量。

        ⚠️ 两次踩坑记录（这个断言我写错了两版）：

        第一版 `assert '"text":' not in src` —— 误报。
        `SegmentRow.to_dict()` 里本来就有一行 `"text": self.text`
        （那是**返回给界面**的，不是写审计的），全文件搜索会打红。

        第二版 `assert "before_text" not in _log_edit 源码` —— 也误报。
        `_log_edit` **接收** `before_text` / `after_text` 作为**参数**
        （它需要拿来算长度），所以名字必然出现在函数签名里。

        真正的红线是**第三层**：正文不许**裸传**进传给审计器的 `detail`。
        `len(before_text)` 是合法且必需的（我们就是要长度）；
        非法的只有"把 `before_text` 本身当成值传出去"。

        ⚠️ **第三版也踩了坑**：用 `ast.walk` 遍历 Call 的实参时，
        它会**下钻到内层**——`len(before_text)` 会被拆出裸的
        `Name("before_text")`，于是合法代码被误报。

        ⚠️ **第四版才是对的**：光"只看 `node.args` 顶层"还不够。
        `ast.walk` 会把 **`len(before_text)` 这个内层 Call 本身**
        也当作一个 Call 节点吐出来，它的 `args[0]` 就是裸 `Name`。
        所以必须**手工递归、且不进入实参的子树**——
        遇到 Call 只看它的实参，但不递归实参内部。
        """
        import ast
        import inspect
        import textwrap

        from mmwf.infra.transcript_store import TranscriptStore

        TEXT_VARS = {"before_text", "after_text", "text"}
        src = textwrap.dedent(inspect.getsource(TranscriptStore._log_edit))
        fn = ast.parse(src).body[0]
        assert isinstance(fn, ast.FunctionDef)

        offenders: list[str] = []

        def bare_text(node: ast.AST) -> str | None:
            """返回裸正文变量名；被 `len()` 包住的返回 None（合法）。"""
            if isinstance(node, ast.Name) and node.id in TEXT_VARS:
                return node.id
            return None

        def scan(node: ast.AST) -> None:
            """在语句/表达式树里找"正文被裸传"的位置。

            规则：**只检查 Call 的实参与 Dict 的 value**，
            然后对它们**整体**判断，不递归进去——
            这样 `len(before_text)` 只看成一个 Call（放行），
            而内层的 `Name` 永远不会被当成独立实参。
            其余节点（如 If/Return/lambda 体内）继续递归，
            保证覆盖不留死角。
            """
            if isinstance(node, ast.Call):
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    got = bare_text(arg)
                    if got:
                        offenders.append(f"arg {got}")
                    # 不下钻进实参内部（否则 len(x) 里的 x 会误报）
                # 但要继续扫 func 之外的普通语句（本函数体里的 If 等）
                return
            if isinstance(node, ast.Dict):
                for v in node.values:
                    got = bare_text(v)
                    if got:
                        offenders.append(f"dict value {got}")
                return
            for child in ast.iter_child_nodes(node):
                scan(child)

        for stmt in fn.body:
            scan(stmt)

        assert not offenders, (
            f"_log_edit 把正文裸传给了调用：{sorted(set(offenders))}。"
            "审计 detail 只允许长度（AUD-05）。"
        )
        # 正向断言：确实传了长度（否则可能因为"什么都没传"而假过），
        # 且长度确实是**从正文变量算出来的**（不是写死的 0）。
        body_src = ast.unparse(fn)
        assert "before_len=len(before_text)" in body_src
        assert "after_len=len(after_text)" in body_src


# ---------------------------------------------------------------------------
# ⑦ 数据类
# ---------------------------------------------------------------------------

class TestSegmentRow:
    def test_to_dict_shape(self, store: TranscriptStore):
        row = _mk(store)
        d = row.to_dict()
        for key in ("segment_id", "meeting_id", "seq", "start_ms", "end_ms",
                    "duration_ms", "text", "confidence", "flags",
                    "review_flags", "reviewed", "version"):
            assert key in d
        assert isinstance(d["flags"], list)

    def test_review_flags_merges_low_conf_and_engine_flags(self, store: TranscriptStore):
        row = _mk(store, confidence=0.2, flags=["silence"])
        rf = row.review_flags
        assert "low_conf" in rf
        assert "silence" in rf

    def test_review_flags_does_not_duplicate_low_conf(self, store: TranscriptStore):
        row = _mk(store, confidence=0.2, flags=["low_conf"])
        assert row.review_flags.count("low_conf") == 1

    def test_review_flags_empty_for_clean_high_confidence(self, store: TranscriptStore):
        assert _mk(store, confidence=0.99).review_flags == ()

    def test_edit_result_to_dict(self, store: TranscriptStore):
        row = _mk(store, text="大家觉得")
        res = store.edit_segment(row.segment_id, "会议认为")
        d = res.to_dict()
        assert d["changed"] is True
        assert d["version"] == 2
        assert "segment" in d

    def test_accept_result_to_dict(self, store: TranscriptStore):
        _mk(store)
        d = store.accept_high_confidence().to_dict()
        assert set(d) == {"accepted", "skipped_reviewed",
                          "skipped_low_confidence", "segment_ids"}


# ---------------------------------------------------------------------------
# ⑧ 构造与边界
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_empty_meeting_id_rejected(self, db: Path):
        with pytest.raises(ValueError, match="meeting_id"):
            make_transcript_store("", db)

    def test_repr_ish_attributes(self, db: Path):
        s = make_transcript_store("m-1", db)
        assert s.meeting_id == "m-1"
        assert s.db_path == db

    def test_replace_empty_list_clears(self, store: TranscriptStore):
        _mk(store)
        assert store.count() == 1
        assert store.replace_segments([]) == 0
        assert store.count() == 0

    def test_replace_requires_seq(self, store: TranscriptStore):
        with pytest.raises(KeyError):
            store.replace_segments([{"start_ms": 0, "end_ms": 1, "text": "x"}])

    def test_replace_with_audio_id(self, store: TranscriptStore):
        store.replace_segments(
            [{"seq": 1, "start_ms": 0, "end_ms": 100, "text": "x", "confidence": 0.9}],
            audio_id="a-1",
        )
        assert store.list_segments()[0].audio_id == "a-1"
