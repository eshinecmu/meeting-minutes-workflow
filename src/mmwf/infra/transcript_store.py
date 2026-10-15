"""转写分段仓储（T1-14，C 档地基）。

本模块是 `transcript_segment` 表的**唯一**读写入口，封装四件事：

  1. 按会议列出/查询分段（含置信度与异常标记）
  2. 取单段
  3. 就地编辑某段（F2.2 逐段校对的写入侧）
  4. 批量接受高置信分段（F2.2 `review/accept`）

为什么本卡先建仓储、不接端点
----------------------------
SAD §9.1 里 `/meetings/{id}/transcript`、`PATCH /segments/{id}`、
`/meetings/{id}/review/accept`、`POST /meetings/{id}/audio` 这四个端点
属于**C 档**（需要复核语义），本卡（T1-14）不接。

但**仓储层必须先建**，原因不是"顺手"：

    API 层**禁止** import `sqlite3`（`tools/check_layers.py` 的
    `FORBIDDEN_IN_API`）。所以端点**不可能**自己去查这个表 ——
    没有仓储层，C 档那四个端点就无处落脚。

也就是说：本模块是 C 档的**硬前置**，早建一层比在端点里
绕过分层检查（那会让 C01 失守）要正确得多。

与 `glossary_store.py` 的分工差异
---------------------------------
两者都是仓储，但**写入语义完全不同**，不能照抄：

| | `GlossaryRepository` | 本模块 |
|---|---|---|
| 写入对象 | 管理员维护的**长期资产**（术语） | 引擎产物 + 人工校订的**过程数据** |
| 写策略 | `upsert`（按 `(category,canonical)` 幂等） | **行级 update**（按 `segment_id` 定位） |
| 版本 | 不记版本 | **`version` 递增**（每次人工改动 +1） |
| 审计 | 由 service 层 `_log_change` | **由本仓储直接写**（见下） |

**为什么本模块直接写审计，而 glossary 由 service 写？**

`segment_edit` 是 `MINIMUM_CHECK_ITEM_ACTIONS` 里"文稿修改记录"一类的
唯一动作之一（与 `record_generate` 并列），AUD-08 要求它**必须留痕**。
而 `AuditLogger` 已经提供了 `segment_edit()` 这个专用方法
（`infra/audit.py:716`），它内部已经处理好了"只记 id、不记正文"（AUD-05）。

把它放在仓储层的理由：**"改了一行"和"记了一条改动"是同一件事的两半**，
拆到两层去写，中间任何一个早退都会造成"改了但没记"——这正是审计最怕的
缺口。放在一起，`edit_segment()` 一个方法调用就同时完成两件事。

⚠️ 但只有**人工编辑**才写审计。`reviewed` 状态位由批量接受操作带动，
那一条审计由 `accept_high_confidence()` 统一写（否则批量接受 300 段
会写 300 条审计，审计日志会被淹没）。

`flags` 与 `aliases` 的同一类坑
-------------------------------
`transcript_segment.flags` 是 JSON 数组的 TEXT 列（001_init.sql:71）。
读写时**必然**要 json 序列化/反序列化，而两边的容错策略必须一致：

  · 读：坏 JSON **不抛**，退回空元组 + 打 warning。
    理由与 `audit.detail_from_json` 相同——不要因为一行坏数据
    就让整个复核页打不开。
  · 写：**必须** `ensure_ascii=False`。用默认的 `ensure_ascii=True`
    会把中文 flag（如"静音"）写成 `\\u9759\\u97f3`，
    界面直接显示乱码。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from mmwf.domain.errors import InputError
from mmwf.infra.db import connect_ctx, transaction

logger = logging.getLogger(__name__)

#: 低置信默认阈值。
#:
#: ⚠️ **只作为"没传阈值时的兜底"**，绝不在模块内部硬编码判定。
#: 理由见 `TranscriptSegmentResult.is_low_confidence` 的 docstring：
#: 阈值必须由调用方传入（配置可热改，N5.2），否则改配置不生效。
DEFAULT_LOW_CONFIDENCE = 0.7

#: `review/accept` 要接受的置信度下限：**阈值以上才算"可以批量接受"**。
ACCEPT_MIN_CONFIDENCE = 0.85


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentRow:
    """`transcript_segment` 一行的快照。

    `confidence=None` 表示**引擎没给置信度**，不是 0——
    这个区分见 `domain/asr_types.py` 的 `TranscriptSegmentResult` 注释，
    仓储层只负责**如实搬运**，不做任何填充。
    """

    segment_id: str
    meeting_id: str
    seq: int
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    speaker: str | None = None
    speaker_conf: float | None = None
    flags: tuple[str, ...] = ()
    reviewed: bool = False
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    version: int = 1
    audio_id: str | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def review_flags(self) -> tuple[str, ...]:
        """需要人工看的标记：低置信 + 任一异常 flag。

        与 `domain/review/split.py` 的分流判据同源——**两条独立判据**：
        分数低要看，被标了 `silence`/`overlap` 的即使分数很高也要看。
        这里把两者合并成一个"要不要看"的视图，供界面直接用。
        """
        out: list[str] = []
        if self.confidence is not None and self.confidence < DEFAULT_LOW_CONFIDENCE:
            out.append("low_conf")
        out.extend(f for f in self.flags if f != "low_conf")
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "meeting_id": self.meeting_id,
            "audio_id": self.audio_id,
            "seq": self.seq,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "text": self.text,
            "confidence": self.confidence,
            "speaker": self.speaker,
            "speaker_conf": self.speaker_conf,
            "flags": list(self.flags),
            "review_flags": list(self.review_flags),
            "reviewed": self.reviewed,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "version": self.version,
        }


@dataclass
class EditResult:
    """一次就地编辑的结果。

    `changed=False` 是**正常返回**而不是错误：用户点开一段、看了一眼、
    原文没动就保存，是极常见的操作。把它当错误会让界面弹红框。
    """

    segment: SegmentRow
    changed: bool
    before_text: str = ""
    version: int = 1
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment.to_dict(),
            "changed": self.changed,
            "before_text": self.before_text,
            "version": self.version,
            "warnings": list(self.warnings),
        }


@dataclass
class AcceptResult:
    """批量接受高置信分段的结果。"""

    accepted: int
    skipped_reviewed: int
    skipped_low_confidence: int
    segment_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "skipped_reviewed": self.skipped_reviewed,
            "skipped_low_confidence": self.skipped_low_confidence,
            "segment_ids": list(self.segment_ids),
        }


# ---------------------------------------------------------------------------
# flags 的序列化（读写容错必须对称）
# ---------------------------------------------------------------------------

def _parse_flags(raw: str | None) -> tuple[str, ...]:
    """读 flags。坏 JSON 不抛，退回空元组。

    与 `audit.detail_from_json` 的策略一致：一行坏数据不该让整个
    复核页打不开。但**要打 warning** —— 静默吞掉坏数据会让"为什么
    这一段没有标记"永远查不出来。
    """
    if not raw:
        return ()
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("transcript_segment.flags 不是合法 JSON，按无标记处理：%r", raw[:80])
        return ()
    if isinstance(obj, str):
        # 容忍有人手工存了 `"low_conf"` 而不是 `["low_conf"]`
        return (obj,) if obj else ()
    if isinstance(obj, list):
        return tuple(str(x) for x in obj if str(x).strip())
    logger.warning("transcript_segment.flags 不是数组，按无标记处理：%r", obj)
    return ()


def _dump_flags(flags: Iterable[str] | None) -> str | None:
    """写 flags。空集合存 NULL（而不是 `[]`）—— 与 001_init.sql 的可空语义一致。

    ★ T1-15 Review 补的**第 21 个缺陷**，而且是个很阴的：

    第一版直接 `[str(f) for f in flags if str(f).strip()]`。
    而**字符串是可迭代的** —— 传 `flags="silence"`（用户以为"给个标记名"
    就够了）会静默存成 `["s","i","l","e","n","c","e"]`：

      · **不报错**：字符串满足 `Iterable[str]`，类型注解也拦不住运行时；
      · **不告警**：`_dump_flags` 只负责序列化，不知道什么是合法标记；
      · **后果**：那一行的 flags 从此谁也不匹配 —— `REVIEW_FLAGS`
        （`silence`/`overlap`）里没有单个字母，于是"两人抢话"的分段
        在复核页上**看起来干干净净**，直接被放过。

    这与 T1-14 的坑 12 是**同一个形状**（`parse_aliases({'a':1})` 静默
    迭代出 `('a',)`），只是换了一个函数。所以修法也照抄那个的结论：
    **用白名单（只接受明确的序列类型），不要用黑名单（排除字符串）**。
    黑名单在这个场景下必漏：`bytes` 可迭代、`dict` 可迭代（且迭代出 key，
    比字符串更隐蔽）、生成器也可以。

    ⚠️ 顺序很重要：**先判类型，再判空，最后才构造**。空集合要在
    类型检查之后处理（`()` / `[]` 都是合法序列，只是"清空"）。

    抛 `InputError`（E1001，是 `MmwfError` 子类）而**不是** `ValueError`：
    后者会掉进 API 的全局兜底处理器变成 **E9001**，把"用户传错了"
    报成"系统故障"—— T1-14 的坑 10 就是这个形状。
    """
    if not flags:
        return None
    if isinstance(flags, str):
        raise InputError(
            code="E1001",
            detail=f"flags 不能是字符串：{flags!r}",
            user_message=(
                "分段标记要填一个数组（比如 [\"overlap\"]），不能直接填一个词。"
                "不填表示不改动原有标记。"
            ),
            context={"area": "transcript", "field": "flags"},
        )
    if not isinstance(flags, (list, tuple, set, frozenset)):
        raise InputError(
            code="E1001",
            detail=f"flags 类型不支持：{type(flags).__name__}",
            user_message="分段标记要填一个数组（比如 [\"overlap\"]）。",
            context={"area": "transcript", "field": "flags"},
        )
    seq = [str(f) for f in flags if str(f).strip()]
    if not seq:
        return None
    # ensure_ascii=False：中文 flag 必须原样存，否则界面乱码
    return json.dumps(seq, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------

class TranscriptStore:
    """`transcript_segment` 表的读写。

    连接策略与 `GlossaryRepository` 一致：**每次操作新建连接**（走
    `infra/db.connect_ctx`，PRAGMA 由那里统一施加）。理由相同 ——
    复核页的写入可能来自不同线程，共享长连接会踩
    `SQLite objects created in a thread...`；而这是低频人工操作，
    每次开连接的开销可以忽略。
    """

    def __init__(
        self,
        meeting_id: str,
        db_path: Path | str,
        *,
        audit: Any = None,
    ) -> None:
        if not meeting_id:
            raise ValueError("meeting_id 不能为空")
        self._meeting_id = meeting_id
        self._db_path = Path(db_path)
        #: `AuditLogger`。**可以为 None**（只读场景/单测），
        #: 但写操作在没有它时会打 warning —— 不静默放过。
        self._audit = audit

    @property
    def meeting_id(self) -> str:
        return self._meeting_id

    @property
    def db_path(self) -> Path:
        return self._db_path

    # -- 读 ----------------------------------------------------------------

    def list_segments(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        min_seq: int | None = None,
        max_seq: int | None = None,
    ) -> list[SegmentRow]:
        """按 `seq` 升序列出分段。

        ⚠️ `limit=None` 时**不加 LIMIT，返回全部** —— 这是刻意的：
        "看转写稿"就是要看全篇。但调用方（端点）必须自己决定是否
        加一个上限并**告知用户已截断**，不要让用户以为看到的是全部。
        """
        sql = (
            "SELECT segment_id, meeting_id, audio_id, seq, start_ms, end_ms, text,"
            " confidence, speaker, speaker_conf, flags, reviewed, reviewed_by,"
            " reviewed_at, version FROM transcript_segment WHERE meeting_id = ?"
        )
        params: list[Any] = [self._meeting_id]
        if min_seq is not None:
            sql += " AND seq >= ?"
            params.append(int(min_seq))
        if max_seq is not None:
            sql += " AND seq <= ?"
            params.append(int(max_seq))
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([max(1, int(limit)), max(0, int(offset))])

        with connect_ctx(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_segment(r) for r in rows]

    def get_segment(self, segment_id: str) -> SegmentRow | None:
        """取单段。**必须同时校验 meeting_id** —— 见下方 docstring。"""
        with connect_ctx(self._db_path) as conn:
            row = conn.execute(
                "SELECT segment_id, meeting_id, audio_id, seq, start_ms, end_ms, text,"
                " confidence, speaker, speaker_conf, flags, reviewed, reviewed_by,"
                " reviewed_at, version FROM transcript_segment"
                " WHERE segment_id = ? AND meeting_id = ?",
                (segment_id, self._meeting_id),
            ).fetchone()
        return _row_to_segment(row) if row is not None else None

    def count(self) -> int:
        with connect_ctx(self._db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM transcript_segment WHERE meeting_id = ?",
                (self._meeting_id,),
            ).fetchone()[0]
        return int(n)

    def counts_by_review_state(self) -> dict[str, int]:
        """复核进度统计（供界面显示"还有多少段没看"）。

        `no_confidence` 单列：引擎没给置信度的段**无法用分数分流**，
        必须全部人工过（见 `TranscriptSegmentResult` 的注释）。
        把它混进 `low_confidence` 会让"低置信"这个数虚高、失去意义。
        """
        with connect_ctx(self._db_path) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM transcript_segment WHERE meeting_id = ?",
                    (self._meeting_id,),
                ).fetchone()[0]
            )
            reviewed = int(
                conn.execute(
                    "SELECT COUNT(*) FROM transcript_segment"
                    " WHERE meeting_id = ? AND reviewed = 1",
                    (self._meeting_id,),
                ).fetchone()[0]
            )
            low = int(
                conn.execute(
                    "SELECT COUNT(*) FROM transcript_segment"
                    " WHERE meeting_id = ? AND confidence IS NOT NULL"
                    "   AND confidence < ?",
                    (self._meeting_id, DEFAULT_LOW_CONFIDENCE),
                ).fetchone()[0]
            )
            no_conf = int(
                conn.execute(
                    "SELECT COUNT(*) FROM transcript_segment"
                    " WHERE meeting_id = ? AND confidence IS NULL",
                    (self._meeting_id,),
                ).fetchone()[0]
            )
        return {
            "total": total,
            "reviewed": reviewed,
            "pending": total - reviewed,
            "low_confidence": low,
            "no_confidence": no_conf,
        }

    # -- 写（人工编辑）------------------------------------------------------

    def edit_segment(
        self,
        segment_id: str,
        text: str,
        *,
        operator: str = "system",
        flags: Sequence[str] | None = None,
        now: str | None = None,
    ) -> EditResult:
        """就地编辑某段（F2.2）。

        **一次事务里做三件事**：改 text、`version += 1`、标记 `reviewed`。
        拆开做的话，任何一步失败都会留下"文本改了但版本没动"这类
        不可检测的中间态。

        审计（`segment_edit`）在**提交之后**写。顺序很重要：
        如果先写审计再改库，改库失败就会留下一条"改过了"的假记录；
        反过来（本实现）最坏情况是"改了但没记上"，而 `AuditLogger.log`
        本身极少失败，且失败会抛出来让调用方知道。

        ⚠️ **无论正文是否变化都会写一条审计**（T1-15 修正）。
        这个动作即使"一字未改"也真的改了 `version` / `reviewed` /
        `reviewed_by`，而且"我确认过这一段没问题"本身就是最该留痕的
        判断。详见 `_log_edit` 的调用处注释。

        Args:
            text: 新的正文。**空字符串不合法** —— 逐段校对是"改错字"，
                不是"删段落"。删整段是另一个动作（本卡不做）。
            flags: 若给定则**整体替换** flags；`None` 表示不动。
        """
        if not text or not text.strip():
            raise ValueError("分段正文不能为空（逐段校对是改错字，不是删段落）")

        ts = now or _now_iso()
        with connect_ctx(self._db_path) as conn:
            row = conn.execute(
                "SELECT text, version FROM transcript_segment"
                " WHERE segment_id = ? AND meeting_id = ?",
                (segment_id, self._meeting_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"segment_id={segment_id!r} 不存在于会议 {self._meeting_id!r}")

            before_text = str(row["text"])
            version = int(row["version"]) + 1

            sets = [
                "text = mmwf_encrypt(?)",
                "version = ?",
                "reviewed = 1",
                "reviewed_by = ?",
                "reviewed_at = ?",
            ]
            params: list[Any] = [text, version, operator, ts]
            if flags is not None:
                sets.append("flags = ?")
                params.append(_dump_flags(flags))
            params.extend([segment_id, self._meeting_id])

            with transaction(conn):
                conn.execute(
                    f"UPDATE transcript_segment SET {', '.join(sets)}"
                    " WHERE segment_id = ? AND meeting_id = ?",
                    params,
                )

            fresh = conn.execute(
                "SELECT segment_id, meeting_id, audio_id, seq, start_ms, end_ms, text,"
                " confidence, speaker, speaker_conf, flags, reviewed, reviewed_by,"
                " reviewed_at, version FROM transcript_segment"
                " WHERE segment_id = ? AND meeting_id = ?",
                (segment_id, self._meeting_id),
            ).fetchone()

        changed = before_text != text
        # ★ T1-15 Review 补的**第 22 个缺陷**：这里原来写的是
        #     if changed: self._log_edit(...)
        # 但紧接着返回的 `warnings` 文案是
        #     「正文没有变化，**仍记了一次校订**」
        # —— **自相矛盾**：说"记了"，实际没记。
        #
        # 谁错了？文案是对的，条件错了。理由有三条：
        #   ① `edit_segment` 无论内容是否变化，都**真的改了三样东西**：
        #      `version += 1`、`reviewed = 1`、`reviewed_by/at`。
        #      也就是说"无改动保存"**仍然是一次真实的写操作**，
        #      审计的职责就是记录写操作；
        #   ② 这个动作有实际的业务含义：用户点了"确认这段没问题"。
        #      "我确认过第 37 段"恰恰是复核流程最需要留痕的事 ——
        #      比"我改了一个错字"更需要留痕（前者是判断，后者是纠错）；
        #   ③ AUD-08 的最低核查项关心的是"逐段校对有没有被复核"。
        #      如果只有改动才留痕，那"全部复核完但一字未改"的会议
        #      在审计里**看起来像没复核过**。
        #
        # `changed` 字段仍然如实反映"正文有没有变"（前端据此显示
        # 「已保存」还是「正文没有变化」），它与"要不要写审计"是
        # **两件事**，第一版把它们绑在了一起。
        self._log_edit(
            segment_id=segment_id,
            operator=operator,
            before_text=before_text,
            after_text=text,
            version=version,
            changed=changed,
        )
        return EditResult(
            segment=_row_to_segment(fresh),
            changed=changed,
            before_text=before_text,
            version=version,
            warnings=[] if changed else ["正文没有变化，仍记了一次校订"],
        )

    def accept_high_confidence(
        self,
        *,
        operator: str = "system",
        threshold: float = ACCEPT_MIN_CONFIDENCE,
        segment_ids: Sequence[str] | None = None,
        now: str | None = None,
    ) -> AcceptResult:
        """批量接受高置信分段（F2.2）。

        **两条判据都要满足**才接受：

          ① `confidence >= threshold`（分数够高）
          ② `flags` 里**不含**异常标记（`silence` / `overlap` / `low_conf`）

        只看 ① 是错的 —— 引擎对"两人抢话"往往给出很高置信度
        （它很确定自己听见了字），但内容是叠在一起的废数据。
        这正是 `flags` 存在的理由（见 `asr_types.py`）。

        **只写一条审计**（不是每段一条）。批量接受 300 段写 300 条
        `segment_edit` 会把审计日志冲垮，反而让真正需要人工留意的
        逐段改动淹没在噪音里。

        Args:
            segment_ids: 限定要处理的分段；`None` 表示整篇。
        """
        ts = now or _now_iso()
        with connect_ctx(self._db_path) as conn:
            sql = (
                "SELECT segment_id, confidence, flags, reviewed FROM transcript_segment"
                " WHERE meeting_id = ?"
            )
            params: list[Any] = [self._meeting_id]
            if segment_ids is not None:
                ids = list(segment_ids)
                if not ids:
                    return AcceptResult(0, 0, 0, [])
                placeholders = ",".join("?" for _ in ids)
                sql += f" AND segment_id IN ({placeholders})"
                params.extend(ids)

            rows = conn.execute(sql, params).fetchall()

            accepted: list[str] = []
            skipped_reviewed = 0
            skipped_low = 0
            for r in rows:
                if int(r["reviewed"] or 0) == 1:
                    skipped_reviewed += 1
                    continue
                conf = r["confidence"]
                if conf is None or float(conf) < float(threshold):
                    skipped_low += 1
                    continue
                if _parse_flags(r["flags"]):
                    # 有异常标记 → 内容不可用，即使分数高也送人工
                    skipped_low += 1
                    continue
                accepted.append(str(r["segment_id"]))

            if accepted:
                placeholders = ",".join("?" for _ in accepted)
                with transaction(conn):
                    conn.execute(
                        "UPDATE transcript_segment SET reviewed = 1,"
                        " reviewed_by = ?, reviewed_at = ?"
                        f" WHERE meeting_id = ? AND segment_id IN ({placeholders})",
                        [operator, ts, self._meeting_id, *accepted],
                    )

        if accepted:
            self._log_accept(
                operator=operator,
                accepted=len(accepted),
                skipped_reviewed=skipped_reviewed,
                skipped_low=skipped_low,
                threshold=threshold,
            )
        return AcceptResult(
            accepted=len(accepted),
            skipped_reviewed=skipped_reviewed,
            skipped_low_confidence=skipped_low,
            segment_ids=accepted,
        )

    # -- 写（引擎产物，供 transcribe 节点/测试用）---------------------------

    def replace_segments(
        self,
        segments: Sequence[dict[str, Any]],
        *,
        audio_id: str | None = None,
        now: str | None = None,
    ) -> int:
        """整体替换某会议的转写分段（引擎重跑时用）。

        ⚠️ **这是唯一会删除分段的操作**，而且是 DELETE + INSERT 全量替换。
        之所以允许这么重，是因为它就是"重跑转写"的语义——先清干净再落新的，
        否则旧段和新段会混在一起，`seq` 撞车。

        `segment_id` 由 `f"{meeting_id}:{seq:05d}"` 确定性生成：
        重跑后同一 `seq` 的 id 不变，任何指向它的引用（问题清单、
        人工标记）不会在重跑后指向一个不存在的 id。
        """
        ts = now or _now_iso()
        with connect_ctx(self._db_path) as conn:
            with transaction(conn):
                conn.execute(
                    "DELETE FROM transcript_segment WHERE meeting_id = ?",
                    (self._meeting_id,),
                )
                for item in segments:
                    seq = int(item["seq"])
                    conn.execute(
                        "INSERT INTO transcript_segment"
                        " (segment_id, meeting_id, audio_id, seq, start_ms, end_ms,"
                        "  text, confidence, speaker, speaker_conf, flags, reviewed,"
                        "  version)"
                        " VALUES (?,?,?,?,?,?,mmwf_encrypt(?),?,mmwf_encrypt(?),?,?,0,1)",
                        (
                            f"{self._meeting_id}:{seq:05d}",
                            self._meeting_id,
                            audio_id,
                            seq,
                            int(item["start_ms"]),
                            int(item["end_ms"]),
                            str(item.get("text") or ""),
                            item.get("confidence"),
                            item.get("speaker"),
                            item.get("speaker_conf"),
                            _dump_flags(item.get("flags")),
                        ),
                    )
        return len(segments)

    # -- 审计 --------------------------------------------------------------

    def _log_edit(
        self,
        *,
        segment_id: str,
        operator: str,
        before_text: str,
        after_text: str,
        version: int,
        changed: bool = True,
    ) -> None:
        """写 `segment_edit` 审计。

        ⚠️ **绝不把正文写进 detail**（AUD-05：审计 detail 不含转写稿原文）。
        只记 id 与长度差。`AuditLogger.segment_edit()` 内部还有一层
        `check_detail_safety` 兜底，但我们自己也不该往 detail 里塞正文。

        `changed=False`（正文没变）时也照写 —— 理由见 `edit_segment`
        里的长注释：这个动作真的改了 `version`/`reviewed`，
        而且"我确认过这一段没问题"本身就是最该留痕的判断。
        `changed` 会带进 detail，让查阅者能区分"改了字"与"只是确认"。
        """
        if self._audit is None:
            logger.warning(
                "TranscriptStore 未注入 AuditLogger，段落修改未留痕（segment_id=%s）",
                segment_id,
            )
            return
        # 签名已核对（infra/audit.py:716）：
        #   segment_edit(operator, meeting_id, segment_id, *,
        #                fields=, before_len=, after_len=, reason=, result=)
        # 它内部会把 detail 交给 check_detail_safety 兜底，我们只传长度不传正文。
        self._audit.segment_edit(
            operator,
            self._meeting_id,
            segment_id,
            fields=["text"],
            before_len=len(before_text),
            after_len=len(after_text),
            # 正文没变时用 reason 标出来 —— 查阅者一眼能区分
            # "改了字"（reason=None）与"只是确认了这一段的读法"。
            reason=None if changed else "no_text_change",
        )

    def _log_accept(
        self,
        *,
        operator: str,
        accepted: int,
        skipped_reviewed: int,
        skipped_low: int,
        threshold: float,
    ) -> None:
        if self._audit is None:
            logger.warning("TranscriptStore 未注入 AuditLogger，批量接受未留痕")
            return
        self._audit.log(
            operator,
            "segment_edit",
            target_type="meeting",
            target_id=self._meeting_id,
            detail={
                "op": "accept_high_confidence",
                "accepted": accepted,
                "skipped_reviewed": skipped_reviewed,
                "skipped_low_confidence": skipped_low,
                "threshold": threshold,
            },
        )


# ---------------------------------------------------------------------------
# 行 → 数据类
# ---------------------------------------------------------------------------

def _row_to_segment(row: sqlite3.Row | None) -> SegmentRow:
    if row is None:  # pragma: no cover - 调用方已保证非 None
        raise ValueError("row 为 None")
    return SegmentRow(
        segment_id=str(row["segment_id"]),
        meeting_id=str(row["meeting_id"]),
        audio_id=row["audio_id"],
        seq=int(row["seq"]),
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        text=str(row["text"]),
        confidence=row["confidence"],
        speaker=row["speaker"],
        speaker_conf=row["speaker_conf"],
        flags=_parse_flags(row["flags"]),
        reviewed=bool(row["reviewed"]),
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        version=int(row["version"]),
    )


def make_transcript_store(
    meeting_id: str,
    db_path: Path | str,
    *,
    audit: Any = None,
) -> TranscriptStore:
    """构造仓储（推荐入口）。

    `audit` 不传时**不自动创建** `AuditLogger` —— 与
    `make_glossary_service` 的行为不同，这里是刻意的：
    只读场景（列出转写稿）占了本模块调用的大多数，
    为它们各建一个写连接是浪费。
    需要留痕的调用方（端点）必须显式传。
    """
    return TranscriptStore(meeting_id, db_path, audit=audit)
