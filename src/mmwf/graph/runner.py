"""流水线运行入口（T1-10，SAD §5.1 / F6.2）。

`build.py` 产出图，本模块负责**跑图**。分工：

    build.py   —— 图的形状（节点、连边、路由表）。纯静态。
    runner.py  —— 一次具体的运行：投递、暂停、恢复、取消、查进度。

一次运行的三个入口
------------------

    `start()`   —— 首次投递。跑到第一个闸口停下（或一路跑完）。
    `resume()`  —— 人在闸口做了决策后继续。
    `status()`  —— 查当前停在哪、在等谁、已完成哪些产物。

外加两个运维动作：`cancel()`（取消，**保留中间产物**）与
`retry()`（从失败处重试）。

⚠️ 这个模块存在的核心理由：把三个易错约定收在一处
-------------------------------------------------
1. **`thread_id` = `meeting_id`**。散在各处的话，某一次调用漏传
   或传错，表现是"恢复后从头发起了一遍"（因为找到了一个空线程），
   而不是报错 —— 极难排查。
2. **检查点连接按任务开**，不共用（见 `build.make_checkpointer`）。
3. **首次 `invoke` 必须给非空初始状态**。`PipelineState` 是
   `total=False` 的 TypedDict，空 dict 会被 LangGraph 拒
   （`InvalidUpdateError: Must write to at least one of [...]`）。

关于 `interrupt` 的检测
----------------------
`app.invoke()` 首次返回时**拿不到中断信息**（实测：返回值中
`__interrupt__` 为 `None`）。可靠的方式是读
`app.get_state(cfg).tasks[*].interrupts`。本模块把这个细节封在
`_pending_interrupt()` 里，调用方只看 `PendingGate` 对象。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from mmwf.domain.errors import MmwfError
from mmwf.graph import state as S
from mmwf.graph.build import Deps, build_pipeline, make_checkpointer, thread_config
from mmwf.graph.nodes import _base as B

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PendingGate:
    """当前挂起的闸口（等人决策）。"""

    gate: str
    gate_label: str
    risk: str
    payload: dict
    evidence: list[dict]
    node_id: str
    checkpoint_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "gate": self.gate,
            "gate_label": self.gate_label,
            "risk": self.risk,
            "payload": self.payload,
            "evidence": self.evidence,
            "node_id": self.node_id,
        }


@dataclass
class RunStatus:
    """一次运行的当前状况（供 API 层直接序列化）。"""

    meeting_id: str
    status: str
    status_label: str
    current_node: str | None = None
    pending_gate: PendingGate | None = None
    awaiting_actor: str | None = None
    products: dict[str, bool] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    gen_retry: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in S.TERMINAL_STATUSES

    @property
    def is_waiting(self) -> bool:
        return self.status in S.GATE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "status": self.status,
            "status_label": self.status_label,
            "current_node": self.current_node,
            "pending_gate": self.pending_gate.to_dict() if self.pending_gate else None,
            "awaiting_actor": self.awaiting_actor,
            "products": dict(self.products),
            "errors": list(self.errors),
            "gen_retry": self.gen_retry,
            "is_terminal": self.is_terminal,
            "is_waiting": self.is_waiting,
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class GraphRunner:
    """一次会议任务的运行器。

    Args:
        meeting_id: 会议标识。也是 `thread_id`（一期一对一）。
        deps: 依赖容器（见 `build.Deps`）。
        checkpoint_path: 检查点库路径。默认落在会议数据目录下。
        app: 已编译的图（测试可注入）。`None` 时按 deps 构造。
    """

    def __init__(
        self,
        meeting_id: str,
        *,
        deps: Deps | None = None,
        checkpoint_path: Path | str | None = None,
        app: Any = None,
        audit: Any = None,
    ) -> None:
        if not meeting_id:
            raise ValueError("meeting_id 不能为空")
        self.meeting_id = meeting_id
        self.deps = deps or Deps()
        self._audit = audit if audit is not None else getattr(self.deps, "audit", None)
        self._cfg = thread_config(meeting_id)
        self._ckpt_path = Path(checkpoint_path) if checkpoint_path else None
        self._saver = None
        self._app = app
        #: 图是否由本实例编译（决定 `close()` 是否有权把它丢弃）。
        #:
        #: ⚠️ T1-17 缺陷 14：外部注入的 `app`（测试常这么传）**不是**我们的，
        #: `close()` 里不能把它置 None —— 否则调用方手里的图的引用
        #: 会在 runner 析构后失效，表现为"莫名 UnboundLocalError"。
        #: 但**连接**（`_saver`）永远归本实例管，无论图是谁的。
        self._owns_app = app is None
        #: 已取消/已失败（图恢复不了这些"外部"状态，只能记在这里 + 库里）。
        #:
        #: ⚠️ **必须记在 runner 上**，不能只写 `task_state` 表。
        #: 只写库的话 `status()` 读的是图状态（`stage`），
        #: 于是"取消"在下一次查询里**凭空消失**、又变回 `G2_awaiting` ——
        #: 表现是：取消返回 `cancelled`，紧接着 `status()` 说还在等确认，
        #: `resume()` 里的 E4005 守卫永远不触发，已取消的任务照样能提交决策。
        self._terminal_override: str | None = None
        self._cancel_reason: str = ""
        #: 各节点执行次数（图外计数，见 `_bump_node`）。
        self._runs: dict[str, int] = {}

    # -- 生命周期 ------------------------------------------------------

    def __enter__(self) -> "GraphRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 兜底，正常路径靠 close()
        """兜底释放（**只兜底，不依赖它**）。

        为什么不依赖 `__del__` 释放 sqlite 连接：CPython 之外的实现
        （PyPy 等）回收时机不确定，且解释器退出期模块可能已被拆掉。
        正常路径一律走 `with GraphRunner(...)` 或显式 `close()`。
        这里存在只是为了**忘记 `with` 时不留悬挂连接**（T1-17 缺陷 14）。
        """
        try:
            self.close()
        except Exception:
            # 解释器退出期什么都可能发生，兜底路径**绝不抛**
            pass

    def close(self) -> None:
        """释放检查点连接。**必须调用**（连接不是全局的）。

        ⚠️ T1-17 缺陷 14：**复位 `_app` 和复位 `_saver` 必须同时做**。
        ================================================================

        第一版只把 `_saver` 置 None，不动 `_app` —— 于是出现两种坏情况：

        ① **关不掉**：`_app` 由外部注入（`GraphRunner(app=...)`）时
           `_saver` 一直是 None，`close()` 什么也没关 → **连接泄漏**。
        ② **关错了**：`close()` 之后若再有人摸 `.app`，因为 `_app` 还不是
           None，属性直接返回**那个已经关掉的图** —— 后续调用抛
           `ProgrammingError: Cannot operate on a closed database`，
           而且报错点在几十行以外，极难定位。

        正确做法：两者**一起清**。`_app` 置回 None 之后，
        下次访问 `.app` 会重新编译并**重新建连**（惰性，不需要提前建）。
        `close()` 因此变成**幂等**的：连调两次不抛。
        """
        if self._saver is not None:
            try:
                self._saver.conn.close()
            except Exception:  # pragma: no cover
                pass
            self._saver = None
        # ★ 关键：图与连接必须一起失效，否则 ② 那种"拿着已关的连接"的情况必然出现。
        #   注意只在**我们自己建的**图上复位；外部注入的 `app` 由调用方负责，
        #   但连接归我们管 —— 所以外部注入时至少把 `_saver` 清掉（上面已做）。
        if self._owns_app:
            self._app = None

    @property
    def app(self):
        """惰性编译图（带检查点）。"""
        if self._app is None:
            if self._ckpt_path is not None:
                self._saver = make_checkpointer(self._ckpt_path)
                ckpt = self._saver
            else:
                ckpt = None
            self._app = build_pipeline(
                self.deps, checkpointer=ckpt, on_node=self._bump_node
            )
            #: 图由本实例编译 → 本实例有权在 `close()` 时把它一并丢弃。
            self._owns_app = True
        return self._app

    # -- 节点执行计数（T1-11）-------------------------------------------

    def _bump_node(self, name: str) -> None:
        """节点执行回调（由 `build_pipeline` 的 `on_node` 钩子调用）。

        计数挂在 runner 上、**不进图状态** —— 原因见 `state.py` 的注释：
        状态会被检查点持久化并在恢复时重放，计数器跟着重放一起累加，
        `rerun_nodes == []` 这类断言就永远算不准。
        """
        self._runs[name] = self._runs.get(name, 0) + 1

    def run_counts(self) -> dict[str, int]:
        """各节点累计执行次数（快照，改它不影响内部状态）。

        ⚠️ 这是**单次 runner 生命周期内**的计数，不是持久化的。
        跨进程/跨实例比较需要调用方自己拿两次快照做差 ——
        API 层算 `rerun_nodes` 就是这么做的。
        """
        return dict(self._runs)

    def reset_run_counts(self) -> None:
        """清零计数（提交闸口决策前调用，以便只看"这次决策触发了什么"）。"""
        self._runs.clear()

    # -- 运行 ----------------------------------------------------------

    def start(self, initial: dict[str, Any] | None = None,
              **extra: Any) -> RunStatus:
        """首次投递并跑到第一个闸口（或跑完）。

        Args:
            initial: 完整初始状态。给了就以它为准（会补上必备键）。
            **extra: 直接合并进初始状态的字段（如 `audio_paths=[...]`）。
        """
        state = B.initial_state(self.meeting_id)
        if initial:
            state.update(initial)
        state.update(extra)

        # 重新投递要清掉上一次的取消/失败标记 ——
        # 否则一次取消会把 runner 永久钉在 cancelled 上。
        self._terminal_override = None
        self._cancel_reason = ""

        # 有音频就从 ingest 开始，没有音频（文字稿导入）同样从 ingest 开始 ——
        # ingest 节点自己判断"没音频就跳过"（见 nodes/ingest.py）。
        logger.info("启动流水线：%s", self.meeting_id)
        self._invoke(state)
        return self.status()

    def resume(self, decision: dict[str, Any]) -> RunStatus:
        """在闸口提交决策后继续。

        Args:
            decision: `{"decision": "approve|reject|edit", ...}`。
                字段含义见 `interrupts.ResumeDecision`。

        Raises:
            MmwfError: E4001（非法决策 / 没有闸口在等）/ E4005（任务已取消）。
        """
        from langgraph.types import Command

        if not isinstance(decision, dict):
            raise MmwfError(
                code="E4001",
                detail=f"决策必须是对象，实际 {type(decision).__name__}",
            )
        # 取消过的任务不接受决策（E4005）
        cur = self.status()
        if cur.status == S.STATUS_CANCELLED:
            raise MmwfError(
                code="E4005",
                detail=f"任务已取消（{self.meeting_id}），不再接受闸口决策",
            )

        # ⚠️ **必须显式检查"确实有闸口在等"**，不能只靠 LangGraph 报错。
        #
        # 两种漏检的后果不同，但都不可接受：
        #   · 还没投递就 resume -> LangGraph 抛 `InvalidUpdateError`
        #     （"Must write to at least one of [...]"），再被 `_invoke`
        #     包成 **E9001 未预期错误** —— 用户看到的是"程序出错了"，
        #     而真相是"现在没有东西需要你确认"。错误码指错了方向，
        #     排查会跑偏。
        #   · **跑完之后再 resume -> 完全静默**（返回 archived，
        #     不报任何错）。这条最危险：调用方以为决策被采纳了。
        #     LangGraph 不报错是因为"没有下一步"本身不构成错误。
        #
        # 所以这里自己判断：当前必须挂在一个闸口上。U-ST-08 要求
        # "在非闸口状态提交决策 → E4001"，这两条路径都归到它。
        if cur.pending_gate is None:
            raise MmwfError(
                code="E4001",
                detail=(
                    f"当前没有等待确认的事项（任务状态：{cur.status_label}），"
                    "无法提交决策。决策只能在议题确认（G2）或纪要审定（G3）时提交。"
                ),
                context={"meeting_id": self.meeting_id, "status": cur.status},
            )

        logger.info("闸口决策：%s -> %s",
                    self.meeting_id, decision.get("decision"))
        self._invoke(Command(resume=decision))
        return self.status()

    def _invoke(self, payload: Any) -> None:
        """调 `invoke` 并落一次 `task_state` 打点。"""
        try:
            self.app.invoke(payload, self._cfg)
        except MmwfError as exc:
            from mmwf.domain.errors import GroundingBlockedError
            if isinstance(exc, GroundingBlockedError):
                values = self.values()
                if values.get("stage") == "validation_blocked":
                    self._touch(S.STATUS_FAILED, last_error=exc.user_message)
                    raise
                # 旧 next=assemble 的防御性硬检异常同样保留原稿与诊断。
                from mmwf.domain.minutes.issues import aggregate_issues
                issues = aggregate_issues(values.get("issues") or [], exc.context.get("issues", []))
                self.app.update_state(self._cfg, {"stage": "validation_blocked", "issues": issues,
                    "validation_issues": exc.context.get("issues", []),
                    "errors": list(values.get("errors") or []) + [exc.to_dict()]})
                self._touch(S.STATUS_FAILED, last_error=exc.user_message)
            raise
        except Exception as exc:
            raise B.translate_exception("runner", exc) from exc
        # validate 已正常完成并持久化诊断；终态异常不再进入节点异常处理分支。
        if self.values().get("stage") == "validation_blocked":
            from mmwf.domain.errors import GroundingBlockedError
            error = GroundingBlockedError()
            self._touch(S.STATUS_FAILED, last_error=error.user_message)
            raise error

    # -- 查询 ----------------------------------------------------------

    def status(self) -> RunStatus:
        """当前状况。"""
        snapshot = self.app.get_state(self._cfg)
        values = dict(snapshot.values or {})
        pending = _pending_interrupt(snapshot)
        # 取消/失败是**图外部**的状态：图停在闸口上并不知道自己被取消了，
        # 所以这里要让 override 压过 `_derive_status` 的结果（见 __init__）。
        if self._terminal_override is not None:
            status = self._terminal_override
            pending = None
        else:
            status = _derive_status(values, snapshot, pending)
        node = _current_node(snapshot, pending)
        return RunStatus(
            meeting_id=self.meeting_id,
            status=status,
            status_label=S.status_label(status),
            current_node=node,
            pending_gate=pending,
            awaiting_actor=values.get("operator") or None if pending else None,
            products={k: values.get(k) is not None for k in _all_products()},
            errors=list(values.get("errors") or []),
            gen_retry=int(values.get("gen_retry") or 0),
        )

    def checkpoint(self):
        """图外闸口服务读取持久化检查点及 created_at。"""
        return self.app.get_state(self._cfg)

    def values(self) -> dict[str, Any]:
        """当前状态的完整快照（排查用）。"""
        return dict(self.app.get_state(self._cfg).values or {})

    # -- 运维 ----------------------------------------------------------

    def cancel(self, *, reason: str = "") -> RunStatus:
        """取消任务。

        **保留已完成的中间产物**（施工卡 U-ST-10 / F6.6）：
        取消不等于删除 —— 用户可能只是想停下来，回头接着看。
        所以这里只置状态，**不清理任何产物**。

        取消后 `status()` 会持续返回 `cancelled`，
        且 `resume()` 一律报 E4005（不再接受闸口决策）。
        """
        logger.info("取消任务：%s（%s）", self.meeting_id, reason or "未填原因")
        self._cancel_reason = reason
        self._terminal_override = S.STATUS_CANCELLED
        self._touch(S.STATUS_CANCELLED)
        cur = self.status()
        return RunStatus(
            meeting_id=cur.meeting_id,
            status=S.STATUS_CANCELLED,
            status_label=S.status_label(S.STATUS_CANCELLED),
            current_node=cur.current_node,
            pending_gate=None,
            products=cur.products,
            errors=cur.errors + [{"code": "E4005", "node": "runner",
                                   "detail": reason or "任务已取消"}],
            gen_retry=cur.gen_retry,
        )

    def mark_failed(self, error: str) -> None:
        """把任务标记为失败（异常出口用）。"""
        self._terminal_override = S.STATUS_FAILED
        self._touch(S.STATUS_FAILED, last_error=error)

    def _touch(self, status: str, *, last_error: str | None = None) -> None:
        """直接更新 `task_state` 的状态列（只用于取消/失败这类外部动作）。

        流水线**正常推进**时不用它 —— 那时由各节点调
        `_base.touch_task_state` 自己打点。本函数只服务于
        "从图外面改状态"的两个场景（取消 / 失败）。
        """
        db = self._db_path()
        if db is None:
            logger.debug("没有 db 路径，跳过 task_state 更新")
            return
        from mmwf.infra import db as dbmod

        try:
            with dbmod.connect_ctx(db) as conn:
                conn.execute(
                    "UPDATE task_state SET status = ?, last_error = ?, "
                    "updated_at = ? WHERE task_id = ?",
                    (status, last_error, _now_iso(), self.meeting_id),
                )
                if status == S.STATUS_FAILED:
                    conn.execute("UPDATE meeting SET status = ?, updated_at = ? WHERE meeting_id = ?",
                                 (status, _now_iso(), self.meeting_id))
        except Exception:  # pragma: no cover
            logger.warning("task_state 状态更新失败", exc_info=True)

    def _db_path(self) -> Path | None:
        settings = getattr(self.deps, "settings", None)
        if settings is None:
            return None
        try:
            return Path(settings.resolve_path(settings.app.data_dir)) / "mmwf.db"
        except Exception:  # pragma: no cover
            return None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _pending_interrupt(snapshot: Any) -> PendingGate | None:
    """从快照里取出挂起的闸口。

    实测：`invoke()` 的返回值里 `__interrupt__` 是 `None`，
    只有 `get_state().tasks[*].interrupts` 能可靠拿到中断载荷。
    """
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            v = getattr(intr, "value", None)
            if not isinstance(v, dict):
                continue
            gate = str(v.get("gate") or "")
            return PendingGate(
                checkpoint_id=(getattr(snapshot, "config", None) or {}).get("configurable", {}).get("checkpoint_id", ""),
                gate=gate,
                gate_label=str(v.get("gate_label") or S.GATE_LABEL.get(gate, gate)),
                risk=str(v.get("risk") or S.GATE_RISK.get(gate, "medium")),
                payload=dict(v.get("payload") or {}),
                evidence=list(v.get("evidence") or []),
                node_id=f"gate_{gate.lower()}" if gate else "",
            )
    return None


def _derive_status(values: dict, snapshot: Any, pending: PendingGate | None) -> str:
    """推导当前状态。

    优先级（从高到低）：

        ⓪ 硬阻断标记 -> failed（重建后仍为失败）
        ① 闸口挂起  -> 该闸口的 awaiting 状态（`G2_awaiting` / `G3_awaiting`）
        ② 显式 stage -> 状态里被显式写过的 stage（如 archive 写的 `archived`）
        ③ 已取消    -> `cancelled`
        ④ 下一节点  -> 按 `snapshot.next` 查 `NODE_STATUS`
        ⑤ 兜底      -> `created`

    为什么闸口优先级最高：`interrupt` 时节点的**状态增量还没合并**，
    所以 `values` 里读不到 `G2_awaiting` —— 只能从"挂起的闸口"推。
    """
    if values.get("stage") == "validation_blocked":
        return S.STATUS_FAILED
    if pending is not None:
        return f"{pending.gate}_awaiting"
    stage = values.get("stage")
    if stage == S.STATUS_CANCELLED:
        return S.STATUS_CANCELLED
    if stage == S.STATUS_ARCHIVED:
        return S.STATUS_ARCHIVED
    nxt = tuple(getattr(snapshot, "next", ()) or ())
    if not nxt:
        # 没有下一节点：要么已跑完，要么还没开始
        return S.STATUS_ARCHIVED if values.get("export_paths") else S.STATUS_CREATED
    return S.NODE_STATUS.get(nxt[0], S.STATUS_CREATED)


def _current_node(snapshot: Any, pending: PendingGate | None) -> str | None:
    if pending is not None:
        return pending.node_id
    nxt = tuple(getattr(snapshot, "next", ()) or ())
    return nxt[0] if nxt else None


def _all_products() -> tuple[str, ...]:
    out: list[str] = []
    for fields in S.NODE_OUTPUTS.values():
        out.extend(fields)
    return tuple(dict.fromkeys(out))


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["GraphRunner", "PendingGate", "RunStatus"]
