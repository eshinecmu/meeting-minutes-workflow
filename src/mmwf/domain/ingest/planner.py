"""音频切分计划与重叠归并（T1-06 领域层核心）。

本模块是**纯函数**：输入时长/配置，输出切分计划；不碰文件、不碰子进程、
不碰 ffmpeg。这么切分的原因是：

1. 切分计划是最容易出错的地方（越界、漏尾、块间重叠算错），
   而它完全不依赖外部工具 —— 放进程里会拖慢单测、也没必要。
2. `tools/check_layers.py` 强制领域层不得 import 框架/子进程相关模块，
   `subprocess` 属于"外部能力"，必须留在 `adapters/audio/`。

为什么必须切分（F1.2）
----------------------
两个独立的硬上限，任一个都会让"整段喂进去"失败：

· **FunASR 侧**：超长音频整段推理会 OOM（T1-05 施工卡明写）。
· **格式侧**：部分录音笔产出的 wma/amr 单文件时长有上限，且
  ffmpeg 对 >4GB 文件的索引会显著变慢甚至失败。

切分**不等于**可以随意切。块边界若落在句子中间，转写结果会在边界处
断词。所以相邻块留一段重叠（默认 50 秒 ≈ 5%），让每一边都有完整的
上下文；重复部分由 `merger` 在**拿到文本之后**去重。

为什么去重不在这里做
--------------------
施工卡原文："去重逻辑写在 `merger` 里，不写在切分里，因为只有拿到
文本才能判断边界。" 本模块只负责**时间轴**上的诚实记录（哪个区间属于
哪一块），文本层的去重在 `tests/.../merger` 与 T1-08 处理。
"""

from __future__ import annotations

from dataclasses import dataclass

from mmwf.domain.errors import UnsupportedFormatError

#: 支持的音频扩展名（SAD §12.4 兼容性要求 + 施工卡）。
#: 全部小写带点，比较前调用方须 `.lower()`。
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".mp3", ".m4a", ".wma", ".amr", ".flac", ".aac", ".ogg"}
)

#: 单文件大小上限（字节）。4 GiB。
#: 选这个值的原因：32 位偏移量的文件中枢上限就是 4GiB，
#: 超了 ffmpeg 处理会失败或极慢。施工卡要求"有明确提示"，
#: 而不是静默失败 —— 静默失败会让用户以为系统卡死。
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024

#: 默认块长与重叠（秒）。与 `config/models.yaml` 的 `asr` 段保持一致，
#: 但这里给出默认值，让领域层在没配置时也能独立工作（便于单测）。
DEFAULT_CHUNK_SECONDS = 1800
DEFAULT_OVERLAP_SECONDS = 50

#: 目标采样率与声道数。ASR 模型的标准输入；提前转换可让引擎少做一次重采样。
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1


@dataclass(frozen=True)
class ChunkPlanItem:
    """一个切分片段在**原音频时间轴**上的区间。

    注意 `seq` 从 1 开始（与 `transcript_segment.seq` 同约定），
    因为最终要落库、要在界面上显示"第 3 / 6 段"。
    """

    seq: int
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError(f"seq 必须从 1 开始，当前 {self.seq}")
        if self.start_ms < 0:
            raise ValueError(f"片段 {self.seq} 起点为负：{self.start_ms}")
        if self.start_ms >= self.end_ms:
            raise ValueError(
                f"片段 {self.seq} 区间无效：start={self.start_ms} >= end={self.end_ms}"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, int]:
        """与 SAD §9.2 的 `chunk_plan` 响应字段一致。"""
        return {"seq": self.seq, "start_ms": self.start_ms, "end_ms": self.end_ms}


def validate_extension(filename: str) -> str:
    """校验扩展名，返回规范化后的小写扩展名（含点）。

    调用方要用返回值而不是原始字符串做后续判断 —— 统一转小写
    能避免 `A.M4A` 与 `a.m4a` 被判成两种格式。
    """
    from pathlib import PurePath

    ext = PurePath(filename).suffix.lower()
    if not ext:
        raise UnsupportedFormatError(
            detail=f"文件名没有扩展名，无法判断格式：{filename}",
            filename=filename,
        )
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            detail=(
                f"暂不支持 {ext} 格式（文件名 {filename}）。"
                f"支持：{'、'.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
            filename=filename,
            extension=ext,
        )
    return ext


def build_chunk_plan(
    duration_ms: int,
    *,
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
) -> list[ChunkPlanItem]:
    """生成切分计划。

    算法（与施工卡伪代码一致，此处补齐边界处理）：

        step = chunk - overlap
        start = 0
        while start < duration:
            end = min(start + chunk, duration)
            记一段 (start, end)
            if end >= duration: break      ← 关键：最后一块必须收在末尾
            start += step

    **两个必须保住的性质**（验收标准 1 逐条检查）：

    1. **无遗漏**：`plan[0].start_ms == 0` 且 `plan[-1].end_ms == duration_ms`。
       少了这一条，音频首尾会被悄悄丢掉。
    2. **无越界**：每个 `end_ms <= duration_ms`。

    另外**单调递增**（`start` 严格递增）必须成立，否则 ffmpeg 会切出
    重复片段、块数也会算错。

    边界情况：
    · `duration_ms <= chunk_seconds*1000` → 只有一段 (0, duration)。
      短音频**不做无谓切分** —— 多切一次就多一次解码开销和一次边界风险。
    · `duration_ms == 0` → 返回空列表。调用方应在此之前已用 E1003 拒掉
      零长音频；这里返回空而不是抛错，是为了让"探测失败"与"真没内容"可区分。
    """
    if duration_ms < 0:
        raise ValueError(f"duration_ms 不能为负：{duration_ms}")
    if chunk_seconds <= 0:
        raise ValueError(f"chunk_seconds 必须为正：{chunk_seconds}")
    if overlap_seconds < 0:
        raise ValueError(f"overlap_seconds 不能为负：{overlap_seconds}")
    if overlap_seconds >= chunk_seconds:
        # 与 `AsrConfig` 的校验同一条规则：重叠 >= 块长会让 step <= 0，
        # 循环永远推进不了。这里再挡一层，因为领域层可能被直接调用。
        raise ValueError(
            f"overlap_seconds({overlap_seconds}) 必须小于 chunk_seconds({chunk_seconds})，"
            "否则切分会死循环"
        )
    if duration_ms == 0:
        return []

    chunk_ms = chunk_seconds * 1000
    step_ms = (chunk_seconds - overlap_seconds) * 1000

    plan: list[ChunkPlanItem] = []
    start = 0
    while start < duration_ms:
        end = min(start + chunk_ms, duration_ms)
        plan.append(ChunkPlanItem(seq=len(plan) + 1, start_ms=start, end_ms=end))
        if end >= duration_ms:
            break
        start += step_ms

    return plan


def merge_overlapping_texts(
    pieces: list[tuple[int, int, str]],
) -> tuple[str, list[dict[str, object]]]:
    """把带时间戳的片段文本拼成一份文稿，**并去掉重叠区的重复**。

    Args:
        pieces: `[(start_ms, end_ms, text), ...]`，按 seq 顺序传入。
            `text` 是该片段转写出的文本。

    Returns:
        `(merged_text, decisions)`。`decisions` 逐条记录每对相邻片段
        的判定结果，写进审计与测试报告，让"去重做了什么"可复核 ——
        静默去重会让"为什么这句话没了"无法追查。

    去重算法（**不是简单的按字数切**）
    ----------------------------------
    重叠区是两个片段在时间轴上共享的那一段。理想情况下两个片段在重叠区
    转写出**相同**的文本，此时把后一段开头的重复部分去掉即可。

    但实际不会完全相同（引擎在边界处的断句不同、带上了不同的上下文），
    所以这里做的是"**最长公共后缀/前缀匹配**"：

        取前段文本的**尾部若干字**、后段文本的**头部若干字**，
        找最长的匹配，认为那一段就是重叠区被重复转写的部分。

    为什么用字符匹配而不是时间匹配：重叠区的时间戳来自两个独立解码
    批次，同一句话在两块里的时间戳会有几十到几百毫秒的漂移，
    按时间切会切歪。文本是更可靠的锚点。

    保守策略（重要）
    ----------------
    找不到任何公共部分时**不强行去重**，原样保留两段（`deduped=False`）。
    宁可留一点重复，也不能凭空删掉内容 —— 纪要场景下"多一句"和
    "少一句"的严重性完全不对等。
    """
    if not pieces:
        return "", []

    decisions: list[dict[str, object]] = []
    out = pieces[0][2]

    for i in range(1, len(pieces)):
        prev_start, prev_end, _prev_text = pieces[i - 1]
        cur_start, cur_end, cur_text = pieces[i]

        overlap_ms = prev_end - cur_start
        if overlap_ms <= 0:
            # 时间轴上不重叠（配置改成了无重叠切分）→ 直接接上
            out += cur_text
            decisions.append({
                "between": [i, i + 1],
                "overlap_ms": 0,
                "deduped": False,
                "reason": "no_overlap_in_timeline",
            })
            continue

        kept, matched_len = _strip_duplicated_prefix(out, cur_text)
        out += kept
        decisions.append({
            "between": [i, i + 1],
            "overlap_ms": overlap_ms,
            "deduped": matched_len > 0,
            "matched_chars": matched_len,
            "reason": "prefix_suffix_match" if matched_len else "no_match_kept_verbatim",
        })

    return out, decisions


def _strip_duplicated_prefix(merged: str, incoming: str) -> tuple[str, int]:
    """从 `incoming` 头部去掉与 `merged` 尾部重复的部分。

    返回 `(保留的部分, 去掉的字数)`。

    搜索范围有上限（见 `_MAX_LOOKBACK_CHARS`）：不加限制会让"整段重复"
    这种病态输入退化成 O(n²) 的逐字比对。
    """
    limit = min(len(merged), len(incoming), _MAX_LOOKBACK_CHARS)
    for n in range(limit, 0, -1):
        if merged.endswith(incoming[:n]):
            return incoming[n:], n
    return incoming, 0


#: 单次去重最多回看的字符数。取 200 字：
#: 50 秒重叠按正常语速约 150—200 字，留一点余量即可；
#: 再大就属于"配置异常"（重叠过长），此时宁可不完全去重也不该拖慢。
_MAX_LOOKBACK_CHARS = 200
