"""模板仓储（T1-16）。

职责边界
--------
本模块是**唯一**碰 `template` 表的地方，也是**唯一**读
`config/templates/*.yaml` 的地方。理由与 `transcript_store.py` 完全相同：

    `sqlite3` 是 `check_layers.py` 的 `FORBIDDEN_IN_API` 成员。
    api 层要读模板，**必须先有这一层** —— 否则一写就是分层违规。

本模块做三件事
--------------
1. **读内置模板**：扫 `config/templates/*.yaml`，解析成领域对象。
   ★ 每次调用**重新读盘**，不缓存。这是 N5.2（改一行 yaml 立即生效）
   的技术保障 —— 一缓存，"热改"就变成了"改完要重启"。
2. **读自建模板**：`template` 表的行 → 领域对象。
3. **写自建模板**：`POST` 新建 / `PUT` 改（**版本自增**）。

合并策略（本卡裁决 D1）
----------------------
`list_templates()` 把两类**合并**返回，用 `source` 区分。
`template_id` 撞车时 **builtin 优先**。
**绝不把 builtin 灌进表里** —— 灌库会让 yaml 的热改失效，
而那是最难排查的一类问题（文件对、代码对、结果不对）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from mmwf.domain.template import (
    SOURCE_BUILTIN,
    SOURCE_DB,
    MinutesTemplate,
    TemplateBuiltinError,
    TemplateFormatError,
    parse_template,
    template_from_row,
)
from mmwf.infra.db import connect_ctx, transaction

logger = logging.getLogger(__name__)

__all__ = ["TemplateStore", "make_template_store"]

#: 内置模板目录名（相对 `config/`）。
TEMPLATES_SUBDIR = "templates"

#: 模板 id 前缀。生成的 id 长这样：`tpl-custom-3f2a1b9c`。
_ID_PREFIX = "tpl-custom-"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TemplateList:
    """`GET /templates` 的响应体。"""

    templates: tuple[MinutesTemplate, ...]
    builtin: int
    db: int

    @property
    def total(self) -> int:
        return len(self.templates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "templates": [t.to_dict() for t in self.templates],
            "counts": {
                "total": self.total,
                "builtin": self.builtin,
                "db": self.db,
            },
        }


class TemplateStore:
    """模板的读与写。

    与 `TranscriptStore` 不同，本类**不绑定会议**（模板是全局资源），
    所以构造时只要 `db_path` 与可选的 `config_dir`。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        config_dir: Path | str | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        #: 内置模板目录。`None` 时**只**返回表里的（便于测试）。
        self._config_dir = Path(config_dir) if config_dir is not None else None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def templates_dir(self) -> Path | None:
        if self._config_dir is None:
            return None
        return self._config_dir / TEMPLATES_SUBDIR

    # -- 读内置 -----------------------------------------------------------

    def load_builtin(self) -> tuple[list[MinutesTemplate], list[str]]:
        """扫 `config/templates/*.yaml`。

        返回 `(模板, 问题清单)`，**不抛错**：单个 yaml 坏了不该让
        `GET /templates` 整体 500 —— 其余 3 个还是好的，用户该看到它们。
        坏的那个进 `problems`，由端点放进 `warnings`。
        """
        directory = self.templates_dir
        if directory is None or not directory.is_dir():
            return [], []

        out: list[MinutesTemplate] = []
        problems: list[str] = []
        for path in sorted(directory.glob("*.yaml")):
            try:
                # ⚠️ 编码**必须**显式 utf-8。Windows 默认 GBK，
                # 中文模板名会直接乱码（同 glossary_store.read_csv 的坑）。
                raw = path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw)
            except (OSError, yaml.YAMLError) as exc:
                problems.append(f"{path.name}: 读不了或不是合法 YAML（{exc}）")
                continue
            if data is None:
                problems.append(f"{path.name}: 是空文件")
                continue
            try:
                out.append(parse_template(data, source=SOURCE_BUILTIN))
            except (TemplateFormatError, ValueError) as exc:
                problems.append(f"{path.name}: {exc}")
        return out, problems

    # -- 读表 -------------------------------------------------------------

    def list_db(
        self,
        *,
        meeting_kind: str | None = None,
        include_disabled: bool = False,
    ) -> list[MinutesTemplate]:
        sql = (
            "SELECT template_id, name, meeting_kind, schema_json, style_json,"
            " version_no, enabled, created_at FROM template"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if not include_disabled:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY template_id"

        with connect_ctx(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        out: list[MinutesTemplate] = []
        for row in rows:
            try:
                tpl = template_from_row(dict(row))
            except TemplateFormatError as exc:
                # 表里的坏行不该拖垮整个列表 —— 同上，能返回多少返回多少。
                logger.warning("模板行解析失败，已跳过：%s", exc)
                continue
            if meeting_kind is not None and tpl.meeting_kind != meeting_kind:
                continue
            out.append(tpl)
        return out

    # -- 合并 -------------------------------------------------------------

    def list_templates(
        self,
        *,
        meeting_kind: str | None = None,
        include_disabled: bool = False,
    ) -> tuple[TemplateList, list[str]]:
        """合并内置 + 自建。返回 `(结果, 问题清单)`。

        排序：**内置在前**（它们是"出厂设置"，界面上一眼看到），
        同类内按 `template_id` 稳定排序（避免列表跳动）。
        """
        builtin, problems = self.load_builtin()
        if meeting_kind is not None:
            builtin = [t for t in builtin if t.meeting_kind == meeting_kind]
        if not include_disabled:
            builtin = [t for t in builtin if t.enabled]

        db_rows = self.list_db(
            meeting_kind=meeting_kind, include_disabled=include_disabled
        )
        # ★ builtin 优先：撞车时表里的那条**被丢掉**（不是覆盖 builtin）。
        #   这样用户改了 yaml，看到的仍是 yaml 那份 —— 热改语义不破。
        shadowed = {t.template_id for t in builtin}
        db_rows = [t for t in db_rows if t.template_id not in shadowed]

        merged = sorted(builtin, key=lambda t: t.template_id) + db_rows
        return (
            TemplateList(
                templates=tuple(merged),
                builtin=len(builtin),
                db=len(db_rows),
            ),
            problems,
        )

    def get(self, template_id: str) -> MinutesTemplate | None:
        """按 id 取一个（内置优先）。找不到返回 `None`。"""
        builtin, _ = self.load_builtin()
        for t in builtin:
            if t.template_id == template_id:
                return t
        with connect_ctx(self._db_path) as conn:
            row = conn.execute(
                "SELECT template_id, name, meeting_kind, schema_json, style_json,"
                " version_no, enabled, created_at FROM template"
                " WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        if row is None:
            return None
        return template_from_row(dict(row))

    # -- 写表 -------------------------------------------------------------

    def create(
        self,
        tpl: MinutesTemplate,
        *,
        template_id: str | None = None,
    ) -> MinutesTemplate:
        """新建模板。id 已存在 → 抛 `TemplateFormatError`（E1001）。

        ⚠️ 这里**不校验"是否与内置撞车"** —— 撞车不报错，而是
        `list_templates` 里内置优先、自建的被屏蔽（见 `shadowed`）。
        为什么不报错？因为 yaml 是**可热改的外部文件**：用"表里有没有"
        来决定允不允许建，会出现"现在能建、明天 yaml 加了一个同名的就建不了"
        这种随外部文件波动而变的行为。
        """
        tid = template_id or tpl.template_id
        existing = self.get(tid)
        if existing is not None and not existing.is_builtin:
            raise TemplateFormatError(
                f"template_id={tid!r} 已存在，请换一个 id 或改用 PUT 修改",
                field="template_id",
                template_id=tid,
            )
        if existing is not None and existing.is_builtin:
            # 与内置同名 —— 允许建（会被内置屏蔽），但要如实告警。
            logger.warning(
                "新建模板 %s 与内置模板同名，列表内置优先，该条将被屏蔽", tid
            )

        now = _now_iso()
        payload = tpl.to_row_payload()
        with connect_ctx(self._db_path) as conn:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO template (template_id, name, meeting_kind,"
                    " schema_json, style_json, version_no, enabled, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tid,
                        tpl.name,
                        tpl.meeting_kind,
                        payload["schema_json"],
                        payload["style_json"],
                        1,
                        1 if tpl.enabled else 0,
                        now,
                    ),
                )
        fresh = self.get(tid)
        assert fresh is not None  # 刚插进去的
        return fresh

    def update(
        self,
        tpl: MinutesTemplate,
        *,
        template_id: str,
    ) -> MinutesTemplate:
        """改模板。

        **两条硬规则**（施工卡 §2.3）：

        ① ★ 内置模板**不许改** → `TemplateBuiltinError`（E1003）。
        ② ★ `version_no` **由服务端自增**，客户端传的一律忽略。
           F7.2 要求「修改后 version 递增，历史版本可查」。
           让客户端传 = 允许它把版本号改回去或跳号。
        """
        existing = self.get(template_id)
        if existing is None:
            raise TemplateFormatError(
                f"template_id={template_id!r} 不存在",
                field="template_id",
                template_id=template_id,
            )
        if existing.is_builtin:
            # ★ 这一步是"内置模板不可改"的**唯一执行点**。
            #   没有它，PUT 会往表里插一条与内置同名的行 ——
            #   然后 list_templates 里内置优先把它屏蔽掉，
            #   用户看到"我改了，但没生效"，比报错难查十倍。
            raise TemplateBuiltinError(template_id)

        nxt = int(existing.version_no) + 1
        now = _now_iso()
        payload = tpl.to_row_payload()
        with connect_ctx(self._db_path) as conn:
            with transaction(conn):
                cur = conn.execute(
                    "UPDATE template SET name = ?, meeting_kind = ?,"
                    " schema_json = ?, style_json = ?, version_no = ?,"
                    " enabled = ? WHERE template_id = ?",
                    (
                        tpl.name,
                        tpl.meeting_kind,
                        payload["schema_json"],
                        payload["style_json"],
                        nxt,
                        1 if tpl.enabled else 0,
                        template_id,
                    ),
                )
                if cur.rowcount == 0:
                    raise TemplateFormatError(
                        f"template_id={template_id!r} 更新时没命中任何行",
                        field="template_id",
                    )
        fresh = self.get(template_id)
        assert fresh is not None
        return fresh

    def new_template_id(self) -> str:
        """生成一个不与现有任何模板撞车的 id。

        先取现有（内置 + 表）id 集合，再随机取短串，
        撞了就重取 —— 概率极低，但**必须真的检查**，
        否则偶发的 `UNIQUE` 失败会变成"创建模板偶尔失败"的幽灵 bug。
        """
        import secrets

        taken = {t.template_id for t in self.list_templates(include_disabled=True)[0].templates}
        for _ in range(50):
            candidate = f"{_ID_PREFIX}{secrets.token_hex(4)}"
            if candidate not in taken:
                return candidate
        # 50 次都撞 —— 极不可能，说明 id 空间被占满了或前缀写错了。
        raise TemplateFormatError(
            "生成模板 id 连续碰撞 50 次，请检查现有模板数量",
            field="template_id",
        )

    def count(self, *, include_disabled: bool = True) -> int:
        return len(self.list_templates(include_disabled=include_disabled)[0].templates)


def make_template_store(
    db_path: Path | str,
    *,
    config_dir: Path | str | None = None,
) -> TemplateStore:
    """构造 `TemplateStore`。

    ⚠️ 与 `make_transcript_store` 一样，**不自动创建 AuditLogger** ——
    审计需要 `operator`（谁改的），那只有调用方（端点层）知道。
    """
    return TemplateStore(db_path, config_dir=config_dir)
