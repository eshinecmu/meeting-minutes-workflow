"""转写稿端点（F1.5 / F2.2，T1-15）：读 / 改 / 批量接受。

⚠️ **API 层禁止 `import sqlite3`**（`tools/check_layers.py` 的
`FORBIDDEN_IN_API`）—— 所以读写 `transcript_segment` 一律走
`TranscriptStore`，端点里**不写一句 SQL**。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Path as PathParam

from mmwf.api import envelope as env
from mmwf.domain.errors import MmwfError
from mmwf.infra.audit import AuditLogger
from mmwf.infra.transcript_store import ACCEPT_MIN_CONFIDENCE

from ._context import Runtime
from ._support import (
    DEFAULT_OPERATOR,
    TRANSCRIPT_DEFAULT_LIMIT,
    TRANSCRIPT_MAX_LIMIT,
    _meeting_of_segment,
    _require_meeting,
)


def register(router: APIRouter, rt: Runtime) -> None:
    """注册转写稿端点。"""

    # -- ⑩ 语音稿：读 / 改 / 批量接受（T1-15）-----------------------------
    #
    # 这 4 个端点是 T1-14 铺好的 `infra/transcript_store.py` 的**接线**。
    #
    # ⚠️ **API 层禁止 `import sqlite3`**（`tools/check_layers.py` 的
    # `FORBIDDEN_IN_API`）——所以读写 `transcript_segment` 一律走仓储层，
    # 端点里**不写一句 SQL**（唯一的例外是「按 segment_id 反查 meeting_id」，
    # 那一步必须先知道会议才能构造 store，见 `_meeting_of_segment`）。

    @router.get("/meetings/{meeting_id}/transcript")
    def get_transcript(
        meeting_id: str = PathParam(...),
        limit: int = TRANSCRIPT_DEFAULT_LIMIT,
        offset: int = 0,
        min_seq: int | None = None,
        max_seq: int | None = None,
        only_review_needed: bool = False,
    ):
        """取转写分段（F1.5 / F2.2）。

        ★ **分页必须显式告知截断**。这是本端点最要紧的一条：

        `list_segments(limit=None)` 会把全篇吐出来，一份 3 小时会议的稿子
        上千段，不现实；但**默默截断更危险** —— 前端拿到 500 段就渲染 500 段，
        用户会以为"这会议就说了这么多"。所以响应里必须同时有：

          · `page.total` —— 全篇段数（来自 `count()`，**不是** `len(segments)`）
          · `truncated`  —— 布尔，且**由服务端算好**，不让前端自己算
          · `message`    —— 人话说明"共 N 段、本次显示 M 段"

        ⚠️ `page.total` 用 `len(segments)` 是错的：那是**本页条数**。
        拿它当总数 → 前端算出 `has_more=false` → 用户看不全还不自知。

        ⚠️ `only_review_needed` 是**端点层的过滤**，发生在**分页之后**，
        所以 `page.returned` 可能小于 `limit`。这是刻意不做进仓储层的：
        "只翻待复核的页"要在仓储层重做分页，而这是界面偏好不是数据语义。
        """
        _require_meeting(meeting_id, rt.db_path)
        store = rt.transcript_store(meeting_id)

        lim = max(1, min(int(limit), TRANSCRIPT_MAX_LIMIT))
        off = max(0, int(offset))

        rows = store.list_segments(
            limit=lim, offset=off, min_seq=min_seq, max_seq=max_seq
        )
        total = store.count()

        if only_review_needed:
            before_filter = len(rows)
            rows = [r for r in rows if r.review_flags]
            filtered_hits = len(rows)
        else:
            before_filter = len(rows)
            filtered_hits = len(rows)

        returned = len(rows)
        has_more = (off + before_filter) < total
        truncated = has_more

        data: dict[str, Any] = {
            "meeting_id": meeting_id,
            "segments": [r.to_dict() for r in rows],
            "page": {
                "limit": lim,
                "offset": off,
                "returned": returned,
                # ⚠️ 这里是**全篇**段数，不是本页条数
                "total": total,
                "has_more": has_more,
            },
            "counts": store.counts_by_review_state(),
            "truncated": truncated,
        }

        if total == 0:
            return env.ok(data, "尚未生成转写稿")

        if only_review_needed:
            # 过滤后条数与扫描条数不是一回事，两个数都要说，
            # 否则用户看到"本页 132 段"会以为系统少给了 368 段。
            msg = (
                f"已按「待人工核对」筛选：本页扫描 {before_filter} 段，"
                f"命中 {filtered_hits} 段；全篇共 {total} 段"
            )
        elif truncated:
            msg = (
                f"本次返回 {returned} 段（全篇共 {total} 段），"
                f"还有 {total - off - before_filter} 段未显示"
            )
        else:
            msg = f"共 {total} 段"
        return env.ok(data, msg)

    @router.patch("/segments/{segment_id}")
    def edit_segment(segment_id: str = PathParam(...), payload: dict[str, Any] = Body(default={})):
        """就地编辑某段（F2.2）。

        ⚠️⚠️ **路径里没有 `meeting_id`**（SAD §9.1 原文如此），
        而 `TranscriptStore` 是**按会议构造**的 —— 所以必须先反查归属，
        见 `_meeting_of_segment`。

        ⚠️⚠️ **`edit_segment` 会抛 `KeyError` / `ValueError`，两者都不是
        `MmwfError`**。不显式接住就会掉进 `@app.exception_handler(Exception)`
        → **E9001**，把"这个分段不存在"报成"系统故障"。
        这类错误的共同点是：**用户填错，系统背锅**。所以下面整段包在
        同一个 `try` 里 —— T1-14 的教训是"校验写在 try 外"会让同一类
        输入走两个不同的错误码（坑 2）。

        ⚠️ **`flags` 的 `None` 与 `[]` 不是一个意思**：
        `None` = 不动 flags，`[]` = 清空 flags。所以**绝不能**写
        `payload.get("flags") or []` —— 那会把"不传"静默变成"清空"，
        抹掉 `silence` / `overlap` 标记，让一段两人抢话的废数据
        从此"看起来没问题"。
        """
        text = payload.get("text")
        operator = payload.get("operator") or DEFAULT_OPERATOR

        # ★ 反查归属（必须在构造 store 之前）
        meeting_id = _meeting_of_segment(segment_id, rt.db_path)
        store = rt.transcript_store(meeting_id, audit=AuditLogger(rt.db_path))

        try:
            # ① 先自己拦一道"空正文"，文案才说得出人话
            if not isinstance(text, str) or not text.strip():
                raise MmwfError(
                    code="E1001",
                    detail=f"text 为空或不是字符串：{text!r}",
                    user_message=(
                        "分段正文不能为空。逐段校对是改错字；"
                        "要删掉整段请用别的操作。"
                    ),
                    context={"area": "transcript", "segment_id": segment_id},
                )

            # ② `flags` 原样传：None 表示"不动"，[] 表示"清空"
            flags = payload.get("flags")

            # ⚠️⚠️ **必须挡字符串**（T1-15 实测踩到）。
            # `payload.get("flags")` 若是 `"silence"`（用户以为给个标记名就行），
            # 它会一路传到 `_dump_flags()`，而那里是 `for f in flags` ——
            # **字符串是可迭代的**，于是静默存成 `["s","i","l","e","n","c","e"]`。
            #
            # 这类错误的可怕之处：**不报错、不告警**，只是那一列的标记
            # 从此再也不匹配任何白名单（`REVIEW_FLAGS` 里没有单个字母），
            # 于是"两人抢话"的分段看起来干干净净，复核直接放过。
            #
            # 判据用**白名单**（只接受序列）而不是黑名单（排除字符串）——
            # 黑名单永远会漏（`bytes`、`dict` 都是可迭代的，且 `dict`
            # 迭代出的是 key，更隐蔽）。
            if flags is not None and not isinstance(flags, (list, tuple, set, frozenset)):
                raise MmwfError(
                    code="E1001",
                    detail=f"flags 类型不支持：{type(flags).__name__}",
                    user_message=(
                        "分段标记要填一个数组（比如 [\"overlap\"]），"
                        "不能直接填一个词。不填表示不改动原有标记。"
                    ),
                    context={"area": "transcript", "segment_id": segment_id},
                )

            result = store.edit_segment(
                segment_id,
                text,
                operator=operator,
                flags=flags,
            )
        except MmwfError:
            raise
        except KeyError as exc:
            # 理论上进不来（上面已经反查过），但反查与编辑之间有竞态窗口：
            # 若另一端在此期间删了段，这里要给人话而不是 E9001。
            # ⚠️ 同样必须用 `MmwfError` 基类 —— `FileMissingError` 会吞掉
            # `user_message`，把文案落回"找不到要处理的文件"（见
            # `_meeting_of_segment` 的说明）。
            raise MmwfError(
                code="E1002",
                detail=f"segment_id={segment_id!r} 不存在：{exc}",
                user_message=(
                    "这一段刚被删掉了（可能是另一端重新转写过）。请刷新后再试。"
                ),
                context={"area": "transcript", "segment_id": segment_id},
            ) from exc
        except ValueError as exc:
            raise MmwfError(
                code="E1001",
                detail=f"分段正文不合法：{exc}",
                user_message=(
                    "这一段的正文没通过校验（不能为空或只有空格）。"
                    "逐段校对是改错字，不是删段落。"
                ),
                context={"area": "transcript", "segment_id": segment_id},
            ) from exc

        data = result.to_dict()
        # `changed=False` 是**正常返回**：用户点开看了一眼、原文没动就保存，
        # 是极常见的操作。把它当错误会让界面弹红框。
        msg = "已保存" if result.changed else "正文没有变化，仍记了一次校订"
        return env.ok(data, msg)

    @router.post("/meetings/{meeting_id}/review/accept")
    def accept_review(
        meeting_id: str = PathParam(...), payload: dict[str, Any] = Body(default={})
    ):
        """批量接受高置信分段（F2.2）。

        `accept_high_confidence` 的**两条判据都要满足**才接受：

          ① `confidence >= threshold`（分数够高）
          ② `flags` 里**不含**任何异常标记

        只看 ① 是错的 —— 引擎对"两人抢话"往往给出很高置信度
        （它很确定自己听见了字），但内容是叠在一起的废数据。
        这正是 `flags` 存在的理由。

        ⚠️ `threshold` 必须校验范围。`0` 会让**整篇**被接受
        （`confidence >= 0` 恒真），而这本该是最需要人工的场景。
        这是"参数静默失效"型缺陷，界面不会报错，只会让复核形同虚设。

        ⚠️ `message` 要**报三个数**：接受 / 此前已复核 / 因低分或有标记保留。
        只报第一个数，用户会以为剩下的是"系统忘了处理"。
        """
        _require_meeting(meeting_id, rt.db_path)
        operator = payload.get("operator") or DEFAULT_OPERATOR

        threshold = payload.get("threshold", ACCEPT_MIN_CONFIDENCE)
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise MmwfError(
                code="E1001",
                detail=f"threshold 不是数字：{threshold!r}",
                user_message="置信度阈值要填一个 0 到 1 之间的小数（比如 0.85）。",
                context={"area": "transcript", "meeting_id": meeting_id},
            )
        threshold = float(threshold)
        if not (0.0 < threshold <= 1.0):
            raise MmwfError(
                code="E1001",
                detail=f"threshold={threshold} 越界（要求 0 < threshold <= 1）",
                user_message=(
                    "置信度阈值要在 0 和 1 之间（不含 0）。"
                    "填 0 等于把整篇稿子都放过，那样复核就没意义了。"
                ),
                context={"area": "transcript", "meeting_id": meeting_id},
            )

        segment_ids = payload.get("segment_ids")
        if segment_ids is not None and not isinstance(segment_ids, (list, tuple, set)):
            raise MmwfError(
                code="E1001",
                detail=f"segment_ids 不是数组：{type(segment_ids).__name__}",
                user_message="segment_ids 要填一个数组（不填表示处理全篇）。",
                context={"area": "transcript", "meeting_id": meeting_id},
            )

        store = rt.transcript_store(meeting_id, audit=AuditLogger(rt.db_path))
        result = store.accept_high_confidence(
            operator=operator,
            threshold=threshold,
            segment_ids=list(segment_ids) if segment_ids is not None else None,
        )
        data = result.to_dict()
        if result.accepted == 0:
            msg = (
                f"没有可接受的分段（{result.skipped_reviewed} 段此前已复核，"
                f"{result.skipped_low_confidence} 段因置信度不足或有异常标记保留人工核对）"
            )
        else:
            msg = (
                f"已接受 {result.accepted} 段；{result.skipped_reviewed} 段此前已复核，"
                f"{result.skipped_low_confidence} 段因置信度不足或有异常标记保留人工核对"
            )
        return env.ok(data, msg)
