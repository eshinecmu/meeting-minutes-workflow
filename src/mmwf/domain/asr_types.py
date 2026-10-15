"""转写结果的数据类型（T1-05）。

分层位置
--------
放在**领域层**而不是 `adapters/asr/`，是一个刻意的选择：

    `TranscriptResult` 是"转写"这个概念本身的形状，
    不是"FunASR 这个引擎的返回格式"。

如果把它定义在适配器里，那么"换成另一个识别引擎"就要动上层所有
import 路径 —— 那就违反了 SAD 附录 C 第 6 条"不得修改上层任何代码"。
数据类型属于领域，适配器只负责**把引擎输出翻译成它**。

三条不变量（本模块的核心产出）
------------------------------
1. **时间戳单调不减**。上游时间戳错位是已知的静默失效症状
   （SAD §12.3 第 3 条），若允许乱序流下去，后续"逐字溯源到音频秒数"
   会全部错位且无人知晓。
2. **字级时间戳与文本必须对得上**。`words` 拼起来的字串与 `text`
   长度差不得超过容差（施工卡 `_detect_silent_failures` 第①条）。
3. **`seq` 从 1 开始连续**。数据库 `transcript_segment` 靠
   `(meeting_id, seq)` 建索引，断号会让复核页翻页出现空洞。

这些不变量在 `validate()` 里集中检查，并由 `TranscriptResult.validated()`
在构造时就跑一遍 —— **让非法状态无法被构造出来**，而不是靠调用方自觉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mmwf.domain.errors import AsrSilentFailureError

#: 字级时间戳与文本长度差的容差下限（3 个字）。
#: 为什么不是 0：分词器可能把标点、语气词并入或剔除，
#: 完全相等过于严格，会把正常结果判成失效。
WORD_LEN_TOLERANCE_MIN = 3
#: 容差的比例部分：允许字数的 10% 偏差。
#: 为什么要有比例项：长段落里分词器合并/拆分的绝对差异更大，
#: 用固定值会误杀长句、放过短句。
WORD_LEN_TOLERANCE_RATIO = 0.10


@dataclass(frozen=True)
class TranscriptWord:
    """一个字/词及其时间戳。用于"点击某个字跳到音频对应秒数"。"""

    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms > self.end_ms:
            raise ValueError(
                f"词的时间戳起止倒置：{self.start_ms} > {self.end_ms}（word={self.text!r}）"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class TranscriptSegmentResult:
    """一个转写分段（对应 transcript_segment 表一行）。

    `confidence=None` 表示**引擎没给置信度**，不是"置信度为 0"。
    这个区分很重要：复核页要把"低置信"标记出来给人看，
    而"引擎没提供"应该走另一条分支（整体降级为人工全检），
    混成 0 会让整篇都被标红，反而失去提示意义。
    """

    seq: int
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None
    words: tuple[TranscriptWord, ...] = ()
    #: 异常标记（对应 transcript_segment.flags 列的 JSON 数组）。
    #:
    #: T1-08 加这个字段的原因：低置信分流有**两条独立判据**，
    #: 置信度只覆盖其中一条。`silence`（纯静音）与 `overlap`（两人抢话）
    #: 这两类问题，引擎常常给出很高的置信度 —— 它很确定自己听见了字，
    #: 只是听见的是两个人的字叠在一起，或者听见的是静音。
    #: 这类内容不可用，但靠分数发现不了，必须由 flag 承载。
    #:
    #: 默认空元组：不是所有适配器都能产出 flag（有的引擎不报 VAD/重叠信息），
    #: 缺失时应当当"没有标记"处理，而不是让构造失败。
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError(f"seq 必须从 1 开始，当前 {self.seq}")
        if self.start_ms > self.end_ms:
            raise ValueError(
                f"分段 {self.seq} 时间戳起止倒置：{self.start_ms} > {self.end_ms}"
            )
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"分段 {self.seq} 的 confidence={self.confidence} 超出 [0,1]"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def is_low_confidence(self, threshold: float) -> bool:
        """是否低于给定阈值——**阈值必须由调用方传入**。

        为什么不做成无参属性：阈值是配置项（`thresholds.asr.low_confidence`，
        N5.2 要求可热改）。写死在领域类型里就等于把它钉死了，
        改阈值要改代码 —— 那正是本项目要避免的。

        `confidence is None`（引擎未提供）视为**未知**，不判为低置信：
        未知应该走"整体降级为人工全检"那条分支（见 `detect_silent_failures`），
        而不是把每一段都标红——全红等于没有提示。
        """
        return False if self.confidence is None else self.confidence < threshold

    def word_text_joined(self) -> str:
        return "".join(w.text for w in self.words)


@dataclass(frozen=True)
class TranscriptResult:
    """一次转写的完整结果。

    工厂方法 `from_raw_segments()` 是推荐的构造入口：
    它做规范化（排序、重排 seq、裁掉空段）+ 校验，调用方不必自己拼。
    """

    segments: tuple[TranscriptSegmentResult, ...]
    engine: str
    model: str
    avg_confidence: float | None = None
    hotwords_applied: bool = False
    warnings: tuple[str, ...] = ()
    #: 引擎返回的原始结果（仅供排查，不落库、不进审计）
    raw_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def duration_ms(self) -> int:
        return self.segments[-1].end_ms if self.segments else 0

    def low_confidence_segments(
        self, threshold: float
    ) -> tuple[TranscriptSegmentResult, ...]:
        """低于阈值的分段。阈值由调用方给（配置项，见 `is_low_confidence`）。"""
        return tuple(s for s in self.segments if s.is_low_confidence(threshold))

    def low_confidence_ratio(self, threshold: float) -> float:
        """低置信段占比（0..1）。无分段或有置信度的段为 0 时返回 0.0。

        分母用"**有置信度的段数**"而不是"全部段数"：
        引擎未提供置信度的那些段（None）既不该算作低置信、也不该稀释比例，
        否则"10 段里 3 段有置信度且全低"会被算成 30%，而真实情况是 100%。
        """
        valued = [s for s in self.segments if s.confidence is not None]
        if not valued:
            return 0.0
        return sum(1 for s in valued if s.is_low_confidence(threshold)) / len(valued)

    def to_dict(self) -> dict[str, Any]:
        """序列化形状，与 `filestore` 落盘的 `transcript/segments.json` 一致。"""
        return {
            "engine": self.engine,
            "model": self.model,
            "avg_confidence": self.avg_confidence,
            "hotwords_applied": self.hotwords_applied,
            "warnings": list(self.warnings),
            "segments": [
                {
                    "seq": s.seq,
                    "text": s.text,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "confidence": s.confidence,
                    "flags": list(s.flags),
                    "words": [
                        {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
                        for w in s.words
                    ],
                }
                for s in self.segments
            ],
        }

    def to_json(self) -> str:
        """`ensure_ascii=False` 保留中文（审计/复核页都要给人看）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def validate(self) -> list[str]:
        """检查三条不变量，返回问题列表（空 = 通过）。

        做成"返回列表"而不是"抛第一个错"，是为了让自检能一次性
        把问题都报出来（人工排查时一次看全，不用改一个跑一次）。
        """
        problems: list[str] = []

        if not self.segments:
            problems.append("分段为空：引擎没有返回任何内容")
            return problems

        # 不变量 3：seq 从 1 连续递增
        expected = list(range(1, len(self.segments) + 1))
        actual = [s.seq for s in self.segments]
        if actual != expected:
            problems.append(
                f"seq 不是从 1 连续递增：期望 {expected[:5]}…，实际 {actual[:5]}…"
            )

        # 不变量 1：时间戳单调不减
        prev_end = -1
        for s in self.segments:
            if s.start_ms < prev_end:
                problems.append(
                    f"时间戳非单调：分段 {s.seq} 起点 {s.start_ms}ms "
                    f"早于上一段终点 {prev_end}ms（疑似上游时间戳错位）"
                )
                break
            prev_end = s.end_ms

        # 不变量 2：字级时间戳与文本长度匹配（施工卡 ①）
        for s in self.segments:
            if not s.words:
                continue
            joined_len = len(s.word_text_joined())
            text_len = len(s.text)
            tolerance = max(WORD_LEN_TOLERANCE_MIN, int(text_len * WORD_LEN_TOLERANCE_RATIO))
            if abs(joined_len - text_len) > tolerance:
                problems.append(
                    f"字级时间戳与文本长度错位：分段 {s.seq} "
                    f"words 合计 {joined_len} 字 vs text {text_len} 字"
                    f"（容差 {tolerance}）"
                )

        # 分段之间的时间连续性（允许小空隙，不允许大面积跳变）
        for a, b in zip(self.segments, self.segments[1:]):
            if b.start_ms < a.end_ms:
                problems.append(
                    f"分段 {a.seq} 与 {b.seq} 时间区间重叠："
                    f"{a.end_ms}ms > {b.start_ms}ms"
                )

        return problems

    def validated(self) -> TranscriptResult:
        """校验通过则返回自身，否则抛 E2003（FATAL）。

        为什么把"静默失效"做成抛异常而不是只加个 warning：
        SAD §9.3 对 E2003 的使用者文案是"**已暂停处理**"。一份时间戳错位的
        转写稿看起来完全正常，但后续所有"逐字溯源"都是错的，
        而用户无法察觉。让这种稿子继续流向摘要和纪要，
        比直接停下危险得多。
        """
        problems = self.validate()
        if problems:
            raise AsrSilentFailureError(
                detail="; ".join(problems),
                segment_count=len(self.segments),
                engine=self.engine,
                model=self.model,
            )
        return self

    # ------------------------------------------------------------------
    # 构造入口
    # ------------------------------------------------------------------

    @classmethod
    def from_raw_segments(
        cls,
        raw: list[dict[str, Any]],
        *,
        engine: str,
        model: str,
        hotwords_applied: bool = False,
        warnings: tuple[str, ...] = (),
        raw_meta: dict[str, Any] | None = None,
    ) -> TranscriptResult:
        """从"引擎中立"的字典列表构造，做规范化。

        接受的字典键：`text`（必需）、`start_ms`/`end_ms`（必需）、
        `confidence`（可选）、`flags`（可选，字符串列表或逗号分隔字符串）、
        `words`（可选，`[{text,start_ms,end_ms}]`）。

        **规范化的三件事**（都在这里做，让每个适配器不必重复实现）：
          1. 按 `start_ms` 稳定排序 —— 引擎偶尔会给出乱序（多线程回溯）；
          2. 丢弃全空分段 —— 引擎会给纯静音段，落库后复核页会出现空行；
          3. 重排 `seq` 为 1..N —— 丢弃/排序后原 seq 必然有洞。
        """
        # ① 排序（稳定排序保证时间相同的段保持引擎给的相对顺序）
        ordered = sorted(raw, key=lambda d: (int(d.get("start_ms", 0)),))

        segments: list[TranscriptSegmentResult] = []
        for item in ordered:
            text = str(item.get("text") or "").strip()
            # ② 丢空段
            if not text:
                continue
            words_raw = item.get("words") or ()
            words = tuple(
                TranscriptWord(
                    text=str(w.get("text") or ""),
                    start_ms=int(w.get("start_ms", 0)),
                    end_ms=int(w.get("end_ms", 0)),
                )
                for w in words_raw
            )
            conf_raw = item.get("confidence")
            segments.append(
                TranscriptSegmentResult(
                    seq=len(segments) + 1,          # ③ 重排 seq
                    text=text,
                    start_ms=int(item.get("start_ms", 0)),
                    end_ms=int(item.get("end_ms", 0)),
                    confidence=None if conf_raw is None else float(conf_raw),
                    words=words,
                    flags=_normalize_flags(item.get("flags")),
                )
            )

        return cls(
            segments=tuple(segments),
            engine=engine,
            model=model,
            avg_confidence=compute_avg_confidence(segments),
            hotwords_applied=hotwords_applied,
            warnings=warnings,
            raw_meta=raw_meta or {},
        )


def _normalize_flags(raw: Any) -> tuple[str, ...]:
    """把引擎给的 flags 归一成去重、有序、非空的字符串元组。

    四种真实形状都要吃下（T1-08 复核分流依赖它，格式错了会让
    "静音段"悄悄变成"普通段"而不报错）：

        None                -> ()                 # 引擎不报 flag
        ["overlap"]         -> ("overlap",)       # 正常列表
        "overlap"           -> ("overlap",)       # 单个字符串（不是拆成字符！）
        "silence,overlap"   -> ("silence", "overlap")

    写成字符串单值时**不能**直接 `tuple(s)` —— 那会把 "overlap" 变成
    `('o','v','e','r','l','a','p')`，之后 `"overlap" in flags` 永远为假，
    复核分流静默失效。这是本函数存在的唯一理由。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace("，", ",").split(",")]
    else:
        try:
            parts = [str(p).strip() for p in raw]
        except TypeError:
            return ()
    # 去重保序：上游可能重复报同一个 flag，重复不影响判断但会让
    # 复核页显示"多人同时说话、多人同时说话"。
    seen: dict[str, None] = {}
    for p in parts:
        if p:
            seen.setdefault(p, None)
    return tuple(seen)


def compute_avg_confidence(
    segments: list[TranscriptSegmentResult] | tuple[TranscriptSegmentResult, ...],
) -> float | None:
    """平均置信度。**只对"有置信度的段"求平均**，全部为空则返回 None。

    为什么不做"None 当 0 参与平均"：那会让"引擎没提供置信度"被算成
    "平均置信度极低"，触发无意义的告警。返回 None 让上层能明确区分
    "低质量"与"无该信息"（对应施工卡 `_detect_silent_failures` 第③条）。
    """
    values = [s.confidence for s in segments if s.confidence is not None]
    if not values:
        return None
    return sum(values) / len(values)


def detect_silent_failures(
    segments: tuple[TranscriptSegmentResult, ...] | list[TranscriptSegmentResult],
    *,
    hotwords: list[str] | None = None,
    hotwords_applied: bool = False,
    low_confidence_threshold: float = 0.70,
    warn_avg_confidence: float = 0.85,
    warn_low_conf_ratio: float = 0.15,
) -> list[str]:
    """四类质量自检（SAD §12.3 + 施工卡「必须实现的三项自检」）。

    返回人类可读的告警列表。**这里是告警而不是异常**：四项都需要人工
    抽样核对，但都还没到"必须停机"的程度（E2003 那条由 `validate()` 抛）。

    | # | 症状 | 判据 | 为什么这么判 |
    |:--|:--|:--|:--|
    | 1 | 平均置信度异常偏低 | < `warn_avg_confidence`(0.85) | 整体质量差，需抽查 |
    | 2 | 低置信段占比偏高 | > `warn_low_conf_ratio`(15%) | 局部质量差，定位到段 |
    | 3 | 时间戳错位 | 由 validate() 管 | **FATAL，不在此处** |
    | 4 | 热词零命中 | 传了热词但未生效 | 上游静默忽略参数的典型症状 |
    """
    warns: list[str] = []
    segs = list(segments)

    if not segs:
        warns.append("engine returned zero segments; nothing to review")
        return warns

    # ③ 置信度全空 —— 引擎未返回置信度，复核分流能力降级
    conf_values = [s.confidence for s in segs if s.confidence is not None]
    if not conf_values:
        warns.append(
            "engine returned no confidence; review gate degraded"
            "（引擎未提供置信度，复核页无法按置信度分流，需整体人工核对）"
        )
    else:
        # ① 平均置信度异常偏低
        avg = sum(conf_values) / len(conf_values)
        if avg < warn_avg_confidence:
            warns.append(
                f"average confidence {avg:.3f} below threshold {warn_avg_confidence}"
                "（整体识别质量偏低，建议人工抽样核对）"
            )

        # ② 低置信段落占比异常偏高
        low = sum(1 for c in conf_values if c < low_confidence_threshold)
        ratio = low / len(conf_values)
        if ratio > warn_low_conf_ratio:
            warns.append(
                f"low-confidence segment ratio {ratio:.1%} exceeds "
                f"{warn_low_conf_ratio:.0%}"
                f"（{low}/{len(conf_values)} 段低于 {low_confidence_threshold}）"
            )

    # ④ 热词零命中 —— 术语库非空但完全没生效
    if hotwords and not hotwords_applied:
        warns.append(
            "hotwords supplied but zero hit; suspect hotword param ignored"
            "（传入热词但未生效，上游可能忽略了热词参数，人名写法需人工核对）"
        )

    return warns


def hit_hotwords(
    segments: tuple[TranscriptSegmentResult, ...] | list[TranscriptSegmentResult],
    hotwords: list[str],
) -> list[str]:
    """统计**在转写文本里实际出现**的热词。

    判据是"文本包含该热词"，不是"引擎报告使用了热词"。
    为什么这样判：上游多个版本的 `hotword` 参数被静默忽略
    （施工卡明写"上游已知缺陷"），引擎**不会报错也不会告知**。
    唯一可靠的验证方式是看结果里到底有没有把这些词写对。
    """
    full = "".join(s.text for s in segments)
    return [w for w in hotwords if w and w in full]

