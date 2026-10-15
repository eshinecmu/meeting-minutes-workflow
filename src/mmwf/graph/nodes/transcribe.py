"""`transcribe` 节点 —— 本地 ASR 转写 + 术语热词/回改（T1-05 / T1-07）。

本节点还清了两笔接线债
----------------------
  · **T1-07 债 1**：`GlossaryService.prepare_hotwords()` 此前从未被调用 ——
    术语库建好了、热词导得出来，但转写时根本没传进去。
    本节点在转写前取热词包，并把 `warnings`（超限/别名冲突）带进状态。
  · **T1-07 债 2**：`GlossaryService.correct_text()` 同样从未被调用 ——
    同音错字（"张山" -> "张三"）一直没被回改。本节点在转写后逐段回改，
    并把命中统计带进状态。

为什么回改放在**这里**而不是 review 节点
--------------------------------------
回改的目标是"转写稿里的同音错字"，它的输入是 ASR 原始输出。
放在 review 会让 review 同时承担"分流"和"改字"两件事，
而转写稿一旦被人手改过，再回改就危险了（可能改掉人故意写的字）。
在转写出口立即回改，语义最干净。

⚠️ 引擎不可用时**不抛异常**
---------------------------
`available() == False`（funasr 没装 / 模型文件不在）在开发机上是常态。
抛异常会让整条流水线跑不起来，连"用假引擎跑通编排"都做不到。
所以本节点在依赖缺失时把 `transcript` 置为空并记一条 E2001，
**由下游 `review` 节点**根据空稿报错 —— 那里报错才指得准
（"转写稿是空的，无法生成记录稿"）。
这个取舍让"编排"可以被独立验证，不必先装好 2GB 的模型。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mmwf.domain.asr_types import TranscriptResult, TranscriptSegmentResult, hit_hotwords
from mmwf.domain.errors import MmwfError
from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes.ingest import _Cfg, _resolve

logger = logging.getLogger(__name__)

#: 转写稿在存储层的位置（与 `review` 节点的读取路径一致）。
SEGMENTS_PATH = "transcript/segments.json"


def make_transcribe(
    deps: Any = None,
    *,
    provider_factory: Callable[[Any], Any] | None = None,
    glossary_factory: Callable[[Any, str], Any] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `transcribe` 节点。

    Args:
        provider_factory: `settings -> AsrProvider`。默认走适配器注册表
            `adapters.asr.build_from_settings`。测试注入假引擎。
        glossary_factory: `(settings, meeting_id) -> GlossaryService`。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)

    def transcribe(state: dict) -> dict:
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_TRANSCRIBE)

        session = cfg.session(state)
        # ① 热词包（T1-07 债 1）
        bundle = _hotwords(glossary_factory, cfg, state)
        hotwords = list(getattr(bundle, "words", []) or [])
        warn_msgs = list(getattr(bundle, "warnings", []) or [])

        engine = _provider(provider_factory, cfg, state)

        def do() -> dict[str, Any]:
            alerts = []
            try:
                got = _run_transcription(
                    engine, state, session=session, hotwords=hotwords,
                )
            except MmwfError as exc:
                if exc.code != "E2003":
                    raise
                alerts.append(exc.code)
                logger.error("%s：%s", exc.code, exc.user_message)
                _audit_quality(session, alerts, failed=True)
                raise
            result = got.get("_result")
            if result is not None and hotwords and not hit_hotwords(result.segments, hotwords):
                alerts.append("E2004")
            if result is not None:
                _audit_quality(session, alerts)
            got["_alert_codes"] = alerts
            return got

        got, cached = B.idempotent(cfg.idem_store, S.NODE_TRANSCRIBE, state, do)

        result: TranscriptResult | None = got.get("_result")
        out: dict[str, Any] = {
            "transcript": got.get("transcript") or [],
            "transcript_meta": got.get("transcript_meta") or {},
        }

        # ② 术语回改（T1-07 债 2）—— 逐段改，并把命中统计记进 meta
        if result is not None and result.segments:
            corrected, hits = _apply_corrections(
                glossary_factory, cfg, state, result, session=session,
            )
            if hits:
                out["transcript"] = _segments_to_dicts(corrected)
                meta = dict(out["transcript_meta"])
                meta["glossary_corrections"] = hits
                out["transcript_meta"] = meta

        errors = list(state.get("errors") or [])
        for code in got.get("_alert_codes", []):
            errors.append({**MmwfError(code=code).to_dict(), "node": S.NODE_TRANSCRIBE})
        for w in warn_msgs:
            errors.append({"code": "E2004", "node": S.NODE_TRANSCRIBE, "detail": str(w)})
        if got.get("_skipped"):
            errors.append({
                "code": "E2001",
                "node": S.NODE_TRANSCRIBE,
                "detail": got.get("_skip_reason") or "转写引擎不可用，未产出转写稿",
            })
        if errors != list(state.get("errors") or []):
            out["errors"] = errors

        logger.info("transcribe 完成（%s）：%d 段%s",
                    "幂等命中" if cached else "本次执行",
                    len(out["transcript"]),
                    f"，术语回改 {out['transcript_meta'].get('glossary_corrections')} 处"
                    if out["transcript_meta"].get("glossary_corrections") else "")
        return out

    transcribe.__name__ = S.NODE_TRANSCRIBE
    transcribe.__doc__ = "本地 ASR 转写（含热词注入与术语回改，幂等）"
    return transcribe


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _provider(factory: Any, cfg: _Cfg, state: dict) -> Any:
    if factory is not None:
        return factory(cfg.settings)
    from mmwf.adapters.asr import build_from_settings

    return build_from_settings(cfg.settings)


def _hotwords(factory: Any, cfg: _Cfg, state: dict) -> Any:
    """取热词包。**失败不阻断转写** —— 没有术语库照样能转写。

    这里刻意吞掉异常：术语库文件损坏不该让 2 小时音频转不了。
    代价是"热词没生效"，而它已经由 `bundle.warnings` 通道报告了
    （超限/冲突），再加一条"读库失败"的日志就够。
    """
    try:
        svc = _glossary(factory, cfg, state)
        if svc is None:
            return _EmptyBundle()
        return svc.prepare_hotwords()
    except Exception:
        logger.warning("热词包准备失败，本次转写不使用热词", exc_info=True)
        return _EmptyBundle()


class _EmptyBundle:
    __slots__ = ()

    words: list[str] = []
    warnings: list[str] = []


def _glossary(factory: Any, cfg: _Cfg, state: dict) -> Any:
    if factory is not None:
        return factory(cfg.settings, B.meeting_id_of(state))
    try:
        from mmwf.app.services.glossary import make_glossary_service

        return make_glossary_service(cfg.settings)
    except Exception:
        logger.warning("术语库服务不可用", exc_info=True)
        return None


def _run_transcription(engine: Any, state: dict, *, session: Any,
                       hotwords: list[str]) -> dict[str, Any]:
    """跑转写并落盘 `transcript/segments.json`。

    引擎不可用 -> 返回 `_skipped=True` 而不是抛错（见模块文档）。
    """
    if not _available(engine):
        return {
            "_result": None,
            "_skipped": True,
            "_skip_reason": _unavailable_reason(engine),
            "transcript": [],
            "transcript_meta": {"engine": getattr(engine, "name", "unknown"),
                                "available": False},
        }

    paths = _chunk_paths(state)
    if not paths:
        # 没有音频 -> 文字稿导入路径，交给下游
        return {
            "_result": None,
            "_skipped": True,
            "_skip_reason": "没有可转写的音频分片（按文字稿导入路径处理）",
            "transcript": [],
            "transcript_meta": {"engine": getattr(engine, "name", "unknown"),
                                "available": True, "mode": "text_import"},
        }

    results: list[TranscriptResult] = []
    for p in paths:
        results.append(B.guard(S.NODE_TRANSCRIBE,
                               lambda p=p: engine.transcribe(p, hotwords=hotwords)).validated())
    merged = _merge(results)
    _write_segments(session, merged, hotwords=hotwords)
    return {
        "_result": merged,
        "transcript": _segments_to_dicts(merged),
        "transcript_meta": _meta_of(merged, hotwords=hotwords),
    }


def _audit_quality(session: Any, codes: list[str], *, failed: bool = False) -> None:
    """仅持久化告警码；自检也能看见未进入 review 的 FATAL 结果。"""
    session.audit.log(session.operator, "transcribe_done", target_type="meeting",
                      target_id=session.meeting_id, detail={"asr_alert_codes": codes},
                      result="failure" if failed else "success")


def _available(engine: Any) -> bool:
    try:
        return bool(engine.available())
    except Exception:  # pragma: no cover - 引擎自身坏了
        return False


def _unavailable_reason(engine: Any) -> str:
    try:
        health = engine.health_check() or {}
    except Exception:  # pragma: no cover
        health = {}
    name = getattr(engine, "name", "未知引擎")
    model = health.get("model") or "未指定模型"
    return (f"转写引擎「{name}」不可用（模型 {model}）。"
            "请确认引擎依赖已安装且模型文件已就位。")


def _chunk_paths(state: dict) -> list[str]:
    """从 `chunk_plan` 里取出实际切好的分片路径。

    `chunk_plan` 是 `ingest` 的产物。它每项带 `chunk_files`
    （切分后才非空；`probe_only=True` 时是空的）——
    空的话退回源文件，让引擎自己处理整段音频。
    """
    out: list[str] = []
    for item in state.get("chunk_plan") or []:
        files = item.get("chunk_files") if isinstance(item, dict) else None
        if files:
            out.extend(str(f) for f in files)
        elif isinstance(item, dict) and item.get("source"):
            out.append(str(item["source"]))
    if out:
        return out
    return [str(p) for p in (state.get("audio_paths") or [])]


def _merge(results: list[TranscriptResult]) -> TranscriptResult:
    """把多个分片的转写结果合成一份，并**重排 seq**。

    为什么必须重排：每个分片各自从 `seq=1` 开始，直接拼起来会得到
    `1,2,3,1,2,3` —— 而下游（`review` 重建时）要求 `seq` 严格从 1 递增。
    时间戳也要整体平移，否则每段都从 0 开始，时间码全乱。
    """
    if len(results) == 1:
        return results[0]

    segments = []
    seq = 0
    offset = 0
    warns: list[str] = []
    for r in results:
        for seg in r.segments:
            seq += 1
            segments.append(_shift_segment(seg, seq, offset))
        warns.extend(r.warnings or ())
        offset += _span_of(r)

    return TranscriptResult(
        segments=tuple(segments),
        engine=results[0].engine,
        model=results[0].model,
        hotwords_applied=any(r.hotwords_applied for r in results),
        warnings=tuple(dict.fromkeys(warns)),
    )


def _shift_segment(seg: Any, new_seq: int, offset_ms: int) -> Any:
    words = tuple(
        type(w)(
            text=w.text,
            start_ms=w.start_ms + offset_ms,
            end_ms=w.end_ms + offset_ms,
        )
        for w in (getattr(seg, "words", ()) or ())
    )
    return TranscriptSegmentResult(
        seq=new_seq,
        text=seg.text,
        start_ms=seg.start_ms + offset_ms,
        end_ms=seg.end_ms + offset_ms,
        confidence=seg.confidence,
        words=words,
        flags=tuple(getattr(seg, "flags", ()) or ()),
    )


def _span_of(result: TranscriptResult) -> int:
    if not result.segments:
        return 0
    return max(int(s.end_ms) for s in result.segments)


def _segments_to_dicts(result: TranscriptResult) -> list[dict]:
    """转成检查点可安全序列化的形状（dataclass -> dict）。

    见 `ingest._plan_to_dicts` 的同款理由：状态会被 JSON 序列化，
    读回来是 dict，前后类型不一致会让下游属性访问失败。
    """
    return [
        {
            "seq": s.seq,
            "text": s.text,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "confidence": s.confidence,
            "words": [
                {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
                for w in (getattr(s, "words", ()) or ())
            ],
            "flags": list(getattr(s, "flags", ()) or ()),
        }
        for s in result.segments
    ]


def _meta_of(result: TranscriptResult, *, hotwords: list[str]) -> dict:
    return {
        "engine": result.engine,
        "model": result.model,
        "segment_count": len(result.segments),
        "avg_confidence": result.avg_confidence,
        "hotwords_applied": bool(result.hotwords_applied),
        "hotword_count": len(hotwords),
        "warnings": list(result.warnings or ()),
    }


def _write_segments(session: Any, result: TranscriptResult,
                    *, hotwords: list[str]) -> None:
    """把转写稿落盘（`transcript/segments.json`）。"""
    payload = {
        "engine": result.engine,
        "model": result.model,
        "hotwords_applied": bool(result.hotwords_applied),
        "hotword_count": len(hotwords),
        "warnings": list(result.warnings or ()),
        "segments": _segments_to_dicts(result),
    }
    session.store.write_json(SEGMENTS_PATH, payload)


def _apply_corrections(factory: Any, cfg: _Cfg, state: dict,
                       result: TranscriptResult, *, session: Any) -> tuple[Any, int]:
    """逐段做术语回改（T1-07 债 2）。

    返回 `(改后的结果, 命中总数)`。命中总数进 `transcript_meta`
    而不是 `errors` —— 回改成功不是错误，只是需要让审核人知道
    "这份稿子被机器改过 N 处"。
    """
    try:
        svc = _glossary(factory, cfg, state)
    except Exception:
        svc = None
    if svc is None:
        return result, 0

    total = 0
    changed = []
    for seg in result.segments:
        try:
            res = svc.correct_text(
                seg.text,
                target_type="transcript_segment",
                target_id=f"{B.meeting_id_of(state)}#{seg.seq}",
                write_audit=False,   # 逐段写审计会刷爆；汇总由 review 写
            )
        except Exception:
            logger.warning("术语回改失败，保留原文（seq=%s）", seg.seq, exc_info=True)
            changed.append(seg)
            continue
        total += int(getattr(res, "hits", 0) or 0)
        new_text = getattr(res, "text", None) or seg.text
        if new_text == seg.text:
            changed.append(seg)
        else:
            changed.append(TranscriptSegmentResult(
                seq=seg.seq, text=new_text,
                start_ms=seg.start_ms, end_ms=seg.end_ms,
                confidence=seg.confidence,
                words=tuple(getattr(seg, "words", ()) or ()),
                flags=tuple(getattr(seg, "flags", ()) or ()),
            ))

    if total:
        logger.info("术语回改命中 %d 处（meeting=%s）", total, B.meeting_id_of(state))
    return TranscriptResult(
        segments=tuple(changed),
        engine=result.engine,
        model=result.model,
        avg_confidence=result.avg_confidence,
        hotwords_applied=result.hotwords_applied,
        warnings=tuple(result.warnings or ()),
        raw_meta=dict(result.raw_meta or {}),
    ), total


__all__ = ["SEGMENTS_PATH", "make_transcribe"]
