"""权威源溯源：兼容精确绑定与确定性模糊、多段绑定。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from mmwf.domain.extraction.segmentize import Segment

__all__ = [
    "MATCH_FUZZY",
    "bind_grounding",
    "levenshtein",
    "union_refs",
    "MATCH_EXACT",
    "MATCH_NONE",
    "MATCH_NORMALIZED",
    "GroundingResult",
    "bind_grounding_exact",
    "normalize_punct",
]

MATCH_EXACT = "exact"
MATCH_NORMALIZED = "normalized"
MATCH_NONE = None

#: 标点/空白归一化映射。**全角 → 半角**统一，而不是反过来 ——
#: 半角集合更小、更稳定，且不会引入中文字符。
_PUNCT_MAP = {
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "、": ",", "（": "(", "）": ")", "【": "[", "】": "]",
    "《": "<", "》": ">", "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "－": "-", "～": "~", "…": "...",
}

#: 所有空白（含全角空格）。
_WS_RE = re.compile(r"\s+")


def normalize_punct(text: str) -> str:
    """标点归一化：全角→半角、去空白、统一引号。

    只用于**比较**，不用于输出 —— 原文一个字都不能改。
    """
    s = text or ""
    for k, v in _PUNCT_MAP.items():
        s = s.replace(k, v)
    s = _WS_RE.sub("", s)
    return s


@dataclass(frozen=True)
class GroundingResult:
    """一条引用与原文的绑定结果。

    Attributes:
        matched: `"exact"` / `"normalized"` / `"fuzzy"` / `None`。
        ref: 命中的 ref 字典（`segment_id` / `start_ms` / `text` / `quote`）；
            未命中为 `None`。
        confidence: 绑定置信度。精确 = 1.0；归一化命中 = 0.95
            （**降级**：标点不同意味着不是逐字相同，SAD §10.3 要求降级标注）；
            未命中 = 0.0。
    """

    matched: str | None
    ref: dict[str, Any] | None
    confidence: float

    refs: tuple[dict[str, Any], ...] = ()
    issues: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.matched is not None

    def to_dict(self) -> dict[str, Any]:
        data = {"matched": self.matched, "ref": self.ref, "confidence": self.confidence}
        # 旧精确 API 的三键序列化不变；新绑定同时暴露全部引用和诊断。
        if self.refs or self.issues:
            data.update(refs=list(self.refs), issues=list(self.issues))
        return data


def _make_ref(seg: Segment, quote: str) -> dict[str, Any]:
    return {
        "segment_id": f"seg-{seg.seq}",
        "start_ms": seg.start_ms,
        "end_ms": seg.end_ms,
        "text": seg.text,
        "quote": quote,
        "speaker": seg.speaker,
    }


def bind_grounding_exact(
    quote: str,
    segments: Sequence[Segment],
) -> GroundingResult:
    """把一条 `source_quote` 绑到原文段落上（**只做精确 + 标点归一化**）。

    先做**逐字**匹配（`matched="exact"`, confidence 1.0），
    失败再退到**标点归一化**后匹配（`matched="normalized"`, confidence 0.95），
    都失败返回 `matched=None`；领域计算不执行流程阻断。

    ⚠️ 逐字匹配优先于归一化匹配，这个顺序不能反：
    归一化会抹掉标点差异，若先做归一化，就永远拿不到 `"exact"` 这个
    **更强**的结论 —— 而"这条引用是逐字抄的"与"标点对不上但内容一致"
    在验收上是两回事（U-GR-01 vs U-GR-02）。
    """
    q = (quote or "").strip()
    if not q or not segments:
        return GroundingResult(matched=MATCH_NONE, ref=None, confidence=0.0)

    # ---- 第 1 层：逐字 ----
    for seg in segments:
        if q in seg.text:
            return GroundingResult(MATCH_EXACT, _make_ref(seg, q), 1.0)

    # ---- 第 2 层：标点归一化 ----
    nq = normalize_punct(q)
    if nq:
        for seg in segments:
            if nq in normalize_punct(seg.text):
                return GroundingResult(MATCH_NORMALIZED, _make_ref(seg, q), 0.95)

    # 兼容函数仅标记失败；模糊绑定由新 API 显式调用。
    return GroundingResult(matched=MATCH_NONE, ref=None, confidence=0.0)


MATCH_FUZZY = "fuzzy"


def union_refs(*groups) -> list[dict]:
    """同段不同 quote 必须保留，不能只按 segment_id 消重。"""
    out, seen = [], set()
    for group in groups:
        for ref in group:
            key = tuple(ref.get(k) for k in ("segment_id", "quote", "start_ms", "end_ms"))
            if key not in seen:
                seen.add(key)
                out.append(dict(ref))
    return out


def _normalized_map(text):
    chars, positions = [], []
    for i, char in enumerate(text):
        normalized = normalize_punct(char)
        chars.extend(normalized)
        positions.extend([i] * len(normalized))
    return "".join(chars), positions


def levenshtein(a: str, b: str, limit: int = 2) -> int:
    """单位代价编辑距离；超限统一返回 limit+1。"""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i] + [limit + 1] * len(b)
        for j in range(max(1, i - limit), min(len(b), i + limit) + 1):
            row[j] = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (ca != b[j - 1]))
        if min(row) > limit:
            return limit + 1
        prev = row
    return min(prev[-1], limit + 1)


def bind_grounding(quote: str, segments: Sequence[Segment]) -> GroundingResult:
    """全源精确优先、归一化其次，最后仅在单段中做模糊滑窗。"""
    from mmwf.domain.minutes.issues import make_issue

    q = (quote or "").strip()
    nq = normalize_punct(q)
    if not nq or not segments:
        return GroundingResult(None, None, 0.0)

    def result(mode, selected, windows, confidence, ambiguous=False):
        refs = []
        for seg, window in zip(selected, windows):
            ref = _make_ref(seg, quote)
            ref.update(matched_text=window, matched=mode, confidence=confidence)
            refs.append(ref)
        issues = []
        if mode == MATCH_FUZZY:
            issues.append(make_issue("GROUNDING_FUZZY", "", "原文有字词差异，待核对", source="grounding"))
        if ambiguous:
            issues.append(make_issue("GROUNDING_AMBIGUOUS", "", "存在多个引用候选，已选择最早匹配", source="grounding"))
        if any(s.start_ms is None or s.end_ms is None for s in selected):
            issues.append(make_issue("NO_TIMECODE", "", "暂无时间信息", source="grounding"))
        return GroundingResult(mode, refs[0], confidence, tuple(refs), tuple(issues))

    for mode in (MATCH_EXACT, MATCH_NORMALIZED):
        matches = []
        for i in range(len(segments)):
            for count in range(1, min(3, len(segments) - i) + 1):
                selected = segments[i:i + count]
                text = "".join(s.text for s in selected)
                hay, positions = (text, list(range(len(text)))) if mode == MATCH_EXACT else _normalized_map(text)
                needle = q if mode == MATCH_EXACT else nq
                start = hay.find(needle)
                if start < 0:
                    continue
                begin, end = positions[start], positions[start + len(needle) - 1] + 1
                offsets, cursor = [], 0
                for seg in selected:
                    offsets.append(seg.text[max(0, begin - cursor):max(0, min(len(seg.text), end - cursor))])
                    cursor += len(seg.text)
                # 拼接容器外的段不能伪称参与命中。
                involved = [(s, w) for s, w in zip(selected, offsets) if w]
                signature = tuple((s.seq, w) for s, w in involved)
                if signature not in [m[0] for m in matches]:
                    matches.append((signature, involved))
        if matches:
            chosen = matches[0][1]
            return result(mode, [s for s, _ in chosen], [w for _, w in chosen],
                          1.0 if mode == MATCH_EXACT else 0.95, len(matches) > 1)
    candidates = []
    if len(nq) >= 5:
        for si, seg in enumerate(segments):
            text, positions = _normalized_map(seg.text)
            for start in range(len(text)):
                local = []
                for size in range(max(1, len(nq) - 2), len(nq) + 3):
                    window = text[start:start + size]
                    if len(window) != size:
                        continue
                    distance = levenshtein(nq, window)
                    ratio = distance / max(len(nq), size)
                    if 1 <= distance <= 2 and ratio <= 0.20:
                        local.append((distance, ratio, si, start, size,
                                           seg.text[positions[start]:positions[start + size - 1] + 1]))
                # 同段同起点的重叠窗口只算一个候选，优先保留等长替换窗口。
                if local:
                    candidates.append(min(local, key=lambda c: (c[0], abs(c[4] - len(nq)), c[4])))
    if not candidates:
        return GroundingResult(None, None, 0.0)
    candidates.sort(key=lambda c: (c[0], abs(c[4] - len(nq)), c[2], c[3], c[4]))
    d, ratio, si, start, size, window = candidates[0]
    return result(MATCH_FUZZY, [segments[si]], [window], min(0.90, 1 - ratio),
                  sum((c[0], abs(c[4] - len(nq))) == (d, abs(size - len(nq)))
                      for c in candidates) > 1)
