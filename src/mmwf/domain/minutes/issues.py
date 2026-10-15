"""统一问题清单：结构去重、阶段替换与 UTF-8 文本表示。"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import TypedDict


class Issue(TypedDict):
    code: str
    level: str
    blocking: bool
    source: str
    target: str
    message: str
    evidence: dict
    suggestion: str
    rule: str
    detail: str


_LEVEL = {"error": 0, "warning": 1, "info": 2}
_GROUNDING_ERRORS = {"SOURCE_UNAVAILABLE", "UNGROUNDED", "REF_INVALID", "REF_MISMATCH"}


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_issue(code, target, message, *, source, level="warning", blocking=False,
               evidence=None, suggestion="核对原文") -> Issue:
    """仅无依据类 grounding error 可以阻断；软检查不能扩大阻断范围。"""
    if source == "template":
        level = "warning"
    return {"code": code, "level": level, "blocking": bool(
        blocking and source == "grounding" and level == "error" and code in _GROUNDING_ERRORS),
        "source": source, "target": target, "message": message,
        "evidence": deepcopy(evidence or {}), "suggestion": suggestion,
        "rule": code.lower(), "detail": message}


def normalize_issue(raw, *, source="extraction") -> Issue:
    code = str(raw.get("code") or raw.get("rule") or "UNKNOWN").upper()
    origin = raw.get("source", source)
    level = raw.get("level", "warning")
    if level not in _LEVEL:
        level = "warning"
    evidence = deepcopy(raw.get("evidence") or {})
    for key in ("chunk", "chunk_index", "keys", "expected", "actual", "matched"):
        if key in raw:
            evidence.setdefault(key, deepcopy(raw[key]))
    # 不把异常、模型输出或堆栈投影到用户消息。
    message = str(raw.get("message") or raw.get("detail") or "检查发现问题，请核对")
    if (code in {"CHUNK_LLM_FAILED", "CHUNK_BAD_JSON", "CHUNK_SCHEMA_FAILED"}
            or any(key in raw for key in ("traceback", "raw_output", "exception"))
            or "Traceback (" in message):
        message = "抽取检查未完成，请核对原文后重试"
    return make_issue(code, raw.get("target", raw.get("field", "")), message,
                      source=origin, level=level, blocking=raw.get("blocking", False),
                      evidence=evidence, suggestion=raw.get("suggestion", "核对原文"))


def aggregate_issues(*groups, source="extraction") -> list[Issue]:
    merged = {}
    for group in groups:
        for raw in group:
            item = normalize_issue(raw, source=source)
            key = (item["source"], item["code"], item["target"], canonical_json(item["evidence"]))
            if key not in merged:
                merged[key] = item
                continue
            old = merged[key]
            if _LEVEL[item["level"]] < _LEVEL[old["level"]]:
                old["level"] = item["level"]
            old["blocking"] |= item["blocking"]
            old["suggestion"] = "；".join(dict.fromkeys(
                s for text in (old["suggestion"], item["suggestion"]) for s in text.split("；") if s))
    return sorted(merged.values(), key=lambda i: (
        _LEVEL[i["level"]], i["source"], i["target"], i["code"], canonical_json(i["evidence"])))


def replace_issues(existing, current, *, source, targets=None) -> list[Issue]:
    """替换本阶段目标的诊断；空 current 同样清除已修复项。"""
    return aggregate_issues([i for i in existing if not (
        i.get("source") == source and (targets is None or i.get("target") in targets))], current)


def issues_payload(meeting_id, issues, *, complete=True, source_fingerprint="") -> dict:
    items = aggregate_issues(issues)
    return {"meeting_id": meeting_id, "issues": items,
            "counts": {level: sum(i["level"] == level for i in items) for level in _LEVEL},
            "complete": complete, "source_fingerprint": source_fingerprint}


def render_issues_text(meeting_id, issues, *, complete=True, source_fingerprint="", source_version="") -> str:
    payload = issues_payload(meeting_id, issues, complete=complete, source_fingerprint=source_fingerprint)
    def line(value):
        return str(value).replace("\r", " ").replace("\n", " ")
    rows = [f"会议标识：{line(meeting_id)}", f"源版本：{line(source_version)}；指纹：{line(source_fingerprint)}",
            "完整性：" + ("检查完成" if complete else "历史检查未完成"),
            "各级计数：" + "，".join(f"{k}={v}" for k, v in payload["counts"].items())]
    for i in payload["issues"]:
        rows.append(f'[{i["level"]}] {i["code"]} {line(i["target"])}：{line(i["message"])}；依据：{canonical_json(i["evidence"])}；建议：{line(i["suggestion"])}')
    if not payload["issues"]:
        rows.append("当前清单无问题")
    return "\n".join(rows) + "\n"
