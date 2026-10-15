"""系统级端点：启动握手 / 离线合规自检 / 字体预检。

这三个端点**不绑会议** —— 用户还没建任何会议时也该能查。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header

from mmwf.api import envelope as env
from mmwf.infra.audit import AuditLogger

from ._context import Runtime
from ._support import _load_recent_segments

logger = logging.getLogger(__name__)


def register(router: APIRouter, rt: Runtime) -> None:
    """注册系统级端点。"""

    # -- ① 握手 --------------------------------------------------------

    @router.post("/system/handshake")
    def handshake(x_mmwf_token: str | None = Header(default=None, alias="X-MMWF-Token")):
        """启动握手：前端拿到令牌后调一次，确认后端真的活着。

        这个端点**必须排在令牌依赖之下**（它本身就是来验令牌的）。
        成功时回一个空 data + 版本号，前端据此判断兼容性。
        """
        return env.ok(
            {"version": rt.cfg.app.version, "api_prefix": "/api/v1"},
            "已连接",
        )

    # -- ⑧ 系统自检 ----------------------------------------------------

    @router.get("/system/selfcheck")
    def selfcheck():
        """离线合规自检（F8.1 / V9 / 红线项 1，SAD §9.2）。

        四项：网络行为 / ASR 质量 / 字体可用性 / 审计链完整性。
        返回形状严格照 SAD §9.2 的示例（四项平铺 + 各自 `verdict`）。

        ⚠️ 三个设计要点（都是施工卡明确点到的）：

        ① **主动探测，不读配置**。网络行为真的去问操作系统
           （`psutil.Process().net_connections()`），不是声明"我们没联网"。
           保密检查人员会问"你怎么证明"，配置声明没有说服力。

        ② **判定只认本进程**。`outbound_connections` 统计的是
           **本服务进程**的对外连接 —— 别的程序（浏览器、系统更新）
           连不连外网与本程序无关。第一版把全系统连接算进来，
           实测本机有 52 条（浏览器），直接把验收项打红。详见
           `api/system.py` 的 `probe_network`。

        ③ **自检本身要被审计**。部署手册 §6.2 把 `selfcheck` 列为
           8 类最小核查项之一（AUD-08）—— "谁来查过、什么时候查的"
           本身也是要留痕的。所以这里写一条审计记录。
           ⚠️ 写审计失败**不阻断自检**：自检的价值是报出问题，
           如果因为审计写不进去就不给报告，等于把工具自己废了。
        """
        from mmwf.api import system as selfcheck_mod

        try:
            report = selfcheck_mod.run_selfcheck(
                db_path=rt.db_path,
                segment_loader=lambda: _load_recent_segments(rt.db_path),
            )
        except Exception:  # pragma: no cover - 兜底：自检绝不该 500
            # `run_selfcheck` 内部每项都兜了异常，走到这里说明汇总层
            # 本身出了问题（属于程序缺陷）。不冒堆栈给用户，
            # 记日志交给运维，响应仍是合法信封。
            logger.exception("自检汇总失败")
            raise

        # 留痕（AUD-08）。失败只记日志，不影响报告。
        try:
            AuditLogger(rt.db_path).log(
                "system",
                "selfcheck",
                target_type="system",
                detail={
                    "overall": report.overall,
                    "network": report.network_probe.verdict,
                    "asr": report.asr_quality.verdict,
                    "font": report.font_availability.verdict,
                    "audit": report.audit_chain.verdict,
                    "outbound_connections": report.network_probe.detail.get(
                        "outbound_connections"
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - 见 docstring ③
            logger.warning("自检留痕失败（不影响自检结论）", exc_info=True)

        message = {
            "pass": "自检完成，各项正常",
            "degraded": "自检完成，有项目未能验证",
            "fail": "自检完成，发现问题，请查看详情",
        }.get(report.overall, "自检完成")
        return env.ok(report.to_dict(), message)

    # -- ⑨ 字体预检（T1-14 A 档）---------------------------------------

    @router.get("/system/fonts")
    def system_fonts():
        """字体可用性预检（F5.7 / R7 / SAD §12.4）。

        ⚠️ **不构造 `ExportService`**（T1-14 裁决 D6）。
        `ExportService` 是**按会议**构造的（它要 `FileStore` 定位 manifest），
        而本端点是**系统级**的 —— 用户还没建任何会议时也该能查字体。
        为了查字体造一个假会议，等于把系统级端点焊到会议生命周期上。

        `check_fonts()` 的 `policy` 用默认值即可：它的默认是 `POLICY_WARN`，
        而**`font_policy` 并不在 `Settings` 里**（已核对 `settings.py`，
        只有 `provider: str = "docx_exporter"`）。别去读 `rt.cfg.docx.font_policy`。

        **`UNKNOWN` 必须与 `MISSING` 区分**（T1-14 裁决 D8）：
        探测不可用（非 Windows / 无权限）时 `font_check` 会把每项标为
        `UNKNOWN` / `precheck_available=False`。若把它当 `MISSING` 报出去，
        用户会去装一个本来就装着的字体 —— 这是**会浪费别人半天**的错误。

        ⚠️⚠️ **`build_check_result_json` 没有 `precheck_available` 键**
        （T1-14 验收抓到）。它给的是 `available`（同一个布尔值，
        `report.precheck_available`，只是名字不同），
        `build_check_result_json(font_check.py:356)` 原文：
        `{"available": report.precheck_available, ...}`。
        于是前端**拿不到**"这次探测到底可不可用"这个信息 ——
        它只看到 `all_installed=False` + `missing=[5 个字体]`，
        根本无从分辨"真缺"与"查不了"。裁决 D8 于是落空。
        修法：两个键都给（`available` 保持兼容 T1-09 的既有形态，
        `precheck_available` 是裁决 D8 要求的前端判据），
        并且探测不可用时**把 detail 里的 missing 降级成 unknown** ——
        光给个 flag 让前端自己判断是不够的，字段内容本身就不该说谎。
        """
        from mmwf.domain.docx.font_check import (
            STATUS_UNKNOWN,
            build_check_result_json,
            check_fonts,
        )

        report = check_fonts()
        payload = build_check_result_json(report)
        # ⚠️ `build_check_result_json` 给的键是
        # `available / all_installed / missing / policy / detail`，
        # **没有 `required`** —— 而 SAD §9.2 的示例响应里明确列了
        # `required`（"导出前要知道**需要哪些**字体，再看缺哪些"）。
        # 这里补上，不给既有函数改名（那是 T1-09 的产物，
        # 动它要连带回归 `test_docx_*`，不划算）。
        payload["required"] = [c.font_name for c in report.checks]

        precheck_available = bool(getattr(report, "precheck_available", True))
        # 同一个布尔值给两个键名 —— `available` 是 T1-09 的既有拼法（不动它，
        # 对外已有消费者），`precheck_available` 是裁决 D8 要求前端能看到的判据。
        payload["precheck_available"] = precheck_available

        if not precheck_available:
            # ★ 探测不可用时，**字段内容本身**就不能声称"缺"。
            # 只加一个 flag 让前端自己判断是不够的：任何直接读
            # `missing` 或 `all_installed` 的消费方（含将来的验收脚本、
            # 第三方集成）都会得出错误结论。这里把 detail 的 status
            # 统一降级为 unknown，并清空 missing —— 缺失清单在一个
            # "查不了"的探测里**没有任何信息量**。
            #
            # `all_installed` 也必须置 False：它不是"都装好了"，
            # 而是"未知"。此处保留 build_check_result_json 的原值
            # （通常已是 False），只在它为 True 时强制纠正 ——
            # 不能让"查不了"被读成"全齐了"。
            payload["detail"] = [
                {"font": d["font"], "status": STATUS_UNKNOWN,
                 "resolved": d.get("resolved")}
                for d in payload.get("detail", [])
            ]
            payload["missing"] = []
            payload["all_installed"] = False
            return env.ok(
                payload,
                "无法读取本机字体列表（可能是权限或系统限制），"
                "本次预检结果不可作为『字体缺失』的依据",
            )
        missing = list(getattr(report, "missing", ()) or ())
        if missing:
            return env.ok(
                payload,
                "本机缺少导出所需字体：" + "、".join(missing) + "，导出版式会回退",
            )
        return env.ok(payload, "字体齐备")
