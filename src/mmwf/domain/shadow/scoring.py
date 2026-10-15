"""只汇总人工评分；定位校验不代表语义判断正确。"""
from __future__ import annotations

from dataclasses import asdict
from fractions import Fraction
import re

from .models import (Metric, ShadowSummary, freeze, parse_scores, thaw, envelope)
from .risks import aggregate_risks


def resolve_location(documents, location):
    """允许显式文件的 JSON Pointer 或权威分段 seg-N；不做模糊文本匹配。"""
    if not isinstance(location, str) or "#" not in location:
        raise ValueError("定位须为 文件#JSON-Pointer 或 文件#seg-N")
    name, anchor = location.split("#", 1)
    if name not in documents:
        raise ValueError(f"定位文件不存在：{name}")
    value = documents[name]
    if anchor.startswith("seg-"):
        if not isinstance(value, (list, tuple)) or not any(str(s.get("seq")) == anchor[4:] for s in value):
            raise ValueError("源分段定位不存在")
        return
    if not anchor.startswith("/"):
        raise ValueError("须定位具体内容")
    try:
        for part in anchor[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, (list, tuple)) and part.isdecimal() else value[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"定位不存在：{location}") from exc
    if value is None or value == "":
        raise ValueError("定位内容为空")


def validate_scores(batch, scores):
    """缺评留给整批 INCOMPLETE；额外、混批、改稿或非法定位直接拒绝。"""
    envelope(scores)
    if scores["batch_id"] != batch.batch_id or scores["dataset_kind"] != batch.dataset_kind:
        raise ValueError("评分混批或数据性质不一致")
    parsed = parse_scores(scores)
    meetings = {m["meeting_id"]: m for m in batch.meetings}
    if len(meetings) != len(batch.meetings):
        raise ValueError("计划会议 id 重复")
    for score in parsed:
        if score.meeting_id not in meetings:
            raise ValueError("评分含额外会议")
        meeting = meetings[score.meeting_id]
        candidate = meeting["candidate"]
        row = score.data
        for field in ("candidate_fingerprint", "source_fingerprint", "template_fingerprint"):
            if row[field] != candidate[field]:
                raise ValueError(f"{field} 不匹配，改稿须重新评分")
        docs = meeting["documents"]
        for kind, section in (("topic", "topics"), ("resolution", "resolutions")):
            for match in row[f"{kind}_matches"]:
                path = match["candidate_id"]
                pattern = r"topics\[\d+\]" if kind == "topic" else r"topics\[\d+\]\.resolutions\[\d+\]"
                if not re.fullmatch(pattern, path):
                    raise ValueError(f"{section} candidate_id 非法，不得混入待议事项")
                pointer = re.sub(r"\[(\d+)\]", r"/\1", path).replace(".", "/")
                resolve_location(docs, "candidate.json#/minutes/" + pointer)
                resolve_location(docs, match["evidence"])
                if match["evidence"] != "candidate.json#/minutes/" + pointer:
                    raise ValueError("配对证据必须指向所配候选项")
        seen = set()
        for statement in row["statements"]:
            loc = statement["candidate_location"]
            if not loc.startswith("candidate.json#/minutes/") or loc in seen:
                raise ValueError("陈述候选定位重复或不属于候选正文")
            seen.add(loc)
            resolve_location(docs, loc)
            for source in statement["source_locations"]:
                if source.startswith("candidate.json#"):
                    raise ValueError("源证据不得指向候选稿")
                resolve_location(docs, source)
        for divergence in row["divergences"]:
            if not divergence["location"].startswith("candidate.json#/minutes/"):
                raise ValueError("分歧须定位候选稿")
            resolve_location(docs, divergence["location"])
        aggregate_risks(meeting["issues"], row["issue_reviews"])
    return parsed


def _metric(n, d, threshold, *, maximum=False):
    passed = None if not d else ((Fraction(n, d) <= Fraction(str(threshold))) if maximum else
                                 (Fraction(n, d) >= Fraction(str(threshold))))
    return asdict(Metric(n, d, n / d if d else None, threshold, passed))


def _merge_risk(rows):
    keys = ("total", "reviewed", "true_positive", "false_positive", "uncertain", "unreviewed", "numerator", "denominator")
    result = {k: sum(r[k] for r in rows) for k in keys}
    result.update(rate=result["numerator"] / result["denominator"] if result["denominator"] else None,
                  coverage=result["reviewed"] / result["total"] if result["total"] else None,
                  complete=all(r["complete"] for r in rows))
    return result


def _action_population(score, risk):
    """可选人工待办全集：独立分母，不替换降级事件分母。"""
    evidence = score.get("supplementary") or {}
    ids = evidence.get("real_action_ids")
    matches = evidence.get("downgrade_action_matches")
    if ids is None and matches is None:
        return {"status": "PENDING", "numerator": None, "denominator": None, "rate": None}
    if not isinstance(ids, (list, tuple)) or any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("real_action_ids 必须为唯一人工待办数组")
    if not isinstance(matches, (list, tuple)):
        raise ValueError("downgrade_action_matches 必须提供人工对应关系")
    labels = {r["issue_id"]: r for r in score["issue_reviews"]}
    downgraded = {i["issue_id"] for i in risk["issues"] if i["code"] == "ACTION_DOWNGRADED"}
    affected, seen = set(), set()
    for match in matches:
        if not hasattr(match, "get") or match.get("action_id") not in ids or match.get("issue_id") not in downgraded:
            raise ValueError("人工待办对应关系引用不存在")
        pair = (match["issue_id"], match["action_id"])
        if pair in seen:
            raise ValueError("人工待办对应关系重复")
        seen.add(pair)
        label = labels.get(match["issue_id"], {})
        if label.get("verdict") != "uncertain" and label.get("false_downgrade") is True:
            affected.add(match["action_id"])
    covered = {m["issue_id"] for m in matches}
    complete = risk["downgrade"]["complete"] and downgraded <= covered
    return {"status": "REVIEWED" if complete else "INCOMPLETE", "numerator": len(affected),
            "denominator": len(ids), "rate": len(affected) / len(ids) if ids and complete else None,
            "real_action_ids": list(ids), "downgrade_action_matches": thaw(matches)}


def aggregate_metrics(batch, scores, thresholds):
    parsed = validate_scores(batch, scores)
    by_id = {s.meeting_id: s.data for s in parsed}
    positions = {s.meeting_id: i for i, s in enumerate(parsed)}
    a = thresholds.agreement
    rows, risks, gaps = [], [], list(batch.metadata.get("gaps", ()))
    divergences = {c: {"count": 0, "meeting_count": 0} for c in ("更保守", "更激进", "系统错误")}
    supplementary = []
    for sequence, meeting in enumerate(batch.meetings, 1):
        mid = meeting["meeting_id"]
        score = by_id.get(mid)
        row = {"sequence": sequence, "meeting_name": meeting["name"], "meeting_kind": meeting["meeting_kind"],
               "duration_seconds": meeting["duration_seconds"], "meeting_id": mid, "batch_id": batch.batch_id,
               "dataset_kind": batch.dataset_kind, "score_path": str(batch.metadata.get("scores_path", "scores.json")) +
               (f"#/scores/{positions[mid]}" if mid in positions else f"#unscored-meeting={mid}"),
               "notes": meeting.get("failure_reason", ""),
               **{k: None for k in ("elements_complete", "topics_matched", "topics_human", "resolutions_matched",
                                    "resolutions_human", "ungrounded_count", "statement_count", "structure_compliant", "divergence_classes")}}
        if score:
            if set(score["required_elements"]) != set(a["required_elements"]):
                raise ValueError("评分必填项与批次约定不一致")
            row.update(elements_complete=all(score["element_presence"].values()),
                       topics_matched=len(score["topic_matches"]), topics_human=len(score["human_topic_ids"]),
                       resolutions_matched=len(score["resolution_matches"]), resolutions_human=len(score["human_resolution_ids"]),
                       ungrounded_count=sum(not s["supported"] for s in score["statements"]),
                       statement_count=len(score["statements"]), structure_compliant=score["structure_compliant"],
                       divergence_classes=[d["class"] for d in score["divergences"]])
            for c, counts in divergences.items():
                count = row["divergence_classes"].count(c)
                counts["count"] += count
                counts["meeting_count"] += bool(count)
            supplementary.append({"meeting_id": mid, "evidence": thaw(score.get("supplementary")),
                                  "status": "PROVIDED_NOT_VERIFIED" if score.get("supplementary") else "PENDING"})
        else:
            gaps.append(f"{mid} 漏评分")
        risk = aggregate_risks(meeting["issues"], score["issue_reviews"] if score else ())
        risk["action_population"] = _action_population(score, risk) if score else {"status": "PENDING", "numerator": None, "denominator": None, "rate": None}
        risks.append({"meeting_id": mid, **risk})
        if not meeting.get("issues_complete", True):
            gaps.append(f"{mid} 原始 issues 检查未完成")
        if not risk["complete"]:
            gaps.append(f"{mid} issue 审查不完整（含未审/uncertain/未明确误降级）")
        if meeting["status"] != "generated":
            gaps.append(f"{mid} {meeting['status']}: {meeting.get('failure_reason', '')}")
        rows.append(row)
    total = len(batch.meetings)
    complete = (len(parsed) == total and batch.metadata.get("complete", True) is True and
                all(m["status"] == "generated" and m.get("issues_complete", True) is True for m in batch.meetings) and
                all(r["complete"] for r in risks))
    def count(key):
        return sum(r[key] or 0 for r in rows)
    metrics = {
        "required_elements": _metric(count("elements_complete"), total, thresholds.required_elements_min),
        "topic_recall": _metric(count("topics_matched"), count("topics_human"), thresholds.topic_recall_min),
        "resolution_recall": _metric(count("resolutions_matched"), count("resolutions_human"), thresholds.resolution_recall_min),
        "ungrounded": _metric(count("ungrounded_count"), count("statement_count"), thresholds.ungrounded_max, maximum=True),
        "structure": _metric(count("structure_compliant"), total, thresholds.structure_min),
    }
    # 不完整批次保留原始数，但不将已评分子集描述为全批次达标。
    if not complete:
        for metric in metrics.values():
            metric.update(value=None, passed=None)
    evaluable = all(m["denominator"] for m in metrics.values())
    quality = complete and evaluable and all(m["passed"] for m in metrics.values()) and not (
        divergences["更激进"]["count"] or divergences["系统错误"]["count"])
    kinds = {m["meeting_kind"] for m in batch.meetings}
    boundary = a["long_meeting_boundary_seconds"]
    coverage = {"four_kinds": set(a["meeting_kinds"]) <= kinds,
                "short": any(m["duration_seconds"] < boundary for m in batch.meetings),
                "long": any(m["duration_seconds"] >= boundary for m in batch.meetings)}
    eligible = bool(quality and batch.dataset_kind == "real" and total >= 20 and all(coverage.values()) and a["confirmed"])
    if batch.dataset_kind == "real":
        if total < 20:
            gaps.append("真实唯一会议不足20场")
        if not all(coverage.values()):
            gaps.append("四类会议或长短会覆盖不足")
        if not a["confirmed"]:
            gaps.append("阈值约定未确认")
    if count("ungrounded_count"):
        gaps.append("出现无依据陈述：回到阶段 1 整改，保留失败批次后重新验证")
    groups = {}
    for risk in risks:
        for group in risk["groups"]:
            groups.setdefault((group["source"], group["code"]), []).append(group)
    populations = [r["action_population"] for r in risks]
    supplied = [p for p in populations if p["denominator"] is not None]
    population_complete = bool(populations) and all(p["status"] == "REVIEWED" for p in populations)
    population_n = sum(p["numerator"] for p in supplied)
    population_d = sum(p["denominator"] for p in supplied)
    population_summary = {"status": "REVIEWED" if population_complete else "PENDING",
        "provided_meetings": len(supplied), "planned_meetings": total,
        "numerator": population_n if supplied else None, "denominator": population_d if supplied else None,
        "rate": population_n / population_d if population_complete and population_d else None}
    data = {"format_version": 1, "batch_id": batch.batch_id, "dataset_kind": batch.dataset_kind,
            "labels": ["影子生成，禁止交付"] + (["SYNTHETIC / 合成数据，非真实验收结果"] if batch.dataset_kind == "synthetic" else []),
            "status": "INCOMPLETE" if not complete else ("EVALUATED" if evaluable else "NOT_EVALUABLE"),
            "complete": complete, "metrics": metrics, "thresholds": {k: thaw(getattr(thresholds, k)) for k in thresholds.__dataclass_fields__},
            "records": rows, "per_meeting_recall": [{"meeting_id": r["meeting_id"],
                "topics": r["topics_matched"] / r["topics_human"] if r["topics_human"] else None,
                "resolutions": r["resolutions_matched"] / r["resolutions_human"] if r["resolutions_human"] else None} for r in rows],
            "divergences": divergences, "coverage": coverage,
            "risks": {"meetings": risks, "downgrade": _merge_risk([r["downgrade"] for r in risks]),
                      "rules": _merge_risk([r["rules"] for r in risks]), "action_population": population_summary,
                      "groups": [{"source": s, "code": c, **_merge_risk(v)} for (s, c), v in sorted(groups.items())]},
            "quality_pass": bool(quality), "eligible_for_stage2": eligible,
            "recommendation": "建议进入阶段 2（须单位决定）" if eligible else ("待验" if batch.dataset_kind == "synthetic" else "不满足"),
            "real_run_status": "PENDING" if batch.dataset_kind == "synthetic" or not complete else "REVIEWED_METADATA_ONLY",
            "real_meetings_counted": 0 if batch.dataset_kind == "synthetic" or not complete else total,
            "tool_validation_status": "REPORT_GENERATED_NOT_ACCEPTANCE",
            "meetings": [thaw({k: v for k, v in m.items() if k != "documents"}) for m in batch.meetings],
            "provenance": thaw(batch.metadata), "gaps": sorted(set(gaps)), "supplementary": supplementary,
            "evidence_ledger": {"Q1": "人工要素齐备率，真实证据另验", "Q5": "人工无依据率，真实证据另验",
                "Q2": "PENDING：阶段2实际返工轮次≤1", "Q3": "PENDING：两小时会议人工投入≤20分钟，非生成耗时",
                "Q4": "PENDING：U-GR + 人名/数字/机构人工抽检、全部溯源及约定准确率",
                "R-5": "PENDING：DR-05/DR-06 + 真实影子无依据率=0 联证",
                "other": "PENDING：R-1～R-4、V4时间跳转、Electron视觉及整体交付另验"}}
    return ShadowSummary(freeze(data))
