"""数字和人名的一致性提醒：只读正文和权威源，不自动纠正。"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import cast

from mmwf.domain.segments import Segment

from .issues import Issue, aggregate_issues, make_issue
from .post_validation import VerifiedRef, business_entries, source_segments, verify_references
from .template_checks import _canonical_texts

_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_N = r"[0-9零〇一二两三四五六七八九十百千万亿点.]+"
_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)|(?:([0-9零〇一二两三四五六七八九十]{4})年)?([0-9一二三四五六七八九十]{1,3})月([0-9一二三四五六七八九十]{1,3})[日号]")
_NUMBER = re.compile(f"({_N})(亿元|万元|元|万|亿|人|名|位|%|百分比)")
_U = r"(?:万元|亿元|元|人|名|位|%)"
_UNSUPPORTED = re.compile(
    rf"{_N}(?:{_U})?(?:至|到|[-~～]){_N}(?:{_U})?"
    rf"|(?:约|大约|近|超过|不足|数|几){_N}(?:{_U})?"
    rf"|{_N}分之{_N}|{_N}(?:{_U})?(?:余|多|左右)(?:{_U})?"
)
_PERSON = re.compile(r"(?:由|请|让)([\u4e00-\u9fff]{2,4}?)(?:同志|先生|女士|负责|牵头)")
_ORG_END = ("处", "科", "局", "办公室", "部门", "工作组", "公司")
_PLACEHOLDERS = {"", "暂无", "待补充"}


def chinese_decimal(text: str) -> Decimal:
    """十进制中文数（含万亿节与逐位小数），不使用浮点数。"""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return Decimal(text)
    whole = text
    for unit in ("亿", "万"):
        if unit in whole:
            left, right = whole.split(unit, 1)
            return chinese_decimal(left or "一") * _UNITS[unit] + (chinese_decimal(right) if right else 0)
    whole, dot, fraction = text.partition("点")
    if dot:
        return chinese_decimal(whole) + Decimal("0." + "".join(str(_DIGITS[c]) for c in fraction))
    if all(c in _DIGITS for c in whole):
        return Decimal("".join(str(_DIGITS[c]) for c in whole))
    total, digit = 0, 0
    for char in whole:
        if char in _DIGITS:
            digit = _DIGITS[char]
        else:
            total += (digit or 1) * _UNITS[char]
            digit = 0
    return Decimal(total + digit)


@dataclass(frozen=True)
class NumericToken:
    raw: str
    category: str
    value: object


def numeric_tokens(text: str | None) -> tuple[list[NumericToken], list[str]]:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"(?<=[0-9零〇一二两三四五六七八九十百千万亿点.])\s+(?=[0-9零〇一二两三四五六七八九十百千万亿点.元人名位%])", "", text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    tokens = []
    for m in _DATE.finditer(text):
        y, month, day = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
        tokens.append(NumericToken(m.group(), "date", (int(chinese_decimal(y)) if y else None,
                                                     int(chinese_decimal(month)), int(chinese_decimal(day)))))
    text = _DATE.sub(lambda m: " " * len(m.group()), text)
    unsupported = [m.group() for m in _UNSUPPORTED.finditer(text) if not m.group().startswith("百分之")]
    text = _UNSUPPORTED.sub(lambda m: m.group() if m.group().startswith("百分之") else " " * len(m.group()), text)
    # 百分之与百分数使用独立类别。
    for m in re.finditer(f"百分之({_N})", text):
        tokens.append(NumericToken(m.group(), "percent", chinese_decimal(m.group(1))))
    text = re.sub(f"百分之({_N})", "", text)
    for m in _NUMBER.finditer(text):
        number, unit = m.groups()
        try:
            value = chinese_decimal(number)
        except (DecimalException, KeyError, ValueError):
            unsupported.append(m.group())
            continue
        category = "money" if unit in ("元", "万元", "亿元", "万", "亿") else "people" if unit in ("人", "名", "位") else "percent"
        value *= {"万元": 10000, "万": 10000, "亿元": 100000000, "亿": 100000000}.get(unit, 1)
        tokens.append(NumericToken(m.group(), category, value))
    return tokens, unsupported


def explicit_names(text: str | None) -> list[str]:
    return cast(list[str], _PERSON.findall(text or ""))


ReferenceCheck = tuple[list[VerifiedRef], list[Issue]]
Scopes = Mapping[str, ReferenceCheck]


def _scopes(minutes: Mapping[str, object], segments: Sequence[Segment]) -> dict[str, ReferenceCheck]:
    return {path: verify_references(entry, segments, path) for path, entry in business_entries(minutes)}


def _scope(path: str, scopes: Scopes, segments: Sequence[Segment]) -> tuple[str, list[str], bool]:
    parent = max((p for p in scopes if path.startswith(p + ".")), key=len, default=None)
    refs, issues = cast(Mapping[str | None, ReferenceCheck], scopes).get(parent, ([], []))
    valid = refs and not any(i["blocking"] for i in issues)
    return ("\n".join(r["text"] for r in refs) if valid else "\n".join(s.text for s in segments),
            [r["segment_id"] for r in refs] if valid else [], bool(valid))


def check_numbers(minutes: Mapping[str, object], segments: Iterable[Segment] | None = None, *,
                  record_markdown: str | None = None) -> list[Issue]:
    source = source_segments(segments, record_markdown)
    scopes = _scopes(minutes, source)
    issues = []
    for path, text in _canonical_texts(minutes):
        if not isinstance(text, str):
            continue
        tokens, unsupported = numeric_tokens(text)
        scope, ids, local = _scope(path, scopes, source)
        expected, _ = numeric_tokens(scope)
        def add(code: str, message: str, actual: str) -> None:
            issues.append(make_issue(code, path, message, source="consistency", evidence={
                "actual": actual, "expected": [t.raw for t in expected], "segment_ids": ids}))
        for raw in unsupported:
            add("NUMERIC_UNPARSED", "数值表达暂不支持，请人工核对", raw)
        if (tokens or unsupported) and not local:
            add("NUMERIC_SCOPE_FALLBACK", "无已验证的局部引用，退到全记录稿核对", text)
        for token in tokens:
            same = [t for t in expected if t.category == token.category]
            if not any(t.value == token.value for t in same):
                add("NUMERIC_MISMATCH" if same else "NUMERIC_UNSUPPORTED", "数值可能与原文不一致，请核对", token.raw)
    return aggregate_issues(issues)


def _term_value(term: object, key: str, default: object = None) -> object:
    return term.get(key, default) if isinstance(term, dict) else getattr(term, key, default)


def check_person_names(minutes: Mapping[str, object], segments: Iterable[Segment] | None = None, *,
                  record_markdown: str | None = None, terms: Iterable[object] = ()) -> list[Issue]:
    source = source_segments(segments, record_markdown)
    scopes = _scopes(minutes, source)
    aliases: dict[str, set[str]] = {}
    orgs: set[str] = set()
    for term in terms:
        if not _term_value(term, "enabled", True):
            continue
        canonical = cast(str, _term_value(term, "canonical", ""))
        names = [canonical, *cast(Sequence[str], _term_value(term, "aliases", []) or [])]
        category = _term_value(term, "category", _term_value(term, "kind", ""))
        if category == "org":
            orgs.update(names)
        if category == "person":
            for name in names:
                aliases.setdefault(name, set()).add(canonical)
    def person(name: str) -> bool:
        return name not in _PLACEHOLDERS and name not in orgs and not name.endswith(_ORG_END)
    full = "\n".join((s.speaker or "") + "：" + s.text for s in source)
    issues = []
    for path, text in _canonical_texts(minutes):
        if not isinstance(text, str):
            continue
        slot = path in ("host", "recorder") or path.startswith("attendees[") or path.endswith((".owner", ".decision_maker"))
        names = [text.strip()] if slot else explicit_names(text)
        for name in names:
            if not person(name):
                continue
            canonicals = aliases.get(name, {name})
            code, level = None, "warning"
            scope, ids, _ = _scope(path, scopes, source)
            if slot and not path.startswith("topics["):
                scope = full
            elif ids:
                scope += "\n" + "\n".join(s.speaker or "" for s in source if f"seg-{s.seq}" in ids)
            if len(canonicals) > 1:
                code = "PERSON_AMBIGUOUS"
            else:
                canonical = next(iter(canonicals))
                equivalents = {a for a, cs in aliases.items() if cs == {canonical}} | {canonical}
                # 只认原文槽位/有界模式/说话人，或原文明确列名；不猜相近字。
                source_names = set(explicit_names(scope))
                source_names.update(re.findall(r"(?:主持人|记录人|参会人员|参会人|出席)[：:]\s*([^\n。]+)", scope))
                source_names.update(s.speaker for s in source if s.speaker and (not ids or f"seg-{s.seq}" in ids))
                present = any(a in scope and (a in source_names or any(a in n for n in source_names if n)) for a in equivalents)
                if not present:
                    code = "PERSON_UNSUPPORTED"
                elif name != canonical or name not in scope:
                    code, level = "PERSON_ALIAS", "info"
            if code:
                issues.append(make_issue(code, path, "人名缺少明确依据，请核对" if code == "PERSON_UNSUPPORTED" else
                                         "人名别名存在歧义，请核对" if code == "PERSON_AMBIGUOUS" else "人名按精确别名匹配",
                                         source="consistency", level=level, evidence={"actual": name, "canonical": sorted(canonicals), "segment_ids": ids},
                                         suggestion="核对原文列名和术语别名"))
    return aggregate_issues(issues)
