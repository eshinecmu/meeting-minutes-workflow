"""`review` 节点 —— 低置信分流 + 生成「会议记录.md」（T1-08）。

职责
----
把转写稿变成**给人核对的第一份稿子**：
  1. 读回 `transcript/segments.json`（`transcribe` 落的）；
  2. 按阈值分流，标出需要人工核对的段（`plan_review`）；
  3. 渲染成记录稿 Markdown 并落盘（`generate_record`）。

为什么它读**文件**而不是状态里的 `transcript`
---------------------------------------------
状态里的 `transcript` 是给图内传递用的**摘要形状**，
而 `ReviewService` 的契约是"从存储层读、按存储层写"（T1-08 定的）。
两者刻意不合并：让 review 走文件有**独立可验证性**
（可以只用 CLI 生成记录稿，不启动流水线）。
两边的一致性由 `transcribe` 节点保证 —— 它同时写状态和写文件。

状态推进由**节点**负责
----------------------
`generate_record(write_state=...)` 默认会自己 `UPDATE meeting.status`。
本节点显式传 `write_state=False` 并改由 `_base.touch_task_state` 推进
`task_state` —— 否则"谁在改状态"会有两个来源，恢复时互相覆盖。
这是把 T1-08 的"服务直接改状态"接进编排层的一次收口。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes.ingest import _Cfg, _resolve

logger = logging.getLogger(__name__)


def make_review(
    deps: Any = None,
    *,
    service_factory: Callable[[Any, str], Any] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `review` 节点。

    Args:
        service_factory: `(settings, meeting_id) -> ReviewService`。
            默认 `app.services.review.make_review_service`。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)

    def review(state: dict) -> dict:
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_REVIEW)

        def do() -> dict[str, Any]:
            svc = _service(service_factory, cfg, state)
            # 先规划分流（纯计算），把"哪些段要人为核对"记进状态 ——
            # 界面上"文稿待核对"这个状态要靠它才知道要给用户看哪几段。
            plan = _plan(svc)
            # write_state=False：状态推进收归编排层（见模块文档）
            result = svc.generate_record(
                write_state=False,
                extra_warnings=_upstream_warnings(state),
            )
            return {"_result": result, "_plan": plan}

        got, cached = B.idempotent(cfg.idem_store, S.NODE_REVIEW, state, do)
        result = got.get("_result")

        out: dict[str, Any] = {
            "review_result": got.get("_plan") or {},
        }
        if result is not None:
            # 下游（extract）拿的是**正文**，不是"给人看的完整稿"。
            # 完整稿含元信息表与待核对清单，被 extract 逐行当成议题 ——
            # 实测一篇正常记录稿会因此产出 15 条自检问题、14 条是误报，
            # 并且每篇纪要都会走满 validate 的重试路径。
            # 完整稿仍在 `review_result` 的 rel_path 里（可随时读回）。
            out["record_markdown"] = _body_of(result)
            out["review_result"] = {
                **(out["review_result"] or {}),
                **_summary_of(result),
            }

        logger.info(
            "review 完成（%s）：记录稿 v%s，%d 段，%d 段待核对",
            "幂等命中" if cached else "本次执行",
            getattr(result, "version", "?"),
            getattr(result, "segment_count", 0),
            getattr(result, "review_count", 0),
        )
        return out

    review.__name__ = S.NODE_REVIEW
    review.__doc__ = "低置信分流 + 生成记录稿（幂等）"
    return review


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _service(factory: Any, cfg: _Cfg, state: dict) -> Any:
    if factory is not None:
        return factory(cfg.settings, B.meeting_id_of(state))
    from mmwf.app.services.review import make_review_service

    return make_review_service(
        B.meeting_id_of(state), cfg.settings,
        operator=str(state.get("operator") or "system"),
    )


def _plan(svc: Any) -> dict[str, Any]:
    """规划分流。失败不阻断 —— 分流是"辅助定位"，记录稿本身才是产物。"""
    try:
        items = svc.plan_review_report()
    except Exception:
        logger.warning("分流规划失败，记录稿仍会生成", exc_info=True)
        return {"items": [], "count": 0, "error": "分流规划失败"}
    return {"items": list(items or []), "count": len(items or [])}


def _upstream_warnings(state: dict) -> list[str]:
    """把上游（转写）的告警转述给记录稿末尾的"生成提示"。

    只挑转写相关的码：E2xxx 是 ASR 族的（见 `domain/errors.py`）。
    把编排层的 E4xxx / E9xxx 也带进去会让记录稿末尾出现
    "闸口被驳回"这种内部信息 —— 记录稿是给参会人看的。
    """
    out: list[str] = []
    for e in state.get("errors") or []:
        code = str(e.get("code") or "")
        if code.startswith("E2"):
            out.append(str(e.get("detail") or code))
    return out


def _body_of(result: Any) -> str:
    """从 `RecordResult` 取**正文**（去掉元信息表与待核对清单）。

    分组理由见 `make_review.review()` 里的注释：完整稿是给人看的，
    `record_markdown` 是给下游抽议题用的，两者混用会产生大量误报。

    **算不出来就退回全文**而不是抛错 —— 抽议题的质量差一些，
    总好过整条流水线因为"取正文失败"而停摆。
    """
    try:
        from mmwf.app.services.review import extract_body

        return extract_body(str(getattr(result, "markdown", "") or ""),
                            getattr(result, "version", None))
    except Exception:  # pragma: no cover - 兜底
        logger.warning("取记录稿正文失败，退回全文（extract 可能因此误报）",
                       exc_info=True)
        return str(getattr(result, "markdown", "") or "")


def _summary_of(result: Any) -> dict[str, Any]:
    """`RecordResult` 的关键字段（不含正文，正文单列 `record_markdown`）。

    **刻意不放 `markdown`**：它会进状态、进检查点、进 API 响应。
    状态里放一份正文就够（`record_markdown`），放两份会让状态膨胀
    一倍，且两份可能不一致（一份是生成时的、一份是后来的）。
    """
    return {
        "meeting_id": getattr(result, "meeting_id", None),
        "version": getattr(result, "version", None),
        "rel_path": getattr(result, "rel_path", None),
        "sha256": getattr(result, "sha256", None),
        "segment_count": getattr(result, "segment_count", None),
        "review_count": getattr(result, "review_count", None),
        "review_ratio": getattr(result, "review_ratio", None),
        "metadata_missing": list(getattr(result, "metadata_missing", ()) or ()),
        "summary": _summary_text(result),
    }


def _summary_text(result: Any) -> str:
    fn = getattr(result, "summary", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:  # pragma: no cover
            pass
    return ""


__all__ = ["make_review"]
