"""审计端点（F8.2 / F8.7，T1-14 A 档）：日志检索与报告导出。

⚠️ 检索结果里的 `returned` **不是** `total`（裁决 D3）：`audit.query()`
的 `LIMIT` 直接作用于 SQL，拿不到全库总数。把本页条数叫 `total` 是错的
命名，用户会以为"全库就这么多"。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Body

from mmwf.api import envelope as env

from ._context import Runtime
from ._support import AUDIT_MAX_LIMIT


def register(router: APIRouter, rt: Runtime) -> None:
    """注册审计端点。"""

    # -- ⑩ 审计检索与报告导出（T1-14 A 档）------------------------------

    @router.get("/audit/logs")
    def audit_logs(
        from_ts: str | None = None,
        to_ts: str | None = None,
        operator: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        result: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ):
        """审计日志检索（F8.2 / F8.7，验收 AUD-07：按时间/人员/类型检索）。

        ⚠️ **`returned` 不是 `total`**（T1-14 裁决 D3）。
        `audit.query()` 的 `LIMIT` 直接作用于 SQL，拿不到全库总数。
        把本页条数叫 `total` 是**错的命名**（用户会以为"全库就这么多"）。
        这里用 `returned` 如实表达，另给 `has_more` 让前端知道能不能翻页。

        为什么不改 `audit.query` 加 `COUNT(*)`：那是 T1-04 的既有 API，
        为一个字段名去动它、还要连带回归它的全部单测，不划算。
        真需要总数时另开卡加 `count_query()`。

        ⚠️ **`limit` 必须有天花板**（T1-14 裁决 D7）：
        不设上限等于给了一个"把整库拉进内存"的接口。
        """
        from mmwf.infra.audit import AuditQuery, query as audit_query

        safe_limit = max(1, min(int(limit), AUDIT_MAX_LIMIT))
        safe_offset = max(0, int(offset))
        q = AuditQuery(
            from_ts=from_ts or None,
            to_ts=to_ts or None,
            operator=operator or None,
            action=action or None,
            target_type=target_type or None,
            target_id=target_id or None,
            result=result or None,
            limit=safe_limit,
            offset=safe_offset,
        )
        records = audit_query(rt.db_path, q)
        return env.ok(
            {
                "returned": len(records),
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": len(records) >= safe_limit,
                # ⚠️ `AuditRecord` **没有** `to_dict()`（只有 `detail_text`
                # 这个 property）。用 `dataclasses.asdict` 而不是手写字段表：
                # 手写会在 `AuditRecord` 加字段时**静默漏掉**，
                # 而审计检索"少给一个字段"是很难被发现的。
                "records": [asdict(r) for r in records],
            }
        )

    @router.post("/audit/export")
    def audit_export(payload: dict[str, Any] = Body(default={})):
        """生成审计报告（F8.7，验收 AUD-06：报告含完整性校验结论）。

        ⚠️ **返回 JSON，不落文件**（T1-14 裁决 D2）。三条理由：

        ① **落盘位置无处可放**。`filestore.PLAINTEXT_AREAS = {"export"}`，
           而 `export/` 是**按会议**组织的（`data/store/{m_id}/export/`）。
           审计报告不属于任何会议。
        ② **`export/` 是明文区，而审计报告必须加密**。`export.py` 模块文档
           写明：`export/` 是 data 目录里唯一不加密的地方，只准放**最终
           交付物**（要给领导看、要打印）。审计报告含全部操作记录，
           属敏感内容，放明文区是**违规**。
        ③ **前端要的是字段**。保密检查要的是 `integrity.result` 这个结论，
           JSON 让前端能渲染成任何格式；给文件反而逼前端去解析。

        那"可提交保密检查的报告"怎么办？JSON 里要素齐全，
        **由 Electron 侧导出成用户指定路径的文件** —— 用户本来就该选存哪。
        服务端落一份"不知道放哪、不该放明文、还可能被遗忘删掉"的副本，
        是负资产。

        ⚠️ **完整性失败时口径必须变**：`build_report` 会塞一句
        `action_required`（"立即停止使用本系统、封存设备，上报保密机构"）。
        这时还回"审计报告已生成"是**误导**。
        """
        from mmwf.infra.audit import build_report

        report = build_report(
            rt.db_path,
            from_ts=payload.get("from_ts") or None,
            to_ts=payload.get("to_ts") or None,
        )
        integrity = report.get("integrity", {}).get("result")
        if integrity != "pass":
            # 不抛异常：报告本身有效且必要（里面带着问题清单）。
            # 用 ok=True + 严重文案，让前端能把报告**展示出来**，
            # 而不是只看到一个错误码——用户需要看到的是"哪一行坏了"。
            return env.ok(
                report,
                "审计链完整性校验未通过，请立即按报告中的处置要求执行",
            )
        return env.ok(report, "审计报告已生成")
