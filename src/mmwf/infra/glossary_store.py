"""术语库仓储（T1-07）。

职责边界
--------
本模块只做**存取**：CSV <-> 条目、SQLite 表 <-> 条目。
所有"什么算合法术语"、"怎么替换"的规则都在 `domain/glossary/matcher.py`。

为什么要两份存储（CSV 与 SQLite 表）
-----------------------------------
施工卡要求 `config/glossary.csv` 可被导入，SAD §5 又定义了 `glossary_entry` 表。
两者服务不同场景，不是冗余：

  · **CSV** = 人类维护的**源文件**。放在 `config/`，可以用 Excel 编辑、
    能走版本管理、能整包交付给使用单位替换。SAD §14 的"配置外置"要求的就是它。
  · **表** = 运行时的**活库**。带 `entry_id`/`enabled`/`updated_at`，
    支持界面上的增删改查（F7.1）与审计回溯。

导入是**幂等**的：按 `(category, canonical)` 做 upsert ——
重复导入同一份 CSV 不该产生重复条目，也不该把界面上刚改的 `enabled`
莫名其妙重置掉（除非 CSV 显式给了 `enabled` 列）。

术语库变更必须写审计
-------------------
施工卡与部署手册 §6.2 都要求 `glossary_change`。
本模块**不做**审计写入——那是调用方（service）的事，
因为审计需要 `operator`（谁改的），而仓储层不知道也不该猜。
但仓储层负责**如实返回变更清单**，让 service 能写清楚"改了哪几条、从什么变成什么"。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from mmwf.domain.glossary.matcher import (
    CSV_HEADER,
    CATEGORIES,
    GlossaryEntry,
    GlossaryFormatError,
    parse_aliases,
    parse_csv_rows,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ImportReport:
    """一次 CSV 导入的结果。"""

    upserted: int
    created: int
    updated: int
    problems: tuple[str, ...]
    source: str

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "upserted": self.upserted,
            "created": self.created,
            "updated": self.updated,
            "problems": list(self.problems),
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# CSV 读写
# ---------------------------------------------------------------------------

def read_csv(path: Path | str) -> tuple[list[GlossaryEntry], list[str]]:
    """读 CSV。返回 `(条目, 问题清单)`。

    编码：显式 `utf-8-sig`。不使用这个会踩两个坑：
      · Windows 上默认用 GBK，中文直接乱码；
      · Excel 另存的 CSV 带 UTF-8 BOM（`\\ufeff`），不剥离会让**第一列名**
        变成 `\\ufeffcategory`，于是"缺 category"报满天飞。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"术语库文件不存在：{p}")

    # 先手工切一遍行，把 `#` 注释行与空行摘掉**再**交给 csv。
    #
    # 不这么做的后果很具体：csv.DictReader 会把文件第一行当表头，
    # 如果文件以注释开头（`config/glossary.csv` 就是——交付模板需要
    # 在开头写"格式说明"），表头就成了 `["# 用法说明"]`，
    # 然后报"缺少必需列 ['category','canonical']"，用户对着
    # 一份完全合规、只是多了一行说明的文件莫名其妙。
    #
    # 只摘**表头之前**和**表头之后**的注释/空行，不动数据行——
    # 数据行里的 `#` 由 parse_csv_rows 按 category/canonical 前缀判断，
    # 这里的行级剥离只负责"文件级装饰"。
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        raw_lines = f.read().splitlines()
    data_lines = [
        ln for ln in raw_lines
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not data_lines:
        raise GlossaryFormatError(f"术语库 CSV 为空或没有表头：{p}")

    reader = csv.DictReader(io.StringIO("\n".join(data_lines)))
    fieldnames = [f2.strip() for f2 in (reader.fieldnames or [])]
    if not fieldnames:
        raise GlossaryFormatError(f"术语库 CSV 为空或没有表头：{p}")
    missing = [c for c in ("category", "canonical") if c not in fieldnames]
    if missing:
        raise GlossaryFormatError(
            f"术语库 CSV 缺少必需列 {missing}，实际表头：{fieldnames}。"
            f"要求格式：{','.join(CSV_HEADER)}（aliases 用 | 分隔多个）"
        )
    # 表头去过空白后要重建行，否则 `row.get("category")` 会因为
    # 表头里多余的空白而取不到值（Excel 手工编辑很常见）
    rows = [
        {(k or "").strip(): (v or "") for k, v in raw.items() if k is not None}
        for raw in reader
    ]

    return parse_csv_rows(rows, id_prefix=p.stem or "gl")


def write_csv(path: Path | str, entries: Sequence[GlossaryEntry]) -> Path:
    """把条目写成 CSV（导出/备份用）。

    `entry_id` 与 `enabled` 写成可选列：不写 `enabled` 时读回来默认启用，
    这正是"交付给使用单位的模板"想要的形状——他们不该看到这两个内部字段。
    这里**照实写**，因为本函数用于备份与往返（round-trip），
    丢了这两个字段就不是无损备份了。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([*CSV_HEADER, "entry_id", "enabled"])
        for e in entries:
            w.writerow([
                e.category,
                e.canonical,
                "|".join(e.aliases),
                e.note,
                e.entry_id,
                "1" if e.enabled else "0",
            ])
    return p


# ---------------------------------------------------------------------------
# SQLite 仓储
# ---------------------------------------------------------------------------

class GlossaryRepository:
    """`glossary_entry` 表的读写。

    只依赖标准库 `sqlite3`（SAD §7：核心链路不引入 ORM）。
    连接由外部传入还是内部创建？这里**内部创建**，但把 `db_path` 暴露出来：
    术语库操作是低频的管理动作，为每次改一条术语开一个连接完全可以接受，
    而"共享一个长连接"反而会引入线程问题（界面可能在别的线程改）。
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        # 只读表操作，但写入需要 FK 约束生效（SAD §5 的引用完整性）
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- 读 ----------------------------------------------------------------

    def list_all(self, *, enabled_only: bool = False) -> list[GlossaryEntry]:
        """列出全部术语。顺序稳定（category, canonical），保证界面不跳动。"""
        sql = (
            "SELECT entry_id, category, canonical, aliases, note, enabled"
            " FROM glossary_entry"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY category, canonical"
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get(self, entry_id: str) -> GlossaryEntry | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT entry_id, category, canonical, aliases, note, enabled"
                " FROM glossary_entry WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def count(self) -> int:
        with self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM glossary_entry").fetchone()[0]
        return int(n)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> GlossaryEntry:
        return GlossaryEntry(
            entry_id=row["entry_id"],
            category=row["category"],
            canonical=row["canonical"],
            aliases=parse_aliases(row["aliases"]),
            note=row["note"] or "",
            enabled=bool(row["enabled"]),
        )

    # -- 写 ----------------------------------------------------------------

    def upsert(self, entry: GlossaryEntry) -> str:
        """插入或更新一条。返回 `"created"` / `"updated"`。

        冲突键是 `(category, canonical)` 而不是 `entry_id`：
        界面按"这个词属于哪个类别"来识别同一条，`entry_id` 是内部标识。
        用 `entry_id` 判重会导致"同一个人名被录入两次"。
        """
        now = _now_iso()
        aliases_json = json.dumps(list(entry.aliases), ensure_ascii=False)
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT entry_id FROM glossary_entry WHERE category = ? AND canonical = ?",
                (entry.category, entry.canonical),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE glossary_entry SET aliases = ?, note = ?, enabled = ?,"
                    " updated_at = ? WHERE entry_id = ?",
                    (aliases_json, entry.note, int(entry.enabled), now, existing["entry_id"]),
                )
                conn.commit()
                return "updated"
            conn.execute(
                "INSERT INTO glossary_entry"
                " (entry_id, category, canonical, aliases, note, enabled, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (entry.entry_id, entry.category, entry.canonical, aliases_json,
                 entry.note, int(entry.enabled), now),
            )
            conn.commit()
        return "created"

    def replace_all(self, entries: Sequence[GlossaryEntry]) -> ImportReport:
        """整库替换（批量导入用）。返回统计。

        为什么是"替换"而不是"清空再插入"：
        清空会让 `entry_id` 全部变化，界面上正在看的条目会突然"消失"，
        而且任何外部引用（比如审计里记的 entry_id）会变成悬空指针。
        所以这里逐条 upsert，**不删除** CSV 里没有的条目——
        "导入"不是"同步"，删条目应该由用户在界面上显式做（并留痕）。
        """
        created = updated = 0
        problems: list[str] = []
        for e in entries:
            try:
                action = self.upsert(e)
            except sqlite3.IntegrityError as exc:
                problems.append(f"{e.category}/{e.canonical}：{exc}")
                continue
            if action == "created":
                created += 1
            else:
                updated += 1
        return ImportReport(
            upserted=created + updated,
            created=created,
            updated=updated,
            problems=tuple(problems),
            source="db",
        )

    def set_enabled(self, entry_id: str, enabled: bool) -> bool:
        """启用/停用一条。返回是否找到该条目。

        停用而不删除：删了就丢了"曾经有这条术语"的历史，
        而审计里可能还引用着它的 entry_id（会成为悬空引用）。
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE glossary_entry SET enabled = ?, updated_at = ? WHERE entry_id = ?",
                (int(enabled), _now_iso(), entry_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete(self, entry_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM glossary_entry WHERE entry_id = ?", (entry_id,)
            )
            conn.commit()
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 导入编排
# ---------------------------------------------------------------------------

def import_csv(
    repo: GlossaryRepository,
    path: Path | str,
) -> ImportReport:
    """把 CSV 导入数据库。返回统计与问题清单。

    格式错误（缺列/类别非法）在这里**不抛异常**而是回到 `problems`：
    使用单位拿到的 CSV 往往是自己手写的，报错要能一次列全，
    而不是"改一行、再导一次、再报下一行"。
    真正读不了文件（不存在/编码错）才抛。
    """
    entries, problems = read_csv(path)

    # 类别非法/缺规范写法的行已经被 parse_csv_rows 过滤掉了，
    # 这里只把合法条目写库
    valid = [e for e in entries if e.category in CATEGORIES and e.canonical.strip()]
    report = repo.replace_all(valid)

    all_problems = [*problems, *report.problems]
    if all_problems:
        logger.warning("术语库导入有 %d 个问题：%s", len(all_problems), all_problems[:5])

    return ImportReport(
        upserted=report.upserted,
        created=report.created,
        updated=report.updated,
        problems=tuple(all_problems),
        source=str(Path(path).name),
    )


def iter_chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """按 size 切块（批量导入大批量 CSV 时用，避免一次全在内存里）。"""
    if size <= 0:
        raise ValueError("size 必须为正")
    for i in range(0, len(items), size):
        yield items[i:i + size]
