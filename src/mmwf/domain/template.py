"""纪要模板的领域模型与校验（T1-16）。

职责边界
--------
本模块只做**纯逻辑**：

    YAML 文本 / dict   →   `MinutesTemplate` 领域对象（含校验）

**不读文件、不碰数据库**。原因有两条：

① 分层约束。`check_layers.py` 的 `FORBIDDEN_IN_DOMAIN` 里没有 `yaml`
   （它是纯解析库），但"打开文件"属于 IO —— 领域层只该接受
   "已经被读进来的文本"。读盘放在 `infra/template_store.py`。
② 可测性。纯函数可以拿字符串直接喂，不需要 tmp 文件或 monkeypatch。

模板从哪里来（本卡的裁决 D1）
----------------------------
两类来源，**合并使用、不落地互相覆盖**：

    · **builtin**（`source="builtin"`）
      来自 `config/templates/*.yaml`。SAD §14 的"配置外置"要求：
      **改一行 yaml 立即生效，不改代码、不重启库**（N5.2）。

    · **db**（`source="db"`）
      用户在界面上新建/修改的模板，进 `template` 表。

为什么**不**把 builtin 灌进表里
-------------------------------
灌库（首次启动把 4 个 yaml 写进 `template` 表）看起来更"统一"，
但它会让 N5.2 失效：用户改了 yaml，读到的却是表里那份旧的，
于是"我改了配置没反应"——而**这是最难排查的一类问题**
（文件是对的、代码是对的、结果不对）。

所以：**yaml 永远是活的源头**，表只存"用户自建的增量"。
两类的 `template_id` 若撞车，**builtin 优先**（且 `PUT` 会拒绝改它，
见 `TemplateBuiltinError`）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, cast

from mmwf.domain.errors import InputError, MmwfError

__all__ = [
    "SOURCE_BUILTIN",
    "SOURCE_DB",
    "MAX_NAME_LEN",
    "MAX_SECTION_ORDER",
    "RequiredElement",
    "MinutesTemplate",
    "TemplateFormatError",
    "TemplateBuiltinError",
    "parse_template",
    "template_from_row",
]

#: 内置模板的来源标记。
SOURCE_BUILTIN = "builtin"
#: 自建（进表）模板的来源标记。
SOURCE_DB = "db"

#: 模板名字段上限。50 是个经验值：界面上一行能放下，
#: 又足够写「党组理论学习中心组第 N 次专题学习会纪要」这种长名。
MAX_NAME_LEN = 50

#: `section_order` 的段数上限。设上限是为了防"把整篇纪要塞进模板"，
#: 那会让模板失去"结构约束"的意义。
MAX_SECTION_ORDER = 30

#: `meeting_kind` 允许的取值 —— 来自施工文档 §T2-02：
#: 「四类模板（党组会、专题会、协调会、例会）」。
#: ⚠️ 这里**不写死**为中文字面量，因为 SAD 里 `template.meeting_kind`
#: 是自由文本；但 `config/templates/*.yaml` 里用的是英文枚举。
#: 本卡只做"非空 + 长度"校验，不做枚举校验 —— 枚举会让二期新增会议类型
#: 必须先改代码（违反 N5.2 的扩展预期）。**这是有意留活口。**
_KIND_MIN_LEN = 1
_KIND_MAX_LEN = 40


class TemplateFormatError(InputError):
    """模板内容不合格式（E1001 子类）。

    继承 `InputError` 而不是 `ValueError`，是因为**它一定会走到 API 层**：
    T1-14 的 `GlossaryFormatError` 就吃过这个亏 ——
    它不是 `MmwfError` 子类，端点没接住，用户填错被报成 **E9001**。
    这里从源头避免：**是 `MmwfError` 的子类，E1001，端点不用额外翻译。**
    """

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(
            code="E1001",
            detail=detail,
            user_message="模板内容有问题，请检查后重试。",
            context=dict(ctx),
        )


class TemplateBuiltinError(MmwfError):
    """试图修改内置模板（E1003）。

    ⚠️ **不能复用 E1001**。语义不同：

        E1001 = 「你填的东西不合格式」→ 前端提示"请检查填写内容"
        E1003 = 「这个对象不允许这样操作」→ 前端提示"这是内置模板，
                 不能改；请另存为新模板"

    用错码位，前端就只能在两种完全不同的处境下说同一句话。
    """

    def __init__(self, template_id: str, detail: str = "") -> None:
        super().__init__(
            code="E1003",
            detail=detail or f"template_id={template_id!r} 是内置模板，不允许修改",
            user_message=(
                "这是软件自带的模板，不能直接改。"
                "如果你只想改这一场的写法，请改用「另存为新模板」。"
            ),
            context={"area": "template", "template_id": template_id},
        )


@dataclass(frozen=True)
class RequiredElement:
    """模板的一个必填要素。对应 yaml 里 `required_elements` 的一项。"""

    key: str
    label: str
    pattern: str | None = None
    min_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label}
        # ⚠️ 可选字段**只在有值时出现**。写成 `"pattern": None` 会让前端
        # 多一个"这个字段是什么"的判断分支，而它的语义就是"没有这个约束"。
        if self.pattern is not None:
            out["pattern"] = self.pattern
        if self.min_count is not None:
            out["min_count"] = self.min_count
        return out


@dataclass(frozen=True)
class MinutesTemplate:
    """一份纪要模板。"""

    template_id: str
    name: str
    meeting_kind: str
    required_elements: tuple[RequiredElement, ...] = ()
    section_order: tuple[str, ...] = ()
    style: Mapping[str, Any] = field(default_factory=dict)
    version_no: int = 1
    enabled: bool = True
    source: str = SOURCE_BUILTIN
    updated_at: str | None = None
    note: str | None = None

    @property
    def is_builtin(self) -> bool:
        return self.source == SOURCE_BUILTIN

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "template_id": self.template_id,
            "name": self.name,
            "meeting_kind": self.meeting_kind,
            "version_no": self.version_no,
            "enabled": self.enabled,
            "source": self.source,
            "required_elements": [e.to_dict() for e in self.required_elements],
            "section_order": list(self.section_order),
            "style": dict(self.style),
        }
        if self.updated_at is not None:
            out["updated_at"] = self.updated_at
        if self.note is not None:
            out["note"] = self.note
        return out

    def to_row_payload(self) -> dict[str, str]:
        """转成 `template` 表的两列 JSON（`schema_json` / `style_json`）。

        ⚠️ `ensure_ascii=False`。用默认值会把中文转成 `\\uXXXX`，
        数据库里看起来是一团乱码 —— 排查时人会以为编码坏了。
        """
        schema = {
            "required_elements": [e.to_dict() for e in self.required_elements],
            "section_order": list(self.section_order),
            "meeting_kind": self.meeting_kind,
        }
        return {
            "schema_json": json.dumps(schema, ensure_ascii=False),
            "style_json": json.dumps(dict(self.style), ensure_ascii=False),
        }


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------

def _as_text(value: Any, *, where: str) -> str:
    if value is None:
        raise TemplateFormatError(f"{where} 缺失", field=where)
    if not isinstance(value, str):
        raise TemplateFormatError(
            f"{where} 必须是字符串，实际是 {type(value).__name__}",
            field=where,
            actual=type(value).__name__,
        )
    return value


def _parse_required_elements(raw: Any) -> tuple[RequiredElement, ...]:
    if raw is None:
        raise TemplateFormatError("required_elements 缺失", field="required_elements")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TemplateFormatError(
            "required_elements 必须是数组", field="required_elements"
        )
    if len(raw) == 0:
        raise TemplateFormatError(
            "required_elements 不能是空数组 —— 没有必填要素的模板等于没有约束",
            field="required_elements",
        )

    out: list[RequiredElement] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"required_elements[{i}]"
        if not isinstance(item, Mapping):
            raise TemplateFormatError(f"{where} 必须是对象", field="required_elements")
        key = _as_text(item.get("key"), where=f"{where}.key").strip()
        label = _as_text(item.get("label"), where=f"{where}.label").strip()
        if not key:
            raise TemplateFormatError(f"{where}.key 不能为空", field="required_elements")
        if not label:
            raise TemplateFormatError(
                f"{where}.label 不能为空 —— 界面上要显示它，空标签用户看不懂",
                field="required_elements",
            )
        # ★ 重复 key 会让"缺失即报"的校验**漏报**：同一个 key 检查两遍、
        #   另一个要素永远不被检查。这是静默的，必须在这里拦。
        if key in seen:
            raise TemplateFormatError(
                f"{where}.key 重复：{key!r}",
                field="required_elements",
                duplicate=key,
            )
        seen.add(key)
        if key not in {"title", "meeting_time", "location", "host", "recorder",
                       "attendees", "topics", "resolutions", "action_items", "pending_decisions"}:
            raise TemplateFormatError(f"未知必填要素：{key}", field=key)

        pattern = item.get("pattern")
        if pattern is not None:
            pattern = _as_text(pattern, where=f"{where}.pattern")
            # ⚠️ 正则**编译一次**验它合法。不验的话，坏正则要到
            # 真正校验要素时才炸，那时错误离出错点很远。
            try:
                re.compile(pattern)
            except re.error as exc:
                raise TemplateFormatError(
                    f"{where}.pattern 不是合法正则：{exc}",
                    field="required_elements",
                ) from exc

        min_count = item.get("min_count")
        if min_count is not None:
            if isinstance(min_count, bool) or not isinstance(min_count, int):
                raise TemplateFormatError(
                    f"{where}.min_count 必须是整数"
                    f"（实际 {type(min_count).__name__}）",
                    field="required_elements",
                )
            if min_count < 0:
                raise TemplateFormatError(
                    f"{where}.min_count 不能是负数", field="required_elements"
                )

        if min_count is not None and key in {"title", "meeting_time", "location", "host", "recorder"}:
            raise TemplateFormatError(f"{key} 是标量，不能配置 min_count", field=key)

        out.append(
            RequiredElement(key=key, label=label, pattern=pattern, min_count=min_count)
        )
    return tuple(out)


def _parse_section_order(raw: Any) -> tuple[str, ...]:
    if raw is None:
        raise TemplateFormatError("section_order 缺失", field="section_order")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TemplateFormatError("section_order 必须是数组", field="section_order")
    if len(raw) == 0:
        raise TemplateFormatError("section_order 不能是空数组", field="section_order")
    if len(raw) > MAX_SECTION_ORDER:
        raise TemplateFormatError(
            f"section_order 最多 {MAX_SECTION_ORDER} 段，实际 {len(raw)} 段",
            field="section_order",
        )
    sections: list[str] = []
    for i, item in enumerate(raw):
        text = _as_text(item, where=f"section_order[{i}]").strip()
        if not text:
            raise TemplateFormatError(
                f"section_order[{i}] 是空字符串 —— 空标题在界面上是一条空白行",
                field="section_order",
            )
        if text in sections:
            raise TemplateFormatError(f"section_order 标题重复：{text}", field="section_order")
        sections.append(text)
    return tuple(sections)


def _parse_style(raw: Any, section_order: Sequence[str] = ()) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TemplateFormatError("style 必须是对象", field="style")
    style = dict(raw)
    for key in ("readings_allowed", "readings_forbidden"):
        if key not in style:
            continue
        words = style[key]
        if not isinstance(words, list) or any(not isinstance(w, str) or not w.strip() for w in words):
            raise TemplateFormatError(f"style.{key} 必须是非空字符串数组", field=key)
        if key == "readings_forbidden":
            # 禁词按实际匹配口径去重，白名单则保持精确字符串语义。
            style[key] = list(dict.fromkeys(normalize_expression(w) for w in words))
    if "section_bindings" in style:
        bindings = style["section_bindings"]
        if not isinstance(bindings, Mapping) or set(bindings) != set(section_order):
            raise TemplateFormatError("section_bindings 必须与 section_order 标题一一对应", field="section_bindings")
        if any(not isinstance(v, str) or v not in {"meeting_meta", "discussions", "resolutions", "actions"}
               for v in bindings.values()):
            raise TemplateFormatError("section_bindings 含未知源类型", field="section_bindings")
        style["section_bindings"] = dict(bindings)
    return style


def normalize_expression(text: str) -> str:
    """禁词的统一字面匹配口径；不删除标点。"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def template_section_bindings(template: MinutesTemplate) -> dict[str, str]:
    """旧模板允许读取，实际应用时必须补齐章节绑定。"""
    order = _parse_section_order(template.section_order)
    style = _parse_style(template.style, order)
    if "section_bindings" not in style:
        raise TemplateFormatError("应用模板缺少 style.section_bindings", field="section_bindings")
    # _parse_style validates the values and materializes the bindings as a dict.
    return cast(dict[str, str], style["section_bindings"])


def parse_template(
    data: Mapping[str, Any],
    *,
    source: str = SOURCE_BUILTIN,
    template_id: str | None = None,
) -> MinutesTemplate:
    """把一份模板 dict 解析成 `MinutesTemplate`。

    ⚠️ 两种输入形状**都要接**，因为它们是真实存在的两份数据：

    · **yaml 文件**：顶层有 `meta:`（`template_id/name/meeting_kind/version`），
      其余键在顶层（`required_elements` / `section_order` / `style`）。
    · **API 请求体**：**扁平**的（没有 `meta` 层）。

    第一版只写了认 `meta` 的分支 → 从 API 建模板时 `name` 报"缺失"，
    而请求体里明明有 `name`。**这是本卡实际踩到的缺陷**（详见施工卡 §4 坑 1）。
    """
    if not isinstance(data, Mapping):
        raise TemplateFormatError(
            f"模板必须是对象，实际是 {type(data).__name__}"
        )

    meta = data.get("meta")
    if meta is not None and not isinstance(meta, Mapping):
        raise TemplateFormatError("meta 必须是对象", field="meta")
    meta_map: Mapping[str, Any] = meta or {}

    def pick(key: str) -> Any:
        """先在 `meta` 里找，再在顶层找。"""
        if key in meta_map:
            return meta_map[key]
        return data.get(key)

    tid = template_id or pick("template_id")
    if tid is None:
        # ⚠️ **内置模板必须带 id**（它是 yaml 里的固定契约，丢了就找不回来），
        # 但**自建模板可以没有** —— 前端建模板时还没 id，由服务端生成
        # （`TemplateStore.new_template_id()`）。
        # 第一版这里无条件抛"template_id 缺失"，从 API 建模板时直接失败。
        if source == SOURCE_DB:
            tid = ""
        else:
            raise TemplateFormatError("template_id 缺失", field="template_id")
    tid = _as_text(tid, where="template_id").strip()
    if not tid and source != SOURCE_DB:
        raise TemplateFormatError("template_id 不能为空", field="template_id")

    name = _as_text(pick("name"), where="name").strip()
    if not name:
        raise TemplateFormatError("name 不能为空", field="name")
    if len(name) > MAX_NAME_LEN:
        raise TemplateFormatError(
            f"name 最多 {MAX_NAME_LEN} 字，实际 {len(name)} 字", field="name"
        )

    kind = _as_text(pick("meeting_kind"), where="meeting_kind").strip()
    if not (_KIND_MIN_LEN <= len(kind) <= _KIND_MAX_LEN):
        raise TemplateFormatError(
            f"meeting_kind 长度必须在 {_KIND_MIN_LEN}~{_KIND_MAX_LEN} 之间",
            field="meeting_kind",
        )

    version_raw = pick("version")
    version_no = 1
    if version_raw is not None:
        if isinstance(version_raw, bool) or not isinstance(version_raw, int):
            raise TemplateFormatError("version 必须是整数", field="version")
        if version_raw < 1:
            raise TemplateFormatError("version 必须 >= 1", field="version")
        version_no = version_raw

    enabled_raw = pick("enabled")
    enabled = True
    if enabled_raw is not None:
        if not isinstance(enabled_raw, bool):
            raise TemplateFormatError("enabled 必须是布尔值", field="enabled")
        enabled = enabled_raw

    note = pick("note")
    if note is not None:
        note = _as_text(note, where="note")

    section_order = _parse_section_order(pick("section_order"))
    return MinutesTemplate(
        template_id=tid,
        name=name,
        meeting_kind=kind,
        required_elements=_parse_required_elements(pick("required_elements")),
        section_order=section_order,
        style=_parse_style(pick("style"), section_order),
        version_no=version_no,
        enabled=enabled,
        source=source,
        note=note,
    )


def template_from_row(row: Mapping[str, Any]) -> MinutesTemplate:
    """把 `template` 表的一行还原成领域对象。

    表把 `required_elements` / `section_order` / `meeting_kind` 塞进了
    `schema_json` 一列（DDL 如此），所以这里要拆包。
    """
    try:
        schema = json.loads(row["schema_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise TemplateFormatError(
            f"template_id={row.get('template_id')!r} 的 schema_json 不是合法 JSON",
            field="schema_json",
        ) from exc
    if not isinstance(schema, Mapping):
        raise TemplateFormatError(
            f"template_id={row.get('template_id')!r} 的 schema_json 不是对象",
            field="schema_json",
        )
    try:
        style = json.loads(row["style_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise TemplateFormatError(
            f"template_id={row.get('template_id')!r} 的 style_json 不是合法 JSON",
            field="style_json",
        ) from exc
    if not isinstance(style, Mapping):
        style = {}

    merged: dict[str, Any] = {
        "template_id": row["template_id"],
        "name": row["name"],
        "meeting_kind": schema.get("meeting_kind") or row.get("meeting_kind") or "",
        "required_elements": schema.get("required_elements"),
        "section_order": schema.get("section_order"),
        "style": style,
        "version": row.get("version_no"),
        "enabled": bool(row.get("enabled", 1)),
    }
    return parse_template(merged, source=SOURCE_DB)
