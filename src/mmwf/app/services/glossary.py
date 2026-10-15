"""术语库应用服务（T1-07）。

本模块把三样东西拼起来，并承担只有 service 该做的事：

    domain/glossary/matcher  （纯规则：索引、替换、热词导出）
    infra/glossary_store     （存取：CSV 与 glossary_entry 表）
    infra/audit              （留痕：glossary_change）

三条硬要求（都在这里落地）
--------------------------
1. **变更必须写审计**（施工卡："术语库的增删改必须写审计日志"）。
   仓储层不知道 operator 是谁，只有 service 知道。
2. **命中数写入审计 detail**（施工卡验收标准 4）。
   转写回改发生后，把"改了几处、哪些别名"记进 detail —— 
   于是"这份稿子为什么和张三的写法不一样"可以追溯到是术语库干的。
3. **热词超限告警要传出去**（施工卡验收标准 3）。
   热词超限是**静默失效**型故障，丢了告警等于用户以为热词生效了。

审计 detail 的字段约束（容易踩）
--------------------------------
`infra/audit.check_detail_safety()` 会拦两类东西：
  · 键名在黑名单里（`text`/`content`/`原文`…）—— 因为那些键天然装正文；
  · 值超过 256 字符，或值是 list/dict。
所以这里写进 detail 的只能是**计数**与**短标识**：
`{"created": 3, "alias": "中办"}` 可以，
`{"changed": [{"alias": ...}, ...]}` 不行（容器类值会被拒）。
`aliases` 这类可能很长的字段要截断后再放。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from mmwf.domain.glossary.matcher import (
    ALIAS_INDEX_TYPE,
    DEFAULT_HOTWORD_LIMIT,
    GlossaryEntry,
    HotwordExport,
    apply_corrections,
    build_alias_index,
    detect_alias_conflicts,
    export_hotwords,
)
from mmwf.infra.audit import AuditLogger
from mmwf.infra.glossary_store import GlossaryRepository, ImportReport, import_csv

logger = logging.getLogger(__name__)

#: 写进审计 detail 的别名字符串上限。audit 的硬上限是 256，
#: 这里留足余量——detail 里还有别的键，长别名列表不该把整个 detail 撑爆。
_AUDIT_TEXT_LIMIT = 60


def _clip(s: str, limit: int = _AUDIT_TEXT_LIMIT) -> str:
    """截断字符串用于审计。超长时加省略号，让阅读者知道被截了。"""
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


@dataclass
class HotwordBundle:
    """准备一次转写所需的热词包。

    比裸 `list[str]` 多带两样**必须传到用户眼前**的东西：
      · `warnings`：超限告警（静默失效型故障，丢了就没人知道）
      · `conflicts`：术语库维护冲突（同一别名指向两个规范写法）
    """

    words: list[str]
    warnings: list[str] = field(default_factory=list)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    truncated: bool = False
    total: int = 0
    limit: int = DEFAULT_HOTWORD_LIMIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.words),
            "total": self.total,
            "limit": self.limit,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "conflicts": {k: list(v) for k, v in self.conflicts.items()},
        }


@dataclass
class CorrectionResult:
    """一次术语回改的结果。"""

    text: str
    hits: int
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.hits > 0

    def to_dict(self) -> dict[str, Any]:
        return {"hits": self.hits, "changed": self.changed, "details": list(self.details)}


class GlossaryService:
    """术语库服务。

    Args:
        repo: 仓储。
        audit: 审计写入器。**可以为 None**（只读场景/测试），
            但任何写操作在没有 audit 时会打一条 warning —— 不静默放过。
        operator: 当前操作者。审计的核心是"谁做的"，所以写操作必须有它。
    """

    def __init__(
        self,
        repo: GlossaryRepository,
        *,
        audit: AuditLogger | None = None,
        operator: str = "system",
        hotword_limit: int = DEFAULT_HOTWORD_LIMIT,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._operator = operator
        self._hotword_limit = hotword_limit

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    @property
    def repo(self) -> GlossaryRepository:
        return self._repo

    def list_entries(self, *, enabled_only: bool = False) -> list[GlossaryEntry]:
        return self._repo.list_all(enabled_only=enabled_only)

    def build_index(self, *, enabled_only: bool = True) -> ALIAS_INDEX_TYPE:
        """构建别名索引（供回改用）。默认只用启用的条目（U-GL-03）。"""
        return build_alias_index(self.list_entries(enabled_only=enabled_only))

    def conflicts(self) -> dict[str, list[str]]:
        """术语库里的别名冲突（同一别名 -> 多个规范写法）。"""
        return detect_alias_conflicts(self.list_entries(enabled_only=True))

    def prepare_hotwords(self) -> HotwordBundle:
        """组装给引擎的热词包（含截断告警与冲突报告）。

        这是转写前**唯一**该调用的方法 —— 直接调 `export_hotwords`
        会丢掉 warnings，而那正是本卡要防的静默失效。
        """
        entries = self.list_entries(enabled_only=True)
        exported: HotwordExport = export_hotwords(entries, limit=self._hotword_limit)
        conflicts = detect_alias_conflicts(entries)

        warnings = list(exported.warnings)
        if conflicts:
            shown = list(conflicts)[:5]
            warnings.append(
                f"术语库有 {len(conflicts)} 个别名指向多个规范写法（{('、'.join(shown))}…），"
                "同一别名只会按先出现的那条替换。请检查术语库是否有重复录入。"
            )

        if exported.truncated:
            logger.warning(
                "热词超限：total=%d limit=%d dropped=%d",
                exported.total_before_limit, exported.limit, exported.dropped,
            )

        return HotwordBundle(
            words=list(exported.words),
            warnings=warnings,
            conflicts=conflicts,
            truncated=exported.truncated,
            total=exported.total_before_limit,
            limit=exported.limit,
        )

    # ------------------------------------------------------------------
    # 回改（转写后）
    # ------------------------------------------------------------------

    def correct_text(
        self,
        text: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        write_audit: bool = True,
    ) -> CorrectionResult:
        """把文本里的易错写法改回规范写法，并把命中数写进审计。

        审计只在**真的改了东西**时写（`hits > 0`）：
        每段都写一条"改了 0 处"会把审计刷爆，反而淹没真正的修改。
        """
        index = self.build_index()
        new_text, hits, hit_list = apply_corrections(text, index)
        details = [h.to_dict() for h in hit_list]

        if hits and write_audit:
            self._log_change(
                action_detail={
                    "op": "correct",
                    "hits": hits,
                    "aliases": _clip("、".join(h.alias for h in hit_list)),
                    "entries": len(hit_list),
                    "chars_before": len(text),
                    "chars_after": len(new_text),
                },
                target_type=target_type,
                target_id=target_id,
            )

        return CorrectionResult(text=new_text, hits=hits, details=details)

    # ------------------------------------------------------------------
    # 写（全部写审计）
    # ------------------------------------------------------------------

    def upsert(
        self,
        entry: GlossaryEntry,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> str:
        """新增或更新一条术语。返回 `"created"` / `"updated"`。"""
        before = self._repo.get(entry.entry_id)
        action = self._repo.upsert(entry)
        self._log_change(
            action_detail={
                "op": action,
                "category": entry.category,
                "canonical": _clip(entry.canonical),
                "aliases": _clip("、".join(entry.aliases)),
                "alias_count": len(entry.aliases),
                "enabled": entry.enabled,
                # 更新时记下"改之前是什么样的"，否则事后无法判断这次改动做了什么
                "before_aliases": _clip("、".join(before.aliases)) if before else "",
            },
            target_type=target_type or "glossary",
            target_id=target_id or entry.entry_id,
        )
        return action

    def set_enabled(
        self,
        entry_id: str,
        enabled: bool,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> bool:
        """启用/停用。返回是否找到条目。"""
        found = self._repo.set_enabled(entry_id, enabled)
        if not found:
            return False
        entry = self._repo.get(entry_id)
        self._log_change(
            action_detail={
                "op": "enable" if enabled else "disable",
                "category": entry.category if entry else "",
                "canonical": _clip(entry.canonical) if entry else "",
            },
            target_type=target_type or "glossary",
            target_id=target_id or entry_id,
        )
        return True

    def delete(
        self,
        entry_id: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> bool:
        """删除一条。返回是否找到条目。

        删除前先取内容写进审计 —— 删了之后就再也查不到"删的是什么"了。
        这是删除类审计的通用要求。
        """
        entry = self._repo.get(entry_id)
        found = self._repo.delete(entry_id)
        if not found:
            return False
        self._log_change(
            action_detail={
                "op": "delete",
                "category": entry.category if entry else "",
                "canonical": _clip(entry.canonical) if entry else "",
                "aliases": _clip("、".join(entry.aliases)) if entry else "",
            },
            target_type=target_type or "glossary",
            target_id=target_id or entry_id,
        )
        return True

    def import_file(self, path: Path | str) -> ImportReport:
        """导入 CSV（F7.1 的批量导入）。写一条审计。"""
        report = import_csv(self._repo, path)
        self._log_change(
            action_detail={
                "op": "import",
                "source": _clip(Path(path).name),
                "created": report.created,
                "updated": report.updated,
                "problems": len(report.problems),
            },
            target_type="glossary_file",
            target_id=Path(path).name,
        )
        return report

    def export_file(self, path: Path | str) -> Path:
        """导出当前库为 CSV（备份/交付用）。写一条审计。"""
        from mmwf.infra.glossary_store import write_csv

        entries = self.list_entries()
        out = write_csv(path, entries)
        self._log_change(
            action_detail={
                "op": "export",
                "target": _clip(Path(path).name),
                "count": len(entries),
            },
            target_type="glossary_file",
            target_id=Path(path).name,
        )
        return out

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _log_change(
        self,
        *,
        action_detail: dict[str, Any],
        target_type: str | None,
        target_id: str | None,
    ) -> None:
        """写 `glossary_change` 审计。

        没有 audit 时**打 warning 而不是静默跳过**：
        "术语库变更没留痕"是合规问题（部署手册 §6.2 的 8 类核查项之一），
        静默跳过会让问题在验收时才暴露。
        """
        if self._audit is None:
            logger.warning(
                "术语库变更未写审计（未注入 AuditLogger）：%s", action_detail
            )
            return
        self._audit.log(
            operator=self._operator,
            action="glossary_change",
            target_type=target_type,
            target_id=target_id,
            detail=action_detail,
        )


def make_glossary_service(
    settings: object | None = None,
    *,
    db_path: Path | str | None = None,
    repo: GlossaryRepository | None = None,
    audit: AuditLogger | None = None,
    operator: str = "system",
) -> GlossaryService:
    """按配置构造 service（推荐入口）。

    热词上限的取值链路：`settings.extraction.glossary.hotword_limit`
    → 施工卡默认 2000。允许配置覆盖是为了换引擎版本时能按新引擎的
    实际上限调整（N5.2 可热改），而默认值保证"不改配置也能跑对"。

    这里**逐层 getattr** 而不是直接 `settings.extraction.glossary.hotword_limit`：
    service 层不该硬依赖 Settings 的具体形状（测试里塞一个最小桩对象
    就够了），缺哪一层就退回默认值，不抛异常。
    """
    limit = DEFAULT_HOTWORD_LIMIT
    if settings is not None:
        ex = getattr(settings, "extraction", None)
        gl = getattr(ex, "glossary", None) if ex is not None else None
        # 兼容旧写法：允许 hotword_limit 直接挂在 extraction 下
        limit = getattr(gl, "hotword_limit", None) or getattr(
            ex, "hotword_limit", None
        ) or limit

    if repo is None:
        if db_path is None:
            if settings is None:
                raise ValueError("需要 settings 与 db_path 中的至少一个")
            data_dir = settings.resolve_path(settings.app.data_dir)  # type: ignore[attr-defined]
            db_path = Path(data_dir) / "mmwf.db"
        repo = GlossaryRepository(db_path)

    return GlossaryService(
        repo, audit=audit, operator=operator, hotword_limit=limit
    )
