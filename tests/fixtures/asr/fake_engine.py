"""离线假转写引擎（T1-05 测试夹具，**不进生产代码**）。

为什么需要它
------------
T1-05 的验收标准要求"转写 `sample_5min.m4a` 成功、段落数 > 0、
时间戳单调递增"以及"术语库里 3 个人名在音频中出现 → 转写后写法正确"。
这些断言需要**跑通整条链路**，而真引擎有三个现实障碍：

1. `funasr` + `torch` 是几个 GB 的大件，施工卡明确写"等 T1-05 开工时
   按当时的硬件条件单独装"——日常门禁机器上不一定有；
2. 模型 220MB 要预先离线下载，CI 里没有；
3. 真引擎单次推理几十秒，进不了"每次改动都跑"的门禁。

所以：**真引擎的接口一致性**由 `tests/contract/test_asr_contract.py`
的 `real_model` 用例守（有模型就跑，没有就 skip）；
**业务链条的正确性**由本夹具守。

为什么它必须是"确定性"的
------------------------
假引擎**不做真实识别**，而是按音频的时长和字节内容**推导出一份固定结果**。
这样验收脚本可以对结果做精确断言（"第 3 段的文本必须是 X"），
而不是"跑起来不报错就行"——后者等于没验。

它**注册进真正的注册表**（`adapters.asr.register`），
因此走的是和真引擎完全相同的代码路径：
`build_from_settings` → `AsrProvider` 四个方法 → `TranscriptResult`。
这正是"新增引擎 = 新文件 + 注册"这条 SAD 附录 C 要求的实际演练。

关键：它明确模拟 FunASR 的**两个真实缺陷**，好让验收标准 3 有意义
-----------------------------------------------------------------
· `simulate_hotword_ignored=True` 时，**接受热词参数但完全不用它** ——
  这就是上游"静默忽略热词"的行为。验收标准 3 要求此时
  `warnings` 必须包含 `hotwords supplied but zero hit`，
  只有夹具真的复现了这个缺陷，那条断言才在验真东西。
· 返回的 timestamp 可以是"逐字"也可以是"整段一对"，
  两种形状都要能被 `_normalize` 吃下。
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any

from mmwf.domain.asr_types import (
    TranscriptResult,
    TranscriptSegmentResult,
    TranscriptWord,
    compute_avg_confidence,
    detect_silent_failures,
    hit_hotwords,
)
from mmwf.domain.errors import FileMissingError

PROVIDER_NAME = "fake_offline"

#: 每个字占多少毫秒。180ms/字 ≈ 5.5 字/秒，接近正常中文播报语速。
#: 这个常数同时决定了"50 秒重叠区大约多少字"（约 280 字）——
#: 是 T1-06 标准 2 里重叠区去重能不能被真正压到的关键（见 `_slice_script`）。
_CHAR_MS = 180

#: 夹具专有的"台词本"。真引擎没有这个，这里只是为了断言可精确。
#: 刻意包含 3 个术语库人名（张建国 / 李国强 / 王振华），
#: 对应验收标准 2 的"5 个名字里有 3 个出现在音频中"。
SCRIPT_LINES: tuple[str, ...] = (
    "同志们今天我们召开党组会",
    "先请张建国同志汇报前期工作",
    "李国强同志补充一下基层调研情况",
    "王振华同志谈谈下一步打算",
    "最后我再强调三点意见",
)


class FakeOfflineProvider:
    """确定性假引擎。实现 `AsrProvider` 的四个方法。

    `respect_duration=True` 时进入**时长敏感模式**（T1-06 拼接验收用）
    -----------------------------------------------------------------
    默认模式下夹具"不看音频内容"，无论传进来的是整段 60 秒音频还是切出来的
    20 秒片段，都返回完整台词本。这会让 T1-06 的标准 2 变成一句废话：
    每段都返回全文，去重自然 100% 命中，拼接结果必然等于参考文本。

    真正要验的是"切分后每段只覆盖台词本的一部分，重叠区被正确去重"。
    所以这个模式做两件事：

      1. 读音频的**真实时长**（`wave` 标准库，兼容参数变化）；
      2. 把台词本按"每字 `_CHAR_MS` 毫秒"折算成一条时间轴，
         只返回与该音频时长相交的那部分文字。

    注意偏移量的估计：夹具不知道"这段音频是从原文件第几秒切的"，
    所以用 `假设 (文件大小 ≈ 时长)` 的办法反推——见 `_slice_script` 的说明。
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        model_name: str = "fake-offline-zh",
        model_dir: Path | str = "./models/fake",
        device: str = "cpu",
        *,
        lines: tuple[str, ...] | None = None,
        simulate_hotword_ignored: bool = False,
        simulate_no_confidence: bool = False,
        words_mode: str = "per_char",
        fail_with: str | None = None,
        respect_duration: bool = False,
    ) -> None:
        self._model_name = model_name
        self._model_dir = Path(model_dir)
        self._device = device
        self._lines = lines if lines is not None else SCRIPT_LINES
        self._hotword_ignored = simulate_hotword_ignored
        self._no_confidence = simulate_no_confidence
        #: "per_char" = 逐字时间戳；"per_segment" = 整段一对（真实也见过）
        self._words_mode = words_mode
        #: 注入指定异常类型名，用于验证错误码翻译
        self._fail_with = fail_with
        #: 是否按"音频真实时长"裁剪台词（T1-06 拼接验收需要，见类文档）
        self._respect_duration = respect_duration
        self._loaded = False
        self._last_hit_hotwords: list[str] = []
        self._last_generate_kwargs: dict[str, Any] = {}

    # -- 契约方法 1：只探测，不加载 --------------------------------------
    def available(self) -> bool:
        return self._fail_with != "unavailable"

    # -- 契约方法 2：显式加载 --------------------------------------------
    def warmup(self) -> None:
        if self._fail_with == "not_ready":
            from mmwf.domain.errors import AsrNotReadyError

            raise AsrNotReadyError(detail="假引擎：模型目录未就绪（注入故障）")
        self._loaded = True

    # -- 契约方法 3：纯函数式转写 ----------------------------------------
    def transcribe(
        self,
        audio_path: Path | str,
        hotwords: list[str] | None = None,
    ) -> TranscriptResult:
        path = Path(audio_path)
        if not path.exists():
            raise FileMissingError(detail=f"音频文件不存在：{path}", audio_path=str(path))
        if self._fail_with == "corrupt":
            from mmwf.domain.errors import CorruptAudioError

            raise CorruptAudioError(
                detail="假引擎：解码失败（注入故障）", audio_path=str(path)
            )

        self.warmup()
        words_hotwords = [w.strip() for w in (hotwords or []) if w and w.strip()]
        self._last_generate_kwargs = {
            "input": str(path),
            "batch_size_s": 300,
            "sentence_timestamp": True,
            **({"hotword": " ".join(dict.fromkeys(words_hotwords))} if words_hotwords else {}),
        }

        # 台词本：respect_duration=True 时按音频真实时长 + 偏移标记裁剪
        lines = self._lines
        offset_ms = 0
        if self._respect_duration:
            dur_ms = _wav_duration_ms(path)
            if dur_ms is None:
                lines = ()      # 读不出时长 → 空结果（更接近真引擎对垃圾输入的行为）
            else:
                offset_ms = _read_offset_marker(path) or 0
                lines = _slice_script(self._lines, offset_ms, dur_ms)

        # 时间戳按"每字固定节奏"铺开，保证单调递增。
        # 注意 `offset_ms` 是"本段音频在原始台词本里的起点"：夹具不做真实识别，
        # 而是假设每段音频从台词本的某个偏移开始，从而让"切分后的每段只
        # 覆盖台词本的一部分"——这正是标准 2 要去重的东西。
        segments: list[TranscriptSegmentResult] = []
        cursor = offset_ms
        for i, line in enumerate(lines, start=1):
            dur = max(600, len(line) * _CHAR_MS)
            if self._words_mode == "per_char":
                step = dur // max(1, len(line))
                words = tuple(
                    TranscriptWord(
                        text=ch,
                        start_ms=cursor + j * step,
                        end_ms=cursor + (j + 1) * step,
                    )
                    for j, ch in enumerate(line)
                )
            else:
                words = (
                    TranscriptWord(text=line, start_ms=cursor, end_ms=cursor + dur),
                )
            segments.append(
                TranscriptSegmentResult(
                    seq=i,
                    text=line,
                    start_ms=cursor,
                    end_ms=cursor + dur,
                    confidence=None if self._no_confidence else 0.94,
                    words=words,
                )
            )
            cursor += dur + 120          # 段间留 120ms 空隙

        # 坑 ④：模拟"热词被静默忽略"—— 结果里一个热词都不出现
        if self._hotword_ignored and words_hotwords:
            placeholder = "某某同志"
            segments = [
                TranscriptSegmentResult(
                    seq=s.seq,
                    text=placeholder,
                    start_ms=s.start_ms,
                    end_ms=s.end_ms,
                    confidence=s.confidence,
                    words=tuple(
                        TranscriptWord(
                            text=ch,
                            start_ms=w.start_ms,
                            end_ms=w.end_ms,
                        )
                        for ch, w in zip(placeholder * 10, s.words)
                    )[: len(placeholder)],
                )
                for s in segments
            ]

        hits = hit_hotwords(segments, words_hotwords)
        self._last_hit_hotwords = hits
        applied = bool(words_hotwords) and bool(hits)

        return TranscriptResult(
            segments=tuple(segments),
            engine=self.name,
            model=self._model_name,
            avg_confidence=compute_avg_confidence(segments),
            hotwords_applied=applied,
            warnings=tuple(
                detect_silent_failures(
                    segments, hotwords=words_hotwords, hotwords_applied=applied
                )
            ),
            raw_meta={
                "device": self._device,
                "simulated": True,
                "hotwords_hit": hits,
                "audio_sha1": hashlib.sha1(path.read_bytes()[:65536]).hexdigest()[:12],
            },
        )

    # -- 契约方法 4：结构化自检 ------------------------------------------
    def health_check(self) -> dict[str, Any]:
        return {
            "available": self.available(),
            "model": self._model_name,
            "device": self._device,
            "hotword_supported": True,
            "loaded": self._loaded,
            "last_hit_hotwords": list(self._last_hit_hotwords),
            "status": "ok" if self.available() else "unavailable",
            "expected_speed": "离线夹具：瞬时返回",
            "simulated": True,
        }

    def __repr__(self) -> str:
        return f"FakeOfflineProvider(model={self._model_name!r}, device={self._device!r})"


def install(
    *,
    lines: tuple[str, ...] | None = None,
    simulate_hotword_ignored: bool = False,
    words_mode: str = "per_char",
    respect_duration: bool = False,
) -> None:
    """把假引擎注册进真正的注册表（供验收脚本与测试使用）。"""
    from mmwf.adapters.asr import register

    def factory(**kwargs: Any) -> FakeOfflineProvider:
        return FakeOfflineProvider(
            lines=lines,
            simulate_hotword_ignored=simulate_hotword_ignored,
            words_mode=words_mode,
            respect_duration=respect_duration,
            **kwargs,
        )

    register(PROVIDER_NAME, factory, override=True)


def _wav_duration_ms(path: Path) -> int | None:
    """读 wav 的真实时长（毫秒）。非 wav / 读不出时返回 None。"""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            if rate <= 0:
                return None
            return int(round(w.getnframes() / rate * 1000))
    except Exception:  # noqa: BLE001 - 夹具不该因为读不出时长而抛错
        return None


#: 夹具音频的"偏移标记"：头 32 个采样点被替换成一个固定的
#: 32 位魔数 + 偏移量（毫秒）。真音频不可能带这个序列，
#: 所以标记不存在时如实返回 None，不会误判真实音频。
_MARKER_MAGIC = 0x4D4D57  # "MMW"：本项目前缀，避免与任何真实音频巧合
_MARKER_LEN = 40          # 3(magic) + 1(hi) + 18(offset) 个采样点，留余量


def write_offset_marker(path: Path, offset_ms: int) -> None:
    """在 wav 开头写"本片段起始于原文件第 offset_ms 毫秒"的标记。

    为什么需要它：`AsrProvider.transcribe(audio_path)` 的签名里**没有**
    "这段音频在原始时间轴上的位置"，而 T1-06 的标准 2 恰恰要验
    "切分后每段只覆盖台词本的一部分、重叠区被正确去重"。没有偏移信息，
    夹具只能假设每段都从 0 开始 —— 那样重叠区根本对不上，验不出真东西。

    编码方式刻意选得**最笨最稳**：直接用采样点承载数字，不做傅里叶变换、
    不做频率调制。测试基建不该自己变成被测对象。

    格式（小端 int16 序列，共 40 个采样点）：
        [0] =  0x4D ('M')
        [1] =  0x4D ('M')
        [2] =  0x57 ('W')
        [3] =  0x270F (9999, 格式版本)
        [4..9]  = offset_ms 的十进制数字逐位存（最多 6 位，补 0 前缀）
        [10..39] = 0（分隔与余量）

    真音频读到 [0..3] 不等于魔数时返回 None，走默认偏移 0。
    """
    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
    except Exception:  # noqa: BLE001 - 夹具不该因为造标记失败而抛错
        return

    import array as _array

    samples = _array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if len(samples) < _MARKER_LEN:
        return

    digits = f"{int(offset_ms):06d}"[-6:].encode("ascii")
    samples[0] = 0x004D
    samples[1] = 0x004D
    samples[2] = 0x0057
    samples[3] = 9999
    for i, d in enumerate(digits):
        samples[4 + i] = d          # ASCII 码，天然就是 48..57，不会越界
    for i in range(10, _MARKER_LEN):
        samples[i] = 0

    with wave.open(str(path), "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(samples.tobytes() + frames[len(samples) * 2 :])


def _read_offset_marker(path: Path) -> int | None:
    """读回 `write_offset_marker` 写入的偏移（毫秒）。无标记返回 None。"""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            raw = w.readframes(min(_MARKER_LEN, w.getnframes()))
    except Exception:  # noqa: BLE001
        return None

    import array as _array

    samples = _array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if len(samples) < 10:
        return None
    if (samples[0], samples[1], samples[2], samples[3]) != (0x4D, 0x4D, 0x57, 9999):
        return None
    try:
        digits = "".join(chr(int(samples[4 + i])) for i in range(6))
        return int(digits)
    except (ValueError, OverflowError):
        return None


def _slice_script(
    lines: tuple[str, ...], offset_ms: int, chunk_ms: int
) -> tuple[str, ...]:
    """把台词本投影到"原始时间轴 `[offset_ms, offset_ms+chunk_ms)`"上。

    `offset_ms` 由 `write_offset_marker` 写在夹具音频里（见其文档）。
    没有标记时调用方传 0 —— 也就是"这段就是从开头切的"，适用于
    "直接转写整段音频"的参考路径。

    返回与该窗口相交的台词行。相邻块在重叠区会返回**相同的行**，
    这正是 `merge_overlapping_texts` 要处理的输入。
    """
    start, end = offset_ms, offset_ms + chunk_ms
    picked: list[str] = []
    cursor = 0
    for line in lines:
        dur = max(600, len(line) * _CHAR_MS) + 120
        line_start, line_end = cursor, cursor + dur
        if line_end > start and line_start < end:
            picked.append(line)
        cursor = line_end
    return tuple(picked)
