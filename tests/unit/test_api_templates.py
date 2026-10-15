"""模板 3 端点单测（T1-16）。

重点在**语义**，不在"跑通了没"：

  ① 两类来源（builtin/db）**合并返回**、`source` 每项都有
  ② 内置**不可改** → **E1003**（不是 E1001、不是 200）
  ③ `version_no` 服务端自增，客户端传的**被忽略**
  ④ YAML 改一行 → 再读就变了（N5.2 热改，**无缓存**）
  ⑤ 用户填错 → E1001，**不许是 E9001**
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmwf.api import deps as api_deps

API = "/api/v1"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def isolated_appdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def cfg_root(tmp_path: Path) -> Path:
    """一份**可以随便改**的 config 副本 —— 用于验"改 yaml 即生效"。"""
    dest = tmp_path / "cfg"
    shutil.copytree(CONFIG_DIR, dest)
    return dest


@pytest.fixture
def settings(tmp_path: Path, cfg_root: Path):
    from mmwf.settings import load_settings

    cfg = load_settings(cfg_root)
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


def _body(**over) -> dict:
    body = {
        "name": "站会议纪要",
        "meeting_kind": "regular_meeting",
        "required_elements": [{"key": "title", "label": "标题"}],
        "section_order": ["基本情况", "议定事项"],
        "style": {"readings_allowed": ["会议认为"]},
    }
    body.update(over)
    return body


def _no_500(r) -> None:
    """任何输入都不该落到 E9xxx（T1-14 坑 9 的判据）。"""
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    code = str(body.get("code") or "")
    assert not code.startswith("E9"), f"落到 E9xxx 了：{body}"


# ---------------------------------------------------------------------------
# 受保护
# ---------------------------------------------------------------------------

class TestProtected:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", f"{API}/templates"),
            ("POST", f"{API}/templates"),
            ("PUT", f"{API}/templates/tpl-regular-meeting-v1"),
        ],
    )
    def test_requires_token(self, anon_client, method, path):
        r = anon_client.request(method, path, json={})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert r.json()["code"] == "E6001"


# ---------------------------------------------------------------------------
# ① GET /templates
# ---------------------------------------------------------------------------

class TestListTemplates:
    def test_builtin_four(self, client):
        r = client.get(f"{API}/templates")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        d = body["data"]
        assert d["counts"] == {"total": 4, "builtin": 4, "db": 0}

    def test_every_item_has_source(self, client):
        d = client.get(f"{API}/templates").json()["data"]
        for t in d["templates"]:
            assert t["source"] in {"builtin", "db"}
            assert t["template_id"] and t["name"] and t["meeting_kind"]

    def test_filter_by_kind(self, client):
        d = client.get(f"{API}/templates?meeting_kind=party_group").json()["data"]
        assert d["counts"]["total"] == 1
        assert d["templates"][0]["meeting_kind"] == "party_group"

    def test_filter_unknown_kind_empty(self, client):
        r = client.get(f"{API}/templates?meeting_kind=nope")
        assert r.status_code == 200
        assert r.json()["data"]["counts"]["total"] == 0

    def test_message_reports_counts(self, client):
        msg = client.get(f"{API}/templates").json()["message"]
        assert "4" in msg and "内置" in msg

    def test_yaml_change_takes_effect_without_code_change(self, client, cfg_root):
        """★ N5.2：改一行 yaml → 再读就变了。**这是本端点不缓存的唯一理由。**"""
        p = cfg_root / "templates" / "regular-meeting.yaml"
        text = p.read_text(encoding="utf-8")
        assert "会议基本情况" in text
        p.write_text(
            text.replace("会议基本情况", "改写过的首段"), encoding="utf-8"
        )
        d = client.get(f"{API}/templates?meeting_kind=regular_meeting").json()["data"]
        tpl = d["templates"][0]
        assert "改写过的首段" in tpl["section_order"], (
            f"yaml 改了但列表没变 —— 说明被缓存了：{tpl['section_order']}"
        )

    def test_broken_yaml_does_not_500(self, client, cfg_root):
        """坏掉一个 yaml → 200 + warning，其余照常（不该整体 500）。"""
        (cfg_root / "templates" / "bad.yaml").write_text(
            "meta: [\n  broken", encoding="utf-8"
        )
        r = client.get(f"{API}/templates")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["counts"]["builtin"] == 4
        assert "bad.yaml" in (d.get("warning") or "")


# ---------------------------------------------------------------------------
# ② POST /templates
# ---------------------------------------------------------------------------

class TestCreateTemplate:
    def test_create_autogen_id(self, client):
        r = client.post(f"{API}/templates", json=_body())
        body = r.json()
        assert r.status_code == 200 and body["ok"] is True
        t = body["data"]["template"]
        assert t["template_id"].startswith("tpl-custom-")
        assert t["source"] == "db"
        assert t["version_no"] == 1

    def test_create_with_explicit_id(self, client):
        r = client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        assert r.json()["data"]["template"]["template_id"] == "tpl-mine"

    def test_create_shows_up_in_list(self, client):
        client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        d = client.get(f"{API}/templates").json()["data"]
        assert d["counts"] == {"total": 5, "builtin": 4, "db": 1}

    def test_duplicate_id_is_e1001(self, client):
        client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        r = client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        assert r.json()["code"] == "E1001"
        _no_500(r)

    @pytest.mark.parametrize(
        "bad",
        [
            {},
            {"name": ""},
            {"name": "x", "meeting_kind": "k"},
            {"name": "x", "meeting_kind": "k", "required_elements": [], "section_order": ["a"]},
            {"name": "x", "meeting_kind": "k", "required_elements": [1], "section_order": ["a"]},
            {"name": "x", "meeting_kind": "k", "required_elements": [{"key": "t", "label": "T"}], "section_order": "a"},
            {"name": "x", "meeting_kind": "k", "required_elements": [{"key": "t", "label": "T"}], "section_order": ["a"], "style": [1]},
            {"name": "字" * 60, "meeting_kind": "k", "required_elements": [{"key": "t", "label": "T"}], "section_order": ["a"]},
        ],
    )
    def test_bad_input_is_e1001_not_e9001(self, client, bad):
        r = client.post(f"{API}/templates", json=bad)
        assert r.json()["code"] == "E1001", r.json()
        _no_500(r)

    def test_builtin_name_collision_warns(self, client):
        """与内置同名 → 200 但**必须有 warning**（否则用户以为丢数据）。"""
        r = client.post(
            f"{API}/templates",
            json=_body(template_id="tpl-regular-meeting-v1"),
        )
        body = r.json()
        assert body["ok"] is True
        assert body["data"].get("warning"), "同名撞车必须告警"
        # 列表里仍是内置那份
        d = client.get(f"{API}/templates").json()["data"]
        hit = [t for t in d["templates"] if t["template_id"] == "tpl-regular-meeting-v1"]
        assert hit[0]["source"] == "builtin"


# ---------------------------------------------------------------------------
# ③ PUT /templates/{id}
# ---------------------------------------------------------------------------

class TestUpdateTemplate:
    def test_update_increments_version(self, client):
        client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        r = client.put(f"{API}/templates/tpl-mine", json=_body(name="改过"))
        body = r.json()
        assert body["ok"] is True
        assert body["data"]["template"]["version_no"] == 2
        assert body["data"]["template"]["name"] == "改过"

    def test_client_version_ignored(self, client):
        """★ 客户端传 version=999 必须被忽略。"""
        client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        r = client.put(f"{API}/templates/tpl-mine", json=_body(version=999))
        assert r.json()["data"]["template"]["version_no"] == 2

    def test_builtin_is_e1003(self, client):
        """★ 内置不可改 → **E1003**。"""
        r = client.put(f"{API}/templates/tpl-regular-meeting-v1", json=_body())
        body = r.json()
        assert body["code"] == "E1003", body
        assert "不能" in body["message"] or "不能" in body["message"]
        _no_500(r)

    def test_builtin_unchanged_after_attempt(self, client):
        client.put(f"{API}/templates/tpl-regular-meeting-v1", json=_body(name="xyz"))
        d = client.get(f"{API}/templates").json()["data"]
        hit = [t for t in d["templates"] if t["template_id"] == "tpl-regular-meeting-v1"]
        assert hit[0]["name"] != "xyz"

    def test_missing_is_e1002_or_e1001(self, client):
        r = client.put(f"{API}/templates/nope", json=_body())
        assert r.json()["code"] in {"E1001", "E1002"}
        _no_500(r)

    @pytest.mark.parametrize(
        "bad",
        [
            {},
            {"name": ""},
            {"name": "x", "meeting_kind": "k", "required_elements": [], "section_order": ["a"]},
        ],
    )
    def test_bad_body_is_e1001(self, client, bad):
        client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        r = client.put(f"{API}/templates/tpl-mine", json=bad)
        assert r.json()["code"] == "E1001"
        _no_500(r)

    def test_audit_written(self, client, db_path):
        """★ 写操作必须留 `template_change`。"""
        from mmwf.infra.audit import AuditQuery, query

        client.post(f"{API}/templates", json=_body(template_id="tpl-mine"))
        client.put(f"{API}/templates/tpl-mine", json=_body(name="改过"))
        recs = query(db_path, AuditQuery(action="template_change"))
        assert len(recs) == 2, f"应有 2 条（create+update），实际 {len(recs)}"
        ops = {r.detail.get("op") for r in recs}
        assert ops == {"create", "update"}
