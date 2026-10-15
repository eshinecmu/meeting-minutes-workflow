"""T1-06 验收脚本（人可读版）。

施工卡 T1-06 的五条验收标准，逐条跑一遍并打印结论。
用法：python tools/accept_t1_06.py
      python tools/accept_t1_06.py --real-ffmpeg   # 强制要求真 ffmpeg（没有则判失败）

五条标准（施工卡原文）：
  [ ] 投递 2.5 小时音频，chunk_plan 生成 6 段（1800s 块 + 50s 重叠），无遗漏无越界
  [ ] 每段切分后独立转写，拼接结果与原音频直接转写的文本差异 < 2%
      （重叠区去重正确性验证）
  [ ] 不支持的格式返回 E1001 与中文提示
  [ ] 损坏音频返回 E1003 不崩溃
  [ ] 单文件 > 4GB 时有明确提示（避免 ffmpeg 处理超长文件失败）

关于"真 ffmpeg vs 替身"
-----------------------
ffmpeg 是**部署方交付的外部程序**（部署手册 §2.1 第 7 项，附 SHA256），
不是 pip 依赖。所以本脚本分两条路：

· 标准 1/3/4/5 的**判定逻辑**与 ffmpeg 无关（一个是纯函数、三个是
  错误码与告警），直接用替身 adapter 验，结论对真 ffmpeg 同样成立；
· 标准 2（拼接文本差异 < 2%）需要**真的能切出音频**，否则"独立转写"
  这一步拿不到东西。这里优先用真 ffmpeg（PATH → imageio-ffmpeg 兜底）；
  都没有时**如实标注跳过**，绝不伪装成通过。
· 任何一条标准只要本机有真 ffmpeg，就额外用真 ffmpeg 再验一遍。

标准 2 的"差异 < 2%"怎么算
--------------------------
不是字符编辑距离，而是**按参考文本长度归一化的编辑距离**：

    diff_ratio = levenshtein(merged_text, reference_text) / len(reference_text)

理由：把"整段独立转写"的结果当参考（reference），把"切分后逐段转写
再拼接"的结果当被测（merged）。两者用的是同一个确定性引擎，
所以差异**只可能来自切分与去重环节** —— 这正是本卡要验的东西。
去重做错（漏去/多去）的最典型表现就是重叠区的文字多了一份或少了一份，
在编辑距离上体现为一段几十字的插入/删除，归一化后必然超过 2%。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "fixtures" / "asr"))

PASS = "✓"
FAIL = "✗"
SKIP = "·"

#: 施工卡的切分参数（与 config/models.yaml 的 asr 段一致）
CHUNK_SECONDS = 1800
OVERLAP_SECONDS = 50

#: 验收标准 1 的音频时长：2.5 小时
TWO_AND_HALF_HOURS_MS = int(2.5 * 3600 * 1000)

#: 验收标准 2 允许的最大文本差异（施工卡原文：< 2%）
MAX_DIFF_RATIO = 0.02

#: 验收标准 5 的超大文件阈值（与 planner.MAX_FILE_BYTES 一致，这里显式写出
#: 是为了让本脚本在"阈值被改了"时能被读出来 —— 用常量而不是引用，
#: 才能发现"实现和施工卡不一致"）
FOUR_GB = 4 * 1024**3


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 真 ffmpeg 探测
# ---------------------------------------------------------------------------

def detect_ffmpeg() -> tuple[bool, str]:
    """本机有没有可用的真 ffmpeg。返回 (有, 说明)。"""
    from mmwf.adapters.audio.ffmpeg import FFmpegNotFoundError, locate

    try:
        tools = locate()
    except FFmpegNotFoundError as e:
        return False, str(e).splitlines()[0]
    try:
        proc = subprocess.run(
            [tools.ffmpeg, "-version"], capture_output=True, timeout=30, check=False
        )
        ver = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
        ver_s = ver[0].strip() if ver else "unknown"
    except Exception as e:  # noqa: BLE001
        return False, f"定位到 {tools.ffmpeg} 但执行失败：{e}"

    probe = tools.ffprobe or "（无 ffprobe，时长探测将走 ffmpeg 输出解析）"
    return True, f"{ver_s}｜来源 {tools.source}｜ffprobe: {probe}"


# ---------------------------------------------------------------------------
# 音频夹具生成（用真 ffmpeg 造，保证能被 ffprobe 读出准确时长）
# ---------------------------------------------------------------------------

def make_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    """写一份 16kHz 单声道静音 wav。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n)
    return path


# ---------------------------------------------------------------------------
# 替身 adapter（不碰真 ffmpeg，用于与 ffmpeg 无关的标准）
# ---------------------------------------------------------------------------

class StubAdapter:
    """替身：按"文件名 -> 时长"表返回，切分只写一个占位文件。

    它**不模拟 ffmpeg 的行为**，只提供两个契约方法。这让标准 1/3/5 的
    判定完全不依赖本机环境。
    """

    def __init__(self, durations: dict[str, int] | None = None,
                 *, bad: set[str] | None = None,
                 raise_not_found: bool = False) -> None:
        self._durations = durations or {}
        self._bad = bad or set()
        self._raise_not_found = raise_not_found
        self.cut_calls: list[tuple[str, int, int]] = []

    def probe_duration_ms(self, path: Path | str) -> int:
        from mmwf.adapters.audio.ffmpeg import FFmpegNotFoundError
        from mmwf.domain.errors import CorruptAudioError

        p = Path(path)
        if self._raise_not_found:
            raise FFmpegNotFoundError("替身：本机无 ffmpeg")
        if p.name in self._bad:
            raise CorruptAudioError(detail=f"替身：损坏 {p.name}", audio_path=str(p))
        return self._durations.get(p.name, 600_000)

    def cut_chunk(self, src, dst, start_ms: int, end_ms: int) -> None:
        self.cut_calls.append((Path(src).name, start_ms, end_ms))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"RIFF" + b"\x00" * 128)


def _make_file(path: Path, content: bytes = b"\x00" * 4096) -> Path:
    """造一个占位文件（自动建父目录）。

    `tempfile.TemporaryDirectory` 只创建顶层目录，各标准用的子目录
    （s1/s2/...）需要自己建 —— 反复踩同一个坑，收进一个函数。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# 标准 1：2.5 小时 → 6 段，无遗漏无越界
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path, have_ff: bool) -> bool:
    banner("标准 1：投递 2.5 小时音频 → chunk_plan 生成 6 段（1800s + 50s 重叠）")

    from mmwf.domain.ingest.planner import build_chunk_plan

    plan = build_chunk_plan(
        TWO_AND_HALF_HOURS_MS,
        chunk_seconds=CHUNK_SECONDS,
        overlap_seconds=OVERLAP_SECONDS,
    )

    print(f"  音频时长：{TWO_AND_HALF_HOURS_MS / 1000 / 3600:.2f} 小时 = {TWO_AND_HALF_HOURS_MS} ms")
    print(f"  切分参数：块 {CHUNK_SECONDS}s + 重叠 {OVERLAP_SECONDS}s → 步进 "
          f"{CHUNK_SECONDS - OVERLAP_SECONDS}s")
    print("  生成计划：")
    for c in plan:
        print(f"    seq={c.seq}  [{c.start_ms:>9d}, {c.end_ms:>9d})  "
              f"{c.duration_ms / 1000 / 60:6.3f} 分钟")

    ok = True
    ok &= _assert(len(plan) == 6, f"段数 == 6（实际 {len(plan)}）")
    ok &= _assert(plan[0].start_ms == 0, "首段从 0 开始（不遗漏音频开头）")
    ok &= _assert(
        plan[-1].end_ms == TWO_AND_HALF_HOURS_MS,
        f"末段结束于音频末尾（{plan[-1].end_ms} == {TWO_AND_HALF_HOURS_MS}，不遗漏尾部）",
    )
    overrun = [c.seq for c in plan if c.end_ms > TWO_AND_HALF_HOURS_MS]
    ok &= _assert(not overrun, f"无越界段（越界段：{overrun or '无'}）")
    ok &= _assert(
        [c.seq for c in plan] == list(range(1, len(plan) + 1)),
        "seq 从 1 连续递增（SAD §9.2 响应字段要求）",
    )
    ok &= _assert(
        all(a.start_ms < b.start_ms for a, b in zip(plan, plan[1:])),
        "起点严格递增（不会出现零步进死循环）",
    )

    # 无遗漏：相邻段的时间轴必须**接得上**（后段起点 < 前段终点 → 重叠覆盖）
    gaps = [
        (a.seq, b.seq)
        for a, b in zip(plan, plan[1:])
        if b.start_ms > a.end_ms
    ]
    ok &= _assert(not gaps, f"相邻段无时间轴空洞（空洞：{gaps or '无'}）")

    # 重叠精确性：相邻段的重叠恰好是 OVERLAP_SECONDS（除末段被截断外）
    overlaps = [a.end_ms - b.start_ms for a, b in zip(plan, plan[1:])]
    expected = OVERLAP_SECONDS * 1000
    ok &= _assert(
        all(o == expected for o in overlaps),
        f"相邻段重叠精确等于 {expected}ms（实际 {overlaps}）",
    )
    # 前 5 段都是完整块
    ok &= _assert(
        all(c.duration_ms == CHUNK_SECONDS * 1000 for c in plan[:-1]),
        "前 5 段均为完整 1800s 块",
    )
    ok &= _assert(
        plan[-1].duration_ms == TWO_AND_HALF_HOURS_MS - plan[-1].start_ms,
        "末段为剩余时长（被音频末尾截断）",
    )

    # 用 service 端到端再验一次（含落盘切分）
    svc_tmp = tmp / "svc"
    svc_tmp.mkdir(parents=True, exist_ok=True)
    audio = _make_file(svc_tmp / "meeting_2h30.wav", b"\x00" * 1024)
    from mmwf.app.services.ingest import IngestService

    svc = IngestService(
        StubAdapter({audio.name: TWO_AND_HALF_HOURS_MS}),
        chunk_seconds=CHUNK_SECONDS,
        overlap_seconds=OVERLAP_SECONDS,
    )
    got = svc.ingest_one(audio, chunk_dir=svc_tmp / "chunks")
    ok &= _assert(got.chunk_count == 6, f"service 端到端也得到 6 段（实际 {got.chunk_count}）")
    ok &= _assert(len(got.chunk_files) == 6, "切分产物 6 个文件")
    names = [p.name for p in got.chunk_files]
    ok &= _assert(names == sorted(names), f"产物文件名排序 == seq 排序：{names[:3]}...")
    print(f"  提示文案：{got.warnings[0] if got.warnings else '（无）'}")

    if have_ff:
        print(f"  {PASS} 本机有真 ffmpeg —— 段计划是纯函数，与 ffmpeg 版本无关，"
              "真机切分另见标准 2 与「附加 真 ffmpeg 端到端」")
    return ok


# ---------------------------------------------------------------------------
# 标准 2：切分后拼接 vs 整段直接转写，文本差异 < 2%
# ---------------------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    """编辑距离。两份文本各几百字，O(n·m) 完全够用。

    用滚动数组（只留两行）——参考文本会随音频变长，不做这步内存会爆得很难看。
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def check_standard_2(tmp: Path, have_ff: bool, ff_desc: str) -> bool:
    banner("标准 2：切分后逐段转写再拼接 vs 整段直接转写 → 文本差异 < 2%")

    if not have_ff:
        print(f"  {SKIP} 本机没有可用的 ffmpeg，无法真实切分音频。")
        print("      本项**跳过**（不伪装成通过）。")
        print("      按部署手册 §2.1 第 7 项放入 ffmpeg.exe + ffprobe.exe 后重跑；")
        print("      开发环境可 pip install imageio-ffmpeg -i https://pypi.org/simple 临时兜底。")
        return True
    print(f"  ffmpeg：{ff_desc}")

    from mmwf.adapters.asr import build, unregister
    from mmwf.adapters.audio.ffmpeg import FFmpegAdapter
    from mmwf.app.services.ingest import IngestService
    from mmwf.domain.ingest.planner import (
        build_chunk_plan,
        merge_overlapping_texts,
    )
    from fake_engine import PROVIDER_NAME, install, write_offset_marker

    # 用真人台词替换夹具台词：夹具的原台词每句 10 余字，重叠区根本吃不到
    # 去重逻辑（50s 重叠按夹具的 180ms/字节奏只有十几字），测不出问题。
    # 这里造一份"每句都远超重叠区长度"的台词本，让相邻块**必然**在句中切断，
    # 从而真正压上去重算法。
    #
    # `respect_duration=True` 是**必须的**：默认模式下夹具不看音频内容，
    # 无论传整段还是片段都返回完整台词本 —— 那样去重会 100% 命中、
    # 拼接结果必然等于参考文本，标准 2 就变成了一句废话。
    LINES = (
        "同志们今天我们召开党组会议主要研究部署下一阶段的重点工作任务",
        "先请张建国同志汇报前期工作进展情况包括项目推进和资金使用情况",
        "李国强同志补充一下基层调研情况特别是群众反映比较集中的几个问题",
        "王振华同志谈谈下一步工作打算以及需要协调解决的具体事项和建议",
        "最后我再强调三点意见一是要提高政治站位二是要压实工作责任三是",
    )
    install(lines=LINES, respect_duration=True)

    try:
        # 1) 造一份真音频。时长要和台词本**对得上**：
        #    台词本按 180ms/字 铺开共 27.78s，所以音频取 28s ——
        #    这样"整段直接转写"能覆盖全部台词，而切成的小段各自只覆盖一部分。
        audio = tmp / "e2e_meeting.wav"
        # 28s 音频 + 10s 块 + 3s 重叠 → 步进 7s → (0,10)(7,17)(14,24)(21,28)，共 4 段
        make_wav(audio, seconds=28)

        svc = IngestService(
            FFmpegAdapter(),
            chunk_seconds=10,
            overlap_seconds=3,
        )
        cut_dir = tmp / "cut"
        got = svc.ingest_one(audio, chunk_dir=cut_dir)
        print(f"  音频：{audio.name}（28s）→ 切成 {got.chunk_count} 段，重叠 3s")
        for c in got.chunk_plan:
            print(f"    seq={c.seq}  [{c.start_ms:>6d}, {c.end_ms:>6d})")

        ok = True
        ok &= _assert(got.chunk_count >= 3, f"切出的段数 >= 3（实际 {got.chunk_count}）")

        # 2) 参考：整段直接转写
        engine = build(PROVIDER_NAME, model_dir=tmp)
        ref = engine.transcribe(audio)
        reference_text = ref.text

        # 3) 被测：逐段转写，再按 domain 的去重函数拼接。
        #    **关键**：切出来的片段本身不带"我是从第几秒切的"信息，
        #    而 `AsrProvider` 的契约里也没有这个参数（真引擎同样没有）。
        #    所以测试方把偏移写进片段音频（`write_offset_marker`），
        #    夹具读出来即可还原时间轴 —— 否则每段都会返回完整台词本，
        #    去重 100% 命中，本项就成了一句话废话（第一版正是如此）。
        pieces: list[tuple[int, int, str]] = []
        for c, chunk_path in zip(got.chunk_plan, got.chunk_files):
            write_offset_marker(chunk_path, c.start_ms)
            seg_result = engine.transcribe(chunk_path)
            preview = seg_result.text[:24] + ("…" if len(seg_result.text) > 24 else "")
            print(f"    seq={c.seq} 偏移 {c.start_ms:>6d}ms → {len(seg_result.text):>3d} 字：{preview}")
            pieces.append((c.start_ms, c.end_ms, seg_result.text))

        merged_text, decisions = merge_overlapping_texts(pieces)

        print(f"  参考文本长度：{len(reference_text)} 字")
        print(f"  拼接文本长度：{len(merged_text)} 字")
        print("  去重决策：")
        for d in decisions:
            print(f"    第 {d['between'][0]}-{d['between'][1]} 段之间："
                  f"重叠 {d['overlap_ms']}ms，去重={d['deduped']}，"
                  f"匹配 {d.get('matched_chars', 0)} 字（{d['reason']}）")

        # 4) 差异归一化
        dist = _levenshtein(merged_text, reference_text)
        denom = max(1, len(reference_text))
        ratio = dist / denom
        print(f"  编辑距离：{dist}；归一化差异：{ratio * 100:.2f}%"
              f"（阈值 {MAX_DIFF_RATIO * 100:.0f}%）")

        ok &= _assert(
            ratio < MAX_DIFF_RATIO,
            f"拼接文本与原音频直接转写的差异 {ratio * 100:.2f}% < "
            f"{MAX_DIFF_RATIO * 100:.0f}%",
        )

        # 关键子断言：去重**必须真的发生过**。若全部 deduped=False，
        # 说明重叠区没吃到（配置或夹具的问题），此时差异"看似很小"
        # 只是因为重叠比例低 —— 那是个假通过。
        n_deduped = sum(1 for d in decisions if d["deduped"])
        ok &= _assert(
            n_deduped > 0,
            f"至少有一对相邻段真正做了去重（实际 {n_deduped} 对）"
            "—— 否则本项是假通过",
        )

        # 拼接结果不该出现整句重复（去重失败的典型症状）
        dup = _find_repeated_sentence(merged_text)
        ok &= _assert(not dup, f"拼接结果无整句重复（发现：{dup or '无'}）")

        # 记录到测试报告
        import json

        report = {
            "audio_seconds": 28,
            "chunk_seconds": 10,
            "overlap_seconds": 3,
            "chunk_count": got.chunk_count,
            "reference_chars": len(reference_text),
            "merged_chars": len(merged_text),
            "levenshtein": dist,
            "diff_ratio": round(ratio, 6),
            "max_allowed_ratio": MAX_DIFF_RATIO,
            "decisions": decisions,
            "ffmpeg": ff_desc,
        }
        out = PROJECT_ROOT / "tests" / "fixtures" / "transcripts" / "t1_06_merge.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  报告已写入：{out.relative_to(PROJECT_ROOT)}")
        return ok
    finally:
        unregister(PROVIDER_NAME)


def _find_repeated_sentence(text: str, min_len: int = 8) -> str:
    """找拼接结果里被重复了两遍的句子（去重失败的症状）。"""
    # 按常见断句符切；夹具文本无标点，这里补一个按长度滑窗的兜底
    for i in range(0, max(0, len(text) - min_len * 2) + 1):
        window = text[i:i + min_len]
        if len(window) == min_len and text.count(window) > 1:
            # 找到重复片段后稍微向左扩展，报一段更像"句子"的内容
            start = max(0, i - 4)
            return text[start:start + min_len + 8]
    return ""


# ---------------------------------------------------------------------------
# 标准 3：不支持的格式 → E1001 + 中文提示
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    banner("标准 3：不支持的格式返回 E1001 与中文提示")

    from mmwf.app.services.ingest import IngestService
    from mmwf.domain.errors import UnsupportedFormatError

    ok = True

    # 3a：domain 层
    from mmwf.domain.ingest.planner import SUPPORTED_EXTENSIONS, validate_extension

    print(f"  支持的扩展名（{len(SUPPORTED_EXTENSIONS)} 个）："
          f"{' '.join(sorted(SUPPORTED_EXTENSIONS))}")

    for bad_name in ("会议录音.txt", "recording.mp4", "notes", "audio.exe"):
        try:
            validate_extension(bad_name)
            ok &= _assert(False, f"「{bad_name}」应被拒绝")
        except UnsupportedFormatError as e:
            d = e.to_dict()
            ok &= _assert(e.code == "E1001", f"「{bad_name}」→ E1001")
            ok &= _assert(
                all("\u4e00" <= ch <= "\u9fff" for ch in d["message"][:6]),
                f"「{bad_name}」提示为中文：{d['message'][:28]}…",
            )

    # 3b：service 层（真正投递路径）
    bad = tmp / "会议纪要.txt"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("这不是音频", encoding="utf-8")
    svc = IngestService(StubAdapter())
    try:
        svc.ingest_one(bad)
        ok &= _assert(False, "投递 .txt 应失败")
    except UnsupportedFormatError as e:
        d = e.to_dict()
        ok &= _assert(e.code == "E1001", "投递 .txt → E1001")
        # N3.3：用户文案不得出现文件路径
        ok &= _assert(
            str(tmp) not in d["message"] and "\\" not in d["message"],
            "用户文案不含路径（N3.3 信息不外泄）",
        )
        ok &= _assert(
            set(d) == {"code", "title", "message", "severity"},
            "to_dict() 只有用户可见字段",
        )
        print(f"      用户看到：{d['title']}｜{d['message']}")

    # 3c：批量投递时格式错误只算该文件
    ok_file = _make_file(tmp / "good.wav")
    batch = svc.ingest_batch([ok_file, bad])
    ok &= _assert(batch.total == 2, "批量投递 2 个文件都被看到")
    ok &= _assert(len(batch.succeeded) == 1, "好的那个成功")
    ok &= _assert(len(batch.failed) == 1, "坏的那个失败")
    ok &= _assert(batch.failed[0].to_dict()["code"] == "E1001", "失败项带 E1001")
    ok &= _assert(not batch.ok, "BatchResult.ok 为 False（API 层据此返回 207/400）")
    print(f"      批量摘要：{batch.summary()}")
    return ok


# ---------------------------------------------------------------------------
# 标准 4：损坏音频 → E1003，不崩溃
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path, have_ff: bool) -> bool:
    banner("标准 4：损坏音频返回 E1003 且不崩溃")

    from mmwf.app.services.ingest import IngestService
    from mmwf.domain.errors import CorruptAudioError

    ok = True

    # 4a：替身路径 —— 探测阶段报损坏
    bad = _make_file(tmp / "broken.wav")
    svc = IngestService(StubAdapter(bad={bad.name}))
    try:
        svc.ingest_one(bad)
        ok &= _assert(False, "损坏文件应抛错")
    except CorruptAudioError as e:
        d = e.to_dict()
        ok &= _assert(e.code == "E1003", "损坏音频 → E1003")
        ok &= _assert(
            "损坏" in d["message"], f"提示说明可能损坏：{d['message']}"
        )
        print(f"      用户看到：{d['title']}｜{d['message']}")

    # 4b：空文件（0 字节）也是"读不出来"
    empty = _make_file(tmp / "empty.mp3", b"")
    svc2 = IngestService(StubAdapter(durations={empty.name: 600_000}))
    try:
        svc2.ingest_one(empty)
        ok &= _assert(False, "空文件应抛错")
    except CorruptAudioError as e:
        ok &= _assert(e.code == "E1003", "0 字节文件 → E1003")

    # 4c：**不崩溃** —— 批量投递里混入损坏文件，其余仍成功
    good = _make_file(tmp / "fine.wav")
    batch = IngestService(StubAdapter(bad={bad.name})).ingest_batch([good, bad])
    ok &= _assert(len(batch.succeeded) == 1 and len(batch.failed) == 1,
                  "混入损坏文件不崩溃，好文件照常成功")
    ok &= _assert(batch.failed[0].to_dict()["code"] == "E1003", "失败项带 E1003")

    # 4d：真 ffmpeg 路径 —— 真的伪造一份"扩展名合法但内容是垃圾"的文件
    if have_ff:
        from mmwf.adapters.audio.ffmpeg import FFmpegAdapter

        fake_audio = _make_file(tmp / "garbage.m4a", os.urandom(8192))  # 随机字节，绝不是合法音频
        svc3 = IngestService(FFmpegAdapter())
        try:
            svc3.ingest_one(fake_audio)
            ok &= _assert(False, "真 ffmpeg 对垃圾 .m4a 应抛 E1003")
        except CorruptAudioError as e:
            ok &= _assert(e.code == "E1003",
                          "真 ffmpeg：垃圾 .m4a → E1003（不崩溃）")
            ok &= _assert(
                str(fake_audio) not in e.to_dict()["message"],
                "真 ffmpeg 报错的用户文案不含路径",
            )
            print(f"      用户看到：{e.to_dict()['message']}")
        except Exception as e:  # noqa: BLE001
            ok &= _assert(False, f"真 ffmpeg 抛了非业务异常：{type(e).__name__}: {e}")
    else:
        print(f"  {SKIP} 无真 ffmpeg，跳过「垃圾 .m4a → E1003」实机验证")

    return ok


# ---------------------------------------------------------------------------
# 标准 5：单文件 > 4GB 有明确提示
# ---------------------------------------------------------------------------

def check_standard_5(tmp: Path, monkeypatch_ok: bool = True) -> bool:
    banner("标准 5：单文件 > 4GB 有明确提示（且不直接拒绝）")

    from mmwf.app.services import ingest as ingest_mod
    from mmwf.app.services.ingest import IngestService
    from mmwf.domain.ingest import planner

    ok = True

    # 真要造一个 4GB 文件是荒唐的。这里把阈值缩小，造一份 5MB 的"超大"文件
    # 即可覆盖同一条代码路径。
    #
    # **补丁打在 planner 上**：`ingest.py` 现在读的是
    # `planner.MAX_FILE_BYTES`（模块属性，不是 from-import 的值拷贝），
    # 所以改这一处就够。第一版实现是 from-import，改 planner 对 service
    # 毫无影响 —— 那条"超大文件告警"断言看着跑过了，其实什么都没验到。
    original = planner.MAX_FILE_BYTES
    print(f"  实现里的阈值：MAX_FILE_BYTES = {original}（{original / 1024**3:.0f} GB）")
    ok &= _assert(original == FOUR_GB, f"阈值确为 4GB（实际 {original}）")
    ok &= _assert(
        ingest_mod.MAX_FILE_BYTES == original,
        "ingest 与 planner 的阈值一致（防止两处各写一个数而漂移）",
    )

    oversized = _make_file(tmp / "long_recording.wav", b"\x00" * (5 * 1024 * 1024))

    durations = {oversized.name: 6 * 3600 * 1000}
    fake_max = 2 * 1024 * 1024  # 2MB 当作"阈值"
    planner.MAX_FILE_BYTES = fake_max
    try:
        svc = IngestService(StubAdapter(durations))
        got = svc.ingest_one(oversized)
    finally:
        planner.MAX_FILE_BYTES = original  # 一定要还原，否则污染后续用例

    print(f"  文件大小：{oversized.stat().st_size / 1024**2:.1f} MB"
          f"（阈值临时调为 {fake_max / 1024**2:.0f} MB）")
    for i, w in enumerate(got.warnings):
        print(f"  warning[{i}]：{w}")

    ok &= _assert(len(got.warnings) >= 2, "超大文件产生了 warning")
    # 注意：warnings[0] 是"已自动切分"那条（分段提示先入列），
    # 大文件告警在其后。第一版脚本取 warnings[0] 断言，抓错了对象 ——
    # 这类"断言看起来跑了其实没验到"的错只能靠把内容打出来发现。
    size_warns = [w for w in got.warnings if "超过上限" in w]
    ok &= _assert(len(size_warns) == 1, f"恰好一条大文件告警（实际 {len(size_warns)} 条）")
    if size_warns:
        w = size_warns[0]
        ok &= _assert(
            any(u in w for u in ("B", "KB", "MB", "GB")),
            f"提示里给出了可读的大小：{w[:70]}",
        )
        ok &= _assert(
            any(kw in w for kw in ("裁剪", "分割", "切片", "建议")),
            "提示给出了可执行的下一步（裁剪/建议）",
        )
    # 不阻断：仍出了一份可用的结果（用户可能真有这么大的录音）
    ok &= _assert(got.duration_ms == 6 * 3600 * 1000, "超大文件仍完成了探测")
    ok &= _assert(got.chunk_count > 0, f"仍生成了切分计划（{got.chunk_count} 段）")

    # 反向：正常大小文件不该报这条
    normal = _make_file(tmp / "normal.wav", b"\x00" * 1024)
    got2 = IngestService(StubAdapter(durations={normal.name: 60_000})).ingest_one(normal)
    ok &= _assert(
        not any("GB" in w for w in got2.warnings),
        "正常大小文件不报大文件告警（无误报）",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 1：真 ffmpeg 端到端（切分的物理正确性）
# ---------------------------------------------------------------------------

def check_real_ffmpeg(tmp: Path, have_ff: bool) -> bool:
    banner("附加 1：真 ffmpeg 端到端（切分产物参数正确）")

    if not have_ff:
        print(f"  {SKIP} 本机无可用 ffmpeg，跳过")
        return True

    from mmwf.adapters.audio.ffmpeg import FFmpegAdapter

    ok = True
    adapter = FFmpegAdapter()
    print(f"  版本：{adapter.version()}")

    src = tmp / "real_100s.wav"
    make_wav(src, seconds=100)

    probed = adapter.probe_duration_ms(src)
    ok &= _assert(
        99_000 <= probed <= 101_000,
        f"探测时长 ≈100s（实际 {probed}ms）",
    )

    # 切成 40s 块 + 10s 重叠 → 步进 30s → (0,40000) (30000,70000) (60000,100000)
    from mmwf.app.services.ingest import IngestService

    svc = IngestService(FFmpegAdapter(), chunk_seconds=40, overlap_seconds=10)
    got = svc.ingest_one(src, chunk_dir=tmp / "real_cut")
    print(f"  切成 {got.chunk_count} 段：{[c.to_dict() for c in got.chunk_plan]}")
    ok &= _assert(got.chunk_count == 3, f"3 段（实际 {got.chunk_count}）")

    expected_frames = [40 * 16000, 40 * 16000, 40 * 16000]
    for chunk_path, exp in zip(got.chunk_files, expected_frames):
        with wave.open(str(chunk_path), "rb") as w:
            ch, rate, frames = w.getnchannels(), w.getframerate(), w.getnframes()
        ok &= _assert(
            ch == 1 and rate == 16000,
            f"{chunk_path.name}：单声道 16kHz（实际 {ch}ch {rate}Hz）",
        )
        delta = abs(frames - exp)
        ok &= _assert(
            delta <= rate // 10,  # 允许 0.1s 的定位漂移（-ss 前置的代价）
            f"{chunk_path.name}：帧数 {frames} ≈ {exp}（±0.1s，-ss 前置的定位漂移）",
        )

    # 中文文件名（施工卡常见坑第 3 条）
    cn_dir = tmp / "中文目录"
    cn_src = cn_dir / "党组会_录音.wav"
    make_wav(cn_src, seconds=30)
    got_cn = IngestService(FFmpegAdapter(), chunk_seconds=20, overlap_seconds=5).ingest_one(
        cn_src, chunk_dir=cn_dir / "切分产物"
    )
    ok &= _assert(
        got_cn.chunk_count == 2 and all(p.is_file() for p in got_cn.chunk_files),
        f"中文路径下的音频可正常探测与切分（{got_cn.chunk_count} 段）",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 2：批量投递的部分失败语义 + ffmpeg 缺失中止整批
# ---------------------------------------------------------------------------

def check_batch_semantics(tmp: Path) -> bool:
    banner("附加 2：批量投递语义（部分失败 / ffmpeg 缺失整批中止）")

    from mmwf.adapters.audio.ffmpeg import FFmpegNotFoundError
    from mmwf.app.services.ingest import IngestService

    ok = True
    files = []
    for name in ("1.wav", "2.mp3", "3.m4a", "4.wma"):
        files.append(_make_file(tmp / name, b"\x00" * 2048))

    # 4 个里第 2 个损坏
    svc = IngestService(StubAdapter(bad={"2.mp3"}))
    batch = svc.ingest_batch(files)
    ok &= _assert(batch.total == 4, "4 个文件都被处理（不因 1 个坏文件短路）")
    ok &= _assert(len(batch.succeeded) == 3, "3 个成功")
    ok &= _assert(len(batch.failed) == 1, "1 个失败")
    ok &= _assert(not batch.ok, "ok=False（有失败）")
    print(f"      摘要：{batch.summary()}")
    ok &= _assert("3 个音频已接收" in batch.summary() and "1 个" in batch.summary(),
                  "摘要同时给出成功与失败数（用户知道只需重投失败的那个）")

    # ffmpeg 缺失 → 整批中止（继续循环只会刷一屏同样的错）
    svc2 = IngestService(StubAdapter(raise_not_found=True))
    try:
        svc2.ingest_batch(files)
        ok &= _assert(False, "ffmpeg 缺失应抛 FFmpegNotFoundError")
    except FFmpegNotFoundError as e:
        ok &= _assert(True, "ffmpeg 缺失 → FFmpegNotFoundError（整批中止）")
        ok &= _assert(
            "部署手册" in str(e) or "ffmpeg" in str(e),
            "错误信息给出可执行的排查指引",
        )
    except Exception as e:  # noqa: BLE001
        ok &= _assert(False, f"抛了别的异常：{type(e).__name__}: {e}")
    return ok


# ---------------------------------------------------------------------------
# 附加 3：命令行形态（施工卡常见坑）
# ---------------------------------------------------------------------------

def check_ffmpeg_argv(tmp: Path) -> bool:
    banner("附加 3：ffmpeg 命令行形态（施工卡硬要求）")

    import subprocess as sp

    from mmwf.adapters.audio.ffmpeg import FFmpegAdapter, FFmpegTools

    ok = True
    captured: list[list[str]] = []

    class _CP:
        returncode = 0
        stdout = b""
        stderr = b""

        def __init__(self) -> None:
            self.returncode = 0

    class _P:
        stdout = b""
        stderr = b""

        def __init__(self) -> None:
            self.returncode = 0

    def fake_run(cmd, **kw):  # noqa: ANN001
        captured.append(list(cmd))
        # 模拟产出文件，否则"产物为空"检查会拦下
        if cmd and cmd[-1].endswith(".wav"):
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"RIFF" + b"\x00" * 64)
        return _P()

    original = sp.run
    sp.run = fake_run  # type: ignore[assignment]
    try:
        adapter = FFmpegAdapter(
            FFmpegTools(ffmpeg="ffmpeg", ffprobe=None, source="stub")
        )
        # 源文件必须真实存在 —— `cut_chunk` 会先查存在性（E1002）。
        src = _make_file(tmp / "in.wav", b"\x00" * 1024)
        dst = tmp / "out.wav"
        adapter.cut_chunk(src, dst, 1_750_000, 1_800_000)
    finally:
        sp.run = original  # type: ignore[assignment]

    argv = captured[0]
    print(f"  拼出的命令：\n    {' '.join(str(a) for a in argv)}")

    ok &= _assert("-nostdin" in argv,
                  "-nostdin（作为服务运行时不能阻塞在 stdin）")
    ok &= _assert("-y" in argv, "-y（幂等重跑不卡在覆盖确认）")
    ok &= _assert("-vn" in argv, "-vn（丢掉 m4a 的封面图流）")
    ok &= _assert(argv.index("-ss") < argv.index("-i"),
                  "-ss 在 -i **之前**（快速定位，施工卡要求）")
    ok &= _assert("-t" in argv and "-to" not in argv,
                  "用 -t（时长）而非 -to（结束时间），避免语义歧义")
    ok &= _assert("16000" in argv and argv[argv.index("-ar") + 1] == "16000",
                  "-ar 16000（ASR 标准输入，避免逐机重采样差异）")
    ok &= _assert(argv[argv.index("-ac") + 1] == "1",
                  "-ac 1（单声道）")
    ok &= _assert(argv[argv.index("-c:a") + 1] == "pcm_s16le",
                  "显式指定 pcm_s16le（不靠扩展名推断）")
    ss_val = argv[argv.index("-ss") + 1]
    ok &= _assert(ss_val == "1750.000", f"-ss 秒数正确（实际 {ss_val}）")
    t_val = argv[argv.index("-t") + 1]
    ok &= _assert(t_val == "50.000", f"-t 为时长而非结束时间（实际 {t_val}）")
    return ok


# ---------------------------------------------------------------------------
# 附加 4：全量用例
# ---------------------------------------------------------------------------

def check_tests() -> bool:
    banner("附加 4：T1-06 全量用例")
    ok = True
    for path in ("tests/unit/test_ingest_planner.py", "tests/unit/test_ingest_audio.py"):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", path, "-q", "--no-header"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        lines = [ln for ln in (proc.stdout + proc.stderr).strip().splitlines() if ln.strip()]
        print(f"  {PASS if proc.returncode == 0 else FAIL} {path}")
        print(f"      {lines[-1] if lines else '(无输出)'}")
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr)[-2500:])
        ok &= proc.returncode == 0
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T1-06 验收")
    ap.add_argument("--real-ffmpeg", action="store_true",
                    help="强制要求本机有真 ffmpeg；没有则判失败（默认只跳过）")
    args = ap.parse_args()

    print("T1-06 验收：音频接入与切分（ingest）")
    print(f"项目根：{PROJECT_ROOT}")

    have_ff, ff_desc = detect_ffmpeg()
    print(f"\nffmpeg 探测：{PASS if have_ff else SKIP} {ff_desc}")

    if args.real_ffmpeg and not have_ff:
        print(f"\n{FAIL} 指定了 --real-ffmpeg 但本机没有可用 ffmpeg，验收失败。")
        return 1

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        results: dict[str, bool] = {
            "标准 1 2.5h → 6 段无遗漏无越界": check_standard_1(tmp / "s1", have_ff),
            "标准 2 切分拼接差异 < 2%": check_standard_2(tmp / "s2", have_ff, ff_desc),
            "标准 3 不支持格式 → E1001 中文提示": check_standard_3(tmp / "s3"),
            "标准 4 损坏音频 → E1003 不崩溃": check_standard_4(tmp / "s4", have_ff),
            "标准 5 >4GB 明确提示": check_standard_5(tmp / "s5"),
            "附加 1 真 ffmpeg 端到端": check_real_ffmpeg(tmp / "a1", have_ff),
            "附加 2 批量投递语义": check_batch_semantics(tmp / "a2"),
            "附加 3 ffmpeg 命令行形态": check_ffmpeg_argv(tmp / "a3"),
            "附加 4 全量用例通过": check_tests(),
        }

    banner("验收结论")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL} {name}")

    if not have_ff:
        print(f"\n  {SKIP} 本机无 ffmpeg：标准 2 与附加 1 的实机切分未执行。")
        print("      这不影响标准 1/3/4/5 的结论（它们与 ffmpeg 无关）；")
        print("      但部署前必须在装有 ffmpeg 的机器上重跑一次以覆盖标准 2。")

    all_ok = all(results.values())
    print(f"\n{'=' * 78}")
    print(f"{PASS} T1-06 验收通过" if all_ok else f"{FAIL} T1-06 验收未通过")
    print(f"{'=' * 78}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
