"""转写稿复核与记录稿生成（T1-08 service 层，M07 `review`）。

本模块把三样东西接起来：

    `domain/review/split`     纯规则：哪些段要人看
    `domain/review/record`    纯渲染：记录稿长什么样
    `infra/filestore`         `record/vN.md` 落盘 + manifest 登记
    `infra/audit`             `record_generate` 留痕

service 层只做四件"纯函数做不了"的事，其余一律委托：

  1. **读输入**：`transcript/segments.json`（T1-05 落盘的那份）
  2. **读元信息**：`meeting` 表的一行
  3. **决定版本号**：`record/vN.md` 里 N 取多少
  4. **落盘 + 留痕 + 推进状态**

为什么版本号要在 service 层定，而不让渲染层管
-------------------------------------------
施工卡「常见坑」第三条：**记录稿的文件名与版本号要与 manifest.json 一致**。
"一致"这件事需要同时看到三处（文件名、manifest 条目、审计 detail），
只有 service 层能同时看到它们。渲染层根本不知道文件叫什么，
让它参与版本决策只会制造第三份真相。

版本号规则：**递增，绝不覆盖**。
每次生成写 `record/v{N+1}.md`，N 从 manifest 里已有的 `record/v*.md` 推。
为什么不覆盖 v1：记录稿是要给人核对的，核对过程中有人改了正文、
之后又重跑一次生成，覆盖会让"我核的是哪一版"无从回答。
留版本、留 manifest、留审计，三处一起才构成可回溯的链条
（`record/` 还是加密区，多留几版的代价很小）。

为什么状态推进要单独做一个方法而不是顺手在 generate 里改库
------------------------------------------------------
`meeting.status` 是 SAD §5.1 状态机的字段，它的每一次变更都该有出处。
把它塞进 `generate_record()` 会让"生成记录稿"和"改会议状态"绑死，
而 T1-09 之后的状态推进由 `graph` 节点统一负责（M03）。
本方法只提供 `record_ready` 这一步的**显式入口**，
调用方（将来的 graph 节点）决定什么时候调它。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from mmwf.domain.asr_types import TranscriptResult
from mmwf.domain.errors import MmwfError
from mmwf.domain.review.record import (
    RENDER_SOURCE,
    RecordMetadata,
    RecordRender,
    render_record,
)
from mmwf.domain.review.split import (
    plan_review,
    review_ratio,
)
from mmwf.infra.audit import AuditLogger
from mmwf.infra.db import connect_ctx
from mmwf.infra.filestore import FileStore

logger = logging.getLogger(__name__)

#: 转写分段落盘的相对路径（与 T1-05 / filestore 约定一致）。
TRANSCRIPT_SEGMENTS_PATH = "transcript/segments.json"

#: 记录稿放 `record/`（**加密区**，见 filestore.ENCRYPTED_AREAS）。
RECORD_AREA = "record"

#: 记录稿文件名模板。`v1.md` / `v2.md` …（SAD §8.2 的目录约定写作 "record/ v1.md …"）
RECORD_NAME_TEMPLATE = "v{version}.md"

#: 状态机里"记录稿已生成"那个状态（SAD §5.2）。
STATE_RECORD_READY = "record_ready"

#: 允许进入本步骤的前置状态（SAD §5.1：`reviewing` 的下一步）。
#:
#: `transcript_ready` 也放行：小会（比如 5 分钟）可能一段都不需要复核，
#: 直接 `transcript_ready -> record_ready` 是合理的快路径。
#:
#: ⚠️ T1-11 Review 修正：`created` / `ingest_pending` / `transcribing`
#: **必须放行**。第一版把它们挡在外面，理由是"那说明调用方跳步了"，
#: 但那个理由只对**直接调用 service 的人**成立。接进编排层之后：
#:
#:   · 从 **API 建会议**进来的会议，`meeting.status` 是 `created`
#:     —— 建表默认值，**流水线从来不写它**（编排层只写 `task_state`，
#:     见 `graph/nodes/_base.touch_task_state` 与 `review` 节点的
#:     "状态推进收归编排层"那条）。
#:   · 于是"正常从头跑一遍"的会议，其 `meeting.status` 恰好就是 `created`。
#:
#: 两者叠加的结果是：**每次 `start` 都会在 `review` 节点抛
#: `ReviewServiceError`，被包成 E9001** —— 整条流水线一步都走不动，
#: 而且错误信息（"当前状态是 created，不能生成记录稿"）会把排查引向
#: "谁把状态弄错了"，而真相是"这道门槛从一开始就拦错了人"。
#:
#: 真正该守的不是"meeting.status 是什么"，而是**输入有没有**
#: —— 那由 `load_segments()` 守（缺 `transcript/segments.json` 就抛）。
#: 用"输入不存在"当前置条件，比用"某个字段等于某个值"稳得多。
#:
#: 保留 `reviewing` / `record_ready` 的场景意义：从复核页重新生成记录稿
#: （v2、v3…）走的是这两个状态，它们仍然是**最典型**的入口。
ALLOWED_PREV_STATES = frozenset({
    "created",
    "ingest_pending",
    "transcribing",
    "transcript_ready",
    "reviewing",
    "record_ready",
})


class ReviewServiceError(MmwfError):
    """复核/记录稿环节的使用错误（缺输入、状态不对等）。

    ★★ 同 `ExportServiceError`：继承 `MmwfError` 而非裸 `RuntimeError`（T1-16 坑 16）。

    本模块文档（第 92 行）早就记过这个后果 ——
    "*每次 `start` 都会在 `review` 节点抛 `ReviewServiceError`，
    被包成 E9001* —— 整条流水线一步都走不动"。当时的修法是
    **绕过它**（放宽 `ALLOWED_PREV_STATES`，让那道错的门槛不拦人），
    但**根因留在原地**：只要还有任何一条路径抛到这个类，
    用户看到的就还是"程序遇到一个未预期的问题 + 请把编号告诉技术人员"。

    把继承链改对，才算把这个坑填平 —— 放宽门槛是"不再踩"，
    改继承是"踩到也不再摔"。
    """

    code = "E1002"

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        """同 `ExportServiceError`：`MmwfError` 是 dataclass，
        第一个字段是 `code`，不显式写 `__init__` 会把消息串当成错误码。"""
        super().__init__(code=type(self).code, detail=detail, context=ctx)


@dataclass
class RecordResult:
    """一次记录稿生成的结果。"""

    meeting_id: str
    version: int
    rel_path: str
    markdown: str
    sha256: str
    segment_count: int
    review_count: int
    review_ratio: float
    metadata_filled: tuple[str, ...]
    metadata_missing: tuple[str, ...]
    review_items: tuple[Any, ...]
    warnings: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{RECORD_NAME_TEMPLATE.format(version=self.version)}"

    @property
    def metadata_complete(self) -> bool:
        return not self.metadata_missing

    def summary(self) -> str:
        """一句人话（界面提示条 / 验收输出用）。"""
        from mmwf.domain.review.record import summarize_review

        parts = [f"记录稿 v{self.version} 已生成（{self.filename}）"]
        parts.append(summarize_review(self.review_items, self.segment_count))
        if self.metadata_missing:
            parts.append(f"元信息待补充 {len(self.metadata_missing)} 项")
        return "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "version": self.version,
            "path": self.rel_path,
            "sha256": self.sha256,
            "segment_count": self.segment_count,
            "review_count": self.review_count,
            "review_ratio": round(self.review_ratio, 4),
            "metadata_filled": list(self.metadata_filled),
            "metadata_missing": list(self.metadata_missing),
            "summary": self.summary(),
        }


class ReviewService:
    """转写稿复核与记录稿生成服务。

    Args:
        store: 该会议的 `FileStore`（透明加解密，service 层不感知加密）。
        db_path: 库路径，用于读 `meeting` 表、推进 `status`。
        thresholds: 分流阈值（**必须传**，形状见 `domain/review/split`）。
            刻意不给默认值：默认值会让"配置改了却没生效"无从察觉。
        audit: 审计写入器。None 时按设计打 warning（不静默吞掉）——
            与 T1-07 的 `GlossaryService` 保持同一约定。
        operator: 操作者标识，写审计用。
    """

    def __init__(
        self,
        store: FileStore,
        *,
        db_path: Path | str,
        thresholds: Mapping[str, Any],
        audit: AuditLogger | None = None,
        operator: str = "system",
    ) -> None:
        self._store = store
        self._db_path = Path(db_path)
        self._thresholds = dict(thresholds)
        self._audit = audit
        self._operator = operator

    # ------------------------------------------------------------------
    # 读输入
    # ------------------------------------------------------------------

    def load_segments(self) -> TranscriptResult:
        """读取 `transcript/segments.json` 并重建 `TranscriptResult`。

        为什么重建而不是直接读字典：`TranscriptResult` 的字段是一套契约
        （`seq` 连续、时间戳单调、`confidence` 可为 None），
        直接吃字典会把这些保证丢掉，下游渲染出时间码错乱的稿子
        而没有任何一层会发现。重建一次的成本可以忽略。

        Raises:
            ReviewServiceError: 文件不存在，或内容不是预期的形状。
        """
        rel = TRANSCRIPT_SEGMENTS_PATH
        if not self._store.exists(rel):
            raise ReviewServiceError(
                f"转写分段不存在：{rel}（meeting_id={self._store.meeting_id}）。"
                "请先完成转写（T1-05），再生成记录稿。"
            )
        try:
            raw = self._store.read_json(rel)
        except (json.JSONDecodeError, ValueError) as e:
            raise ReviewServiceError(f"转写分段不是合法 JSON：{rel}（{e}）") from e

        if not isinstance(raw, dict):
            raise ReviewServiceError(
                f"转写分段顶层应为对象，实际 {type(raw).__name__}：{rel}"
            )
        seg_dicts = raw.get("segments")
        if not isinstance(seg_dicts, list):
            raise ReviewServiceError(f"转写分段缺少 segments 数组：{rel}")

        return TranscriptResult.from_raw_segments(
            seg_dicts,
            engine=str(raw.get("engine") or "unknown"),
            model=str(raw.get("model") or "unknown"),
            hotwords_applied=bool(raw.get("hotwords_applied")),
            warnings=tuple(raw.get("warnings") or ()),
        )

    def load_metadata(self, *, generated_at: str) -> RecordMetadata:
        """读 `meeting` 表一行，构造元信息（缺行则全部渲染成「待补充」）。

        **不抛错**：会议行缺失是"元信息全缺"，不是"不能生成记录稿"。
        正文（转写分段）才是记录稿的主体，元信息是抬头。
        因为抬头缺了就不给正文，用户会以为"系统坏了"——
        而这本来就是他该看到的"请补齐会议信息"的提示。
        """
        row: Mapping[str, Any] | None = None
        with connect_ctx(self._db_path, readonly=True) as conn:
            r = conn.execute(
                "SELECT title, meeting_time, location, host, attendees,"
                "       absentees, recorder, status"
                "  FROM meeting WHERE meeting_id = ?",
                (self._store.meeting_id,),
            ).fetchone()
            if r is not None:
                row = dict(r)

        if row is None:
            logger.warning(
                "meeting 表中没有 %s 这一行，记录稿元信息将全部为「待补充」",
                self._store.meeting_id,
            )
            return RecordMetadata(generated_at=generated_at)

        return RecordMetadata.from_row(row, generated_at=generated_at)

    def current_status(self) -> str | None:
        """读当前会议状态（无行返回 None）。"""
        with connect_ctx(self._db_path, readonly=True) as conn:
            r = conn.execute(
                "SELECT status FROM meeting WHERE meeting_id = ?",
                (self._store.meeting_id,),
            ).fetchone()
        return str(r[0]) if r is not None else None

    # ------------------------------------------------------------------
    # 版本号
    # ------------------------------------------------------------------

    def existing_versions(self) -> list[int]:
        """扫 manifest + 目录，列出已有的记录稿版本号（升序）。

        两个来源都要看，缺一不可：
          · **目录**：真实存在什么文件（manifest 被误删时仍能工作）
          · **manifest**：登记过什么（文件被外力删掉时仍知道"曾经有 v2"）
        取并集，让新版本号**跳过**任何曾经用过的号 —— 否则会出现
        "v2 曾经存在、文件被删、重新生成又写 v2"，审计里两条 detail
        指向同一个路径但内容不同，无法分辨。
        """
        versions: set[int] = set()

        d = self._store.store_root / RECORD_AREA
        if d.is_dir():
            for p in d.glob("v*.md"):
                n = _version_from_name(p.name)
                if n is not None:
                    versions.add(n)

        for e in self._store.manifest():
            if e.path.startswith(f"{RECORD_AREA}/"):
                n = _version_from_name(Path(e.path).name)
                if n is not None:
                    versions.add(n)

        return sorted(versions)

    def next_version(self) -> int:
        """下一个可用版本号（从 1 开始）。"""
        existing = self.existing_versions()
        return (existing[-1] + 1) if existing else 1

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def generate_record(
        self,
        *,
        now: str | None = None,
        write_state: bool = True,
        extra_warnings: Sequence[str] = (),
    ) -> RecordResult:
        """从转写分段生成「会议记录.md」，落盘 + 登记 manifest + 留痕。

        Args:
            now: ISO8601 时间戳（"记录整理时间"，测试可注入）。
                 生产调用不要传 —— 时间必须只在一处产生（与 audit 同规矩）。
            write_state: 是否把 `meeting.status` 推进到 `record_ready`。
                默认 True（这是本步骤的**结果**）。设 False 用于
                "只想看看会生成什么、别动状态"的场景。
            extra_warnings: 上游告警（如 T1-05 的"引擎未提供置信度"），
                 会附在记录稿末尾的"生成提示"里 —— 让看稿子的人
                 也知道"这份稿子有几个段是不带分数的"。

        Returns:
            `RecordResult`（含版本号、相对路径、哈希、统计）。

        Raises:
            ReviewServiceError: 前置状态不对 / 缺转写分段。
        """
        prev_status = self.current_status()
        if prev_status is not None and prev_status not in ALLOWED_PREV_STATES:
            raise ReviewServiceError(
                f"当前状态是 {prev_status!r}，不能生成记录稿"
                f"（允许的前置状态：{sorted(ALLOWED_PREV_STATES)}）。"
                "请先完成转写与复核分流。"
            )

        transcript = self.load_segments()
        segments = list(transcript.segments)
        generated_at = now or _now_iso()
        metadata = self.load_metadata(generated_at=generated_at)
        version = self.next_version()

        render: RecordRender = render_record(
            segments,
            metadata,
            self._thresholds,
            meeting_id=self._store.meeting_id,
            version=version,
            extra_warnings=extra_warnings,
        )

        rel_path = f"{RECORD_AREA}/{RECORD_NAME_TEMPLATE.format(version=version)}"
        stored = self._store.write_text(rel_path, render.markdown, stage=RECORD_AREA)

        ratio = review_ratio(segments, self._thresholds)

        if write_state:
            self.set_status(STATE_RECORD_READY)

        self._write_audit(
            version=version,
            rel_path=rel_path,
            segment_count=render.body_segment_count,
            review_count=render.review_count,
            ratio=ratio,
            metadata_missing=render.metadata_missing,
        )

        logger.info(
            "记录稿已生成：meeting_id=%s %s（%d 段，%d 段待核对，%.1f%%）",
            self._store.meeting_id, rel_path,
            render.body_segment_count, render.review_count, ratio * 100,
        )

        return RecordResult(
            meeting_id=self._store.meeting_id,
            version=version,
            rel_path=rel_path,
            markdown=render.markdown,
            sha256=stored.sha256,
            segment_count=render.body_segment_count,
            review_count=render.review_count,
            review_ratio=ratio,
            metadata_filled=render.metadata_filled,
            metadata_missing=render.metadata_missing,
            review_items=render.review_items,
            warnings=list(render.warnings),
        )

    def plan_review_report(self) -> list[dict[str, Any]]:
        """复核分流清单（复核页/API 用）：逐段的结论与理由。

        形式是**字典列表**而不是领域对象：这一层是应用层，
        产物要能被 JSON 序列化后直接给前端（`GET /meetings/{id}/transcript`
        返回形状的一部分）。
        """
        transcript = self.load_segments()
        return [
            {
                "seq": d.seq,
                "needs_review": d.needs_review,
                "reason": d.reason,
            }
            for d in plan_review(list(transcript.segments), self._thresholds)
        ]

    def read_record(self, version: int | None = None) -> str:
        """读回记录稿 Markdown（`GET /meetings/{id}/record`，F2.3）。

        `version=None` 时取**最新一版**。
        """
        v = version if version is not None else self.latest_version()
        if v is None:
            raise ReviewServiceError(
                f"还没有生成过记录稿（meeting_id={self._store.meeting_id}）"
            )
        rel = f"{RECORD_AREA}/{RECORD_NAME_TEMPLATE.format(version=v)}"
        if not self._store.exists(rel):
            raise ReviewServiceError(f"记录稿不存在：{rel}")
        return self._store.read_text(rel)

    def read_record_body(self, version: int | None = None) -> str:
        """读回记录稿的**正文部分**（去掉元信息表与待核对清单）。

        ★ 为什么要有这个方法（T1-11 Review 补）

        `record_markdown` 是"给人看的完整稿"，含三块：元信息表、
        正文、待核对清单。但下游 `extract` 节点把它当作
        **"这次会议说了什么"** 的唯一输入 —— 于是元信息表被逐行抽成议题：

            议题「| 会议时间 | 待补充 |」没有来源依据，无法追溯
            议题「> 记录稿版本：v2」没有来源依据，无法追溯

        实测一整篇正常记录稿能产出 **15 条自检问题，其中 14 条是误报**，
        全部来自元信息表与清单本身。而误报的代价不是"多几条提示"：
        `validate` 的 `route_after_validate` 一看到 issues 就重试，
        重试到上限后 `give_up` —— **每篇纪要都会走完整条重试路径**，
        并且带着 15 条噪音进最终稿，审定人根本看不出真正的问题在哪。

        所以"给人看的稿"与"给机器读的稿"必须分开：本方法给后者。
        标题行（`#` / `##`）一并去掉，它们由 `extract` 从正文里自己认。
        """
        v = version if version is not None else self.latest_version()
        if v is None:
            raise ReviewServiceError(
                f"还没有生成过记录稿（meeting_id={self._store.meeting_id}）"
            )
        rel = f"{RECORD_AREA}/{RECORD_NAME_TEMPLATE.format(version=v)}"
        if not self._store.exists(rel):
            raise ReviewServiceError(f"记录稿不存在：{rel}")
        return extract_body(self._store.read_text(rel), v)

    def latest_version(self) -> int | None:
        """最新记录稿版本号；从未生成过返回 None。"""
        existing = self.existing_versions()
        return existing[-1] if existing else None

    # ------------------------------------------------------------------
    # 状态推进
    # ------------------------------------------------------------------

    def set_status(self, status: str) -> None:
        """推进 `meeting.status`（带 updated_at）。

        会议行不存在时**打 warning 而不是抛错**：本 service 的输入是
        filestore 里的转写稿，会议行可能因为"接入了文字稿、还没建会议"
        而缺失。这时记录稿仍应能生成（它有自己的 manifest 与审计），
        状态推进失败不该让整个动作失败。

        用参数化 SQL（不拼字符串）——`status` 值虽然来自内部常量，
        但把拼接习惯带进这个文件，早晚会有人照着写一个带用户输入的版本。
        """
        with connect_ctx(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE meeting SET status = ?, updated_at = ? WHERE meeting_id = ?",
                (status, _now_iso(), self._store.meeting_id),
            )
            if cur.rowcount == 0:
                logger.warning(
                    "meeting 表无 %s 行，未推进状态到 %s（记录稿与审计已照常落盘）",
                    self._store.meeting_id, status,
                )

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def _write_audit(
        self,
        *,
        version: int,
        rel_path: str,
        segment_count: int,
        review_count: int,
        ratio: float,
        metadata_missing: Sequence[str],
    ) -> None:
        """写 `record_generate`。

        **不带正文**（部署手册 §5.3）：只记版本、路径、统计、缺项标签。
        `check_detail_safety` 会在 audit 侧再做一次兜底校验。
        """
        if self._audit is None:
            logger.warning(
                "未注入 AuditLogger：记录稿 %s 的生成未留痕"
                "（部署手册 §6.2 要求记录稿生成可追溯）",
                rel_path,
            )
            return
        self._audit.record_generate(
            operator=self._operator,
            meeting_id=self._store.meeting_id,
            version=version,
            rel_path=rel_path,
            segment_count=segment_count,
            review_count=review_count,
            review_ratio=ratio,
            metadata_missing=list(metadata_missing),
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def make_review_service(
    meeting_id: str,
    settings: object,
    *,
    audit: AuditLogger | None = None,
    operator: str = "system",
    encrypt_enabled: bool = True,
) -> ReviewService:
    """按配置构造 `ReviewService`（推荐入口）。

    三样配置来源：
      · `settings.app.data_dir`          -> 库与 store 的根
      · `settings.thresholds.asr`        -> 分流阈值
      · `settings.resolve_path()`        -> data_dir 若是相对路径，按项目根解析

    阈值**从 `settings.thresholds` 现取**，不在此处写死 0.70 ——
    N5.2 要求阈值可热改，写死就等于改配置不生效。
    """
    app = getattr(settings, "app", None)
    if app is None:
        raise ReviewServiceError("settings 缺少 app 段，无法定位 data_dir")

    data_dir = app.data_dir
    resolver = getattr(settings, "resolve_path", None)
    if resolver is not None:
        data_dir = resolver(data_dir)
    data_dir = Path(data_dir)

    thr = getattr(settings, "thresholds", None)
    asr_thr = getattr(thr, "asr", None) if thr is not None else None
    if asr_thr is None:
        raise ReviewServiceError("settings 缺少 thresholds.asr 段（分流阈值无处可取）")

    thresholds = {
        "asr": {
            "low_confidence": float(getattr(asr_thr, "low_confidence", 0.70)),
            "warn_avg_confidence": float(getattr(asr_thr, "warn_avg_confidence", 0.85)),
            "warn_low_conf_ratio": float(getattr(asr_thr, "warn_low_conf_ratio", 0.15)),
        }
    }

    from mmwf.infra.db import app_db_path
    from mmwf.infra.filestore import make_filestore

    store = make_filestore(meeting_id, data_dir, encrypt_enabled=encrypt_enabled)
    return ReviewService(
        store,
        db_path=app_db_path(data_dir),
        thresholds=thresholds,
        audit=audit,
        operator=operator,
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def extract_body(markdown: str, version: int | None = None) -> str:
    """从完整记录稿里取**正文**（去掉元信息表、版本行、待核对清单）。

    这是"给人看的稿"与"给机器读的稿"的分界线 —— 见
    `ReviewService.read_record_body()` 的说明。

    做法：按 `render_record()` 的固定结构切分，而不是猜。
    `## 二、会议记录正文` 到下一个 `##` 之间就是正文。
    **认不出结构时退回原文** —— 宁可让下游多几条误报，
    也不能因为格式微调就把整篇正文丢掉（那会产出一篇空纪要）。
    """
    from mmwf.domain.review.record import RECORD_BODY_HEADING

    lines = markdown.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(f"## {RECORD_BODY_HEADING}"):
            start = i + 1
            break
    if start is None:
        logger.warning(
            "记录稿里找不到「%s」章节，正文按全文处理（版本 %s）",
            RECORD_BODY_HEADING, version,
        )
        return markdown

    body: list[str] = []
    for line in lines[start:]:
        # 遇到下一个二级标题就是正文结束（待核对清单 / 生成提示）
        if line.strip().startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def _version_from_name(name: str) -> int | None:
    """`v3.md` -> 3；其它返回 None。

    用 `removeprefix`/`removesuffix` 而不是正则：形状是固定的，
    正则反而要处理"为什么要用正则"的疑问。
    """
    if not name.startswith("v") or not name.endswith(".md"):
        return None
    body = name[1:-3]
    return int(body) if body.isdigit() else None


def _now_iso() -> str:
    """ISO8601 UTC（秒级、带 Z）——与 audit / filestore 同一格式，
    定长字典序 == 时间序。"""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
