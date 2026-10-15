"""节点公共骨架（T1-10）。

11 个节点长得都不一样，但有**五件事**是每个节点都必须做的。
本模块把它们收在一处，节点实现只管业务那一段。

五个共性
--------

1. **入口校验**（`require_inputs`）
   每个节点需要的前置状态字段写在 `state.NODE_REQUIRES` 里。缺了立刻报错，
   而不是等 `KeyError` 飘到一半 —— 那时栈里已经看不出是哪个节点的问题。

2. **状态打点**（`node_status`）
   节点开始执行时把任务状态写进 `task_state`，界面才能显示"正在梳理议题"。
   没有这一步，用户看到的永远是 `created`。

3. **异常翻译**（`guard`）
   领域层抛的 `MmwfError` 带错误码与中文文案，直接放行；
   其它异常（`RuntimeError` / `OSError` …）是"程序写错了"或"环境坏了"，
   包成 E9001 再抛 —— 否则会一路冒到 API 层变成英文 500。

4. **产物返回的是增量**（不是完整状态）
   LangGraph 的节点返回 `dict` 表示"这些键变了"，没返回的键保持原值。
   返回 `{}` 表示不改（闸口 approve 就是这么干的）。

5. **副作用节点走幂等**（`idempotent`）
   导出、归档这类"做了就留下痕迹"的节点必须走 `IdempotencyStore`。

⚠️ 一个刻意的**不做**
--------------------
`_base` **不**在每个节点前后写审计。原因是闸口节点会被重放
（见 `interrupts` 模块文档），而审计写在节点里就会写两次。
审计一律由"发生副作用的那一步"自己写（见 `nodes/export.py` 的
`_audit`），位置明确、只发生一次。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from mmwf.domain.errors import MmwfError
from mmwf.graph import idempotency as idem
from mmwf.graph import state as S

logger = logging.getLogger(__name__)


class NodeInputError(MmwfError):
    """节点前置输入缺失 —— 编排 bug，不是用户错。"""

    def __init__(self, node_id: str, missing: list[str]) -> None:
        super().__init__(
            code="E9001",
            detail=(
                f"节点 {node_id} 缺少前置输入 {missing}；"
                f"它要求 {list(S.NODE_REQUIRES.get(node_id, ()))}。"
                "这通常意味着图的连边顺序被改动了。"
            ),
            context={"node": node_id, "missing": missing},
        )


class DependencyMissingError(MmwfError):
    """外部依赖未安装 / 未就绪（模型、soffice、ffmpeg …）。

    与"业务失败"分开：依赖缺失是**环境**问题，用户自己改不了，
    文案要指向"请联系技术人员"。
    """

    def __init__(self, what: str, detail: str = "") -> None:
        super().__init__(
            code="E9001",
            detail=detail or f"{what}不可用（依赖未安装或模型文件缺失）",
            context={"dependency": what},
        )


# ---------------------------------------------------------------------------
# 入口校验
# ---------------------------------------------------------------------------

def _is_blank(value: Any) -> bool:
    """空值判定：`None` / `[]` / `{}` / `""` / 只有空白都算空。

    ⚠️ 空白串**必须在"空"之列**。第一版只比 `in (None, [], "")`，
    于是 `"   "` 被判为"有输入" —— 节点会拿着三个空格继续跑，
    产出一篇空纪要，而不是在入口停下报"缺少记录稿"。
    这类"看着非空、实则无用"的输入是最难查的一类（2026-09-27 记忆日志）。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


#: 这些字段是**追加语义**的列表：`extract` 从记录稿里抽不出决议时，
#: 它会**合理地**留下一个空列表 —— 那是一条"本次会议没有形成决议"
#: 的正常结论，不是"上游没跑"。
#:
#: ⚠️ 为什么必须区分这两种情况（T1-11 Review 补的**致命缺陷**）：
#:
#: T1-07 定的 `missing_inputs` 只看 `_is_blank()`，把 `[]` 一律判为"缺"。
#: 于是 `generate` 的 `require_inputs` 在"记录稿里没有决议句"时会抛
#: `NodeInputError: 节点 generate 缺少前置输入 ['resolutions']`。
#: 报错文案说"这通常意味着图的连边顺序被改动了" —— **指向完全错误
#: 的方向**：连边没问题，是抽取结果本来就是空的。
#:
#: 后果不是某个边角 case，而是**主线断掉**：
#:   · 一份只含议题（没有明确决议用语）的会议记录是**极常见**的；
#:   · 这类会议一律在 G2 approve 后炸成 E9001，
#:     用户看到"程序遇到一个未预期的问题"，永远拿不到纪要。
#:
#: 判据不是"字段名"而是"**产出方是否跑过**"：如果产出该字段的节点
#: （见 `NODE_OUTPUTS` 反查）已经执行过，那么"字段存在但是空"
#: 就是正常结果；只有"字段压根不在状态里"（`None`）才是缺输入。
#:
#: 实测（`C:/tmp/probe17.py`）：一份 3 句的记录稿抽出
#: `topics=2 / resolutions=0 / action_items=1`，G2 送审 payload 里
#: `resolutions` 是 `[]` —— 闸口能正常展示，却在下游被自己的
#: 空值校验拦住。这类"两个模块对同一个 `[]` 理解不同"的坑，
#: 靠读单一文件是看不出来的，必须端到端跑一遍。
_APPEND_ONLY_FIELDS: frozenset[str] = frozenset({
    "topics", "resolutions", "action_items", "transcript", "audio_paths",
    "export_paths", "errors", "validation_issues", "warnings",
})


def _producer_ran(field: str, state: dict) -> bool:
    """产出 `field` 的节点是否已经跑过（用它的**其他**产物或状态打点判断）。

    用两条独立证据，任一成立即可：
      · 该节点要求的**输入**已经在了（`NODE_REQUIRES`）—— 说明它被调起过；
      · 该节点的**其他产物**里有一个非空白（`NODE_OUTPUTS`）—— 说明它跑完了。
    """
    producers = [
        node for node, outs in S.NODE_OUTPUTS.items() if field in outs
    ]
    for node in producers:
        for req in S.NODE_REQUIRES.get(node, ()):
            if not _is_blank(state.get(req)):
                return True
        others = [o for o in S.NODE_OUTPUTS.get(node, ()) if o != field]
        if any(not _is_blank(state.get(o)) for o in others):
            return True
    return False


def missing_inputs(node_id: str, state: dict) -> list[str]:
    """列出该节点缺失的前置字段。

    规则（顺序很重要）：

        · 字段**不在状态里**（`None`）        -> 缺（上游没跑）
        · 空白**字符串**                      -> 缺（"看着非空、实则无用"）
        · **空的追加型列表**且产出方跑过了     -> **不缺**（"本次就是没有"）
        · **空的追加型列表**但产出方没跑过     -> 缺（上游真的没跑）
        · 其余空容器                           -> 缺（沿用 T1-07 的严格判定）

    最后一条保留严格是有意的：`minutes_structured` 这类"一个对象"的
    字段为空，几乎只能是上游没产出。
    """
    from mmwf.graph.state import NODE_REQUIRES as _REQ

    missing: list[str] = []
    for k in _REQ.get(node_id, ()):
        value = state.get(k)
        if value is None:
            missing.append(k)
            continue
        if isinstance(value, (list, tuple, set)) and len(value) == 0:
            # 追加型字段 + 产出方跑过 = 这就是正常结果，放行
            if k in _APPEND_ONLY_FIELDS and _producer_ran(k, state):
                continue
            missing.append(k)
            continue
        if _is_blank(value):
            missing.append(k)
    return missing


def require_inputs(node_id: str, state: dict) -> None:
    """缺前置输入就抛 `NodeInputError`。"""
    missing = missing_inputs(node_id, state)
    if missing:
        raise NodeInputError(node_id, missing)


# ---------------------------------------------------------------------------
# 状态打点
# ---------------------------------------------------------------------------

def task_state_of(node_id: str) -> str:
    """节点 -> 该节点执行时任务应处的状态。"""
    return S.NODE_STATUS.get(node_id, S.STATUS_CREATED)


def touch_task_state(
    db_path: Path | str,
    task_id: str,
    node_id: str,
    *,
    awaiting_actor: str | None = None,
    awaiting_since: str | None = None,
    last_error: str | None = None,
    now: Callable[[], str] | None = None,
) -> None:
    """把 `task_state` 推进到该节点的状态（upsert）。

    **不抛错**：`task_state` 是给界面看的进度表，不是业务真相。
    写失败（比如库被锁）不该让一次跑得好好的流水线失败 ——
    顶多是界面上的进度条卡住。所以这里只记日志。
    """
    from mmwf.infra import db as dbmod

    stamp = (now or _now_iso)()
    status = task_state_of(node_id)
    try:
        with dbmod.connect_ctx(db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_state
                    (task_id, current_node, status, awaiting_actor,
                     awaiting_since, retry_count, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    current_node   = excluded.current_node,
                    status         = excluded.status,
                    awaiting_actor = excluded.awaiting_actor,
                    awaiting_since = excluded.awaiting_since,
                    last_error     = excluded.last_error,
                    updated_at     = excluded.updated_at
                """,
                (task_id, node_id, status, awaiting_actor, awaiting_since,
                 last_error, stamp),
            )
    except Exception:  # pragma: no cover - 只可能在库异常时走到
        logger.warning("task_state 打点失败（不影响流水线）：%s/%s",
                       task_id, node_id, exc_info=True)


def read_task_state(db_path: Path | str, task_id: str) -> dict[str, Any] | None:
    """读一行 `task_state`（供 runner / 验收脚本查询）。"""
    from mmwf.infra import db as dbmod

    with dbmod.connect_ctx(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM task_state WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 异常翻译
# ---------------------------------------------------------------------------

def translate_exception(node_id: str, exc: BaseException) -> MmwfError:
    """把任意异常翻译成带错误码的 `MmwfError`。

    `MmwfError` 原样返回（它已经有码和中文文案了）。

    其余异常包成 **E9001**（SAD §9 的"未预期错误"）：
    这类异常的原文往往是英文的（`FileNotFoundError: [Errno 2] ...`），
    直接给用户看没有意义，所以 `user_message` 由码表给，
    `detail` 留原文给日志。
    """
    if isinstance(exc, MmwfError):
        return exc
    return MmwfError(
        code="E9001",
        detail=f"节点 {node_id} 发生未预期错误：{type(exc).__name__}: {exc}",
        context={"node": node_id, "exc_type": type(exc).__name__},
    )


def guard(node_id: str, fn: Callable[[], Any]) -> Any:
    """执行 `fn`，把非业务异常翻译掉。"""
    try:
        return fn()
    except MmwfError:
        raise
    except Exception as exc:
        raise translate_exception(node_id, exc) from exc


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def idempotent(
    store: idem.IdempotencyStore | None,
    node_id: str,
    state: dict,
    fn: Callable[[], Any],
    *,
    result_ref_of: Callable[[Any], str | None] | None = None,
) -> tuple[Any, bool]:
    """在幂等保护下执行 `fn`。

    `store` 为 None 时**直接执行**（不保护）。这不是偷懒：
    单测与本地探查常不需要落一张幂等记录表，强制要求 store
    会让每个测试都要先建库。生产路径由 `build.py` 一定注入 store。

    Returns:
        `(结果, 是否命中既有记录)`。**未命中时"结果"是 `fn` 的返回值本身**
        （不是引用）—— 引用要从 `store.get(key).result_ref` 读。
    """
    if store is None:
        return fn(), False
    task_id = str(state.get("task_id") or state.get("meeting_id") or "")
    if not task_id:
        raise MmwfError(
            code="E9001",
            detail=f"节点 {node_id} 无法构造幂等键：状态里没有 task_id / meeting_id",
        )
    node_input = idem.node_input_from_state(node_id, state)
    return store.get_or_run(
        task_id, node_id, node_input, fn, result_ref_of=result_ref_of
    )


# ---------------------------------------------------------------------------
# 会话 / 路径
# ---------------------------------------------------------------------------

def session_of(meeting_id: str, settings: Any, *, operator: str = "system") -> Any:
    """按 `meeting_id` 组装一套"会话"（store + db 路径 + 审计）。

    节点不各自去拼路径：`data_dir` 该由 `settings.resolve_path` 解析，
    每处都拼一遍的话，改一次目录结构要改 11 个节点。
    """
    from mmwf.infra.audit import AuditLogger
    from mmwf.infra.filestore import make_filestore

    data_dir = settings.resolve_path(settings.app.data_dir)
    data_dir = Path(data_dir)
    db_path = data_dir / "mmwf.db"
    return _Session(
        meeting_id=meeting_id,
        data_dir=data_dir,
        db_path=db_path,
        store=make_filestore(meeting_id, data_dir),
        audit=AuditLogger(db_path),
        operator=operator,
        settings=settings,
    )


class _Session:
    """一次流水线运行的会话上下文（store / db / audit）。"""

    __slots__ = ("meeting_id", "data_dir", "db_path", "store",
                 "audit", "operator", "settings")

    def __init__(self, *, meeting_id, data_dir, db_path, store, audit,
                 operator, settings) -> None:
        self.meeting_id = meeting_id
        self.data_dir = data_dir
        self.db_path = db_path
        self.store = store
        self.audit = audit
        self.operator = operator
        self.settings = settings

    def rel(self, *parts: str) -> str:
        """拼一个存储层用的相对路径。"""
        return "/".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Session {self.meeting_id} @ {self.data_dir}>"


def meeting_id_of(state: dict) -> str:
    """从状态取会议号。`task_id` 与 `meeting_id` 一期是同一个值。"""
    got = state.get("meeting_id") or state.get("task_id")
    if not got:
        raise MmwfError(
            code="E9001",
            detail="状态里既没有 meeting_id 也没有 task_id，无法定位任务",
        )
    return str(got)


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------

def append_error(state: dict, code: str, node_id: str, detail: str) -> list[dict]:
    """向 `errors` **追加**一条（LangGraph 对 list 字段默认是替换，不是追加）。"""
    prev = list(state.get("errors") or [])
    prev.append({"code": code, "node": node_id, "detail": detail})
    return prev


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: 状态里的"不含产物"的初始键。首次 `invoke` 必须给非空 dict，
#: 否则 TypedDict + total=False 会报 `InvalidUpdateError`。
INITIAL_STATE: dict[str, Any] = {"errors": [], "_gate_route": None}


def initial_state(meeting_id: str, **extra: Any) -> dict[str, Any]:
    """构造首次 `invoke` 的初始状态。"""
    base = dict(INITIAL_STATE)
    base["task_id"] = meeting_id
    base["meeting_id"] = meeting_id
    base["stage"] = S.STATUS_CREATED
    base.update(extra)
    return base


__all__ = [
    "DependencyMissingError",
    "INITIAL_STATE",
    "NodeInputError",
    "append_error",
    "guard",
    "idempotent",
    "initial_state",
    "meeting_id_of",
    "missing_inputs",
    "read_task_state",
    "require_inputs",
    "session_of",
    "task_state_of",
    "touch_task_state",
    "translate_exception",
]
