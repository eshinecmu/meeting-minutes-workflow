"""路由层共享常量与无状态工具（原 `api/routes.py` 的常量区与工具区）。

**收录标准**：只放**不依赖 `Runtime`**的东西 —— 常量、纯函数，以及
只需要 `db_path` / `Settings` 的小工具。一个函数只要需要访问服务
（ingest / export / runner / 各 store），就不属于本模块。

⚠️ 这些常量原先是 `routes.py` 的模块级符号，被单测与验收脚本直接导入
（`AUDIT_MAX_LIMIT`、`ALLOWED_DECISIONS`、`_decision_message` …）。
拆包后由 `routes/__init__.py` 原样 re-export，外部 import 路径不变。
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mmwf.domain.errors import MmwfError
from mmwf.infra.db import connect_ctx
from mmwf.settings import Settings

logger = logging.getLogger(__name__)


#: 闸口名 -> 流水线里的节点名。API 用 "G2"/"G3"，图上用 "gate_g2"/"gate_g3"。
#: 这层映射必须显式写出来：直接拼 f"gate_{gate.lower()}" 看着能work，
#: 但一旦将来闸口改名（比如加 G4），拼出来的节点名会静默不存在。
GATE_NODE_MAP = {"G2": "gate_g2", "G3": "gate_g3"}

#: 允许的闸口决策。与 `graph/interrupts.py` 的 `_apply_decision` 三分支一一对应。
ALLOWED_DECISIONS = frozenset({"approve", "reject", "edit"})

#: 密级取值（与 meeting.classification 的 CHECK 约束一致）
CLASSIFICATIONS = frozenset({"public", "internal", "work_secret", "state_secret"})

#: 审计检索的**硬上限**（T1-14 裁决 D7）。
#:
#: 不设上限 = 给了一个"把整库拉进内存"的接口。1000 是任选的天花板，
#: 依据是：审计日志保留 3 年，一次人工核查要看的是"某个时间段/某个人"
#: 的记录，不会有人真的想一次看 10 万条。
AUDIT_MAX_LIMIT = 1000

#: 审计里 `operator` 缺省值。前端一般会传，但命令行/验收脚本常常不传。
DEFAULT_OPERATOR = "system"

#: 转写稿分页的默认页大小（T1-15）。
#:
#: 500 的依据：一份 3 小时会议约 800~1200 段，500 段大约对应 1~1.5 小时。
#: 一次性给全篇会让响应体到几 MB，前端渲染也要几秒 —— 而复核是**从头看到尾**
#: 的动作，用户本来就会一页一页翻。
TRANSCRIPT_DEFAULT_LIMIT = 500

#: 转写稿分页的硬上限（T1-15）。
#:
#: 与 `AUDIT_MAX_LIMIT` 同理：不设上限等于给了个"把全库拉进内存"的接口。
#: 2000 段已经覆盖了 SAD 的规模假设（SAD §2 单次会议 ≤ 4 小时的量级）。
TRANSCRIPT_MAX_LIMIT = 2000

def _now() -> str:
    """统一时间戳格式：带时区的 ISO8601。

    不用 `datetime.now()`（无时区）—— 一期虽然只在本机跑，
    但 `meeting_time` 是用户填的（可能带 +08:00），
    如果系统侧记的是 naive 时间，两者比较时 Python 会直接抛
    `TypeError: can't compare offset-naive and offset-aware`。
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def _config_dir(cfg: Settings) -> Path:
    """配置目录（`config/`）。

    T1-16 起被 `_template_store()` 用来定位内置模板目录
    （`config/templates/*.yaml`）。

    ⚠️ 用 `cfg.config_dir` 而**不是** `cfg.resolve_path(...)` ——
    `config_dir` 已经是绝对路径（`settings._find_config_dir` 解析过），
    再走一次 `resolve_path`（它是给"相对项目根"的路径用的）会把它
    当成相对路径再拼一次，拼出一个不存在的目录。
    症状是"内置模板一个都读不到"，而且**不报错**（目录不存在 → 返回空）。
    """
    return Path(cfg.config_dir)

def _chunk_dir(cfg: Settings, meeting_id: str) -> Path:
    """切分产物目录：`{data_dir}/{meeting_id}/audio/chunks`。

    ⚠️ **这个目录约定必须与 `ingest` 节点一致**
    （`graph/nodes/ingest.py` 的 `CHUNK_DIR = "audio/chunks"`）。
    不一致的后果很隐蔽：投递时切分写到 A 处，转写节点去 B 处读，
    `transcribe` 会看到**空目录** → 报"没有可转写的音频"，
    而用户在投递那一步看到的是"已接收 6 个切分块"。

    为什么不 import 那个常量：`graph/nodes/*` 属于流水线层，
    API 层引它会把两层绑在一起（`check_layers` 没禁，但这是坏味道）。
    这里**显式重写**一遍并在此处留下"必须与 X 一致"的注记 ——
    两处字符串，一处含义，靠这条注释维系。
    """
    return cfg.resolve_path(cfg.app.data_dir) / meeting_id / "audio" / "chunks"

def _require_meeting(meeting_id: str, db_path: Any) -> dict[str, Any]:
    """取会议行；不存在时抛业务异常。

    为什么统一在这里抛、而不是让端点各自 `if not row`：
    会议不存在是**所有** `/{meeting_id}/...` 端点的共同前置条件，
    散落各处必然文案不一致（用户会看到"找不到会议"/"会议不存在"
    /"无效 id" 三种说法）。

    ⚠️ T1-15 修：这里原来用 `FileMissingError`，结果是用户删/拼错
    `meeting_id` 时看到 E1002 的默认文案
    「**找不到要处理的文件**。请确认文件没有被移动或删除。」
    —— 而这里根本没有文件。错误码对，文案把人往"去找文件"的方向带。

    根因是 `FileMissingError.__init__(self, detail="", **ctx)` 会把
    **所有**关键字参数吞进 `context`，包括 `user_message`；
    子类不重写构造函数就改不了文案。所以改用 **`MmwfError` 基类**
    显式构造 —— `code` 仍是 E1002（错误码不变，`accept_t1_11.py`
    与两个单测断言的都是这个码）。
    """
    with connect_ctx(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM meeting WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise MmwfError(
            code="E1002",
            detail=f"meeting_id={meeting_id!r} 不存在",
            user_message=(
                "找不到这次会议。可能是它已经被删除，或者链接里的编号不对。"
                "请回到会议列表重新打开。"
            ),
            context={"area": "meeting", "meeting_id": meeting_id},
        )
    return dict(row)

def _meeting_of_segment(segment_id: str, db_path: Any) -> str:
    """按 `segment_id` 反查它属于哪个会议（T1-15）。

    ★ 为什么必须有这个函数：

    SAD §9.1 把编辑端点定成 `PATCH /segments/{seg_id}` —— 路径里
    **没有 `meeting_id`**。而 `TranscriptStore` 是**按会议构造**的
    （它的每个查询都带 `WHERE meeting_id = ?`），所以要先知道会议。

    ⚠️ **不能遍历所有会议去"碰运气"**：错误信息会退化成
    "在会议 A 里没找到、在会议 B 里也没找到…"，而正确答案是
    "这个 id 根本不存在"。反查一次直达结论。

    ⚠️ **不能改用 `TranscriptStore.get_segment()` 来判断存在性** ——
    它的 SQL 是 `WHERE segment_id = ? AND meeting_id = ?`，
    需要先有 `meeting_id`，是**循环依赖**。这里只取"归属"这一个字段，
    不取内容（内容由后面的 `edit_segment` 走正规通道读）。

    ⚠️ **这句 SQL 是本文件唯一的例外**（API 层不许 `import sqlite3`，
    但 `connect_ctx` 是 `infra/db` 的封装，分层检查放行）。
    之所以不塞进仓储层：它是一个"跨会议"的查询，而 `TranscriptStore`
    的每个方法都**以会议为前提**，把它放进去会破坏那个不变量。

    ⚠️⚠️ **报错必须用 `MmwfError` 基类构造，不能用 `FileMissingError`**。
    `FileMissingError.__init__(self, detail="", **ctx)` —— 它把**所有**
    关键字参数吞进 `context`（见 `domain/errors.py`），包括 `user_message`。
    于是 `FileMissingError("找不到这段", user_message="人话")` 的
    `user_message` 仍然是 E1002 的**默认文案**：
    「找不到要处理的文件。请确认文件没有被移动或删除。」

    实测（T1-15 冒烟）：改一个不存在的 `segment_id`，用户看到的是
    **"找不到要处理的文件"** —— 而这里根本没有文件。
    错误码对（E1002），文案完全指错方向。
    这与 T1-14 的坑 10 同源（子类静默吞掉 kwargs）。
    所以这里显式用**基类** `MmwfError(code="E1002", ...)` 构造。
    """
    with connect_ctx(db_path) as conn:
        row = conn.execute(
            "SELECT meeting_id FROM transcript_segment WHERE segment_id = ?",
            (segment_id,),
        ).fetchone()
    if row is None:
        raise MmwfError(
            code="E1002",
            detail=f"segment_id={segment_id!r} 不在 transcript_segment 中",
            user_message=(
                "找不到这一段转写稿。可能是它已经被删除，或者会议重新转写过。"
                "请刷新页面后重试。"
            ),
            context={"area": "transcript", "segment_id": segment_id},
        )
    return str(row["meeting_id"])

def _decision_message(decision: str) -> str:
    """决策对应的用户提示。文案固定，别在端点里现编 —— 见 SAD §5.3。

    特别强调 `edit` 说成「改完继续」而不是「已重做」：
    用户最怕"我改了一个字，之前的活全白干"，而 `edit` 恰恰不重跑。
    """
    return {
        "approve": "已确认，继续处理",
        "edit": "已按您的修改继续，未重做之前的工作",
        "reject": "已退回重做",
    }[decision]

def _media_type_for(filename: str) -> str:
    """按扩展名给 Content-Type。**只认已知几种**，其余给 octet-stream。

    为什么不引 `mimetypes`：它在本机会去读注册表，
    同一个 `.doc` 在不同机器上可能给 `application/msword` 或
    `application/octet-stream`，**结果不可复现**。
    本系统只导几种文件，写死比查表更稳。
    """
    lower = filename.lower()
    if lower.endswith(".docx"):
        return (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )
    if lower.endswith(".doc"):
        return "application/msword"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if lower.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"

def _load_recent_segments(db_path: Path | str, *, limit: int = 500) -> list[dict[str, Any]]:
    """读最近一批转写分段（供自检评估 ASR 质量）。

    为什么取"最近 N 段"而不是"全部"或"某次会议的"：
      · 自检关心的是"**当前这台机器的转写质量如何**"，
        最近一段时间的样本比全历史更能反映现状
        （换了模型/换了硬件后，旧样本会稀释判断）；
      · 端点会被前端轮询，读全表在大会量下是浪费。

    ⚠️ **只取 `confidence IS NOT NULL` 的行**：`confidence` 允许为 NULL
    （"引擎未提供"，见 001_init.sql 的列注释），把 NULL 混进来会让
    "平均置信度"变成 None 或者被当 0 算 —— 两种都是错的。
    过滤掉才是"只用可判定的样本"。

    返回 dict 列表而非 sqlite3.Row：Row 出了连接就失效，
    而"读取"与"使用"之间隔着自检的其余步骤。
    """
    sql = (
        "SELECT confidence, flags, seq FROM transcript_segment "
        "WHERE confidence IS NOT NULL "
        "ORDER BY seq DESC LIMIT ?"
    )
    with connect_ctx(db_path) as conn:
        rows = conn.execute(sql, (max(1, min(int(limit), 5000)),)).fetchall()
    return [dict(r) for r in rows]

def _latest_record_rel(store: Any) -> str | None:
    """取最新一版记录稿的相对路径（`record/vN.md`），没有则 None。

    版本号从文件名解析，**不用** `ReviewService.latest_version()` ——
    那个要构造 service（带阈值配置），而这里只想知道"有没有文件"。
    """
    root = store.store_root / "record"
    if not root.is_dir():
        return None
    versions: list[tuple[int, str]] = []
    for p in root.glob("v*.md"):
        body = p.stem[1:]
        if body.isdigit():
            versions.append((int(body), f"record/{p.name}"))
    return max(versions)[1] if versions else None

def _make_tmp_dir(cfg: Settings) -> Path:
    """在 `data_dir/.tmp` 下建一个受控临时目录（T1-14）。

    ⚠️ **不放系统 `/tmp`**。理由与 `filestore.plaintext()` 完全相同：
    `/tmp` 不受 `data/` 的目录权限与磁盘加密保护，而这里要落的是
    **术语库**（含单位专有名词）与**审计报告**（含全部操作记录）。
    放进系统临时目录 = 把敏感内容丢到一个谁都可能读的地方。

    调用方**必须**在 `finally` 里 `shutil.rmtree(..., ignore_errors=True)`。
    """
    parent = Path(cfg.resolve_path(cfg.app.data_dir)) / ".tmp"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="api-", dir=parent))

def _slug(entry_id: str) -> str:
    """给 `data_dir/.tmp` 下的临时目录起个安全的名字。

    `entry_id` 里可能有 `:`（我们自己的生成规则）或更糟的东西，
    直接当目录名在 Windows 上会炸（`:` 是保留字符）。
    """
    safe = "".join(ch if ch.isalnum() else "_" for ch in entry_id)
    return (safe[:40] or "glossary") + "_" + uuid.uuid4().hex[:8]
