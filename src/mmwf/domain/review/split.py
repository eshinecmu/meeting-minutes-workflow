"""低置信分流规则（T1-08，SAD §11 F2.1）。

需求原文（F2.1）：**"不设全文校对"**。
四个字是这一整张卡的立足点：一份 3 小时的会议转写稿动辄上千段，
如果全部推给人工逐段确认，那等于让用户重听一遍录音 —— 自动化就没意义了。
所以只把**机器自己都不确定**的段落挑出来，其余默认"可用"。

三条分流判据（施工卡 `needs_review` 原文，这里逐条落地）
------------------------------------------------------
| # | 判据 | 为什么 |
|:--|:--|:--|
| 1 | `confidence < low_confidence`(0.70) | 引擎自己报的低分，最直接的信号 |
| 2 | `flags` 含 `silence` / `overlap` | 纯静音、两人抢话 —— 这两种情况**不管分数多高都得看** |
| 3 | `len(text.strip()) <= 1` | 残余噪声（"嗯"、单字）。可能是引擎切分残留，也可能是真有语气词 |

第 2 条为什么必须独立于第 1 条
-----------------------------
`confidence` 是**整体**的声学置信度。一段"两人同时说话"的音频，
引擎可能给出 0.95 —— 它很有信心自己听见了字，但听见的是**两个人的字叠在一起**，
内容本身不可用。这类错误靠分数发现不了，只能靠 flag。
反过来 `silence` 也一样：静音段的置信度可能很高（引擎很确定"这里是静音"），
但那一段根本不该出现在记录稿正文里。

`confidence=None` 的处置（施工卡「常见坑」第一条，最要紧的一条）
--------------------------------------------------------------
`None` 表示**引擎没提供置信度**，不是"置信度为零"。
如果把 `None < 0.70` 当成真（Python 里会 `TypeError`；若用 `or 0.0` 兜底
则恒为真），整篇稿子会被全量送入人工复核 —— 一个"引擎版本没带分数"的
小差异，会退化成"每个会议都要人工全检"。所以必须写 `is not None`
显式判断，`None` 一律不算低置信。

"引擎没提供置信度"这个整体降级由谁管：`domain/asr_types.py` 的
`detect_silent_failures()` 会报 `review gate degraded` 告警（T1-05 已实现），
由调度层决定是否提示用户"本次需整体人工核对"。本模块**不**擅自扩大
复核范围 —— 分流规则只管它自己这一条线。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

#: 触发复核的异常标记（施工卡原文只写这两个）。
#:
#: 刻意**不做成"任何 flag 都送复核"**：`transcript_segment.flags` 这一列
#: （001_init.sql）是 JSON 数组，将来上游会往里加各种标记
#: （`low_conf` / `hotword_miss` / `punctuation_fixed`…）。
#: 如果只要有标记就送复核，那新增一个纯信息性的 flag 就会静默地把
#: 复核量翻倍。白名单是这里的正确形状：新标记要**显式**决定是否复核。
REVIEW_FLAGS: frozenset[str] = frozenset({"silence", "overlap"})

#: 疑似残余噪声的最大字数。`<= 1` 而不是 `== 0`：
#: 空段在 `TranscriptResult.from_raw_segments` 里已经被丢掉了（T1-05），
#: 能走到这里的"1 个字"多半是切分残留（"嗯"/"啊"/半个词）。
#: 2 个字起就不再判 —— "同意"、"可以"这类两字回应在会议里是有效内容，
#: 误判成噪声会让人白看一堆正常段落。
MIN_TEXT_CHARS_FOR_AUTO_PASS = 2


@runtime_checkable
class _ReviewableSegment(Protocol):
    """分流只需要三个属性。

    用 Protocol 而不是绑死 `TranscriptSegmentResult`：单测里可以直接传
    一个 `SimpleNamespace`，不必为了测"置信度为 None"去构造完整对象；
    而且将来若出现"编辑后的分段"（`segment_edit` 产物）也能复用同一套规则。
    """

    text: str
    confidence: float | None


@dataclass(frozen=True)
class ReviewDecision:
    """一个分段的分流结论（带**为什么**，供复核页显示）。"""

    seq: int
    needs_review: bool
    reason: str            # 空串 = 无需复核

    def __bool__(self) -> bool:
        return self.needs_review


def review_reason(seg: Any, thresholds: dict[str, Any]) -> str:
    """返回"为什么需要复核"，不需要复核则返回空串。

    为什么把判据和理由写在同一个函数里：分散成
    `needs_review()` + `explain()` 两份实现，迟早会漂移 ——
    复核页显示"因低置信"但实际送复核的原因已经改成别的了，很难发现。
    单一实现、两种用法（判真值 / 取文案）从结构上避免这件事。

    `thresholds` 的形状与 `config/thresholds.yaml` 一致::

        {"asr": {"low_confidence": 0.70, ...}}

    Args:
        seg: 任何有 `text` / `confidence` 属性的对象
             （`TranscriptSegmentResult` 或测试替身）。
        thresholds: 阈值字典。**必须由调用方传入**（N5.2 可热改），
             本模块不读配置文件、不设默认值 —— 一旦有默认值，
             配置改了却不生效就成了新的静默失效。

    Returns:
        人类可读的中文理由；不需要复核时返回 `""`。
    """
    conf = getattr(seg, "confidence", None)
    threshold = _low_confidence_threshold(thresholds)

    # 判据 1：低置信。**必须 `is not None`**（见模块文档）。
    if conf is not None and conf < threshold:
        return f"识别置信度偏低（{conf:.2f} < {threshold:.2f}）"

    # 判据 2：异常标记。`flags` 可能不存在（T1-05 的 dataclass 还没有这个字段），
    # 用 getattr 兜底 —— 缺字段当"没有标记"，而不是报错。
    flags = set(getattr(seg, "flags", None) or ())
    hit_flags = sorted(flags & REVIEW_FLAGS)
    if hit_flags:
        return "音频异常（" + "、".join(_FLAG_LABELS.get(f, f) for f in hit_flags) + "）"

    # 判据 3：残余噪声
    text = getattr(seg, "text", "") or ""
    stripped = text.strip()
    if len(stripped) <= 1:
        shown = stripped if stripped else "（空白）"
        return f"疑似残余噪声（仅 {len(stripped)} 字符：{shown}）"

    return ""


def needs_review(seg: Any, thresholds: dict[str, Any]) -> bool:
    """是否需要人工复核（施工卡原文签名）。

    等价于 `bool(review_reason(seg, thresholds))`，保留独立名字是因为
    施工卡直接给了这个名字，调用点读起来也更直白。
    """
    return bool(review_reason(seg, thresholds))


def split_for_review(
    segments: list[Any] | tuple[Any, ...],
    thresholds: dict[str, Any],
) -> tuple[list[Any], list[Any]]:
    """把分段分成 `(待复核, 可直接用)` 两堆（施工卡原文签名）。

    返回**两堆都保留原顺序**：复核页要按时间码顺序展示，
    记录稿正文也要按时间顺序拼，两边都不能因为分流而乱序。

    这里对每个分段只调用一次判据（`review_reason`），
    不在两个列表推导里各调一次 —— 那不仅跑两遍，
    万一判据含随机性或状态（将来可能加），两堆会同时包含同一段。
    """
    to_review: list[Any] = []
    auto_pass: list[Any] = []
    for seg in segments:
        (to_review if review_reason(seg, thresholds) else auto_pass).append(seg)
    return to_review, auto_pass


def plan_review(
    segments: list[Any] | tuple[Any, ...],
    thresholds: dict[str, Any],
) -> list[ReviewDecision]:
    """逐段给出带理由的结论（复核页/验收报告用）。

    与 `split_for_review` 的关系：那个给"两堆数据"，这个给"一份说明"。
    两处都调 `review_reason`，所以结论必然一致。
    """
    return [
        ReviewDecision(
            seq=int(getattr(seg, "seq", i + 1)),
            needs_review=bool(r),
            reason=r,
        )
        for i, seg in enumerate(segments)
        for r in (review_reason(seg, thresholds),)
    ]


def review_ratio(
    segments: list[Any] | tuple[Any, ...],
    thresholds: dict[str, Any],
) -> float:
    """进入人工复核的段落占比（0..1）。空输入返回 0.0。

    这是本卡验收标准第 1 条要量的数（3 小时会议 < 20%）。
    分母用**全部段数**（不是"有置信度的段数"）—— 与
    `TranscriptResult.low_confidence_ratio()` 的口径不同，是刻意的：
    那个函数量的是"识别质量"（只对有分数的段有意义），
    本函数量的是"人的工作量"（每个被挑出来的段都要人看，
    不管它是因为低分还是因为静音被挑出来的）。
    """
    total = len(segments)
    if total == 0:
        return 0.0
    return sum(1 for s in segments if review_reason(s, thresholds)) / total


def format_duration_ms(ms: int) -> str:
    """毫秒 -> `HH:MM:SS`（超过 99 小时则自然进位到 3 位小时）。

    为什么单独一个函数而不是 `str(timedelta)`：`timedelta` 超过一天会输出
    `1 day, 0:12:34`，那不是时间码。会议记录的时间码必须是
    `[00:12:34]` 这种**定宽、可直接比较、可直接被抓取**的形状
    （验收标准第 3 条）。定宽还有一个好处：导出的 Markdown 里
    时间码列能对齐，人一眼扫得下来。
    """
    if ms < 0:
        raise ValueError(f"时间码不允许为负：{ms}ms")
    total_sec, _ = divmod(int(ms), 1000)
    hours, rem = divmod(total_sec, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

#: flag 的中文说明。只登记 `REVIEW_FLAGS` 里的两个——
#: 出现未登记 flag 时原样显示（不编造解释），这比显示一个错的中文更安全。
_FLAG_LABELS: dict[str, str] = {
    "silence": "静音段",
    "overlap": "多人同时说话",
}


def _low_confidence_threshold(thresholds: dict[str, Any]) -> float:
    """从嵌套阈值字典里取 `asr.low_confidence`。

    容错但**不兜底成默认值**：配置缺这一项说明配置本身有问题，
    静默用一个写死的 0.7 会让"改了配置没生效"无从察觉。
    这里只做"兼容两种写法"（嵌套 / 扁平），缺失则明确抛错。

    Raises:
        KeyError: 阈值缺失（附上实际拿到的结构，方便定位配置问题）。
    """
    if not isinstance(thresholds, dict):
        raise KeyError(f"thresholds 必须是字典，当前 {type(thresholds).__name__}")

    asr = thresholds.get("asr")
    if isinstance(asr, dict):
        if "low_confidence" in asr:
            return float(asr["low_confidence"])
        raise KeyError(
            f"thresholds['asr'] 缺少 low_confidence：{sorted(asr)}"
            "（应形如 {'asr': {'low_confidence': 0.70}}）"
        )
    # 扁平写法：{"low_confidence": 0.70}。测试与 CLI 快速调用时方便。
    if "low_confidence" in thresholds:
        return float(thresholds["low_confidence"])
    raise KeyError(
        "thresholds 找不到 asr.low_confidence"
        f"（实际顶层键：{sorted(thresholds) if isinstance(thresholds, dict) else '?'}）"
    )


#: 供类型标注引用的字面量（避免各处硬写字符串）
ReviewSide = Literal["review", "auto"]
