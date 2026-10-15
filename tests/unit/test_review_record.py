"""T1-08 单元测试：记录稿渲染 + service 落盘（含 manifest/审计/版本号）。

对标施工卡的另外三条验收标准：

    验收1  3 小时会议转写稿，进入人工复核的段落占比 < 20%
    验收2  记录稿含全部 8 项元信息，attendees 正确展开为顿号分隔
    验收3  记录稿正文段落带时间码，格式 [00:12:34]

以及施工卡「常见坑」第三条：

    坑3   记录稿的文件名与版本号要与 manifest.json 一致

还有测试方案 §9.1 的 V2（转写稿到记录稿）："一键生成结构化记录.md，
元信息完整，低置信段落已标记"。

本文件分三段：
  A. 纯渲染（`render_record` / `join_names` / `format_timestamp`）—— 无 IO
  B. service 落盘（`ReviewService`）—— 真库真 filestore，临时目录
  C. 合规（审计 detail 不含正文、记录稿在加密区）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from mmwf.app.services.review import (
    ALLOWED_PREV_STATES,
    RECORD_AREA,
    RECORD_NAME_TEMPLATE,
    STATE_RECORD_READY,
    TRANSCRIPT_SEGMENTS_PATH,
    RecordResult,
    ReviewService,
    ReviewServiceError,
    _version_from_name,
)
from mmwf.domain.asr_types import TranscriptSegmentResult
from mmwf.domain.review import (
    METADATA_FIELDS,
    MISSING_METADATA_PLACEHOLDER,
    RecordMetadata,
    RecordRenderError,
    ReviewItem,
    format_timestamp,
    join_names,
    render_record,
    summarize_review,
)
from mmwf.infra.audit import (
    AuditLogger,
    query,
    verify_chain,
)
from mmwf.infra.db import connect_ctx
from mmwf.infra.filestore import ENCRYPTED_AREAS, make_filestore
from mmwf.infra.migrations.runner import migrate

THRESHOLDS = {"asr": {"low_confidence": 0.70}}
MEETING_ID = "mtg-t108"
OPERATOR = "zhangsan"
GENERATED_AT = "2026-09-27T12:00:00Z"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _seg(seq, text, conf, start_ms=0, end_ms=None, flags=()):
    return TranscriptSegmentResult(
        seq=seq,
        text=text,
        start_ms=start_ms,
        end_ms=end_ms if end_ms is not None else start_ms + 3000,
        confidence=conf,
        flags=tuple(flags),
    )


def _full_meta(**over) -> RecordMetadata:
    base = dict(
        title="党组理论学习中心组会议",
        meeting_time="2026-09-27T09:00:00Z",
        location="三楼第一会议室",
        host="张三丰",
        attendees=["李四", "王五", "赵六"],
        absentees=["钱七"],
        recorder="孙八",
        generated_at=GENERATED_AT,
    )
    base.update(over)
    return RecordMetadata(**base)


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def db_path(data_root: Path) -> Path:
    p = data_root / "mmwf.db"
    migrate(p)
    return p


@pytest.fixture()
def meeting_row(db_path: Path):
    """建一行 meeting（status=reviewing，即 SAD §5.1 允许的前置状态）。"""

    def _make(status: str = "reviewing", **over):
        row = {
            "meeting_id": MEETING_ID,
            "title": "党组理论学习中心组会议",
            "meeting_time": "2026-09-27T09:00:00Z",
            "location": "三楼第一会议室",
            "host": "张三丰",
            "attendees": json.dumps(["李四", "王五", "赵六"], ensure_ascii=False),
            "absentees": json.dumps(["钱七"], ensure_ascii=False),
            "recorder": "孙八",
            "classification": "internal",
            "template_id": None,
            "status": status,
            "created_by": "tester",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
        }
        row.update(over)
        cols = ", ".join(row)
        ph = ", ".join("?" for _ in row)
        with connect_ctx(db_path) as conn:
            conn.execute(
                f"INSERT INTO meeting ({cols}) VALUES ({ph})", list(row.values())
            )
        return row

    return _make


@pytest.fixture()
def store(data_root: Path):
    data_root.mkdir(parents=True, exist_ok=True)
    return make_filestore(MEETING_ID, data_root, encrypt_enabled=False)


@pytest.fixture()
def audit(db_path: Path) -> AuditLogger:
    return AuditLogger(db_path)


def _write_segments(store, segments, **meta) -> None:
    payload = {
        "engine": "funasr",
        "model": "paraformer-zh",
        "avg_confidence": 0.9,
        "hotwords_applied": True,
        "warnings": [],
        "segments": [
            {
                "seq": s.seq,
                "text": s.text,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "confidence": s.confidence,
                "flags": list(s.flags),
                "words": [],
            }
            for s in segments
        ],
    }
    payload.update(meta)
    store.write_json(TRANSCRIPT_SEGMENTS_PATH, payload, stage="transcript")


@pytest.fixture()
def svc(store, db_path, audit) -> ReviewService:
    return ReviewService(
        store, db_path=db_path, thresholds=THRESHOLDS,
        audit=audit, operator=OPERATOR,
    )


# ===========================================================================
# A. 纯渲染
# ===========================================================================

class TestFormatTimestamp:
    """验收标准第 3 条：时间码格式 `[00:12:34]`。"""

    def test_bracketed_hhmmss(self):
        assert format_timestamp(754_000) == "[00:12:34]"

    def test_matches_card_example_shape(self):
        """施工卡模板里出现的正是 `[00:12:34]` 这个字面量。"""
        assert format_timestamp(12 * 60_000 + 34_000) == "[00:12:34]"

    def test_zero(self):
        assert format_timestamp(0) == "[00:00:00]"

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            format_timestamp(-1)

    def test_regex_extractable(self):
        """导出/复核页按正则抓 `\\[(\\d{2}:\\d{2}:\\d{2})\\]`。"""
        import re

        pat = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
        for ms in (0, 1000, 754_000, 3_661_000, 45_296_000):
            m = pat.fullmatch(format_timestamp(ms))
            assert m is not None, ms


class TestJoinNames:
    """验收标准第 2 条：attendees 正确展开为顿号分隔。"""

    def test_list_to_ideographic_comma(self):
        assert join_names(["张三", "李四", "王五"]) == "张三、李四、王五"

    def test_single_name(self):
        assert join_names(["张三"]) == "张三"

    def test_tuple_input(self):
        assert join_names(("张三", "李四")) == "张三、李四"

    def test_none_is_placeholder(self):
        assert join_names(None) == MISSING_METADATA_PLACEHOLDER

    def test_empty_list_is_placeholder(self):
        assert join_names([]) == MISSING_METADATA_PLACEHOLDER

    def test_whitespace_items_dropped(self):
        assert join_names(["张三", "  ", "李四"]) == "张三、李四"

    def test_string_with_english_comma_normalized(self):
        """库里手工填的 `"张三, 李四"` 也要出顿号 —— 半角逗号在公文里是硬伤。"""
        assert join_names("张三, 李四") == "张三、李四"

    def test_string_with_ideographic_comma_kept(self):
        assert join_names("张三、李四") == "张三、李四"

    def test_string_with_semicolon_split(self):
        assert join_names("张三；李四;王五") == "张三、李四、王五"

    def test_whitespace_only_string_is_placeholder(self):
        assert join_names("   ") == MISSING_METADATA_PLACEHOLDER

    def test_never_renders_python_list_repr(self):
        """关键回归：绝不能出现 `['张三', '李四']` 这种 Python 字面量。

        `meeting.attendees` 在库里是 JSON 文本，历史上的实现
        直接把它 str() 出来就写进了记录稿 —— 那会一眼暴露"机器生成"。
        """
        out = join_names(["张三", "李四"])
        assert "[" not in out and "'" not in out and '"' not in out


class TestRecordMetadataFromRow:

    def test_parses_json_name_columns(self):
        row = {
            "title": "例会",
            "meeting_time": "2026-09-27T09:00:00Z",
            "location": "会议室",
            "host": "张三",
            "attendees": '["李四","王五"]',
            "absentees": '["赵六"]',
            "recorder": "孙八",
        }
        m = RecordMetadata.from_row(row, generated_at=GENERATED_AT)
        assert list(m.attendees) == ["李四", "王五"]
        assert list(m.absentees) == ["赵六"]
        assert m.generated_at == GENERATED_AT

    def test_tolerates_plain_string_names(self):
        """手工在库里写 `"张三、李四"`（不是 JSON）也要吃下，别抛错。"""
        row = {"attendees": "张三、李四", "absentees": None}
        m = RecordMetadata.from_row(row, generated_at=GENERATED_AT)
        assert list(m.attendees) == ["张三、李四"]

    def test_broken_json_falls_back_to_string(self):
        """坏了 JSON（少了右括号）不炸，当普通字符串处理。"""
        row = {"attendees": '["张三","李四"'}
        m = RecordMetadata.from_row(row, generated_at=GENERATED_AT)
        assert m.attendees is not None and len(list(m.attendees)) == 1

    def test_missing_keys_become_none(self):
        m = RecordMetadata.from_row({}, generated_at=GENERATED_AT)
        assert m.title is None
        assert m.attendees is None
        assert m.generated_at == GENERATED_AT

    def test_blank_strings_become_none(self):
        m = RecordMetadata.from_row({"title": "   "}, generated_at=GENERATED_AT)
        assert m.title is None

    def test_list_value_passed_through(self):
        """列值已经是 list（不是 JSON 文本）时直接用。"""
        m = RecordMetadata.from_row(
            {"attendees": ["李四", "王五"]}, generated_at=GENERATED_AT
        )
        assert list(m.attendees) == ["李四", "王五"]

    def test_blank_attendees_string_is_none(self):
        m = RecordMetadata.from_row({"attendees": "   "}, generated_at=GENERATED_AT)
        assert m.attendees is None

    def test_json_array_of_non_strings_stringified(self):
        """JSON 数组里混了非字符串（数字/None）也要能渲染，不抛错。"""
        m = RecordMetadata.from_row(
            {"attendees": "[1, null, \"张三\"]"}, generated_at=GENERATED_AT
        )
        assert [str(x) for x in m.attendees] == ["1", "None", "张三"]


class TestRenderRecord:
    """施工卡 F2.3 模板逐项对齐。"""

    def test_has_all_three_sections(self):
        md = render_record(
            [_seg(1, "内容", 0.95, 12_000)], _full_meta(), THRESHOLDS
        ).markdown
        assert "# 会议记录" in md
        assert "## 一、会议基本信息" in md
        assert "## 二、会议记录正文" in md
        assert "## 三、待核对事项" in md

    def test_all_eight_metadata_fields_present(self):
        """验收标准第 2 条：8 项元信息全部出现，一项不少。"""
        md = render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS
        ).markdown
        for _key, label in METADATA_FIELDS:
            assert f"| {label} |" in md, label
        assert len(METADATA_FIELDS) == 8

    def test_metadata_values_rendered(self):
        md = render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS
        ).markdown
        assert "| 会议名称 | 党组理论学习中心组会议 |" in md
        assert "| 会议地点 | 三楼第一会议室 |" in md
        assert "| 主持人 | 张三丰 |" in md
        assert "| 记录人 | 孙八 |" in md

    def test_attendees_joined_by_ideographic_comma(self):
        """验收标准第 2 条：attendees 正确展开为顿号分隔。"""
        md = render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS
        ).markdown
        assert "| 参会人员 | 李四、王五、赵六 |" in md

    def test_missing_metadata_filled_with_placeholder(self):
        """坑 2：缺失项填「待补充」，**不留空**。

        留空是"沉默的谎言"——归档后没人分得清是"没有主持人"
        还是"我们没填"。
        """
        meta = RecordMetadata(generated_at=GENERATED_AT)     # 其余全空
        result = render_record([_seg(1, "内容", 0.95)], meta, THRESHOLDS)
        md = result.markdown
        for _key, label in METADATA_FIELDS:
            if label == "记录整理时间":
                continue
            assert f"| {label} | {MISSING_METADATA_PLACEHOLDER} |" in md, label
        # 表格里不该出现 "| |"（空单元格）
        assert "| |" not in md
        assert len(result.metadata_missing) == 7
        assert "会议名称" in result.metadata_missing
        assert "记录整理时间" in result.metadata_filled

    def test_body_segments_have_timestamps(self):
        """验收标准第 3 条：正文每段带 `[HH:MM:SS]` 时间码。"""
        segments = [
            _seg(1, "第一段内容", 0.95, 0),
            _seg(2, "第二段内容", 0.95, 12_000),
            _seg(3, "第三段内容", 0.95, 754_000),
        ]
        md = render_record(segments, _full_meta(), THRESHOLDS).markdown
        assert "[00:00:00] 第一段内容" in md
        assert "[00:00:12] 第二段内容" in md
        assert "[00:12:34] 第三段内容" in md

    def test_body_keeps_all_segments_including_reviewed(self):
        """正文**保留全部段落**（含待复核的）。

        待复核只是额外在第三节列出，不从正文抽走 ——
        抽走会让正文时间码出现空洞，使用者会以为"那段时间没人说话"。
        """
        segments = [
            _seg(1, "正常内容", 0.95, 1_000),
            _seg(2, "低分内容", 0.30, 5_000),
            _seg(3, "嗯", 0.99, 9_000),
        ]
        result = render_record(segments, _full_meta(), THRESHOLDS)
        assert result.body_segment_count == 3
        assert "[00:00:05] 低分内容" in result.markdown
        assert "[00:00:09] 嗯" in result.markdown
        assert result.review_count == 2
        assert result.auto_pass_count == 1

    def test_review_items_have_timestamp_and_reason_and_text(self):
        """第三节条目要含：时间码、理由、原文（施工卡模板）。"""
        segments = [_seg(1, "这一段听不清", 0.30, 754_000)]
        result = render_record(segments, _full_meta(), THRESHOLDS)
        item = result.review_items[0]
        assert item.timestamp == "[00:12:34]"
        assert "置信度" in item.reason
        assert item.text == "这一段听不清"
        assert "**[00:12:34]**" in result.markdown
        assert "> 这一段听不清" in result.markdown

    def test_empty_review_section_states_none_needed(self):
        """无待核对项时给一行说明，**不留空小节** ——
        空小节会让人以为渲染坏了，而不是"确实没有"。"""
        result = render_record(
            [_seg(1, "很清楚的内容", 0.99)], _full_meta(), THRESHOLDS
        )
        assert result.review_count == 0
        assert "无需人工核对" in result.markdown
        assert "## 三、待核对事项" in result.markdown

    def test_empty_segments_renders_without_crash(self):
        result = render_record([], _full_meta(), THRESHOLDS)
        assert "（无正文）" in result.markdown
        assert result.body_segment_count == 0

    def test_empty_segments_warns(self):
        result = render_record([], _full_meta(), THRESHOLDS)
        assert any("没有任何转写分段" in w for w in result.warnings)

    def test_version_line_when_given(self):
        md = render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS, version=3
        ).markdown
        assert "> 记录稿版本：v3" in md

    def test_no_version_line_when_absent(self):
        md = render_record([_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS).markdown
        assert "记录稿版本" not in md

    def test_metadata_accepts_mapping(self):
        md = render_record(
            [_seg(1, "内容", 0.95)],
            {"title": "从字典来的标题", "generated_at": GENERATED_AT},
            THRESHOLDS,
        ).markdown
        assert "| 会议名称 | 从字典来的标题 |" in md

    def test_missing_metadata_warning_mentions_labels(self):
        meta = RecordMetadata(title="会议", generated_at=GENERATED_AT)
        result = render_record([_seg(1, "内容", 0.95)], meta, THRESHOLDS)
        warn = " ".join(result.warnings)
        assert "待补充" in warn and "会议地点" in warn

    def test_missing_start_ms_raises(self):
        """缺 start_ms 必须当场停 —— 时间码错位在成品里看不出来。"""

        class NoStart:
            seq = 1
            text = "内容"
            confidence = 0.9

        with pytest.raises(RecordRenderError):
            render_record([NoStart()], _full_meta(), THRESHOLDS)

    def test_extra_warnings_go_into_section_four(self):
        result = render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS,
            extra_warnings=["引擎未提供置信度，本次复核分流能力降级"],
        )
        assert "## 四、生成提示" in result.markdown
        assert "复核分流能力降级" in result.markdown

    def test_no_section_four_when_no_warnings(self):
        result = render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS
        )
        assert not result.warnings
        assert "## 四" not in result.markdown

    def test_blank_segment_text_marked(self):
        """空文本段在正文里标「（空）」，不留一行光秃秃的时间码。"""
        result = render_record([_seg(1, "   ", 0.99, 1_000)], _full_meta(), THRESHOLDS)
        assert "[00:00:01] （空）" in result.markdown

    def test_metadata_complete_flag(self):
        assert render_record(
            [_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS
        ).metadata_complete is True
        assert render_record(
            [_seg(1, "内容", 0.95)], RecordMetadata(title="x"), THRESHOLDS
        ).metadata_complete is False

    def test_table_row_count_matches_eight_fields(self):
        """元信息表必须是 8 行 —— 多一行少一行都会让"会议时间"串行。"""
        md = render_record([_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS).markdown
        table = []
        for line in md.splitlines():
            if line.startswith("| ") and "项目" not in line and "---" not in line:
                table.append(line)
        assert len(table) == 8

    def test_markdown_ends_with_newline(self):
        md = render_record([_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS).markdown
        assert md.endswith("\n")

    def test_no_trailing_whitespace_lines(self):
        md = render_record([_seg(1, "内容", 0.95)], _full_meta(), THRESHOLDS).markdown
        assert not any(ln != ln.rstrip() for ln in md.splitlines())


class TestReviewItem:

    def test_to_line_shape(self):
        item = ReviewItem(seq=3, start_ms=754_000, reason="识别置信度偏低", text="原文内容")
        line = item.to_line()
        assert line.startswith("- **[00:12:34]**")
        assert "识别置信度偏低" in line
        assert "  > 原文内容" in line

    def test_to_line_uses_quote_not_code_span(self):
        """原文用 `>` 引用块，不用行内反引号 —— 原文里可能含反引号。"""
        item = ReviewItem(seq=1, start_ms=0, reason="理由", text="含 ` 反引号的内容")
        assert not item.to_line().count("`") >= 2

    def test_to_line_blank_text_marked(self):
        item = ReviewItem(seq=1, start_ms=0, reason="理由", text="   ")
        assert "（空）" in item.to_line()

    def test_timestamp_property(self):
        assert ReviewItem(seq=1, start_ms=1000, reason="r", text="t").timestamp == "[00:00:01]"


class TestSummarizeReview:

    def test_zero_total(self):
        assert "无分段" in summarize_review([], 0)

    def test_no_review_items(self):
        assert "全部通过" in summarize_review([], 10)

    def test_with_items(self):
        items = [ReviewItem(seq=i, start_ms=0, reason="r", text="t") for i in range(3)]
        s = summarize_review(items, 10)
        assert "3/10" in s and "30.0%" in s


# ===========================================================================
# B. service 落盘
# ===========================================================================

class TestVersionParsing:

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("v1.md", 1), ("v2.md", 2), ("v10.md", 10), ("v0.md", 0)],
    )
    def test_valid(self, name, expected):
        assert _version_from_name(name) == expected

    @pytest.mark.parametrize(
        "name", ["v.md", "1.md", "v1.txt", "vX.md", "vv1.md", "record/v1.md".split("/")[-1] + "x"]
    )
    def test_invalid(self, name):
        assert _version_from_name(name) is None


class TestGenerateRecord:

    def test_happy_path(self, svc, store, meeting_row):
        """施工卡 V2：一键生成结构化记录.md，元信息完整。"""
        meeting_row(status="reviewing")
        _write_segments(store, [
            _seg(1, "今天召开党组理论学习中心组会议", 0.96, 12_000),
            _seg(2, "下面请李四同志发言", 0.41, 31_000),
            _seg(3, "两人抢话内容", 0.97, 45_000, flags=("overlap",)),
        ])

        result = svc.generate_record(now=GENERATED_AT)

        assert isinstance(result, RecordResult)
        assert result.version == 1
        assert result.rel_path == "record/v1.md"
        assert result.segment_count == 3
        assert result.review_count == 2
        assert "[00:00:12] 今天召开党组理论学习中心组会议" in result.markdown
        assert "| 参会人员 | 李四、王五、赵六 |" in result.markdown
        assert result.metadata_complete is True

    def test_writes_to_record_area(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        result = svc.generate_record(now=GENERATED_AT)
        assert store.exists(result.rel_path)
        assert store.read_text(result.rel_path) == result.markdown

    def test_record_area_is_encrypted(self, svc, store, meeting_row):
        """记录稿属于加密区（filestore.ENCRYPTED_AREAS）—— 合规要求。"""
        assert RECORD_AREA in ENCRYPTED_AREAS

    def test_sha256_matches_content(self, svc, store, meeting_row):
        import hashlib

        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        result = svc.generate_record(now=GENERATED_AT)
        expect = hashlib.sha256(result.markdown.encode("utf-8")).hexdigest()
        assert result.sha256 == expect

    def test_manifest_entry_registered(self, svc, store, meeting_row):
        """坑 3（前半）：manifest.json 必须登记这条记录稿。"""
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        result = svc.generate_record(now=GENERATED_AT)

        entries = {e.path: e for e in store.manifest()}
        assert result.rel_path in entries
        e = entries[result.rel_path]
        assert e.stage == RECORD_AREA
        assert e.size_bytes > 0
        # manifest 记的是明文哈希（未加密场景）= 磁盘哈希
        assert e.plain_sha256 == result.sha256

    def test_filename_version_matches_manifest(self, svc, store, meeting_row):
        """坑 3（核心）：**文件名里的版本号 == manifest 里登记的路径**。

        三处（文件名 / manifest.path / 审计 detail.version）必须指向同一版。
        这是施工卡明确点名的坑：不一致时，出问题后无从知道
        "我核的是哪一版"，而三处不一致在成品里完全看不出来。
        """
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        result = svc.generate_record(now=GENERATED_AT)

        # ① 文件名里的版本
        assert Path(result.rel_path).name == RECORD_NAME_TEMPLATE.format(
            version=result.version
        )
        # ② manifest 里的路径
        paths = [e.path for e in store.manifest()]
        assert f"{RECORD_AREA}/{RECORD_NAME_TEMPLATE.format(version=result.version)}" in paths
        # ③ 磁盘上真有这个文件
        assert (store.store_root / result.rel_path).is_file()

    def test_manifest_verify_clean(self, svc, store, meeting_row):
        """生成之后 manifest 与实际文件必须完全一致（无未登记/无丢失）。"""
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)
        assert store.verify_manifest() == []

    def test_state_advanced_to_record_ready(self, svc, meeting_row):
        """SAD §5.1：`reviewing -> record_ready`。"""
        meeting_row(status="reviewing")
        _write_segments(svc._store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)
        assert svc.current_status() == STATE_RECORD_READY

    def test_state_not_written_when_disabled(self, svc, meeting_row):
        meeting_row(status="reviewing")
        _write_segments(svc._store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT, write_state=False)
        assert svc.current_status() == "reviewing"

    def test_version_increments_on_rerun(self, svc, store, meeting_row):
        """重跑**绝不覆盖**：v1 保留，新写 v2。

        覆盖会让"我核的是哪一版"无从回答。
        """
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        r1 = svc.generate_record(now=GENERATED_AT)
        r2 = svc.generate_record(now=GENERATED_AT)

        assert (r1.version, r2.version) == (1, 2)
        assert store.exists("record/v1.md") and store.exists("record/v2.md")
        assert r1.markdown  # v1 内容仍在

    def test_versions_reported_ascending(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        for _ in range(3):
            svc.generate_record(now=GENERATED_AT)
        assert svc.existing_versions() == [1, 2, 3]
        assert svc.latest_version() == 3

    def test_version_skips_deleted_but_recorded(self, svc, store, meeting_row):
        """手工删掉 v1.md 后，manifest 仍记得它 —— 新版本号必须跳到 2。

        否则会写出 `v1.md`，而 manifest 里已有一条指向 v1 的旧记录，
        审计中两个 detail 指向同路径但内容不同，无法分辨。
        """
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)
        (store.store_root / "record" / "v1.md").unlink()      # 绕过 filestore 直接删

        assert 1 in svc.existing_versions()                   # 仍从 manifest 得知
        assert svc.next_version() == 2

    def test_missing_meeting_row_still_generates(self, svc, store):
        """会议行不存在时**仍能生成**，元信息全填「待补充」。

        理由：正文（转写稿）才是记录稿主体，抬头缺了不给正文，
        用户会以为系统坏了 —— 而这本来就是他该看到的补齐提示。
        """
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        result = svc.generate_record(now=GENERATED_AT)
        assert result.version == 1
        assert len(result.metadata_missing) == 7      # 除"记录整理时间"外全缺
        assert MISSING_METADATA_PLACEHOLDER in result.markdown

    def test_missing_segments_file_raises(self, svc, meeting_row):
        meeting_row()
        with pytest.raises(ReviewServiceError, match="转写分段不存在"):
            svc.generate_record(now=GENERATED_AT)

    def test_corrupt_segments_json_raises(self, svc, store, meeting_row):
        meeting_row()
        store.write_text(TRANSCRIPT_SEGMENTS_PATH, "{ not json", stage="transcript")
        with pytest.raises(ReviewServiceError):
            svc.generate_record(now=GENERATED_AT)

    def test_segments_not_object_raises(self, svc, store, meeting_row):
        meeting_row()
        store.write_json(TRANSCRIPT_SEGMENTS_PATH, [1, 2, 3], stage="transcript")
        with pytest.raises(ReviewServiceError, match="顶层应为对象"):
            svc.generate_record(now=GENERATED_AT)

    def test_segments_missing_array_raises(self, svc, store, meeting_row):
        meeting_row()
        store.write_json(TRANSCRIPT_SEGMENTS_PATH, {"engine": "e"}, stage="transcript")
        with pytest.raises(ReviewServiceError, match="缺少 segments"):
            svc.generate_record(now=GENERATED_AT)

    def test_created_is_allowed_because_api_leaves_it_there(self, svc, store, meeting_row):
        """★ T1-11 Review 修正：`created` **必须放行**（这条以前是反着断言的）。

        原来的断言是"`created` 不许生成记录稿，说明调用方跳步了"。
        那个理由只对**直接调用 service 的人**成立。接进编排层之后：

          · 从 API 建会议进来的会议，`meeting.status` 就是建表默认值
            `created`，而**流水线从来不写它**（编排层只写 `task_state`）；
          · 于是"从 API 正常从头跑一遍"的会议，状态恰好是被挡住的那个，
            每次 `start` 都在 `review` 抛错 → 被包成 E9001
            → **整条流水线一步都走不动**。

        真正该守的是**输入有没有**，不是"某个字段等于某个值" ——
        那由 `load_segments()` 守（缺 `transcript/segments.json` 就抛，
        见 `test_segments_missing_raises`）。

        所以这条断言翻过来：`created` 下**应当**能生成。
        """
        meeting_row(status="created")
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        assert svc.generate_record(now=GENERATED_AT).version >= 1

    def test_missing_segments_still_blocks_regardless_of_status(self, svc, meeting_row):
        """★ 前置条件改由**输入**来守：状态再正常、没有分段照样拦。"""
        meeting_row(status="transcript_ready")
        with pytest.raises(ReviewServiceError, match="转写分段不存在"):
            svc.generate_record(now=GENERATED_AT)

    def test_every_graph_upstream_state_is_allowed(self):
        """上游所有可能状态都必须在白名单里（用图的反查，不硬列）。

        硬列会在"将来加了节点"时静默失效：新状态没进白名单，
        表现为**流水线在 review 处停摆**，而没有任何测试变红。
        """
        from mmwf.graph import state as S

        upstream = S.NODE_SEQUENCE[: S.NODE_SEQUENCE.index(S.NODE_REVIEW)]
        required = {S.NODE_STATUS[n] for n in upstream} | {S.STATUS_CREATED}
        missing = required - ALLOWED_PREV_STATES
        assert not missing, f"这些上游状态会让 start 失败：{sorted(missing)}"

    @pytest.mark.parametrize("state", sorted(ALLOWED_PREV_STATES))
    def test_allowed_prev_states(self, svc, store, meeting_row, state):
        meeting_row(status=state)
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        assert svc.generate_record(now=GENERATED_AT).version >= 1

    def test_missing_meeting_row_state_write_is_warning_not_error(self, svc, store, caplog):
        """无会议行时状态推进失败只告警，不阻断（记录稿与审计已落盘）。"""
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        with caplog.at_level(logging.WARNING):
            result = svc.generate_record(now=GENERATED_AT)
        assert result.version == 1
        assert any("未推进状态" in r.message for r in caplog.records)

    def test_empty_transcript_generates_placeholder_body(self, svc, store, meeting_row):
        """空转写稿：不崩，正文标「（无正文）」，并给出告警。"""
        meeting_row()
        store.write_json(
            TRANSCRIPT_SEGMENTS_PATH,
            {"engine": "funasr", "model": "m", "segments": []},
            stage="transcript",
        )
        result = svc.generate_record(now=GENERATED_AT)
        assert "（无正文）" in result.markdown
        assert any("没有任何转写分段" in w for w in result.warnings)

    def test_read_record_returns_latest(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)
        r2 = svc.generate_record(now=GENERATED_AT)
        assert svc.read_record() == r2.markdown

    def test_read_specific_version(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        r1 = svc.generate_record(now=GENERATED_AT)
        svc.generate_record(now=GENERATED_AT)
        assert svc.read_record(version=1) == r1.markdown

    def test_read_before_generate_raises(self, svc, meeting_row):
        meeting_row()
        with pytest.raises(ReviewServiceError, match="还没有生成过记录稿"):
            svc.read_record()

    def test_read_missing_file_raises(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)
        (store.store_root / "record" / "v1.md").unlink()
        # manifest 已知 v1 存在，但文件被外力删了
        with pytest.raises(ReviewServiceError, match="不存在"):
            svc.read_record()

    def test_summary_is_human_readable(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [
            _seg(1, "正常内容够长", 0.95),
            _seg(2, "嗯", 0.99),
        ])
        result = svc.generate_record(now=GENERATED_AT)
        s = result.summary()
        assert "v1" in s and "1/2" in s

    def test_summary_mentions_missing_metadata(self, svc, store):
        """元信息有缺项时，summary 要把它说出来（界面提示条直接用）。"""
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        result = svc.generate_record(now=GENERATED_AT)
        # 无 meeting 行 -> 7 项待补充
        assert "元信息待补充" in result.summary()
        assert "7" in result.summary()

    def test_summary_omits_metadata_clause_when_complete(
        self, svc, store, meeting_row
    ):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        assert "元信息待补充" not in svc.generate_record(now=GENERATED_AT).summary()

    def test_to_dict_shape(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        d = svc.generate_record(now=GENERATED_AT).to_dict()
        for k in ("meeting_id", "version", "path", "sha256", "segment_count",
                  "review_count", "review_ratio", "metadata_missing", "summary"):
            assert k in d


class TestPlanReviewReport:

    def test_report_lists_every_segment(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [
            _seg(1, "正常内容够长", 0.95),
            _seg(2, "低分内容", 0.3),
            _seg(3, "静音段内容", 0.9, flags=("silence",)),
            _seg(4, "无分数内容", None),
        ])
        report = svc.plan_review_report()
        assert [r["seq"] for r in report] == [1, 2, 3, 4]
        assert [r["needs_review"] for r in report] == [False, True, True, False]
        assert report[3]["reason"] == ""

    def test_report_is_json_serializable(self, svc, store, meeting_row):
        """复核页要 JSON —— 领域对象不能直接塞进响应。"""
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        json.dumps(svc.plan_review_report(), ensure_ascii=False)


class TestLoadSegments:

    def test_rebuilds_transcript_result(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)], engine="funasr")
        tr = svc.load_segments()
        assert tr.engine == "funasr"
        assert len(tr.segments) == 1
        assert tr.segments[0].text == "内容够长的一段"

    def test_restores_flags(self, svc, store, meeting_row):
        """flags 必须往返无损 —— 丢了会让"静音段"静默变成普通段。"""
        meeting_row()
        _write_segments(store, [
            _seg(1, "两人抢话内容", 0.97, flags=("overlap",)),
        ])
        tr = svc.load_segments()
        assert tr.segments[0].flags == ("overlap",)

    def test_restores_none_confidence(self, svc, store, meeting_row):
        meeting_row()
        _write_segments(store, [_seg(1, "无分数内容段", None)])
        assert svc.load_segments().segments[0].confidence is None

    def test_drops_empty_segments_and_repacks_seq(self, svc, store, meeting_row):
        """`from_raw_segments` 的规范化在 service 路径上也生效。"""
        meeting_row()
        store.write_json(
            TRANSCRIPT_SEGMENTS_PATH,
            {
                "engine": "funasr", "model": "m",
                "segments": [
                    {"seq": 5, "text": "后来的段", "start_ms": 5000,
                     "end_ms": 6000, "confidence": 0.9},
                    {"seq": 1, "text": "", "start_ms": 0, "end_ms": 100, "confidence": 0.9},
                    {"seq": 3, "text": "先说的段", "start_ms": 1000,
                     "end_ms": 2000, "confidence": 0.9},
                ],
            },
            stage="transcript",
        )
        tr = svc.load_segments()
        assert [s.seq for s in tr.segments] == [1, 2]
        assert [s.text for s in tr.segments] == ["先说的段", "后来的段"]


class TestThresholdWiring:

    def test_threshold_from_service_is_used(self, store, db_path, audit, meeting_row):
        """service 的 thresholds **实际决定**分流 —— 不是摆设。"""
        meeting_row()
        _write_segments(store, [_seg(1, "分数 0.80 的段落内容", 0.80)])

        loose = ReviewService(
            store, db_path=db_path, thresholds={"asr": {"low_confidence": 0.70}},
            audit=audit,
        )
        strict = ReviewService(
            store, db_path=db_path, thresholds={"asr": {"low_confidence": 0.90}},
            audit=audit,
        )
        assert loose.generate_record(now=GENERATED_AT).review_count == 0
        assert strict.generate_record(now=GENERATED_AT).review_count == 1


# ===========================================================================
# C. 审计与合规
# ===========================================================================

class TestAudit:

    def test_writes_record_generate_action(self, svc, store, db_path, meeting_row):
        """施工卡要求动作可追溯；`record_generate` 已登记进 ACTIONS。"""
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)

        rows = query(db_path, )
        actions = [r.action for r in rows]
        assert "record_generate" in actions

    def test_audit_detail_has_stats_and_path(self, svc, store, db_path, meeting_row):
        meeting_row()
        _write_segments(store, [
            _seg(1, "正常内容够长", 0.95),
            _seg(2, "低分内容段", 0.3),
        ])
        svc.generate_record(now=GENERATED_AT)

        rec = [r for r in query(db_path) if r.action == "record_generate"][0]
        d = rec.detail
        assert d["meeting_id"] == MEETING_ID
        assert d["version"] == 1
        assert d["path"] == "record/v1.md"
        assert d["segments"] == 2
        assert d["review_segments"] == 1
        assert d["review_ratio"] == pytest.approx(0.5)

    def test_audit_detail_never_contains_transcript_text(
        self, svc, store, db_path, meeting_row
    ):
        """**合规红线**（部署手册 §5.3）：审计 detail 不得含转写正文。

        审计要导出提交保密检查 —— 把正文放进去等于把涉密内容的扩散面
        从加密数据目录扩大到审计报告与备份介质。
        """
        meeting_row()
        secret = "这一段是不允许出现在审计日志里的转写正文内容"
        _write_segments(store, [_seg(1, secret, 0.3)])

        svc.generate_record(now=GENERATED_AT)

        rec = [r for r in query(db_path) if r.action == "record_generate"][0]
        assert secret not in rec.detail_raw
        assert secret not in json.dumps(rec.detail, ensure_ascii=False)

    def test_audit_detail_records_missing_metadata_labels(
        self, svc, store, db_path, meeting_row
    ):
        """只记缺项**标签**（如"会议地点"），不记值。"""
        meeting_row(location=None, host=None)
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)

        rec = [r for r in query(db_path) if r.action == "record_generate"][0]
        assert "metadata_missing" in rec.detail
        assert "会议地点" in rec.detail["metadata_missing"]

    def test_audit_chain_still_valid(self, svc, store, db_path, meeting_row):
        """写审计不能破坏链 —— 每次追加都要正确续链。"""
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)
        svc.generate_record(now=GENERATED_AT)

        v = verify_chain(db_path)
        assert v.ok, v.summary()

    def test_no_audit_logger_warns(self, store, db_path, meeting_row, caplog):
        """未注入 AuditLogger 时打 warning，**不静默吞掉**。"""
        meeting_row()
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc = ReviewService(store, db_path=db_path, thresholds=THRESHOLDS, audit=None)

        with caplog.at_level(logging.WARNING):
            svc.generate_record(now=GENERATED_AT)
        assert any("未注入 AuditLogger" in r.message for r in caplog.records)

    def test_record_generate_registered_in_actions(self):
        from mmwf.infra.audit import ACTIONS, MINIMUM_CHECK_ITEM_ACTIONS

        assert "record_generate" in ACTIONS
        assert "record_generate" in MINIMUM_CHECK_ITEM_ACTIONS["文稿修改记录"]

    def test_audit_written_after_state_change(self, svc, store, db_path, meeting_row):
        """状态推进与审计都要发生（顺序不影响结论，但两者不能缺一）。"""
        meeting_row(status="reviewing")
        _write_segments(store, [_seg(1, "内容够长的一段", 0.95)])
        svc.generate_record(now=GENERATED_AT)

        assert svc.current_status() == STATE_RECORD_READY
        assert any(r.action == "record_generate" for r in query(db_path))


class TestMakeReviewService:
    """工厂：按配置装配，阈值从 `settings.thresholds` 现取。"""

    def _settings(self, tmp_path: Path, low_conf: float = 0.70):
        from types import SimpleNamespace

        data_dir = tmp_path / "factory_data"

        def resolve_path(p):
            return p if Path(p).is_absolute() else (tmp_path / Path(p)).resolve()

        return SimpleNamespace(
            app=SimpleNamespace(data_dir=data_dir),
            thresholds=SimpleNamespace(
                asr=SimpleNamespace(
                    low_confidence=low_conf,
                    warn_avg_confidence=0.85,
                    warn_low_conf_ratio=0.15,
                )
            ),
            resolve_path=resolve_path,
        )

    def test_builds_service(self, tmp_path: Path):
        from mmwf.app.services.review import make_review_service

        settings = self._settings(tmp_path)
        svc = make_review_service(MEETING_ID, settings, operator="lisi")
        assert isinstance(svc, ReviewService)
        assert svc._operator == "lisi"

    def test_threshold_taken_from_settings(self, tmp_path: Path):
        """阈值必须来自 settings（N5.2 可热改），不能在工厂里写死 0.70。"""
        from mmwf.app.services.review import make_review_service

        svc = make_review_service(MEETING_ID, self._settings(tmp_path, 0.93))
        assert svc._thresholds["asr"]["low_confidence"] == 0.93

    def test_db_path_lives_under_data_dir(self, tmp_path: Path):
        from mmwf.app.services.review import make_review_service

        svc = make_review_service(MEETING_ID, self._settings(tmp_path))
        assert svc._db_path == tmp_path / "factory_data" / "mmwf.db"

    def test_store_points_at_meeting(self, tmp_path: Path):
        from mmwf.app.services.review import make_review_service

        svc = make_review_service(MEETING_ID, self._settings(tmp_path))
        assert svc._store.meeting_id == MEETING_ID

    def test_missing_app_section_raises(self, tmp_path: Path):
        from types import SimpleNamespace

        from mmwf.app.services.review import make_review_service

        with pytest.raises(ReviewServiceError, match="app"):
            make_review_service(MEETING_ID, SimpleNamespace())

    def test_missing_thresholds_raises(self, tmp_path: Path):
        from types import SimpleNamespace

        from mmwf.app.services.review import make_review_service

        settings = SimpleNamespace(app=SimpleNamespace(data_dir=tmp_path / "d"))
        with pytest.raises(ReviewServiceError, match="thresholds"):
            make_review_service(MEETING_ID, settings)

    def test_works_with_real_settings_file(self, tmp_path: Path):
        """吃真实的 `config/thresholds.yaml`（回归：配置文件字段没改名）。"""
        from mmwf.app.services.review import make_review_service
        from mmwf.settings import load_settings

        cfg = Path(__file__).resolve().parents[2] / "config"
        settings = load_settings(cfg)
        svc = make_review_service(MEETING_ID, settings)
        assert svc._thresholds["asr"]["low_confidence"] == 0.70


# ===========================================================================
# D. 端到端（3 小时会议 + 20% 目标 + 全链路）
# ===========================================================================

class TestEndToEnd:
    """施工卡验收标准第 1 条的综合场景：3 小时会议复核占比 < 20%。"""

    def _three_hour_transcript(self, svc_store) -> list:
        """造一份 ~3 小时、质量尚可的转写稿（1200 段）。

        使分布接近真实：绝大多数分段合格，少量低分，
        夹杂几处静音与残余噪声。复核占比落在 10% 上下。
        """
        segments = []
        noise_positions = {17, 233, 601, 987}
        silence_positions = {81, 445, 1102}
        lowconf_positions = set(range(50, 1200, 92))       # 约 12 段
        for i in range(1, 1201):
            start = (i - 1) * 9000                          # 每段 9 秒 -> 共 3 小时
            if i in noise_positions:
                segments.append(_seg(i, "嗯", 0.98, start))
            elif i in silence_positions:
                segments.append(_seg(i, "（疑似无有效发言）", 0.93, start,
                                     flags=("silence",)))
            elif i in lowconf_positions:
                segments.append(_seg(i, f"第 {i} 段识别质量偏低的会议内容", 0.48, start))
            else:
                segments.append(
                    _seg(i, f"第 {i} 段正常的会议发言内容，长度足够构成一个分段", 0.93, start)
                )
        return segments

    def test_review_ratio_under_20_percent(self, svc, store, meeting_row):
        """验收标准第 1 条：进入人工复核的段落占比 < 20%。"""
        meeting_row()
        segments = self._three_hour_transcript(store)
        _write_segments(store, segments)

        result = svc.generate_record(now=GENERATED_AT)

        ratio = result.review_count / result.segment_count
        assert result.segment_count == 1200
        assert ratio < 0.20, f"复核占比 {ratio:.1%} 超过 20%（{result.review_count} 段）"

    def test_three_hour_record_metadata_and_timestamps(self, svc, store, meeting_row):
        """同一份稿子：8 项元信息齐 + 正文时间码可抓。"""
        import re

        meeting_row()
        _write_segments(store, self._three_hour_transcript(store))
        result = svc.generate_record(now=GENERATED_AT)
        md = result.markdown

        for _key, label in METADATA_FIELDS:
            assert f"| {label} |" in md
        assert "| 参会人员 | 李四、王五、赵六 |" in md

        stamps = re.findall(r"^\[(\d{2}:\d{2}:\d{2})\] ", md, flags=re.MULTILINE)
        assert len(stamps) == 1200
        # 时间码递增（正文按时间顺序）
        assert stamps == sorted(stamps)

    def test_full_chain_gates_green(self, svc, store, meeting_row):
        """全链路：生成 -> manifest 一致 -> 审计链完好 -> 状态就位。"""
        from mmwf.infra.audit import verify_chain

        meeting_row(status="reviewing")
        _write_segments(store, self._three_hour_transcript(store))
        result = svc.generate_record(now=GENERATED_AT)

        assert store.verify_manifest() == []
        assert svc.current_status() == STATE_RECORD_READY
        assert verify_chain(svc._db_path).ok
        assert store.exists(result.rel_path)
