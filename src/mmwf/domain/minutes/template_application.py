"""显式图输入适配与模板成文；领域层不读取模板或会议存储。"""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping, Sequence

from mmwf.domain.template import MinutesTemplate, template_section_bindings

_META = ("title", "meeting_time", "location", "host", "attendees", "recorder")


def from_graph_payload(payload: Mapping, *, meeting_meta: Mapping) -> dict:
    """以 G2 后三列表为准，元信息只取调用方快照；不补造溯源。"""
    out = {key: deepcopy(meeting_meta.get(key, [] if key == "attendees" else "")) for key in _META}
    topics = []
    for raw in payload.get("topics") or []:
        topic = deepcopy(dict(raw))
        topic["name"] = topic.pop("title", "")
        # 三列表事项是权威来源，不能复用旧议题内的事项。
        topic.update(resolutions=[], action_items=[], pending_decisions=[])
        topics.append(topic)
    unmatched = {"name": "待补充", "discussion": "", "readings": [],
                 "resolutions": [], "action_items": [], "pending_decisions": []}
    for key in ("resolutions", "action_items"):
        for raw in payload.get(key) or []:
            item = deepcopy(dict(raw))
            item["content"] = item.pop("text", "")
            target = next((t for t in topics if t["name"] == raw.get("topic")), unmatched)
            target[key].append(item)
    if unmatched["resolutions"] or unmatched["action_items"]:
        topics.append(unmatched)
    out["topics"] = topics
    return out


def _display(value) -> str:
    if isinstance(value, list):
        return "、".join(str(v) for v in value if v) or "待补充"
    return str(value).strip() if value is not None and str(value).strip() else "待补充"


def _discussion(topic: Mapping) -> str:
    readings = list(dict.fromkeys(topic.get("readings") or []))
    discussion = topic.get("discussion") or ""
    return ("、".join(readings) + "，" if readings else "") + (discussion or "待补充")


def _item_text(item: Mapping, pending: bool) -> str:
    text = ("待决策：" if pending else "") + _display(item.get("content"))
    text += f"；责任人：{_display(item.get('owner'))}；时限：{_display(item.get('deadline'))}"
    if pending:
        text += f"；决策人：{_display(item.get('decision_maker'))}"
    return text


def apply_template(minutes: Mapping, template: MinutesTemplate) -> dict:
    """保留 canonical 数据，添加 sections；编号最多到章节/议题/事项三级。"""
    bindings = template_section_bindings(template)
    out = deepcopy(dict(minutes))
    sections = []
    for si, heading in enumerate(template.section_order, 1):
        source = bindings[heading]
        section = {"heading": heading, "number": str(si), "items": []}
        if source == "meeting_meta":
            section["items"] = [f"{label}：{_display(minutes.get(key))}" for key, label in (
                ("meeting_time", "时间"), ("location", "地点"), ("host", "主持"),
                ("attendees", "参会人员"), ("recorder", "记录人"))]
        else:
            for topic in minutes.get("topics") or []:
                if not isinstance(topic, Mapping):
                    continue
                entries = []
                if source == "discussions":
                    body = _discussion(topic)
                else:
                    keys = ("resolutions",) if source == "resolutions" else ("action_items", "pending_decisions")
                    for key in keys:
                        for item in topic.get(key) or []:
                            if isinstance(item, Mapping):
                                entries.append((item, key == "pending_decisions"))
                    body = "" if entries else "待补充"
                ti = len(section["items"]) + 1
                child = {"heading": _display(topic.get("name")), "number": f"{si}.{ti}",
                         "text": body, "items": []}
                for ii, (item, pending) in enumerate(entries, 1):
                    entry = deepcopy(dict(item))
                    entry.update(number=f"{si}.{ti}.{ii}", text=_item_text(item, pending))
                    child["items"].append(entry)
                section["items"].append(child)
        if not section["items"]:
            section["items"] = ["待补充"]
        sections.append(section)
    out["sections"] = sections
    return out


def render_template_markdown(structured: Mapping, issues: Sequence[dict]) -> str:
    """按 sections 顺序展示；结构编号不拼入 Markdown 标题。"""
    lines = [f"# {_display(structured.get('title'))}", ""]
    for section in structured.get("sections") or []:
        lines.extend([f"## {section['heading']}", ""])
        for item in section.get("items") or []:
            if isinstance(item, str):
                lines.extend([item, ""])
                continue
            lines.extend([f"### {item['heading']}", ""])
            if item.get("text"):
                lines.extend([item["text"], ""])
            for i, entry in enumerate(item.get("items") or [], 1):
                lines.append(f"{i}. {entry['text']}")
            lines.append("")
    if issues:
        lines.extend(["## 待核对清单", ""])
        for i, issue in enumerate(issues, 1):
            lines.append(f"{i}. {issue.get('detail') or issue.get('rule') or ''}")
    return "\n".join(lines).rstrip() + "\n"
