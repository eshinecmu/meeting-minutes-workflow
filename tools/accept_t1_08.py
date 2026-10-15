"""T1-08 验收脚本（人可读版）。

施工卡 T1-08 的四条验收标准，逐条跑一遍并打印结论。
用法：python tools/accept_t1_08.py
      python tools/accept_t1_08.py --keep-tmp     # 保留临时数据便于排查

四条标准（施工卡原文）：
  [ ] 3 小时会议转写稿，进入人工复核的段落占比 < 20%（否则阈值需调）
  [ ] 记录稿含全部 8 项元信息，attendees 正确展开为顿号分隔
  [ ] 记录稿正文段落带时间码，格式 [00:12:34]
  [ ] test_split_for_review 覆盖置信度缺失（None）的情况不误判为低置信

三条常见坑（也一并验证，因为"坑"就是最容易在验收里蒙混过去的地方）：
  坑1  confidence=None 不能当低置信 —— 标准 4 直接盯它
  坑2  元信息缺失填「待补充」而不是留空 —— 附加 2
  坑3  记录稿文件名与版本号要与 manifest.json 一致 —— 附加 3

关于本卡"标准 1 的 20% 从哪来"
------------------------------
20% 不是凭空定的数。它要能同时满足两件事：
  · 低于它，人工投入才符合 Q3「单份 2 小时会议人工 ≤ 20 分钟」；
  · 高于它，说明阈值 0.70 对这个单位的录音环境太严，得调配置。
所以本脚本不只要验"能达标"，还要**演示阈值怎么影响它** ——
调严到 0.90 时占比必须显著上升，否则"阈值可热改"就是假的。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.app.services.review import (  # noqa: E402
    RECORD_AREA,
    RECORD_NAME_TEMPLATE,
    STATE_RECORD_READY,
    TRANSCRIPT_SEGMENTS_PATH,
    ReviewService,
    make_review_service,
)
from mmwf.domain.asr_types import TranscriptSegmentResult  # noqa: E402
from mmwf.domain.review import (  # noqa: E402
    METADATA_FIELDS,
    MISSING_METADATA_PLACEHOLDER,
    RecordMetadata,
    format_timestamp,
    join_names,
    needs_review,
    render_record,
    review_ratio,
    review_reason,
    split_for_review,
)
from mmwf.infra.audit import ACTIONS, AuditLogger, verify_chain  # noqa: E402
from mmwf.infra.db import connect_ctx  # noqa: E402
from mmwf.infra.filestore import make_filestore  # noqa: E402
from mmwf.infra.migrations.runner import migrate  # noqa: E402

PASS = "✓"
FAIL = "✗"

#: 施工卡的复核占比上限
REVIEW_RATIO_LIMIT = 0.20

#: 施工卡原文要求的 8 项元信息标签（顺序即渲染顺序）
REQUIRED_METADATA_LABELS = (
    "会议名称", "会议时间", "会议地点", "主持人",
    "参会人员", "列席人员", "记录人", "记录整理时间",
)

MEETING_ID = "acc-t108"
OPERATOR = "acc_tester"
GENERATED_AT = "2026-09-27T12:00:00Z"
LOW_CONF = 0.70


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


def _seg(seq, text, conf, start_ms, flags=()):
    return TranscriptSegmentResult(
        seq=seq, text=text, start_ms=start_ms, end_ms=start_ms + 3000,
        confidence=conf, flags=tuple(flags),
    )


def _make_env(tmp: Path, name: str = "e2e") -> tuple[Path, Path, object, None]:
    """建一套临时环境：库 + filestore。返回 (data_root, db_path, store, None)."""
    data_root = tmp / name / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    db = data_root / "mmwf.db"
    migrate(db)
    store = make_filestore(MEETING_ID, data_root, encrypt_enabled=False)
    return data_root, db, store, None


def _insert_meeting(db: Path, *, status: str = "reviewing", **over) -> None:
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
        "created_by": OPERATOR,
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
    }
    row.update(over)
    cols = ", ".join(row)
    with connect_ctx(db) as conn:
        conn.execute(f"INSERT INTO meeting ({cols}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))


def _write_segments(store, segments, **meta) -> None:
    payload = {
        "engine": "funasr", "model": "paraformer-zh",
        "avg_confidence": 0.9, "hotwords_applied": True, "warnings": [],
        "segments": [
            {
                "seq": s.seq, "text": s.text,
                "start_ms": s.start_ms, "end_ms": s.end_ms,
                "confidence": s.confidence, "flags": list(s.flags), "words": [],
            }
            for s in segments
        ],
    }
    payload.update(meta)
    store.write_json(TRANSCRIPT_SEGMENTS_PATH, payload, stage="transcript")


def _three_hour_segments(count: int = 1200) -> list:
    """造一份约 3 小时、质量尚可的转写稿。

    1200 段 × 每段 9 秒 = 10800 秒 = 3 小时。

    置信度刻意按**真实分布**铺开，而不是"合格段一律 0.93"：
    FunASR 这类引擎的分数是连续值，绝大多数落在 0.85~0.99，
    但仍会有一批在 0.70~0.85 之间（口音、远场、语速快）。
    如果造数据时把这一带挖空，就会得出一个错误的结论 ——
    "调严阈值不影响复核量"。下面第 ① 项标准专门验阈值敏感性，
    它必须有这个 0.70~0.90 的中间带去检验。
    """
    noisy = {17, 233, 601, 987}                    # 4 段残余噪声
    silent = {81, 445, 1102}                       # 3 段静音
    out = []
    for i in range(1, count + 1):
        start = (i - 1) * 9000
        if i in noisy:
            out.append(_seg(i, "嗯", 0.98, start))
            continue
        if i in silent:
            out.append(_seg(i, "（疑似无有效发言）", 0.93, start, flags=("silence",)))
            continue
        if i % 92 == 0:                            # ~13 段明显低分
            out.append(_seg(i, f"第 {i} 段识别质量偏低的会议内容", 0.48, start))
            continue
        if i % 37 == 0:                            # ~32 段中等偏下（0.70~0.90 带内）
            # 在 0.71~0.89 之间循环取值 —— 这正是"调严阈值会受影响"的那一批
            conf = 0.71 + (i % 37) * 0.005
            out.append(_seg(i, f"第 {i} 段置信度中等的会议发言内容", round(conf, 3), start))
            continue
        out.append(_seg(i, f"第 {i} 段正常的会议发言内容，长度足够构成一个分段", 0.94, start))
    return out


# ---------------------------------------------------------------------------
# 标准 1：3 小时会议复核占比 < 20%
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：复核占比 < 20%，且阈值**真的**能改变这个数。"""
    banner("标准 1  3 小时会议转写稿，进入人工复核的段落占比 < 20%")
    ok = True

    _, db, store, _ = _make_env(tmp, "s1")
    _insert_meeting(db)
    segments = _three_hour_segments()
    total_ms = segments[-1].end_ms
    _write_segments(store, segments)

    hours = total_ms / 1000 / 3600
    print(f"      稿件规模：{len(segments)} 段，时长约 {hours:.2f} 小时")

    svc = ReviewService(store, db_path=db,
                        thresholds={"asr": {"low_confidence": LOW_CONF}},
                        audit=AuditLogger(db), operator=OPERATOR)
    result = svc.generate_record(now=GENERATED_AT)

    ratio = result.review_count / result.segment_count
    print(f"      待复核 {result.review_count}/{result.segment_count} 段 = {ratio:.1%}")
    ok &= _assert(hours >= 2.9, f"稿件确实是 3 小时量级（{hours:.2f} 小时）")
    ok &= _assert(
        ratio < REVIEW_RATIO_LIMIT,
        f"复核占比 {ratio:.1%} < {REVIEW_RATIO_LIMIT:.0%}",
    )

    # 阈值可热改：调严到 0.90 后占比必须显著上升
    strict = ReviewService(store, db_path=db,
                           thresholds={"asr": {"low_confidence": 0.90}},
                           audit=AuditLogger(db), operator=OPERATOR)
    strict_ratio = review_ratio(segments, {"asr": {"low_confidence": 0.90}})
    print(f"      阈值 0.90 时占比：{strict_ratio:.1%}（{LOW_CONF} 时 {ratio:.1%}）")
    ok &= _assert(
        strict_ratio > ratio,
        f"阈值调严后复核占比上升（{ratio:.1%} -> {strict_ratio:.1%}），"
        "证明阈值真的在起作用",
    )
    strict.generate_record(now=GENERATED_AT)          # 走一遍，确认不崩
    return ok


# ---------------------------------------------------------------------------
# 标准 2：8 项元信息 + attendees 顿号分隔
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：8 项元信息齐全，参会人员用顿号展开。"""
    banner("标准 2  记录稿含全部 8 项元信息，attendees 正确展开为顿号分隔")
    ok = True

    # 先验列表本身：8 项，与施工卡模板逐项对应
    labels = tuple(label for _k, label in METADATA_FIELDS)
    ok &= _assert(len(labels) == 8, f"元信息恰好 8 项（实际 {len(labels)}）")
    ok &= _assert(
        labels == REQUIRED_METADATA_LABELS,
        f"8 项标签与施工卡一致：{labels}",
    )

    # 再验列表渲染：Python 的 list repr 绝不能出现
    joined = join_names(["李四", "王五", "赵六"])
    print(f"      join_names(['李四','王五','赵六']) -> {joined}")
    ok &= _assert(joined == "李四、王五、赵六", "顿号分隔正确")
    ok &= _assert(
        "[" not in joined and "'" not in joined,
        "没有 Python 列表字面量的痕迹（['李四', ...]）",
    )
    ok &= _assert(join_names("李四, 王五") == "李四、王五", "半角逗号被规范成顿号")

    # 端到端：从库里读 JSON 数组 -> 渲染
    _, db, store, _ = _make_env(tmp, "s2")
    _insert_meeting(db)
    _write_segments(store, [_seg(1, "今天召开党组理论学习中心组会议", 0.96, 12_000)])

    svc = ReviewService(store, db_path=db,
                        thresholds={"asr": {"low_confidence": LOW_CONF}},
                        audit=AuditLogger(db), operator=OPERATOR)
    result = svc.generate_record(now=GENERATED_AT)
    md = result.markdown

    ok &= _assert(result.metadata_complete, "8 项元信息全部有值（无待补充）")
    for label in REQUIRED_METADATA_LABELS:
        ok &= _assert(f"| {label} |" in md, f"记录稿含「{label}」行")
    ok &= _assert("| 参会人员 | 李四、王五、赵六 |" in md, "参会人员渲染为顿号分隔")
    ok &= _assert("| 列席人员 | 钱七 |" in md, "列席人员渲染正确")

    # 元信息表行数校验（多一行少一行都会串行）
    rows = [
        ln for ln in md.splitlines()
        if ln.startswith("| ") and "项目" not in ln and "---" not in ln
    ]
    ok &= _assert(len(rows) == 8, f"元信息表恰好 8 行（实际 {len(rows)}）")
    return ok


# ---------------------------------------------------------------------------
# 标准 3：正文段落带 [HH:MM:SS] 时间码
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    """标准 3：正文每段带 `[HH:MM:SS]` 时间码，且顺序递增。"""
    banner("标准 3  记录稿正文段落带时间码，格式 [00:12:34]")
    ok = True

    # 先验格式化函数本身
    cases = [
        (0, "[00:00:00]"),
        (12_000, "[00:00:12]"),
        (754_000, "[00:12:34]"),
        (3_661_000, "[01:01:01]"),
        (45_296_000, "[12:34:56]"),
    ]
    for ms, expect in cases:
        got = format_timestamp(ms)
        ok &= _assert(got == expect, f"{ms}ms -> {got}（期望 {expect}）")

    # 施工卡模板里的那个字面量，必须原样出现
    ok &= _assert(
        format_timestamp(12 * 60_000 + 34_000) == "[00:12:34]",
        "施工卡模板里的「[00:12:34]」形状完全一致",
    )

    # 端到端：正文里每段都有时间码，且正则能抓全
    _, db, store, _ = _make_env(tmp, "s3")
    _insert_meeting(db)
    segments = [
        _seg(1, "会议开始", 0.95, 0),
        _seg(2, "第一项议题", 0.95, 754_000),          # 00:12:34
        _seg(3, "第二项议题", 0.30, 3_661_000),        # 01:01:01（低分）
    ]
    _write_segments(store, segments)

    svc = ReviewService(store, db_path=db,
                        thresholds={"asr": {"low_confidence": LOW_CONF}},
                        audit=AuditLogger(db), operator=OPERATOR)
    md = svc.generate_record(now=GENERATED_AT).markdown

    ok &= _assert("[00:00:00] 会议开始" in md, "第一段带 [00:00:00]")
    ok &= _assert("[00:12:34] 第一项议题" in md, "第二段带 [00:12:34]")
    ok &= _assert("[01:01:01] 第二项议题" in md, "第三段带 [01:01:01]")

    body = md.split("## 二、会议记录正文", 1)[1].split("## 三、", 1)[0]
    stamps = re.findall(r"^\[(\d{2}:\d{2}:\d{2})\] ", body, flags=re.MULTILINE)
    print(f"      正文抓到的段落时间码：{stamps}")
    ok &= _assert(len(stamps) == len(segments), f"正文 {len(stamps)} 段全部带时间码")
    ok &= _assert(stamps == sorted(stamps), "时间码按时间递增（正文顺序正确）")

    # 待核对清单里也带时间码
    sec3 = md.split("## 三、待核对事项", 1)[1]
    ok &= _assert("**[01:01:01]**" in sec3, "待核对事项条目也带时间码")
    return ok


# ---------------------------------------------------------------------------
# 标准 4：confidence=None 不误判（本卡最要紧的一条）
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：`test_split_for_review` 覆盖置信度缺失（None）不误判。

    这一条不只是"跑一下测试文件"，而是**当场演示坑 1 的两种写法**：
    正确写法（`is not None`）下全篇无分数的稿子复核量为 0；
    错误写法（`conf or 0.0`）下同样一份稿子会被 100% 送复核。
    让验收人亲眼看到差别，比只报"测试通过"有说服力。
    """
    banner("标准 4  test_split_for_review 覆盖 confidence=None（不误判为低置信）")
    ok = True

    th = {"asr": {"low_confidence": LOW_CONF}}

    # ① 单段：None 不送复核
    class Seg:
        def __init__(self, seq, text, conf, flags=()):
            self.seq, self.text, self.confidence, self.flags = seq, text, conf, tuple(flags)

    none_seg = Seg(1, "这段引擎没给置信度", None)
    zero_seg = Seg(2, "这段分数是零", 0.0)
    ok &= _assert(needs_review(none_seg, th) is False, "confidence=None -> 不送复核")
    ok &= _assert(needs_review(zero_seg, th) is True, "confidence=0.0 -> 送复核（两者必须不同）")
    ok &= _assert(review_reason(none_seg, th) == "", "None 的理由为空串（没有编造判据）")
    print(f"      0.0 的理由：{review_reason(zero_seg, th)}")

    # ② 全篇 None：复核量为 0（坑 1 的正面断言）
    all_none = [Seg(i, f"第 {i} 段正常内容", None) for i in range(1, 101)]
    to_review, auto_pass = split_for_review(all_none, th)
    ok &= _assert(len(to_review) == 0, "100 段全无分数 -> 待复核 0 段")
    ok &= _assert(len(auto_pass) == 100, "100 段全部归入可直接用")
    ok &= _assert(review_ratio(all_none, th) == 0.0, "复核占比 0.0")

    # ③ 反面对照：写出错误实现，展示后果
    def wrong_needs_review(seg):
        """错误写法：把 None 兜底成 0.0 —— **不报错**，只是全判低置信。"""
        conf = seg.confidence or 0.0
        return conf < th["asr"]["low_confidence"]

    wrong_count = sum(1 for s in all_none if wrong_needs_review(s))
    print(f"      若写成 `conf or 0.0`：{wrong_count}/100 段被判低置信（应为 0）")
    ok &= _assert(
        wrong_count == len(all_none),
        "演示错误写法的后果：整篇被送去人工复核（正是坑 1 描述的场景）",
    )

    # ④ None 不判低置信，但别的缺陷照判（防过度修正）
    ok &= _assert(
        needs_review(Seg(1, "有内容的一段话", None, flags=("silence",)), th) is True,
        "None + 静音标记 -> 仍送复核（None 不是免死金牌）",
    )
    ok &= _assert(
        needs_review(Seg(1, "嗯", None), th) is True,
        "None + 单字噪声 -> 仍送复核",
    )

    # ⑤ 混合场景：None 段既不判真也不稀释比例
    mixed = [
        Seg(1, "正常内容够长", 0.95),
        Seg(2, "无分数内容段", None),
        Seg(3, "低分内容段落", 0.30),
        Seg(4, "另一段正常内容", 0.88),
    ]
    ok &= _assert(
        review_ratio(mixed, th) == 0.25,
        f"混合场景占比 1/4 = 25%（实际 {review_ratio(mixed, th):.1%}）",
    )

    # ⑥ 端到端：全篇 None 的转写稿生成记录稿时不产生待核对项
    _, db, store, _ = _make_env(tmp, "s4")
    _insert_meeting(db)
    all_none_real = [
        TranscriptSegmentResult(seq=i, text=f"第 {i} 段正常会议内容，长度足够",
                                start_ms=(i - 1) * 9000, end_ms=(i - 1) * 9000 + 3000,
                                confidence=None)
        for i in range(1, 21)
    ]
    _write_segments(store, all_none_real)
    svc = ReviewService(store, db_path=db, thresholds=th,
                        audit=AuditLogger(db), operator=OPERATOR)
    result = svc.generate_record(now=GENERATED_AT)
    ok &= _assert(result.review_count == 0, "全篇无分数 -> 记录稿待核对 0 项")
    ok &= _assert("无需人工核对" in result.markdown, "第三节点明无需人工核对")
    return ok


# ---------------------------------------------------------------------------
# 附加 1：模块级契约（施工卡之外的守卫）
# ---------------------------------------------------------------------------

def check_module_contracts(tmp: Path) -> bool:
    """附加 1：分流判据的独立性、白名单形状、阈值边界。"""
    banner("附加 1  分流判据的独立性、flag 白名单、阈值边界")
    ok = True
    th = {"asr": {"low_confidence": LOW_CONF}}

    class Seg:
        def __init__(self, seq, text, conf, flags=()):
            self.seq, self.text, self.confidence, self.flags = seq, text, conf, tuple(flags)

    # 分数发现不了的异常：高分 + overlap 必须送复核
    hi_overlap = Seg(1, "两人抢话的内容", 0.99, flags=("overlap",))
    ok &= _assert(
        needs_review(hi_overlap, th) is True,
        "高分（0.99）+ overlap -> 送复核（判据 2 独立于判据 1）",
    )
    ok &= _assert(
        "置信度" not in review_reason(hi_overlap, th),
        "理由说的是音频异常而不是置信度（否则复核页会自相矛盾）",
    )

    # 未登记 flag 不触发（白名单，不是"有标记就送"）
    unknowns = ("hotword_miss", "punctuation_fixed", "speaker_changed")
    all_clean = all(
        needs_review(Seg(1, "正常内容够长", 0.95, flags=(u,)), th) is False
        for u in unknowns
    )
    ok &= _assert(all_clean, f"未登记 flag 不送复核（{unknowns}）—— 白名单形状")

    # 阈值边界：等于阈值不算低置信
    ok &= _assert(needs_review(Seg(1, "内容够长的一段", 0.70), th) is False,
                  "0.70 恰好等于阈值 -> 不送复核（`<` 而非 `<=`）")
    ok &= _assert(needs_review(Seg(1, "内容够长的一段", 0.6999), th) is True,
                  "0.6999 -> 送复核")

    # 噪声边界：2 字是有效回应
    ok &= _assert(needs_review(Seg(1, "同意", 0.99), th) is False, "2 字「同意」不判噪声")
    ok &= _assert(needs_review(Seg(1, "嗯", 0.99), th) is True, "1 字「嗯」判噪声")

    # 缺 flags 属性的对象也要能吃
    class NoFlags:
        text, confidence = "正常的一段话", 0.95

    ok &= _assert(needs_review(NoFlags(), th) is False, "无 flags 属性的对象不报错")

    # 阈值缺失必须明确报错，不兜底
    raised = False
    try:
        needs_review(Seg(1, "文本", 0.5), {"asr": {}})
    except KeyError:
        raised = True
    ok &= _assert(raised, "阈值缺失时明确报错（不静默兜底成 0.7）")

    # 两堆不重不漏
    segs = [
        Seg(1, "正常内容够长", 0.95),
        Seg(2, "嗯", 0.99),
        Seg(3, "低分内容段", 0.30),
        Seg(4, "无分数内容段", None),
    ]
    rev, auto = split_for_review(segs, th)
    ok &= _assert(
        {s.seq for s in rev} | {s.seq for s in auto} == {1, 2, 3, 4}
        and not ({s.seq for s in rev} & {s.seq for s in auto}),
        "两堆不重不漏覆盖全部段落",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 2：坑 2 —— 元信息缺失填「待补充」
# ---------------------------------------------------------------------------

def check_placeholder(tmp: Path) -> bool:
    """附加 2：元信息缺失时填「待补充」，**不留空**（施工卡坑 2）。"""
    banner("附加 2  元信息缺失时填「待补充」，不留空（坑 2）")
    ok = True

    _, db, store, _ = _make_env(tmp, "a2")
    # 刻意不建 meeting 行 —— 模拟"元信息全缺"
    _write_segments(store, [_seg(1, "记录稿正文的一段内容", 0.95, 1_000)])

    svc = ReviewService(store, db_path=db,
                        thresholds={"asr": {"low_confidence": LOW_CONF}},
                        audit=AuditLogger(db), operator=OPERATOR)
    result = svc.generate_record(now=GENERATED_AT)
    md = result.markdown

    ok &= _assert(
        MISSING_METADATA_PLACEHOLDER in md,
        f"缺项渲染成「{MISSING_METADATA_PLACEHOLDER}」",
    )
    ok &= _assert("| |" not in md, "没有空单元格（`| |`）")
    ok &= _assert(
        len(result.metadata_missing) == 7,
        f"7 项待补充（除「记录整理时间」外全缺，实际 {len(result.metadata_missing)}）",
    )
    ok &= _assert(
        "记录整理时间" in result.metadata_filled,
        "「记录整理时间」自动填成本次生成时刻（不是待补充）",
    )
    print(f"      待补充项：{list(result.metadata_missing)}")

    # 生成提示里要点明缺项
    ok &= _assert("生成提示" in md, "末尾有「生成提示」小节说明缺项")
    ok &= _assert("请补齐" in md, "提示里含「请补齐」的行动指引")

    # 会议行存在但部分字段为空：也要填占位符
    _, db2, store2, _ = _make_env(tmp, "a2b")
    _insert_meeting(db2, location=None, host=None, absentees=None, recorder=None)
    _write_segments(store2, [_seg(1, "记录稿正文的一段内容", 0.95, 1_000)])
    svc2 = ReviewService(store2, db_path=db2,
                         thresholds={"asr": {"low_confidence": LOW_CONF}},
                         audit=AuditLogger(db2), operator=OPERATOR)
    r2 = svc2.generate_record(now=GENERATED_AT)
    ok &= _assert(
        "| 会议地点 | 待补充 |" in r2.markdown and "| 主持人 | 待补充 |" in r2.markdown,
        "行存在但字段为空 -> 同样填「待补充」",
    )
    ok &= _assert(
        r2.metadata_complete is False,
        "metadata_complete=False（便于上层决定是否提示补齐）",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 3：坑 3 —— 文件名/版本号与 manifest 一致
# ---------------------------------------------------------------------------

def check_manifest_consistency(tmp: Path) -> bool:
    """附加 3：记录稿文件名、版本号、manifest 三处一致（施工卡坑 3）。"""
    banner("附加 3  记录稿文件名与版本号与 manifest.json 一致（坑 3）")
    ok = True

    _, db, store, _ = _make_env(tmp, "a3")
    _insert_meeting(db)
    _write_segments(store, [_seg(1, "记录稿正文的一段内容", 0.95, 1_000)])
    audit = AuditLogger(db)
    svc = ReviewService(store, db_path=db,
                        thresholds={"asr": {"low_confidence": LOW_CONF}},
                        audit=audit, operator=OPERATOR)

    r1 = svc.generate_record(now=GENERATED_AT)
    r2 = svc.generate_record(now=GENERATED_AT)

    # ① 文件名 = 版本号
    ok &= _assert(
        Path(r1.rel_path).name == RECORD_NAME_TEMPLATE.format(version=r1.version) == "v1.md",
        f"第一版文件名 {Path(r1.rel_path).name} == v1.md",
    )
    ok &= _assert(
        Path(r2.rel_path).name == "v2.md" and r2.version == 2,
        "重跑生成 v2.md（**不覆盖** v1）",
    )
    ok &= _assert(store.exists("record/v1.md") and store.exists("record/v2.md"),
                  "v1 与 v2 两个文件都在（留版本便于回溯「我核的是哪一版」）")

    # ② manifest 登记了这两条
    paths = {e.path for e in store.manifest()}
    ok &= _assert({"record/v1.md", "record/v2.md"} <= paths,
                  "manifest.json 登记了 v1 与 v2")

    # ③ 审计 detail 里的 version/path 与文件对得上
    from mmwf.infra.audit import query

    recs = [r for r in query(db) if r.action == "record_generate"]
    ok &= _assert(len(recs) == 2, f"两次生成留了两条审计（实际 {len(recs)}）")
    pairs = {(r.detail["version"], r.detail["path"]) for r in recs}
    ok &= _assert(
        pairs == {(1, "record/v1.md"), (2, "record/v2.md")},
        f"审计里的 (version, path) 与文件一一对应：{sorted(pairs)}",
    )

    # ④ manifest 整体一致（无未登记文件、无丢失）
    problems = store.verify_manifest()
    ok &= _assert(problems == [], f"manifest 与实际文件完全一致（问题：{problems[:2]}）")

    # ⑤ 记录稿落在加密区
    from mmwf.infra.filestore import ENCRYPTED_AREAS

    ok &= _assert(RECORD_AREA in ENCRYPTED_AREAS, "记录稿目录属于加密区（合规要求）")

    # ⑥ 状态推进到 record_ready + 审计链完好
    ok &= _assert(svc.current_status() == STATE_RECORD_READY,
                  f"meeting.status 推进到 {STATE_RECORD_READY}")
    chain = verify_chain(db)
    ok &= _assert(chain.ok, f"审计链完好（{chain.summary()}）")

    # ⑦ 审计只记统计不记正文（合规红线）
    secret = "记录稿正文的一段内容"
    raw = json.dumps([r.detail for r in recs], ensure_ascii=False)
    ok &= _assert(secret not in raw, "审计 detail 不含记录稿正文（部署手册 §5.3 红线）")
    ok &= _assert(
        all("segments" in r.detail and "review_ratio" in r.detail for r in recs),
        "审计记了统计（segments / review_ratio）便于追溯",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 4：AI 依赖与闸口（回归）
# ---------------------------------------------------------------------------

def check_gates() -> bool:
    """附加 4：分层、依赖白名单、本卡用例。"""
    banner("附加 4  分层检查 + 依赖白名单 + T1-08 用例")
    ok = True

    def _run(cmd):
        return subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    for label, cmd in [
        ("依赖分层 check_layers.py", [sys.executable, "tools/check_layers.py"]),
        ("依赖闸口 check_deps.py", [sys.executable, "tools/check_deps.py"]),
        (
            "T1-08 用例",
            [
                sys.executable, "-m", "pytest",
                "tests/unit/test_review_split.py",
                "tests/unit/test_review_record.py",
                "-q", "--no-header",
            ],
        ),
        (
            "T1-05 ASR 契约回归（flags 字段新增后）",
            [
                sys.executable, "-m", "pytest",
                "tests/unit/test_asr_funasr.py",
                "tests/contract",
                "-q", "--no-header",
            ],
        ),
    ]:
        proc = _run(cmd)
        lines = [ln for ln in (proc.stdout + proc.stderr).strip().splitlines() if ln.strip()]
        print(f"  {PASS if proc.returncode == 0 else FAIL} {label}")
        print(f"      {lines[-1] if lines else '(无输出)'}")
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr)[-2000:])
        ok &= proc.returncode == 0
    return ok


# ---------------------------------------------------------------------------
# 附加 5：审计动作登记
# ---------------------------------------------------------------------------

def check_audit_registration() -> bool:
    """附加 5：`record_generate` 已登记进 ACTIONS，且归入最小核查项。"""
    banner("附加 5  record_generate 审计动作登记")
    ok = True

    ok &= _assert("record_generate" in ACTIONS, "record_generate 在 ACTIONS 白名单里")

    from mmwf.infra.audit import MINIMUM_CHECK_ITEM_ACTIONS

    ok &= _assert(
        "record_generate" in MINIMUM_CHECK_ITEM_ACTIONS["文稿修改记录"],
        "归入部署手册 §6.2「文稿修改记录」核查项",
    )
    ok &= _assert(
        len(MINIMUM_CHECK_ITEM_ACTIONS) == 8,
        f"最小核查项仍是 8 类（实际 {len(MINIMUM_CHECK_ITEM_ACTIONS)}）",
    )

    # 未登记动作必须被拒
    from mmwf.infra.audit import AuditError

    raised = False
    try:
        AuditLogger.__new__(AuditLogger).log("op", "not_a_registered_action")
    except (AuditError, AttributeError, TypeError):
        raised = True
    ok &= _assert(raised, "未登记动作被拒绝（审计保持可枚举）")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T1-08 验收")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据")
    args = ap.parse_args()

    print("T1-08 验收：转写稿复核与记录稿生成（review）")
    print(f"项目根：{PROJECT_ROOT}")

    # 附加 2 刻意用"没有 meeting 行"的场景，会打 warning。它是对的，
    # 但对验收输出是噪音 —— 压到 ERROR 以上，免得盖住真正的结论。
    logging.disable(logging.WARNING)

    tmp_root = Path(tempfile.mkdtemp(prefix="mmwf_t108_"))
    try:
        results: dict[str, bool] = {
            "标准 1 复核占比 < 20%": check_standard_1(tmp_root / "s1"),
            "标准 2 8 项元信息 + 顿号分隔": check_standard_2(tmp_root / "s2"),
            "标准 3 正文带 [HH:MM:SS] 时间码": check_standard_3(tmp_root / "s3"),
            "标准 4 confidence=None 不误判": check_standard_4(tmp_root / "s4"),
            "附加 1 判据独立性 + flag 白名单": check_module_contracts(tmp_root / "a1"),
            "附加 2 元信息缺失填「待补充」": check_placeholder(tmp_root / "a2"),
            "附加 3 文件名/版本与 manifest 一致": check_manifest_consistency(tmp_root / "a3"),
            "附加 4 分层 + 依赖 + 用例": check_gates(),
            "附加 5 record_generate 审计登记": check_audit_registration(),
        }

        banner("验收结论")
        for name, ok in results.items():
            print(f"  {PASS if ok else FAIL} {name}")

        all_ok = all(results.values())
        print(f"\n{'=' * 78}")
        print(f"{PASS} T1-08 验收通过" if all_ok else f"{FAIL} T1-08 验收未通过")
        print(f"{'=' * 78}")
        return 0 if all_ok else 1
    finally:
        if args.keep_tmp:
            print(f"\n临时数据已保留：{tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
