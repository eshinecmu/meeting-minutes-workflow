"""T1-13 合规自检单元测试。

覆盖三块：
  ① `api/system.py` 的四项探测与判定逻辑（**重点在判定边界**，
     尤其是"本进程 vs 全机器"这条最容易搞错的分工）；
  ② 环境告警（HTTP_PROXY 指向外网）的判据与脱敏；
  ③ `/system/selfcheck` 端点（信封、令牌门、报告形状）。

⚠️ 本文件的重点是**防假绿**：判定逻辑如果写错成"永远 pass"，
自检就成了摆设。所以每个 verdict 取值都有正向与反向用例，
而不是只断言"能返回一个 dict"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmwf.api import system as sc


# ---------------------------------------------------------------------------
# ①② 回环判定 —— 所有网络判定的地基
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.2", "127.1.2.3",
                                "::1", "localhost", "::ffff:127.0.0.1"])
def test_is_loopback_true(ip):
    """回环谱系：整个 127/8、IPv6 ::1、IPv4-mapped 写法都要认。"""
    assert sc.is_loopback(ip) is True


@pytest.mark.parametrize("ip", ["0.0.0.0", "::", "10.0.0.1", "192.168.1.1",
                                "8.8.8.8", "", None, "example.com"])
def test_is_loopback_false(ip):
    """⚠️ `0.0.0.0` / `::` 必须是 **False**。

    它们是"监听所有网卡"的通配地址，出现在本地地址位上恰恰是
    **最该报警**的信号。把它们当回环放行，等于让最严重的情况静默通过。
    """
    assert sc.is_loopback(ip) is False


# ---------------------------------------------------------------------------
# 假进程 / 假连接 —— 用来造出"确定的外发"以验证检查本身有效
# ---------------------------------------------------------------------------


class _Addr:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port


class _Conn:
    def __init__(self, status, laddr=None, raddr=None):
        self.status = status
        self.laddr = laddr
        self.raddr = raddr


class FakeProcess:
    """假进程：返回一批预设连接。用于证明"检查能检出外发"。"""

    def __init__(self, conns=None, pid=99999):
        self._conns = conns or []
        self.pid = pid

    def net_connections(self, kind="inet"):
        return list(self._conns)


# ---------------------------------------------------------------------------
# ① 网络探测：判定只认本进程
# ---------------------------------------------------------------------------


def test_network_own_process_clean_is_pass():
    """本进程只有回环连接 -> outbound=0 -> pass。"""
    p = FakeProcess([
        _Conn("LISTEN", laddr=_Addr("127.0.0.1", 53412)),
        _Conn("ESTABLISHED", laddr=_Addr("127.0.0.1", 5000),
              raddr=_Addr("127.0.0.1", 53412)),
    ])
    res = sc.probe_network(proc=p)
    assert res.verdict == sc.VERDICT_PASS
    assert res.detail["outbound_connections"] == 0
    assert res.detail["listening_ports"] == ["127.0.0.1:53412"]


def test_network_detects_real_outbound_and_fails():
    """★ SEC-04 的核心：注入一条真外发 -> 必须 fail。

    这条用例是"验证检查本身有效，不是摆设"在**单元层**的体现。
    如果哪天有人把判定写宽（比如把非回环也算 pass），这条会当场红。
    """
    p = FakeProcess([
        _Conn("LISTEN", laddr=_Addr("127.0.0.1", 53412)),
        _Conn("ESTABLISHED", laddr=_Addr("127.0.0.1", 5000),
              raddr=_Addr("203.0.113.9", 443)),
    ])
    res = sc.probe_network(proc=p)
    assert res.verdict == sc.VERDICT_FAIL, "有外发连接却没判 fail —— 自检失效"
    assert res.detail["outbound_connections"] == 1
    assert res.detail["outbound_detail"][0]["ip"] == "203.0.113.9"
    assert res.detail["outbound_detail"][0]["port"] == 443


def test_network_listening_on_wildcard_is_flagged():
    """★ 监听在 0.0.0.0 上：不算"外发"，但 note 必须点名。

    通配监听是 C11 红线（只允许回环），属于必须报出来的情况。
    """
    p = FakeProcess([_Conn("LISTEN", laddr=_Addr("0.0.0.0", 53412))])
    res = sc.probe_network(proc=p)
    # 它没有"出站连接"，所以不是 fail —— 但绝不能被当成正常
    assert res.detail["listening_ports"] == ["0.0.0.0:53412"]
    assert "不是本机回环" in res.note


def test_network_scope_is_own_process():
    """探测范围必须自报 `own_process` —— 这是"证明的是我自己"的凭据。"""
    res = sc.probe_network(proc=FakeProcess())
    assert res.detail["scope"] == "own_process"


def test_network_empty_laddr_and_raddr_do_not_crash():
    """psutil 在某些状态下把 laddr/raddr 置为 `()` 而不是 None。

    直接取 `.ip` 会 AttributeError —— 这条防止"探测自己把自己搞崩"。
    """
    p = FakeProcess([
        _Conn("NONE", laddr=None, raddr=None),
        _Conn("CLOSE_WAIT", laddr=(), raddr=()),
    ])
    res = sc.probe_network(proc=p)   # 不抛就算过
    assert res.detail["outbound_connections"] == 0


def test_dedupe_outbound():
    """同一条连接被数两次时必须去重 —— 否则报告数字偏大。"""
    items = [
        {"ip": "1.1.1.1", "port": 443, "status": "ESTABLISHED"},
        {"ip": "1.1.1.1", "port": 443, "status": "ESTABLISHED"},
        {"ip": "1.1.1.1", "port": 80, "status": "ESTABLISHED"},
    ]
    out = sc._dedupe_outbound(items)
    assert len(out) == 2


def test_machine_overview_excludes_own_pid():
    """★ 全机器概况必须排除本进程 —— 否则报告里会"自己重复自己"。"""
    detail = {"machine_outbound_processes": []}
    psutil = _FakePsutil(
        own_pid=1111,
        procs={
            1111: [_Conn("ESTABLISHED", raddr=_Addr("203.0.113.1", 443))],
            2222: [_Conn("ESTABLISHED", raddr=_Addr("198.51.100.5", 443))],
        },
    )
    sc._collect_machine_overview(psutil, detail)
    ips = [e["ip"] for e in detail["machine_outbound_processes"]]
    assert "198.51.100.5" in ips
    assert "203.0.113.1" not in ips, "本该排除的本进程连接出现在了概况里"


class _FakePsutil:
    """最小可用的 psutil 替身（只给 _collect_machine_overview 用）。"""

    def __init__(self, own_pid, procs):
        self._own = FakeProcess(pid=own_pid)
        self._procs = [FakeProcess(conns=c, pid=pid) for pid, c in procs.items()]

    def Process(self):
        return self._own

    def process_iter(self):
        return iter(self._procs)


# ---------------------------------------------------------------------------
# ② ASR 质量
# ---------------------------------------------------------------------------


def test_asr_no_records_is_unsupported_not_fail():
    """★ 没有转写记录是**正常状态**（新装机），必须 unsupported 而非 fail。

    报 fail 会让一台刚部署、还没投递过音频的机器显示"不合格"。
    """
    res = sc.probe_asr_quality(records=[])
    assert res.verdict == sc.VERDICT_UNSUPPORTED
    assert res.detail["avg_confidence"] is None


def test_asr_high_quality_is_pass():
    res = sc.probe_asr_quality(records=[{"confidence": 0.95}, {"confidence": 0.9}])
    assert res.verdict == sc.VERDICT_PASS
    assert res.detail["avg_confidence"] == pytest.approx(0.925)
    assert res.detail["anomaly"] is None


def test_asr_globally_bad_is_fail():
    """整体低置信度 = 静默失效（E2003 那一类）-> fail。"""
    res = sc.probe_asr_quality(
        records=[{"confidence": 0.3}, {"confidence": 0.4}, {"confidence": 0.35}]
    )
    assert res.verdict == sc.VERDICT_FAIL
    assert res.detail["anomaly"] == "average_confidence_too_low"


def test_asr_few_low_segments_is_not_anomaly():
    """★ 个别段落低置信度**不算**异常。

    会议里有方言、有麦克风远的人 —— 用"存在低置信度段"当判据会
    把正常会议全判成异常，自检立刻就没人信了。
    """
    res = sc.probe_asr_quality(
        records=[{"confidence": 0.95}, {"confidence": 0.92},
                 {"confidence": 0.94}, {"confidence": 0.4}]
    )
    assert res.verdict == sc.VERDICT_PASS
    assert res.detail["anomaly"] is None


def test_asr_ignores_missing_confidence():
    """全是 NULL 置信度 -> unsupported（而不是把 None 当 0）。"""
    res = sc.probe_asr_quality(records=[{"confidence": None}, {"flags": "[]"}])
    assert res.verdict == sc.VERDICT_UNSUPPORTED


def test_asr_loader_exception_is_contained():
    """读库抛异常不能让自检崩掉 —— 要翻译成 unsupported。"""
    def boom():
        raise RuntimeError("db down")

    res = sc.probe_asr_quality(loader=boom)
    assert res.verdict == sc.VERDICT_UNSUPPORTED


# ---------------------------------------------------------------------------
# ③ 字体可用性
# ---------------------------------------------------------------------------


class _FontCheck:
    def __init__(self, name, status, resolved, is_fallback):
        self.font_name = name
        self.status = status
        self.resolved = resolved
        self._fb = is_fallback

    @property
    def is_fallback(self):
        return self._fb


class _FontReport:
    def __init__(self, checks, missing, precheck_available=True):
        self.checks = tuple(checks)
        self.missing = tuple(missing)
        self.precheck_available = precheck_available


def test_font_all_installed_pass():
    from mmwf.domain.docx import font_check as fc
    rep = _FontReport(
        checks=[_FontCheck("仿宋_GB2312", fc.STATUS_INSTALLED, "仿宋_GB2312", False)],
        missing=[],
    )
    res = sc.probe_font_availability(report=rep)
    assert res.verdict == sc.VERDICT_PASS


def test_font_missing_but_fallback_pass():
    """缺字体但能回退 -> pass（SAD §9.3 的 E5002 是**警告级**）。"""
    from mmwf.domain.docx import font_check as fc
    rep = _FontReport(
        checks=[_FontCheck("方正小标宋简体", fc.STATUS_MISSING, "黑体", True)],
        missing=["方正小标宋简体"],
    )
    res = sc.probe_font_availability(report=rep)
    assert res.verdict == sc.VERDICT_PASS
    assert res.detail["fallback_risk"] is False


def test_font_missing_no_fallback_fails():
    """★ 缺字体且回退链耗尽 -> fail + fallback_risk=True。

    这才是真正的版式风险（导出会与规范有差异）。
    """
    from mmwf.domain.docx import font_check as fc
    rep = _FontReport(
        checks=[_FontCheck("黑体", fc.STATUS_MISSING, "黑体", False)],
        missing=["黑体"],
    )
    res = sc.probe_font_availability(report=rep)
    assert res.verdict == sc.VERDICT_FAIL
    assert res.detail["fallback_risk"] is True
    assert res.detail["unrecoverable"] == ["黑体"]


def test_font_precheck_unavailable_is_unsupported():
    from mmwf.domain.docx import font_check as fc
    rep = _FontReport(
        checks=[_FontCheck("黑体", fc.STATUS_UNKNOWN, "黑体", False)],
        missing=[], precheck_available=False,
    )
    res = sc.probe_font_availability(report=rep)
    assert res.verdict == sc.VERDICT_UNSUPPORTED


# ---------------------------------------------------------------------------
# ④ 审计链
# ---------------------------------------------------------------------------


def test_audit_chain_pass_on_real_db(tmp_path):
    """真库（迁移后）链完好 -> pass。"""
    from mmwf.infra.audit import AuditLogger
    from mmwf.infra.migrations.runner import migrate

    db = tmp_path / "mmwf.db"
    migrate(db)
    AuditLogger(db).log("tester", "login", detail={"client": "unit"})
    res = sc.probe_audit_chain(db)
    assert res.verdict == sc.VERDICT_PASS
    assert res.detail["integrity"] == "pass"
    assert res.detail["total_rows"] == 1
    assert res.detail["last_verified_row"] == 1


def test_audit_chain_empty_db_pass(tmp_path):
    """空库（迁移后无记录）链是"空即完好" -> pass，不是 unsupported。"""
    from mmwf.infra.migrations.runner import migrate

    db = tmp_path / "mmwf.db"
    migrate(db)
    res = sc.probe_audit_chain(db)
    assert res.verdict == sc.VERDICT_PASS
    assert res.detail["total_rows"] == 0


def test_audit_chain_detects_tamper(tmp_path):
    """★ 直改库里的 row_hash -> 必须 fail。证明审计链检查不是摆设。

    ⚠️ 要绕过的不只是"应用层没有 UPDATE 接口"——`001_init.sql` 里装了
    **append-only 触发器**，直接 `UPDATE audit_log` 会被数据库拒绝
    （`sqlite3.IntegrityError: audit_log is append-only`）。

    真实攻击者是**绕过应用直改 db 文件**（测试方案 AUD-03 的设定），
    他当然也会把触发器一并删掉。所以这里先 `DROP TRIGGER` 再改，
    模拟"文件到了攻击者手里"的最坏情况 —— 而链式哈希正是为这种
    情况准备的最后一道防线。
    """
    import sqlite3
    from mmwf.infra.audit import AuditLogger
    from mmwf.infra.migrations.runner import migrate

    db = tmp_path / "mmwf.db"
    migrate(db)
    log = AuditLogger(db)
    log.log("tester", "login")
    log.log("tester", "logout")

    conn = sqlite3.connect(db)
    # 模拟离线攻击：摘掉防篡改触发器，再改内容
    for t in ("audit_log_no_update", "audit_log_no_delete",
              "trg_audit_no_update", "trg_audit_no_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {t}")
    conn.execute("UPDATE audit_log SET operator='hacker' WHERE log_id=1")
    conn.commit()
    conn.close()

    res = sc.probe_audit_chain(db)
    assert res.verdict == sc.VERDICT_FAIL
    assert res.detail["integrity"] == "fail"


def test_audit_chain_missing_db_is_fail(tmp_path):
    """★ 库文件不存在 -> **fail**（不是 unsupported）。

    这里刻意与"读库出错"区分开：
      · 库**不存在** = 没有可校验的对象 = **完整性失败**
        （自检跑在一个连审计库都没有的系统上，这本身就是问题）；
      · 库存在但**读不出来**（权限/损坏）= 探测跑不成 = unsupported。

    把"库不存在"放成 unsupported 会让"审计库丢了"这种严重情况
    只显示为"这一项没测成"，而它其实是个 fail 级事故。
    """
    res = sc.probe_audit_chain(tmp_path / "nope.db")
    assert res.verdict == sc.VERDICT_FAIL
    assert res.detail["integrity"] == "fail"


# ---------------------------------------------------------------------------
# ⑤ 环境告警（HTTP_PROXY）
# ---------------------------------------------------------------------------


def test_proxy_external_warns():
    w = sc.check_environment(env={"HTTP_PROXY": "http://203.0.113.10:8080"})
    assert len(w) == 1
    assert "HTTP_PROXY" in w[0]


def test_proxy_local_does_not_warn():
    """代理指向本机 -> 不告警（本机代理常见，且不构成外发）。"""
    assert sc.check_environment(env={"HTTP_PROXY": "http://127.0.0.1:58045"}) == []
    assert sc.check_environment(env={"http_proxy": "http://localhost:8080"}) == []


def test_proxy_case_variants_warn():
    w = sc.check_environment(env={"http_proxy": "http://198.51.100.7:3128"})
    assert len(w) == 1


def test_proxy_credentials_are_redacted():
    """★ 代理串里的凭据**不得**出现在告警文案里。

    告警会进日志与界面 —— 不脱敏等于把口令写进报告。
    """
    w = sc.check_environment(
        env={"HTTPS_PROXY": "http://user:s3cret@203.0.113.10:8080"}
    )
    assert len(w) == 1
    assert "s3cret" not in w[0]
    assert "user" not in w[0]
    assert "203.0.113.10" in w[0]


def test_proxy_host_parsing():
    assert sc._proxy_host("http://1.2.3.4:8080") == "1.2.3.4"
    assert sc._proxy_host("socks5://proxy.corp:1080") == "proxy.corp"
    assert sc._proxy_host("1.2.3.4:8080") == "1.2.3.4"
    assert sc._proxy_host("[::1]:8080") == "::1"


def test_no_proxy_env_no_warning():
    assert sc.check_environment(env={}) == []


# ---------------------------------------------------------------------------
# ⑥ 汇总报告
# ---------------------------------------------------------------------------


def test_overall_pass_when_all_pass():
    r = sc.SelfCheckReport(
        network_probe=sc.CheckResult("network_probe", sc.VERDICT_PASS),
        asr_quality=sc.CheckResult("asr_quality", sc.VERDICT_PASS),
        font_availability=sc.CheckResult("font_availability", sc.VERDICT_PASS),
        audit_chain=sc.CheckResult("audit_chain", sc.VERDICT_PASS),
    )
    assert r.overall == sc.VERDICT_PASS


def test_overall_degraded_when_any_unsupported():
    """★ 有 unsupported -> `degraded`，不是 pass 也不是 fail。

    "有一项没测成"时说不出"全部通过"，但也没发现不合格 ——
    需要这个中间态，让运维一眼看出"这份报告有几项是空的"。
    """
    r = sc.SelfCheckReport(
        network_probe=sc.CheckResult("network_probe", sc.VERDICT_PASS),
        asr_quality=sc.CheckResult("asr_quality", sc.VERDICT_UNSUPPORTED),
        font_availability=sc.CheckResult("font_availability", sc.VERDICT_PASS),
        audit_chain=sc.CheckResult("audit_chain", sc.VERDICT_PASS),
    )
    assert r.overall == "degraded"


def test_overall_fail_dominates():
    r = sc.SelfCheckReport(
        network_probe=sc.CheckResult("network_probe", sc.VERDICT_FAIL),
        asr_quality=sc.CheckResult("asr_quality", sc.VERDICT_UNSUPPORTED),
        font_availability=sc.CheckResult("font_availability", sc.VERDICT_PASS),
        audit_chain=sc.CheckResult("audit_chain", sc.VERDICT_PASS),
    )
    assert r.overall == sc.VERDICT_FAIL


def test_check_result_to_dict_is_flat():
    """★ SAD §9.2 的形状是**平铺**的（特有字段 + verdict），不套 detail。

    文档即契约，前端按文档解析。这条守住"不另发明形状"。
    """
    res = sc.CheckResult(
        "network_probe", sc.VERDICT_PASS,
        detail={"outbound_connections": 0, "listening_ports": ["127.0.0.1:1"]},
        note="ok",
    )
    d = res.to_dict()
    assert d["outbound_connections"] == 0
    assert d["listening_ports"] == ["127.0.0.1:1"]
    assert d["verdict"] == "pass"
    assert "detail" not in d


def test_report_to_dict_shape():
    r = sc.run_selfcheck(db_path="does-not-exist.db", env={})
    d = r.to_dict()
    for key in ("network_probe", "asr_quality", "font_availability",
                "audit_chain", "environment_warnings", "overall"):
        assert key in d
    assert d["network_probe"]["verdict"] in {
        sc.VERDICT_PASS, sc.VERDICT_FAIL, sc.VERDICT_UNSUPPORTED,
    }


def test_report_is_json_serializable():
    """整个报告必须能直接 json.dumps —— 端点会这么发它。"""
    r = sc.run_selfcheck(db_path="does-not-exist.db", env={})
    json.dumps(r.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# ⑦ run_selfcheck 的容错：任何单项炸掉都不能让整份报告没有
# ---------------------------------------------------------------------------


def test_run_selfcheck_survives_every_probe_failing(monkeypatch):
    """★ 各项探测**内部依赖**全炸 -> 仍返回完整报告，不向外抛。

    自检的价值是"报出哪里坏了"；探测本身把自检搞崩，
    检查人员看到的就是"自检程序报错"而不是"哪一项不合格"。

    ⚠️ 这里**不能**直接把 `probe_*` 函数 patch 成抛异常 ——
    那样测的是"`run_selfcheck` 有没有 try/except"，而设计上
    `run_selfcheck` **刻意不做** try/except（见其 docstring：
    若它抛了，说明某节漏兜了，应该显式暴露）。
    正确做法是 patch 每节的**底层依赖**，验证"底层炸了，
    这一节自己把异常翻译成 unsupported"。
    """
    # ① 网络：psutil 导不进来
    monkeypatch.setattr(sc, "_import_psutil", lambda: (None, "boom"))
    # ② ASR：取数据的 loader 抛异常
    def boom_loader():
        raise RuntimeError("db down")
    # ③ 字体：font_check 模块整体不可用
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.endswith("docx.font_check") or name == "mmwf.domain.docx":
            raise ImportError("boom")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    r = sc.run_selfcheck(
        db_path="definitely-not-a-real-db.db",
        env={},
        segment_loader=boom_loader,
    )
    # 四项都必须有结论，且都不是 pass
    assert r.network_probe.verdict == sc.VERDICT_UNSUPPORTED
    assert r.asr_quality.verdict == sc.VERDICT_UNSUPPORTED
    assert r.font_availability.verdict == sc.VERDICT_UNSUPPORTED
    # 审计：库不存在 -> 按设计判 fail（见 test_audit_chain_missing_db_is_fail）
    assert r.audit_chain.verdict == sc.VERDICT_FAIL
    # 但整份报告仍在，且能序列化
    assert r.overall == sc.VERDICT_FAIL
    import json
    json.dumps(r.to_dict(), ensure_ascii=False)


def test_dns_attempts_is_none_not_zero():
    """★ DNS 计数一期**如实报不支持**（None），不能假装是 0。

    施工卡原文："一期可简化为报告不支持此项并说明原因，
    但要在文档中明确标注为已知限制，**不能假装有**。"
    一个恒为 0 的数字看起来像"已验证没有 DNS 查询"，实际只是"没数到"。
    """
    assert sc._count_dns_attempts() is None
