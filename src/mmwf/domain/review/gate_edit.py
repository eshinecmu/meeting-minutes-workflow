"""闸口编辑的纯校验、规范化与字段摘要（T2-03 C3）。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from mmwf.domain.errors import MmwfError

G2_FIELDS = ("topics", "resolutions", "action_items")
EDITABLE = {
    "topics": {"title", "discussion", "readings"},
    "resolutions": {"text", "owner", "deadline"},
    "action_items": {"text", "owner", "deadline"},
}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def invalid(message):
    raise MmwfError(code="E4001", detail=message)


def normalise(value):
    """仅规范化可编辑文本；只读证据逐字保留。"""
    out = deepcopy(value)
    for name in G2_FIELDS:
        items = out.get(name)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in EDITABLE[name]:
                if isinstance(item.get(key), str):
                    item[key] = item[key].replace("\r\n", "\n").replace("\r", "\n")
                    if key == "owner" and not item[key].strip():
                        item[key] = "暂无"
    if isinstance(out.get("minutes_markdown"), str):
        out["minutes_markdown"] = out["minutes_markdown"].replace("\r\n", "\n").replace("\r", "\n")
    return out


def gate_content(gate, state):
    keys = G2_FIELDS if gate == "G2" else ("minutes_markdown",)
    return normalise({key: deepcopy(state.get(key, [] if gate == "G2" else "")) for key in keys})


def validate_edit(gate, baseline, payload):
    """整列表替换；索引 / id 定位既有条目，不新增、删除或迁移归属。"""
    allowed = set(G2_FIELDS) if gate == "G2" else {"minutes_markdown"}
    if gate not in {"G2", "G3"} or not isinstance(payload, dict) or not payload or set(payload) - allowed:
        invalid("编辑载荷必须是非空对象，且仅含本闸口白名单字段")
    before = gate_content(gate, baseline)
    after = deepcopy(before)
    if gate == "G3":
        value = payload["minutes_markdown"]
        if not isinstance(value, str) or not value.strip():
            invalid("纪要必须是非空字符串")
        after["minutes_markdown"] = value
        return normalise(after)
    for items in payload.values():
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            invalid("编辑列表必须是对象列表")
    if not any(payload.get(name, before.get(name)) for name in G2_FIELDS):
        raise MmwfError(code="E4003", detail="三列表不能全部为空")
    for name, items in payload.items():
        old_items = baseline.get(name) or []
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            invalid(f"{name} 必须是对象列表")
        # §2.2 同时提及可清空与禁止删除；按较窄范围保留既有条目。
        # 原为空的列表可提交 []，最终三列表全空仍优先报 E4003。
        if len(items) != len(old_items):
            invalid("不允许新增或删除条目")
        ids = [i["id"] for i in items if "id" in i]
        if len({canonical_json(i) for i in ids}) != len(ids):
            invalid("条目 id 不得重复")
        for old, new in zip(old_items, items):
            if set(new) - set(old) - EDITABLE[name]:
                invalid("不允许新增只读元数据")
            for key in set(old) | set(new):
                if key not in EDITABLE[name] and (key not in old or key not in new or canonical_json(old[key]) != canonical_json(new[key])):
                    invalid(f"只读字段不得修改：{name}.{key}")
            required = "title" if name == "topics" else "text"
            if not isinstance(new.get(required), str) or not new[required].strip():
                invalid(f"{name}.{required} 必须是非空字符串")
            for key in EDITABLE[name] & new.keys():
                value = new[key]
                if key == "readings":
                    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                        invalid("readings 必须是字符串列表")
                elif key == "deadline" and value is None:
                    pass
                elif not isinstance(value, str):
                    invalid(f"{key} 必须是字符串")
        after[name] = deepcopy(items)
    if not any(after.get(name) for name in G2_FIELDS):
        raise MmwfError(code="E4003", detail="三列表不能全部为空")
    # 无 id 时按原索引定位；同形无 id 条目无法推断身份，不猜测重排。
    titles = [t.get("title") for t in before["topics"]]
    renames = {old["title"]: new["title"] for old, new in zip(before["topics"], after["topics"]) if old["title"] != new["title"]}
    new_titles = [t.get("title") for t in after["topics"]]
    if renames and (len(titles) != len(set(titles)) or len(new_titles) != len(set(new_titles))):
        invalid("议题标题重复，无法确定事项关联，请人工核对")
    for name in ("resolutions", "action_items"):
        for item in after[name]:
            for key in ("topic", "topic_title"):
                if isinstance(item.get(key), str) and item[key] in renames:
                    item[key] = renames[item[key]]
    return normalise(after)


def field_diff(before, after):
    """字段路径排序；摘要不携带正文，字符数按规范化 JSON 计算。"""
    changed = {}

    def walk(old, new, path):
        if canonical_json(old) == canonical_json(new):
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                child = f"{path}.{key}" if path else key
                if key not in old or key not in new:
                    emit(old.get(key), new.get(key), child, "add" if key not in old else "remove")
                else:
                    walk(old[key], new[key], child)
        elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new) and all(isinstance(i, dict) for i in old + new):
            for i, (a, b) in enumerate(zip(old, new)):
                walk(a, b, f"{path}[{i}]")
        else:
            emit(old, new, path, "replace")

    def emit(old, new, path, op):
        changed[path] = {"op": op, "old_sha256": fingerprint(old), "new_sha256": fingerprint(new),
                         "old_chars": len(canonical_json(old)), "new_chars": len(canonical_json(new))}

    walk(before, after, "")
    return dict(sorted(changed.items()))
