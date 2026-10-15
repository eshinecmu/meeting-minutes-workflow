"""差异对比 + 导出下载 端点单测（T1-16）。

★ 本文件最需要注意的一条（写下来免得后人以为测试写漏了）：

    **HTTP 层测不到 `../` 的穿越**。
    Starlette 在**路由之前**就把 URL 路径里的 `..` 规范化掉了
    （`/export/../keystore/master.key` 会被改写成 `/keystore/master.key`），
    所以这种请求**根本走不到**我们端点的 `file` 参数。
    实测结果是 E1002（"找不到会议"）而不是 E1001。

    这**不是漏洞**（没有任何字节流出），但**不能拿它当"穿越已被防住"的证据** ——
    真正该验的是**判据本身**。所以：

      · HTTP 层：验"带目录成分 / 百分号编码的输入都拿不到文件字节"
      · 判据层（`TestTraversalGuard`）：**直接**用恶意 `file` 调端点函数，
        断言 E1001。这才是"防穿越"的真凭据。

    两件都做，才算诚实地验过（施工卡标准 7）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmwf.api import deps as api_deps

API = "/api/v1"
MEETING = "m-t116"

DOCX = b"PK\x03\x04" + b"fake-docx-payload"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_appdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path):
    from mmwf.settings import load_settings

    root = Path(__file__).resolve().parents[2]
    cfg = load_settings(root / "config")
    return cfg.model_copy(
        update={
            "app": cfg.app.model_copy(
                update={
                    "data_dir": tmp_path / "data",
                    "log_dir": tmp_path / "data" / "logs",
                }
            )
        }
    )


@pytest.fixture
def db_path(settings) -> Path:
    from mmwf.infra.migrations.runner import migrate

    p = settings.resolve_path(settings.app.data_dir) / "mmwf.db"
    migrate(p)
    return p


@pytest.fixture
def client(settings, isolated_appdir, db_path) -> TestClient:
    from mmwf.main import create_app

    c = TestClient(
        create_app(settings), client=("127.0.0.1", 51234), raise_server_exceptions=False
    )
    c.headers.update({"X-MMWF-Token": api_deps.create_session_token()})
    return c


@pytest.fixture
def anon_client(settings, isolated_appdir, db_path) -> TestClient:
    from mmwf.main import create_app

    return TestClient(create_app(settings), client=("127.0.0.1", 51234))


@pytest.fixture
def meeting(db_path: Path) -> str:
    from mmwf.infra.db import connect_ctx

    with connect_ctx(db_path) as conn:
        conn.execute(
            "INSERT INTO meeting (meeting_id, title, classification, status,"
            " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (MEETING, "对比下载测试会", "internal", "archived",
             "tester", "2026-09-29T00:00:00Z", "2026-09-29T00:00:00Z"),
        )
        conn.commit()
    return MEETING


@pytest.fixture
def fs(settings, meeting):
    """该会议的 FileStore（用来造记录稿与导出文件）。"""
    from mmwf.infra.filestore import make_filestore

    return make_filestore(meeting, settings.resolve_path(settings.app.data_dir))


def _write_records(fs, versions: dict[int, str]) -> None:
    for v, text in versions.items():
        fs.write_text(f"record/v{v}.md", text, stage="record")


def _write_export(fs, name: str, payload: bytes = DOCX) -> str:
    fs.write(f"export/{name}", payload, stage="export")
    return name


def _export_docx(client, meeting: str, *, version: int | None = None) -> str:
    """走**真端点**导出一份 docx，返回服务端给的 `rel_path`。

    ⚠️ 为什么不在测试里手工往 `export/` 里塞个文件了事：
    `undo` 端点是**从 manifest 里注销条目**（`ExportService.undo_export`），
    光有文件、没有 manifest 记录，`undo` 会报 `FileMissingError`（E1002）。
    撤回这件事的凭据是 **manifest**，不是文件存在与否 ——
    所以必须让服务自己走一遍导出，才能验到真路径。
    """
    body = {} if version is None else {"version": version}
    r = client.post(f"{API}/meetings/{meeting}/export", json=body)
    assert r.status_code == 200, r.text[:300]
    payload = r.json()
    assert payload["ok"] is True, payload
    rel = payload["data"].get("rel_path") or payload["data"].get("path")
    assert rel, payload
    return rel


def _no_500(r) -> None:
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    code = str(body.get("code") or "")
    assert not code.startswith("E9"), f"落到 E9xxx 了：{body}"


# ---------------------------------------------------------------------------
# 受保护
# ---------------------------------------------------------------------------

class TestProtected:
    @pytest.mark.parametrize(
        "path",
        [f"{API}/meetings/m-x/diff", f"{API}/meetings/m-x/export/a.docx"],
    )
    def test_requires_token(self, anon_client, path):
        r = anon_client.get(path)
        assert r.status_code == 200
        assert r.json()["code"] == "E6001"


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

class TestDiff:
    def test_meeting_missing_is_e1002(self, client):
        r = client.get(f"{API}/meetings/nope/diff")
        assert r.json()["code"] == "E1002"
        _no_500(r)

    def test_no_record_is_e1002(self, client, meeting):
        r = client.get(f"{API}/meetings/{meeting}/diff")
        assert r.json()["code"] == "E1002"
        _no_500(r)

    def test_single_version_is_not_an_error(self, client, meeting, fs):
        """★ 只有 v1 → 200 + comparable=false，**不是 E1002**（裁决 D4）。"""
        _write_records(fs, {1: "第一段\n\n第二段"})
        r = client.get(f"{API}/meetings/{meeting}/diff")
        body = r.json()
        assert body["ok"] is True
        d = body["data"]
        assert d["comparable"] is False
        assert d["segments"] == []
        assert "没有" in body["message"]

    def test_two_versions_diff(self, client, meeting, fs):
        _write_records(fs, {
            1: "会议基本情况\n\n会议认为，三季度预算可行。\n\n会议要求，抓好落实。",
            2: "会议基本情况\n\n会议认为，四季度预算可行。\n\n会议要求，抓好落实。",
        })
        d = client.get(f"{API}/meetings/{meeting}/diff").json()["data"]
        assert d["base_version"] == 1 and d["compare_version"] == 2
        assert d["comparable"] is True
        assert d["summary"]["changed"] == 1
        assert d["summary"]["removed"] == 0 and d["summary"]["added"] == 0
        kinds = [s["type"] for s in d["segments"]]
        assert kinds.count("changed") == 1

    def test_unchanged_does_not_contain_changed_text(self, client, meeting, fs):
        """★ 被改动的内容**不许**出现在 unchanged 段里。"""
        _write_records(fs, {
            1: "甲\n\n乙原句\n\n丙",
            2: "甲\n\n乙改句\n\n丙",
        })
        d = client.get(f"{API}/meetings/{meeting}/diff").json()["data"]
        unchanged_blob = "".join(
            line for s in d["segments"] if s["type"] == "unchanged" for line in s["lines"]
        )
        assert "乙原句" not in unchanged_blob
        assert "乙改句" not in unchanged_blob

    def test_added_and_removed(self, client, meeting, fs):
        _write_records(fs, {
            1: "甲\n\n乙",
            2: "乙\n\n丙",
        })
        s = client.get(f"{API}/meetings/{meeting}/diff").json()["data"]["summary"]
        assert s["removed"] >= 1 and s["added"] >= 1

    def test_explicit_versions(self, client, meeting, fs):
        _write_records(fs, {1: "A", 2: "B", 3: "C"})
        d = client.get(f"{API}/meetings/{meeting}/diff?base=1&compare=3").json()["data"]
        assert d["base_version"] == 1 and d["compare_version"] == 3

    def test_missing_compare_version_is_e1002(self, client, meeting, fs):
        _write_records(fs, {1: "A"})
        r = client.get(f"{API}/meetings/{meeting}/diff?compare=99")
        assert r.json()["code"] == "E1002"
        _no_500(r)

    def test_compare_zero_is_e1001(self, client, meeting, fs):
        _write_records(fs, {1: "A"})
        r = client.get(f"{API}/meetings/{meeting}/diff?compare=0")
        assert r.json()["code"] == "E1001"
        _no_500(r)

    def test_same_version_zero_changes(self, client, meeting, fs):
        _write_records(fs, {1: "A\n\nB", 2: "A\n\nB"})
        r = client.get(f"{API}/meetings/{meeting}/diff?base=2&compare=2")
        d = r.json()["data"]
        assert d["summary"]["changed"] == 0

    def test_crlf_no_false_diff(self, client, meeting, fs):
        """★ CRLF 不该造成假差异（换行符统一是 diff 的第一道坎）。"""
        _write_records(fs, {1: "甲\n\n乙", 2: "甲\r\n\r\n乙"})
        d = client.get(f"{API}/meetings/{meeting}/diff").json()["data"]
        assert d["summary"]["changed"] == 0
        assert d["summary"]["added"] == 0 and d["summary"]["removed"] == 0

    def test_long_unchanged_is_folded(self, client, meeting, fs):
        """★ 超长未变区间要折叠（带 `elided`），否则响应 99% 是没变的内容。"""
        base = "\n\n".join(f"第{i}段" for i in range(1, 101))
        v2 = base.replace("第50段", "第50段改了")
        _write_records(fs, {1: base, 2: v2})
        d = client.get(f"{API}/meetings/{meeting}/diff").json()["data"]
        folded = [s for s in d["segments"] if s["type"] == "unchanged" and s.get("elided")]
        assert folded, "100 段的未变区间没有被折叠"
        assert folded[0]["elided"] > 0

    def test_summary_has_four_keys(self, client, meeting, fs):
        _write_records(fs, {1: "A", 2: "B"})
        s = client.get(f"{API}/meetings/{meeting}/diff").json()["data"]["summary"]
        assert set(s) == {"unchanged", "changed", "removed", "added"}


# ---------------------------------------------------------------------------
# 导出下载
# ---------------------------------------------------------------------------

class TestDownload:
    def test_meeting_missing_is_e1002(self, client):
        r = client.get(f"{API}/meetings/nope/export/a.docx")
        assert r.json()["code"] == "E1002"
        _no_500(r)

    def test_download_returns_bytes(self, client, meeting, fs):
        name = _write_export(fs, f"{MEETING}-v1.docx")
        r = client.get(f"{API}/meetings/{meeting}/export/{name}")
        assert r.status_code == 200
        assert r.content[:4] == b"PK\x03\x04", "docx 应是 zip（PK 魔数）"
        assert r.content == DOCX

    def test_docx_media_type(self, client, meeting, fs):
        name = _write_export(fs, f"{MEETING}-v1.docx")
        r = client.get(f"{API}/meetings/{meeting}/export/{name}")
        assert "wordprocessingml" in r.headers["content-type"]

    def test_disposition_has_utf8_fallback(self, client, meeting, fs):
        name = _write_export(fs, f"{MEETING}-v1.docx")
        r = client.get(f"{API}/meetings/{meeting}/export/{name}")
        cd = r.headers["content-disposition"]
        assert "attachment" in cd
        assert "filename*=UTF-8''" in cd

    def test_missing_file_is_e1002(self, client, meeting, fs):
        r = client.get(f"{API}/meetings/{meeting}/export/nope.docx")
        assert r.json()["code"] == "E1002"
        _no_500(r)

    def test_not_found_returns_no_bytes(self, client, meeting, fs):
        r = client.get(f"{API}/meetings/{meeting}/export/nope.docx")
        assert r.content[:2] != b"PK"

    @pytest.mark.parametrize("ext,expect", [
        ("a.docx", "wordprocessingml"),
        ("a.doc", "msword"),
        ("a.pdf", "pdf"),
        ("a.md", "markdown"),
        ("a.json", "json"),
        ("a.bin", "octet-stream"),
    ])
    def test_media_type_mapping(self, client, meeting, fs, ext, expect):
        _write_export(fs, ext, b"payload")
        r = client.get(f"{API}/meetings/{meeting}/export/{ext}")
        if ext.endswith('.pdf'):
            # F5.9：MIME 映射保留，伪名 PDF 不能沿一期记录稿兼容放行。
            from mmwf.api.routes._support import _media_type_for
            assert _media_type_for(ext) == 'application/pdf'
            assert r.json()['code'] == 'E4001'
        else:
            assert expect in r.headers["content-type"], r.headers["content-type"]

    def test_download_writes_audit(self, client, meeting, fs, db_path):
        """★ 下载要留 `export_download` 痕（纪要正文被取走）。"""
        from mmwf.infra.audit import AuditQuery, query

        name = _write_export(fs, f"{MEETING}-v1.docx")
        client.get(f"{API}/meetings/{meeting}/export/{name}")
        recs = query(db_path, AuditQuery(action="export_download"))
        assert len(recs) == 1, f"应有 1 条下载审计，实际 {len(recs)}"
        assert recs[0].detail.get("file") == name

    def test_export_undo_route_not_shadowed(self, client, meeting, fs):
        """★ 坑 14：`/export/undo` 不能被 `{file}` 吃掉。

        判据**不是状态码**（信封约定下业务响应恒为 200 —— `MmwfError`
        的处理器写死 `status_code=200`），而是这两条**性质**：

          ① 不能返回**文件字节流**（真被 `{file}` 吃掉就是在下载一个
             名叫 `undo` 的文件，那是最坏的情况：`undo` 成了可下载对象，
             而下载区里根本没有这个文件，行为完全不可预期）；
          ② 不能报 **E1002**（"找不到这个导出文件"）—— 这是最迷惑人的
             假象：用户会去导出记录里翻那个不存在的 `undo` 文件，
             而真因是"这个地址只支持 POST"。

        所以正确的行为是：**200 + E1001**（方法用错属于"请求不合法"），
        `user_message` 要指向真因。
        """
        r = client.get(f"{API}/meetings/{meeting}/export/undo")

        # ① 绝不能是文件流：内容类型必须是 JSON
        ctype = r.headers.get("content-type", "")
        assert "application/json" in ctype, (
            f"`undo` 被 `{{file}}` 吃掉了，返回了文件流：{ctype!r}"
        )
        assert "content-disposition" not in {
            k.lower() for k in r.headers
        }, "被 `{file}` 吃掉：响应带了下载头"

        body = r.json()
        # ② 绝不能是"文件不存在"那条误导性文案
        assert body.get("code") != "E1002", (
            f"`undo` 被 `{{file}}` 吃掉了：{r.status_code} {r.text[:160]}"
        )
        # 真行为：E1001 + 指向"该用 POST"的文案
        assert body.get("code") == "E1001", body
        assert "撤销" in body.get("message", ""), body

    def test_post_undo_still_works(self, client, meeting, fs):
        """★ 对照：占位路由**不能**把真正的 `POST .../export/undo` 带偏。

        这是"加一条占位路由"最典型的翻车方式 —— 路由**加对了**，
        但把功能路由遮住了，结果修好一个坏掉一个。
        """
        from mmwf.api.deps import create_session_token

        # ⚠️ 导出**必须先有记录稿**（`decide` 的决定）：
        #    `export_docx` 会先 `latest_record_version()`，没有
        #    `record/v*.md` 会抛 `ExportServiceError`。前面几组下载测试
        #    用的是 `_write_export` 手工塞文件，但 `undo` 要从 **manifest**
        #    注销，只有真走一遍导出才拿得到条目 —— 所以这里得先把记录稿备好。
        _write_records(fs, {1: "会议基本情况\n\n会议认为，方案可行。"})

        created = _export_docx(client, meeting)
        assert created.startswith("export/"), created

        r = client.post(
            f"{API}/meetings/{meeting}/export/undo",
            json={"rel_path": created},
            headers={"X-MMWF-Token": create_session_token()},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True, body
        assert body["data"]["removed"] is True, body

    def test_export_without_record_is_not_internal_error(self, client, meeting, fs):
        """★ 没有记录稿时导出 → 必须是**业务错误**，不能是 E9001。

        这条是"验收时顺手发现"的真缺陷（见施工卡坑 16）：

        `ExportService.latest_record_version()` 在没记录稿时抛
        `ExportServiceError` —— 而它是 `RuntimeError` 的子类，
        **不是 `MmwfError`**。于是它绕过了 `@app.exception_handler(MmwfError)`，
        掉进兜底的 `@app.exception_handler(Exception)`，被渲染成
        **E9001「程序遇到一个未预期的问题」**。

        这对用户是**双重误导**：
          · "未预期" —— 其实这是完全可预期、且最常见的首次使用场景；
          · "请把编号告诉技术人员" —— 用户去报障，而正确答案是
            "你还没生成记录稿"。

        正确行为：E1002（"找不到要处理的文件"），文案指向"先生成记录稿"。
        """
        r = client.post(f"{API}/meetings/{meeting}/export", json={})
        assert r.status_code == 200
        body = r.json()
        _no_500(r)  # ① 绝不能是 E9xxx
        assert body["ok"] is False, body
        assert body["code"] != "E9001", (
            f"ExportServiceError 漏成了内部错误：{body}"
        )


class TestTraversalGuard:
    """★★ 直接调端点函数验判据 —— 这是"防穿越"的真凭据（见文件头注释）。"""

    @pytest.mark.parametrize(
        "bad",
        [
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
        ],
    )
    def test_guard_rejects(self, settings, isolated_appdir, db_path, meeting, bad):
        from mmwf.api.routes import build_router
        from mmwf.domain.errors import MmwfError

        router = build_router(settings)
        # 找到下载端点的处理函数
        func = None
        for route in router.routes:
            if getattr(route, "path", None) == "/meetings/{meeting_id}/export/{file:path}":
                func = route.endpoint
                break
        assert func is not None, "没找到下载端点"
        with pytest.raises(MmwfError) as ei:
            func(meeting_id=meeting, file=bad)
        assert ei.value.code == "E1001", f"{bad!r} 应被拒为 E1001，实际 {ei.value.code}"

    @pytest.mark.parametrize("good", ["a.docx", "会议纪要-v2.docx", "报告.doc"])
    def test_guard_allows_plain_names(self, settings, isolated_appdir, db_path, meeting, fs, good):
        """★ 合法文件名不能被误伤（含中文名）。"""
        from mmwf.api.routes import build_router
        from mmwf.domain.errors import MmwfError

        _write_export(fs, good, b"PK\x03\x04ok")
        router = build_router(settings)
        func = None
        for route in router.routes:
            if getattr(route, "path", None) == "/meetings/{meeting_id}/export/{file:path}":
                func = route.endpoint
                break
        try:
            resp = func(meeting_id=meeting, file=good)
        except MmwfError as exc:  # pragma: no cover - 失败时给出清晰信息
            pytest.fail(f"合法文件名 {good!r} 被拒了：{exc.code} {exc.detail}")
        assert resp.body[:4] == b"PK\x03\x04"


class TestNoInternalErrorForBadInput:
    @pytest.mark.parametrize("method,path,json_body", [
        ("GET", f"{API}/meetings/m-x/diff?base=abc", None),
        ("GET", f"{API}/meetings/m-x/diff?compare=-1", None),
        ("GET", f"{API}/meetings/m-x/export/", None),
        ("GET", f"{API}/meetings//diff", None),
    ])
    def test_never_e9xxx(self, client, method, path, json_body):
        r = client.request(method, path, json=json_body)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        code = str(body.get("code") or "")
        assert not code.startswith("E9"), f"{path} -> {body}"
