"""Step 3 块间归并（SAD §10.1 Step 3，F3.2）。

施工卡把这里标成"**最容易出错的地方**"，三件事必须对：

① `source_refs` 取**并集**而非覆盖
-----------------------------------
同一议题跨块出现时，若用 `dict.update` 式的覆盖，**只有最后一块的依据留下**，
前几块的溯源信息**静默丢失**。表现很隐蔽：议题看着完整，
但点"溯源"只跳到会议末尾。施工卡原文点名要求并集。
本模块所有合并点都走 `_union_refs()`，**不做任何覆盖式赋值**。

② 重叠区的重复判定
------------------
相邻块有 200 字重叠，同一句话会被抽两次。要判断这是"同一条的重复"
还是"两条不同的信息"。施工卡给的判据：

    相似度 > 0.9 且 **区间重叠** → 合并

**两个条件缺一不可**：只看相似度高就合并，会把"同一议题下两次提到
相近但不相同的事"错误地压成一条（丢信息）；只看区间重叠就合并，
会把"同一段里说的两件不同的事"压成一条。

③ 相似度用什么算
----------------
SAD §10.1 允许"本地 embedding 或字符 n-gram Jaccard"。本卡选
**字符 bigram Jaccard**，理由：**不需要模型**（本机无 LLM/embedding 环境，
见施工卡 §2.4），纯离线、确定性、可复跑。阈值 0.82 / 0.90 从配置读
（写死会让 U-EX-08"阈值可配"失败）。

⚠️ 但这是个**有代价的选择**：Jaccard 对**近义改写**不敏感
（"通过预算方案" vs "同意该方案" 相似度很低）。embedding 能抓住，
但它需要额外模型权重。**若目标机型上实测归并质量不达标，换 embedding 即可**
—— 换的时候只需替换 `similarity()` 一个函数，调用方不用动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, cast

__all__ = [
    "DEFAULT_DEDUP_THRESHOLD",
    "DEFAULT_TOPIC_THRESHOLD",
    "MergeStats",
    "merge_chunk_results",
    "similarity",
]

#: SAD §10.1 给的议题聚类阈值。
DEFAULT_TOPIC_THRESHOLD = 0.82
#: SAD §10.1 / §10.4 给的议定去重阈值。
DEFAULT_DEDUP_THRESHOLD = 0.90


def _bigrams(text: str) -> set[str]:
    """字符 bigram 集合（中文用字符级，不做分词）。"""
    s = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard 相似度，值域 [0, 1]。

    空串对空串返回 1.0（"两条都没有内容"视为相同）；
    一边为空返回 0.0（有内容 vs 没内容，不同）。
    """
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba and not bb:
        return 1.0
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def _ref_key(ref: dict[str, object]) -> tuple[object, ...]:
    """ref 的去重键：优先用 `(segment_id)`，退化到 `(start_ms, quote)`。"""
    sid = ref.get("segment_id")
    if sid:
        return ("sid", str(sid))
    return ("ms", ref.get("start_ms"), str(ref.get("quote") or ref.get("text") or ""))


def _union_refs(*ref_lists: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """★ 取并集（**不是覆盖**）。保序去重，先出现的在前。

    这是本模块最重要的一行契约：施工卡原文"`source_refs` 要取并集而非覆盖，
    否则溯源会丢"。所有合并点都必须走这里。
    """
    out: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for lst in ref_lists:
        for ref in lst or []:
            if not isinstance(ref, dict):
                continue
            k = _ref_key(ref)
            if k in seen:
                continue
            seen.add(k)
            out.append(dict(ref))
    return out


def _intervals_overlap(a: dict[str, object], b: dict[str, object]) -> bool:
    """两个 ref 的区间是否重叠（重叠区重复判定的**必要条件**）。

    判据（任一成立即算重叠）：
      · 同一 `segment_id` —— 同一段被两块都抽到，最强证据；
      · 都有 `start_ms` 且时间区间相交。

    ⚠️ 两边都没有 `start_ms` 时**返回 False**（不判定为重叠）。
    理由：判不了就是判不了，**不能默认"算重叠"** —— 那会把
    "没有时间码的两条不同事项"错误合并，丢信息。
    """
    if a.get("segment_id") and a.get("segment_id") == b.get("segment_id"):
        return True
    a0, a1 = cast(int | None, a.get("start_ms")), cast(int | None, a.get("end_ms"))
    b0, b1 = cast(int | None, b.get("start_ms")), cast(int | None, b.get("end_ms"))
    if a0 is None or b0 is None:
        return False
    # 单点时间戳（无 end）时，视为一个 1ms 的瞬时点，只要同起点即算重叠
    a1 = a0 if a1 is None else a1
    b1 = b0 if b1 is None else b1
    return not (a1 < b0 or b1 < a0)


def _info_score(item: dict[str, object]) -> int:
    """一条事项的"信息量"（重叠区裁决用，SAD §10.1 Step 3 末条）。

    信息量 = 正文长度 + 责任人非空加分 + 有时限加分。
    为什么不是单纯比长度：一条 30 字但带了责任人和时限的议定，
    比一条 40 字什么信息都没有的，**对纪要更有用**。
    """
    text = str(item.get("text") or item.get("content") or "")
    score = len(text)
    if str(item.get("owner") or "").strip() not in ("", "暂无"):
        score += 20
    if str(item.get("deadline") or "").strip():
        score += 10
    return score


@dataclass
class MergeStats:
    """归并的统计（用于日志与验收断言）。"""

    topics_in: int = 0
    topics_out: int = 0
    resolutions_in: int = 0
    resolutions_out: int = 0
    actions_in: int = 0
    actions_out: int = 0
    #: 因"相似度 + 区间重叠"被合并掉的条数（重叠区裁决）。
    merged_duplicates: int = 0
    #: 仅因相似度高但区间不重叠而**保留**的条数（防误合并的见证）。
    kept_distinct: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "topics_in": self.topics_in, "topics_out": self.topics_out,
            "resolutions_in": self.resolutions_in,
            "resolutions_out": self.resolutions_out,
            "actions_in": self.actions_in, "actions_out": self.actions_out,
            "merged_duplicates": self.merged_duplicates,
            "kept_distinct": self.kept_distinct,
        }


@dataclass
class MergeResult:
    """归并结果。

    `topics` / `resolutions` / `action_items` 是**图兼容形状**
    （与 `naive_extract` 的产物同构：`title`/`text`/`owner`/`refs`），
    这样下游的闸口送审、`naive_validate`、导出**一行都不用改**（T1-10 的承诺）。
    """

    topics: list[dict[str, Any]] = field(default_factory=list)
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    action_items: list[dict[str, Any]] = field(default_factory=list)
    stats: MergeStats = field(default_factory=MergeStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topics": self.topics,
            "resolutions": self.resolutions,
            "action_items": self.action_items,
        }


def _item_text(item: dict[str, object]) -> str:
    return str(item.get("text") or item.get("content") or "")


def _merge_one_group(group: Sequence[dict[str, object]], *, dedup_threshold: float,
                     stats: MergeStats) -> dict[str, object]:
    """把一组"同一条事项"的多个副本合成一条：refs 取并集，正文取信息量最大的。"""
    best = max(group, key=_info_score)
    merged = dict(best)
    merged["refs"] = _union_refs(*(cast(list[dict[str, object]], g.get("refs")) or [] for g in group))
    # 责任人/时限：任一副本有就带上（并集语义，别丢）
    for k in ("owner", "deadline"):
        if not str(merged.get(k) or "").strip():
            for g in group:
                if str(g.get(k) or "").strip():
                    merged[k] = g[k]
                    break
    if len(group) > 1:
        stats.merged_duplicates += len(group) - 1
    return merged


def _dedup_items(
    items: Sequence[dict[str, object]], *, threshold: float, stats: MergeStats,
    counter: str,
) -> list[dict[str, object]]:
    """按"相似度 > 阈值 **且** 区间重叠"去重（施工卡判据，两个条件缺一不可）。

    用**聚类**而不是"和上一条比"：同一事项可能在 3 个块里各出现一次，
    只和上一条比会漏掉不相邻的那次。
    """
    groups: list[list[dict[str, object]]] = []
    for it in items:
        placed = False
        for g in groups:
            if similarity(_item_text(g[0]), _item_text(it)) <= threshold:
                continue
            if not any(_intervals_overlap(r, s)
                       for r in (cast(list[dict[str, object]], g[0].get("refs")) or [])
                       for s in (cast(list[dict[str, object]], it.get("refs")) or [])):
                # 相似但区间不重叠 —— 是**两条不同的信息**，不合并
                stats.kept_distinct += 1
                continue
            g.append(it)
            placed = True
            break
        if not placed:
            groups.append([it])
    out = [_merge_one_group(g, dedup_threshold=threshold, stats=stats)
           for g in groups]
    setattr(stats, counter, len(out))
    return out


def merge_chunk_results(
    results: Sequence[dict[str, Any]],
    *,
    topic_threshold: float = DEFAULT_TOPIC_THRESHOLD,
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
) -> MergeResult:
    """把各块的抽取结果归并成一份。

    Args:
        results: 每块的 `{topics, resolutions, action_items}`（图兼容形状）。
            畸形输入（不是 dict / 字段不是 list）**一律跳过**，
            不抛异常 —— 归并的职责是合并，不是拒绝数据。
        topic_threshold: 议题聚类阈值（配置 `extraction.merge.topic_similarity_threshold`）。
        dedup_threshold: 议定去重阈值（配置 `extraction.merge.resolution_dedup_threshold`）。

    Returns:
        `MergeResult`。空输入返回空结果（**不抛**，U-EX-10）。
    """
    stats = MergeStats()
    topics_raw: list[dict[str, object]] = []
    resolutions_raw: list[dict[str, object]] = []
    actions_raw: list[dict[str, object]] = []

    for r in results or []:
        if not isinstance(r, dict):
            continue
        for k, bucket in (("topics", topics_raw),
                          ("resolutions", resolutions_raw),
                          ("action_items", actions_raw)):
            v = r.get(k)
            if isinstance(v, (list, tuple)):
                bucket.extend(x for x in v if isinstance(x, dict))

    stats.topics_in = len(topics_raw)
    stats.resolutions_in = len(resolutions_raw)
    stats.actions_in = len(actions_raw)

    # ---- 议题聚类：名称 + 讨论 一起比（只比名称会把"预算"和"预算执行"当成两个）----
    topic_groups: list[list[dict[str, object]]] = []
    for t in topics_raw:
        key = f"{t.get('title') or t.get('name') or ''} {t.get('discussion') or ''}"
        placed = False
        for g in topic_groups:
            gkey = (f"{g[0].get('title') or g[0].get('name') or ''} "
                    f"{g[0].get('discussion') or ''}")
            if similarity(gkey, key) >= topic_threshold:
                g.append(t)
                placed = True
                break
        if not placed:
            topic_groups.append([t])

    merged_topics: list[dict[str, object]] = []
    for g in topic_groups:
        best = max(g, key=lambda x: len(str(x.get("discussion") or "")))
        merged = dict(best)
        merged["title"] = best.get("title") or best.get("name") or ""
        merged["refs"] = _union_refs(*(cast(list[dict[str, object]], x.get("refs")) or [] for x in g))
        merged_topics.append(merged)
    stats.topics_out = len(merged_topics)

    merged_resolutions = _dedup_items(
        resolutions_raw, threshold=dedup_threshold,
        stats=stats, counter="resolutions_out",
    )
    merged_actions = _dedup_items(
        actions_raw, threshold=dedup_threshold,
        stats=stats, counter="actions_out",
    )

    return MergeResult(
        topics=merged_topics,
        resolutions=merged_resolutions,
        action_items=merged_actions,
        stats=stats,
    )
