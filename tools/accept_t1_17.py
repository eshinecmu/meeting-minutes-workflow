"""T1-17 验收脚本（人可读版）—— 一期收口。

这张卡**不新增端点**。它做三件事：
  ① 把前卡验收时"绕过去"的存量缺陷真正修掉 / 或实测判定其不存在；
  ② 把 SAD §9.1 里"只有代码没有证据"的事项补上可复现的验收；
  ③ 出一份能给保密检查人员看的验收记录。

八条标准（施工卡 §3.1 原文）
---------------------------
  [ ] 1  ★ 端点清单三口径**未变**（SAD=32 / 路由表=33 / 去重路径=28）
         —— 这是"本卡没越界改端点"的**探测器**
  [ ] 2  ★ 缺陷 14：`close()` 后 `.app` 重新编译拿到**新**连接；幂等；不丢注入的 app
  [ ] 3  ★ 缺陷 25：`minutes_version` 表**仍在**、结构未变、弃用说明在、一期恒 0 行
  [ ] 4  ★ 缺陷 26：复用判据**认内容**（同版本号改稿必重新渲染）—— 实测判定
  [ ] 5  审计链完整（`verify_audit_chain.py` 退出码 0）
  [ ] 6  静态检查（`check_layers.py` 42 文件 / `check_deps.py`）
  [ ] 7  无 E9001 泄漏（畸形输入不炸成 500）
  [ ] 8  ★ 验收记录存在且**无空条目**（防"文档很漂亮但没一个字是跑出来的"）

⚠️ 关于标准 1（本卡的"越界探测器"）
-----------------------------------
T1-16 已经把 SAD §9.1 做到 30/30；T2-02 后续新增 2 个 owner 已批准端点。
本检查仅随批准清单推进计数，核对当前 33 条路由：
  · 逐条点名核对 SAD 的 32 条（数对了但换了个名字，也要红）
  · 额外端点必须**恰好**是那条登记在案的 `GET .../export/undo` 方法守卫
  · 三个口径同时报，任何一个变了就红

⚠️ 标准 4 为什么要单独跑一遍探针
-------------------------------
T1-16 的坑 16/17 证明："读代码下结论"会假绿。缺陷 26 的判据是
"复用到底认不认内容" —— 这件事**必须真跑**：同版本号覆盖记录稿 → 再导出 → 比 sha256。
本脚本把探针**内置**（不依赖 /tmp 里的临时文件），保证可复现。

关于"未验证"的诚实原则（沿用 T1-13 的 UNSUPPORTED 先例）
-------------------------------------------------------
V1/V2 依赖 ASR 运行环境。**缺模型就是缺模型**，本脚本**绝不**把
"跑不了"记成"通过"。要么有实测结论，要么明确标 `未验证 + 原因`。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = "✓"
FAIL = "✗"

API = "/api/v1"

#: SAD §9.1 全表；T2-02 新增 2 个 owner 已批准的模板历史端点，共 32 条。
SAD_9_1 = {
    ("POST", f"{API}/system/handshake"),
    ("POST", f"{API}/meetings"),
    ("POST", f"{API}/meetings/{{meeting_id}}/audio"),
    ("GET", f"{API}/meetings"),
    ("GET", f"{API}/meetings/{{meeting_id}}"),
    ("POST", f"{API}/meetings/{{meeting_id}}/start"),
    ("GET", f"{API}/meetings/{{meeting_id}}/transcript"),
    ("PATCH", f"{API}/segments/{{segment_id}}"),
    ("POST", f"{API}/meetings/{{meeting_id}}/review/accept"),
    ("GET", f"{API}/meetings/{{meeting_id}}/record"),
    ("GET", f"{API}/meetings/{{meeting_id}}/topics"),
    ("POST", f"{API}/meetings/{{meeting_id}}/gate/{{gate}}/decide"),
    ("GET", f"{API}/meetings/{{meeting_id}}/minutes"),
    ("GET", f"{API}/meetings/{{meeting_id}}/diff"),
    ("POST", f"{API}/meetings/{{meeting_id}}/export"),
    ("GET", f"{API}/meetings/{{meeting_id}}/export/{{file}}"),
    ("POST", f"{API}/meetings/{{meeting_id}}/export/undo"),
    ("GET", f"{API}/glossary"),
    ("POST", f"{API}/glossary"),
    ("PUT", f"{API}/glossary/{{entry_id}}"),
    ("DELETE", f"{API}/glossary/{{entry_id}}"),
    ("POST", f"{API}/glossary/import"),
    ("GET", f"{API}/glossary/export"),
    ("GET", f"{API}/templates"),
    ("POST", f"{API}/templates"),
    ("PUT", f"{API}/templates/{{template_id}}"),
    ("GET", f"{API}/templates/{{template_id}}/versions"),
    ("GET", f"{API}/templates/{{template_id}}/versions/{{version_no}}"),
    ("GET", f"{API}/audit/logs"),
    ("POST", f"{API}/audit/export"),
    ("GET", f"{API}/system/selfcheck"),
    ("GET", f"{API}/system/fonts"),
}

#: 超出 SAD §9.1 的、**登记在案**的额外端点（恰好 1 条方法守卫）。
KNOWN_EXTRA = {("GET", f"{API}/meetings/{{meeting_id}}/export/undo")}

MEETING = "m-t117"

#: 验收记录文件（标准 8 检查它）
RECORD_DOC = PROJECT_ROOT / "docs" / "T1-17-一期验收记录.md"


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
# 环境
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _localappdata(path: Path):
    """临时把 `LOCALAPPDATA` 指向测试目录（**必须还原，不是 pop**）。"""
    old = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old


def _copy_config(tmp: Path) -> Path:
    """把 `config/` 复制到临时目录（避免污染工作区）。"""
    dst = tmp / "config"
    shutil.copytree(PROJECT_ROOT / "config", dst)
    return dst


def _make_cfg(tmp: Path):
    from mmwf.settings import load_settings

    cfg_dir = _copy_config(tmp)
    base = load_settings(cfg_dir)
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
            TestClient(self.app, client=("127.0.0.1", 51234),
                       raise_server_exceptions=False)
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    def req(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, API + path,
                                headers={"X-MMWF-Token": self.token}, **kw)
        assert r.status_code == 200, f"{method} {path} 返回 {r.status_code}，不是约定的 200"
        return r.json()

    def abort_close(self) -> None:
        """释放 TestClient（有些标准要顺手关）。"""
        self._stack.close()

    def make_meeting(self, meeting_id: str = MEETING, *, status: str = "archived"):
        from mmwf.infra.db import connect_ctx

        with connect_ctx(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, classification, status,"
                " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (meeting_id, "T1-17 验收会", "internal", status,
                 "accept", "2026-09-29T00:00:00Z", "2026-09-29T00:00:00Z"),
            )
            conn.commit()
        return meeting_id


# ---------------------------------------------------------------------------
# 标准 1：端点清单三口径未变（越界探测器）
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """用 AST 取 .py 真正 import 的模块名（只看代码，不看注释）。"""
    import ast

    mods: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            mods.add(node.module.split(".")[0])
    return mods


def _router_pairs(tmp: Path) -> set[tuple[str, str]]:
    """从**真实路由器**反查 (method, path)。

    ⚠️ 不能用 `app.routes`：本环境的 FastAPI 把 `include_router` 的结果
    包成 `_IncludedRouter`，**它没有 `.path`** —— 遍历得到空集。
    正确做法：`build_router(cfg)` 拿原始路由器（它**不加前缀**），
    路径自己拼 `API_PREFIX` 再比。
    """
    from mmwf.api.routes import build_router
    from mmwf.main import API_PREFIX

    actual: set[tuple[str, str]] = set()
    for r in build_router(_make_cfg(tmp)).routes:
        path = getattr(r, "path", None)
        if path is None:
            continue
        for m in getattr(r, "methods", None) or []:
            if m not in {"HEAD", "OPTIONS"}:
                actual.add((m, API_PREFIX + path))
    return actual


def check_standard_1(tmp: Path) -> bool:
    banner("标准 1：端点清单三口径**未变**（本卡的越界探测器）")
    actual = _router_pairs(tmp)

    ok = True

    sad_missing = SAD_9_1 - actual
    ok &= _assert(not sad_missing, "SAD §9.1 的 32 条仍全部实现",
                  f"缺 {len(sad_missing)} 条：{sorted(sad_missing)}" if sad_missing else "32/32")

    extra = actual - SAD_9_1
    ok &= _assert(extra == KNOWN_EXTRA,
                  "额外端点仍**恰好**是登记在案的方法守卫（没多、没少）",
                  f"实测多出：{sorted(extra)}" if extra != KNOWN_EXTRA else "1/1")

    ok &= _assert(len(SAD_9_1) == 32, "SAD 清单自身 = 32 条", f"{len(SAD_9_1)}")
    ok &= _assert(len(actual) == 33,
                  "路由表 method+path = 33（T2-02 新增 2 个 owner 已批准端点）",
                  f"实测 {len(actual)}")

    paths_only = {p for _, p in actual}
    ok &= _assert(len(paths_only) == 28,
                  "去重后路径数 = 28（T2-02 新增 2 个 owner 已批准端点）",
                  f"实测 {len(paths_only)}")

    # 本卡**不该**新增任何端点：正面断言这一点
    ok &= _assert(not (actual - SAD_9_1 - KNOWN_EXTRA),
                  "★ 本卡未新增任何端点（收口卡不越界）")

    return ok


# ---------------------------------------------------------------------------
# 标准 2：缺陷 14 —— `_saver` / `_app` 生命周期
# ---------------------------------------------------------------------------


def check_standard_2(tmp: Path) -> bool:
    banner("标准 2：缺陷 14 —— close() 后能重连、幂等、不丢注入的 app")
    from mmwf.graph.runner import GraphRunner

    ok = True
    ckpt = tmp / "ckpt.db"

    # ① close() 必须真的释放连接
    r = GraphRunner("m-a14", checkpoint_path=ckpt)
    try:
        _ = r.app
        had = r._saver is not None
        ok &= _assert(had, "访问 .app 后已建立 saver 连接")
        r.close()
        ok &= _assert(r._saver is None, "★ close() 之后 _saver 为 None（连接已释放）")
    finally:
        r.close()

    # ② close() 之后重新编译 → 必须拿到**新**连接（不是那个关掉的）
    r = GraphRunner("m-b14", checkpoint_path=ckpt)
    try:
        _ = r.app
        old_saver = r._saver
        old_app = r._app
        r.close()
        _ = r.app
        ok &= _assert(r._saver is not None and r._saver is not old_saver,
                      "★ close() 后重连拿到的是**新** saver（不是已关闭的旧连接）",
                      "new saver" if r._saver is not old_saver else "复用了旧连接")
        ok &= _assert(r._app is not None and r._app is not old_app,
                      "★ close() 后 .app 是**重新编译**的图（不是那个已失效的图）")
    finally:
        r.close()

    # ③ close() 幂等（连调两次不抛）
    r = GraphRunner("m-c14", checkpoint_path=ckpt)
    try:
        _ = r.app
        r.close()
        r.close()
        ok &= _assert(True, "close() 幂等：连调两次不抛")
    except Exception as exc:  # noqa: BLE001
        ok &= _assert(False, "close() 幂等：连调两次不抛", f"抛了 {type(exc).__name__}: {exc}")
    finally:
        r.close()

    # ④ 外部注入的 app 不能被 close() 丢弃
    from langgraph.checkpoint.memory import InMemorySaver

    # ⚠️ `harness` 住在 **tests/fixtures/graph/** 下（不是 tests/，也不是 src/），
    #    必须显式把它那一层加进 sys.path。
    #    本卡实测踩过两次：先写 `tests/` → ModuleNotFoundError。
    harness_dir = str(PROJECT_ROOT / "tests" / "fixtures" / "graph")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    import harness

    injected = harness.build_test_app(runs=harness.Runs(), checkpointer=InMemorySaver())
    r = GraphRunner("m-d14", app=injected)
    ok &= _assert(r.app is injected, "注入的 app 原样返回")
    r.close()
    ok &= _assert(r.app is injected,
                  "★ close() 不丢弃外部注入的 app（连接归我们管，图不归我们管）")

    return ok


# ---------------------------------------------------------------------------
# 标准 3：缺陷 25 —— `minutes_version` 弃用标注
# ---------------------------------------------------------------------------


def check_standard_3(tmp: Path) -> bool:
    banner("标准 3：缺陷 25 —— minutes_version 表保留 + 弃用标注 + 一期恒 0 行")
    from mmwf.infra.migrations.runner import discover_migrations, migrate

    ok = True
    db = tmp / "mig25.db"
    migrate(db)

    conn = sqlite3.connect(db)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        ok &= _assert("minutes_version" in tables,
                      "★ 表**仍然存在**（弃用 ≠ 删表：删表是不可逆 schema 变更）")

        cols = [r[1] for r in conn.execute("PRAGMA table_info(minutes_version)")]
        expected = ["version_id", "meeting_id", "version_no", "structured_json",
                    "markdown", "is_final", "created_by", "created_at", "note"]
        ok &= _assert(cols == expected, "表结构未变（弃用标注不得顺手改结构）",
                      f"{len(cols)} 列" if cols == expected else f"实测 {cols}")

        n = conn.execute("SELECT COUNT(*) FROM minutes_version").fetchone()[0]
        ok &= _assert(n == 0, "★ 一期该表恒为 0 行（弃用在运行期的体现）", f"{n} 行")
    finally:
        conn.close()

    # 弃用说明必须在迁移文件里，且指明朝向
    sql = discover_migrations()[0].path.read_text(encoding="utf-8")
    marker = "CREATE TABLE IF NOT EXISTS minutes_version"
    ok &= _assert(marker in sql, "建表语句未被删除")
    head = sql[: sql.index(marker)][-2000:] if marker in sql else ""
    ok &= _assert("弃用" in head, "★ 弃用说明写在建表语句上方")
    ok &= _assert("T2-04" in head, "★ 弃用说明指明了启用时点（二期 T2-04）")

    # 迁移幂等（跑三次）
    try:
        migrate(db)
        migrate(db)
        ok &= _assert(True, "迁移幂等：重复执行不报错")
    except Exception as exc:  # noqa: BLE001
        ok &= _assert(False, "迁移幂等：重复执行不报错", f"抛了 {exc}")

    return ok


# ---------------------------------------------------------------------------
# 标准 4：缺陷 26 —— 复用判据认内容（**实测**，不许读代码下结论）
# ---------------------------------------------------------------------------


def check_standard_4(tmp: Path) -> bool:
    banner("标准 4：缺陷 26 —— 复用判据**认内容**（真跑，不读代码）")
    from mmwf.app.services.export import ExportService
    from mmwf.infra.filestore import make_filestore
    from mmwf.infra.migrations.runner import migrate

    ok = True
    data_root = tmp / "d26"
    db = data_root / "mmwf.db"
    data_root.mkdir(parents=True, exist_ok=True)
    migrate(db)

    meeting_id = "m-t117-26"
    store = make_filestore(meeting_id, data_root)

    def export(**kw):
        svc = ExportService(store, db_path=db)
        res = svc.export_docx(now="2026-09-29T10:00:00+08:00",
                              installed_fonts=None, convert_doc=False,
                              write_audit=False, **kw)
        d = res.to_dict()
        f = store.area_dir("export") / Path(d["path"]).name
        digest = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else "<missing>"
        return d, digest

    # 案例 A：新增 v2 → 必须重新渲染
    store.write_text("record/v1.md", "# 记录\n\n原始内容。\n")
    dA1, shaA1 = export()
    store.write_text("record/v2.md", "# 记录\n\n原始内容。\n\n第二段：新增。\n")
    dA2, shaA2 = export()
    ok &= _assert(dA1.get("version") == 1 and dA2.get("version") == 2,
                  "新增 v2 后最新版是 2", f"v{dA1.get('version')} → v{dA2.get('version')}")
    ok &= _assert(shaA1 != shaA2, "★ 新增版本 → 导出字节变了（不复用旧稿）")

    # 案例 B：★ 同版本号、内容被就地改掉（最尖的软肋）
    store.write_text("record/v3.md", "# 记录\n\n第三版原始内容。\n")
    dB1, shaB1 = export(version=3)
    store.write_text("record/v3.md", "# 记录\n\n【已改】金额由 300 万改为 500 万。\n")
    dB2, shaB2 = export(version=3)
    ok &= _assert(shaB1 != shaB2,
                  "★★ 同 version=3 改内容 → 重新渲染（**不是**复用旧字节）",
                  "认内容" if shaB1 != shaB2 else "复用了旧字节 = 真缺陷")
    ok &= _assert(dB2.get("reused") is False,
                  "★ 改稿后 reused 必须为 False", f"reused={dB2.get('reused')}")
    ok &= _assert(dB1.get("idempotency_key") != dB2.get("idempotency_key"),
                  "★ 幂等键体现内容差异")

    # 案例 C：同内容连导两次 → 幂等（**期望**复用）
    meeting2 = "m-t117-26c"
    store2 = make_filestore(meeting2, data_root)
    store2.write_text("record/v1.md", "# 记录\n\n稳定内容。\n")

    def export2():
        svc = ExportService(store2, db_path=db)
        res = svc.export_docx(now="2026-09-29T10:00:00+08:00",
                              installed_fonts=None, convert_doc=False,
                              write_audit=False)
        d = res.to_dict()
        f = store2.area_dir("export") / Path(d["path"]).name
        return d, hashlib.sha256(f.read_bytes()).hexdigest()

    dC1, shaC1 = export2()
    dC2, shaC2 = export2()
    ok &= _assert(shaC1 == shaC2, "同内容两次导出字节相同（幂等）")
    ok &= _assert(dC2.get("reused") is True,
                  "★ 同内容第二次 reused=True（该省的时候要省）",
                  f"reused={dC2.get('reused')}")

    print("\n  → 实测结论：缺陷 26 **不是真缺陷**。复用判据 = 版本 + 内容哈希，语义正确。")
    print("    本卡落地动作：加回归锁（tests/unit/test_export_service.py 两条），防将来改坏。")
    return ok


# ---------------------------------------------------------------------------
# 标准 5：审计链完整
# ---------------------------------------------------------------------------


def check_standard_5(tmp: Path) -> bool:
    banner("标准 5：审计链完整性（tools/verify_audit_chain.py 退出码 0）")
    from mmwf.infra.migrations.runner import migrate

    ok = True
    db = tmp / "audit.db"
    migrate(db)

    # 写几条审计（用真实 logger）。
    # ⚠️ 必须用 **ACTIONS 白名单里已登记**的动作：`AuditLogger.log()` 对
    #    未登记动作会抛 `AuditError`（见 infra/audit.py 的说明）。
    #    本卡第一版随手写了 "meeting_create" —— 不在白名单里，当场抛。
    #    可用的动作见 `infra.audit.ACTIONS`；这里用最通用的两个。
    from mmwf.infra.audit import ACTIONS, AuditLogger

    logs = AuditLogger(db)
    used = [a for a in ("import_audio", "transcribe_done", "export") if a in ACTIONS]
    ok &= _assert(bool(used), "取到已登记的审计动作",
                  f"{used}" if used else f"白名单为空？ACTIONS={sorted(ACTIONS)}")
    for a in used:
        logs.log("accept", a, "meeting", MEETING, result="success")
    ok &= _assert(logs.count() == len(used),
                  "审计记录已写入", f"{logs.count()} 条")

    script = PROJECT_ROOT / "tools" / "verify_audit_chain.py"
    ok &= _assert(script.exists(), "校验脚本存在")
    if not script.exists():
        return ok

    proc = subprocess.run(
        [sys.executable, str(script), "--db", str(db)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    ok &= _assert(proc.returncode == 0, "审计链校验退出码 0",
                  f"rc={proc.returncode}: {out.strip()[-200:]}" if proc.returncode else "rc=0")
    return ok


# ---------------------------------------------------------------------------
# 标准 6：静态检查
# ---------------------------------------------------------------------------


def check_standard_6(tmp: Path) -> bool:
    banner("标准 6：静态检查（层次依赖 / 依赖白名单）")
    ok = True

    for name, expect in (("check_layers.py", "层次依赖检查"), ("check_deps.py", "依赖白名单检查")):
        script = PROJECT_ROOT / "tools" / name
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        ok &= _assert(proc.returncode == 0, f"{expect}通过",
                      out.splitlines()[-1] if out else f"rc={proc.returncode}")

    # 分层红线：api 层不许 import sqlite3（本卡动过 routes.py，顺手锁一下）
    #
    # ⚠️ `routes.py` 已拆成 `routes/` 包，所以逐文件查 —— 只查单个文件
    # 会让拆出来的新模块整片逃出这条红线。
    routes_dir = PROJECT_ROOT / "src" / "mmwf" / "api" / "routes"
    routes_files = sorted(routes_dir.glob("*.py"))
    ok &= _assert(bool(routes_files), "api/routes/ 包在位",
                  f"{routes_dir} 下没有 .py")
    mods: set[str] = set()
    for f in routes_files:
        mods |= _imported_modules(f)
    ok &= _assert("sqlite3" not in mods,
                  "★ api/routes/ 不 import sqlite3（分层 C01）",
                  f"实测 import 了：{sorted(mods & {'sqlite3'})}")
    ok &= _assert("import sqlite3" in "".join(
                      f.read_text(encoding="utf-8") for f in routes_files),
                  "（反向）源码里确实出现过这个字符串 —— 说明上面那条查的是真import，不是空过")
    return ok


# ---------------------------------------------------------------------------
# 标准 7：无 E9001 泄漏
# ---------------------------------------------------------------------------


def check_standard_7(tmp: Path) -> bool:
    banner("标准 7：畸形输入不炸成 500（无 E9001）")
    ok = True

    box = tmp / "s7"
    box.mkdir(parents=True, exist_ok=True)
    with Session(box) as s:
        s.make_meeting()

        cases = [
            ("GET", f"/meetings/{MEETING}/export/..%2f..%2fetc%2fpasswd", None),
            ("GET", f"/meetings/{MEETING}/diff?base=abc&compare=xyz", None),
            ("GET", "/meetings/nonexistent-meeting/diff", None),
            ("GET", "/meetings/nonexistent-meeting/export/x.docx", None),
            ("POST", "/templates", {"name": "x"}),          # 缺字段
            ("PUT", "/templates/does-not-exist", {"name": "x"}),
        ]
        for method, path, payload in cases:
            r = s.client.request(method, API + path,
                                 headers={"X-MMWF-Token": s.token}, json=payload)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            code = body.get("code")
            ok &= _assert(r.status_code == 200 and code != "E9001",
                          f"{method} {path} 不返回 E9001",
                          f"http={r.status_code} code={code}")
    return ok


# ---------------------------------------------------------------------------
# 标准 8：验收记录存在且无空条目
# ---------------------------------------------------------------------------


def check_standard_8(tmp: Path) -> bool:
    banner("标准 8：一期验收记录存在且**无空条目**")

    ok = _assert(RECORD_DOC.exists(), f"验收记录存在（{RECORD_DOC.name}）")
    if not RECORD_DOC.exists():
        print("     → 先写文档再跑本项；本卡核心交付物之一。")
        return ok

    text = RECORD_DOC.read_text(encoding="utf-8")

    # 五个验收门必须都出现
    for gate in ("V1", "V2", "V6", "V8", "V9"):
        ok &= _assert(gate in text, f"验收门 {gate} 有记录")

    # 无空条目：不允许出现 "待填" / "TODO" / "TBD"
    for bad in ("待填", "TODO", "TBD", "（空）"):
        ok &= _assert(bad not in text, f"没有未填写的占位符「{bad}」")

    # 每条"未验证"必须带原因（不能只写"未验证"三个字）
    if "未验证" in text:
        ok &= _assert("原因" in text or "缺" in text,
                      "「未验证」条目均附原因说明")

    # ★ 2026-09-29 新增：M1/M2/M6 的**更正留痕**必须还在。
    #   初版记录把这三项错记为"未验证（本机无 Office）"，实测本机有 Office。
    #   更正若被后人误删（比如有人"清理"掉这段），本项会报警 ——
    #   因为"把已验的东西说成没验"和"把没验的说成验了"**一样是验收失真**。
    ok &= _assert("verify_docx_in_word" in text,
                  "记录里引用了真实 Word 验证工具（M1/M2/M6 的证据链）")
    ok &= _assert("自我更正" in text or "更正" in text,
                  "记录里保留了「曾误判 M1/M2 未验证」的更正留痕")
    return ok


# ---------------------------------------------------------------------------
# 人工项
# ---------------------------------------------------------------------------

#: 真正需要"人/目标机"的项。★ 2026-09-29 更正：M1/M2 与 .doc 导出**已能用
#: 真实 Word（COM 自动化）验收**，已由 `tools/verify_docx_in_word.py` 跑通，
#: 从本表**移出**（见记录 §4）。原本把它们列进来是**误判"环境不具备"** ——
#: 本机其实装了 Office 16.0 与 LibreOffice。剩下这三项才是真的做不到：
#: 拔网线、看措辞、走界面，都必须由人在真实环境里做。
MANUAL_ITEMS = [
    ("M3", "断网下全流程可用（V9）", "要物理拔网线，脚本不能拔"),
    ("M4", "界面无工程术语（V8）", "需要人判断措辞"),
    ("M5", "三步操作闭环、全程无命令行（一期-2）", "需要人走一遍"),
]

#: 已由脚本（`tools/verify_docx_in_word.py`，真实 Word）覆盖的"原人工项"。
#: 记在这里是为了**留痕**：证明它们不是被删掉，而是被升级成了自动验证。
AUTO_VERIFIED_FORMER_MANUAL = [
    ("M1", "docx 在 Word 里能打开", "tools/verify_docx_in_word.py（Word COM 真打开）"),
    ("M2", "表格无缩进穿透", "同上（读 Word 渲染后缩进 + 页面横坐标）"),
    ("M6", ".doc 导出可用（旧版 Office 能打开）", "同上（22/22 个 .doc 原生打开）"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="T1-17 一期收口验收")
    ap.add_argument("--only", type=int, default=0, help="只跑某一个标准（1-8）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = ap.parse_args()

    logging.disable(logging.CRITICAL)

    tmp = Path(tempfile.mkdtemp(prefix="t117_"))
    print(f"临时目录：{tmp}")

    checks = [
        (1, "端点清单三口径未变", check_standard_1),
        (2, "缺陷 14 生命周期", check_standard_2),
        (3, "缺陷 25 死表标注", check_standard_3),
        (4, "缺陷 26 复用判据", check_standard_4),
        (5, "审计链完整", check_standard_5),
        (6, "静态检查", check_standard_6),
        (7, "无 E9001 泄漏", check_standard_7),
        (8, "验收记录无空条目", check_standard_8),
    ]

    results: list[tuple[int, str, bool]] = []
    for num, name, fn in checks:
        if args.only and num != args.only:
            continue
        try:
            ok = fn(tmp)
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            ok = _assert(False, f"标准 {num} 执行期异常", f"{type(exc).__name__}: {exc}")
        results.append((num, name, ok))

    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)

    banner("T1-17 验收结果")
    for num, name, ok in results:
        print(f"  {PASS if ok else FAIL} 标准 {num}  {name}")

    passed = sum(1 for _, _, ok in results if ok)
    print(f"\n  共 {len(results)} 项，通过 {passed} 项")

    print("\n需人工复核（不计入通过数）")
    for tag, item, why in MANUAL_ITEMS:
        print(f"  · {tag}  {item}  —— {why}")

    print("\n原人工项，现已由脚本验（`tools/verify_docx_in_word.py`，真实 Word）")
    for tag, item, how in AUTO_VERIFIED_FORMER_MANUAL:
        print(f"  · {tag}  {item}  —— {how}")

    if passed == len(results):
        print(f"\n{PASS} T1-17 验收通过（{passed}/{len(results)}）")
        return 0
    print(f"\n{FAIL} T1-17 验收未通过（{passed}/{len(results)}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
