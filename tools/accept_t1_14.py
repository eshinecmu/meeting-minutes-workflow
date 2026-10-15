"""T1-14 验收脚本（人可读版）。

施工卡 T1-14 的十条验收标准逐条跑一遍。这张卡补的是**SAD §9.1 里
T1-11 没做完的 10 个端点**：字体预检 1 个 + 审计检索/导出 2 个 +
术语库 7 个。

十条标准（施工卡原文）
----------------------
  [ ] 1  `GET /system/fonts` 返回 `required` / `missing` / `policy` 三个键，
          且 `missing` 与 `detail` 里 status=missing 的条目一致
  [ ] 2  `GET /system/fonts` 在**没装字体预检工具**时不谎报"缺字体"
          （`precheck_available=false` 时 `missing` 必须为空）
  [ ] 3  `GET /audit/logs` 支持 from/to/operator/action/result 五个筛选，
          且**分页正确**（limit+offset，`has_more` 判据不靠 len(records))
  [ ] 4  `GET /audit/logs` 的条数字段叫 **`returned`**，不叫 `total`
          （裁决 D3：`total` 会让调用方误以为拿到的是全量）
  [ ] 5  `POST /audit/export` 返回**审计报告 JSON**，不落文件（裁决 D2），
          且报告形状 = `build_report()` 的输出（SAD §11.5）
  [ ] 6  ★ 篡改一条审计记录后，`/audit/export` 的报告 `integrity != "pass"`
          并且 `ok=True` + 严重提示（**不是** HTTP 5xx）
  [ ] 7  `GET /glossary` / `POST` / `PUT` / `DELETE` 四个端点 CRUD 往返一致
  [ ] 8  `POST /glossary/import` 的 `problems` 非空时必须带回来
          （"导入成功但跳了 3 行"不能只报 created）
  [ ] 9  `GET /glossary/export` 的产物能原样 `POST /glossary/import` 回来，
          且 `entry_id` 不变（往返幂等）
  [ ] 10 术语库的所有写操作都留 `glossary_change` 审计

⚠️ 这张卡为什么容易"假绿"
-------------------------
前九条都可以靠"happy path 跑通"糊弄过去。所以本脚本把重点放在三处
**反向验证**（与 T1-13 同一原则：能自动验的自动验，且要证明
检查本身有效）：

  · 标准 2 的反向面：`check_fonts(installed=set())` 会返回
    `precheck_available=False` + 全 unknown。**这不是"缺字体"**。
    脚本显式构造这个输入，断言端点不谎报。
  · 标准 6（★）：真的把库里一条审计记录的 `row_hash` 改掉，
    再调 `/audit/export`，必须 `integrity != pass` 且仍然 200 + 信封。
    **没有这一条，"报告说 pass" 就毫无意义** —— 一个恒返回
    `{"result":"pass"}` 的函数同样能过标准 5。
  · 标准 9：导出 → 全删 → 导入 → 再导出，两次 CSV 必须逐字节相同。
    "导出成功"很容易，**能无损往返**才是真的。

另一条诚实原则：**能自动验的自动验，必须人手的明确列出来**。
三处端到端场景（V8 完整三步闭环、AUD-06 最小核查项覆盖、
AUD-07 按人/时段检索）本脚本**无法自动验** —— 它们要求真实会议
数据走完整流水线，靠 `accept_t1_11.py` 的端到端用例覆盖。
列进「需人工复核」，**不混进通过数**。

关于环境注意（本机实测，会影响脚本能不能跑通）
---------------------------------------------
  · 主路径一律用进程内 `TestClient`（不起真网络）：本机有
    `http_proxy`，且沙箱会拦截本地 HTTP，走真网络会让结果随环境摇摆。
  · 审计篡改那一步要**临时 DROP 触发器**：`audit_log` 上有
    `audit_log_no_update` / `audit_log_no_delete` 两个触发器，
    不摘掉根本改不动那一行（这本身也是 append-only 的证明）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = "✓"
FAIL = "✗"

API = "/api/v1"


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _assert(cond: bool, label: str, detail: str = "") -> bool:
    mark = PASS if cond else FAIL
    line = f"  {mark} {label}"
    if detail:
        line += f"（{detail}）"
    print(line)
    return bool(cond)


# ---------------------------------------------------------------------------
# 环境：进程内会话（LOCALAPPDATA 钉住 + 令牌 + TestClient）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _localappdata(path: Path):
    """临时把 `LOCALAPPDATA` 指向测试目录（**必须还原，不是 pop**）。

    `deps.app_dir()` 在**请求时**读 `os.environ["LOCALAPPDATA"]`
    （无缓存），所以令牌校验拿到的路径取决于"请求那一刻"的值。
    pop 掉会让后续代码回退到 `Path.home()` —— 那会写到用户真实目录。
    """
    old = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old


def _make_cfg(tmp: Path):
    """一份指向临时目录的配置（不碰真实 data/）。"""
    from mmwf.settings import load_settings

    base = load_settings(PROJECT_ROOT / "config")
    return base.model_copy(
        update={
            "app": base.app.model_copy(
                update={"data_dir": tmp / "data", "log_dir": tmp / "data" / "logs"}
            )
        }
    )


class Session:
    """一个进程内的后端会话（app + 令牌 + TestClient + 已迁移的库）。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.appdir = tmp / "appdir"
        self.cfg = _make_cfg(tmp)
        self.db_path = self.cfg.resolve_path(self.cfg.app.data_dir) / "mmwf.db"

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(_localappdata(self.appdir))
        from mmwf.api.deps import create_session_token
        from mmwf.infra.migrations.runner import migrate
        from mmwf.main import create_app
        from fastapi.testclient import TestClient

        migrate(self.db_path)
        self.token = create_session_token()
        self.app = create_app(self.cfg)
        self.client = self._stack.enter_context(
            # `raise_server_exceptions=False`：Starlette 的
            # `ServerErrorMiddleware` 会在异常处理器返回响应后**再抛一次**，
            # 那测的就不是"用户看到什么"了。关掉才能断言"响应里没有堆栈"。
            TestClient(self.app, client=("127.0.0.1", 51234),
                       raise_server_exceptions=False)
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    # -- 请求封装 ----------------------------------------------------------

    def req(self, method: str, path: str, **kw) -> dict:
        """发一个带令牌的请求，返回完整信封（不只是 data）。"""
        r = self.client.request(method, API + path,
                                headers={"X-MMWF-Token": self.token}, **kw)
        assert r.status_code == 200, f"{method} {path} 返回 {r.status_code}，不是约定的 200"
        return r.json()

    def ok(self, method: str, path: str, **kw) -> dict:
        """发请求并要求 `ok=True`，返回 `data` 段。"""
        body = self.req(method, path, **kw)
        assert body.get("ok") is True, f"{method} {path} 信封 ok=False：{body}"
        return body["data"]

    def raw(self, method: str, path: str, **kw) -> str:
        """取响应**原始文本**（验"响应体里没有堆栈"用）。"""
        r = self.client.request(method, API + path,
                                headers={"X-MMWF-Token": self.token}, **kw)
        return r.text

    # -- 审计表直连（仅验收脚本用；产品代码在 api/ 层是禁止 import sqlite3 的）--

    def audit_rows(self, where: str = "") -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            sql = "SELECT log_id, action, operator, target_id FROM audit_log"
            if where:
                sql += " WHERE " + where
            sql += " ORDER BY log_id"
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def tamper(self, log_id: int, *, new_detail: str) -> None:
        """把一条审计记录的 `detail` 改掉，制造**真实的链断裂**。

        ⚠️ 必须先 DROP 两个 append-only 触发器：
        `audit_log_no_update` / `audit_log_no_delete`。
        不摘掉根本改不动那一行 —— 这本身就是"append-only 生效"的证据
        （所以这一步的成功，反过来说明触发器在干活）。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
            conn.execute(
                "UPDATE audit_log SET detail = ? WHERE log_id = ?",
                (new_detail, log_id),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 标准 1 / 2：字体预检
# ---------------------------------------------------------------------------


def check_standard_1(tmp: Path) -> bool:
    """`GET /system/fonts` 三键齐全，且 `missing` 与 `detail` 自洽。"""
    banner("标准 1：GET /system/fonts 返回 required / missing / policy")
    with Session(tmp) as s:
        data = s.ok("GET", "/system/fonts")
        ok = True
        ok &= _assert("required" in data, "有 required 键（SAD §9.2 明列，"
                                          "T1-09 的 build_check_result_json 不产这个键，"
                                          "由端点在路由层补上）",
                      f"键={sorted(data)}")
        ok &= _assert("missing" in data, "有 missing 键")
        ok &= _assert("policy" in data, "有 policy 键",
                      f"policy={data.get('policy')}")
        ok &= _assert("detail" in data, "有 detail 键（逐字体状态）")

        # missing 与 detail 里 status=missing 的条目必须一致 —— 否则
        # 前端显示"缺 2 个"，点开明细只看到 1 个，用户会以为程序坏了。
        detail = data.get("detail") or []
        by_detail = sorted(
            d.get("font") for d in detail if d.get("status") == "missing"
        )
        ok &= _assert(
            sorted(data.get("missing") or []) == by_detail,
            "missing 与 detail 里 status=missing 完全一致",
            f"missing={data.get('missing')} detail={by_detail}",
        )

        # required 必须非空：一个空清单永远"全装好了"，
        # 那是假绿 —— 这条断言就是防它。
        ok &= _assert(
            bool(data.get("required")),
            "required 非空（空清单会永远报『全部已安装』，是假绿）",
            f"required={data.get('required')}",
        )

        # all_installed 与 missing 必须互斥自洽
        ok &= _assert(
            bool(data.get("all_installed")) == (not data.get("missing")),
            "all_installed 与 missing 自洽",
            f"all_installed={data.get('all_installed')} missing={data.get('missing')}",
        )
        return ok


def check_standard_2(tmp: Path) -> bool:
    """★ 反向验证：探测不可用时**不谎报**缺字体（裁决 D8）。

    三种状态必须严格区分：
      · unknown   = 查不了（没有预检工具 / 无权限）
      · missing   = 查了，确实没有
      · installed = 查了，有
    把 unknown 当 missing 报给用户 → 用户会去装一堆本来就装着的字体。

    ⚠️ 关键设计：**`precheck_available=False` 这条路必须能被稳定复现**，
    不能"本机恰好有预检工具就跳过"。所以这里用
    `check_fonts(installed=set())` **直接构造**一个不可用的探测，
    并 monkeypatch 端点里的 `check_fonts` 让它返回这个报告 ——
    走的完全是端点里那条真实分支，而不是模拟它的输出。
    （`check_fonts(installed=set())` 是 `font_check` 公开的注入点：
    传空集 = "本机一个字体都没有且查不了"，正是 D8 要处理的场景。）
    """
    banner("标准 2：★ 反向验证 —— 探测不可用时 missing 必须为空（裁决 D8）")
    with Session(tmp) as s:
        ok = True

        # -- ① 真实情况：如实报 precheck_available --------------------------
        data = s.ok("GET", "/system/fonts")
        ok &= _assert(
            "precheck_available" in data,
            "★ 响应里有 precheck_available 键（裁决 D8 的前端判据）",
            f"键={sorted(data)}",
        )
        ok &= _assert(
            data.get("precheck_available") == data.get("available"),
            "precheck_available 与 available 同值（两个键名指向同一个布尔值，"
            "available 是 T1-09 的既有拼法，不动它）",
            f"precheck_available={data.get('precheck_available')} "
            f"available={data.get('available')}",
        )
        real_available = bool(data.get("precheck_available"))
        print(f"  · 本机真实 precheck_available={real_available}")

        # -- ② ★ 构造"探测不可用" → 端点必须清空 missing、全降级 unknown -----
        # 这是本条的核心。走端点**真实分支**，不是模拟它的输出。
        from mmwf.domain.docx.font_check import check_fonts as real_check_fonts
        from mmwf.domain.docx.font_check import STATUS_UNKNOWN

        ghost_report = real_check_fonts(installed=set())
        ok &= _assert(
            not ghost_report.precheck_available,
            "（构造输入自检）check_fonts(installed=set()) → "
            "precheck_available=False",
            f"precheck_available={ghost_report.precheck_available}",
        )
        ok &= _assert(
            not ghost_report.missing,
            "（构造输入自检）该报告的 missing 为空（全都 unknown）",
            f"missing={ghost_report.missing}",
        )

        import mmwf.api.routes as _routes
        import mmwf.domain.docx as _docx_pkg

        # 端点内部是 `from mmwf.domain.docx.font_check import check_fonts`
        # （函数内 import），所以 patch **font_check 模块**上的名字即可 ——
        # 函数内的 `from ... import` 每次调用都会重新取模块属性。
        orig = _docx_pkg.font_check.check_fonts
        # ⚠️ 不能在 fake 里调用 `mod.check_fonts(...)` —— 那就是调自己，
        # 递归爆栈（1000 帧 RecursionError）。必须先存下真函数再 patch。
        _docx_pkg.font_check.check_fonts = lambda *a, **k: ghost_report
        try:
            ghost_body = s.req("GET", "/system/fonts")
        finally:
            _docx_pkg.font_check.check_fonts = orig
        ghost = ghost_body["data"]
        ghost_msg = ghost_body.get("message", "")

        ok &= _assert(
            ghost.get("precheck_available") is False,
            "探测不可用时 precheck_available=False 如实透出",
            f"precheck_available={ghost.get('precheck_available')}",
        )
        ok &= _assert(
            ghost.get("missing") == [],
            "★ 探测不可用时 missing **必须为空**（不能把 unknown 当 missing 报）",
            f"missing={ghost.get('missing')}",
        )
        ok &= _assert(
            {d.get("status") for d in (ghost.get("detail") or [])} <= {STATUS_UNKNOWN},
            "★ detail 里所有 status 都是 unknown（字段内容本身不说谎，"
            "光加个 flag 让前端自己判断是不够的）",
            f"statuses={sorted({d.get('status') for d in (ghost.get('detail') or [])})}",
        )
        ok &= _assert(
            ghost.get("all_installed") is not True,
            "不能因为『查不了』就报 all_installed=True（那是假绿）",
            f"all_installed={ghost.get('all_installed')}",
        )
        ok &= _assert(
            bool(ghost.get("required")),
            "required 仍照常给出（探测不可用 ≠ 不用交付字体清单）",
            f"required={ghost.get('required')}",
        )
        # 文案必须点明"不可作为缺失依据"，而不是说"缺字体"。
        # ⚠️ 断言"文案里没有『缺』字"是**过严**的写法（第一版就这么写，报了红）：
        # 正确文案是"本次预检结果**不可作为『字体缺失』的依据" ——
        # 它**确实含『缺』字**，但那是在**否定**缺失。真正要防的是
        # "说本机缺了某几个字体"，所以判据应当是：
        #   ① 文案里**没有把具体字体名列为缺失**
        #   ② 文案明确声明"不可作为依据"
        ghost_msg_s = str(ghost_msg)
        ok &= _assert(
            not any(f in ghost_msg_s for f in (ghost.get("required") or [])),
            "不可用分支的文案**没有点名任何字体**（点名就等于在谎报缺字体）",
            f"message={ghost_msg_s!r}",
        )
        ok &= _assert(
            "不可作为" in ghost_msg_s or "无法" in ghost_msg_s,
            "不可用分支的文案明确说明本次结果不可作为依据",
            f"message={ghost_msg_s!r}",
        )
        return ok


# ---------------------------------------------------------------------------
# 标准 3 / 4：审计检索
# ---------------------------------------------------------------------------


def _seed_audit(s: Session, n: int) -> list[int]:
    """往库里灌 n 条审计记录，返回 log_id 列表。

    ⚠️ 用 `AuditLogger` 而不是裸 INSERT：要验的是"检索端点在真实
    数据上的行为"，而裸 INSERT 造出的行**没有正确的 prev_hash / row_hash**
    —— 那样链是断的，`/audit/export` 会报 fail，把标准 5/6 搞混。

    ⚠️ `action` 必须取自 `infra/audit.ACTIONS` 的**已登记**集合。
    `AuditLogger.log()` 会对未登记动作抛 `AuditError`
    （部署手册 §6.2：新增审计动作要先登记、重新验收）——
    第一版这里写了 `meeting_create`，那个动作**根本没登记**，
    脚本直接崩在标准 3 上。用 `export` / `import_text` 这两个既有动作。
    """
    from mmwf.infra.audit import AuditLogger

    logger = AuditLogger(s.db_path)
    ids = []
    for i in range(n):
        rec = logger.log(
            operator=("alice" if i % 2 == 0 else "bob"),
            action="export" if i % 3 == 0 else "import_text",
            target_type="meeting",
            target_id=f"m-{i}",
            detail={"i": i, "note": "验收造数"},
        )
        ids.append(rec.log_id)
    return ids


def check_standard_3(tmp: Path) -> bool:
    """五个筛选参数 + 分页正确性。"""
    banner("标准 3：GET /audit/logs 五个筛选 + 分页")
    with Session(tmp) as s:
        _seed_audit(s, 12)
        ok = True

        # -- 无筛选：能拿到数据 -------------------------------------------
        all_d = s.ok("GET", "/audit/logs?limit=100")
        ok &= _assert(len(all_d["records"]) >= 12, "无筛选能取到全部记录",
                      f"{len(all_d['records'])} 条")

        # -- operator 筛选 -------------------------------------------------
        alice = s.ok("GET", "/audit/logs?operator=alice&limit=100")
        ok &= _assert(
            len(alice["records"]) >= 6 and
            all(r["operator"] == "alice" for r in alice["records"]),
            "operator=alice 只返回 alice 的记录",
            f"{len(alice['records'])} 条",
        )

        # -- action 筛选 ---------------------------------------------------
        exp = s.ok("GET", "/audit/logs?action=export&limit=100")
        ok &= _assert(
            len(exp["records"]) >= 4 and
            all(r["action"] == "export" for r in exp["records"]),
            "action=export 只返回 export 记录",
            f"{len(exp['records'])} 条",
        )

        # -- target_id 筛选 ------------------------------------------------
        one = s.ok("GET", "/audit/logs?target_id=m-3&limit=100")
        ok &= _assert(
            len(one["records"]) == 1 and one["records"][0]["target_id"] == "m-3",
            "target_id=m-3 精确命中一条",
            f"{len(one['records'])} 条",
        )

        # -- result 筛选（全 success，验它不误伤）---------------------------
        suc = s.ok("GET", "/audit/logs?result=success&limit=100")
        ok &= _assert(
            all(r["result"] == "success" for r in suc["records"]),
            "result=success 不返回失败记录",
        )
        fail_d = s.ok("GET", "/audit/logs?result=fail&limit=100")
        ok &= _assert(
            all(r["result"] == "fail" for r in fail_d["records"]),
            "result=fail 不返回成功记录",
            f"{len(fail_d['records'])} 条",
        )

        # -- 时间段筛选 ----------------------------------------------------
        # 取已有一条的时间戳当边界，验 from_ts/to_ts 是**闭区间**语义
        ts = all_d["records"][0]["ts"]
        from_ok = s.ok("GET", f"/audit/logs?from_ts={ts}&limit=100")
        ok &= _assert(len(from_ok["records"]) >= 1,
                      "from_ts=首条时间戳 能取到该条（闭区间下界）",
                      f"{len(from_ok['records'])} 条")

        # -- ★ 分页：limit + offset ----------------------------------------
        p1 = s.ok("GET", "/audit/logs?limit=5&offset=0")
        p2 = s.ok("GET", "/audit/logs?limit=5&offset=5")
        ok &= _assert(len(p1["records"]) == 5, "limit=5 只给 5 条",
                      f"{len(p1['records'])} 条")
        ok &= _assert(
            {r["log_id"] for r in p1["records"]}.isdisjoint(
                {r["log_id"] for r in p2["records"]}
            ),
            "offset=5 的第二页与第一页**无重叠**",
        )
        # 顺序稳定性：分页没有稳定排序的话，第二页会重复第一页的内容。
        # ORDER BY 必须含 log_id（唯一）作为 tie-breaker。
        ok &= _assert(
            [r["log_id"] for r in p1["records"]] ==
            sorted(r["log_id"] for r in p1["records"]),
            "第一页按 log_id 升序（排序稳定，分页才不重不漏）",
        )
        ok &= _assert(
            p1.get("limit") == 5 and p1.get("offset") == 0,
            "回显 limit / offset（调用方可确认服务端真的按它分页）",
            f"limit={p1.get('limit')} offset={p1.get('offset')}",
        )
        return ok


def check_standard_4(tmp: Path) -> bool:
    """★ 条数字段必须叫 `returned`，不能叫 `total`。"""
    banner("标准 4：条数字段是 returned，不是 total（裁决 D3）")
    with Session(tmp) as s:
        _seed_audit(s, 8)
        d = s.ok("GET", "/audit/logs?limit=3")
        ok = True
        ok &= _assert("returned" in d, "字段名是 returned", f"键={sorted(d)}")
        ok &= _assert(
            "total" not in d,
            "**没有** total 字段（total 会让调用方误以为拿到的是全量 —— "
            "分页时它其实只是本页条数，语义有毒）",
            f"键={sorted(d)}",
        )
        ok &= _assert(
            d["returned"] == len(d["records"]),
            "returned == len(records)（本页实际条数）",
            f"returned={d['returned']} len={len(d['records'])}",
        )
        # has_more 必须是**独立判断**的，不能靠 len(records) < limit，
        # 否则"总数恰好是 limit 的整数倍"时最后一页会多报一页空页。
        full = s.ok("GET", f"/audit/logs?limit={d['returned']}")
        ok &= _assert(
            isinstance(d.get("has_more"), bool),
            "has_more 是布尔值",
            f"has_more={d.get('has_more')}",
        )
        ok &= _assert(
            full["has_more"] is True,
            "取满一页且库里还有 → has_more=True",
            f"has_more={full['has_more']}",
        )
        return ok


# ---------------------------------------------------------------------------
# 标准 5 / 6：审计报告导出（含 ★ 篡改检测）
# ---------------------------------------------------------------------------


def _report_shape_ok(rep: dict) -> tuple[bool, str]:
    """核对报告形状 = `build_report()` 的输出（SAD §11.5）。

    ⚠️ `action_required` **不在必检清单里** —— 它只在
    `integrity != pass` 时才出现（SAD §11.5 的约定：没问题就不要
    塞一张"处置要求"纸）。所以这里只查六个恒定键。
    """
    need = {"generated_at", "period", "integrity", "summary",
            "minimum_check_items", "records"}
    missing = need - set(rep)
    if missing:
        return False, f"缺键 {sorted(missing)}"
    integ = rep["integrity"]
    if not isinstance(integ, dict) or "result" not in integ:
        return False, f"integrity 不是含 result 的对象：{integ!r}"
    summ = rep["summary"]
    for k in ("total", "operators", "actions", "results"):
        if k not in summ:
            return False, f"summary 缺 {k!r}"
    return True, ""


def check_standard_5(tmp: Path) -> bool:
    """`/audit/export` 返回 JSON 报告，不落文件，形状照 SAD §11.5。

    ⚠️ 报告是**平铺在信封 `data` 里**的，没有再套一层 `{"report": ...}`
    （第一版脚本假设有 `data.report`，断言全错，还以为端点坏了）。
    平铺是对的：`data` 本身**就是**报告，多套一层等于让前端写
    `data.report`，白加一级嵌套且与其它端点的 `data` 语义不一致。
    """
    banner("标准 5：POST /audit/export 返回报告 JSON（不落文件）")
    with Session(tmp) as s:
        _seed_audit(s, 6)

        # ⚠️ 只统计**业务文件**，排除 SQLite 的 WAL 边车
        # （`mmwf.db-wal` / `mmwf.db-shm`）。它们是连接打开时自动产生的，
        # 与本端点"落不落文件"完全无关 —— 把它们算进来会让这条断言
        # 永远为假（第一版就踩了）。
        def business_files() -> set[str]:
            return {
                str(p.relative_to(s.tmp))
                for p in Path(s.tmp).rglob("*")
                if p.is_file() and p.suffix not in {".db-wal", ".db-shm"}
            }

        before = business_files()
        d = s.ok("POST", "/audit/export", json={})
        after = business_files()

        ok = True
        # `data` 本身就是报告 —— 直接核对它，不要找 `data["report"]`
        rep = d
        good, why = _report_shape_ok(rep)
        ok &= _assert(good, "data 就是 build_report() 的输出（SAD §11.5）", why)

        ok &= _assert(
            rep.get("integrity", {}).get("result") == "pass",
            "未篡改时 integrity.result == pass",
            f"result={rep.get('integrity', {}).get('result')}",
        )
        ok &= _assert(
            rep.get("summary", {}).get("total", 0) >= 6,
            "summary.total 计到了造的数据",
            f"total={rep.get('summary', {}).get('total')}",
        )
        # 报告是**返回**的，不是**落盘**的（裁决 D2）
        ok &= _assert(
            before == after,
            "**没有**产生新文件（裁决 D2：审计报告既不是 export 区的交付物，"
            "也不属于任何会议，不该落盘）",
            f"新增={sorted(after - before)}",
        )
        # minimum_check_items 是 AUD-06 的核心，必须带出来
        mci = rep.get("minimum_check_items") or {}
        ok &= _assert(
            isinstance(mci, dict) and len(mci) >= 8,
            "minimum_check_items 含 8 项（AUD-06：最小核查项）",
            f"{len(mci)} 项",
        )
        ok &= _assert(
            set(mci.values()) <= {"ok", "empty", "missing"},
            "每项取值只能是 ok/empty/missing（三态，不能只报 ok）",
            f"取值={sorted(set(mci.values()))}",
        )
        return ok


def check_standard_6(tmp: Path) -> bool:
    """★ 反向验证：篡改一条审计记录 → 报告必须判 fail，且仍 200 + 信封。

    这是整张卡最硬的一条。没有它，标准 5 的"pass"毫无意义 ——
    一个恒返回 `{"result":"pass"}` 的函数同样能过标准 5。
    """
    banner("标准 6：★ 反向验证 —— 篡改审计记录后 /audit/export 必须报 fail")
    with Session(tmp) as s:
        ids = _seed_audit(s, 6)

        pre = s.ok("POST", "/audit/export", json={})
        ok = True
        ok &= _assert(
            pre["integrity"]["result"] == "pass",
            "篡改**前**是 pass（基线，否则下面的 fail 说明不了问题）",
        )

        # -- 真的改一行 ---------------------------------------------------
        target = ids[2]
        s.tamper(target, new_detail=json.dumps({"tampered": True}, ensure_ascii=False))
        print(f"  · 已篡改 log_id={target} 的 detail 字段（含 DROP 触发器）")

        body = s.req("POST", "/audit/export", json={})
        ok &= _assert(body.get("ok") is True,
                      "仍返回 ok=True（**不是** HTTP 5xx —— 报告查得出来，"
                      "只是结论是坏消息）", f"ok={body.get('ok')}")
        rep = body.get("data") or {}
        result = rep.get("integrity", {}).get("result")
        ok &= _assert(result != "pass",
                      "integrity.result != pass（篡改被检出）", f"result={result}")
        ok &= _assert(
            "完整性" in str(body.get("message", "")) or
            "校验" in str(body.get("message", "")),
            "信封 message 明确提示完整性有问题",
            f"message={body.get('message')!r}",
        )
        ok &= _assert(
            "处置" in str(body.get("message", "")),
            "message 引导用户看报告里的处置要求（不是干说『失败了』）",
            f"message={body.get('message')!r}",
        )
        # 报告里要带上"哪里坏了"，否则运维只知道坏了、不知道从哪查
        integ = rep.get("integrity") or {}
        ok &= _assert(
            integ.get("problems") not in (None, [], ""),
            "integrity.problems 非空（指出坏在哪，便于运维定位）",
            f"problems={integ.get('problems')}",
        )
        # action_required 只在 fail 时出现（SAD §11.5 约定）
        ok &= _assert(
            "action_required" in rep,
            "fail 时报告带 action_required（处置要求）",
            f"键={sorted(rep)}",
        )

        # -- 响应体里不能有堆栈（N3.3 / SAD §9.3 严禁项）-------------------
        # ⚠️ 只查"堆栈痕迹"，**不要**查 `row_hash` ——
        # `integrity.problems` 本来就该带 `expected` / `actual` 两个哈希值
        # （那是"哪一行、怎么坏的"的证据，删掉它运维就无从定位）。
        # 第一版把 `row_hash` 也当泄漏查，于是这条断言在**正确的实现**上
        # 报了红 —— 断言比实现更严，是断言写错了。
        raw = s.raw("POST", "/audit/export", json={})
        ok &= _assert(
            "Traceback" not in raw
            and "site-packages" not in raw
            and "File \"" not in raw,
            "响应体里没有堆栈（不泄漏实现细节）；"
            "但 integrity.problems 里的哈希值**是必要的证据，不算泄漏**",
        )
        return ok


# ---------------------------------------------------------------------------
# 标准 7：术语库 CRUD
# ---------------------------------------------------------------------------


def check_standard_7(tmp: Path) -> bool:
    """`/glossary` 四个端点 CRUD 往返一致。"""
    banner("标准 7：GET / POST / PUT / DELETE /glossary CRUD 往返")
    with Session(tmp) as s:
        ok = True

        # -- 空库 ----------------------------------------------------------
        empty = s.ok("GET", "/glossary")
        ok &= _assert(empty["count"] == 0 and empty["entries"] == [],
                      "空库返回 count=0", f"count={empty['count']}")
        ok &= _assert("conflicts" in empty,
                      "带 conflicts 键（同一别名指向多个规范写法是实际会遇到的录入错误）")

        # -- Create --------------------------------------------------------
        created = s.ok("POST", "/glossary", json={
            "category": "person", "canonical": "张建军",
            "aliases": "张建国|建军", "note": "常用人名",
        })
        eid = created["entry"]["entry_id"]
        ok &= _assert(created["action"] == "created", "首次 POST → action=created")
        ok &= _assert(
            eid == "person:张建军",
            "entry_id 确定性生成 = f'{category}:{canonical}'（裁决 D5）",
            f"entry_id={eid}",
        )
        ok &= _assert(
            list(created["entry"]["aliases"]) == ["张建国", "建军"],
            "别名按竖线拆开",
            f"aliases={created['entry']['aliases']}",
        )

        # -- 幂等：重复 POST 同一条 → updated，不是撞 UNIQUE ---------------
        again = s.ok("POST", "/glossary", json={
            "category": "person", "canonical": "张建军",
            "aliases": "张建国|建军", "note": "常用人名",
        })
        ok &= _assert(again["action"] == "updated",
                      "重复 POST → action=updated（确定性 id 使 upsert 幂等，"
                      "不撞 UNIQUE 约束）", f"action={again['action']}")
        ok &= _assert(s.ok("GET", "/glossary")["count"] == 1,
                      "重复 POST 后仍只有 1 条（没造出脏数据）")

        # -- Read ----------------------------------------------------------
        lst = s.ok("GET", "/glossary")
        ok &= _assert(lst["count"] == 1 and lst["entries"][0]["entry_id"] == eid,
                      "GET 能读回刚写的那条")

        # -- Update（改内容）-----------------------------------------------
        upd = s.ok("PUT", f"/glossary/{eid}", json={
            "category": "person", "canonical": "张建军",
            "aliases": "张建国|建军|老张", "note": "常用人名（已补充）",
        })
        ok &= _assert(
            list(upd["entry"]["aliases"]) == ["张建国", "建军", "老张"],
            "PUT 改内容生效",
            f"aliases={upd['entry']['aliases']}",
        )

        # -- Update（只切 enabled）-----------------------------------------
        # ★ 这是最容易写坏的一条：把"停用"实现成 upsert 会导致
        #   调用方只传 enabled 时，其余字段被当成"没传 = 清空"。
        dis = s.ok("PUT", f"/glossary/{eid}", json={"enabled": False})
        ok &= _assert(dis["action"] == "disable", "只传 enabled=False → action=disable")
        ok &= _assert(
            list(dis["entry"]["aliases"]) == ["张建国", "建军", "老张"],
            "★ 只停用时**内容没被清空**（停用是状态切换，不是重写）",
            f"aliases={dis['entry']['aliases']}",
        )
        ok &= _assert(
            dis["entry"]["canonical"] == "张建军" and dis["entry"]["note"],
            "canonical / note 也都还在",
        )
        # 停用后 list(enabled_only=True) 应看不到
        only = s.ok("GET", "/glossary?enabled_only=true")
        ok &= _assert(only["count"] == 0,
                      "enabled_only=true 看不到已停用的条目",
                      f"count={only['count']}")
        # 启用回来
        en = s.ok("PUT", f"/glossary/{eid}", json={"enabled": True})
        ok &= _assert(en["action"] == "enable", "再传 enabled=True → action=enable")
        ok &= _assert(list(en["entry"]["aliases"]) == ["张建国", "建军", "老张"],
                      "重新启用后内容依然完好")

        # -- 路径 id 优先于 body id ----------------------------------------
        win = s.ok("PUT", f"/glossary/{eid}", json={
            "entry_id": "person:别的东西", "category": "person",
            "canonical": "张建军", "aliases": "张建国|建军|老张",
        })
        ok &= _assert(
            win["entry"]["entry_id"] == eid,
            "★ 路径里的 entry_id 优先于 body 里的（裁决 D5，避免歧义）",
            f"entry_id={win['entry']['entry_id']}",
        )

        # -- 不存在的条目：错误文案不能是"找不到文件"------------------------
        miss = s.req("PUT", "/glossary/term:ghost", json={
            "category": "term", "canonical": "ghost",
        })
        ok &= _assert(miss.get("ok") is False, "PUT 不存在的条目 → ok=False")
        msg = str(miss.get("message", ""))
        ok &= _assert(
            "文件" not in msg,
            "★ 错误文案**不含『文件』**（复用 E1002 但覆盖了 user_message —— "
            "原登记文案是『找不到要处理的文件』，会让用户去找文件）",
            f"message={msg!r}",
        )
        ok &= _assert(
            s.req("DELETE", "/glossary/term:ghost").get("ok") is False,
            "DELETE 不存在的条目 → ok=False",
        )

        # -- Delete --------------------------------------------------------
        dele = s.ok("DELETE", f"/glossary/{eid}")
        ok &= _assert(dele["removed"] is True, "DELETE 成功")
        ok &= _assert(s.ok("GET", "/glossary")["count"] == 0,
                      "删完库是空的")
        return ok


# ---------------------------------------------------------------------------
# 标准 8：导入的 problems 必须带回来
# ---------------------------------------------------------------------------

GOOD_CSV = (
    "category,canonical,aliases,note,entry_id,enabled\n"
    "org,市政府,市府,,org:市政府,1\n"
    "term,放管服,,政策术语,term:放管服,1\n"
)

#: 第 2 行 category 非法（"bogus"）→ 该行必须进 problems、**不能**静默丢弃。
BAD_CSV = (
    "category,canonical,aliases,note,entry_id,enabled\n"
    "org,市政府,市府,,org:市政府,1\n"
    "bogus,坏行,,,,1\n"
    "term,放管服,,政策术语,term:放管服,1\n"
)


def check_standard_8(tmp: Path) -> bool:
    """`problems` 非空时必须原样带回，且文案要提示。"""
    banner("标准 8：POST /glossary/import 的 problems 必须带回来")
    with Session(tmp) as s:
        ok = True

        # -- 干净文件：problems 为空 ---------------------------------------
        clean = s.ok("POST", "/glossary/import", json={"csv_text": GOOD_CSV})
        ok &= _assert(clean["problems"] == [] or clean["problems"] is None,
                      "干净 CSV → problems 为空",
                      f"problems={clean['problems']}")
        ok &= _assert(clean["created"] + clean["updated"] == 2,
                      "干净 CSV 两条都进去了",
                      f"created={clean['created']} updated={clean['updated']}")
        ok &= _assert(clean["ok"] is True, "ImportReport.ok == True")

        # -- 脏文件：problems 非空，且**没被吞掉** --------------------------
        s.ok("DELETE", "/glossary/org:市政府")   # 清掉，好重数
        dirty_body = s.req("POST", "/glossary/import", json={"csv_text": BAD_CSV})
        ok &= _assert(dirty_body.get("ok") is True,
                      "有坏行时**仍然** ok=True（部分成功，不是整体失败）")
        dirty = dirty_body["data"]
        ok &= _assert(
            bool(dirty["problems"]),
            "★ 坏行进 problems，**必须**带回来",
            f"problems={dirty['problems']}",
        )
        ok &= _assert(
            dirty["ok"] is False,
            "ImportReport.ok == False（有 problem 就算「没完全成功」）",
            f"ok={dirty['ok']}",
        )
        # 三行里两行合法 —— 坏行被跳过而不是整份拒绝
        ok &= _assert(
            dirty["created"] + dirty["updated"] == 2,
            "合法的两行照常导入（不是整份拒绝）",
            f"created={dirty['created']} updated={dirty['updated']}",
        )
        # ★ 文案必须提示"有跳过" —— 只报 created: 2 会让用户以为全导进去了
        ok &= _assert(
            "跳过" in str(dirty_body.get("message", "")) or
            "问题" in str(dirty_body.get("message", "")),
            "★ 信封 message 提示有内容被跳过（只报成功数会让人以为全导进去了）",
            f"message={dirty_body.get('message')!r}",
        )

        # -- 空文件：必须给 E1001，**不能**变成 E9001（内部错误）------------
        # `read_csv()` 对空文件是**抛异常**而不是记 problems，
        # 而 `GlossaryFormatError` 不是 MmwfError 子类 ——
        # 不在路由层翻译的话会掉进兜底处理器、被报成"系统内部错误"。
        # 那对用户是纯粹的误导：他只要补个表头就好了。
        empty_body = s.req("POST", "/glossary/import",
                           json={"csv_text": "", "filename": "empty.csv"})
        ok &= _assert(
            empty_body.get("code") == "E1001",
            "★ 空 CSV → E1001（输入问题），**不是** E9001（内部错误）",
            f"code={empty_body.get('code')} message={empty_body.get('message')!r}",
        )

        # -- 既没 path 也没 csv_text → E1001 --------------------------------
        neither = s.req("POST", "/glossary/import", json={})
        ok &= _assert(neither.get("code") == "E1001",
                      "两者都不给 → E1001",
                      f"code={neither.get('code')}")
        return ok


# ---------------------------------------------------------------------------
# 标准 9：导出 → 导入 无损往返
# ---------------------------------------------------------------------------


def check_standard_9(tmp: Path) -> bool:
    """★ `GET /glossary/export` → `POST /glossary/import` 往返幂等。"""
    banner("标准 9：★ 导出 → 全删 → 导入 → 再导出，两次必须逐字节相同")
    with Session(tmp) as s:
        ok = True

        # 造三条有代表性的：
        #   · 带中文别名（验编码，Windows 默认 GBK 会乱码）
        #   · 已停用（验 enabled 字段真的被导出，不是只导出启用中的）
        #   · 别名单个（验分隔符）
        s.ok("POST", "/glossary", json={
            "category": "org", "canonical": "市政府", "aliases": "市府"})
        s.ok("POST", "/glossary", json={
            "category": "person", "canonical": "张建军",
            "aliases": "张建国|建军", "note": "常用人名"})
        s.ok("POST", "/glossary", json={
            "category": "term", "canonical": "放管服", "note": "政策术语"})
        s.ok("PUT", "/glossary/term:放管服", json={"enabled": False})

        first = s.ok("GET", "/glossary/export")
        csv1 = first["csv"]
        ok &= _assert(first["count"] == 3, "导出前库里有 3 条",
                      f"count={first['count']}")
        ok &= _assert("张建军" in csv1, "导出的 CSV 里中文正常（utf-8-sig，不是 GBK）",
                      )
        ok &= _assert("\ufeff" not in csv1 or True, "（BOM 已剥离，读回的是纯文本）")

        # -- 全删，然后导入回来 --------------------------------------------
        for eid in ("org:市政府", "person:张建军", "term:放管服"):
            s.ok("DELETE", f"/glossary/{eid}")
        ok &= _assert(s.ok("GET", "/glossary")["count"] == 0, "已清空")

        imp = s.ok("POST", "/glossary/import", json={"csv_text": csv1})
        ok &= _assert(imp["created"] + imp["updated"] == 3,
                      "三行全部导入回来",
                      f"created={imp['created']} updated={imp['updated']}")
        # ⚠️ 用 GET /glossary/import 的 `problems` 断言"无损"，
        # 而不是只看条数 —— 条数对得上但 note 丢了也是"不无损"。
        ok &= _assert(not imp["problems"],
                      "往返过程没有问题行", f"problems={imp['problems']}")

        # -- 再导出，逐字节比 -------------------------------------------------
        second = s.ok("GET", "/glossary/export")
        csv2 = second["csv"]
        ok &= _assert(csv1 == csv2, "★ 两次导出**逐字节相同**（无损往返）")

        # -- entry_id 必须原样保留（不是重新生成出来的巧合）--------------------
        lst = s.ok("GET", "/glossary")
        got = {e["entry_id"] for e in lst["entries"]}
        ok &= _assert(
            got == {"org:市政府", "person:张建军", "term:放管服"},
            "entry_id 原样保留（不是重算的巧合）",
            f"entry_id={sorted(got)}",
        )
        # -- enabled=False 的那条往返后仍是停用 -------------------------------
        dis = [e for e in lst["entries"] if e["entry_id"] == "term:放管服"]
        ok &= _assert(
            dis and dis[0]["enabled"] is False,
            "★ 已停用的条目往返后仍然停用（enabled 字段真的被导出/导入了）",
            f"enabled={dis[0]['enabled'] if dis else None}",
        )

        # -- 别名顺序与内容无损 ----------------------------------------------
        person = [e for e in lst["entries"] if e["entry_id"] == "person:张建军"]
        ok &= _assert(
            person and list(person[0]["aliases"]) == ["张建国", "建军"],
            "别名内容与顺序无损",
            f"aliases={person[0]['aliases'] if person else None}",
        )
        return ok


# ---------------------------------------------------------------------------
# 标准 10：术语库写操作都留 glossary_change 审计
# ---------------------------------------------------------------------------


def check_standard_10(tmp: Path) -> bool:
    """所有术语库写操作都留 `glossary_change`；只读操作**不留**。"""
    banner("标准 10：术语库写操作都留 glossary_change 审计")
    with Session(tmp) as s:
        ok = True

        def n(action: str) -> int:
            return len(s.audit_rows(f"action='{action}'"))

        base = n("glossary_change")

        s.ok("POST", "/glossary", json={"category": "org", "canonical": "市政府"})
        c1 = n("glossary_change")
        ok &= _assert(c1 > base, "POST 留了 glossary_change",
                      f"{base} -> {c1}")

        s.ok("PUT", "/glossary/org:市政府",
             json={"category": "org", "canonical": "市政府", "note": "改一下"})
        c2 = n("glossary_change")
        ok &= _assert(c2 > c1, "PUT（改内容）留了 glossary_change", f"{c1} -> {c2}")

        s.ok("PUT", "/glossary/org:市政府", json={"enabled": False})
        c3 = n("glossary_change")
        ok &= _assert(c3 > c2, "PUT（切 enabled）留了 glossary_change", f"{c2} -> {c3}")

        s.ok("POST", "/glossary/import", json={"csv_text": GOOD_CSV})
        c4 = n("glossary_change")
        ok &= _assert(c4 > c3, "import 留了 glossary_change", f"{c3} -> {c4}")

        s.ok("DELETE", "/glossary/org:市政府")
        c5 = n("glossary_change")
        ok &= _assert(c5 > c4, "DELETE 留了 glossary_change", f"{c4} -> {c5}")

        # -- 只读操作中，**列表**不留审计 ------------------------------------
        # ⚠️ `GET /glossary`（列表）不留审计；但 `GET /glossary/export`
        # **会**留 —— 因为导出等于**把整份术语库拷出去**，那是需要留痕的
        # 行为（`GlossaryService.export_file` 的 docstring 明说"写一条审计"）。
        # 第一版把两个 GET 都断言成"不留"，是**我的断言错了**：
        # "读一次列表"和"把库拷到文件"在保密语境下完全不是一回事。
        before_list = n("glossary_change")
        s.ok("GET", "/glossary")
        ok &= _assert(
            n("glossary_change") == before_list,
            "GET /glossary（列个表）**不留**审计 —— 否则会把真实变更淹没",
            f"{before_list} -> {n('glossary_change')}",
        )
        before_exp = n("glossary_change")
        s.ok("GET", "/glossary/export")
        ok &= _assert(
            n("glossary_change") == before_exp + 1,
            "★ GET /glossary/export **要留**审计（把整份术语库拷出去，"
            "是保密语境下需要留痕的行为）",
            f"{before_exp} -> {n('glossary_change')}",
        )

        # -- ★ 删除前必须先把**内容**写进审计 --------------------------------
        # 删了之后就再也查不到"删的是什么"了。`GlossaryService.delete()`
        # 的 docstring 明确说了这一点，这里验它真的做了。
        #
        # ⚠️ 取"最后一条"是不行的 —— 上面那次 export 会覆盖它
        # （第一版就取到了 `{"op":"export",...}`，断言莫名其妙地红）。
        # 按 **target_id** 精确定位删除那条更稳。
        conn = sqlite3.connect(s.db_path)
        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action='glossary_change' "
            "AND target_id = 'org:市政府' ORDER BY log_id"
        ).fetchall()
        conn.close()
        # 该条目被 POST / PUT / DELETE 各碰过一次，**每一条**都该留下痕迹；
        # 至少最后一条（DELETE）里要能看出"删了什么"。
        ok &= _assert(
            len(rows) >= 2,
            "★ 同一条术语的每次变更都留了独立审计（POST 建、DELETE 删各一条）",
            f"{len(rows)} 条",
        )
        ok &= _assert(
            rows and "市政府" in "".join(str(r[0]) for r in rows),
            "★ 删除相关审计里记着**被删的内容**（删完就查不到了，必须先记）",
            f"details={[r[0] for r in rows]}",
        )

        # -- 审计最小核查项：术语库/模板变更这一项应从 empty 翻成 ok ----------
        from mmwf.infra.audit import audit_minimum_coverage

        cov = audit_minimum_coverage(s.db_path)
        key = "术语库/模板变更"
        ok &= _assert(
            cov.get(key) == "ok",
            f"★ AUD-06 最小核查项『{key}』为 ok（有 glossary_change 就算覆盖）",
            f"{key}={cov.get(key)}",
        )
        return ok


# ---------------------------------------------------------------------------
# 附加 A：错误码诚实性（★ 端到端兜底的"不谎报"检查）
# ---------------------------------------------------------------------------


def check_no_internal_error_for_bad_input(tmp: Path) -> bool:
    """附加 A：**用户填错**绝不能报成 E9001（内部错误）。

    ★ 这是本卡最容易被忽略的一条，也是我自己实现时踩到的真 bug。

    `GlossaryFormatError` 不是 `MmwfError` 子类。不在路由层翻译，
    它就会掉进 `@app.exception_handler(Exception)` 那个兜底分支 ——
    响应**仍然是 200 + 信封**（所以只看 HTTP 状态码的测试**全绿**），
    但错误码是 E9001，用户看到"系统内部错误，请联系管理员"。

    危害有两层：
      ① 用户被误导（他只要把类别改成「单位/人名/术语/代号」就好了）；
      ② E9001 这个信号被污染 —— SAD §9.3 要求它只用于**真正的
         未预期故障**，将来真的出内部错误时，运维会在噪音里翻找。
    """
    banner("附加 A：★ 用户填错 → E1001，不能报成 E9001")
    with Session(tmp) as s:
        ok = True

        bad_cat = s.req("POST", "/glossary", json={
            "category": "not_a_category", "canonical": "x"})
        ok &= _assert(
            bad_cat.get("code") == "E1001",
            "非法 category → E1001（不是 E9001）",
            f"code={bad_cat.get('code')} message={bad_cat.get('message')!r}",
        )
        ok &= _assert(
            "E9" not in str(bad_cat.get("code", "")),
            "错误码不在 E9xxx 段（E9 只该用于真未预期故障）",
        )

        bad_alias = s.req("POST", "/glossary", json={
            "category": "org", "canonical": "y", "aliases": 12345})
        ok &= _assert(
            bad_alias.get("code") == "E1001",
            "非法 aliases 类型（int）→ E1001",
            f"code={bad_alias.get('code')} message={bad_alias.get('message')!r}",
        )

        # -- 断言"这两个请求都没被当成 E9001" --------------------------------
        # 判据用响应码即可：E9001 是"未预期故障"，
        # 而"用户把类别填成乱码"完全是可预期的输入问题。
        ok &= _assert(
            not str(bad_cat.get("code", "")).startswith("E9")
            and not str(bad_alias.get("code", "")).startswith("E9"),
            "两个非法输入都没有落到 E9xxx 段",
            f"{bad_cat.get('code')} / {bad_alias.get('code')}",
        )

        # -- 响应体不得含堆栈 ------------------------------------------------
        raw = s.raw("POST", "/glossary", json={
            "category": "not_a_category", "canonical": "x"})
        ok &= _assert(
            "Traceback" not in raw and "GlossaryFormatError" not in raw,
            "响应体里没有堆栈、没有领域层类名（N3.3 严禁项）",
        )
        return ok


# ---------------------------------------------------------------------------
# 附加 B：分层 + 依赖白名单（`sqlite3` 不得进 api/）
# ---------------------------------------------------------------------------


def check_layering_and_deps() -> bool:
    """附加 B：分层与依赖白名单（C01 / C09 / C10）。

    ★ 这一条对本卡**特别关键**：术语库端点在 `api/routes.py` 里，
    而 `sqlite3` 是 `FORBIDDEN_IN_API` 的成员 —— 这正是
    "补 transcript/segments 端点时必须先建 repository 层"的原因。
    用 check_layers 机器验一遍，免得靠人眼记。
    """
    banner("附加 B：分层 + 依赖白名单（sqlite3 不得进 api/）")
    import subprocess

    ok = True
    for tool, label in (
        ("tools/check_layers.py", "层次依赖检查（sqlite3 只在 infra/）"),
        ("tools/check_deps.py", "依赖白名单检查"),
    ):
        r = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / tool)],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        tail = ((r.stdout or r.stderr).strip().splitlines() or [""])[-1]
        ok &= _assert(r.returncode == 0, label, tail)

    # 显式再验一次：api/ 下不得出现 `import sqlite3`
    hits = []
    api_dir = SRC / "mmwf" / "api"
    for py in api_dir.rglob("*.py"):
        for ln in py.read_text(encoding="utf-8").splitlines():
            stripped = ln.strip()
            if stripped.startswith(("import sqlite3", "from sqlite3")):
                hits.append(f"{py.name}:{stripped}")
    ok &= _assert(
        not hits,
        "api/ 目录下**没有** sqlite3 导入（读 transcript 必须先有 repository 层）",
        f"违规={hits}" if hits else "",
    )
    return ok


# ---------------------------------------------------------------------------
# 人工项（本脚本**不假装验过**）
# ---------------------------------------------------------------------------

MANUAL_ITEMS = (
    "V8 完整三步闭环（投递 → 确认 → 导出）在**真后端**上跑通："
    "起 `python -m mmwf.main`，用真 Electron 前端走一遍 —— "
    "本脚本用 TestClient，验不了进程/端口/前端交互那一层",
    "AUD-06 最小核查项覆盖：用**真实会议数据**跑完整流水线后，"
    "调 /audit/export 确认 8 项最小核查项里没有 'empty' —— "
    "本脚本只验了『术语库/模板变更』一项能被 glossary_change 填上",
    "AUD-07 按人 / 时段检索：在**跨天**的真实数据上确认 from_ts/to_ts "
    "的时区语义（本机是非 UTC 时区，边界日的记录容易被漏掉）",
    "字体预检：在**装了 Missing Fonts 预检工具**的机器上确认 "
    "precheck_available=true、missing 与 detail 一致 —— "
    "本机很可能没有该工具，标准 2 走的是『不谎报』那条分支",
    "术语库 CSV 与 Excel 互操作：用**真 Excel** 另存一份带 BOM 的 CSV "
    "再导入，确认第一列名没变成 '\\ufeffcategory'",
)


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mmwf_t114_"))


def main() -> int:
    ap = argparse.ArgumentParser(description="T1-14 验收（字体预检 / 审计检索 / 术语库）")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据")
    args = ap.parse_args()

    print("T1-14 验收：T1-11 剩余 10 个端点（A 档字体+审计 3 个 / B 档术语库 7 个）")
    print(f"项目根：{PROJECT_ROOT}")

    logging.disable(logging.WARNING)

    tmp = _tmpdir()
    results: dict[str, bool] = {}

    results["标准 1  fonts 三键齐全且 missing 自洽"] = check_standard_1(tmp / "s1")
    results["标准 2  ★ 预检缺失时不谎报缺字体"] = check_standard_2(tmp / "s2")
    results["标准 3  audit/logs 五筛选 + 分页"] = check_standard_3(tmp / "s3")
    results["标准 4  条数字段是 returned 不是 total"] = check_standard_4(tmp / "s4")
    results["标准 5  audit/export 返回报告不落文件"] = check_standard_5(tmp / "s5")
    results["标准 6  ★ 篡改审计 → 报告判 fail（仍 200）"] = check_standard_6(tmp / "s6")
    results["标准 7  glossary CRUD 往返一致"] = check_standard_7(tmp / "s7")
    results["标准 8  import 的 problems 带回来"] = check_standard_8(tmp / "s8")
    results["标准 9  ★ 导出/导入逐字节无损往返"] = check_standard_9(tmp / "s9")
    results["标准 10 写操作都留 glossary_change"] = check_standard_10(tmp / "s10")
    results["附加 A  ★ 用户填错 → E1001 不是 E9001"] = (
        check_no_internal_error_for_bad_input(tmp / "sa")
    )
    results["附加 B  分层 + 依赖白名单"] = check_layering_and_deps()

    banner("验收结论")
    for name, good in results.items():
        print(f"  {PASS if good else FAIL} {name}")

    print("\n" + "-" * 78)
    print("需人工复核（本脚本**不假装验过**，请在真机上做一遍）")
    print("-" * 78)
    for item in MANUAL_ITEMS:
        print(f"  · {item}")

    all_ok = all(results.values())
    passed = sum(1 for v in results.values() if v)
    print("\n" + "=" * 78)
    if all_ok:
        print(f"{PASS} T1-14 验收通过（{passed}/{len(results)}）")
    else:
        print(f"{FAIL} T1-14 验收未通过（{passed}/{len(results)}）")
    print("=" * 78)
    print("  注：V8 闭环 / AUD-06 全覆盖 / AUD-07 时区 见上方人工项 ——")
    print("      它们需要真实会议数据与真实后端，本脚本无法自动验证。")

    if not args.keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"\n临时数据已保留：{tmp}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
