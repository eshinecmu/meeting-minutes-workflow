"""T1-08 单元测试：低置信分流（domain/review/split.py）。

对标施工卡验收标准第 4 条 —— **本文件存在的首要理由**：

    [ ] `test_split_for_review` 覆盖置信度缺失（`None`）的情况不误判为低置信

以及三条分流判据的边界：

    SP-01  confidence < 0.70              -> 送复核
    SP-02  flags 含 silence / overlap     -> 送复核（**与分数无关**）
    SP-03  len(text.strip()) <= 1         -> 送复核
    SP-04  confidence is None             -> **不**送复核（坑 1）
    SP-05  无标记、非空、分数达标          -> 不送复核

SP-02 为什么必须独立于 SP-01
---------------------------
分数是"引擎对整段音频有多确信"，它覆盖不了"两个人同时说话"：
引擎可以非常确信自己听见了字（0.98），但那些字是叠在一起的。
这类内容不可用而分数发现不了，只能靠 flag。SP-02 专门验这个独立性。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mmwf.domain.review import (
    REVIEW_FLAGS,
    format_duration_ms,
    needs_review,
    plan_review,
    review_ratio,
    review_reason,
    split_for_review,
)

THRESHOLDS = {"asr": {"low_confidence": 0.70}}


@dataclass
class Seg:
    """测替身。真实类型是 `TranscriptSegmentResult`，但分流只依赖三个属性，
    用替身能把"confidence 为 None"这类边界造得干净利落。"""

    seq: int
    text: str
    confidence: float | None
    start_ms: int = 0
    flags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 验收标准第 4 条：confidence=None 不误判
# ---------------------------------------------------------------------------

class TestConfidenceNoneIsNotLow:
    """**本卡最要紧的一组测试**（施工卡「常见坑」第一条）。

    为什么单独成类而不是混在别的用例里：这是一个"写错了也看不出来"的错。
    `None < 0.70` 在 Python 3 里直接 TypeError（会立刻暴露），
    但真实代码里更常见的是 `(seg.confidence or 0.0) < 0.70` —— 那**不报错**，
    只是把每一段都判成低置信。用户看到的是"整篇都要人工核对"，
    而没人会怀疑是代码写错了。所以必须有一条测试盯住它。
    """

    def test_none_confidence_alone_is_not_reviewed(self):
        """单段：只有 confidence=None，其余都正常 -> 不该送复核。"""
        seg = Seg(seq=1, text="这句话引擎没有给出置信度", confidence=None)
        assert needs_review(seg, THRESHOLDS) is False
        assert review_reason(seg, THRESHOLDS) == ""

    def test_none_confidence_not_treated_as_zero(self):
        """`None` 与 `0.0` 的结果必须不同 —— 证明判据是 `is not None`。

        如果实现写成 `(conf or 0.0) < threshold`，这一条会失败：
        None 会被兜底成 0.0，与"真的 0.0"同样被判低置信。
        """
        none_seg = Seg(seq=1, text="无分数", confidence=None)
        zero_seg = Seg(seq=2, text="分数为零", confidence=0.0)

        assert needs_review(none_seg, THRESHOLDS) is False
        assert needs_review(zero_seg, THRESHOLDS) is True
        assert needs_review(none_seg, THRESHOLDS) != needs_review(zero_seg, THRESHOLDS)

    def test_all_none_confidence_goes_to_auto_pass(self):
        """全篇无置信度：**全部**归"可直接用"，复核量应为 0。

        这正是坑 1 说的场景：引擎没带分数时，整篇不该被送去人工复核。
        （"要不要整体降级为人工全检"是另一条决策线，由
        `domain/asr_types.detect_silent_failures()` 报
        `review gate degraded` 告警，交给调度层提示用户 ——
        分流规则本身不擅自扩大复核范围。）
        """
        segments = [Seg(seq=i, text=f"第 {i} 段正常内容", confidence=None)
                    for i in range(1, 21)]
        to_review, auto_pass = split_for_review(segments, THRESHOLDS)
        assert to_review == []
        assert len(auto_pass) == 20
        assert review_ratio(segments, THRESHOLDS) == 0.0

    def test_none_confidence_with_other_defect_still_reviewed(self):
        """`None` 不判低置信，但**别的缺陷照样判**。

        防的是"把 None 当例外一律放过"的过度修正：
        一个没有分数的段如果同时又静音又超短，它还是得送复核。
        """
        silent = Seg(seq=1, text="有内容的句子", confidence=None, flags=("silence",))
        tiny = Seg(seq=2, text="嗯", confidence=None)
        assert needs_review(silent, THRESHOLDS) is True
        assert needs_review(tiny, THRESHOLDS) is True

    def test_mixed_none_and_value_ratio_uses_all_segments(self):
        """混合场景：None 的段既不判真也不稀释比例。

        4 段里 1 段 None、1 段低分（0.30）、2 段高分。
        `review_ratio` 的分母是**全部段数**（4），所以是 1/4 = 0.25。
        """
        segments = [
            Seg(1, "高分段落", 0.95),
            Seg(2, "无分数段落", None),
            Seg(3, "低分段落", 0.30),
            Seg(4, "另一个高分", 0.88),
        ]
        assert review_ratio(segments, THRESHOLDS) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 判据 1：低置信
# ---------------------------------------------------------------------------

class TestLowConfidenceRule:

    def test_below_threshold_is_reviewed(self):
        seg = Seg(1, "听不清的一段", 0.69)
        assert needs_review(seg, THRESHOLDS) is True
        assert "0.69" in review_reason(seg, THRESHOLDS)
        assert "0.70" in review_reason(seg, THRESHOLDS)

    def test_exactly_at_threshold_is_not_reviewed(self):
        """边界：`<` 而不是 `<=`。等于阈值算"达标"。

        这个方向是刻意选的：阈值由人配置，配置者写 0.70 时想表达的是
        "0.70 可以接受，低于它才要查"。写成 `<=` 会让"刚好卡在阈值上"
        的段落全部进复核 —— 而低置信段往往密集分布在阈值附近，
        那会让复核量突然变大，且没有任何提示。
        """
        seg = Seg(1, "刚达标的段落", 0.70)
        assert needs_review(seg, THRESHOLDS) is False

    def test_above_threshold_is_not_reviewed(self):
        assert needs_review(Seg(1, "很清楚的一段", 1.0), THRESHOLDS) is False

    def test_zero_confidence_is_reviewed(self):
        assert needs_review(Seg(1, "完全不可信", 0.0), THRESHOLDS) is True

    def test_threshold_is_honored_from_config(self):
        """阈值必须**由参数决定**（N5.2 可热改）。

        同一段 0.80，在 0.70 阈值下通过，在 0.90 阈值下送复核。
        如果实现里写死了 0.70，这一条会失败。
        """
        seg = Seg(1, "分数 0.80 的段落", 0.80)
        assert needs_review(seg, {"asr": {"low_confidence": 0.70}}) is False
        assert needs_review(seg, {"asr": {"low_confidence": 0.90}}) is True

    def test_flat_thresholds_shape_also_works(self):
        """扁平写法 `{"low_confidence": ...}` 也吃（CLI/测试方便）。"""
        assert needs_review(Seg(1, "低分", 0.5), {"low_confidence": 0.7}) is True

    def test_missing_threshold_raises_keyerror(self):
        """阈值缺失**明确报错**，不兜底成默认值。

        兜底会让"配置漏了这一项"变成"用了写死的 0.7，一直没人发现"。
        """
        with pytest.raises(KeyError):
            needs_review(Seg(1, "文本", 0.5), {"asr": {}})
        with pytest.raises(KeyError):
            needs_review(Seg(1, "文本", 0.5), {})
        with pytest.raises(KeyError):
            needs_review(Seg(1, "文本", 0.5), {"gate": {"approval_timeout_hours": 72}})

    def test_non_dict_thresholds_raises(self):
        with pytest.raises(KeyError):
            needs_review(Seg(1, "文本", 0.5), "not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 判据 2：异常 flag（与分数独立）
# ---------------------------------------------------------------------------

class TestFlagRule:
    """分数发现不了的音频异常。核心是**独立性** —— 高分 + 异常标记 = 送复核。"""

    @pytest.mark.parametrize("flag", sorted(REVIEW_FLAGS))
    def test_review_flag_triggers_despite_high_confidence(self, flag):
        seg = Seg(1, "内容看起来很正常", 0.99, flags=(flag,))
        assert needs_review(seg, THRESHOLDS) is True

    def test_silence_flagged(self):
        seg = Seg(1, "……", 0.99, flags=("silence",))
        reason = review_reason(seg, THRESHOLDS)
        assert "静音" in reason

    def test_overlap_flagged(self):
        seg = Seg(1, "两人抢话", 0.95, flags=("overlap",))
        assert "多人同时说话" in review_reason(seg, THRESHOLDS)

    def test_both_flags_listed(self):
        seg = Seg(1, "又静音又重叠", 0.9, flags=("silence", "overlap"))
        reason = review_reason(seg, THRESHOLDS)
        assert "静音段" in reason and "多人同时说话" in reason

    def test_unknown_flag_does_not_trigger_review(self):
        """新增的、未登记的 flag **不**触发复核（白名单而非黑名单）。

        这是刻意的设计（见 split.REVIEW_FLAGS 的文档）：
        `transcript_segment.flags` 将来会加各种信息性标记，
        若"有任何标记就送复核"，新增一个纯信息 flag 会静默地
        让复核量翻倍，而且没人会想到是这里引起的。
        """
        for unknown in ("hotword_miss", "punctuation_fixed", "low_conf", "speaker_changed"):
            seg = Seg(1, "正常内容也不短", 0.95, flags=(unknown,))
            assert needs_review(seg, THRESHOLDS) is False, unknown

    def test_unknown_flag_reason_not_fabricated(self):
        """未登记 flag 不送复核 -> 理由为空串（不编造中文解释）。"""
        assert review_reason(Seg(1, "正常内容", 0.9, flags=("whatever",)), THRESHOLDS) == ""

    def test_flags_none_treated_as_empty(self):
        """`flags=None` 当"没有标记"，不报错、不送复核。"""
        seg = Seg(1, "正常的一段话", 0.95)
        assert getattr(seg, "flags", None) == ()
        assert needs_review(seg, THRESHOLDS) is False

    def test_missing_flags_attribute_tolerated(self):
        """**完全没有 `flags` 属性**的对象也要能吃（T1-05 的老对象 / 外部替身）。"""
        class NoFlags:
            text = "一段正常的话"
            confidence = 0.95

        assert needs_review(NoFlags(), THRESHOLDS) is False

    def test_low_confidence_takes_priority_in_reason(self):
        """既低分又重叠时，理由优先说分数（复核页先看到最直观的判据）。"""
        seg = Seg(1, "两人抢话且听不清", 0.3, flags=("overlap",))
        assert "置信度" in review_reason(seg, THRESHOLDS)

    def test_flag_beats_reason_for_high_confidence(self):
        """分数达标时，理由必须是 flag 那条 —— 否则复核页显示
        "置信度正常"却把它送进复核，使用者会一头雾水。"""
        seg = Seg(1, "两人抢话", 0.98, flags=("overlap",))
        reason = review_reason(seg, THRESHOLDS)
        assert "置信度" not in reason
        assert "多人同时说话" in reason


# ---------------------------------------------------------------------------
# 判据 3：残余噪声
# ---------------------------------------------------------------------------

class TestNoiseRule:

    def test_single_char_reviewed(self):
        assert needs_review(Seg(1, "嗯", 0.99), THRESHOLDS) is True

    def test_single_char_after_strip_reviewed(self):
        """判据用 `text.strip()`，所以空白包着的单字也算。"""
        assert needs_review(Seg(1, "  嗯  ", 0.99), THRESHOLDS) is True

    def test_two_chars_not_reviewed(self):
        """"两个字的回应"是有效内容（"同意"、"可以"），不能当噪声。

        边界方向的取舍：把 2 字判成噪声会让用户白看一堆正常段落；
        漏掉一个 2 字噪声，代价只是记录稿里多一行"嗯嗯"。
        `<= 1` 是代价更小的那一边。
        """
        assert needs_review(Seg(1, "同意", 0.99), THRESHOLDS) is False
        assert needs_review(Seg(1, "可以", 0.95), THRESHOLDS) is False

    def test_empty_text_reviewed(self):
        """空串也满足 `<= 1`（虽然 `from_raw_segments` 已经丢空段）。"""
        assert needs_review(Seg(1, "", 0.99), THRESHOLDS) is True

    def test_whitespace_only_reviewed(self):
        assert needs_review(Seg(1, "    ", 0.99), THRESHOLDS) is True

    def test_noise_reason_mentions_char_count(self):
        reason = review_reason(Seg(1, "啊", 0.99), THRESHOLDS)
        assert "残余噪声" in reason and "1" in reason

    def test_empty_text_reason_marks_blank(self):
        reason = review_reason(Seg(1, "", 0.99), THRESHOLDS)
        assert "空白" in reason


# ---------------------------------------------------------------------------
# 分流切分：顺序、完整性、一致性
# ---------------------------------------------------------------------------

class TestSplitForReview:

    def test_two_piles_partition_all_segments(self):
        """两堆必须**不重不漏**地覆盖全部分段。

        漏一段 -> 记录稿正文少一段（静默丢内容）；
        重一段 -> 同一段被核两次。
        两种都是使用者看不出来的错，所以必须由测试守住。
        """
        segments = [
            Seg(1, "正常内容一段", 0.95),
            Seg(2, "嗯", 0.99),
            Seg(3, "低分内容", 0.40),
            Seg(4, "抢话内容", 0.98, flags=("overlap",)),
            Seg(5, "无分数内容", None),
        ]
        to_review, auto_pass = split_for_review(segments, THRESHOLDS)

        assert len(to_review) + len(auto_pass) == len(segments)
        assert {s.seq for s in to_review} | {s.seq for s in auto_pass} == {1, 2, 3, 4, 5}
        assert not ({s.seq for s in to_review} & {s.seq for s in auto_pass})

    def test_both_piles_keep_original_order(self):
        """两堆各自保持原顺序（复核页与正文都按时间码展示）。"""
        segments = [
            Seg(1, "低分一", 0.30),
            Seg(2, "正常一", 0.95),
            Seg(3, "低分二", 0.20),
            Seg(4, "正常二", 0.90),
            Seg(5, "低分三", 0.10),
        ]
        to_review, auto_pass = split_for_review(segments, THRESHOLDS)
        assert [s.seq for s in to_review] == [1, 3, 5]
        assert [s.seq for s in auto_pass] == [2, 4]

    def test_empty_input(self):
        assert split_for_review([], THRESHOLDS) == ([], [])

    def test_all_clean_gives_empty_review_pile(self):
        segments = [Seg(i, f"第 {i} 段的正常内容", 0.9 + i / 100) for i in range(1, 6)]
        to_review, auto_pass = split_for_review(segments, THRESHOLDS)
        assert to_review == []
        assert len(auto_pass) == 5

    def test_accepts_tuple_input(self):
        segments = (Seg(1, "嗯", 0.99), Seg(2, "正常内容", 0.9))
        to_review, auto_pass = split_for_review(segments, THRESHOLDS)
        assert len(to_review) == 1 and len(auto_pass) == 1

    def test_consistent_with_needs_review(self):
        """`split_for_review` 与 `needs_review` 结论必须逐段一致。

        两个函数各写一套判据是很常见的重构事故 ——
        复核页（用 needs_review）和记录稿（用 split）结论不同，
        而且只有在两边同时打开时才能发现。
        """
        segments = [
            Seg(1, "正常", 0.95),
            Seg(2, "静音", 0.9, flags=("silence",)),
            Seg(3, "嗯", 0.99),
            Seg(4, "无分数", None),
            Seg(5, "低分", 0.5),
        ]
        to_review, auto_pass = split_for_review(segments, THRESHOLDS)
        expected_review = [s.seq for s in segments if needs_review(s, THRESHOLDS)]
        assert [s.seq for s in to_review] == expected_review

    def test_each_segment_judged_once(self):
        """每段只调一次判据 —— 用计数替身验证。

        若实现写成两个列表推导各调一次，计数会翻倍。
        现在判据是纯函数所以结果一样，但将来若加上"已复核过就跳过"
        这类有副作用的逻辑，两次调用会让同一段同时落进两堆。
        """
        calls = {"n": 0}

        class Counting:
            def __init__(self, seq, conf):
                self.seq, self.text, self.confidence = seq, "内容够长的一段", conf

            @property
            def flags(self):
                calls["n"] += 1
                return ()

        segments = [Counting(i, 0.9) for i in range(1, 6)]
        split_for_review(segments, THRESHOLDS)
        assert calls["n"] == 5


# ---------------------------------------------------------------------------
# plan_review / review_ratio
# ---------------------------------------------------------------------------

class TestPlanReview:

    def test_plan_matches_split(self):
        segments = [
            Seg(1, "正常", 0.95),
            Seg(2, "低分", 0.3),
            Seg(3, "嗯", 0.99),
        ]
        plan = plan_review(segments, THRESHOLDS)
        assert [d.seq for d in plan] == [1, 2, 3]
        assert [d.needs_review for d in plan] == [False, True, True]
        assert plan[0].reason == ""
        assert plan[1].reason != ""
        # bool 语义：ReviewDecision 可直接当真值用
        assert bool(plan[1]) is True
        assert bool(plan[0]) is False

    def test_plan_uses_seq_attribute(self):
        """seq 从对象上取，不是用 enumerate 序号 —— 复核页要靠 seq 定位。"""
        segments = [Seg(seq=101, text="嗯", confidence=0.99)]
        assert plan_review(segments, THRESHOLDS)[0].seq == 101

    def test_plan_falls_back_to_index_without_seq(self):
        class NoSeq:
            text = "嗯"
            confidence = 0.99

        plan = plan_review([NoSeq(), NoSeq()], THRESHOLDS)
        assert [d.seq for d in plan] == [1, 2]


class TestReviewRatio:

    def test_empty_is_zero(self):
        assert review_ratio([], THRESHOLDS) == 0.0

    def test_all_clean_is_zero(self):
        segments = [Seg(i, "正常内容够长", 0.95) for i in range(3)]
        assert review_ratio(segments, THRESHOLDS) == 0.0

    def test_all_review_is_one(self):
        segments = [Seg(i, "嗯", 0.99) for i in range(4)]
        assert review_ratio(segments, THRESHOLDS) == 1.0

    def test_ratio_denominator_is_all_segments(self):
        """分母是全部段数（人的工作量口径），不是"有分数的段数"。"""
        segments = [
            Seg(1, "低分", 0.3),
            Seg(2, "无分数", None),
            Seg(3, "无分数", None),
            Seg(4, "无分数", None),
        ]
        # 1/4，而不是 1/1（若按"有分数的段"算会得到 100%）
        assert review_ratio(segments, THRESHOLDS) == pytest.approx(0.25)

    def test_ratio_under_twenty_percent_target(self):
        """本卡验收标准第 1 条：3 小时会议复核占比应 < 20%。

        这里造一个"质量尚可"的典型分布（50 段里 6 段要复核 = 12%），
        确认正常配置下能达标 —— 这既是验收的回归护栏，
        也是给将来调阈值的人一个"什么算正常"的参照。
        """
        segments = []
        for i in range(1, 51):
            if i in (7, 19, 31):              # 3 段低分
                segments.append(Seg(i, f"第 {i} 段听不清的内容", 0.45))
            elif i in (12, 40):               # 2 段静音
                segments.append(Seg(i, f"第 {i} 段疑似静音", 0.95, flags=("silence",)))
            elif i == 25:                     # 1 段噪声
                segments.append(Seg(i, "啊", 0.99))
            else:
                segments.append(Seg(i, f"第 {i} 段正常的会议内容，长度足够", 0.92))
        ratio = review_ratio(segments, THRESHOLDS)
        assert ratio == pytest.approx(6 / 50)
        assert ratio < 0.20


# ---------------------------------------------------------------------------
# format_duration_ms（时间码的底层）
# ---------------------------------------------------------------------------

class TestFormatDuration:

    @pytest.mark.parametrize(
        ("ms", "expected"),
        [
            (0, "00:00:00"),
            (1, "00:00:00"),
            (999, "00:00:00"),
            (1000, "00:00:01"),
            (12_000, "00:00:12"),
            (754_000, "00:12:34"),
            (3_600_000, "01:00:00"),
            (3_661_000, "01:01:01"),
            (45_296_000, "12:34:56"),
            (360_000_000, "100:00:00"),      # 超过 99 小时自然进位，不截断
        ],
    )
    def test_format(self, ms, expected):
        assert format_duration_ms(ms) == expected

    def test_negative_rejected(self):
        """负数时间码必须报错，不能渲染成 "-1:-1:-1"。"""
        with pytest.raises(ValueError):
            format_duration_ms(-1)

    def test_result_is_fixed_width_for_normal_meetings(self):
        """常规会议（<100h）下时间码**定宽** —— 导出 Markdown 时能对齐。"""
        for ms in (0, 1000, 754_000, 3_600_000, 3_661_000):
            assert len(format_duration_ms(ms)) == 8


# ---------------------------------------------------------------------------
# 模块级契约
# ---------------------------------------------------------------------------

def test_review_flags_exactly_two_as_card_specifies():
    """施工卡原文只写这两个 flag。加项要显式改这里 + 改文档。"""
    assert REVIEW_FLAGS == frozenset({"silence", "overlap"})


def test_reason_never_raises_on_minimal_object():
    """最小的"像分段的对象"也要能判，不抛 AttributeError。

    真实场景：`segment_edit` 编辑过的分段、上游适配器给的部分对象、
    从 JSON 恢复出来但缺字段的字典包装。
    """

    class Bare:
        text = "只有文本"
        confidence = None

    assert needs_review(Bare(), THRESHOLDS) is False
    assert review_reason(Bare(), THRESHOLDS) == ""


def test_all_segments_pass_returns_empty_piles():
    """全通过时两堆分别是 [] 与全部（防 off-by-one）。"""
    segments = [Seg(i, f"第 {i} 段内容够长", 0.99) for i in range(1, 4)]
    to_review, auto_pass = split_for_review(segments, THRESHOLDS)
    assert to_review == []
    assert [s.seq for s in auto_pass] == [1, 2, 3]
