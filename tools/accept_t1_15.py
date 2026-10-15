"""T1-15 验收脚本（人可读版）。

施工卡 T1-15 的九条验收标准逐条跑一遍。这张卡补的是
SAD §9.1 里与「语音稿」直接相关的 4 个端点：

  ① GET   /meetings/{id}/transcript      取转写分段（分页 + 复核进度）
  ② PATCH /segments/{seg_id}             就地编辑某段
  ③ POST  /meetings/{id}/review/accept   批量接受高置信分段
  ④ POST  /meetings/{id}/audio           追加音频文件

端点总数 **21 → 25**。

九条标准（施工卡原文）
----------------------
  [ ] 1  端点清单 21 → 25（从真实路由器反查）
  [ ] 2  `GET /transcript` 分页正确（limit+offset，`seq` 连续、`has_more` 判据对）
  [ ] 3  `GET /transcript` 截断**必须告知**（`truncated` + message 报总数）
  [ ] 4  `GET /transcript` 复核计数正确，`no_confidence` **单独在**
  [ ] 5  `PATCH /segments/{id}` 真改动 + `segment_edit` 审计（detail 不含正文）
  [ ] 6  `PATCH` 无改动**不是错误**（200 + `changed=false` + warnings）
  [ ] 7  `PATCH` 不存在的段 → **E1002**（不是 E9001）
  [ ] 8  `review/accept` 只接受「高分**且**无异常标记」的分段
  [ ] 9  `POST audio` 走真接入流程；空 `audio_paths` → E1001

附加 A：任何用户填错都不许变成 E9001
附加 B：分层与依赖检查

⚠️ 这张卡为什么容易"假绿"
-------------------------
九条里前八条都能靠"happy path 跑通"糊弄过去。所以本脚本在四处做
**反向验证**（与 T1-13/T1-14 同一原则：能自动验的自动验，
且要证明**检查本身有效**）：

  · 标准 3 的反向面：只测 `limit=5` 返回 5 条是不够的 —— 一个
    **恒不截断**（返回全篇）的实现同样能过。必须断言
    `page.total` 是**全篇**段数、`truncated=true`、且
    message 里**报出了总数**。这三条一起，才排除了"默默少给"和
    "默默全给"两种错误。
  · 标准 5 的反向面：光有"审计多了一条"不够，还要断言
    **detail 里没有正文**（AUD-05 是红线，泄漏原文比不记更糟）。
  · 标准 6 的反向面：无改动必须**同时**是 200、`changed=false`、
    且 `warnings` 非空。"静默返回 200" 和 "报了个错但被吞掉" 都不算过。
  · 标准 7 的反向面（★最重要）：这是本卡**最可能抓到真缺陷**的一条。
    不存在 → E1002 必须**同时**验两件事：错误码对，且
    **文案里说的是"分段"不是"文件"**。实测踩到过：`FileMissingError`
    会吞掉 `user_message`，于是错误码正确（E1002）、文案却是
    「找不到要处理的**文件**」—— 用户会去翻硬盘找文件。

另一条诚实原则：**能自动验的自动验，必须人手的明确列出来**。
四项界面呈现相关的东西（分页文案是否人话、`page.total` 会不会被前端
用错、accept 的 message 报数是否完整、追加音频的能力边界是否说清）
本脚本**无法自动判**——它们是"文案好不好读"，不是"对不对"。
列进「需人工复核」，**不混进通过数**。

关于环境注意（本机实测，会影响脚本能不能跑通）
---------------------------------------------
  · 主路径一律用进程内 `TestClient`（不起真网络）：本机有
    `http_proxy`，且沙箱会拦截本地 HTTP，走真网络会让结果随环境摇摆。
  · 造分段**走仓储层**（`transcript_store.replace_segments`），
    不经过 API —— 端点测的是"读/改"，造数据不该依赖两个端点互相自证。
  · `POST audio` 的真接入流程用**替身 adapter**（本机没有 ffmpeg，
    而且不该要求有）。替身只实现 `probe_duration_ms` 与 `cut_chunk`。
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

#: 造一个会议用的固定 id（要与 `_seed_segments` 造的分段 id 对得上）
MEETING = "m-t115"


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


class _FakeTools:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"


class _FakeAdapter:
    """ffmpeg 适配器替身：不真跑外部进程，但**走完整个接入流程**。

    ⚠️ 构造函数签名必须与真 `FFmpegAdapter` 相容 ——
    `make_ingest_service` 是按
    `FFmpegAdapter(tools, timeout_seconds=...)` 调它的
    （另一个分支是 `FFmpegAdapter()`）。只写一个无参 `__init__`
    会 `TypeError` → 又被兜底处理器收成 **E9001**。
    这正是"替身与真身签名不一致"造成的假故障。
    """

    tools = _FakeTools()

    def __init__(self, tools=None, *, timeout_seconds: int = 600):
        self.tools = tools or _FakeTools()
        self.timeout_seconds = timeout_seconds
        self._duration_ms = 90_000
        self.cut_calls: list[tuple] = []

    def probe_duration_ms(self, src):
        return self._duration_ms

    def cut_chunk(self, src, dst, start_ms, end_ms):
        self.cut_calls.append((Path(dst).name, start_ms, end_ms))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"chunk")


#: 真适配器的引用，注入替身后要还原（**不能 pop 掉** —— 那会让后面的
#: 标准拿到一个不存在的名字）。
_ORIG_ADAPTER = None


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

    # -- 造数据（走仓储层，不经过 API）------------------------------------

    def make_meeting(self, meeting_id: str = MEETING, *, status: str = "transcript_ready"):
        from mmwf.infra.db import connect_ctx

        with connect_ctx(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meeting (meeting_id, title, classification, status,"
                " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (meeting_id, "T1-15 验收会", "internal", status,
                 "accept", "2026-09-29T00:00:00Z", "2026-09-29T00:00:00Z"),
            )
            conn.commit()
        return meeting_id

    def set_status(self, meeting_id: str, status: str) -> None:
        from mmwf.infra.db import connect_ctx

        with connect_ctx(self.db_path) as conn:
            conn.execute("UPDATE meeting SET status = ? WHERE meeting_id = ?",
                         (status, meeting_id))
            conn.commit()

    def seed_segments(self, meeting_id: str, segments: list[dict]) -> int:
        """用仓储层落分段。id 由 `f"{meeting_id}:{seq:05d}"` 确定性生成。"""
        from mmwf.infra.transcript_store import make_transcript_store

        return make_transcript_store(meeting_id, self.db_path).replace_segments(segments)

    def make_wav(self, name: str = "fake.wav", size: int = 4096) -> Path:
        p = self.cfg.resolve_path(self.cfg.app.data_dir) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF" + b"\x00" * size)
        return p

    # -- 直连库（仅验收脚本用；产品代码在 api/ 层禁止 import sqlite3）-------

    def audit_actions(self) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT action, operator, target_id, detail FROM audit_log ORDER BY log_id"
            ).fetchall()
        finally:
            conn.close()

    def segment_row(self, segment_id: str) -> tuple | None:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT text, flags, reviewed, version FROM transcript_segment"
                " WHERE segment_id = ?", (segment_id,)
            ).fetchone()
        finally:
            conn.close()

    def seg_raw_flags(self, segment_id: str) -> str | None:
        conn = sqlite3.connect(self.db_path)
        try:
            r = conn.execute(
                "SELECT flags FROM transcript_segment WHERE segment_id = ?",
                (segment_id,)
            ).fetchone()
            return r[0] if r else None
        finally:
            conn.close()


def _segs(n: int, *, confidence: float | None = 0.9, start: int = 1,
          text_fmt: str = "第{i}句") -> list[dict]:
    out = []
    for i in range(start, start + n):
        out.append({
            "seq": i,
            "start_ms": i * 1000,
            "end_ms": i * 1000 + 900,
            "text": text_fmt.format(i=i),
            "confidence": confidence,
        })
    return out


def _sa(sid: str) -> str:
    """拼一个分段 id（与仓储层的生成规则一致）。"""
    seq = int(sid.split(":")[-1])
    return f"{MEETING}:{seq:05d}"


def _no_stack(text: str) -> bool:
    """响应体里没有内部堆栈。

    ⚠️ 只禁 `Traceback` / `site-packages` / `File "`，**不禁** `row_hash`
    这类字段名 —— 它们可能是合法的证据内容（T1-14 记下的教训：
    当时断言 `"row_hash" not in raw`，而 integrity.problems 里
    本来就该带 expected/actual 哈希）。
    """
    return ("Traceback" not in text and "site-packages" not in text
            and 'File "' not in text)


# ---------------------------------------------------------------------------
# 标准 1：端点清单 21 → 25
# ---------------------------------------------------------------------------


def check_standard_1(tmp: Path) -> bool:
    """从**真实路由器**反查端点，双向比对 25 条清单。

    ⚠️ 不能用 `app.routes`：本环境的 FastAPI 把 `include_router` 的结果
    包成 `_IncludedRouter`，**它没有 `.path`** —— 遍历得到空集，
    断言会报"清单里的端点全都不在路由表里"（一大串，看不出真漏了哪条）。
    正确做法：`build_router(cfg)` 拿原始路由器（它**不加前缀**），
    路径自己拼 `API_PREFIX` 再比。
    """
    banner("标准 1：端点清单 21 → 25（从真实路由器反查）")
    from mmwf.api.routes import build_router
    from mmwf.main import API_PREFIX

    cfg = _make_cfg(tmp)
    router = build_router(cfg)

    actual = set()
    for r in router.routes:
        path = getattr(r, "path", None)
        if path is None:
            continue
        for m in getattr(r, "methods", None) or []:
            if m not in {"HEAD", "OPTIONS"}:
                actual.add((m, API_PREFIX + path))

    new_four = {
        ("GET", f"{API_PREFIX}/meetings/{{meeting_id}}/transcript"),
        ("PATCH", f"{API_PREFIX}/segments/{{segment_id}}"),
        ("POST", f"{API_PREFIX}/meetings/{{meeting_id}}/review/accept"),
        ("POST", f"{API_PREFIX}/meetings/{{meeting_id}}/audio"),
    }

    ok = True
    missing = new_four - actual
    ok &= _assert(not missing, "本卡 4 个新端点全部在路由表里",
                  f"缺：{sorted(missing)}" if missing else "4/4")
    # ⚠️ **不要断言端点总数**（原为 `len(actual) == 25`，已删）。
    #
    # 这条断言是**快照**，T1-15 当时确实等于 25。但 T1-16 补完收尾 5 端点后
    # 变成 31（method+path）—— 于是**上一张卡**的验收就永久红了。
    #
    # 这不是产品回归，是**断言选错了对象**：一张卡该守的是
    # "**本卡新增的端点存在**"（上面那条 `new_four` 已经守了），
    # 而不是"全局总数永远是我交付那天的数字"。
    # 总数是**全项目水位**，会随任何后续卡变动；把它钉在单卡验收里，
    # 等于让每张新卡都去改一遍所有旧卡的脚本 —— 那是纯摩擦。
    #
    # 全局水位由 `tests/unit/test_api.py::test_protected_endpoint_list_matches_router`
    # 守（它每次都从真实路由器反查 + 逐条点名核对 SAD §9.1），
    # 那里才是唯一该有总数断言的地方。
    #
    # 保留下面这条**性质**断言（与总数无关，永远成立）：
    ok &= _assert(
        all(m not in {"HEAD", "OPTIONS"} for m, _ in actual),
        "清单只统计真实业务方法（HEAD/OPTIONS 是框架自带，不算）",
    )
    return ok


# ---------------------------------------------------------------------------
# 标准 2：分页正确
# ---------------------------------------------------------------------------


def check_standard_2(tmp: Path) -> bool:
    """分页数学：`seq` 连续、`has_more` 判据正确、末页不报截断。"""
    banner("标准 2：GET /transcript 分页正确")
    with Session(tmp) as s:
        s.make_meeting()
        s.seed_segments(MEETING, _segs(12))
        ok = True

        first = s.ok("GET", f"/meetings/{MEETING}/transcript",
                     params={"limit": 5, "offset": 0})
        ok &= _assert([x["seq"] for x in first["segments"]] == [1, 2, 3, 4, 5],
                      "offset=0 limit=5 → seq 1..5",
                      f"实测 {[x['seq'] for x in first['segments']]}")
        ok &= _assert(first["page"]["returned"] == 5, "page.returned = 5")
        ok &= _assert(first["page"]["has_more"] is True, "has_more = true")

        mid = s.ok("GET", f"/meetings/{MEETING}/transcript",
                   params={"limit": 5, "offset": 5})
        ok &= _assert([x["seq"] for x in mid["segments"]] == [6, 7, 8, 9, 10],
                      "offset=5 limit=5 → seq 6..10",
                      f"实测 {[x['seq'] for x in mid['segments']]}")
        ok &= _assert(mid["page"]["has_more"] is True, "中页 has_more = true")

        last = s.ok("GET", f"/meetings/{MEETING}/transcript",
                    params={"limit": 5, "offset": 10})
        ok &= _assert([x["seq"] for x in last["segments"]] == [11, 12],
                      "offset=10 limit=5 → seq 11..12（只剩 2 条）",
                      f"实测 {[x['seq'] for x in last['segments']]}")
        ok &= _assert(last["page"]["returned"] == 2, "末页 returned = 2（不是 5）")
        ok &= _assert(last["page"]["has_more"] is False, "末页 has_more = false")
        ok &= _assert(last["truncated"] is False, "末页 truncated = false")

        # 按 seq 区间过滤
        rng = s.ok("GET", f"/meetings/{MEETING}/transcript",
                   params={"min_seq": 4, "max_seq": 6})
        ok &= _assert([x["seq"] for x in rng["segments"]] == [4, 5, 6],
                      "min_seq/max_seq 区间过滤",
                      f"实测 {[x['seq'] for x in rng['segments']]}")

        # 升序（不是默认的插入序）
        allb = s.ok("GET", f"/meetings/{MEETING}/transcript")
        seqs = [x["seq"] for x in allb["segments"]]
        ok &= _assert(seqs == sorted(seqs), "分段按 seq 升序返回")
        return ok


# ---------------------------------------------------------------------------
# 标准 3：截断必须告知
# ---------------------------------------------------------------------------


def check_standard_3(tmp: Path) -> bool:
    """★ 反向验证：**默默少给**和**默默全给**两种错都要排除。

    一个"永远返回全篇"的实现能过"limit=5 返回了 5 条"这种浅测，
    但它没有分页；一个"永远返回一页但不报总数"的实现更糟 ——
    用户以为看完了，永远不会去翻下一页。

    所以要**三条一起**断：`page.total` = 全篇段数、`truncated=true`、
    且 message 里同时出现总数与未显示数。
    """
    banner("标准 3：GET /transcript 截断必须告知")
    with Session(tmp) as s:
        s.make_meeting()
        s.seed_segments(MEETING, _segs(12))
        ok = True

        body = s.req("GET", f"/meetings/{MEETING}/transcript", params={"limit": 5})
        data = body["data"]

        ok &= _assert(data["page"]["total"] == 12,
                      "★ page.total = 12（**全篇**段数，不是本页的 5）—— "
                      "写成 len(segments) 会让前端算出 has_more=false",
                      f"实测 {data['page']['total']}")
        ok &= _assert(data["truncated"] is True, "truncated = true（显式给的布尔）")
        ok &= _assert(len(data["segments"]) == data["page"]["returned"],
                      "segments 长度与 page.returned 自洽")

        # 服务端自己算好并**写在文案里** —— 不让前端自己算
        ok &= _assert("12" in body["message"],
                      "★ message 里报出**总数** 12（用户看不到正文也得知道有多少）",
                      f"message={body['message']!r}")
        ok &= _assert("7" in body["message"],
                      "★ message 里报出**未显示数** 7",
                      f"message={body['message']!r}")

        # 不截断时不许谎报
        full = s.req("GET", f"/meetings/{MEETING}/transcript", params={"limit": 100})
        ok &= _assert(full["data"]["truncated"] is False,
                      "给足 limit 时 truncated = false（不谎报截断）")
        ok &= _assert("12" in full["message"], "不截断时 message 也报总数",
                      f"message={full['message']!r}")
        return ok


# ---------------------------------------------------------------------------
# 标准 4：复核计数
# ---------------------------------------------------------------------------


def check_standard_4(tmp: Path) -> bool:
    """`no_confidence` **必须单列**，不能并进 `low_confidence`。"""
    banner("标准 4：GET /transcript 复核计数正确")
    with Session(tmp) as s:
        s.make_meeting()
        segs = []
        for i in (1, 2, 3):                       # 3 段低置信
            segs.append({"seq": i, "start_ms": i, "end_ms": i,
                         "text": f"低{i}", "confidence": 0.4})
        segs.append({"seq": 4, "start_ms": 4, "end_ms": 4,
                     "text": "静音", "confidence": 0.98, "flags": ["silence"]})
        for i in (5, 6):                          # 2 段无置信
            segs.append({"seq": i, "start_ms": i, "end_ms": i,
                         "text": f"无{i}", "confidence": None})
        for i in (7, 8):                          # 2 段正常高分
            segs.append({"seq": i, "start_ms": i, "end_ms": i,
                         "text": f"正{i}", "confidence": 0.95})
        s.seed_segments(MEETING, segs)

        data = s.ok("GET", f"/meetings/{MEETING}/transcript")
        c = data["counts"]
        ok = True

        ok &= _assert(set(c) == {"total", "reviewed", "pending",
                                 "low_confidence", "no_confidence"},
                      "counts 的 5 个键与仓储层一致（不改名、不合并）",
                      f"实测 {sorted(c)}")
        ok &= _assert(c["total"] == 8, "total = 8", f"实测 {c['total']}")
        ok &= _assert(c["low_confidence"] == 3,
                      "★ low_confidence = 3（**不含**那 2 段无置信的）—— "
                      "把它们并进来会让这个数失去意义",
                      f"实测 {c['low_confidence']}")
        ok &= _assert(c["no_confidence"] == 2,
                      "★ no_confidence = 2（单独一栏：引擎没给分，必须全人工过）",
                      f"实测 {c['no_confidence']}")
        ok &= _assert(c["reviewed"] == 0 and c["pending"] == 8,
                      "未复核时 reviewed=0 / pending=total",
                      f"reviewed={c['reviewed']} pending={c['pending']}")

        # `review_flags` 把"低分"与"被标 silence"合并成"要不要看"的视图
        by_id = {x["seq"]: x for x in data["segments"]}
        ok &= _assert("low_conf" in by_id[1]["review_flags"],
                      "低分段 review_flags 含 low_conf")
        ok &= _assert("silence" in by_id[4]["review_flags"],
                      "★ 高分但被标 silence 的段**也要**进人工（分数发现不了）",
                      f"实测 {by_id[4]['review_flags']}")
        ok &= _assert(by_id[7]["review_flags"] == [],
                      "正常高分段的 review_flags 为空",
                      f"实测 {by_id[7]['review_flags']}")
        return ok


# ---------------------------------------------------------------------------
# 标准 5：PATCH 真改动 + 审计
# ---------------------------------------------------------------------------


def check_standard_5(tmp: Path) -> bool:
    """★ 反向验证：光"审计多一条"不够，还要 **detail 里没有正文**。

    AUD-05 是红线：审计 detail **不含转写稿原文**。
    泄漏原文比不记更糟 —— 审计日志是长期留存、可被导出的。
    """
    banner("标准 5：PATCH /segments 真改动 + segment_edit 审计（detail 不含正文）")
    with Session(tmp) as s:
        s.make_meeting()
        s.seed_segments(MEETING, _segs(3, text_fmt="{i}"))
        sid = _sa(f"{MEETING}:00002")
        ok = True

        before = s.segment_row(sid)
        ok &= _assert(before is not None, "目标分段存在", f"flags={before[1]!r}")

        secret = "这一整句都不该出现在审计里"
        data = s.ok("PATCH", f"/segments/{sid}",
                    json={"text": secret, "operator": "张三"})

        ok &= _assert(data["changed"] is True, "changed = true")
        ok &= _assert(data["before_text"] == "2",
                      "before_text 回的是**原文**",
                      f"实测 {data['before_text']!r}")
        ok &= _assert(data["segment"]["text"] == secret, "正文已改")
        ok &= _assert(data["segment"]["version"] == 2,
                      "version 从 1 → 2（版本必须自增，否则无法分辨改过几次）",
                      f"实测 {data['segment']['version']}")
        ok &= _assert(data["segment"]["reviewed"] is True, "改完自动标记 reviewed")
        ok &= _assert(data["segment"]["reviewed_by"] == "张三", "reviewed_by 是操作者")

        rows = s.audit_actions()
        edits = [r for r in rows if r[0] == "segment_edit"]
        ok &= _assert(len(edits) == 1, "★ 审计多**一条** segment_edit（恰好一条）",
                      f"实测 {len(edits)} 条")

        if edits:
            action, operator, target_id, detail = edits[0]
            raw_detail = detail if isinstance(detail, str) else json.dumps(detail)
            ok &= _assert(operator == "张三", "审计记下了操作者",
                          f"实测 {operator!r}")
            ok &= _assert(str(target_id) in (sid, MEETING),
                          "审计 target 指向这次操作",
                          f"实测 {target_id!r}")
            ok &= _assert(secret not in raw_detail,
                          "★ AUD-05：审计 detail **不含转写稿正文**",
                          f"detail={raw_detail[:120]}")
            ok &= _assert("after_len" in raw_detail and "before_len" in raw_detail,
                          "detail 只记长度差（可追溯但不泄漏）",
                          f"detail={raw_detail[:120]}")
        return ok


# ---------------------------------------------------------------------------
# 标准 6：无改动不报错
# ---------------------------------------------------------------------------


def check_standard_6(tmp: Path) -> bool:
    """★ 反向验证：必须**同时**是 200 / `changed=false` / `warnings` 非空。

    "静默返回 200"（warnings 空）算不算过？不算 ——
    用户点了保存、系统什么都没说，他会怀疑"到底存进去了没有"。
    而"报了个错但被吞掉"（200 但 changed 字段缺失）更糟。
    """
    banner("标准 6：PATCH 无改动不是错误（200 + changed=false + warnings）")
    with Session(tmp) as s:
        s.make_meeting()
        s.seed_segments(MEETING, _segs(2, text_fmt="{i}"))
        sid = _sa(f"{MEETING}:00001")
        ok = True

        body = s.req("PATCH", f"/segments/{sid}", json={"text": "1"})
        ok &= _assert(body["ok"] is True,
                      "★ 无改动仍是 ok=true（用户点开看一眼就保存是极常见的操作）",
                      f"ok={body.get('ok')} code={body.get('code')}")
        d = body["data"]
        ok &= _assert(d["changed"] is False, "changed = false")
        ok &= _assert(bool(d.get("warnings")),
                      "★ warnings **非空**（要告诉用户『仍记了一次校订』，"
                      "静默 200 会让人怀疑没存进去）",
                      f"warnings={d.get('warnings')}")

        # 但"无改动"**仍然**是一次校订 → 仍写审计（如实记录"用户又保存了一次"）
        edits = [r for r in s.audit_actions() if r[0] == "segment_edit"]
        ok &= _assert(len(edits) == 1,
                      "无改动**也**写审计（如实记录这次保存动作）",
                      f"实测 {len(edits)} 条")
        return ok


# ---------------------------------------------------------------------------
# 标准 7：不存在的段 → E1002（不是 E9001）
# ---------------------------------------------------------------------------


def check_standard_7(tmp: Path) -> bool:
    """★★ **本卡最重要的一条**（最容易抓到真缺陷）。

    必须**同时**验两件事：
      ① 错误码是 E1002（不是 E9001）；
      ② **文案说的是"分段"，不是"文件"**。

    实测踩到过：`FileMissingError.__init__(self, detail="", **ctx)`
    把 `user_message` 也吞进 `context`，于是错误码正确（E1002）、
    文案却是 E1002 的默认那句「找不到要处理的**文件**。
    请确认文件没有被移动或删除。」—— 用户会去翻硬盘找文件，
    而这里根本就没有文件。
    """
    banner("标准 7：PATCH 不存在的段 → E1002（不是 E9001）")
    with Session(tmp) as s:
        s.make_meeting()
        s.seed_segments(MEETING, _segs(2))
        ok = True

        raw = s.raw("PATCH", f"/segments/{MEETING}:99999", json={"text": "x"})
        body = json.loads(raw)
        ok &= _assert(body["ok"] is False, "ok = false")
        ok &= _assert(body["code"] == "E1002",
                      "★ 错误码 = E1002（`KeyError` 不接住会掉进兜底处理器 → E9001）",
                      f"实测 {body['code']}：{body.get('message')}")
        ok &= _assert(body["code"] != "E9001", "**不是** E9001")
        ok &= _assert(_no_stack(raw), "响应体里没有堆栈/内部路径")
        ok &= _assert("分段" in body["message"] or "转写稿" in body["message"],
                      "★ 文案说的是『分段 / 转写稿』而不是『文件』—— "
                      "否则用户会去翻硬盘找文件（E1002 的默认文案就是这句）",
                      f"message={body['message']!r}")

        # 会议不存在（前缀与上面同码同文案要区分）也一并验
        raw2 = s.raw("PATCH", "/segments/m-nope:00001", json={"text": "x"})
        ok &= _assert(json.loads(raw2)["code"] == "E1002",
                      "别的会议下不存在的段同样 E1002")
        ok &= _assert(_no_stack(raw2), "响应体里没有堆栈")
        return ok


# ---------------------------------------------------------------------------
# 标准 8：批量接受的两条判据
# ---------------------------------------------------------------------------


def check_standard_8(tmp: Path) -> bool:
    """★ 两条判据都要满足 —— 只看分数会让"两人抢话"的高分废数据被放过。"""
    banner("标准 8：review/accept 只接受「高分且无异常标记」")
    with Session(tmp) as s:
        s.make_meeting()
        segs = []
        for i in (1, 2, 3):                       # 高分无标记 → 接受
            segs.append({"seq": i, "start_ms": i, "end_ms": i,
                         "text": f"高{i}", "confidence": 0.92})
        segs.append({"seq": 4, "start_ms": 4, "end_ms": 4,
                     "text": "低分", "confidence": 0.4})
        segs.append({"seq": 5, "start_ms": 5, "end_ms": 5,
                     "text": "抢话", "confidence": 0.98, "flags": ["overlap"]})
        segs.append({"seq": 6, "start_ms": 6, "end_ms": 6,
                     "text": "手动改过的", "confidence": 0.95})
        segs.append({"seq": 7, "start_ms": 7, "end_ms": 7,
                     "text": "高7", "confidence": 0.95})
        s.seed_segments(MEETING, segs)

        # 先把 seq=6 改成已复核 → 它应该被 skip_reviewed 挡下
        #
        # ⚠️ 本项第一版把期望写成 accepted=3，实际是 4 —— **脚本错了，
        # 不是产品错了**。原因是只数了"高分无标记"的 seq 1/2/3，
        # **漏了 seq=7**（它同样是高分无标记、且没被手动改过）。
        # 这类"造数据时漏数一条"的错，症状是断言值差 1，
        # 很容易被当成产品的边界 bug 去改产品。教训：
        # **造完数据先 `counts.total` 对一遍总数**，再算期望。
        s.ok("PATCH", f"/segments/{_sa(f'{MEETING}:00006')}",
             json={"text": "手动改过的"})

        ok = True
        total_expected = 7
        got = s.ok("GET", f"/meetings/{MEETING}/transcript")["counts"]["total"]
        ok &= _assert(got == total_expected,
                      f"造数据的总数对得上（{total_expected} 段）—— "
                      "先对总数再算期望，免得漏数一条导致期望差 1",
                      f"实测 {got}")

        data = s.ok("POST", f"/meetings/{MEETING}/review/accept",
                    json={"operator": "张三"})
        ok &= _assert(data["accepted"] == 4,
                      "★ accepted = 4（seq 1/2/3/7 —— 高分且无标记、且未复核的）",
                      f"实测 {data['accepted']}；ids={data['segment_ids']}")
        ok &= _assert(data["skipped_low_confidence"] == 2,
                      "★ skipped_low_confidence = 2 —— 低分 1 段 **+ 高分但带 "
                      "overlap 的 1 段**（第二条判据的价值就在这里）",
                      f"实测 {data['skipped_low_confidence']}")
        ok &= _assert(data["skipped_reviewed"] == 1,
                      "skipped_reviewed = 1（seq=6 此前已复核，不重复处理）",
                      f"实测 {data['skipped_reviewed']}")
        ok &= _assert(data["accepted"] + data["skipped_reviewed"]
                      + data["skipped_low_confidence"] == total_expected,
                      f"★ 三个数合起来 = 全量 {total_expected} 段（只报第一个数会让人"
                      "以为系统漏处理了）",
                      f"{data['accepted']}+{data['skipped_reviewed']}"
                      f"+{data['skipped_low_confidence']}")
        ok &= _assert(f"{MEETING}:00005" not in data["segment_ids"],
                      "★ 高分但带 overlap 的那段**没有被接受**"
                      "（分数发现不了『两人抢话』，只能靠 flags）")

        # 真的落库了
        counts = s.ok("GET", f"/meetings/{MEETING}/transcript")["counts"]
        ok &= _assert(counts["reviewed"] == 5,
                      "复核数 = 4（批量接受）+ 1（之前手动改的）= 5",
                      f"实测 {counts['reviewed']}")
        ok &= _assert(counts["pending"] == 2, "还剩 2 段待人工（低分 + 抢话）",
                      f"实测 {counts['pending']}")

        # ★ 只写**一条**审计（300 段写 300 条会把审计冲垮）
        accepts = [r for r in s.audit_actions()
                   if r[0] == "segment_edit"
                   and isinstance(r[3], str) and "accept_high_confidence" in r[3]]
        ok &= _assert(len(accepts) == 1,
                      "★ 批量接受只写**一条**审计（不是每段一条）",
                      f"实测 {len(accepts)} 条")

        # threshold 越界必须拦（threshold=0 会让整篇被接受）
        for bad in (0, -1, 1.5):
            b2 = s.req("POST", f"/meetings/{MEETING}/review/accept",
                       json={"threshold": bad})
            ok &= _assert(b2["code"] == "E1001",
                          f"threshold={bad} → E1001（0 会让整篇被放过）",
                          f"实测 {b2['code']}")
        return ok


# ---------------------------------------------------------------------------
# 标准 9：追加音频走真流程
# ---------------------------------------------------------------------------


def check_standard_9(tmp: Path) -> bool:
    """真跑一次接入流程（替身 adapter），并验空入参被拦。"""
    banner("标准 9：POST /audio 走真接入流程 + 空 audio_paths → E1001")
    import mmwf.app.services.ingest as ingest_mod

    with Session(tmp) as s:
        s.make_meeting()
        wav = s.make_wav("后半场.wav")
        ok = True

        # -- 空入参 --
        b = s.req("POST", f"/meetings/{MEETING}/audio", json={"audio_paths": []})
        ok &= _assert(b["code"] == "E1001",
                      "空 audio_paths → E1001（整个调用错了，不是批量失败）",
                      f"实测 {b['code']}：{b.get('message')}")
        ok &= _assert(b["code"] != "E9001", "**不是** E9001")
        ok &= _assert(_no_stack(s.raw("POST", f"/meetings/{MEETING}/audio",
                                      json={"audio_paths": []})),
                      "响应体里没有堆栈")

        b2 = s.req("POST", f"/meetings/{MEETING}/audio", json={})
        ok &= _assert(b2["code"] == "E1001", "缺 audio_paths 键 → E1001",
                      f"实测 {b2['code']}")

        # -- 真流程（替身 adapter）--
        #
        # ⚠️ **注入点在 `make_ingest_service` 内部调用的模块级
        # `FFmpegAdapter`**，不是 `routes._ingest_service` ——
        # 后者是 `build_router` 里的**闭包**，不是模块属性，
        # `monkeypatch.setattr(R, "_ingest_service", ...)` 会报
        # `AttributeError: module 'mmwf.api.routes' has no attribute
        # '_ingest_service'`。
        #
        # ⚠️ **还要绕过 `locate()`**：本机没有 ffmpeg，
        # `mmwf.adapters.audio.ffmpeg.locate()` 会抛
        # `FFmpegNotFoundError`（那是环境问题不是业务错误，
        # 且它在构造 adapter 之前就抛了）。所以 `locate` 也要换掉。
        #
        # 走模块级 `FFmpegAdapter` 的好处：**端点代码一行都不用改**，
        # 测的是真接线（路由 → service → adapter）。
        import mmwf.adapters.audio.ffmpeg as ffmpeg_mod

        adapter = _FakeAdapter()
        global _ORIG_ADAPTER
        if _ORIG_ADAPTER is None:
            _ORIG_ADAPTER = ingest_mod.FFmpegAdapter
        orig_locate = ffmpeg_mod.locate
        ingest_mod.FFmpegAdapter = lambda *a, **k: adapter  # type: ignore[misc]
        ffmpeg_mod.locate = lambda **k: _FakeTools()  # type: ignore[assignment]
        try:
            body = s.req("POST", f"/meetings/{MEETING}/audio",
                         json={"audio_paths": [str(wav)]})
        finally:
            ingest_mod.FFmpegAdapter = _ORIG_ADAPTER  # type: ignore[misc]
            ffmpeg_mod.locate = orig_locate  # type: ignore[assignment]

        ok &= _assert(body["ok"] is True, "真流程 ok = true",
                      f"{body.get('code')} {body.get('message')}")
        data = body["data"]
        ok &= _assert(data["succeeded"] == 1, "succeeded = 1",
                      f"实测 {data.get('succeeded')}：{data.get('audio')}")
        ok &= _assert(data["failed"] == 0, "failed = 0")
        # 90s / 每块 ~600s → 1 块
        ok &= _assert(data["chunk_count"] >= 1,
                      "chunk_count ≥ 1（真的出了切分计划）",
                      f"实测 {data['chunk_count']}")
        ok &= _assert(len(adapter.cut_calls) == data["chunk_count"],
                      "★ 切分计划**真的执行了**（不是只算了个数就返回）—— "
                      f"替身记录到 {len(adapter.cut_calls)} 次 cut_chunk，"
                      f"与 chunk_count={data['chunk_count']} 一致")
        ok &= _assert("不会自动转写" in body["message"],
                      "★ message 必须写明**能力边界**：追加的音频不会自动转写。"
                      "不写用户会以为已经进转写稿了",
                      f"message={body['message']!r}")

        # -- 全批失败仍回 200（尽力而为语义）--
        bf = s.req("POST", f"/meetings/{MEETING}/audio",
                   json={"audio_paths": ["D:/nope/不存在.mp3"]})
        ok &= _assert(bf["ok"] is True,
                      "★ 全批失败**仍回 200/ok=true** —— 批量入口是『尽力而为』，"
                      "回 4xx 会让前端把『一个文件坏了』显示成『这次操作失败』",
                      f"ok={bf.get('ok')} code={bf.get('code')}")
        ok &= _assert(bf["data"]["failed"] == 1, "逐条失败明细在 data 里",
                      f"实测 failed={bf['data'].get('failed')}")

        # -- 已开跑的会议追加 → 要有提示 --
        s.set_status(MEETING, "G2_awaiting")
        bw = s.req("POST", f"/meetings/{MEETING}/audio",
                   json={"audio_paths": ["D:/nope/不存在.mp3"]})
        ok &= _assert("重新" in str(bw["data"].get("warning", "")),
                      "★ 会议已开跑时追加音频 → 必须提示要重新发起处理",
                      f"warning={bw['data'].get('warning')!r}")

        # -- 刚建的会议**不该**有那条提示（反向）--
        s.set_status(MEETING, "created")
        bn = s.req("POST", f"/meetings/{MEETING}/audio",
                   json={"audio_paths": ["D:/nope/不存在.mp3"]})
        ok &= _assert("重新" not in str(bn["data"].get("warning", "")),
                      "★ 反向：status=created 的会议**不该**有『需要重新发起』提示"
                      "（否则提示就成了噪音，用户会忽略它）",
                      f"warning={bn['data'].get('warning')!r}")
        return ok


# ---------------------------------------------------------------------------
# 附加 A：用户填错不许变成 E9001
# ---------------------------------------------------------------------------


def check_no_internal_error_for_bad_input(tmp: Path) -> bool:
    """★ 附加 A（沿用 T1-14 的做法）。

    E9001 本该留给**真的**系统故障。用户填错一个参数就得到 E9001，
    等于把排查方向指到完全错误的地方，用户拿到的追踪号也无意义。

    T1-14 靠这一组用例抓到两个真缺陷。本卡继续做。
    """
    banner("附加 A：任何用户填错都不许变成 E9001")
    with Session(tmp) as s:
        s.make_meeting()
        s.seed_segments(MEETING, _segs(3))
        sid = _sa(f"{MEETING}:00001")

        cases: list[tuple[str, str, dict | None]] = [
            ("GET", f"/meetings/{MEETING}/transcript", {"limit": "abc"}),
            ("GET", f"/meetings/{MEETING}/transcript", {"limit": -1}),
            ("GET", f"/meetings/{MEETING}/transcript", {"offset": "x"}),
            ("GET", f"/meetings/{MEETING}/transcript", {"min_seq": "abc"}),
            ("GET", f"/meetings/{MEETING}/transcript", {"only_review_needed": "x"}),
            ("PATCH", f"/segments/{sid}", {}),
            ("PATCH", f"/segments/{sid}", {"text": ""}),
            ("PATCH", f"/segments/{sid}", {"text": "   "}),
            ("PATCH", f"/segments/{sid}", {"text": 123}),
            ("PATCH", f"/segments/{sid}", {"text": None}),
            ("PATCH", f"/segments/{sid}", {"text": "x", "flags": "abc"}),
            ("PATCH", f"/segments/{sid}", {"text": "x", "flags": 123}),
            ("PATCH", f"/segments/{sid}", {"text": "x", "flags": {"a": 1}}),
            ("POST", f"/meetings/{MEETING}/review/accept", {"threshold": 0}),
            ("POST", f"/meetings/{MEETING}/review/accept", {"threshold": "abc"}),
            ("POST", f"/meetings/{MEETING}/review/accept", {"threshold": None}),
            ("POST", f"/meetings/{MEETING}/review/accept", {"threshold": True}),
            ("POST", f"/meetings/{MEETING}/review/accept", {"segment_ids": "nope"}),
            ("POST", f"/meetings/{MEETING}/review/accept", {"segment_ids": 5}),
            ("POST", f"/meetings/{MEETING}/audio", {"audio_paths": []}),
            ("POST", f"/meetings/{MEETING}/audio", {"audio_paths": "x"}),
            ("POST", f"/meetings/{MEETING}/audio", {"audio_paths": [1, 2]}),
            ("POST", f"/meetings/{MEETING}/audio", {"audio_paths": ["  "]}),
        ]

        ok = True
        e9001: list[str] = []
        for method, path, payload in cases:
            r = s.client.request(
                method, API + path,
                headers={"X-MMWF-Token": s.token},
                **({"params": payload} if method == "GET" else {"json": payload}),
            )
            body = r.json()
            if body.get("code") == "E9001" or r.status_code == 500:
                e9001.append(f"{method} {path} {payload} → "
                             f"{body.get('code')}/{r.status_code}：{body.get('message')}")
            if not _no_stack(r.text):
                e9001.append(f"{method} {path} {payload} → 响应含内部堆栈")
            # 业务失败一律 200（既有约定）+ 信封齐全
            if r.status_code == 200:
                if set(body) != {"ok", "code", "message", "data", "trace_id"}:
                    e9001.append(f"{method} {path} {payload} → 信封形状不对："
                                 f"{sorted(body)}")

        ok &= _assert(not e9001,
                      f"{len(cases)} 组畸形输入，**没有一组**变成 E9001 / 500 / 带堆栈",
                      "\n      ".join(e9001) if e9001 else "全部走了业务错误码")
        return ok


# ---------------------------------------------------------------------------
# 附加 B：分层与依赖
# ---------------------------------------------------------------------------


def check_layering_and_deps(tmp: Path) -> bool:
    """附加 B：`check_layers.py` / `check_deps.py` 都要过。

    ⚠️ 本卡最容易撞的一条：**API 层禁止 `import sqlite3`**。
    4 个端点里 3 个是纯读写 `transcript_segment`，如果直接在端点里
    写 SQL 就会红。这正是 T1-14 单列 `transcript_store.py` 的原因。
    """
    banner("附加 B：分层与依赖检查")
    import subprocess

    ok = True
    for script, label in (("check_layers.py", "分层依赖"),
                          ("check_deps.py", "依赖白名单")):
        p = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / script)],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(PROJECT_ROOT),
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        ok &= _assert(p.returncode == 0, f"{label}检查通过",
                      out.splitlines()[-1] if out else f"退出码 {p.returncode}")
    return ok


# ---------------------------------------------------------------------------
# 需人工复核
# ---------------------------------------------------------------------------

MANUAL_ITEMS = [
    "M1 分页文案是否人话 —— 打开 /docs 调 limit=2，看 message 是不是"
    "「本次返回 2 段（全篇共 12 段），还有 10 段未显示」这种可读的话",
    "M2 `page.total` 前端会不会用错 —— 通读响应字段名，确认 page.total"
    "（全篇）与 page.returned（本页）不会被搞混",
    "M3 accept 的 message 报数是否完整 —— 三个数（接受 / 已复核 / 保留人工）"
    "都出现了吗？只看一个数会让人以为系统漏处理了",
    "M4 追加音频的能力边界是否说清 —— 响应 message 里有「不会自动转写」"
    "这句吗？没有的话用户会以为追加完就进了转写稿",
]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="T1-15 验收脚本")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    ap.add_argument("--only", default="", help="只跑指定标准，如 3 或 3,7")
    args = ap.parse_args()

    logging.disable(logging.WARNING)

    tmp = Path(tempfile.mkdtemp(prefix="mmwf-t115-"))
    checks = [
        ("1", "端点清单 21 → 25", check_standard_1),
        ("2", "分页正确", check_standard_2),
        ("3", "截断必告知", check_standard_3),
        ("4", "复核计数", check_standard_4),
        ("5", "PATCH 真改动 + 审计", check_standard_5),
        ("6", "PATCH 无改动不报错", check_standard_6),
        ("7", "不存在的段 → E1002", check_standard_7),
        ("8", "批量接受两条判据", check_standard_8),
        ("9", "追加音频真流程", check_standard_9),
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
            #
            # 第一版把同一个 `tmp` 直接传给所有 check —— 而 `Session`
            # 以 `tmp/"data"/"mmwf.db"` 为库，于是第 2 项开始就撞
            # `UNIQUE constraint failed: meeting.meeting_id`
            # （每项都 `make_meeting(MEETING)`，id 是常量）。
            #
            # 症状很误导：8 项全红、每项都报"唯一约束失败"，
            # 看着像产品的造数据逻辑坏了，实际是**脚本自己**把两次
            # 独立会话指到了同一个库上。
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
            print(f"{PASS} T1-15 验收通过（{passed}/{len(results)}）")
        else:
            print(f"{FAIL} T1-15 验收**未通过**（{passed}/{len(results)}）")
        print("=" * 78)
        return 0 if passed == len(results) and results else 1
    finally:
        if args.keep:
            print(f"\n临时目录保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
