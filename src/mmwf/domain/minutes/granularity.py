"""最终事项粒度规则；深拷贝、稳定身份和可重复执行的修正记录。"""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass

from mmwf.domain.extraction.merge import similarity
from mmwf.domain.grounding.binder import union_refs
from .consistency import explicit_names, numeric_tokens
from .issues import aggregate_issues, make_issue
from .post_validation import KINDS

_VERBS = ("完成", "提交", "落实", "推进", "负责", "牵头", "配合", "报送", "审核", "制定", "整改", "跟进", "组织", "开展")
_TIME = re.compile(r"尽快|近期|及时|适时|择期|年内|月底|年底|本周|下周|本月|下月|今日|明日|今天|明天|[0-9零〇一二两三四五六七八九十百千万]+[年月日号天周]|\d{4}-\d{2}-\d{2}")
_VAGUE = ("尽快", "近期", "及时", "适时", "择期", "抓紧", "稍后")
_PLACEHOLDERS = (None, "", "暂无", "待补充")


@dataclass(frozen=True)
class GranularityResult:
    minutes: dict
    issues: list[dict]
    changes: list[dict]

    def to_dict(self):
        return deepcopy({"minutes": self.minutes, "issues": self.issues, "changes": self.changes})


def _conflicts(a, b):
    for key in ("owner", "deadline", "decision_maker"):
        av, bv = (str(item.get(key) or "").strip() for item in (a, b))
        if av not in _PLACEHOLDERS and bv not in _PLACEHOLDERS and av != bv:
            return True
    for extract in (lambda s: {(t.category, t.value) for t in numeric_tokens(s)[0]},
                    lambda s: set(explicit_names(s))):
        av, bv = extract(a["content"]), extract(b["content"])
        if av and bv and av != bv:
            return True
    # 约数等未支持表达本身不代表冲突；只有双方表达不同才保留两条。
    av, bv = (set(numeric_tokens(item["content"])[1]) for item in (a, b))
    return bool(av and bv and av != bv)


def normalize_granularity(minutes) -> GranularityResult:
    """补 owner→拆分→降级→时限→组首去重。changes 仅含本次新增修正。"""
    out = deepcopy(minutes)
    # 施工卡 §2.1：扩展元数据（稳定路径映射、粒度修正记录）仅置于 _context。
    context = out.setdefault("_context", {})
    history = context.setdefault("granularity_changes", [])
    previous_metadata = context.get("granularity_items", {})
    metadata, current_metadata = {}, {}
    changes, issues = [], []
    origins, final_paths = {}, {}
    # 保留本次处理的对象，避免跨议题对象回收后 id 被复用。
    tracked_items = []
    identities, final_id_paths, change_ids = {}, {}, {}

    def record(code, path, message, item, **extra):
        change = {"code": code, "original_path": path,
                  "original_content": item.get("content", ""), **extra}
        change_ids[id(change)] = id(item)
        changes.append(change)
        history.append(change)

    for ti, topic in enumerate(out.get("topics", [])):
        groups = {kind: [] for kind in KINDS}
        for kind in KINDS:
            for index, item in enumerate(topic.get(kind, [])):
                path = f"topics[{ti}].{kind}[{index}]"
                metadata[id(item)] = deepcopy(previous_metadata.get(path, {}))
                tracked_items.append(item)
                origins[id(item)] = [path]
                identities[id(item)] = [id(item)]
                if not str(item.get("owner") or "").strip():
                    record("OWNER_DEFAULTED", path, "责任人已补为暂无", item)
                    item["owner"] = "暂无"
                content = str(item.get("content") or "").strip()
                parts = [p.strip(" \t\n，,。.;；:：、！!？?") for p in re.split("以及|同时|并", content)]
                parts = [p for p in parts if p]
                children = [item]
                # 每个原项最多拆分一次，长子项不能在下次运行再次切分。
                if len(content) > 60 and len(parts) >= 2 and not metadata[id(item)].get("split_child"):
                    record("ITEM_SPLIT", path, "多事项已按连接词拆分", item)
                    children = []
                    for number, part in enumerate(parts, 1):
                        child = deepcopy(item)
                        child.update(content=part, id=f'{item.get("id") or path}~{number}')
                        metadata[id(child)] = {"parent_path": path, "original_content": item.get("content", ""), "split_child": True}
                        tracked_items.append(child)
                        origins[id(child)] = [path]
                        identities[id(child)] = [id(child)]
                        children.append(child)
                for child in children:
                    destination = kind
                    if kind == "action_items" and (
                        not any(v in child.get("content", "") for v in _VERBS)
                        or not _TIME.search(child.get("content", "") + str(child.get("deadline") or ""))):
                        destination = "pending_decisions"
                        child["decision_maker"] = child.get("decision_maker") or "暂无"
                        metadata[id(child)].setdefault("parent_path", path)
                        metadata[id(child)].setdefault("original_content", item.get("content", ""))
                        record("ACTION_DOWNGRADED", path, "待办缺动词或时间词，已转为待决策事项", child)
                    groups[destination].append(child)
        for kind, items in groups.items():
            merged, anchors = [], []
            for item in items:
                placed = False
                for j, anchor in enumerate(anchors):
                    if not item.get("content") or not anchor.get("content") or similarity(anchor["content"], metadata[id(item)].get("dedup_anchor", item["content"])) <= 0.90:
                        continue
                    path = f"topics[{ti}].{kind}[{j}]"
                    if _conflicts(merged[j], item):
                        issues.append(make_issue("DUPLICATE_CONFLICT", path, "相似事项字段冲突，已保留两条", source="granularity"))
                        continue
                    first = merged[j]
                    record("DUPLICATE_MERGED", origins[id(item)][0], "重复事项已合并并保留全部引用", item,
                           merged_into=first.get("id", path))
                    origins[id(first)].extend(origins[id(item)])
                    identities[id(first)].extend(identities[id(item)])
                    metadata[id(first)].setdefault("dedup_anchor", anchor["content"])
                    if len(item["content"]) > len(first["content"]):
                        first["content"] = item["content"]
                    for key, value in item.items():
                        if first.get(key) in _PLACEHOLDERS and value not in _PLACEHOLDERS:
                            first[key] = deepcopy(value)
                    for key in ("source_refs", "refs"):
                        if key in first or key in item:
                            first[key] = union_refs(first.get(key, []), item.get(key, []))
                    # 原始引文同样保留，不能只留最长项的 quote。
                    if first.get("source_quote") != item.get("source_quote") and item.get("source_quote"):
                        quotes = []
                        for value in (first.get("source_quote"), item["source_quote"]):
                            for quote in value if isinstance(value, list) else [value]:
                                if quote and quote not in quotes:
                                    quotes.append(quote)
                        first["source_quote"] = quotes
                    placed = True
                    break
                if not placed:
                    anchor = deepcopy(item)
                    anchor["content"] = metadata[id(item)].get("dedup_anchor", item.get("content", ""))
                    anchors.append(anchor)
                    merged.append(item)
            topic[kind] = merged
            for j, item in enumerate(merged):
                if metadata[id(item)]:
                    current_metadata[f"topics[{ti}].{kind}[{j}]"] = metadata[id(item)]
                for identity in identities[id(item)]:
                    final_id_paths[identity] = f"topics[{ti}].{kind}[{j}]"
                for original_path in origins[id(item)]:
                    final_paths.setdefault(original_path, f"topics[{ti}].{kind}[{j}]")
                if any(w in str(item.get("deadline") or "").strip() for w in _VAGUE):
                    issues.append(make_issue("DEADLINE_VAGUE", f"topics[{ti}].{kind}[{j}].deadline", "时限待明确", source="granularity"))
    context["granularity_items"] = current_metadata
    # 修正事实随结果保存；再次处理不产生新修正，诊断仍可展示。
    for change in changes:
        change["target"] = final_id_paths.get(change_ids[id(change)], final_paths.get(
            change["original_path"], change["original_path"]))
    for change in history:
        issues.append(make_issue(change["code"], change["target"], {
            "OWNER_DEFAULTED": "责任人已补为暂无", "ITEM_SPLIT": "多事项已按连接词拆分",
            "ACTION_DOWNGRADED": "待办缺动词或时间词，已转为待决策事项",
            "DUPLICATE_MERGED": "重复事项已合并并保留全部引用"}[change["code"]], source="granularity",
            level="warning" if change["code"] == "ACTION_DOWNGRADED" else "info", evidence=change))
    return GranularityResult(out, aggregate_issues(issues), changes)
