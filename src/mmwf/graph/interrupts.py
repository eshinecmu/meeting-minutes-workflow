"""闸口中断与恢复契约（T1-10，SAD §5.3）。

闸口是什么
----------
流水线有两个地方**必须停下来等人**（PRD 的 HITL）：

    G2（`gate_g2`）—— 待确认议题。**最高优先级闸口**：
        议题错了，后面生成的整篇纪要都是错的。风险等级 `high`。
    G3（`gate_g3`）—— 待审定纪要。成稿前的最后一道关。风险等级 `medium`。

停在闸口时，任务状态置为 `G2_awaiting` / `G3_awaiting`，
`task_state.awaiting_actor` 有值。人做完决策后从**中断处**继续。

三种决策的语义（SAD §5.3，三种必须严格区分）
------------------------------------------------
    ========= ================== ========================== ==============
    决策       语义                状态迁移                    重跑已完成节点
    ========= ================== ========================== ==============
    approve   读检查点，原样继续   G2 -> generating            **否**
    reject    清产物，退回上游     G2 -> extracting            仅被驳回节点
    edit      注入修改后继续       G2 -> generating            **否**
    ========= ================== ========================== ==============

**`edit` 不重跑是降返工的关键**（V5 核心）。界面上
`reject` 显示「退回重做」、`edit` 显示「改完继续」—— 必须让使用者
直观感知"改完继续"不用整篇重来（N3.3）。

本模块与 `langgraph` 的关系
---------------------------
本模块**不在导入时就 import langgraph** —— 它是惰性的（见 `_lg()`）。
原因：`state.py` / `idempotency.py` / 本模块的类型与路由函数是**纯逻辑**，
测试它们不该要求装了 langgraph。真正需要 langgraph 的只有"调用
`interrupt()` 的那一行"和 `build.py`。这与 T1-09 的 `_docx_backend`
是同一种做法：把重依赖收在少数几个文件里。

⚠️ **闸口节点内绝对不能有副作用**（施工卡点名的坑）
------------------------------------------------------
`interrupt()` 恢复时，**中断点之前的代码会被重放**。也就是说 `gate_g2`
的函数体会被完整执行两遍（第一次停在 `interrupt` 处，恢复后再从头跑一遍
直到 `interrupt` 返回）。如果函数体里写了审计、写了文件，就会写两次。

所以本模块的 gate 节点函数体里**只有**：① 组装送审载荷；
② 调用 `interrupt`；③ 把决策转成状态增量。审计与落库一律放到
"恢复之后的下游节点"（`export` / `archive`）或用 `idempotency` 保护。
`accept_t1_10.py` 有一项专门检查这一点（闸口前后审计条数不变）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from mmwf.domain.errors import MmwfError
from mmwf.graph.state import (
    DECISION_LABEL,
    GATE_LABEL,
    GATE_REDO_TARGET,
    GATE_RISK,
    GATE_ROUTE,
    NODE_GATE_G2,
    NODE_GATE_G3,
    ROUTE_CONTINUE,
    ROUTE_REDO,
    VALID_DECISIONS,
    Decision,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 惰性 langgraph
# ---------------------------------------------------------------------------

def _interrupt():
    """惰性取 `langgraph.types.interrupt`。

    单独包一层是为了让"没装 langgraph"的错误信息带安装指引 ——
    与 T1-09 的 `_docx_backend._require_docx()` 同样的理由。
    """
    try:
        from langgraph.types import interrupt as _i
    except ImportError as exc:  # pragma: no cover - 装好依赖后不会走到
        raise MmwfError(
            code="E9001",
            detail=(
                f"编排需要 langgraph：{exc}\n"
                '请在项目目录执行：pip install -e ".[graph]"'
            ),
        ) from exc
    return _i


# ---------------------------------------------------------------------------
# 值对象（SAD §5.3 的两个 dataclass，逐字实现）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InterruptPoint:
    """一个闸口的中断点。

    `checkpoint_id` 由 LangGraph 在中断时生成，恢复时凭它重建 ——
    本类只是把它**带出来给调用方看**（API 层要用它做幂等/去重：
    同一个 checkpoint 的重复决策应当被拒绝）。
    """

    node_id: str
    checkpoint_id: str
    payload: dict
    risk: str
    evidence: list[dict] = field(default_factory=list)

    @property
    def gate(self) -> str:
        """`"gate_g2"` -> `"G2"`。"""
        return self.node_id.replace("gate_", "").upper()

    @property
    def gate_label(self) -> str:
        """界面文案（N3.3）。"""
        return GATE_LABEL.get(self.gate, self.gate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "gate_label": self.gate_label,
            "node_id": self.node_id,
            "checkpoint_id": self.checkpoint_id,
            "payload": self.payload,
            "risk": self.risk,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ResumeDecision:
    """一次恢复决策。

    `payload` 只在 `decision == "edit"` 时有意义；其余情况传了会被忽略
    （而不是报错）—— 前端常把上一次的 payload 一起回传，为此报错
    会让用户看到一个莫名其妙的中断。
    """

    decision: Decision
    payload: dict | None = None
    operator: str = ""
    comment: str | None = None

    def validate(self, *, gate: str) -> None:
        """校验决策合法性。非法决策报 **E4001**（施工卡 U-ST-08）。

        **不做"默认当 approve"的兜底**：把一个拼错的决策当成"通过"
        会让错误决策静默生效，而闸口存在的意义就是拦人。
        """
        if self.decision not in VALID_DECISIONS:
            raise MmwfError(
                code="E4001",
                detail=(
                    f"闸口 {gate} 收到非法决策 {self.decision!r}；"
                    f"合法值为 {sorted(VALID_DECISIONS)}"
                ),
            )
        if self.decision == "edit" and not self.payload:
            # edit 而没带修改内容，等同于"点了改完继续但什么都没改"。
            # 这多半是前端 bug，明确报错比静默当成 approve 好。
            raise MmwfError(
                code="E4001",
                detail=f"闸口 {gate} 的 edit 决策缺少 payload（修改后的内容）",
            )
        if self.decision == "edit":
            # 用**该闸口**的可编辑字段判定，不是全局并集 ——
            # G3 只认 `minutes_markdown`，不接受 `topics`。
            known = set(GATE_EDITABLE_FIELDS.get(gate, ()))
            if not known & set(self.payload or {}):
                raise MmwfError(
                    code="E4001",
                    detail=(
                        f"闸口 {gate} 的 edit payload 里没有该闸口可修改的字段；"
                        f"允许 {sorted(known)}"
                    ),
                )


class GateRejectedError(MmwfError):
    """**内部**信号：闸口被驳回，需要走 redo 边。

    它**不会传播到图外** —— `gate_*` 节点自己捕获它，转成状态里的
    `_gate_route` 字段。之所以不直接 `raise` 到图外，是因为
    LangGraph 的节点抛异常会把整个任务置为 failed，而 reject
    是**正常业务路径**（退回重做），不是失败。
    """

    def __init__(self, gate: str, detail: str = "") -> None:
        super().__init__(
            code="E4001",
            detail=detail or f"闸口 {gate} 被驳回，退回上游重做",
        )
        self.gate = gate


#: 状态下用来暂存"本次闸口走哪条边"的键。
#:
#: 为什么要把路由结果放进状态，而不是在 `route_after_*` 里重新读一次决策？
#: 因为 LangGraph 的条件边函数**只拿得到状态**，拿不到节点内的局部变量。
#: 决策是中断时由人给的，不在状态里 —— 所以节点必须把结论写进状态。
#: 用下划线前缀标明它是**编排内部字段**，不是业务产物。
GATE_ROUTE_KEY = "_gate_route"

#: 驳回次数计数键。用于**防止 reject 死循环**：
#: reject 回到 extract 后又会走到 gate_g2，如果人再点一次 reject，
#: 就再回 extract …… 没有上限的话任务永远出不去。
#: 超过 `MAX_GATE_REJECTS` 次就停止退回，把决策当"需要人工介入"处理。
GATE_REJECT_KEY = "_gate_rejects"

#: 同一闸口最多允许驳回几次。超过后节点不再退回，而是**抛出**让任务
#: 落到 failed（人工介入）。3 次是基于"一般人不会连着错三次"，
#: 且留有余量，不是精确调参的结果。
MAX_GATE_REJECTS = 3


# ---------------------------------------------------------------------------
# 送审载荷
# ---------------------------------------------------------------------------

#: G2 送审的字段（SAD §5.3：议题数组）。
G2_PAYLOAD_KEYS: tuple[str, ...] = ("topics", "resolutions", "action_items")

#: G3 送审的字段（纪要草稿）。同时带上问题清单：
#: 审定人需要知道"机器自己觉得哪里不确定"，否则无从下手核对。
G3_PAYLOAD_KEYS: tuple[str, ...] = ("minutes_markdown", "validation_issues")

#: 闸口允许通过 `edit` 修改的状态字段（SAD §5.3 的 `edited_payload`）。
#:
#: **按闸口分开列**，不是一个共用的大集合。理由：
#: 闸口的职责是"审我该审的那一段"。G2 审议题，G3 审定稿措辞。
#: 如果共用一个集合，G3 就能改议题 —— 而议题在 G2 已经确认过，
#: 那条路径等于**绕过 G2 闸口**直接改议题，闸口的分段防护就漏了。
#: （IT-06 要求 G3 能 edit，但改的是**措辞**即 `minutes_markdown`。）
#:
#: 同时这也不是一条"绕过闸口直接改状态"的通道：只有本表列出的字段
#: 会被接受，其余一律报 E4001。
GATE_EDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "G2": G2_PAYLOAD_KEYS,
    "G3": ("minutes_markdown",),
}

#: 全部可编辑字段的并集（供错误提示与测试引用）。
_EDITABLE_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(f for fields in GATE_EDITABLE_FIELDS.values() for f in fields)
)


def build_gate_payload(gate: str, state: dict) -> dict:
    """组装送审载荷（按闸口取对应字段）。

    只取该闸口关心的字段，**不整个 state 塞进去**：
    `state` 里有 `audio_paths` 这类内部路径，送审载荷会经 API 到前端，
    前端文案要求"N3.3 不出现工程词" —— 带上路径迟早会漏到界面上。
    """
    if gate == "G2":
        keys = G2_PAYLOAD_KEYS
    elif gate == "G3":
        keys = G3_PAYLOAD_KEYS
    else:  # pragma: no cover - 闸口只有两个
        raise KeyError(f"未知闸口 {gate!r}")
    return {k: state.get(k) for k in keys}


def collect_evidence(state: dict) -> list[dict]:
    """收集"每条结论的来源片段与时间戳"（SAD §5.3 的 `evidence`）。

    这是 C04「无依据结论硬拦截」在闸口这一侧的配套：**审定人要能看到
    依据**，否则闸口就只是让人盲签。本函数只做**汇总去重**，
    真正的依据校验在 `domain/grounding`（后续卡）。

    返回空列表是合法的 —— 此时送审载荷里 `evidence` 为空，
    前端应提示"本题议题暂无溯源信息"。不抛错，因为
    "没有依据"是内容问题，不是编排错误。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for item in (state.get("topics") or []) + (state.get("resolutions") or []):
        if not isinstance(item, dict):
            continue
        for ref in item.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            key = f"{ref.get('segment_id')}:{ref.get('start_ms')}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "segment_id": ref.get("segment_id"),
                "start_ms": ref.get("start_ms"),
                "text": ref.get("text", ""),
            })
    return out


# ---------------------------------------------------------------------------
# 闸口节点工厂
# ---------------------------------------------------------------------------

def make_gate(gate: str):
    """构造一个闸口节点函数。

    G2 与 G3 的差别只有"送审哪些字段"和"驳回回到哪"，所以用工厂而不是
    写两个几乎一样的函数 —— 两份代码最可怕的不是重复，是**改了一份
    忘了另一份**（比如给 G2 加了死循环保护却漏了 G3）。

    ⚠️ 本函数返回的函数体内**没有任何副作用**（见模块文档）。
    """
    if gate not in GATE_LABEL:
        raise KeyError(f"未知闸口 {gate!r}；已知 {sorted(GATE_LABEL)}")

    def gate_node(state: dict) -> dict:
        node_id = f"gate_{gate.lower()}"
        payload = build_gate_payload(gate, state)

        # 空载荷：没有要确认的东西就不该停在这里等人。
        # 报 E4003 是**警告级**（SAD 码表），但编排上仍然继续往下 ——
        # 让人"确认一个空列表"是纯粹浪费。
        if _payload_is_empty(gate, payload):
            logger.info("闸口 %s 无待确认内容，自动放行（E4003）", gate)
            return {
                GATE_ROUTE_KEY: ROUTE_CONTINUE,
                "errors": _append_error(
                    state, code="E4003", node_id=node_id,
                    detail=f"闸口 {gate} 待确认项为空，已跳过",
                ),
            }

        decision_raw = _interrupt()({
            "gate": gate,
            "gate_label": GATE_LABEL[gate],
            "payload": payload,
            "risk": GATE_RISK[gate],
            "evidence": collect_evidence(state),
        })

        return _apply_decision(state, gate=gate, node_id=node_id,
                               raw=decision_raw)

    gate_node.__name__ = f"gate_{gate.lower()}"
    gate_node.__doc__ = f"{GATE_LABEL[gate]}闸口节点（{gate}，风险 {GATE_RISK[gate]}）"
    return gate_node


def make_gate_g2():
    """G2「待确认议题」闸口节点（施工卡骨架里的 `make_gate_g2`）。"""
    return make_gate("G2")


def make_gate_g3():
    """G3「待审定纪要」闸口节点。"""
    return make_gate("G3")


def _payload_is_empty(gate: str, payload: dict) -> bool:
    """送审载荷是不是"没什么可确认的"。

    G2：三个议题字段全空 -> 没什么可确认。
    G3：**只要** `minutes_markdown` 非空就必须给人看（成稿审定不能跳过），
        哪怕问题清单是空的。
    """
    if gate == "G2":
        return not any(
            payload.get(k) for k in G2_PAYLOAD_KEYS
        )
    return not payload.get("minutes_markdown")


def _apply_decision(state: dict, *, gate: str, node_id: str, raw: Any) -> dict:
    """把 interrupt 返回的原始决策转成状态增量。

    `raw` 是前端经 `Command(resume=...)` 回传的任意值 —— 它可能不是
    dict（前端 bug / 手写脚本）。这里先做形状校验再解析，
    否则会在下一行以 KeyError 的形式炸掉，排查起来指向不到真正的错处。
    """
    if not isinstance(raw, dict):
        raise MmwfError(
            code="E4001",
            detail=f"闸口 {gate} 收到的恢复数据不是对象（实际 {type(raw).__name__}）",
        )

    d = ResumeDecision(
        decision=raw.get("decision"),
        payload=raw.get("payload"),
        operator=raw.get("operator", "") or "",
        comment=raw.get("comment"),
    )
    d.validate(gate=gate)

    if d.decision == "approve":
        logger.info("闸口 %s 通过（operator=%s）", gate, d.operator or "(未署名)")
        return {GATE_ROUTE_KEY: GATE_ROUTE[gate]["approve"]}

    if d.decision == "edit":
        # edit 的语义是"用修改后的 payload 注入状态，**不再重跑上游**"。
        # 这里只返回被修改的字段（状态增量），其余字段原样保留。
        merged = _merge_edit_payload(state, gate=gate, payload=d.payload or {})
        logger.info("闸口 %s 修改后继续：注入 %s（operator=%s）",
                    gate, sorted(merged), d.operator or "(未署名)")
        out = dict(merged)
        out[GATE_ROUTE_KEY] = GATE_ROUTE[gate]["edit"]
        return out

    # ---- reject ----
    rejects = int(state.get(GATE_REJECT_KEY, 0) or 0) + 1
    if rejects > MAX_GATE_REJECTS:
        # 连着驳到上限仍要退回，说明人机之间对不上（比如上游根本
        # 抽不出议题）。这时**不能继续循环**，也不能静默当成通过 ——
        # 让任务落到 failed，交人工处理。
        raise MmwfError(
            code="E4001",
            detail=(
                f"闸口 {gate} 已驳回 {MAX_GATE_REJECTS} 次，超过上限。"
                f"退回上游重做目标：{GATE_REDO_TARGET[gate]}"
            ),
            context={"gate": gate, "rejects": rejects - 1},
        )

    logger.info("闸口 %s 驳回第 %d 次，退回 %s（operator=%s）",
                gate, rejects, GATE_REDO_TARGET[gate], d.operator or "(未署名)")
    return {
        GATE_ROUTE_KEY: GATE_ROUTE[gate]["reject"],
        GATE_REJECT_KEY: rejects,
        # **清掉本闸口下游的产物**：不清的话，上游重跑后下游若因
        # 幂等命中而复用旧产物，用户看到的还是被驳回的那一版。
        # 这是"驳回"最容易出的错 —— 状态显示"退回重做"，界面上的
        # 内容却还是旧的。
        **{k: None for k in _downstream_outputs(gate)},
        "errors": _append_error(
            state, code="E4001", node_id=node_id,
            detail=f"闸口 {gate} 被驳回：{d.comment or '(未填原因)'}",
        ),
    }


def _merge_edit_payload(state: dict, *, gate: str, payload: dict) -> dict:
    """把 edit 的 payload 合并进状态（**只接受该闸口**允许改的字段）。

    闸口自己该审的那一段之外的字段一律丢弃。丢而不是报错：
    前端常把整张表单回传，里面混着只读字段，为此报错会让用户
    看到一个莫名其妙的中断。但**一个该闸口的字段都没带**要报错 ——
    那等于"点了改完继续却什么都没改"。
    """
    allowed = set(GATE_EDITABLE_FIELDS.get(gate, ()))
    merged = {k: v for k, v in payload.items() if k in allowed}
    if not merged:
        raise MmwfError(
            code="E4001",
            detail=(
                f"闸口 {gate} 的 edit payload 里没有可修改的字段"
                f"（允许：{sorted(allowed)}）"
            ),
        )
    return merged


def _downstream_outputs(gate: str) -> tuple[str, ...]:
    """驳回时需要清空的状态字段（退回目标节点之后的所有产物）。"""
    from mmwf.graph.state import NODE_OUTPUTS, NODE_SEQUENCE

    target = GATE_REDO_TARGET[gate]
    if target not in NODE_SEQUENCE:
        return ()
    keep_until = NODE_SEQUENCE.index(target)
    out: list[str] = []
    for node in NODE_SEQUENCE[keep_until:]:
        out.extend(NODE_OUTPUTS.get(node, ()))
    return tuple(dict.fromkeys(out))


def _append_error(state: dict, *, code: str, node_id: str, detail: str) -> list[dict]:
    """向 `errors` 追加一条（返回新列表，不原地改）。

    `errors` 是**累积**字段：不能用 `[item]` 覆盖，否则前一个节点的
    错误会被后一个节点吃掉，最后只剩最后一条错。LangGraph 对 list
    字段的默认合并是"替换"，所以这里显式拼旧值。
    """
    prev = list(state.get("errors") or [])
    prev.append({"code": code, "node": node_id, "detail": detail})
    return prev


# ---------------------------------------------------------------------------
# 路由函数（`add_conditional_edges` 的键）
# ---------------------------------------------------------------------------

def route_after_g2(state: dict) -> str:
    """G2 之后走哪条边。

    `continue` -> generate（approve / edit）
    `redo`     -> extract （reject，且退回次数未超上限）
    """
    return _route(state, gate="G2")


def route_after_g3(state: dict) -> str:
    """G3 之后走哪条边。

    `continue` -> export（approve / edit）
    `redo`     -> assemble（reject）
    """
    return _route(state, gate="G3")


def _route(state: dict, *, gate: str) -> str:
    """读节点写下的路由结论。

    **不重新解析决策**：决策值根本不在状态里（`interrupt` 的返回值
    只存在于节点内的局部变量）。节点已经把它转成了 `GATE_ROUTE_KEY`，
    路由函数只负责把它读出来。

    缺键时默认 `continue` 而不是报错：缺键意味着此路由被图之外的人
    直接调用了（测试会这么干），默认往下走比抛异常更好用。
    但这个"宽容"只在这里 —— 节点的 `validate()` 是严格的。
    """
    val = state.get(GATE_ROUTE_KEY)
    if val in (ROUTE_CONTINUE, ROUTE_REDO):
        return val
    logger.debug("路由缺少 %s（实际 %r），按 %s 处理", GATE_ROUTE_KEY, val, ROUTE_CONTINUE)
    return ROUTE_CONTINUE


def clear_route_marker(state: dict) -> dict:
    """清掉路由标记。

    路由标记是"一次性"的：它描述**刚刚发生的那次闸口决策**。
    不清掉的话，下一轮跑到同一个闸口时，条件边可能读到上一轮的旧值
    （比如上一轮 reject、这一轮 approve，却因为旧值还在而再次退回）。
    由闸口**下游的第一个节点**负责清理。
    """
    return {GATE_ROUTE_KEY: None}


#: 决策 -> 界面按钮文案的再导出（N3.3）。前端从这里取，不要自己写死。
DECISION_BUTTONS: dict[str, str] = dict(DECISION_LABEL)


__all__ = [
    "DECISION_BUTTONS",
    "G2_PAYLOAD_KEYS",
    "G3_PAYLOAD_KEYS",
    "GATE_EDITABLE_FIELDS",
    "GATE_REJECT_KEY",
    "GATE_ROUTE_KEY",
    "MAX_GATE_REJECTS",
    "GateRejectedError",
    "InterruptPoint",
    "ResumeDecision",
    "build_gate_payload",
    "clear_route_marker",
    "collect_evidence",
    "make_gate",
    "make_gate_g2",
    "make_gate_g3",
    "route_after_g2",
    "route_after_g3",
]
