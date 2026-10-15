"""T1-06 单元测试：切分计划与重叠归并（领域层纯函数）。

对应施工卡 T1-06 的验收标准 1 与 2 的"算法部分"：
  · 2.5 小时音频 → 6 段（1800s 块 + 50s 重叠），无遗漏无越界
  · 重叠区去重正确性

纯函数测试的价值在于**可以穷举边界**。切分是最容易"看起来对、
在某个时长上悄悄出错"的地方 —— 少一段、尾段丢了、
start 不递增导致 ffmpeg 切出重复内容。这些都不报错，只会在
最终文稿里表现为"某几句话没了"，而那时已经无从追溯。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.domain.errors import UnsupportedFormatError  # noqa: E402
from mmwf.domain.ingest.planner import (  # noqa: E402
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_OVERLAP_SECONDS,
    MAX_FILE_BYTES,
    SUPPORTED_EXTENSIONS,
    ChunkPlanItem,
    build_chunk_plan,
    merge_overlapping_texts,
    validate_extension,
)


# ---------------------------------------------------------------------------
# 验收标准 1：2.5 小时 → 6 段，无遗漏无越界
# ---------------------------------------------------------------------------

class TestAcceptanceCriterion1:
    """施工卡原文：投递 2.5 小时音频，chunk_plan 生成 6 段，无遗漏无越界。"""

    DURATION_2H30M_MS = int(2.5 * 3600 * 1000)   # 9_000_000

    def test_2h30m_produces_exactly_6_chunks(self):
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        assert len(plan) == 6, f"期望 6 段，实际 {len(plan)} 段"

    def test_first_chunk_starts_at_zero(self):
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        assert plan[0].start_ms == 0, "首段必须从 0 开始，否则音频开头被静默丢弃"

    def test_last_chunk_ends_at_duration(self):
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        assert plan[-1].end_ms == self.DURATION_2H30M_MS, (
            "末段必须收在音频末尾，否则尾部内容被静默丢弃"
        )

    def test_no_chunk_exceeds_duration(self):
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        overrun = [c.seq for c in plan if c.end_ms > self.DURATION_2H30M_MS]
        assert overrun == [], f"这些段越界：{overrun}"

    def test_starts_are_strictly_increasing(self):
        """start 必须严格递增 —— 否则 ffmpeg 会切出重复内容，块数也算错。"""
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        starts = [c.start_ms for c in plan]
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts), f"存在重复起点：{starts}"

    def test_seq_is_contiguous_from_one(self):
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        assert [c.seq for c in plan] == list(range(1, len(plan) + 1))

    def test_adjacent_chunks_overlap_by_configured_amount(self):
        """相邻段必须真的重叠（除末段外），否则切在句中会丢字。"""
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        for a, b in zip(plan, plan[1:]):
            overlap = a.end_ms - b.start_ms
            if b.end_ms == self.DURATION_2H30M_MS:
                # 末段可能因收尾而变长，重叠 >= 配置值即可
                assert overlap >= DEFAULT_OVERLAP_SECONDS * 1000
            else:
                assert overlap == DEFAULT_OVERLAP_SECONDS * 1000, (
                    f"段 {a.seq}->{b.seq} 重叠为 {overlap}ms，"
                    f"期望 {DEFAULT_OVERLAP_SECONDS * 1000}ms"
                )

    def test_no_coverage_gap(self):
        """**无遗漏**的严格形式：每一毫秒都被某一段覆盖。"""
        plan = build_chunk_plan(self.DURATION_2H30M_MS)
        covered = 0
        for c in plan:
            assert c.start_ms <= covered, f"段 {c.seq} 起点 {c.start_ms} > 已覆盖 {covered}"
            covered = max(covered, c.end_ms)
        assert covered == self.DURATION_2H30M_MS


# ---------------------------------------------------------------------------
# 切分边界
# ---------------------------------------------------------------------------

class TestChunkPlanBoundaries:
    @pytest.mark.parametrize(
        "duration_s,expected_chunks",
        [
            (0, 0),            # 零长 → 空
            (1, 1),            # 1 秒
            (1799, 1),         # 略短于一块
            (1800, 1),         # 正好一块 → 不切
            (1801, 2),         # 略超一块 → 必须切
            (3600, 3),         # 2 块整 → 3 段（1750+1800 的步进）
            (5400, 4),
            (9000, 6),         # 2.5 小时
        ],
    )
    def test_chunk_counts(self, duration_s: int, expected_chunks: int):
        plan = build_chunk_plan(duration_s * 1000)
        assert len(plan) == expected_chunks, (
            f"{duration_s}s 期望 {expected_chunks} 段，实际 {len(plan)} 段"
        )

    def test_exactly_one_chunk_needs_no_splitting(self):
        """刚好一块时**不做无谓切分** —— 多切一次多一次解码开销与边界风险。"""
        plan = build_chunk_plan(1800 * 1000)
        assert len(plan) == 1
        assert plan[0].start_ms == 0 and plan[0].end_ms == 1800 * 1000

    def test_one_ms_audio(self):
        plan = build_chunk_plan(1)
        assert len(plan) == 1
        assert plan[0].start_ms == 0 and plan[0].end_ms == 1

    def test_zero_duration_returns_empty(self):
        """返回空而不是抛错：让"探测失败"与"真没内容"可区分。"""
        assert build_chunk_plan(0) == []

    def test_custom_chunk_and_overlap(self):
        plan = build_chunk_plan(300 * 1000, chunk_seconds=120, overlap_seconds=20)
        assert len(plan) == 3
        assert plan[0].start_ms == 0 and plan[0].end_ms == 120_000
        assert plan[1].start_ms == 100_000 and plan[1].end_ms == 220_000
        assert plan[2].start_ms == 200_000 and plan[2].end_ms == 300_000

    def test_no_overlap_config_still_covers(self):
        """重叠可为 0（只要 < chunk）—— 老配置兼容。"""
        plan = build_chunk_plan(100 * 1000, chunk_seconds=40, overlap_seconds=0)
        assert len(plan) == 3
        assert plan[0].end_ms == plan[1].start_ms, "无重叠时相邻段应首尾相接"

    def test_very_long_audio_terminates(self):
        """24 小时音频必须能算完 —— 防"步进 <= 0 死循环"复发。"""
        plan = build_chunk_plan(24 * 3600 * 1000)
        assert len(plan) > 1
        assert plan[-1].end_ms == 24 * 3600 * 1000

    @pytest.mark.parametrize("bad", [-1, -1000])
    def test_negative_duration_rejected(self, bad: int):
        with pytest.raises(ValueError, match="不能为负"):
            build_chunk_plan(bad)

    def test_overlap_not_less_than_chunk_rejected(self):
        """重叠 >= 块长会让 step <= 0 → 死循环。必须挡住。"""
        with pytest.raises(ValueError, match="必须小于"):
            build_chunk_plan(100_000, chunk_seconds=60, overlap_seconds=60)
        with pytest.raises(ValueError, match="必须小于"):
            build_chunk_plan(100_000, chunk_seconds=60, overlap_seconds=90)

    def test_nonpositive_chunk_rejected(self):
        with pytest.raises(ValueError, match="必须为正"):
            build_chunk_plan(100_000, chunk_seconds=0)
        with pytest.raises(ValueError, match="必须为正"):
            build_chunk_plan(100_000, chunk_seconds=-10)

    def test_negative_overlap_rejected(self):
        with pytest.raises(ValueError, match="不能为负"):
            build_chunk_plan(100_000, chunk_seconds=60, overlap_seconds=-1)


class TestChunkPlanItem:
    def test_duration_property(self):
        assert ChunkPlanItem(seq=1, start_ms=100, end_ms=500).duration_ms == 400

    def test_to_dict_matches_sad_field_names(self):
        """SAD §9.2 的 chunk_plan 字段是 seq/start_ms/end_ms —— 必须一致。"""
        d = ChunkPlanItem(seq=2, start_ms=1750000, end_ms=3600000).to_dict()
        assert d == {"seq": 2, "start_ms": 1750000, "end_ms": 3600000}

    def test_seq_must_start_at_one(self):
        with pytest.raises(ValueError, match="seq 必须从 1 开始"):
            ChunkPlanItem(seq=0, start_ms=0, end_ms=100)

    def test_inverted_range_rejected(self):
        with pytest.raises(ValueError, match="区间无效"):
            ChunkPlanItem(seq=1, start_ms=500, end_ms=500)
        with pytest.raises(ValueError, match="区间无效"):
            ChunkPlanItem(seq=1, start_ms=500, end_ms=100)

    def test_negative_start_rejected(self):
        with pytest.raises(ValueError, match="起点为负"):
            ChunkPlanItem(seq=1, start_ms=-1, end_ms=100)

    def test_frozen(self):
        c = ChunkPlanItem(seq=1, start_ms=0, end_ms=100)
        with pytest.raises(Exception):
            c.start_ms = 50  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 验收标准 3：格式校验 → E1001
# ---------------------------------------------------------------------------

class TestValidateExtension:
    @pytest.mark.parametrize("ext", sorted(SUPPORTED_EXTENSIONS))
    def test_all_supported_formats_accepted(self, ext: str):
        assert validate_extension(f"录音{ext}") == ext

    def test_uppercase_normalised(self):
        """`A.M4A` 与 `a.m4a` 必须判成同一种格式。

        录音笔经常大写扩展名；不归一化会让"同一批文件里有一半被判不支持"。
        """
        assert validate_extension("会议录音.M4A") == ".m4a"
        assert validate_extension("MEETING.WAV") == ".wav"

    def test_unsupported_raises_e1001(self):
        with pytest.raises(UnsupportedFormatError) as ei:
            validate_extension("纪要.docx")
        assert ei.value.code == "E1001"
        assert ".docx" in ei.value.detail

    def test_error_message_lists_supported_formats(self):
        """报错要**告诉用户怎么办**（列举支持格式），而不是只说"不支持"。"""
        with pytest.raises(UnsupportedFormatError) as ei:
            validate_extension("a.mp4")
        for ext in SUPPORTED_EXTENSIONS:
            assert ext in ei.value.detail

    def test_user_message_has_no_technical_detail(self):
        """N3.3：给用户看的文案不得含路径/参数等技术细节。"""
        with pytest.raises(UnsupportedFormatError) as ei:
            validate_extension("D:/some/secret/path.docx")
        assert "D:/some/secret/path" not in ei.value.user_message
        assert ei.value.user_message.strip().endswith("。")

    def test_no_extension_raises(self):
        with pytest.raises(UnsupportedFormatError) as ei:
            validate_extension("没有扩展名的文件")
        assert ei.value.code == "E1001"

    def test_dotfile_without_ext_raises(self):
        with pytest.raises(UnsupportedFormatError):
            validate_extension("录音.")

    def test_to_dict_hides_detail(self):
        with pytest.raises(UnsupportedFormatError) as ei:
            validate_extension("x.exe")
        d = ei.value.to_dict()
        assert "detail" not in d
        assert set(d) == {"code", "title", "message", "severity"}

    def test_multi_dot_filename_uses_last_suffix(self):
        """`会议.20260927.m4a` 的扩展名是 `.m4a` 不是 `.20260927.m4a`。"""
        assert validate_extension("会议.20260927.m4a") == ".m4a"


# ---------------------------------------------------------------------------
# 验收标准 2：重叠区去重
# ---------------------------------------------------------------------------

class TestMergeOverlappingTexts:
    def test_single_piece_passthrough(self):
        text, decisions = merge_overlapping_texts([(0, 1000, "同志们今天我们开会")])
        assert text == "同志们今天我们开会"
        assert decisions == []

    def test_empty_input(self):
        assert merge_overlapping_texts([]) == ("", [])

    def test_no_overlap_concatenates(self):
        pieces = [(0, 1000, "同志们"), (1000, 2000, "今天我们开会")]
        text, decisions = merge_overlapping_texts(pieces)
        assert text == "同志们今天我们开会"
        assert decisions[0]["deduped"] is False
        assert decisions[0]["reason"] == "no_overlap_in_timeline"

    def test_overlap_removes_duplicated_prefix(self):
        """**验收标准 2 的核心**：重叠区被转写两遍，必须只保留一份。"""
        pieces = [
            (0, 2000, "同志们今天我们召开党组会"),
            (1500, 3000, "召开党组会研究部署重点工作"),
        ]
        text, decisions = merge_overlapping_texts(pieces)
        assert text == "同志们今天我们召开党组会研究部署重点工作", text
        assert decisions[0]["deduped"] is True
        assert decisions[0]["matched_chars"] == 5      # "召开党组会"
        assert decisions[0]["overlap_ms"] == 500

    def test_full_duplicate_segment(self):
        """后段完全被前段包含（重叠配得过大）→ 完全去重，不重复两遍。"""
        pieces = [
            (0, 3000, "同志们今天我们召开党组会"),
            (1000, 3000, "今天我们召开党组会"),
        ]
        text, _ = merge_overlapping_texts(pieces)
        assert text == "同志们今天我们召开党组会"

    def test_three_pieces_chain(self):
        pieces = [
            (0, 2000, "第一句第二句"),
            (1500, 3500, "第二句第三句"),
            (3000, 5000, "第三句第四句"),
        ]
        text, decisions = merge_overlapping_texts(pieces)
        assert text == "第一句第二句第三句第四句", text
        assert all(d["deduped"] for d in decisions)

    def test_no_match_keeps_verbatim_conservatively(self):
        """**保守策略**：找不到公共部分时原样保留，绝不凭空删字。

        纪要场景下"多一句"和"少一句"严重性完全不对等 ——
        宁可留一点重复让人看出来，也不能悄悄吞掉内容。
        """
        pieces = [
            (0, 2000, "完全无关的内容甲"),
            (1500, 3000, "另一段毫不相干的话"),
        ]
        text, decisions = merge_overlapping_texts(pieces)
        assert text == "完全无关的内容甲另一段毫不相干的话"
        assert decisions[0]["deduped"] is False
        assert decisions[0]["reason"] == "no_match_kept_verbatim"

    def test_decisions_are_recorded_for_audit(self):
        """去重决定必须留痕 —— 否则"为什么这句话没了"无法追查。"""
        pieces = [(0, 2000, "甲乙丙丁"), (1500, 3000, "丙丁戊己")]
        _, decisions = merge_overlapping_texts(pieces)
        d = decisions[0]
        assert set(d) >= {"between", "overlap_ms", "deduped", "matched_chars", "reason"}
        assert d["between"] == [1, 2]      # 1-based，与 seq 一致

    def test_english_and_punctuation(self):
        pieces = [
            (0, 2000, "Hello world"),
            (1500, 3000, "world again"),
        ]
        text, d = merge_overlapping_texts(pieces)
        assert text == "Hello world again"
        assert d[0]["matched_chars"] == len("world")

    def test_punctuation_boundary(self):
        """标点也参与匹配 —— 引擎常在边界处补/丢标点。"""
        pieces = [
            (0, 2000, "同志们，今天我们开会"),
            (1500, 3000, "今天我们开会。研究三件事"),
        ]
        text, d = merge_overlapping_texts(pieces)
        assert text == "同志们，今天我们开会。研究三件事", text

    def test_does_not_strip_when_prefix_only_coincidence_short(self):
        """单字巧合不该被当成重叠**大幅**削减内容。"""
        pieces = [(0, 2000, "今天开党组会"), (1500, 3000, "天要下雨了")]
        text, d = merge_overlapping_texts(pieces)
        # 匹配到 "天" 1 个字，去掉它
        assert d[0]["matched_chars"] <= 2
        assert "要下雨了" in text

    def test_lookback_is_bounded_for_perf(self):
        """超长重复不该退化成 O(n²)：回看窗口有上限。"""
        long_text = "甲" * 5000
        pieces = [(0, 2000, long_text), (1500, 3000, long_text)]
        text, _ = merge_overlapping_texts(pieces)
        # 至少不能因为窗口无限而卡住；结果应该是"删掉一部分后接上"
        assert isinstance(text, str)
        assert len(text) >= len(long_text)

    def test_zero_overlap_ms_with_equal_boundary(self):
        """时间上恰好首尾相接（overlap == 0）→ 不做去重。"""
        pieces = [(0, 1000, "甲乙"), (1000, 2000, "丙丁")]
        text, d = merge_overlapping_texts(pieces)
        assert text == "甲乙丙丁"
        assert d[0]["overlap_ms"] == 0


class TestMaxFileBytes:
    def test_is_4gib(self):
        assert MAX_FILE_BYTES == 4 * 1024 ** 3


class TestConstants:
    def test_defaults_match_sad(self):
        """施工卡与 SAD 都写 1800s 块 + 50s 重叠，不能悄悄改。"""
        assert DEFAULT_CHUNK_SECONDS == 1800
        assert DEFAULT_OVERLAP_SECONDS == 50

    def test_supported_formats_cover_sad_requirement(self):
        """SAD §12.4 明确要求 wav/mp3/m4a/wma/amr 全覆盖。"""
        for ext in (".wav", ".mp3", ".m4a", ".wma", ".amr"):
            assert ext in SUPPORTED_EXTENSIONS
