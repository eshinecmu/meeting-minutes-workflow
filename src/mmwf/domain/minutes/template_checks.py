"""模板的确定性检查；仅返回问题，不修改纪要或决定图路由。"""

from __future__ import annotations

import re
import unicodedata
from typing import Mapping

from mmwf.domain.template import MinutesTemplate, TemplateFormatError, normalize_expression

_SCALARS = ("title", "meeting_time", "location", "host", "recorder")
_NESTED = ("resolutions", "action_items", "pending_decisions")
_LEVELS = (
    ("会议决定", "会议议定", "会议审议通过", "会议同意"),
    ("会议要求",),
    ("会议强调",),
    ("会议认为", "会议指出"),
)


def _issue(rule: str, field: str, detail: str, **extra) -> dict:
    return {"rule": rule, "field": field, "detail": detail, **extra}


def _present(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "待补充" not in value


def _topics(minutes: Mapping):
    raw = minutes.get("topics")
    if isinstance(raw, list):
        for i, topic in enumerate(raw):
            if isinstance(topic, Mapping):
                yield f"topics[{i}]", topic


def check_required_elements(minutes: Mapping, template: MinutesTemplate) -> list[dict]:
    """按配置检查缺键、占位、有效项数及逐项正则。"""
    issues = []
    for element in template.required_elements:
        key, label = element.key, element.label
        if key not in (*_SCALARS, "attendees", "topics", *_NESTED):
            raise TemplateFormatError(f"未知必填要素：{key}", field=key)
        if key in _SCALARS and element.min_count is not None:
            raise TemplateFormatError(f"{key} 是标量，不能配置 min_count", field=key)
        valid = []
        wrong_container = False

        def type_issue(path):
            issues.append(_issue("required_type", path, f"{label}类型不正确"))

        def collect(raw, path, text_key=None):
            nonlocal wrong_container
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return
            if not isinstance(raw, list):
                type_issue(path)
                wrong_container = True
                return
            for i, item in enumerate(raw):
                item_path = f"{path}[{i}]"
                if text_key is not None:
                    if not isinstance(item, Mapping):
                        type_issue(item_path)
                        continue
                    item = item.get(text_key)
                    item_path += f".{text_key}"
                if not isinstance(item, str) and (item is not None or text_key is None):
                    type_issue(item_path)
                elif _present(item):
                    valid.append((item_path, item.strip()))

        if key in _SCALARS:
            value = minutes.get(key)
            if isinstance(value, list) and not value:
                pass
            elif value is not None and not isinstance(value, str):
                type_issue(key)
                wrong_container = True
            elif _present(value):
                valid.append((key, value.strip()))
        elif key in ("attendees", "topics"):
            collect(minutes.get(key), key, "name" if key == "topics" else None)
        else:
            topics = minutes.get("topics")
            if topics is not None and not isinstance(topics, list):
                type_issue("topics")
                wrong_container = True
            elif isinstance(topics, list):
                for i, topic in enumerate(topics):
                    path = f"topics[{i}]"
                    if not isinstance(topic, Mapping):
                        type_issue(path)
                    else:
                        collect(topic.get(key), f"{path}.{key}", "content")
        if not valid:
            if not wrong_container:
                issues.append(_issue("missing_required_element", key, f"{label}缺失"))
            continue
        if element.min_count is not None and len(valid) < element.min_count:
            issues.append(_issue("required_min_count", key, f"{label}有效项数不足",
                                 expected=element.min_count, actual=len(valid)))
        if element.pattern is not None:
            for path, text in valid:
                if re.search(element.pattern, text) is None:
                    issues.append(_issue("required_pattern", path, f"{label}格式不符合要求",
                                         expected=element.pattern, actual=text))
    return issues


def _canonical_texts(minutes: Mapping):
    # 只列拟成文字段，绝不递归扫引文、refs、sections 或检查清单。
    for key in ("title", "meeting_time", "location", "host", "attendees", "recorder"):
        value = minutes.get(key)
        if key == "attendees" and isinstance(value, list):
            for i, text in enumerate(value):
                yield f"attendees[{i}]", text
        elif key != "attendees":
            yield key, value
    for path, topic in _topics(minutes):
        for key in ("name", "discussion", "readings"):
            value = topic.get(key)
            if key == "readings" and isinstance(value, list):
                for i, text in enumerate(value):
                    yield f"{path}.readings[{i}]", text
            elif key != "readings":
                yield f"{path}.{key}", value
        for key in _NESTED:
            entries = topic.get(key)
            if not isinstance(entries, list):
                continue
            for i, entry in enumerate(entries):
                if not isinstance(entry, Mapping):
                    continue
                fields = ("content", "owner", "deadline")
                if key == "pending_decisions":
                    fields += ("decision_maker",)
                for field in fields:
                    yield f"{path}.{key}[{i}].{field}", entry.get(field)


def check_forbidden_expressions(minutes: Mapping, template: MinutesTemplate) -> list[dict]:
    """NFKC、casefold、去空白后的字面子串匹配，每字段每词一条。"""
    words = list(dict.fromkeys(normalize_expression(w) for w in template.style.get("readings_forbidden", [])))
    issues = []
    for path, text in _canonical_texts(minutes):
        if not isinstance(text, str):
            continue
        normalized = normalize_expression(text)
        for word in words:
            if word in normalized:
                issues.append(_issue("forbidden_expression", path, f"含禁用表述：{word}", matched=word))
    return issues


def _reading_level(discussion: str, allowed) -> int | None:
    text = discussion.strip()
    for prefix in sorted(allowed, key=len, reverse=True):
        if text.startswith(prefix):
            text = re.sub(r"^[，,:：\s]*", "", text[len(prefix):])
            break
    # 先分句再规范化，避免中文分隔符被 NFKC 转成半角后漏分句。
    text = unicodedata.normalize("NFKC", re.split(r"[。！？；\n]", text, maxsplit=1)[0]).strip()
    if any(not text[max(0, match.start() - 2):match.start()].endswith(("不", "不予", "未", "没有"))
           for match in re.finditer("决定|议定|审议通过|同意", text)):
        return 0
    imperative = text.startswith(("请", "要")) or any(w in text for w in ("必须", "务必", "应当", "不得", "严禁"))
    for match in re.finditer("完成|推进|落实|确保|抓紧", text):
        before = text[max(0, match.start() - 4):match.start()]
        if not any(w in before for w in ("已", "已经", "均已", "已于", "正在")):
            imperative = True
    if imperative:
        return 1
    if any(w in text for w in ("重要性", "重要意义", "基本原则", "底线意识")):
        return 2
    if any(w in text for w in ("属于", "表明", "说明", "呈现", "总体", "形势", "可行")):
        return 3
    return None


def check_reading_levels(minutes: Mapping, template: MinutesTemplate) -> list[dict]:
    """议题 readings 是标签集合；只据 discussion 的第一句定级。"""
    allowed = template.style.get("readings_allowed", [])
    issues = []
    for path, topic in _topics(minutes):
        readings = topic.get("readings") or []
        # 完整 schema 已约束为 list[str]；诊断缺值时不强制模式校验。
        if not isinstance(readings, list):
            readings = [readings]
        for i, reading in enumerate(readings):
            if reading not in allowed:
                issues.append(_issue("reading_not_allowed", f"{path}.readings[{i}]", f"领读语未获模板允许：{reading}"))
        discussion = topic.get("discussion")
        level = _reading_level(discussion, allowed) if isinstance(discussion, str) and discussion.strip() else None
        if level is None:
            continue
        if not readings:
            issues.append(_issue("reading_missing", f"{path}.readings", "议题缺少领读语", expected=list(_LEVELS[level])))
        for i, reading in enumerate(readings):
            known = any(reading in group for group in _LEVELS)
            if known and reading not in _LEVELS[level]:
                issues.append(_issue("reading_level_mismatch", f"{path}.readings[{i}]", "领读语与首句层级不符", expected=list(_LEVELS[level]), actual=reading))
            elif not known and reading in allowed:
                issues.append(_issue("reading_ambiguous", f"{path}.readings[{i}]", "领读语层级未定义，请人工确认", expected=list(_LEVELS[level])))
    return issues


def check_template(minutes: Mapping, template: MinutesTemplate) -> list[dict]:
    """固定按必填、禁词、领读语顺序聚合；每次返回独立清单。"""
    return (check_required_elements(minutes, template)
            + check_forbidden_expressions(minutes, template)
            + check_reading_levels(minutes, template))
