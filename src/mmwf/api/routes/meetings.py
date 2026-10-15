"""会议生命周期端点：建会议 / 起流水线 / 查状态 / 议题与闸口 / 成稿 / 追加音频。

这是本产品**主链路**所在：从投递音频到拿到纪要，一步一步都在这里。

⚠️ 端点注册顺序在本模块内部无约束 —— 全项目只有 `export.py` 里
`export/undo` 与 `export/{file}` 有顺序要求，详见那个模块。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Path as PathParam

from mmwf.api import envelope as env
from mmwf.domain.errors import EmptyTranscriptError, MmwfError
from mmwf.infra.db import connect_ctx

from ._context import Runtime
from ._support import (
    ALLOWED_DECISIONS,
    CLASSIFICATIONS,
    GATE_NODE_MAP,
    _chunk_dir,
    _decision_message,
    _latest_record_rel,
    _now,
    _require_meeting,
)

logger = logging.getLogger(__name__)


def register(router: APIRouter, rt: Runtime) -> None:
    """注册会议生命周期端点。"""

    # -- ② 建会议 + 投递 -----------------------------------------------

    @router.post("/meetings")
    def create_meeting(payload: dict[str, Any] = Body(...)):
        """建会议并投递音频/文字稿（F1.1 / F1.6）。

        `audio_paths` 走 `IngestService` 真落盘（校验格式、探测时长、
        算切分计划）；`record_markdown` 直接写记录稿（文字稿模式）。

        两者至少给一个 —— 都不给等于建了个空会议，后面 `start`
        必然失败，不如在这里就拦下来（E1004）。
        """
        title = (payload.get("title") or "").strip()
        if not title:
            raise EmptyTranscriptError("title 为空")

        audio_paths = payload.get("audio_paths") or []
        record_markdown = payload.get("record_markdown")

        if not audio_paths and not record_markdown:
            raise EmptyTranscriptError("既没有 audio_paths 也没有 record_markdown")

        classification = payload.get("classification") or "internal"
        if classification not in CLASSIFICATIONS:
            raise EmptyTranscriptError(
                f"classification={classification!r} 不在允许取值内"
            )

        meeting_id = f"m-{uuid.uuid4().hex[:12]}"
        now = _now()
        with connect_ctx(rt.db_path) as conn:
            conn.execute(
                """
                INSERT INTO meeting (
                    meeting_id, title, meeting_time, location, host,
                    attendees, absentees, recorder, classification,
                    template_id, status, created_by, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    meeting_id,
                    title,
                    payload.get("meeting_time"),
                    payload.get("location"),
                    payload.get("host"),
                    json.dumps(payload.get("attendees") or [], ensure_ascii=False),
                    json.dumps(payload.get("absentees") or [], ensure_ascii=False),
                    payload.get("recorder"),
                    classification,
                    payload.get("template_id"),
                    "created",
                    payload.get("operator") or "unknown",
                    now,
                    now,
                ),
            )

        data: dict[str, Any] = {"meeting_id": meeting_id}
        warnings: list[str] = []

        if audio_paths:
            svc = rt.ingest_service(meeting_id)
            # ⚠️⚠️ `ingest_batch` 返回的是 **`BatchResult` 对象**，不是列表
            # （T1-15 Review 补的第 20 个缺陷）。
            #
            # 第一版写的是 `for r in results:` + `results` 直接当列表用 ——
            # 在 `make_ingest_service` 那个 TypeError 修好之后**立刻**又炸一次：
            #     TypeError: 'BatchResult' object is not iterable
            # 同样是 E9001。
            #
            # 说明这一整段代码**从来没有被真正执行过**：两个错误叠在一起，
            # 前者把它挡在门外，修掉前者才露出后者。
            # 这类"串联的坏代码"只有在有人真的把主路径跑一遍时才会露出来。
            #
            # ⚠️⚠️ **必须传 `chunk_dir`**（第 23 个缺陷）。
            # 不传时 `IngestService.ingest_one` 会跳过切分
            # （`if not self._probe_only and chunk_dir is not None:`），
            # **一个切分文件都不落盘**；而 `chunk_count` 取的是
            # `len(chunk_plan)` —— 于是响应里照样报"6 个切分块"，
            # 磁盘上却是空的。**假报数比缺功能更糟**：用户以为切好了，
            # 等到转写阶段才发现没有输入，而那时早已离场。
            #
            # 目录约定与 `ingest` 节点（`graph/nodes/ingest.py` 的
            # `CHUNK_DIR`）保持一致：`{data_dir}/{meeting_id}/audio/chunks`。
            batch = svc.ingest_batch(
                [Path(p) for p in audio_paths],
                chunk_dir=_chunk_dir(rt.cfg, meeting_id),
            )
            total_chunks = 0
            for item in batch.succeeded:
                total_chunks += int(item.chunk_count)
                warnings.extend(item.warnings)
            for f in batch.failed:
                warnings.append(f"{f.filename}: {f.message}")
            # 逐条结果同时以「成功 / 失败」两栏给出，形状与
            # `BatchResult.to_dict()` 一致 —— 前端一种解析方式管两处端点。
            data["audio"] = [s.to_dict() for s in batch.succeeded] + [
                f.to_dict() for f in batch.failed
            ]
            data["succeeded"] = len(batch.succeeded)
            data["failed"] = len(batch.failed)
            data["chunk_count"] = total_chunks
            # 有失败时把 summary 也带上（人话版："2 个音频都未能接收"）
            if batch.failed:
                warnings.append(batch.summary())

        if record_markdown:
            from mmwf.infra.filestore import make_filestore

            store = make_filestore(meeting_id, rt.cfg.resolve_path(rt.cfg.app.data_dir))
            store.write_text("record/v1.md", record_markdown, stage="record")
            data["record"] = "record/v1.md"

        if warnings:
            data["warning"] = "；".join(warnings)

        return env.ok(data, "已接收")

    # -- ③ 触发流水线 --------------------------------------------------

    @router.post("/meetings/{meeting_id}/start")
    def start_pipeline(meeting_id: str = PathParam(...)):
        """触发流水线（F6.1）。

        跑到第一个闸口（G2）或跑完。返回当前 `RunStatus`，
        前端据 `status` 决定是展示"待确认议题"还是继续轮询。
        """
        # ⚠️ 这里**只是校验会议存在**，不是拿它当上下文管理器。
        # 第一版写成 `with _require_meeting(...)` → 它返回的是 dict，
        # 不是 context manager → `TypeError: 'dict' object does not
        # support the context manager protocol` → `/start` **恒定 E9001**。
        row = _require_meeting(meeting_id, rt.db_path)
        _ensure_text_input(meeting_id, row, rt.cfg)
        with rt.meeting_lock(meeting_id):
            runner = rt.runner(meeting_id)
            try:
                status = runner.start()
                rt.gate_service().observe_pending(meeting_id, runner)
                return env.ok(status.to_dict(), "已开始处理")
            finally:
                runner.close()

    # -- ④ 详情与状态 --------------------------------------------------

    @router.get("/meetings/{meeting_id}")
    def get_meeting(meeting_id: str = PathParam(...)):
        """会议详情与当前状态（F6.4）。

        **前端主要靠轮询这个端点**推进界面，所以它要一次给全：
        会议元信息 + 运行状态 + 闸口载荷（若在等确认）。
        """
        row = _require_meeting(meeting_id, rt.db_path)
        data = dict(row)
        # 参会人是 JSON 字符串，出接口前转回数组，免得前端自己 parse
        for k in ("attendees", "absentees"):
            raw = data.get(k)
            try:
                data[k] = json.loads(raw) if raw else []
            except (TypeError, ValueError):
                data[k] = []

        with rt.meeting_lock(meeting_id):
            runner = rt.runner(meeting_id)
            try:
                data["run"] = rt.gate_service().observe_pending(meeting_id, runner).to_dict()
            except Exception:  # pragma: no cover - 尚未开跑时状态不可读
                data["run"] = None
            finally:
                runner.close()
        return env.ok(data)

    @router.get("/meetings")
    def list_meetings(
        status: str | None = None,
        limit: int = 50,
    ):
        """会议列表，支持按状态筛选（F6.4 / F1.7）。"""
        sql = "SELECT meeting_id, title, status, classification, meeting_time, updated_at FROM meeting"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with connect_ctx(rt.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return env.ok([dict(r) for r in rows])

    # -- ⑤ 议题与闸口 --------------------------------------------------

    @router.get("/meetings/{meeting_id}/topics")
    def get_topics(meeting_id: str = PathParam(...)):
        """取议题抽取结果，送 G2 审（F3.1 / G2）。

        议题来自**图状态**（`extract` 节点的产物），不是数据库 ——
        它在等人工确认前只是中间结果，确认后才进正式产物。
        """
        _require_meeting(meeting_id, rt.db_path)
        with rt.meeting_lock(meeting_id):
            runner = rt.runner(meeting_id)
            try:
                values = runner.values()
            finally:
                runner.close()
        return env.ok(
            {
                "topics": values.get("topics") or [],
                "resolutions": values.get("resolutions") or [],
                "action_items": values.get("action_items") or [],
            }
        )

    @router.post("/meetings/{meeting_id}/gate/{gate}/decide")
    def decide_gate(
        meeting_id: str = PathParam(...),
        gate: str = PathParam(...),
        payload: dict[str, Any] = Body(...),
    ):
        """闸口决策（F4.1，SAD §5.3）。

        `decision` 三选一，语义差别是**本卡的核心**：

            approve —— 原样继续，不重跑任何节点
            reject  —— 清当前产物，**只重跑被驳回的上游节点**
            edit    —— 用改后的载荷注入状态，**从下游继续（不重跑）**

        `rerun_nodes` 放在 data 里返回，前端可据此告诉用户
        "已按您的修改继续，未重做之前的工作" —— 这是 V5 验收项，
        也是用户最能感知"省时间"的地方。
        """
        row = _require_meeting(meeting_id, rt.db_path)
        gate_name = gate.upper()
        if gate_name not in GATE_NODE_MAP:
            raise MmwfError(code="E4001", detail=f"未知闸口 {gate!r}")

        operator = payload.get("operator") or row.get("recorder") or "unknown"
        result = rt.gate_service().apply_decision(
            meeting_id, gate_name, payload.get("checkpoint_id"), payload.get("decision"),
            payload.get("payload"), operator, payload.get("comment", ""),
        )
        return env.ok(result, _decision_message(payload.get("decision")))

    # -- ⑥ 成稿 --

    @router.get("/meetings/{meeting_id}/minutes")
    def get_minutes(meeting_id: str = PathParam(...)):
        """取纪要草稿：结构化 + Markdown（F3.x）。"""
        _require_meeting(meeting_id, rt.db_path)
        with rt.meeting_lock(meeting_id):
            runner = rt.runner(meeting_id)
            try:
                values = runner.values()
                status = runner.status()
            finally:
                runner.close()
        return env.ok(
            {
                "minutes_structured": values.get("minutes_structured"),
                "minutes_markdown": values.get("minutes_markdown") or "",
                "validation_issues": values.get("validation_issues") or [],
                "gen_retry": int(values.get("gen_retry") or 0),
                "status": status.status,
                "status_label": status.status_label,
            }
        )

    @router.get("/meetings/{meeting_id}/record")
    def get_record(
        meeting_id: str = PathParam(...),
        version: int | None = None,
    ):
        """取记录稿 Markdown（F2.3）。

        `version=None` 取最新一版；取不到时 `markdown=""` 而不是报错 ——
        "还没生成记录稿"是一个**正常的中间态**（会议刚建、还没跑 start），
        前端据此显示"尚未生成"即可，不该弹红框。
        """
        _require_meeting(meeting_id, rt.db_path)
        svc = rt.review_service(meeting_id)
        latest = svc.latest_version()
        try:
            markdown = svc.read_record(version)
            found = True
        except Exception:
            markdown, found = "", False
        return env.ok(
            {
                "version": version if version is not None else latest,
                "latest_version": latest,
                "available": found,
                "markdown": markdown,
            },
            "已取到记录稿" if found else "尚未生成记录稿",
        )

    @router.post("/meetings/{meeting_id}/audio")
    def append_audio(
        meeting_id: str = PathParam(...), payload: dict[str, Any] = Body(default={})
    ):
        """追加音频文件（F1.1）。

        ⚠️⚠️ **本端点必须诚实说明它没做什么**，否则用户会以为
        "追加完就自动转写了"：

          · 它走**完整的接入流程**：格式校验 → 存在性/大小 → ffprobe 探时长
            → 出切分计划 → 落盘到该会议的 store（与 `POST /meetings` 同一条路）；
          · 它**不写 `audio_file` 表** —— 那张表的 `audio_id` 没有任何代码
            生成过，`create_meeting` 也没写。本卡**不替它发明一套 id 约定**
            （那会是一张表两个出处）。等有专门的"音频登记"卡时一起改。
          · 它**不触发转写**，也**不合并 `transcript/segments.json`**。
            `transcribe` 节点读的是会议最初的 `audio_paths`，本端点不改
            状态机输入 —— 所以追加的录音**要重新发起处理才会进转写稿**。

        ⚠️ **全批失败也返回 200**：批量入口的语义就是"尽力而为"。
        一个文件坏了就回 4xx，前端会把"这次操作失败"显示出来，
        而实际上另一个文件已经成功落盘了。逐条结果在 `data.audio` 里。

        ⚠️ 但 `audio_paths` **整体为空**是调用错误（E1001），那是另一回事。
        """
        _require_meeting(meeting_id, rt.db_path)

        audio_paths = payload.get("audio_paths")
        if not isinstance(audio_paths, (list, tuple)) or not audio_paths:
            raise MmwfError(
                code="E1001",
                detail=f"audio_paths 为空或类型不对：{audio_paths!r}",
                user_message="请至少给一个音频文件的路径。",
                context={"area": "audio", "meeting_id": meeting_id},
            )
        if not all(isinstance(p, str) and p.strip() for p in audio_paths):
            raise MmwfError(
                code="E1001",
                detail=f"audio_paths 里有非字符串或空项：{audio_paths!r}",
                user_message="音频路径必须是文本，且不能为空。",
                context={"area": "audio", "meeting_id": meeting_id},
            )

        svc = rt.ingest_service(meeting_id)
        # ⚠️ **`chunk_dir` 必须给**（第 23 个缺陷，详见 `create_meeting`
        # 里那段注释）：不给时切分**一步都不做**，而 `chunk_count` 仍照报
        # （它取的是切分计划的长度，不是落盘文件数）—— 假报数。
        batch = svc.ingest_batch(
            [Path(p) for p in audio_paths],
            chunk_dir=_chunk_dir(rt.cfg, meeting_id),
        )

        items = [r.to_dict() for r in batch.succeeded] + [r.to_dict() for r in batch.failed]
        succeeded = len(batch.succeeded)
        failed = len(batch.failed)
        total_chunks = sum(int(r.chunk_count) for r in batch.succeeded)

        warnings: list[str] = []
        for r in batch.failed:
            # ⚠️ `IngestFailure` 的字段是 `.message`（不是 `.user_message`，
            # 也不是 `.error`）—— `.message` 才是**已经翻好的人话**。
            # `.error` 是原始 `MmwfError`，它的 `user_message` 在
            # `.to_dict()` 里才展开。这里直接取 `.message`。
            warnings.append(f"{r.filename}: {r.message}")
        for r in batch.succeeded:
            warnings.extend(r.warnings)

        # 会议已经跑过之后追加音频 → 文件在，稿子里却没有。本卡不阻断
        # （没有任何"重跑"入口，阻断就把用户堵死在没有出口的地方），
        # 但必须提示。
        row = _require_meeting(meeting_id, rt.db_path)
        status = str(row.get("status") or "")
        if status not in {"created", "failed", "cancelled"}:
            warnings.append(
                f"本次会议当前状态是「{status}」，追加的录音不会自动进入已有转写稿；"
                "需要重新发起处理。"
            )

        data: dict[str, Any] = {
            "meeting_id": meeting_id,
            "audio": items,
            "succeeded": succeeded,
            "failed": failed,
            "chunk_count": total_chunks,
        }
        if warnings:
            data["warning"] = "；".join(warnings)

        if succeeded == 0:
            return env.ok(data, f"{failed} 个音频文件都没能接入，失败原因见下")
        return env.ok(
            data,
            f"已接收 {succeeded} 个音频文件（共 {total_chunks} 个切分块）。"
            "⚠️ 追加的音频不会自动转写，需要重新发起处理才会体现在转写稿中。",
        )

def _ensure_text_input(meeting_id: str, row: dict[str, Any], cfg: Settings) -> None:
    """开跑之前，把"文字稿导入"这条路的输入补齐。

    ★ T1-11 Review 补的第 6 个缺陷 —— 也是**唯一一个让闭环真跑不起来**的。

    问题：会议是用 `record_markdown` 建的（F1.6 文字稿导入），
    但 `review` 节点的输入契约是 `transcript/segments.json`
    （`transcribe` 节点落的那份）。文字稿导入路上**没人落它**
    —— `ingest` 跳过、`transcribe` 也跳过。于是 `/start` 一路跑到
    `review` 才炸，被包成 **E9001**（"程序遇到一个未预期的问题"）。
    而这不是系统故障，是**输入形式没被接住**，指成系统错误排查会跑偏。

    修法：在 API 层（唯一同时知道"用户给了什么"和"状态机需要什么"的地方）
    把 `record/vN.md` 反推成行级分段落盘，形状与 `transcribe` 的产出对齐。
    反推是**纯函数**（`domain/review/record_source.py`），本层只负责 IO。

    三种情况：
      · 已有 `transcript/segments.json` -> 什么都不做（音频路 / 重跑）
      · 只有 `record/vN.md`            -> 反推并落盘（**必须有段落**）
      · 两者都没有                      -> 抛 E1004，把话说清楚

    最后一种是最该拦的：什么都不给就点"开始处理"，与其让用户等一轮
    再看到一个内部错误，不如当场告诉他"这次没有可处理的文稿"。
    """
    from mmwf.infra.filestore import make_filestore

    store = make_filestore(meeting_id, cfg.resolve_path(cfg.app.data_dir))
    from mmwf.graph.nodes.transcribe import SEGMENTS_PATH

    if store.exists(SEGMENTS_PATH):
        return

    record_rel = _latest_record_rel(store)
    if record_rel is None:
        raise EmptyTranscriptError(
            f"meeting_id={meeting_id!r} 既没有音频也没有记录稿，无法开始处理"
        )

    from mmwf.domain.review import record_source

    markdown = store.read_text(record_rel)
    payload = record_source.segments_payload(markdown)
    if not payload["segments"]:
        # 有记录稿但正文全被过滤光了（只有标题/空行）。
        # 落一份空 segments 文件比不落更糟：`load_segments` 会成功返回 0 段，
        # 下游渲染出一篇空记录稿，而没人知道问题出在输入上。
        raise EmptyTranscriptError(
            f"记录稿 {record_rel} 里没有可用的正文段落（只有标题或空行）"
        )

    store.write_json(SEGMENTS_PATH, payload)

    logger.info(
        "文字稿导入：已把 %s 反推为 %d 个转写分段（满足 review 的输入契约）",
        record_rel, len(payload["segments"]),
    )
