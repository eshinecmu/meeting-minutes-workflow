"""`ingest` 节点 —— 音频校验、探测时长、出切分计划、实际切分（T1-06）。

职责边界
--------
本节点只做**音频接入**：把用户投的文件变成"可被转写引擎逐段吃进去的
小段 + 一份计划"。它**不**碰转写（那是 `transcribe` 节点）。

为什么它是副作用节点
--------------------
切分会真的往磁盘写文件（`chunks/0001.wav` …），所以：
  · 必须走幂等 —— 恢复时重跑不该把磁盘铺满第二遍；
  · 幂等键的输入是 `audio_paths`（源文件没变，切出来的东西就没变）。

不推进到 `transcribe` 的原因
----------------------------
`ingest` 在 `state.NODE_REQUIRES` 里是空元组 —— 它的输入是**用户投递的
文件**，不在状态里（状态里只有路径）。所以调用方（`runner`）负责把
`audio_paths` 放进初始状态。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.nodes import _base as B

logger = logging.getLogger(__name__)

#: 切分产物目录（相对会议目录）。
CHUNK_DIR = "audio/chunks"


def make_ingest(
    deps: Any = None,
    *,
    service_factory: Callable[[Any, str], Any] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `ingest` 节点。

    Args:
        deps: 依赖容器。约定含 `settings` / `idem_store`（见 `build.py`）。
              也可以直接传 `settings=` 与 `idem_store=` 覆盖。
        service_factory: `(settings, meeting_id) -> IngestService`。
            默认用 `app.services.ingest.make_ingest_service`。测试注入替身。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)

    def ingest(state: dict) -> dict:
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state), S.NODE_INGEST)

        paths = _audio_paths(state)
        if not paths:
            # 没有音频不是"失败"：F1.6 允许"直接导入文字稿"。
            # 此时 ingest 无事可做，交给 transcribe 去做文字稿路径。
            logger.info("无音频输入，ingest 跳过（文字稿导入路径）")
            return {
                "chunk_plan": [],
                "errors": B.append_error(
                    state, "E1002", S.NODE_INGEST,
                    "没有音频文件，按文字稿导入处理",
                ),
            }

        def do() -> dict[str, Any]:
            svc = _service(service_factory, cfg, state)
            chunk_dir = cfg.data_dir_of(state) / CHUNK_DIR
            batch = svc.ingest_batch(paths, chunk_dir=chunk_dir)
            if batch.failed and not batch.succeeded:
                first = batch.failed[0]
                raise first.error
            if batch.failed:
                # 部分失败：**不整批失败**（一个坏文件不该拖垮其余）。
                # 但必须留痕，否则用户以为"都传进去了"。
                logger.warning("部分音频接入失败：%d/%d", len(batch.failed), batch.total)
            plan: list[dict] = []
            ok_paths: list[str] = []
            for item in batch.succeeded:
                plan.extend(_plan_to_dicts(item))
                ok_paths.append(str(item.source_path))
            return {"chunk_plan": plan, "audio_paths": ok_paths,
                    "warnings": [w for it in batch.succeeded for w in it.warnings]}

        got, cached = B.idempotent(cfg.idem_store, S.NODE_INGEST, state, do)
        logger.info("ingest 完成（%s）：%d 段计划",
                    "幂等命中" if cached else "本次执行",
                    len(got.get("chunk_plan") or []))

        out: dict[str, Any] = {"chunk_plan": got.get("chunk_plan") or []}
        if got.get("audio_paths"):
            out["audio_paths"] = got["audio_paths"]
        warnings = got.get("warnings") or []
        if warnings:
            out["errors"] = _warn_entries(state, S.NODE_INGEST, warnings)
        if cached:
            # 幂等命中时也要清 `_gate_route`（见 interrupts.clear_route_marker）
            out["_gate_route"] = None
        return out

    ingest.__name__ = S.NODE_INGEST
    ingest.__doc__ = "音频接入：校验 / 探测 / 切分计划 / 切分（幂等）"
    return ingest


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _resolve(deps: Any, *, settings: Any, idem_store: Any) -> "_Cfg":
    if deps is not None:
        settings = settings if settings is not None else getattr(deps, "settings", None)
        idem_store = idem_store if idem_store is not None else getattr(deps, "idem_store", None)
    if isinstance(deps, dict):
        settings = settings if settings is not None else deps.get("settings")
        idem_store = idem_store if idem_store is not None else deps.get("idem_store")
    if settings is None:
        from mmwf.settings import get_settings

        settings = get_settings()
    return _Cfg(settings=settings, idem_store=idem_store)


class _Cfg:
    """节点共享的配置视图（settings + 幂等表）。"""

    __slots__ = ("settings", "idem_store")

    def __init__(self, *, settings: Any, idem_store: Any) -> None:
        self.settings = settings
        self.idem_store = idem_store

    def data_dir_of(self, state: dict) -> Path:
        d = self.settings.resolve_path(self.settings.app.data_dir)
        return Path(d) / B.meeting_id_of(state)

    def db_path_of(self, state: dict) -> Path:
        d = self.settings.resolve_path(self.settings.app.data_dir)
        return Path(d) / "mmwf.db"

    def session(self, state: dict) -> Any:
        return B.session_of(
            B.meeting_id_of(state), self.settings,
            operator=str(state.get("operator") or "system"),
        )


def _service(factory: Any, cfg: _Cfg, state: dict) -> Any:
    if factory is not None:
        return factory(cfg.settings, B.meeting_id_of(state))
    from mmwf.app.services.ingest import make_ingest_service

    return make_ingest_service(cfg.settings, probe_only=False)


def _audio_paths(state: dict) -> list[Path]:
    raw = state.get("audio_paths") or []
    return [Path(p) for p in raw]


def _plan_to_dicts(item: Any) -> list[dict]:
    """`IngestedAudio.chunk_plan` -> 可 JSON 化的 dict 列表。

    状态要被检查点序列化（SQLite + JSON），放 dataclass 进去能序列化
    但读回来会变成 dict，前后类型不一致会让下游 `.seq` 访问失败。
    所以**入口就转成 dict**。
    """
    out: list[dict] = []
    files = list(getattr(item, "chunk_files", ()) or ())
    for index, c in enumerate(getattr(item, "chunk_plan", ()) or ()):
        out.append({
            "seq": getattr(c, "seq", None),
            "start_ms": getattr(c, "start_ms", None),
            "end_ms": getattr(c, "end_ms", None),
            "source": str(getattr(item, "source_path", "")),
            # Each plan entry owns its chunk; repeating the full list here
            # makes N chunks transcribe N times each.
            "chunk_files": [str(files[index])] if index < len(files) else [],
        })
    return out


def _warn_entries(state: dict, node_id: str, warnings: list[str]) -> list[dict]:
    prev = list(state.get("errors") or [])
    for w in warnings:
        prev.append({"code": "E1003", "node": node_id, "detail": str(w)})
    return prev


__all__ = ["CHUNK_DIR", "make_ingest"]
