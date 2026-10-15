"""`export` / `archive` 两个节点 —— 导出 docx 与归档（T1-09 接线 + 幂等）。

这两个节点是本卡"副作用幂等"要证明的地方（SAD §5.4 / F6.3）
----------------------------------------------------------------
    `export`   —— 落盘一个 `.docx`（可能还有 `.doc` 转换产物）
    `archive`  —— 冻结该会议的全部产物，之后不可再改

判据很简单：**重复触发同一个节点，磁盘上不该出现第二份文件**。
`accept_t1_10.py` 的"同一任务导出两次，路径相同且文件只有一个"
就是查这件事。

为什么 `export` 的幂等键用 `minutes_markdown` 而不是版本号
---------------------------------------------------------
版本号是"再来一次的语义"（T1-09 的记录稿是**留版本**的）。
用它做键的话，"内容没变但版本号变了"会导出第二份一模一样的文件。
用成稿内容做键才符合意图：**内容一样就是同一次导出**。
需要"重新导出一份新的"时，走 `store.drop(key)`（`undo_export` 场景）。

⚠️ 闸口节点无副作用，所以 `gate_decision` 的审计**不在这里写**
------------------------------------------------------------
G3 approve 之后走到本节点才写审计。放在闸口里会因为 `interrupt()`
的重放而被写两次 —— 这是施工卡点名的坑，`accept_t1_10.py` 有专项检查。
"""

from __future__ import annotations

import logging
import errno
import json
from typing import Any, Callable

from mmwf.infra.paths import export_paths
from mmwf.domain.errors import MmwfError
from mmwf.infra.filestore import FileStoreError
from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes.ingest import _Cfg, _resolve

logger = logging.getLogger(__name__)

#: 归档清单在存储层的位置。
ARCHIVE_MANIFEST = "archive/manifest.json"


def make_export(
    deps: Any = None,
    *,
    service_factory: Callable[[Any, str], Any] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `export` 节点。

    Args:
        service_factory: `(settings, meeting_id) -> ExportService`。
            默认 `app.services.export.make_export_service`。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)

    def export(state: dict) -> dict:
        B.require_inputs(S.NODE_EXPORT, state)
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_EXPORT)
        session = cfg.session(state)
        from mmwf.app.services.minutes import MinutesService
        snapshot = MinutesService(cfg.settings).deliver(B.meeting_id_of(state), state,
            approved=True, created_by=str(state.get("operator") or "system"))

        def do() -> dict[str, Any]:
            svc = _service(service_factory, cfg, state, session)
            # write_audit=False：审计由本节点在**幂等之外**写一次，
            # 否则幂等命中时不会写（那时没走 svc），语义就断了。
            result = svc.export_docx(write_audit=False, artifact="minutes",
                minutes_state=state, minutes_snapshot=snapshot, approved=True)
            return {"paths": _paths_of(result), "warnings": list(getattr(result, "font_warnings", ()) or ())}

        store = cfg.idem_store or idem.IdempotencyStore(cfg.db_path_of(state))
        got, cached = B.idempotent(store, S.NODE_EXPORT, state, do,
            result_ref_of=lambda value: json.dumps(value, ensure_ascii=False))
        if cached:
            try:
                got = json.loads(got)
                got["paths"] = export_paths(got["paths"])
                expected = f"export/{B.meeting_id_of(state)}-纪要-v{snapshot['version_no']}.docx"
                valid = got["paths"]["docx"] == expected and session.store.exists(expected)
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                # 旧记录稿缓存不是纪要交付；合法候选重新生成配对文件。
                store.drop(_key_of(state))
                got, cached = B.idempotent(store, S.NODE_EXPORT, state, do,
                    result_ref_of=lambda value: json.dumps(value, ensure_ascii=False))
                if cached:
                    got = json.loads(got)
        paths = export_paths(got["paths"])

        if not cached:
            # 只有真正执行时才写审计 —— 幂等命中说明"上一次已经留过痕了"。
            _audit(session, state, paths)
            logger.info("export 完成：%s", paths.get("docx"))
        else:
            logger.info("export 幂等命中，复用既有产物：%s", paths.get("docx"))

        out: dict[str, Any] = {
            "export_paths": paths,
            "idem_keys": {
                **(state.get("idem_keys") or {}),
                S.NODE_EXPORT: _key_of(state),
            },
            # export 是 G3 的下游第一个节点，负责清路由标记
            "_gate_route": None,
        }
        warns = got["warnings"]
        if warns:
            errs = list(state.get("errors") or [])
            for w in warns:
                errs.append({"code": "E5002", "node": S.NODE_EXPORT, "detail": str(w)})
            out["errors"] = errs
        return out

    export.__name__ = S.NODE_EXPORT
    export.__doc__ = "导出 docx（幂等：同内容不产生第二份文件）"
    return export


def make_archive(
    deps: Any = None,
    *,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
    now: Callable[[], str] | None = None,
) -> Callable[[dict], dict]:
    """构造 `archive` 节点（冻结产物，写归档清单）。"""
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)

    def archive(state: dict) -> dict:
        from mmwf.app.services.export import delivery_lock
        with delivery_lock(cfg.session(state).store):
            try:
                return archive_locked(state)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise MmwfError(code='E9002', detail='归档存储空间不足') from exc
                raise

    def archive_locked(state: dict) -> dict:
        B.require_inputs(S.NODE_ARCHIVE, state)
        from mmwf.app.services.minutes import MinutesService
        minutes_service = MinutesService(cfg.settings)
        minutes_service.validate(B.meeting_id_of(state), state)
        minutes_service.require_final(B.meeting_id_of(state), state.get("export_paths", {}).get("docx", ""))
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_ARCHIVE)
        session = cfg.session(state)

        from mmwf.app.services.export import make_export_service, delivery_lock
        service = make_export_service(B.meeting_id_of(state), cfg.settings)
        with delivery_lock(session.store):
            paths = export_paths(state.get("export_paths") or {})
            pdfs = service.valid_pdf_entries(paths.get('docx', ''))
            paths.pop('pdf', None)
            if pdfs:
                paths['pdf'] = pdfs
            # 集合输入只在 export_paths 内扩展；不增加 PipelineState 顶层键。
            paths['artifact_sha256'] = service.artifact_hashes(paths)
            archive_state = {**state, 'export_paths': paths}

            def do() -> dict[str, Any]:
                previous = session.store.read_json(ARCHIVE_MANIFEST) if session.store.exists(ARCHIVE_MANIFEST) else {}
                manifest = {
                    "meeting_id": B.meeting_id_of(state),
                    "export_paths": paths,
                    "record_version": (state.get("review_result") or {}).get("version"),
                    "validation_issues": len(state.get("validation_issues") or []),
                    "archived_at": previous.get('archived_at') or (now or _now)(),
                }
                service._write_json(ARCHIVE_MANIFEST, manifest)
                return manifest

            store = cfg.idem_store
            key = _key_of(archive_state, S.NODE_ARCHIVE)
            # DB 命中也必须有一致的实际清单；被删/损坏后重写，不能伪称复用。
            if store is not None and store.get(key):
                try:
                    valid = service._archive_for_pdf(True)['export_paths'] == paths
                except (OSError, ValueError, KeyError, TypeError, FileStoreError, MmwfError):
                    valid = False
                if not valid:
                    store.drop(key)
            got, cached = B.idempotent(store, S.NODE_ARCHIVE, archive_state, do)
            if not cached:
                _audit_archive(session, state, got)
            return {
                "stage": S.STATUS_ARCHIVED,
                "export_paths": paths,
                "idem_keys": {**(state.get("idem_keys") or {}), S.NODE_ARCHIVE: key},
            }

    archive.__name__ = S.NODE_ARCHIVE
    archive.__doc__ = "归档产物并写台账（幂等）"
    return archive


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _service(factory: Any, cfg: _Cfg, state: dict, session: Any) -> Any:
    if factory is not None:
        return factory(cfg.settings, B.meeting_id_of(state))
    from mmwf.app.services.export import make_export_service

    return make_export_service(
        B.meeting_id_of(state), cfg.settings,
        audit=session.audit,
        operator=str(state.get("operator") or "system"),
    )


def _paths_of(result: Any) -> dict[str, Any]:
    """`ExportResult` -> 可序列化的路径字典。

    同时带上 `doc_conversion` 的状态：`.doc` 转换依赖本机 LibreOffice，
    缺失时是 **skipped 而不是失败**（T1-09 的决定），
    这里必须把"跳过了"这个事实传出去，否则用户以为 `.doc` 已经生成。
    """
    if result is None:
        return {}
    conv = getattr(result, "doc_conversion", None)
    out: dict[str, Any] = {
        "docx": str(getattr(result, "rel_path", "") or ""),
        "filename": str(getattr(result, "filename", "") or ""),
        "version": getattr(result, "version", None),
        "sha256": getattr(result, "sha256", None),
    }
    if conv is not None:
        out["doc"] = {
            "status": getattr(conv, "status", None),
            "rel_path": getattr(conv, "rel_path", None),
            "detail": getattr(conv, "detail", None),
        }
    return export_paths({k: v for k, v in out.items() if v not in (None, "")})


def _key_of(state: dict, node_id: str = S.NODE_EXPORT) -> str:
    """算一下本节点的幂等键（写进状态供排查）。

    **容错**：算不出来就返回空串 —— 这是个"给人看"的附加信息，
    不该因为它让已经成功的导出失败。
    """
    try:
        node_input = idem.node_input_from_state(node_id, state)
        return idem.make_idempotency_key(B.meeting_id_of(state), node_id, node_input)
    except Exception:  # pragma: no cover
        return ""


def _audit(session: Any, state: dict, paths: dict) -> None:
    """写导出审计（`export` 动作已在 `audit.ACTIONS` 登记）。"""
    try:
        session.audit.log(
            _operator(state),
            "export",
            target_type="meeting",
            target_id=B.meeting_id_of(state),
            detail={"file": paths.get("filename") or "",
                    "version": paths.get("version")},
        )
    except Exception:
        logger.warning("导出审计写入失败", exc_info=True)


def _audit_archive(session: Any, state: dict, manifest: dict) -> None:
    """写归档审计。

    用 `segment_edit` 之外的动作会报错（`ACTIONS` 是白名单），
    所以复用 `export`：归档是导出流程的收尾，语义上最接近。
    **新增动作要先在 `infra/audit.py` 的 `ACTIONS` 登记，
    并按部署手册 §6.2 重新验收** —— 这里刻意不擅自加。
    """
    try:
        session.audit.log(
            _operator(state),
            "export",
            target_type="meeting",
            target_id=B.meeting_id_of(state),
            detail={"action": "archive",
                    "files": list((manifest.get("export_paths") or {}).keys())},
            result="success",
        )
    except Exception:
        logger.warning("归档审计写入失败", exc_info=True)


def _operator(state: dict) -> str:
    op = str(state.get("operator") or "").strip()
    return op or "system"


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["ARCHIVE_MANIFEST", "make_archive", "make_export"]
