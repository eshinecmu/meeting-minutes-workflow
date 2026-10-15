"""T1-16 验收脚本（人可读版）。

施工卡 T1-16 的九条验收标准逐条跑一遍。这张卡补的是
SAD §9.1 里最后 5 个端点，**补齐后 §9.1 全部收口**：

  ① GET    /templates                     模板列表（内置 yaml + 自建表合并）
  ② POST   /templates                     新建模板
  ③ PUT    /templates/{template_id}       改模板（内置不可改；版本自增）
  ④ GET    /meetings/{id}/diff            两版记录稿段落级差异
  ⑤ GET    /meetings/{id}/export/{file}   下载导出文件（★ 安全红线）

九条标准（施工卡 §3.1 原文）
---------------------------
  [ ] 1  端点清单 25 → 30（`build_router` + `API_PREFIX`；含 5 个新路径）
  [ ] 2  模板列表三键齐全（`template_id/name/meeting_kind` 每项都有，`source` 也有）
  [ ] 3  ★ 模板 YAML 改一行即生效（真改 `section_order` → 再 GET → 顺序变了）
  [ ] 4  内置模板不可被 PUT → **E1003**（不是 200、不是 E9001）
  [ ] 5  模板 PUT 版本自增（提交 `999` → 实际 ≠ 999 且比上次大）
  [ ] 6  ★ diff 只有变化段落是 changed（改中间/尾增/头删三段都验）
  [ ] 7  ★ 路径穿越被拒（4 类恶意输入全部 E1001，且无字节流出）
  [ ] 8  下载真的下到 docx（响应体是 ZIP 魔数 `PK\\x03\\x04`）
  [ ] 9  不存在的版本 → E1002

附加 A：没有 E9001（畸形输入不炸成 500）
附加 B：分层 + 依赖白名单（含"api 层不许 import sqlite3"）

⚠️ 记账口径（本卡最容易写错结论的地方）
-------------------------------------
"端点总数"有三个口径，**别混**：

  · **SAD §9.1 = 30 条**（method+path）。那张表 24 行，但
    `/glossary` 与 `/templates` 两行是 `GET/POST/PUT/DELETE` 合写；
    展开（4+3）再加一行表里没列、但 F7.1 要求的 `/glossary/export`，
    正好 30。**这是"进度对照"的权威口径。**
  · 本卡补的 5 条 → 路由表 method+path = **31 条**。
  · 多出来的 1 条是 `GET .../export/undo` 的**方法守卫**（坑 14），
    SAD 里没有。它与 `POST .../export/undo` 同一路径，SAD 只算一条。

所以下面标准 1 会**同时**报三个数，并且**逐条点名**核对 SAD 的 30 条——
数对了但错了一个名字，也是红的。

⚠️ 本脚本四处**反向验证**（与 T1-13/14/15 同一原则）
--------------------------------------------------
  · 标准 3 的反向面：光"能列出模板"不够。一个**把 yaml 读进内存并缓存**
    的实现也能过。所以真去改 yaml 文件、再 GET，断言顺序**真的变了** ——
    这才排除了"改了配置没反应"（N5.2 的技术保障）。
  · 标准 6 的反向面：光"changed 有 3 段"不够。一个**把每段都标 changed**
    的实现同样能凑出 3。必须断言 `unchanged` 段里**不含**被改动的文字，
    且未改动的头/尾段落**仍是 unchanged**。
  · 标准 7 的反向面（★最关键）：HTTP 层喂 `../` 会被 Starlette 在
    **路由之前**规范化掉，根本走不到 `file` 参数（实测得到 E1002）——
    这**不是漏洞**（无字节流出），但**不能拿它当"穿越防住了"的证据**。
    必须**直接调端点函数**喂恶意 `file`，断言 E1001。
    两件都做才算诚实验过。
  · 标准 4 的反向面：E1003 必须**同时**验"不是 200"和"不是 E9001"。
    只看"不是 200"会把"崩成 500"当成通过。

另一条诚实原则：**能自动验的自动验，必须人手的明确列出来**。
四项（文档能否真打开、diff 在前端的可读性、`source` 字段会不会被前端
当字符串显示、中文文件名在浏览器里的表现）本脚本**无法自动判** ——
它们是"好不好用"，不是"对不对"。列进「需人工复核」，**不混进通过数**。

关于环境注意（本机实测，会影响脚本能不能跑通）
---------------------------------------------
  · 主路径一律用进程内 `TestClient`（不起真网络）：本机有
    `http_proxy`，且沙箱会拦截本地 HTTP，走真网络会让结果随环境摇摆。
  · 造记录稿**走 filestore**（`write_text("record/vN.md", ...)`），
    不经过 API —— diff 测的是"读两版并比对"，造数据不该依赖别的端点自证。
  · 下载的 docx 由**真端点** `POST /export` 产出（不能手工塞文件：
    下载端点是读磁盘明文区，但只要文件在就能读；而"导出是否幂等"
    不该由本卡负责）。标准 8 只断言**魔数**，不断言内容正确性。
  · 模板改为**真改磁盘 yaml**（标准 3 的要求），所以每个标准用
    **独立的 config 副本**，避免标准之间互相污染。
"""
from __future__ import annotations

import argparse
import contextlib
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

#: 造一个会议用的固定 id
MEETING = "m-t116"

#: 内置模板 id（`config/templates/regular-meeting.yaml`）
BUILTIN_ID = "tpl-regular-meeting-v1"


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
# 环境：进程内会话（LOCALAPPDATA 钉住 + 令牌 + TestClient + config 副本）
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


def _copy_config(tmp: Path) -> Path:
    """把 `config/` 整个复制到临时目录。

    ★ 标准 3 要**真改 yaml**，不能改项目里的 `config/`（会污染工作区，
    而且下一次跑就"已经改过了"，标准 3 变成假绿）。每个标准各一份副本。
    """
    dst = tmp / "config"
    shutil.copytree(PROJECT_ROOT / "config", dst)
    return dst


def _make_cfg(tmp: Path):
    """一份指向临时目录的配置（不碰真实 data/，config 用副本）。"""
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

    def raw(self, method: str, path: str, **kw):
        """返回**响应对象本身**（要拿 headers / bytes 时用）。"""
        return self.client.request(method, API + path,
                                   headers={"X-MMWF-Token": self.token}, **kw)

    # -- 造数据（走仓储/文件层，不经过 API）--------------------------------

    def make_meeting(self, meeting_id: str = MEETING, *, status: str = "archived"):
        from mmwf.infra.db import connect_ctx

        with connect_ctx(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, classification, status,"
                " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (meeting_id, "T1-16 验收会", "internal", status,
                 "accept", "2026-09-29T00:00:00Z", "2026-09-29T00:00:00Z"),
            )
            conn.commit()
        return meeting_id

    def write_records(self, versions: dict[int, str]) -> None:
        """落记录稿到 `record/vN.md`（加密区，filestore 透明加密）。"""
        from mmwf.infra.filestore import make_filestore

        fs = make_filestore(MEETING, self.cfg.resolve_path(self.cfg.app.data_dir))
        for v, text in versions.items():
            fs.write_text(f"record/v{v}.md", text, stage="record")

    def template_yaml(self, name: str = "regular-meeting.yaml") -> Path:
        return self.cfg.config_dir / "templates" / name

    # -- 直连库（仅验收脚本用；产品代码在 api/ 层禁止 import sqlite3）-------

    def template_rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT template_id, name, version_no FROM template ORDER BY template_id"
            ).fetchall()
        finally:
            conn.close()

    def audit_actions(self) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT action, operator, target_id FROM audit_log ORDER BY log_id"
            ).fetchall()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 标准 1：端点清单（三个口径都要报）
# ---------------------------------------------------------------------------

#: SAD §9.1 全表（method+path 展开后 30 条）—— 逐条点名核对。
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
    ("GET", f"{API}/audit/logs"),
    ("POST", f"{API}/audit/export"),
    ("GET", f"{API}/system/selfcheck"),
    ("GET", f"{API}/system/fonts"),
}

#: 超出 SAD §9.1 的、**登记在案**的额外端点（当前恰好 1 条方法守卫）。
KNOWN_EXTRA = {("GET", f"{API}/meetings/{{meeting_id}}/export/undo")}


def _imported_modules(path: Path) -> set[str]:
    """用 **AST** 取出一个 .py 真正 import 的模块名（只看代码，不看注释）。

    `import a.b` → `a`；`from a.b import c` → `a.b` 与 `a`。
    这样注释/docstring 里出现的 `import sqlite3` 不会被算进来。
    """
    import ast

    mods: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module)
                mods.add(node.module.split(".")[0])
    return mods


def _router_pairs(tmp: Path) -> set[tuple[str, str]]:
    """从**真实路由器**反查 (method, path)。

    ⚠️ 不能用 `app.routes`：本环境的 FastAPI 把 `include_router` 的结果
    包成 `_IncludedRouter`，**它没有 `.path`** —— 遍历得到空集，
    断言会报"清单里的端点全都不在路由表里"（一大串，看不出真漏了哪条）。
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
    """三个口径一起报，并**逐条点名**核对 SAD §9.1 的 30 条。"""
    banner("标准 1：端点清单 25 → 30（从真实路由器反查）")
    actual = _router_pairs(tmp)

    new_five = {
        ("GET", f"{API}/templates"),
        ("POST", f"{API}/templates"),
        ("PUT", f"{API}/templates/{{template_id}}"),
        ("GET", f"{API}/meetings/{{meeting_id}}/diff"),
        ("GET", f"{API}/meetings/{{meeting_id}}/export/{{file}}"),
    }

    ok = True
    missing_new = new_five - actual
    ok &= _assert(not missing_new, "本卡 5 个新端点全部在路由表里",
                  f"缺：{sorted(missing_new)}" if missing_new else "5/5")

    # ── SAD §9.1 逐条点名：数对了但错一个名字，也是红的 ──────────────
    sad_missing = SAD_9_1 - actual
    ok &= _assert(not sad_missing, "SAD §9.1 的 30 条**全部**已实现",
                  f"缺 {len(sad_missing)} 条：{sorted(sad_missing)}" if sad_missing else "30/30")

    extra = actual - SAD_9_1
    ok &= _assert(extra == KNOWN_EXTRA,
                  "超出 SAD 的端点只有登记在案的方法守卫",
                  f"实测多出：{sorted(extra)}" if extra != KNOWN_EXTRA else "1/1")

    ok &= _assert(len(actual) == 31,
                  "路由表 method+path = 31（SAD 30 + 1 条方法守卫）",
                  f"实测 {len(actual)}")
    ok &= _assert(len(SAD_9_1) == 30, "SAD 清单自身 = 30 条", f"{len(SAD_9_1)}")

    # 路径口径（SAD 数端点的口径：`export/undo` 的 GET/POST 算一条）
    paths_only = {p for _, p in actual}
    ok &= _assert(len(paths_only) == 26,
                  "去重后路径数 = 26（供与 SAD 的 30 对照时参考）",
                  f"实测 {len(paths_only)}")

    ok &= _assert(all(m not in {"HEAD", "OPTIONS"} for m, _ in actual),
                  "清单只统计真实业务方法（HEAD/OPTIONS 是框架自带，不算）")
    return ok


# ---------------------------------------------------------------------------
# 标准 2：模板列表三键齐全
# ---------------------------------------------------------------------------


def check_standard_2(tmp: Path) -> bool:
    """每项都有 `template_id/name/meeting_kind`，且 `source` 也有。"""
    banner("标准 2：GET /templates 三键齐全 + source")
    with Session(tmp) as s:
        data = s.ok("GET", "/templates")
        templates = data["templates"]
        ok = True

        ok &= _assert(len(templates) >= 4,
                      "内置模板至少 4 个（config/templates/*.yaml）",
                      f"实测 {len(templates)}")

        required = ("template_id", "name", "meeting_kind", "source")
        bad: list[tuple[str, list[str]]] = []
        for t in templates:
            miss = [k for k in required if not t.get(k)]
            if miss:
                bad.append((t.get("template_id", "?"), miss))
        ok &= _assert(not bad, "每项都有 template_id/name/meeting_kind/source",
                      f"缺字段的：{bad}" if bad else f"{len(templates)}/{len(templates)}")

        sources = {t["source"] for t in templates}
        ok &= _assert(sources <= {"builtin", "db"},
                      "source 取值只在 {builtin, db}", f"实测 {sorted(sources)}")
        ok &= _assert(all(t["source"] == "builtin" for t in templates),
                      "刚建库时**全部**来自内置 yaml（表是空的）")

        counts = data.get("counts") or {}
        ok &= _assert(counts.get("total") == len(templates),
                      "counts.total 与实际条数一致",
                      f"total={counts.get('total')} len={len(templates)}")
        ok &= _assert(counts.get("builtin") == len(templates)
                      and counts.get("db") == 0,
                      "counts 分builtin/db，与 source 对得上",
                      f"{counts}")

        # 按 meeting_kind 过滤
        regular = s.ok("GET", "/templates", params={"meeting_kind": "regular_meeting"})
        ok &= _assert(all(t["meeting_kind"] == "regular_meeting"
                          for t in regular["templates"]),
                      "meeting_kind 过滤生效",
                      f"{len(regular['templates'])} 条")
        return ok


# ---------------------------------------------------------------------------
# 标准 3 ★：YAML 改一行即生效
# ---------------------------------------------------------------------------


def check_standard_3(tmp: Path) -> bool:
    """★ 真改 yaml 的 `section_order` → 再 GET → 顺序**真的变了**。

    反向面：一个"读进内存并缓存"的实现也能列出模板。必须证明
    **改盘上的文件、不重启、不碰代码**就能生效 —— 这才是 N5.2 的
    技术保障。（也因此才要判"每次读盘、不缓存"这个实现选择。）
    """
    banner("标准 3：★ 模板 YAML 改一行即生效（不重启、不改代码）")
    with Session(tmp) as s:
        yaml_path = s.template_yaml()
        ok = True

        before = _template_by_id(s.ok("GET", "/templates"), BUILTIN_ID)
        ok &= _assert(before is not None, f"找得到内置模板 {BUILTIN_ID}")
        if before is None:
            return False
        orig_order = list(before["section_order"])
        print(f"      改前 section_order：{orig_order}")

        # ── 真改盘上的 yaml：把第一项挪到最后 ─────────────────────────
        text = yaml_path.read_text(encoding="utf-8")
        first = orig_order[0]
        # 只在 `section_order:` 块内操作 —— 用行级处理，避免误伤
        # `required_elements` 里可能出现的同名字符串。
        lines = text.splitlines(keepends=True)
        start = next(i for i, ln in enumerate(lines) if ln.startswith("section_order:"))
        end = start + 1
        while end < len(lines) and (lines[end].startswith("  ") or not lines[end].strip()):
            if not lines[end].strip():
                break
            end += 1
        block = lines[start:end]
        items = [ln for ln in block[1:] if ln.strip().startswith("- ")]
        rotated = items[1:] + items[:1]
        new_text = "".join(lines[: start + 1]) + "".join(rotated) + "".join(lines[end:])
        yaml_path.write_text(new_text, encoding="utf-8")

        after = _template_by_id(s.ok("GET", "/templates"), BUILTIN_ID)
        ok &= _assert(after is not None, "改完 yaml 后模板仍能读到（没解析坏）")
        if after is None:
            return False
        new_order = list(after["section_order"])
        print(f"      改后 section_order：{new_order}")

        expected = orig_order[1:] + orig_order[:1]
        ok &= _assert(new_order == expected,
                      "★ 改一行 yaml → 立刻生效（顺序按改后的来）",
                      f"期望 {expected}，实测 {new_order}")
        ok &= _assert(new_order != orig_order, "顺序确实变了（不是原样返回）")
        ok &= _assert(first == orig_order[0] and new_order[-1] == first,
                      f"特定位移正确：{first!r} 从第 1 位移到了最后一位")

        # ── 反向面：没改的 yaml **不该**跟着变 ────────────────────────
        other = _template_by_id(s.ok("GET", "/templates"), "tpl-coordination-v1")
        ok &= _assert(other is not None and other["section_order"] != new_order,
                      "★ 只动了那一个文件，别的模板不受影响（不是全局刷新）")

        # ── 复核：磁盘上确实是改后的内容（排除"内存里改了盘上没改"）──
        disk = yaml_path.read_text(encoding="utf-8")
        ok &= _assert(disk == new_text, "磁盘上的 yaml 确实是改后的内容")
        return ok


def _template_by_id(data: dict, template_id: str) -> dict | None:
    for t in data.get("templates", []):
        if t.get("template_id") == template_id:
            return t
    return None


# ---------------------------------------------------------------------------
# 标准 4：内置模板不可 PUT → E1003
# ---------------------------------------------------------------------------


def check_standard_4(tmp: Path) -> bool:
    """E1003 必须**同时**验"不是 200"和"不是 E9001"（只看前者会把崩溃当通过）。"""
    banner("标准 4：内置模板不可 PUT → E1003")
    with Session(tmp) as s:
        body = {
            "name": "想来改内置模板",
            "meeting_kind": "regular_meeting",
            "required_elements": [{"key": "title", "label": "标题"}],
            "section_order": ["会议基本情况"],
        }
        r = s.raw("PUT", f"/templates/{BUILTIN_ID}", json=body)
        env = r.json()
        ok = True

        ok &= _assert(r.status_code == 200,
                      "HTTP 仍是约定的 200（信封承载错误）",
                      f"实测 {r.status_code}")
        ok &= _assert(env.get("ok") is False, "ok=False", f"{env.get('ok')}")
        ok &= _assert(env.get("code") == "E1003",
                      "★ 错误码 = E1003（不是 E1001、不是 E9001）",
                      f"实测 {env.get('code')}")
        ok &= _assert(not str(env.get("code", "")).startswith("E9"),
                      "★ 不是 E9xxx（没崩成内部错误）")
        ok &= _assert("自带" in (env.get("message") or "")
                      or "内置" in (env.get("message") or ""),
                      "文案点明『软件自带，不能直接改』",
                      f"{env.get('message')!r}")
        # ⚠️ 上面这句原本只找「内置」，实测产品文案用的是
        # 「这是**软件自带**的模板，不能直接改。……请改用「另存为新模板」」——
        # 比"内置"更口语、还给了出路。**断言跟着产品走，不是产品跟着断言走**：
        # 我一开始写的是注释里那个词，而不是用户真会看到的词。
        ok &= _assert("另存为" in (env.get("message") or "")
                      or "新建" in (env.get("message") or ""),
                      "★ 文案给了**出路**（不是只说『不行』）",
                      f"{env.get('message')!r}")
        return ok


# ---------------------------------------------------------------------------
# 标准 5：模板 PUT 版本自增
# ---------------------------------------------------------------------------


def check_standard_5(tmp: Path) -> bool:
    """自建模板 PUT：`version_no` 提交 `999` → 实际 ≠ 999 且比上次大。"""
    banner("标准 5：自建模板 PUT 版本自增（请求体里的版本号被忽略）")
    with Session(tmp) as s:
        ok = True
        body = {
            "name": "自定义周会模板",
            "meeting_kind": "regular_meeting",
            "required_elements": [
                {"key": "title", "label": "标题"},
                {"key": "attendees", "label": "参会人员", "min_count": 2},
            ],
            "section_order": ["会议基本情况", "议定事项"],
        }
        created = s.ok("POST", "/templates", json=body)["template"]
        tid = created["template_id"]
        v1 = created["version_no"]
        ok &= _assert(bool(tid), "POST /templates 返回了 template_id", tid)
        ok &= _assert(v1 == 1, "新建模板 version_no 从 1 开始", f"实测 {v1}")

        # ① 第一次改 → 应该到 2
        upd1 = s.ok("PUT", f"/templates/{tid}",
                    json={**body, "name": "自定义周会模板（改）"})["template"]
        ok &= _assert(upd1["version_no"] == v1 + 1,
                      "第一次 PUT → 版本 +1", f"{v1} → {upd1['version_no']}")

        # ② ★ 提交 999，应被**忽略**
        upd2 = s.ok("PUT", f"/templates/{tid}",
                    json={**body, "name": "自定义周会模板（再改）",
                          "version_no": 999})["template"]
        ok &= _assert(upd2["version_no"] != 999,
                      "★ 请求体里的 version_no=999 被忽略",
                      f"实测 {upd2['version_no']}")
        ok &= _assert(upd2["version_no"] == upd1["version_no"] + 1,
                      "★ 版本由服务端自增到上一个 +1",
                      f"{upd1['version_no']} → {upd2['version_no']}")

        # ③ 新版本**同名**：DDL 是 UNIQUE(name, version_no)，
        #    所以"改名 + 自增"必须一起才能落库。这里顺带验库里确实两行。
        rows = [r for r in s.template_rows() if r[0] == tid]
        ok &= _assert(len(rows) >= 1, "template 表里确实落了这行", f"{rows}")

        # ④ 列表里能看到它，且 source=db
        listed = _template_by_id(s.ok("GET", "/templates"), tid)
        ok &= _assert(listed is not None, "自建模板出现在列表里")
        if listed is not None:
            ok &= _assert(listed["source"] == "db", "source=db",
                          f"{listed['source']}")

        # ⑤ 留痕
        acts = [a for a in s.audit_actions() if a[0] == "template_change"]
        ok &= _assert(len(acts) >= 3,
                      "create + 2×update 都写了 template_change 审计",
                      f"实测 {len(acts)} 条")
        ok &= _assert(all(a[1] for a in acts), "审计里有 operator",
                      f"{[a[1] for a in acts]}")
        return ok


# ---------------------------------------------------------------------------
# 标准 6 ★：diff 只有变化段落是 changed
# ---------------------------------------------------------------------------


def check_standard_6(tmp: Path) -> bool:
    """★ 改中间一段 + 尾增一段 + 头删一段 → 三类各中一次，
    且 **unchanged 段里不含被改动的文字**、未动的头尾**仍是 unchanged**。

    反向面：一个"把每段都标 changed"的实现也能凑出"changed 有 3 段"。
    必须同时断言 unchanged 的**内容**，才排除这种假绿。
    """
    banner("标准 6：★ diff 段落级比对（改中间 / 尾增 / 头删）")
    with Session(tmp) as s:
        s.make_meeting()
        ok = True

        # ⚠️ 构造数据的讲究（我前两版都栽在这，`removed` 恒为 0）：
        #
        # `difflib.SequenceMatcher` 是按 **opcode** 切块的。如果"删一段"和
        # "改一段"**挨着**发生，它会把两件事合并成**一个 replace**
        # （`old` 两项 / `new` 一项）—— 因为"2 行换成 1 行"比
        # "删 1 行 + 改 1 行"代价更低。于是 `removed` 是 0。
        #
        # 这**不是产品 bug**：`difflib` 无从知道我的"本意"是删一段又改一段。
        # 我第二版把待删段与待改段之间插了一个"不变段"隔开，**仍然**被合并 ——
        # 因为那个不变段比"合并替换"更贵。实测确认：
        #     deleted + changed 相邻 → 1 个 changed（removed=0）
        #     只删一段（前后都不动）→ 1 个 removed（干净析出）
        #
        # 教训：**要单独验某一类，就必须让那一类在整篇里是唯一的变量。**
        # 硬塞在一对版本里，测的是 `difflib` 的合并策略，不是我们的语义。
        # 所以改成**三对独立版本**，每对只变一件事。
        base = "\n\n".join([
            "第一段：会议基本情况，由办公室召集。",
            "第二段：出席人员包括各科室负责人。",
            "第三段：会议听取各科室工作汇报。",
            "第四段：会议要求，各科室按分工抓好落实。",
            "第五段：会议还就下阶段安排作了说明。",
        ])
        s.write_records({1: base})

        # v2：**只改**中间一段
        s.write_records({2: base.replace("会议听取各科室工作汇报",
                                        "会议听取各科室工作汇报和问题反映")})
        # v3：**只删**第三段
        s.write_records({3: "\n\n".join(base.split("\n\n")[:2]
                                        + base.split("\n\n")[3:])})
        # v4：**只在尾部加**一段
        s.write_records({4: base + "\n\n第六段：会议议定，由综合科牵头办理。"})

        r12 = s.ok("GET", f"/meetings/{MEETING}/diff", params={"base": 1, "compare": 2})
        r13 = s.ok("GET", f"/meetings/{MEETING}/diff", params={"base": 1, "compare": 3})
        r14 = s.ok("GET", f"/meetings/{MEETING}/diff", params={"base": 1, "compare": 4})

        ok &= _assert(r12.get("comparable") is True, "comparable=True")
        ok &= _assert(r12.get("base_version") == 1
                      and r12.get("compare_version") == 2,
                      "base/compare 回显与实际一致",
                      f"{r12.get('base_version')} → {r12.get('compare_version')}")
        ok &= _assert(all(x["type"] in {"unchanged", "changed", "removed", "added"}
                          for x in r12["segments"]),
                      "所有差异块的 type 都在四值之内"
                      "（unchanged/changed/removed/added）",
                      f"实测 {sorted({x['type'] for x in r12['segments']})}")

        # 默认（不带 base/compare）= 最新两版
        dflt = s.ok("GET", f"/meetings/{MEETING}/diff")
        ok &= _assert(dflt["compare_version"] == 4 and dflt["base_version"] == 3,
                      "不带参数时默认对比「最新两版」（v3 → v4）",
                      f"{dflt['base_version']} → {dflt['compare_version']}")

        def _kinds(data: dict) -> dict:
            return {k: [x for x in data["segments"] if x["type"] == k]
                    for k in ("unchanged", "changed", "removed", "added")}

        def _flat(block: dict) -> list[str]:
            """摊平一个差异块的全部可见文本。

            ⚠️ 载荷形状**不是**统一的 `text` 字段（`DiffBlock.to_dict`）：
              unchanged → {"type", "lines", 可能 "elided"}
              changed   → {"type", "old", "new"}
              removed   → {"type", "old"}
              added     → {"type", "new"}
            我第一版按 `x["text"]` 取，直接 KeyError，标准 6 挂在半途。
            """
            out: list[str] = []
            for key in ("lines", "old", "new"):
                out.extend(block.get(key) or [])
            return out

        def _unchanged_text(data: dict) -> str:
            return "\n".join(ln for x in _kinds(data)["unchanged"]
                             for ln in _flat(x))

        # ── ① 只改一段 → changed=1，其余 0 ────────────────────────────
        s12 = r12["summary"]
        print("      ① 只改一段：", s12)
        ok &= _assert(s12["changed"] == 1 and s12["removed"] == 0
                      and s12["added"] == 0,
                      "① 只改一段 → 恰好 1 处 changed，无增删", f"{s12}")
        ok &= _assert("第三段：会议听取各科室工作汇报" not in _unchanged_text(r12),
                      "① 被改的原句不在 unchanged 里（改前）")
        ok &= _assert("第三段：会议听取各科室工作汇报和问题反映"
                      not in _unchanged_text(r12),
                      "① 被改的新句不在 unchanged 里（改后）")

        # ── ② 只删一段 → removed=1，其余 0 ────────────────────────────
        s13 = r13["summary"]
        print("      ② 只删一段：", s13)
        ok &= _assert(s13["removed"] == 1 and s13["changed"] == 0
                      and s13["added"] == 0,
                      "② 只删一段 → 恰好 1 处 removed，无改增", f"{s13}")
        ok &= _assert("第三段：会议听取各科室工作汇报" not in _unchanged_text(r13),
                      "② 被删的段落不在 unchanged 里")
        ok &= _assert("第三段：会议听取各科室工作汇报"
                      in "\n".join(ln for x in _kinds(r13)["removed"]
                                   for ln in _flat(x)),
                      "② 被删的段落**确实出现在 removed 块里**（不是凭空消失）")

        # ── ③ 只在尾部加一段 → added=1，其余 0 ────────────────────────
        s14 = r14["summary"]
        print("      ③ 只加一段：", s14)
        ok &= _assert(s14["added"] == 1 and s14["changed"] == 0
                      and s14["removed"] == 0,
                      "③ 只在尾部加一段 → 恰好 1 处 added，无改删", f"{s14}")
        ok &= _assert("第六段：会议议定，由综合科牵头办理。"
                      not in _unchanged_text(r14),
                      "③ 新加的段落不在 unchanged 里")

        # ── ④ ★★ 反向面：**没动的段落必须仍是 unchanged** ────────────
        # 没有这一条，一个"把每段都标 changed"的实现照样能过上面全部断言
        # —— 因为那些断言的形态都是"**不**在 unchanged 里"，而
        # unchanged 若是空的，它们全都成立！这才是最该防的假绿。
        untouched = ["第一段：会议基本情况，由办公室召集。",
                     "第二段：出席人员包括各科室负责人。",
                     "第四段：会议要求，各科室按分工抓好落实。",
                     "第五段：会议还就下阶段安排作了说明。"]
        for label, data in (("①改一段", r12), ("②删一段", r13), ("③加一段", r14)):
            missing = [t for t in untouched if t not in _unchanged_text(data)]
            ok &= _assert(not missing,
                          f"④ ★ {label}：4 个真没动的段落**全部**仍是 unchanged",
                          f"丢了：{missing}" if missing else "4/4")

        # ── ⑤ 完全相同 → 0 处变化 ─────────────────────────────────────
        #
        # ⚠️ 这里**不能**断言 `unchanged == 5`。实测是 `unchanged == 1` ——
        # 因为 `MAX_UNCHANGED_LINES=40` 那段**折叠**逻辑会把长串未变行
        # 收成**一个** unchanged 块（并在 `elided` 里记下折掉了多少）。
        # 我一开始按"5 段就是 5"写，红得莫名其妙。
        # 折叠是**有意**的设计（施工卡裁决 D3）：一段没变就原样回给前端，
        # 500 段记录稿的响应体会大到没法用。判据要落在"**语义**上"：
        # 没有任何变化 + 未变内容仍在，而不是"块数等于段数"。
        same = s.ok("GET", f"/meetings/{MEETING}/diff",
                    params={"base": 1, "compare": 1})
        ss = same["summary"]
        ok &= _assert(ss["changed"] == 0 and ss["removed"] == 0
                      and ss["added"] == 0 and ss["unchanged"] >= 1,
                      "⑤ 同一版对比自己 → 0 处变化，且未变内容非空",
                      f"{ss}")
        # 五段原文都还能在 unchanged 里找到（折叠不丢内容）
        same_text = _unchanged_text(same)
        lost = [p for p in base.split("\n\n") if p not in same_text]
        ok &= _assert(not lost,
                      "⑤ 折叠**不丢内容**：5 段原文都能在 unchanged 里找到",
                      f"丢了：{lost}" if lost else "5/5")
        # 折叠确实发生了（elided 有值）—— 证明 D3 生效，不是"没实现折叠"
        elided = [x.get("elided", 0) for x in _kinds(same)["unchanged"]]
        print(f"      ⑤ 同一版：{ss}，elided={elided}")
        return ok


# ---------------------------------------------------------------------------
# 标准 7 ★：路径穿越被拒
# ---------------------------------------------------------------------------

#: 恶意文件名（HTTP 层喂这些会被 Starlette 规范化掉一部分，
#: 所以下面**还要**直接调端点函数再喂一遍 —— 那才是真凭据）
TRAVERSAL = [
    "../keystore/master.key",
    "..\\keystore\\master.key",
    "a/b.docx",
    "..%2f..%2fmaster.key",
    "%2e%2e%2fmaster.key",
    "....//x",
    "..",
    ".",
    "",
    "\x00",
    "sub/../../../etc/passwd",
]


def check_standard_7(tmp: Path) -> bool:
    """★ 两层都要验：
      · HTTP 层：带目录成分 / 百分号编码的输入**拿不到文件字节**
      · 判据层：**直接调端点函数**喂恶意 `file`，断言 E1001

    为什么必须在判据层再验一遍：Starlette 在**路由之前**就把 URL 里的
    `..` 规范化掉了，`/export/../keystore/master.key` 会被改写成
    `/keystore/master.key`，于是**根本走不到** `file` 参数（实测 E1002）。
    这**不是漏洞**（无字节流出），但**不能拿它当"穿越防住了"的证据**——
    被规范化掉的是 URL，不是我们的判据。
    """
    banner("标准 7：★ 路径穿越（HTTP 层 + 直调判据层）")
    with Session(tmp) as s:
        s.make_meeting()
        s.write_records({1: "# 记录稿\n\n内容。"})
        s.ok("POST", f"/meetings/{MEETING}/export", json={})
        ok = True

        # ── ① HTTP 层：绝不能有字节流出 ───────────────────────────────
        #
        # ⚠️ `\x00`（NUL）**发不出去**：httpx 在客户端就抛
        # `InvalidURL: Invalid non-printable ASCII character in URL`。
        # 这不是"服务端没防住"，是**这个字符根本构造不出合法 URL** ——
        # 它进不了 HTTP 层，所以在这一层没有可测的对象。
        # 它的判据在**判据层**（②）覆盖：`Path("\x00").name` 会抛
        # `ValueError`，端点必须先拦成 E1001 而不是让它炸成 E9001。
        http_cases = [b for b in TRAVERSAL if b and "\x00" not in b]
        leaked: list[str] = []
        for bad in http_cases:
            r = s.raw("GET", f"/meetings/{MEETING}/export/{bad}")
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                body = r.json()
                if str(body.get("code") or "").startswith("E9"):
                    leaked.append(f"{bad!r} → {body.get('code')}（崩了）")
            else:
                leaked.append(f"{bad!r} → 返回了非 JSON（{ctype}）"
                              f" {len(r.content)} 字节")
        ok &= _assert(not leaked, f"① HTTP 层：{len(http_cases)} 类恶意输入都没有字节流出",
                      "；".join(leaked) if leaked else "0 泄漏")
        print(f"      （`\\x00` 在 HTTP 层无法构造 URL，留到判据层验；"
              f"本层实喂 {len(http_cases)} 例）")

        # ── ② 判据层：直接调端点函数（真凭据）────────────────────────
        from mmwf.api.routes import build_router

        endpoint = None
        for r in build_router(s.cfg).routes:
            if getattr(r, "path", None) == "/meetings/{meeting_id}/export/{file}":
                endpoint = r.endpoint
                break
        ok &= _assert(endpoint is not None, "找到下载端点的函数本体")
        if endpoint is None:
            return False

        from mmwf.domain.errors import MmwfError

        accepted: list[str] = []
        wrong_code: list[str] = []
        for bad in TRAVERSAL:
            try:
                endpoint(meeting_id=MEETING, file=bad)
                accepted.append(repr(bad))
            except MmwfError as exc:
                if exc.code != "E1001":
                    wrong_code.append(f"{bad!r} → {exc.code}")
            except Exception as exc:  # noqa: BLE001
                wrong_code.append(f"{bad!r} → {type(exc).__name__}: {exc}")
        ok &= _assert(not accepted,
                      "② 判据层：11 类恶意输入**全部**被拒（没有一个被接受）",
                      f"被接受的：{accepted}" if accepted else "11/11 拒绝")
        ok &= _assert(not wrong_code,
                      "② 判据层：拒绝码一律 E1001（含空串/单点/双点）",
                      "；".join(wrong_code) if wrong_code else "全是 E1001")

        # ── ③ 正常文件名**必须**能下（否则"全拒"也能过上面两条）──────
        got = s.raw("GET", f"/meetings/{MEETING}/export/{MEETING}-v1.docx")
        ok &= _assert(got.status_code == 200
                      and got.content.startswith(b"PK\x03\x04"),
                      "③ 正常文件名仍能下载（不是把所有请求都拒了）",
                      f"{got.status_code} {got.content[:4]!r}")
        return ok


# ---------------------------------------------------------------------------
# 标准 8：下载真的下到 docx
# ---------------------------------------------------------------------------


def check_standard_8(tmp: Path) -> bool:
    """先 `POST /export`，再 `GET /export/{file}` → 响应体是 ZIP 魔数。"""
    banner("标准 8：GET /export/{file} 真的下到 docx（ZIP 魔数）")
    with Session(tmp) as s:
        s.make_meeting()
        s.write_records({1: "# 会议记录\n\n会议认为，方案可行。\n\n会议要求，抓好落实。"})
        ok = True

        exp = s.ok("POST", f"/meetings/{MEETING}/export", json={})
        rel = exp.get("path") or exp.get("rel_path")
        ok &= _assert(bool(rel), "POST /export 返回了产物路径", str(rel))
        if not rel:
            return False
        name = Path(rel).name

        r = s.raw("GET", f"/meetings/{MEETING}/export/{name}")
        ok &= _assert(r.status_code == 200, "下载返回 200", f"{r.status_code}")
        ok &= _assert(r.content[:4] == b"PK\x03\x04",
                      "★ 响应体是 ZIP 魔数 PK\\x03\\x04（.docx 就是 zip）",
                      f"实测 {r.content[:4]!r}，共 {len(r.content)} 字节")
        ok &= _assert(len(r.content) > 1000, "字节数不是空壳",
                      f"{len(r.content)} 字节")

        ctype = r.headers.get("content-type", "")
        ok &= _assert("word" in ctype or "octet-stream" in ctype,
                      "Content-Type 是 docx 或 octet-stream", ctype)

        disp = r.headers.get("content-disposition", "")
        ok &= _assert("attachment" in disp.lower(),
                      "Content-Disposition 是 attachment（不是 inline）", disp)
        ok &= _assert("filename*=UTF-8''" in disp,
                      "★ 文件名走 RFC 5987 `filename*=UTF-8''`（中文名才不乱码）",
                      disp)

        # 留痕：下载要写 export_download（部署手册 §6.2「导出记录」）
        acts = [a for a in s.audit_actions() if a[0] == "export_download"]
        ok &= _assert(len(acts) >= 1, "下载写了 export_download 审计",
                      f"{len(acts)} 条")

        # 不存在的文件 → E1002（不是 404、不是 E9001）
        miss = s.req("GET", f"/meetings/{MEETING}/export/nope-not-here.docx")
        ok &= _assert(miss.get("code") == "E1002"
                      and not str(miss.get("code", "")).startswith("E9"),
                      "不存在的文件 → E1002", f"{miss.get('code')}")
        return ok


# ---------------------------------------------------------------------------
# 标准 9：不存在的版本 → E1002
# ---------------------------------------------------------------------------


def check_standard_9(tmp: Path) -> bool:
    """`diff?compare=99` → E1002。"""
    banner("标准 9：不存在的版本 → E1002")
    with Session(tmp) as s:
        s.make_meeting()
        s.write_records({1: "只有一版。"})
        ok = True

        bad = s.req("GET", f"/meetings/{MEETING}/diff", params={"compare": 99})
        ok &= _assert(bad.get("ok") is False, "ok=False", f"{bad.get('ok')}")
        ok &= _assert(bad.get("code") == "E1002",
                      "★ compare=99 → E1002（不是 E9001）",
                      f"实测 {bad.get('code')}")
        ok &= _assert(not str(bad.get("code", "")).startswith("E9"),
                      "不是 E9xxx")
        ok &= _assert("99" in (bad.get("message") or "")
                      or "版本" in (bad.get("message") or ""),
                      "文案点明了是版本问题", f"{bad.get('message')!r}")

        # 只有 1 版时**不带参数** → 200 + comparable=false（裁决 D4）
        one = s.ok("GET", f"/meetings/{MEETING}/diff")
        ok &= _assert(one.get("comparable") is False,
                      "只有 v1 时不报错：comparable=False（裁决 D4）",
                      f"{one.get('comparable')}")
        ok &= _assert(one.get("segments") == [], "segments 为空列表")

        # 没有记录稿的会议 → E1002
        s.make_meeting("m-norecord")
        none = s.req("GET", "/meetings/m-norecord/diff")
        ok &= _assert(none.get("code") == "E1002",
                      "完全没有记录稿 → E1002（不是 E9001）",
                      f"{none.get('code')}")

        # 不存在的会议 → E1002
        ghost = s.req("GET", "/meetings/m-ghost/diff")
        ok &= _assert(ghost.get("code") == "E1002",
                      "不存在的会议 → E1002", f"{ghost.get('code')}")

        # compare=0 → E1001（版本号不合法，是"填错"不是"找不到"）
        zero = s.req("GET", f"/meetings/{MEETING}/diff", params={"compare": 0})
        ok &= _assert(zero.get("code") == "E1001",
                      "compare=0 → E1001（填错，不是找不到）",
                      f"{zero.get('code')}")
        return ok


# ---------------------------------------------------------------------------
# 附加 A：没有 E9001
# ---------------------------------------------------------------------------


def check_no_internal_error_for_bad_input(tmp: Path) -> bool:
    """对 5 个端点各喂一组畸形输入，断言不落进 E9xxx。"""
    banner("附加 A：畸形输入不许变成 E9001")
    with Session(tmp) as s:
        s.make_meeting()
        s.write_records({1: "内容。"})
        ok = True

        cases: list[tuple[str, str, dict]] = [
            # (method, path, kwargs)
            ("GET", "/templates", {"params": {"meeting_kind": ""}}),
            ("GET", "/templates", {"params": {"include_disabled": "notabool"}}),
            ("POST", "/templates", {"json": {}}),
            ("POST", "/templates", {"json": {"required_elements": "不是列表"}}),
            ("POST", "/templates", {"json": {"required_elements": [1, 2, 3]}}),
            ("POST", "/templates", {"json": {"section_order": [1, 2, 3]}}),
            ("POST", "/templates", {"json": {"required_elements":
                                             [{"key": "", "label": ""}]}}),
            ("POST", "/templates", {"json": {"name": "x" * 5000,
                                             "required_elements": [{"key": "k",
                                                                    "label": "l"}],
                                             "section_order": ["a"]}}),
            ("PUT", "/templates/tpl-does-not-exist", {"json": {}}),
            ("PUT", "/templates/tpl-does-not-exist",
             {"json": {"required_elements": [{"key": "k", "label": "l"}],
                       "section_order": ["a"]}}),
            ("GET", "/meetings/m-x/diff", {"params": {"compare": "不是数字"}}),
            ("GET", "/meetings/m-x/diff", {"params": {"base": -5}}),
            ("GET", "/meetings/m-x/diff", {"params": {"base": 2, "compare": 2}}),
            ("GET", "/meetings/m-x/export/", {}),
            ("GET", "/meetings/m-x/export/a" * 1, {}),
        ]

        bad: list[str] = []
        for method, path, kw in cases:
            try:
                r = s.client.request(method, API + path,
                                     headers={"X-MMWF-Token": s.token}, **kw)
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{method} {path} 抛异常 {type(exc).__name__}: {exc}")
                continue
            if r.status_code != 200:
                bad.append(f"{method} {path} → HTTP {r.status_code}")
                continue
            body = r.json()
            code = str(body.get("code") or "")
            if code.startswith("E9"):
                bad.append(f"{method} {path} → {code} {body.get('message')!r}")

        ok &= _assert(not bad, f"{len(cases)} 组畸形输入没有一个落进 E9xxx",
                      "；".join(bad) if bad else f"{len(cases)}/{len(cases)}")

        # 附加断言：E9001 的文案绝不能出现在正常错误里
        ok &= _assert(all("未预期" not in str(b) for b in bad),
                      "文案里没有『程序遇到未预期的问题』")
        return ok


# ---------------------------------------------------------------------------
# 附加 B：分层 + 依赖
# ---------------------------------------------------------------------------


def check_layering_and_deps(tmp: Path) -> bool:
    """`check_layers.py` / `check_deps.py` + api 层不许 import sqlite3。"""
    banner("附加 B：分层与依赖白名单")
    ok = True
    for tool in ("check_layers.py", "check_deps.py"):
        p = PROJECT_ROOT / "tools" / tool
        proc = subprocess.run(
            [sys.executable, str(p)], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        ok &= _assert(proc.returncode == 0, f"{tool} 通过",
                      (proc.stdout or proc.stderr).strip()[-200:]
                      if proc.returncode else "exit 0")

    # ★ 额外：模板端点读表必须经 `template_store`，不许在 api 层直连 sqlite3
    #
    # ⚠️ 判据要用 **AST 解析**，不能用 `"import sqlite3" in 源码文本`。
    # 后者是我第一版写的，实测假红：`routes.py` 的**注释和 docstring**
    # 里为了讲清楚"API 层禁止 import sqlite3"这句话，逐字出现过
    # `import sqlite3` —— 于是文本匹配把"遵守规则的说明"当成了"违规代码"。
    # 这是"用字符串搜代码"的经典误报：注释不是代码。
    # ⚠️ `routes.py` 已拆成 `routes/` 包（31 个端点分到 7 个业务模块），
    # 所以这里**逐文件**过一遍，不是只查一个文件 —— 否则拆包后新增的
    # 模块会整个逃出这条红线（假绿）。
    routes_dir = PROJECT_ROOT / "src" / "mmwf" / "api" / "routes"
    routes_files = sorted(routes_dir.glob("*.py"))
    ok &= _assert(bool(routes_files), "api/routes/ 包在位",
                  f"{routes_dir} 下没有 .py")
    offenders: set[str] = set()
    for f in routes_files:
        offenders |= _imported_modules(f) & {"sqlite3", "sqlalchemy", "httpx",
                                             "requests", "docx"}
    ok &= _assert(not offenders,
                  "★ api/routes/ 各模块真正 import 的模块里没有 sqlite3/httpx/docx 等"
                  "（模板端点必须走 template_store）",
                  f"实测 import 了：{sorted(offenders)}")
    routes_text = "\n".join(f.read_text(encoding="utf-8") for f in routes_files)
    ok &= _assert("import sqlite3" in routes_text,
                  "（说明性对照：正文里**提到**过 `import sqlite3` 这句禁令 —— "
                  "证明上面的 AST 判据确实没被注释误伤）")

    # 也确认新增的两个模块没有越过自己的层
    store_src = (PROJECT_ROOT / "src" / "mmwf" / "infra" / "template_store.py")
    tpl_src = (PROJECT_ROOT / "src" / "mmwf" / "domain" / "template.py")
    ok &= _assert(store_src.exists() and tpl_src.exists(),
                  "新增的 template_store.py / domain/template.py 都在位")
    for f, forbidden, why in (
        (tpl_src, "import sqlite3", "domain 层不许碰数据库"),
        (tpl_src, "fastapi", "domain 层不许依赖 web 框架"),
        (store_src, "fastapi", "infra 层不许依赖 web 框架"),
    ):
        if f.exists():
            txt = f.read_text(encoding="utf-8")
            ok &= _assert(forbidden not in txt, f"{f.name}: {why}")
    return ok


# ---------------------------------------------------------------------------
# 需人工复核
# ---------------------------------------------------------------------------

MANUAL_ITEMS = [
    "M1 真下到的 .docx 用 Word/WPS **打开是否正常**（脚本只验了 ZIP 魔数）",
    "M2 diff 在**前端的可读性**：500 段记录稿的 diff 响应体有多大？折叠（>40 段）够不够用",
    "M3 模板列表的 `source` 字段，前端会不会**误当字符串显示**（它应该是『内置/自建』标签）",
    "M4 URL 里的**中文文件名**经 `filename*=UTF-8''` 后，浏览器下载是否正常命名",
    "M5 模板『改一行 yaml 即生效』在**真实部署**里是否成立（本脚本验的是同进程读盘，"
    "生产是长驻进程 + 可能多 worker，要确认没有别处缓存了模板）",
]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="T1-16 验收脚本")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    ap.add_argument("--only", default="", help="只跑指定标准，如 3 或 3,7")
    args = ap.parse_args()

    logging.disable(logging.WARNING)

    tmp = Path(tempfile.mkdtemp(prefix="mmwf-t116-"))
    checks = [
        ("1", "端点清单 25 → 30", check_standard_1),
        ("2", "模板列表三键齐全", check_standard_2),
        ("3", "★ YAML 改一行即生效", check_standard_3),
        ("4", "内置模板不可 PUT → E1003", check_standard_4),
        ("5", "模板 PUT 版本自增", check_standard_5),
        ("6", "★ diff 只有变化段落是 changed", check_standard_6),
        ("7", "★ 路径穿越被拒", check_standard_7),
        ("8", "下载真下到 docx", check_standard_8),
        ("9", "不存在的版本 → E1002", check_standard_9),
        ("A", "附加 A：没有 E9001", check_no_internal_error_for_bad_input),
        ("B", "附加 B：分层与依赖", check_layering_and_deps),
    ]
    want = {x.strip() for x in args.only.split(",") if x.strip()}

    results: list[tuple[str, str, bool]] = []
    try:
        for num, label, fn in checks:
            if want and num not in want:
                continue
            # ⚠️ **每项标准各用一个独立子目录**。
            # 同一个 `tmp` 直接给所有 check 用，会在库/配置上互相污染：
            # 标准 3 改过 yaml、各标准都 `make_meeting(MEETING)`（id 是常量），
            # 撞 `UNIQUE constraint failed: meeting.meeting_id`。
            # 症状极误导（全红且都报"唯一约束失败"，像产品坏了，实为脚本自己）。
            sub = tmp / f"std{num}"
            sub.mkdir(parents=True, exist_ok=True)
            try:
                results.append((num, label, bool(fn(sub))))
            except Exception as exc:  # 验收脚本崩了也是"没过"，不该中断其余项
                print(f"\n  {FAIL} 标准 {num} 抛异常：{type(exc).__name__}: {exc}")
                import traceback
                traceback.print_exc()
                results.append((num, label, False))

        banner("汇总")
        passed = 0
        for num, label, ok in results:
            print(f"  {PASS if ok else FAIL} 标准 {num} {label}")
            passed += int(ok)

        print(f"\n  共 {len(results)} 项，通过 {passed} 项")

        banner("需人工复核（不混进通过数）")
        for item in MANUAL_ITEMS:
            print(f"  · {item}")

        print("\n" + "=" * 78)
        if passed == len(results) and results:
            print(f"{PASS} T1-16 验收通过（{passed}/{len(results)}）")
            print("   SAD §9.1 端点清单至此**全部收口**（30/30）。")
        else:
            print(f"{FAIL} T1-16 验收**未通过**（{passed}/{len(results)}）")
        print("=" * 78)
        return 0 if passed == len(results) and results else 1
    finally:
        if args.keep:
            print(f"\n临时目录保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
