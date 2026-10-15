"""分块分层抽取的编排（T2-01 主入口，SAD §10.1 四步走）。

    输入：record_markdown（记录稿全文，可能上万字）
    输出：结构化议题/决议候选 + 图兼容形状

四步
----
    Step 1  分块          -> chunker.chunk_segments
    Step 2  逐块抽取（并行）-> 本模块（调 llm）
    Step 3  块间归并       -> merge.merge_chunk_results
    Step 4  全局补全       -> completeness.complete_missing
    （贯穿）精确溯源        -> grounding.bind_grounding_exact

★ 输出形状：**必须与 `naive_extract` 同构**
-------------------------------------------
T1-10 立下的约定：朴素实现与二期真实现的产物**形状一致**，这样下游
（闸口送审、`naive_validate`、导出、`naive_generate`）**一行都不用改**。

所以本模块输出：:

    {
      "topics":        [{"title", "refs", "seq", ...}],
      "resolutions":   [{"text", "owner", "refs", "seq", ...}],
      "action_items":  [{"text", "owner", "refs", "seq", ...}],
      # 下面三项是本卡**额外**给的（朴素实现没有），下游可忽略
      "minutes":       {MinutesSchema 形状},
      "issues":        [{"rule", "detail", ...}],
      "stats":         {分块/归并统计},
    }

⚠️ `topics` 用 `title`（不是 SAD 的 `name`）、`resolutions`/`action_items` 用
`text`（不是 `content`）—— 这是**图契约**的字段名，不是笔误。
SAD 的 `name`/`content` 在 `minutes` 那份里保持原样。

★ 单块失败**不拖垮全局**
------------------------
某块超时/输出畸形 → 记一条 issue 并**跳过该块**，其余块照常归并。
若让异常冒出去，一块超时就让 2 万字的活儿全废 —— 而用户已经等了几十分钟。
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Sequence

from mmwf.domain.extraction import prompts
from mmwf.domain.extraction.chunker import Chunk, chunk_segments
from mmwf.domain.extraction.completeness import complete_missing
from mmwf.domain.extraction.grounding import bind_grounding_exact
from mmwf.domain.extraction.merge import merge_chunk_results
from mmwf.domain.extraction.schemas import (
    MinutesSchema,
    minutes_json_schema,
    validate_minutes,
)
from mmwf.domain.extraction.segmentize import Segment, segmentize

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "ExtractResult",
    "extract_minutes",
]

#: 并行抽取的默认线程数。
#:
#: ★ **保守取 2，不是 4**。施工卡原文：
#: "并行调用本地模型会抢显存。`max_workers` 要根据显存调，**默认值保守取 2** 而非 4"。
#: 取值从 `settings.extraction` 之外单独传（配置里没有这一项，
#: 因为它与显存强相关，属部署参数而非业务参数）。
DEFAULT_MAX_WORKERS = 2

#: 模型输出里常见的 markdown 代码围栏（```json ... ```）。
#: 约束解码理论上不会带，但换个引擎就可能带 —— 剥掉比让 schema 校验失败强。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


@dataclass
class ExtractResult:
    """抽取结果。"""

    topics: list[dict[str, Any]] = field(default_factory=list)
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    action_items: list[dict[str, Any]] = field(default_factory=list)
    minutes: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_graph_dict(self) -> dict[str, Any]:
        """转成 `extract` 节点要返回的状态增量（图兼容形状）。"""
        return {
            "topics": self.topics,
            "resolutions": self.resolutions,
            "action_items": self.action_items,
            "minutes": self.minutes,
            "issues": self.issues,
        }


# ---------------------------------------------------------------------------
# 单块抽取
# ---------------------------------------------------------------------------

def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", (text or "").strip())


def _parse_json(text: str) -> Any:
    """解析模型输出。失败抛 `ValueError`（由调用方记 issue 并跳过该块）。"""
    cleaned = _strip_fence(text)
    if not cleaned:
        raise ValueError("模型输出为空")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # 兜底：有些引擎会在 JSON 前后带一句解释，尝试取第一个 { 到最后一个 }
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"模型输出不是合法 JSON：{e}") from e


def _extract_one_chunk(
    chunk: Chunk,
    *,
    llm: Any,
    max_schema_retries: int,
) -> tuple[MinutesSchema | None, list[dict[str, Any]]]:
    """抽一个块。返回 `(schema 实例或 None, issues)`。

    ★ 重试是"把 pydantic 报错**回注**给模型"（SAD §10.2），
    不是"原样再问一遍" —— 后者对约束解码的偶发不合规几乎无效。
    """
    issues: list[dict[str, Any]] = []
    prompt = prompts.render_prompt(chunk.text)
    schema = minutes_json_schema()

    last_problems: list[Any] = []
    for attempt in range(max_schema_retries + 1):
        try:
            resp = llm.chat(
                prompt,
                system=prompts.SYSTEM_PROMPT,
                json_schema=schema,
            )
        except Exception as e:  # noqa: BLE001 - 单块失败必须被收敛
            issues.append({
                "rule": "chunk_llm_failed",
                "chunk": chunk.index,
                "detail": f"第 {chunk.index} 块调用模型失败：{type(e).__name__}: {e}",
            })
            return None, issues

        if getattr(resp, "truncated", False):
            issues.append({
                "rule": "chunk_truncated",
                "chunk": chunk.index,
                "detail": f"第 {chunk.index} 块输出被截断"
                          f"（done_reason={resp.done_reason!r}），该块结果不可信",
            })
            # 截断的输出拿去解析只会得到莫名其妙的 schema 报错 —— 直接放弃该块
            return None, issues

        try:
            payload = _parse_json(resp.text)
        except ValueError as e:
            last_problems = [{"loc": "<json>", "message": str(e)}]
            if attempt < max_schema_retries:
                prompt = prompts.render_retry_prompt(chunk.text, last_problems)
                continue
            issues.append({
                "rule": "chunk_bad_json",
                "chunk": chunk.index,
                "detail": f"第 {chunk.index} 块输出无法解析为 JSON：{e}",
            })
            return None, issues

        # 中间块通常不知道会议元数据；只补缺键，结论字段继续严格校验。
        if isinstance(payload, dict):
            missing = [k for k in ("title", "meeting_time", "location", "host",
                                   "attendees", "recorder", "topics") if k not in payload]
            if missing:
                existing = next((i for i in issues if i["rule"] == "chunk_meta_missing"), None)
                if existing is None:
                    existing = {"rule": "chunk_meta_missing", "chunk": chunk.index, "keys": []}
                    issues.append(existing)
                existing["keys"] = sorted(set(existing["keys"]) | set(missing))
                existing["detail"] = "块级缺失元数据已补空：" + "、".join(existing["keys"])
                for key in missing:
                    payload[key] = [] if key in {"attendees", "topics"} else ""
        obj, problems = validate_minutes(payload)
        if obj is not None:
            return obj, issues

        last_problems = problems
        if attempt < max_schema_retries:
            prompt = prompts.render_retry_prompt(chunk.text, problems)
            continue
        issues.append({
            "rule": "chunk_schema_failed",
            "chunk": chunk.index,
            "detail": f"第 {chunk.index} 块输出连续 {max_schema_retries + 1} 次"
                      f"未通过结构校验：" + "; ".join(
                          f"{p.loc}:{p.message}" for p in problems[:3]),
        })
        return None, issues

    return None, issues   # pragma: no cover - 循环必然 return


# ---------------------------------------------------------------------------
# schema -> 图兼容形状
# ---------------------------------------------------------------------------

def _bind_refs(quote: str, segments: Sequence[Segment]) -> list[dict[str, Any]]:
    """把 `source_quote` 绑到段落上，产出 `refs`（**取不到就是空列表**）。

    ⚠️ 绑不到时返回**空列表**而不是伪造一个 ref —— 伪造会让
    `naive_validate` 的"无来源依据"检查永远通过，溯源形同虚设。
    空列表会让那条检查如实报出来（这正是我们要的）。
    """
    got = bind_grounding_exact(quote, segments)
    return [got.ref] if got.ref else []


def _schema_to_graph(
    minutes: MinutesSchema,
    segments: Sequence[Segment],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """把 `MinutesSchema` 摊平成图兼容的三个列表。

    议题与事项是**嵌套**关系（事项挂在议题下），但图契约是三个**平铺**列表。
    摊平的同时给事项带上 `topic` 字段（指回所属议题名），
    这样 `naive_generate` 仍能按议题归组渲染。
    """
    topics: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []

    for ti, t in enumerate(minutes.topics, start=1):
        topics.append({
            "title": t.name,
            "refs": _bind_refs(t.discussion, segments) if t.discussion else [],
            "seq": ti,
            "discussion": t.discussion,
            "readings": list(t.readings),
        })
        for r in t.resolutions:
            resolutions.append({
                "text": r.content,
                "owner": r.owner or "暂无",
                "deadline": r.deadline,
                "refs": _bind_refs(r.source_quote, segments),
                "quote": r.source_quote,
                "topic": t.name,
            })
        for a in t.action_items:
            actions.append({
                "text": a.content,
                "owner": a.owner or "暂无",
                "deadline": a.deadline,
                "refs": _bind_refs(a.source_quote, segments),
                "quote": a.source_quote,
                "topic": t.name,
            })
    return topics, resolutions, actions


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def extract_minutes(
    record_markdown: str,
    *,
    llm: Any,
    settings: Any = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    target_chars: int | None = None,
    overlap_chars: int | None = None,
) -> ExtractResult:
    """记录稿 -> 结构化抽取结果。

    Args:
        record_markdown: 记录稿全文。**空字符串是合法输入**（U-EX-10），
            返回空结果且**不调用模型**。
        llm: 实现 `LlmClient` 协议的对象。
        settings: `Settings`；用来读分块/归并/重试参数。`None` 时用默认值。
        max_workers: 并行块数（默认 2，见 `DEFAULT_MAX_WORKERS`）。

    Returns:
        `ExtractResult`。
    """
    cfg = _resolve_cfg(settings, target_chars, overlap_chars)

    # ---- Step 1 分块 ----
    segments, seg_report = segmentize(
        record_markdown or "", speech_chars_per_sec=cfg["speech_chars_per_sec"]
    )
    if not segments:
        # 空输入：不调用模型，直接返回空结果（U-EX-10）
        return ExtractResult(
            stats={"segments": 0, "chunks": 0, "note": "空输入，未调用模型"},
        )

    chunks = chunk_segments(
        segments,
        target_chars=cfg["target_chars"],
        max_chars=cfg["max_chars"],
        overlap_chars=cfg["overlap_chars"],
    )

    issues: list[dict[str, Any]] = []
    if seg_report.no_timecode:
        # ★ 把 D1 有损反解的代价**显式报出来**（见 segmentize 的 docstring）
        issues.append({
            "rule": "no_timecode_segments",
            "detail": f"记录稿有 {seg_report.no_timecode}/{seg_report.total} 段没有时间码，"
                      f"这些段落的长静音分块边界失效（溯源也无法给出时间戳）",
        })

    # ---- Step 2 逐块抽取（并行）----
    chunk_results: list[dict[str, Any]] = []
    chunk_minutes: list[MinutesSchema] = []

    def _work(ch: Chunk) -> tuple[Chunk, MinutesSchema | None, list[dict[str, object]]]:
        obj, iss = _extract_one_chunk(
            ch, llm=llm, max_schema_retries=cfg["max_schema_retries"]
        )
        return ch, obj, iss

    if max_workers <= 1 or len(chunks) == 1:
        # 串行路径：单块或显式要求串行时不做线程池。
        # 保留这条路径不是"优化"，是为了**确定性**——
        # 串行时块的执行顺序与列表顺序一致，调试日志好读。
        done = [_work(c) for c in chunks]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            done = list(pool.map(_work, chunks))

    for ch, obj, iss in done:
        issues.extend(iss)
        if obj is None:
            continue
        chunk_minutes.append(obj)
        t, r, a = _schema_to_graph(obj, ch.segments)
        chunk_results.append({
            "topics": t, "resolutions": r, "action_items": a,
        })

    if not chunk_results:
        issues.append({
            "rule": "no_chunk_succeeded",
            "detail": f"全部 {len(chunks)} 块均未产出可用结果，请检查大模型服务",
        })

    # ---- Step 3 归并 ----
    merged = merge_chunk_results(
        chunk_results,
        topic_threshold=cfg["topic_threshold"],
        dedup_threshold=cfg["dedup_threshold"],
    )

    # ---- Step 4 全局补全 ----
    rich = _rich_minutes(chunk_minutes, merged)
    rich, missing_issues = complete_missing(rich)
    issues.extend(missing_issues)

    stats = {
        "segments": seg_report.total,
        "no_timecode": seg_report.no_timecode,
        "estimated_gaps": seg_report.estimated_gaps,
        "overlap_dropped": sum(ch.overlap_dropped for ch in chunks),
        "chunks": len(chunks),
        "chunks_ok": len(chunk_results),
        "max_workers": max_workers,
        "merge": merged.stats.to_dict(),
    }
    logger.info(
        "extract_minutes 完成：%d 段 / %d 块（成功 %d）/ %d 议题 / %d 决议 / %d 待办",
        seg_report.total, len(chunks), len(chunk_results),
        len(merged.topics), len(merged.resolutions), len(merged.action_items),
    )

    return ExtractResult(
        topics=merged.topics,
        resolutions=merged.resolutions,
        action_items=merged.action_items,
        minutes=rich,
        issues=issues,
        stats=stats,
    )


def _rich_minutes(
    chunk_minutes: Sequence[MinutesSchema],
    merged: Any,
) -> dict[str, Any]:
    """拼出 `minutes`（SAD §10.2 形状）。

    元信息（标题/时间/地点/主持人/记录人）取**第一个非空**的值 ——
    它们通常只在第一块出现，若逐块覆盖会变成最后一块的空值。
    议题/事项用**归并后**的结果（不是某一块的），保证与图形状一致。
    """
    out: dict[str, Any] = {
        "title": "", "meeting_time": "", "location": "",
        "host": "", "attendees": [], "recorder": "",
        "topics": [], "schema_version": "1.0",
    }
    for m in chunk_minutes:
        for k in ("title", "meeting_time", "location", "host", "recorder"):
            if not str(out.get(k) or "").strip() and str(getattr(m, k, "") or "").strip():
                out[k] = getattr(m, k)
        if not out["attendees"] and m.attendees:
            out["attendees"] = list(m.attendees)

    by_topic: dict[str, dict[str, Any]] = {}
    for t in merged.topics:
        name = str(t.get("title") or "")
        by_topic.setdefault(name, {
            "name": name,
            "discussion": str(t.get("discussion") or ""),
            "readings": list(t.get("readings") or []),
            "resolutions": [], "action_items": [], "pending_decisions": [],
        })
    for r in merged.resolutions:
        bucket = by_topic.setdefault(str(r.get("topic") or "会议主要内容"), {
            "name": str(r.get("topic") or "会议主要内容"), "discussion": "",
            "readings": [], "resolutions": [], "action_items": [],
            "pending_decisions": [],
        })
        bucket["resolutions"].append({
            "content": str(r.get("text") or ""),
            "owner": str(r.get("owner") or "暂无"),
            "deadline": r.get("deadline"),
            "source_quote": str(r.get("quote") or ""),
            "source_start_ms": (r.get("refs") or [{}])[0].get("start_ms")
                               if r.get("refs") else None,
            "source_end_ms": (r.get("refs") or [{}])[0].get("end_ms")
                             if r.get("refs") else None,
        })
    for a in merged.action_items:
        bucket = by_topic.setdefault(str(a.get("topic") or "会议主要内容"), {
            "name": str(a.get("topic") or "会议主要内容"), "discussion": "",
            "readings": [], "resolutions": [], "action_items": [],
            "pending_decisions": [],
        })
        bucket["action_items"].append({
            "content": str(a.get("text") or ""),
            "owner": str(a.get("owner") or "暂无"),
            "deadline": a.get("deadline"),
            "source_quote": str(a.get("quote") or ""),
            "source_start_ms": (a.get("refs") or [{}])[0].get("start_ms")
                               if a.get("refs") else None,
            "source_end_ms": (a.get("refs") or [{}])[0].get("end_ms")
                             if a.get("refs") else None,
        })
    # 待决策不在 G2 三列表中，必须从丰富原料保留，交最终粒度处理。
    for minutes in chunk_minutes:
        for topic in minutes.topics:
            if not topic.pending_decisions:
                continue
            bucket = by_topic.setdefault(topic.name, {
                "name": topic.name, "discussion": topic.discussion,
                "readings": list(topic.readings), "resolutions": [],
                "action_items": [], "pending_decisions": [],
            })
            for pending in topic.pending_decisions:
                item = pending.model_dump()
                if item not in bucket["pending_decisions"]:
                    bucket["pending_decisions"].append(item)
    out["topics"] = list(by_topic.values())
    return out


def _resolve_cfg(settings: Any, target_chars: int | None,
                 overlap_chars: int | None) -> dict[str, Any]:
    """把配置收敛成一份参数表。`settings` 为 `None` 时用默认值。

    ★ 阈值**必须**从配置读（U-EX-08"阈值可配"）。这里给默认值只是为了
    让 `settings=None` 的单测能跑，**不是**把阈值写死在业务逻辑里。
    """
    ex = getattr(settings, "extraction", None)
    chunk = getattr(ex, "chunk", None)
    merge = getattr(ex, "merge", None)
    retry = getattr(ex, "retry", None)
    configured_retries = getattr(retry, "max_schema_retries", 2)
    schema_retries = min(2, max(0, configured_retries))
    if schema_retries != configured_retries:
        logger.warning(
            "extraction.retry.max_schema_retries=%s 超出 0～2 范围，采用 %s"
            "（施工卡 §4 验收 9、SAD §10.2：最多重试 2 次）",
            configured_retries, schema_retries,
        )
    return {
        "target_chars": (target_chars if target_chars is not None
                         else getattr(chunk, "target_chars", 1500)),
        "speech_chars_per_sec": getattr(chunk, "speech_chars_per_sec", 4.0),
        "max_chars": getattr(chunk, "max_chars", 2000),
        "overlap_chars": (overlap_chars if overlap_chars is not None
                          else getattr(chunk, "overlap_chars", 200)),
        "topic_threshold": getattr(merge, "topic_similarity_threshold", 0.82),
        "dedup_threshold": getattr(merge, "resolution_dedup_threshold", 0.90),
        # 施工卡规定最多重试两次，配置只能收紧这个上限。
        "max_schema_retries": schema_retries,
    }
