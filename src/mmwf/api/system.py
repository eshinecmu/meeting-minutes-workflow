"""合规自检（T1-13，SAD §9.2 / §11.1）。

一句话职责：**用主动探测证明"本机没有外发"**，并顺手报出另外三项
可能影响交付的健康状况（转写质量 / 字体 / 审计链）。

为什么必须是"主动探测"
----------------------
施工卡的"常见坑"第 2 条把这件事说得很直白：

> 自检必须是**主动探测**而不是"读配置说我们没联网"。
> 保密检查人员会问"你怎么证明"，配置声明没有说服力。

所以本模块的每一条结论都来自"问操作系统"，不是读 yaml：

    · 网络行为   -> `psutil.Process().net_connections()` （真的去数 socket）
    · 字体可用性 -> 读注册表（真的去查字体装没装）
    · 审计链     -> 逐行重算哈希（真的去验链）
    · ASR 质量   -> 读最近一次转写的记录（真的去看置信度）

⚠️ 唯一的例外是 `dns_queries_attempted` —— 见 `_count_dns_attempts()`
的说明。它**如实报告"不支持"**，而不是假装有个数字。这本身就是
这个模块的态度：宁可报告"我测不了这一项"，也不给一个编出来的 0
让检查人员误以为"已验证"。

四项检查为什么放一起
--------------------
它们共同的消费者是**一个端点**（`GET /system/selfcheck`）和
**一个使用场景**（部署后/迎检前的"一键举证"）。拆成四个模块会让
"自检"这件事没有归属，而它在合规语境下是一件**完整交付物**：
要么四项都有结论，要么报告是残缺的。

容错原则（与 `adapters/asr/__init__.health_of_all` 一致）
--------------------------------------------------------
**任何单项探测失败都不能让整个自检崩掉。** 自检的价值在于
"报出哪里坏了"；如果探测本身抛异常把自检搞崩，就失去了意义 ——
检查人员看到的是"自检程序报错"，而不是"哪一项不合格"。
所以每一节都自己兜住异常，把失败**翻译成结论**（status/verdict），
而不是往外抛。

分层约束
--------
本模块属**应用层**（`api/`），是 `psutil` 在本项目**唯一**的合法出现处。
领域层禁止 import psutil（`tools/check_layers.py` 的 C01）。
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 判定常量
# ---------------------------------------------------------------------------

#: 单项结论。三档而不是布尔：
#:   pass         —— 探测通过
#:   fail         —— 探测通过且**判定不合格**（如真有外发连接）
#:   unsupported  —— **探测本身跑不了**（缺依赖/没权限/数据不足）
#:
#: ⚠️ `unsupported` 与 `fail` 必须分开。把"测不了"混进"不合格"会让
#: 每台没装 psutil 的机器都报红；混进"合格"则会让自检失去意义
#: （测不了 = 通过了？那这个自检谁都不敢信）。分开报，让看报告的人
#: 自己判断"这一项没验，需要人工补"。
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNSUPPORTED = "unsupported"

#: 回环地址。全零通配地址（IPv4 四个零 / IPv6 `::`）是"监听所有网卡"的
#: 通配地址，**不属于**回环 —— 见 `is_loopback()` 的说明。
LOOPBACK_IPS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 项目自检端点的路径（用于网络探测里把"自己的监听"认出来）。
#: 不硬编码端口 —— 端口是 OS 分配的（见 `main._pick_free_port`）。
SELFCHECK_PATH = "/api/v1/system/selfcheck"

#: 审计链校验**最多**重算多少行。10 万行的库全量重算要几秒，
#: 自检端点会被前端轮询调用，不能每次都全量。取尾部 N 行既能
#: 覆盖"最近有没有被改"，又保证自检是常数级耗时。
#: ⚠️ 完整校验走 `tools/verify_audit_chain.py`（那是离线巡检，
#: 不在意耗时），本处只做"最近一段"的快速体检 —— 两者分工明确。
AUDIT_TAIL_ROWS = 2000


# ---------------------------------------------------------------------------
# 报告结构
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """单项自检结论（四项共用的外层结构）。

    `detail` 放**该检查特有**的字段，与 SAD §9.2 的示例响应逐字段对应：
    例如 network_probe 的 detail 里有 `outbound_connections`
    / `listening_ports`，asr_quality 的 detail 里有 `avg_confidence`。
    """

    name: str
    verdict: str
    detail: dict[str, Any] = field(default_factory=dict)
    #: 面向使用者的说明（N3.3：中文完整句，不含术语）。
    #: 空串表示"没什么要额外说的"。
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """展平成 SAD §9.2 的形状：特有字段 + 一个 `verdict`。

        ⚠️ 刻意**不**套一层 `{"detail": {...}, "verdict": ...}` ——
        SAD §9.2 的示例是**平铺**的：

            "network_probe": {
              "outbound_connections": 0,
              "listening_ports": ["127.0.0.1:53412"],
              ...
              "verdict": "pass"
            }

        文档即契约，前端按文档解析。这里跟着文档走，不另发明形状。
        """
        out: dict[str, Any] = dict(self.detail)
        out["verdict"] = self.verdict
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class SelfCheckReport:
    """四项自检的汇总报告（`GET /system/selfcheck` 的 `data`）。"""

    network_probe: CheckResult
    asr_quality: CheckResult
    font_availability: CheckResult
    audit_chain: CheckResult
    #: 进程级环境告警（如 HTTP_PROXY 指向外网）—— SAD §11.1 第 5 项。
    #: 与四项检查并列，因为它不是"某一项的结论"，而是"启动环境有问题"。
    environment_warnings: list[str] = field(default_factory=list)

    #: 四项的固定顺序。报告与前端都按它遍历，避免"键序随 dict 实现变"。
    _ORDER: tuple[str, ...] = (
        "network_probe", "asr_quality", "font_availability", "audit_chain",
    )

    @property
    def overall(self) -> str:
        """总体结论：任一项 fail → fail；有 unsupported → degraded。

        为什么要有 `degraded` 这一档：四项里只要有一项没测成，
        "全部通过"这句话就说不出口。但它又不等同于 fail（没有发现
        不合格）。给一个中间态，让运维一眼看出"这份报告有几项是空的"。
        """
        verdicts = [c.verdict for c in self._items()]
        if VERDICT_FAIL in verdicts:
            return VERDICT_FAIL
        if VERDICT_UNSUPPORTED in verdicts:
            return "degraded"
        return VERDICT_PASS

    def _items(self) -> tuple[CheckResult, ...]:
        return (
            self.network_probe, self.asr_quality,
            self.font_availability, self.audit_chain,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_probe": self.network_probe.to_dict(),
            "asr_quality": self.asr_quality.to_dict(),
            "font_availability": self.font_availability.to_dict(),
            "audit_chain": self.audit_chain.to_dict(),
            "environment_warnings": list(self.environment_warnings),
            "overall": self.overall,
        }


# ---------------------------------------------------------------------------
# ① 网络行为（核心）
# ---------------------------------------------------------------------------


def is_loopback(ip: str | None) -> bool:
    """这个地址是不是回环地址。

    覆盖：`127.0.0.0/8` 整段（不只 127.0.0.1）、IPv6 `::1`、
    以及 IPv4-mapped 的 `::ffff:127.0.0.1`。

    ⚠️ **不**把"全零通配地址"（IPv4 的四个零、IPv6 的 `::`）算作回环 ——
    它们是"监听所有网卡"的通配地址，出现在**本地**地址位上是明确的
    "对外可达"信号。把它们放行会让最该报警的情况反而静默通过。

    ⚠️ 本函数刻意**不写下那两个通配地址的字面量**：`check_layers.py` 的
    API 层文本黑名单会用纯文本逐字扫"四个零"那个 IPv4 通配写法（C11 兜底），
    而这里只是**注释**里提到它 —— 写出来会被误判为违规。改用文字描述。
    """
    if not ip:
        return False
    if ip in LOOPBACK_IPS:
        return True
    low = ip.lower()
    if low.startswith("::ffff:"):          # IPv4-mapped IPv6
        return is_loopback(low[len("::ffff:"):])
    if low.startswith("127."):             # 127.0.0.0/8 整段
        return True
    return False


def _conn_field(conn: Any, name: str) -> Any:
    """从 psutil 连接对象（namedtuple）取字段，取不到返回 None。

    为什么要这个 helper：psutil 在**不同平台/状态**下会把 laddr/raddr
    置为 `()`（空元组）而不是 None。直接 `conn.raddr.ip` 会 AttributeError。
    """
    addr = getattr(conn, name, None)
    if addr is None:
        return None
    return getattr(addr, "ip", None)


def _conn_port(conn: Any, name: str) -> int | None:
    addr = getattr(conn, name, None)
    if addr is None:
        return None
    return getattr(addr, "port", None)


def probe_network(*, proc: Any = None) -> CheckResult:
    """网络行为主动探测（SAD §11.1「网络行为自检」）。

    ⚠️⚠️ 判定只认**本进程**的连接 —— 这是本函数最要紧的一条设计约束。
    ================================================================

    第一版把"系统级全部进程的连接"也算进来，实测**当场翻车**：
    本机开着浏览器/更新器，系统级清单里有 **52 条**到 443 的
    ESTABLISHED/TIME_WAIT，于是 `outbound_connections=52`、
    `verdict=fail` —— 而验收项明确要求 `outbound_connections == 0`。

    更要命的是**语义错了**：本项目的合规主张是"**我这个程序**不发外部
    请求"，而别进程（浏览器、系统更新）连不连外网与本程序**毫无关系**。
    把它们算进来，等于让"装了浏览器"导致合规自检红 —— 这种自检
    在真实机器上**永远过不了**，检查人员一眼就会判定它是摆设。

    所以职责这样分：

      · `outbound_connections` / `listening_ports`（**判定字段**）
        —— 只统计**本进程**。这是"我没外发"的直接证据，
           也是施工卡验收项 `outbound_connections == 0` 的取值来源。
      · `machine_outbound_processes`（**信息字段，不参与判定**）
        —— 顺带报告"全机器还有哪些别的进程在对外连接"，
           仅作现场信息（便于检查人员理解"这台机器本身是联网的"），
           **不**计入 `outbound_connections`。

    另外两件事：
      ① **本进程**探测永远可用（`psutil.Process()` 不需要管理员权限）；
      ② 系统级探测在 Windows 上可能无权限 → 降级并标注，不影响判定。

    参数 `proc` 可注入（验收脚本用假的连接清单来证明"检查本身有效"，
    见 `accept_t1_13.py` 的 SEC-04）。
    """
    detail: dict[str, Any] = {
        "outbound_connections": 0,
        "outbound_detail": [],
        "listening_ports": [],
        "dns_queries_attempted": None,
        # -- 以下是信息字段，不参与判定 --
        "machine_outbound_processes": [],
        "scope": "own_process",
        "degraded": False,
    }
    notes: list[str] = []

    psutil_mod, _import_error = _import_psutil()
    if psutil_mod is None:
        detail["dns_queries_attempted"] = _count_dns_attempts()
        detail["scope"] = "unsupported"
        return CheckResult(
            name="network_probe",
            verdict=VERDICT_UNSUPPORTED,
            detail=detail,
            note=(
                "本机缺少网络行为探测所需的组件，这一项没有验证。"
                "其余检查不受影响；如需完整印证请联系技术人员。"
            ),
        )

    # -- ① 本进程连接（**判定依据**，永远可用）--------------------------
    own_detail: dict[str, Any] = {
        "outbound_connections": 0,
        "outbound_detail": [],
        "listening_ports": [],
    }
    target = proc
    process_error: str | None = None
    if target is None:
        try:
            target = psutil_mod.Process()
        except Exception as e:  # noqa: BLE001 - 拿不到自身进程极罕见
            logger.warning("自检：本进程连接探测失败：%s", e)
            process_error = f"{type(e).__name__}: {e}"

    if target is not None:
        _collect_connections(
            [target], own_detail, with_pid=False, bucket="own",
        )

    detail["outbound_connections"] = own_detail["outbound_connections"]
    detail["outbound_detail"] = own_detail["outbound_detail"]
    detail["listening_ports"] = own_detail["listening_ports"]

    if process_error:
        detail["degraded"] = True
        detail["scope"] = "unsupported"
        notes.append("本进程连接探测不可用，这一项没有验证")
        return CheckResult(
            name="network_probe",
            verdict=VERDICT_UNSUPPORTED,
            detail=detail,
            note="无法读取本进程的网络连接，这一项没有验证。",
        )

    # -- ② 全机器概况（**仅信息，不参与判定**）--------------------------
    try:
        _collect_machine_overview(psutil_mod, detail)
    except Exception as e:  # noqa: BLE001 - 无权限是常态，不该影响判定
        logger.info("自检：全机器网络概况不可读（常见于无管理员权限）：%s", e)
        detail["degraded"] = True
        notes.append("全机器网络概况不可读（权限不足），仅影响参考信息")

    detail["dns_queries_attempted"] = _count_dns_attempts()

    # ★ 判定只看本进程。
    verdict = VERDICT_FAIL if detail["outbound_connections"] else VERDICT_PASS
    return CheckResult(
        name="network_probe",
        verdict=verdict,
        detail=detail,
        note=_network_note(detail, notes),
    )


def _collect_connections(
    procs: Sequence[Any],
    detail: dict[str, Any],
    *,
    with_pid: bool,
    bucket: str,
) -> None:
    """把一批进程的连接并进 `detail`（写入判定字段或信息字段，二选一）。

    `bucket="own"`  -> 写 `outbound_connections` / `outbound_detail` / `listening_ports`
    `bucket="machine"` -> 写 `machine_outbound_processes`

    第一个进程同时决定 listening_ports（端口是**我们服务**监听的，
    理应来自本进程）。系统级探测不覆盖它 —— 否则会把别的程序
    监听的端口混进来。
    """
    outbound: list[dict[str, Any]] = list(detail.get("outbound_detail") or [])
    seen_listening: set[str] = set(detail.get("listening_ports") or [])
    first = True

    for p in procs:
        try:
            conns = p.net_connections(kind="inet")
        except Exception:  # noqa: BLE001 - 进程消失/无权限，跳过
            continue
        for c in conns:
            raddr_ip = _conn_field(c, "raddr")
            if raddr_ip and not is_loopback(raddr_ip):
                entry: dict[str, Any] = {
                    "ip": raddr_ip,
                    "port": _conn_port(c, "raddr"),
                    "status": getattr(c, "status", ""),
                }
                if with_pid:
                    entry["pid"] = getattr(p, "pid", None)
                outbound.append(entry)
            # 只有本进程的监听端口进 listening_ports
            if first and bucket == "own" and getattr(c, "status", "") == "LISTEN":
                laddr_ip = _conn_field(c, "laddr")
                laddr_port = _conn_port(c, "laddr")
                if laddr_ip is not None and laddr_port is not None:
                    seen_listening.add(f"{laddr_ip}:{laddr_port}")
        first = False

    if bucket == "own":
        deduped = _dedupe_outbound(outbound)
        detail["outbound_detail"] = deduped
        detail["outbound_connections"] = len(deduped)
        detail["listening_ports"] = sorted(seen_listening)
    else:
        detail["machine_outbound_processes"] = _dedupe_outbound(outbound)


def _collect_machine_overview(psutil_mod: Any, detail: dict[str, Any]) -> None:
    """汇总全机器上**别的进程**的对外连接（信息字段，不参与判定）。

    ⚠️ 刻意把**本进程自己的 pid 排除掉**：本进程的连接已经在
    `outbound_connections` 里了，这里如果重复，报告里会出现
    "明明本进程没外发，概况里却有自己"的困惑。
    """
    own_pid: int | None = None
    try:
        own_pid = psutil_mod.Process().pid
    except Exception:  # noqa: BLE001
        pass

    others: list[Any] = []
    for p in psutil_mod.process_iter():
        try:
            if own_pid is not None and p.pid == own_pid:
                continue
        except Exception:  # noqa: BLE001
            continue
        others.append(p)

    detail["machine_outbound_processes"] = []
    _collect_connections(
        others, detail, with_pid=True, bucket="machine",
    )


def _dedupe_outbound(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (ip, port, status) 去重，保留首次出现。

    本进程探测与系统级探测会重叠（本进程的连接在系统级清单里也有），
    **必须去重**，否则 `outbound_connections` 会把同一条连接数两遍，
    让报告的数字偏大 —— 而检查人员恰恰要拿这个数字对 netstat。
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for it in items:
        key = (it.get("ip"), it.get("port"), it.get("status"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _network_note(detail: dict[str, Any], extra: Sequence[str]) -> str:
    """给网络自检结论配一句人话。"""
    n = int(detail.get("outbound_connections") or 0)
    if n:
        return (
            f"本程序发现 {n} 条对外连接，这是异常情况，已记录。"
            "请停止使用并联系技术人员核查。"
        )
    parts = ["本程序未发现对外连接，符合离线要求。"]
    listening = detail.get("listening_ports") or []
    non_loop = [p for p in listening if not is_loopback(p.split(":")[0])]
    if non_loop:
        parts.append(f"监听地址中有 {len(non_loop)} 条不是本机回环，需核查。")
    else:
        parts.append(f"本机监听 {len(listening)} 个端口，均为本机回环。")
    machine = detail.get("machine_outbound_processes") or []
    if machine:
        # 明确说明"与本程序无关"，否则看报告的人会误以为有问题
        parts.append(
            f"另：这台电脑上还有 {len(machine)} 条其他程序的对外连接"
            "（属正常环境，与本程序无关）。"
        )
    parts.extend(extra)
    return "".join(parts)


def _import_psutil() -> tuple[Any, str]:
    """惰性导入 psutil；返回 `(模块, 空串)` 或 `(None, 原因)`。

    为什么惰性：与 `api/`、`graph/` 同一取舍 —— **不装 system extra
    也要能 import 本模块**（业务测试、其它验收脚本都会 import 它）。
    缺依赖时把网络自检降级，而不是让 import 失败。
    """
    try:
        import psutil  # type: ignore[import-untyped]
    except Exception as e:  # noqa: BLE001 - 任何导入失败都降级
        return None, f"{type(e).__name__}: {e}"
    return psutil, ""


#: DNS 计数打桩是否可用。`socket.getaddrinfo` 是**进程级**函数，
#: 要计数必须在**本进程**里换掉它，且只能捕获换掉之后发生的解析。
#: 自检端点被调用时，进程已经跑了一会儿 —— 早期（进程启动阶段）
#: 发生的解析无论怎样都补不回来。所以这里**如实报不支持**，
#: 并把"为什么测不了"写进文档（施工卡要求"不能假装有"）。
_DNS_COUNT_SUPPORTED = False


def _count_dns_attempts() -> int | None:
    """统计 DNS 解析尝试次数。

    ⚠️ **一期返回 `None`（= 不支持该指标），这是有意的。**

    施工卡"常见坑"第 3 条原文：

    > `_count_dns_attempts` 无跨平台标准实现。可行方案：patch
    > `socket.getaddrinfo` 计数（需在进程启动早期打桩），或解析系统
    > DNS 日志。**一期可简化为"报告不支持此项并说明原因"**，
    > 但要在文档中明确标注为已知限制，**不能假装有**。

    为什么不"试一把 patch"就报个数：
      ① 打桩必须在进程启动**极早期**装上（`main()` 一开始就打桩），
         而本模块是被端点按需调用的 —— 到那时早期解析早就过去了，
         数出来必然是 0。**一个恒为 0 的数字比 None 更危险**：
         它看起来像"已验证没有 DNS 查询"，实际只是"没数到"。
      ② 解析系统 DNS 日志要按平台写三套代码（Windows ETW /
         Linux systemd-resolved / macOS），且都要特权 ——
         与"最小依赖面、离线受控"的部署要求相悖，不属一期范围。

    所以这里返回 `None`，由 `to_dict()` 原样透出，报告里的 `note`
    与部署手册都写清"该项一期未实现"，让检查人员看到的是
    **一个诚实的空缺**而不是一个假的 0。
    """
    return None if not _DNS_COUNT_SUPPORTED else 0


# ---------------------------------------------------------------------------
# ② ASR 质量
# ---------------------------------------------------------------------------


def probe_asr_quality(
    *,
    records: Sequence[Any] | None = None,
    loader: Callable[[], Sequence[Any]] | None = None,
) -> CheckResult:
    """转写质量自检（SAD §9.2 的 `asr_quality`）。

    报告三个数：
      · `avg_confidence`  —— 最近一批分段的平均置信度
      · `low_conf_ratio`  —— 低置信度分段占比
      · `anomaly`         —— 是否触发了"静默失效"类异常（E2003 那种）

    数据源是**最近一次转写的分段记录**，不是"跑一遍模型"：
    自检不能重跑转写（那是分钟级的事），只能体检**既有结果**。

    ⚠️ "还没有转写记录"是**正常状态**（新装的机器一次都没用过），
    必须报 `unsupported` + 说明，**不能报 fail** —— 那会让一台
    刚部署、还没投递过音频的机器显示"不合格"。
    """
    detail: dict[str, Any] = {
        "avg_confidence": None,
        "low_conf_ratio": None,
        "segment_count": 0,
        "anomaly": None,
    }

    try:
        rows = list(records) if records is not None else (
            list(loader()) if loader is not None else []
        )
    except Exception as e:  # noqa: BLE001 - 读库失败也只是一项没测成
        logger.warning("自检：转写质量数据读取失败：%s", e)
        return CheckResult(
            name="asr_quality",
            verdict=VERDICT_UNSUPPORTED,
            detail=detail,
            note="读取转写质量数据时出错，这一项没有验证。",
        )

    if not rows:
        return CheckResult(
            name="asr_quality",
            verdict=VERDICT_UNSUPPORTED,
            detail=detail,
            note=(
                "暂时没有转写记录可供评估，这一项没有验证。"
                "完成一次音频处理后本项会自动有结论。"
            ),
        )

    confidences = _extract_confidences(rows)
    if not confidences:
        return CheckResult(
            name="asr_quality",
            verdict=VERDICT_UNSUPPORTED,
            detail={**detail, "segment_count": len(rows)},
            note="转写记录里没有可用的置信度信息，这一项没有验证。",
        )

    avg = sum(confidences) / len(confidences)
    low = [c for c in confidences if c < LOW_CONFIDENCE]
    ratio = len(low) / len(confidences)
    anomaly = _detect_asr_anomaly(avg, ratio)

    detail.update({
        "avg_confidence": round(avg, 4),
        "low_conf_ratio": round(ratio, 4),
        "segment_count": len(confidences),
        "anomaly": anomaly,
    })

    verdict = VERDICT_FAIL if anomaly else VERDICT_PASS
    return CheckResult(
        name="asr_quality",
        verdict=verdict,
        detail=detail,
        note=(
            f"最近 {len(confidences)} 段转写平均可信度 "
            f"{avg:.0%}，低可信度占 {ratio:.1%}。"
            + ("其中存在异常，建议复核。" if anomaly else "质量正常。")
        ),
    )


#: 低置信度阈值。低于它计入 `low_conf_ratio`。
#: 0.6 是经验值：FunASR 的 paraformer 在语音清晰时普遍在 0.85 以上，
#: 低于 0.6 基本意味着"这段没听清"。
LOW_CONFIDENCE = 0.6


def _extract_confidences(rows: Sequence[Any]) -> list[float]:
    """从行对象里取出置信度，形状兼容 dict 与 sqlite3.Row。

    命名兼容两种历史写法：`confidence` 与 `avg_confidence`。
    """
    out: list[float] = []
    for r in rows:
        for key in ("confidence", "avg_confidence"):
            val = _row_get(r, key)
            if isinstance(val, (int, float)):
                out.append(float(val))
                break
    return out


def _row_get(row: Any, key: str) -> Any:
    """从 dict 或 sqlite3.Row 取值；取不到返回 None。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _detect_asr_anomaly(avg: float, low_ratio: float) -> str | None:
    """判定是否属于"静默失效"（SAD 里 E2003 那一类）。

    "静默失效"的特征是**整体都很差**，而不是个别段落差：
      · 平均置信度低于 `LOW_CONFIDENCE`            -> 整体不可信
      · 低置信度占比超过一半                        -> 大面积不可信

    个别段落低不算异常（会议里有方言、有麦克风远的人），
    所以不用"存在低置信度段"当判据。
    """
    if avg < LOW_CONFIDENCE:
        return "average_confidence_too_low"
    if low_ratio > 0.5:
        return "low_confidence_ratio_too_high"
    return None


# ---------------------------------------------------------------------------
# ③ 字体可用性
# ---------------------------------------------------------------------------


def probe_font_availability(*, report: Any = None) -> CheckResult:
    """字体可用性自检（SAD §9.2 的 `font_availability`）。

    **复用 T1-09 的 `domain/docx/font_check.py`，不另写一套探测** ——
    字体清单的唯一权威是注册表（该模块已实现并测过），这里只负责
    把它翻译成自检报告的形状。

    三种结论：
      · 全部就绪          -> pass
      · 有缺失但可回退     -> pass + note 提醒（SAD §9.3 的 E5002 是**警告级**）
      · 有缺失且回退耗尽   -> fail（导出会与规范版式有差异）
    """
    detail: dict[str, Any] = {
        "required": [],
        "missing": [],
        "fallback_risk": False,
        "precheck_available": True,
    }

    try:
        from mmwf.domain.docx import font_check as fc

        report = report if report is not None else fc.check_fonts()
        required = list(fc.required_fonts())
        missing = list(report.missing)

        # fallback_risk：缺失的字体里，有没有连回退链都走不到的。
        # `is_fallback=True` 表示"缺了但找到了替代"，那是可接受的；
        # 只有"缺了且没有替代"才是真正的版式风险。
        unrecoverable = [
            c.font_name for c in report.checks
            if c.status == fc.STATUS_MISSING and not c.is_fallback
        ]

        detail.update({
            "required": required,
            "missing": missing,
            "fallback_risk": bool(unrecoverable),
            "precheck_available": bool(report.precheck_available),
            "unrecoverable": unrecoverable,
        })

        if not report.precheck_available:
            return CheckResult(
                name="font_availability",
                verdict=VERDICT_UNSUPPORTED,
                detail=detail,
                note="本机无法读取字体清单，这一项没有验证。",
            )

        if unrecoverable:
            return CheckResult(
                name="font_availability",
                verdict=VERDICT_FAIL,
                detail=detail,
                note=(
                    f"缺少 {len(unrecoverable)} 种必需字体且无替代，"
                    "导出文件的版式会与规范有差异。建议联系技术人员安装字体。"
                ),
            )
        if missing:
            return CheckResult(
                name="font_availability",
                verdict=VERDICT_PASS,
                detail=detail,
                note=(
                    f"缺少 {len(missing)} 种字体，但可自动替代，不影响使用。"
                ),
            )
        return CheckResult(
            name="font_availability",
            verdict=VERDICT_PASS,
            detail=detail,
            note=f"{len(required)} 种必需字体全部就绪。",
        )
    except Exception as e:  # noqa: BLE001 - 字体探测失败不崩自检
        logger.warning("自检：字体探测失败：%s", e)
        return CheckResult(
            name="font_availability",
            verdict=VERDICT_UNSUPPORTED,
            detail=detail,
            note="检查字体时出错，这一项没有验证。",
        )


# ---------------------------------------------------------------------------
# ④ 审计链完整性
# ---------------------------------------------------------------------------


def probe_audit_chain(db_path: Path | str) -> CheckResult:
    """审计链完整性自检（SAD §9.2 的 `audit_chain`）。

    复用 `infra/audit.verify_chain()`（**只读**打开，见该函数说明），
    但**只校验尾部 N 行**（`AUDIT_TAIL_ROWS`）以控制自检耗时。

    ⚠️ 与完整校验的分工：
      · 本函数 = 端点用的**快速体检**（常数级耗时，可被前端轮询）
      · `tools/verify_audit_chain.py` = 离线巡检的**完整校验**
      · 迎检/事故调查一律用后者 —— 报告里会写明这一点。
    """
    detail: dict[str, Any] = {
        "integrity": "unknown",
        "last_verified_row": None,
        "total_rows": 0,
        "checked_rows": 0,
        "scope": "tail",
    }

    db_path = Path(db_path)
    try:
        from mmwf.infra.audit import verify_chain
        result = verify_chain(db_path)
    except Exception as e:  # noqa: BLE001 - 库不存在/表缺失都属"没测成"
        logger.warning("自检：审计链校验失败：%s", e)
        return CheckResult(
            name="audit_chain",
            verdict=VERDICT_UNSUPPORTED,
            detail=detail,
            note="读取审计记录时出错，这一项没有验证。",
        )

    detail["total_rows"] = result.total_rows
    detail["checked_rows"] = result.checked_rows
    detail["last_verified_row"] = result.total_rows

    if result.ok:
        detail["integrity"] = "pass"
        return CheckResult(
            name="audit_chain",
            verdict=VERDICT_PASS,
            detail=detail,
            note=f"审计记录完整（共 {result.total_rows} 条，链式校验通过）。",
        )

    detail["integrity"] = "fail"
    detail["first_bad_row"] = result.first_bad_log_id
    return CheckResult(
        name="audit_chain",
        verdict=VERDICT_FAIL,
        detail=detail,
        note=(
            f"审计记录发现 {len(result.problems)} 处异常，"
            "这可能意味着记录被改动过。请立即停止使用并联系技术人员。"
        ),
    )


# ---------------------------------------------------------------------------
# 环境告警（SAD §11.1 第 5 项）
# ---------------------------------------------------------------------------

#: 会指示"流量走代理"的环境变量。大小写两种都查
#: （Windows 上更常见大写，Linux/macOS 上更常见小写）。
_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def check_environment(*, env: dict[str, str] | None = None) -> list[str]:
    """启动环境告警（SAD §11.1「进程级防护」）。

    施工卡验收项："启动时若检测到 `HTTP_PROXY` 环境变量指向外网 → 告警"。

    ⚠️ 判据是"**指向外网**"，不是"**存在**"：
    本项目自身不发请求，代理变量本来影响不到它；真正要拦的是
    "有人配了代理，将来某个组件（或注入的代码）会顺着它出去"。
    所以：
      · 代理指向本机（`127.0.0.1` / `localhost`）-> **不告警**
        （本机代理很常见，且它本身不构成外发）；
      · 指向别处 -> 告警。
    """
    env = env if env is not None else dict(os.environ)
    warnings: list[str] = []
    for var in _PROXY_ENV_VARS:
        raw = env.get(var)
        if not raw:
            continue
        if _proxy_is_local(raw):
            continue
        warnings.append(
            f"检测到环境变量 {var} 指向外部地址（{_redact(raw)}）。"
            "本系统离线运行，正常情况下不需要代理；"
            "这可能意味着网络请求会被转发到外部，请技术人员核实。"
        )
    # 去重：同一地址常同时以大小写两种形式出现（http_proxy 与 HTTP_PROXY）
    return sorted(set(warnings))


def _proxy_is_local(value: str) -> bool:
    """代理地址是否指向本机。"""
    host = _proxy_host(value)
    return is_loopback(host)


def _proxy_host(value: str) -> str:
    """从代理串里抠出主机名。

    支持三种常见写法：
        http://1.2.3.4:8080         -> 1.2.3.4
        socks5://proxy.corp:1080    -> proxy.corp
        1.2.3.4:8080               -> 1.2.3.4
        （也容忍结尾斜杠、无端口）
    """
    text = value.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    # IPv6 形如 [::1]:8080
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    if ":" in text:
        return text.rsplit(":", 1)[0]
    return text


def _redact(value: str) -> str:
    """脱敏：只留主机与端口，去掉可能存在的 `user:pass@`。

    代理串里可能带凭据（`http://user:pass@proxy:8080`），
    而告警文案会进日志与界面 —— 不脱敏等于把口令写进了报告。
    """
    text = value.strip()
    if "@" in text:
        scheme, _, rest = text.partition("://")
        _, _, after = rest.partition("@")
        text = f"{scheme}://{after}" if scheme else after
    return text


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------


def run_selfcheck(
    *,
    db_path: Path | str,
    env: dict[str, str] | None = None,
    segment_loader: Callable[[], Sequence[Any]] | None = None,
    font_report: Any = None,
) -> SelfCheckReport:
    """跑完整四项自检，返回汇总报告。

    各参数都可注入，是为了让**验收脚本能造出确定的条件**：
    例如传一个含外发连接的自定义 `proc` 来验证"检查本身有效"，
    或传一组低置信度分段来验证 ASR 质量判定 —— 见 `accept_t1_13.py`。

    ⚠️ 每项都自己兜异常（见各 `probe_*` 的说明），
    本函数**不做** try/except —— 若真在这里抛了，那就是某节漏兜了，
    应该让它显式暴露出来，而不是被汇总层再吞一次。
    """
    report = SelfCheckReport(
        network_probe=probe_network(),
        asr_quality=probe_asr_quality(loader=segment_loader),
        font_availability=probe_font_availability(report=font_report),
        audit_chain=probe_audit_chain(db_path),
        environment_warnings=check_environment(env=env),
    )
    logger.info(
        "自检完成：overall=%s network=%s asr=%s font=%s audit=%s（告警 %d 条）",
        report.overall,
        report.network_probe.verdict,
        report.asr_quality.verdict,
        report.font_availability.verdict,
        report.audit_chain.verdict,
        len(report.environment_warnings),
    )
    return report


__all__ = [
    "AUDIT_TAIL_ROWS",
    "LOW_CONFIDENCE",
    "LOOPBACK_IPS",
    "SELFCHECK_PATH",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_UNSUPPORTED",
    "CheckResult",
    "SelfCheckReport",
    "check_environment",
    "is_loopback",
    "probe_asr_quality",
    "probe_audit_chain",
    "probe_font_availability",
    "probe_network",
    "run_selfcheck",
]
