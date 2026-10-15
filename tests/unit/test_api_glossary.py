"""术语库端点单测（T1-14 B 档）。

7 个端点全是对 `GlossaryService` 的薄包装，所以测试重点是**接线契约**
与**几处容易漏的语义**：

  ① 受保护 + 信封形状
  ② `entry_id` 的**确定性生成**（裁决 D5）—— 直接决定"重复 POST 幂等"
  ③ `PUT` 的**路径优先**与"只改 enabled 不重传全文"
  ④ 导入：`csv_text` 与 `path` 两条路、**`problems` 必须带出**
  ⑤ 导出：往返幂等（导出 → 清库 → 导入 → 完全一致）
  ⑥ **所有写操作都留 `glossary_change` 审计**（AUD-08 红线）
  ⑦ 错误文案：条目不存在时**不能**说"找不到文件"
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmwf.api import deps as api_deps

API = "/api/v1"

VALID_CSV = (
    "category,canonical,aliases,note\n"
    "org,市政府,市政府办|市府,常用机构\n"
    "person,李处长,李处,常用人名\n"
)

BAD_CSV = (
    "category,canonical,aliases,note\n"
    "org,市政府,市政府办,常用机构\n"
    "bogus,坏类别,x,类别非法\n"
    "person,李处长,李处,常用人名\n"
)


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

    c = TestClient(create_app(settings), client=("127.0.0.1", 51234))
    c.headers.update({"X-MMWF-Token": api_deps.create_session_token()})
    return c


@pytest.fixture
def anon_client(settings, isolated_appdir, db_path) -> TestClient:
    from mmwf.main import create_app

    return TestClient(create_app(settings), client=("127.0.0.1", 51234))


def _add(client: TestClient, **overrides) -> dict:
    payload = {
        "category": "person",
        "canonical": "张建军",
        "aliases": ["张建国"],
        "note": "常用人名",
    }
    payload.update(overrides)
    r = client.post(f"{API}/glossary", json=payload)
    assert r.json()["ok"] is True, r.json()
    return r.json()["data"]["entry"]


def _audits(db_path: Path, action: str = "glossary_change") -> list:
    from mmwf.infra.audit import AuditQuery, query

    return query(db_path, AuditQuery(action=action))


# ---------------------------------------------------------------------------
# ① 受保护
# ---------------------------------------------------------------------------

class TestProtected:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", f"{API}/glossary"),
            ("POST", f"{API}/glossary"),
            ("PUT", f"{API}/glossary/person:x"),
            ("DELETE", f"{API}/glossary/person:x"),
            ("POST", f"{API}/glossary/import"),
            ("GET", f"{API}/glossary/export"),
        ],
    )
    def test_rejects_without_token(self, anon_client: TestClient, method: str, path: str):
        body = anon_client.request(method, path, json={}).json()
        assert body["ok"] is False
        assert body["code"] == "E6001"

    def test_envelope_shape(self, client: TestClient):
        body = client.get(f"{API}/glossary").json()
        assert set(body) == {"ok", "code", "message", "data", "trace_id"}


# ---------------------------------------------------------------------------
# ② 列出
# ---------------------------------------------------------------------------

class TestList:
    def test_empty(self, client: TestClient):
        data = client.get(f"{API}/glossary").json()["data"]
        assert data["entries"] == []
        assert data["count"] == 0
        assert data["conflicts"] == {}

    def test_returns_created(self, client: TestClient):
        _add(client)
        data = client.get(f"{API}/glossary").json()["data"]
        assert data["count"] == 1
        assert data["entries"][0]["canonical"] == "张建军"

    def test_enabled_only_filter(self, client: TestClient):
        e = _add(client)
        _add(client, canonical="李处长", aliases=[])
        client.put(f"{API}/glossary/{e['entry_id']}", json={"enabled": False})

        all_ = client.get(f"{API}/glossary").json()["data"]
        only = client.get(f"{API}/glossary?enabled_only=true").json()["data"]
        assert all_["count"] == 2
        assert only["count"] == 1
        assert only["entries"][0]["canonical"] == "李处长"

    def test_conflicts_reported(self, client: TestClient):
        """同一别名指向两个规范写法 → 列表里要能看到冲突。"""
        _add(client, canonical="张建军", aliases=["建军"])
        _add(client, canonical="张建国", aliases=["建军"])
        data = client.get(f"{API}/glossary").json()["data"]
        assert data["conflicts"], "别名冲突没有被报出来"

    def test_ordering_is_stable(self, client: TestClient):
        _add(client, category="term", canonical="放管服", aliases=[])
        _add(client, category="org", canonical="市政府", aliases=[])
        _add(client, category="person", canonical="张建军", aliases=[])
        got = [e["category"] for e in client.get(f"{API}/glossary").json()["data"]["entries"]]
        assert got == ["org", "person", "term"]


# ---------------------------------------------------------------------------
# ③ 新建（★ entry_id 确定性 —— 裁决 D5）
# ---------------------------------------------------------------------------

class TestCreate:
    def test_creates(self, client: TestClient):
        body = client.post(f"{API}/glossary", json={
            "category": "person", "canonical": "张建军", "aliases": ["张建国"],
        }).json()
        assert body["ok"] is True
        assert body["data"]["action"] == "created"
        assert body["data"]["entry"]["aliases"] == ["张建国"]

    def test_entry_id_is_deterministic(self, client: TestClient):
        """★ `entry_id` 必须是 `f"{category}:{canonical}"`。

        这直接决定"重复 POST 同一条术语"是**优雅 upsert** 还是
        **撞 UNIQUE 约束抛 IntegrityError**。表上有
        `UNIQUE (category, canonical)`，随机 id 会制造脏数据。
        """
        e = _add(client, category="person", canonical="张建军")
        assert e["entry_id"] == "person:张建军"

    def test_repeated_post_is_idempotent(self, client: TestClient):
        """★ 重复 POST 同一条 → updated，**不是** IntegrityError。"""
        _add(client, canonical="张建军", aliases=["张建国"])
        body = client.post(f"{API}/glossary", json={
            "category": "person", "canonical": "张建军", "aliases": ["建军"],
        }).json()
        assert body["ok"] is True
        assert body["data"]["action"] == "updated"
        # 仍然只有一条
        assert client.get(f"{API}/glossary").json()["data"]["count"] == 1

    def test_explicit_entry_id_respected(self, client: TestClient):
        e = _add(client, entry_id="custom-id-1")
        assert e["entry_id"] == "custom-id-1"

    def test_missing_category_rejected(self, client: TestClient):
        body = client.post(f"{API}/glossary", json={"canonical": "张建军"}).json()
        assert body["ok"] is False
        assert body["code"] == "E1001"

    def test_missing_canonical_rejected(self, client: TestClient):
        body = client.post(f"{API}/glossary", json={"category": "person"}).json()
        assert body["ok"] is False
        assert body["code"] == "E1001"

    def test_blank_strings_rejected(self, client: TestClient):
        body = client.post(f"{API}/glossary", json={
            "category": "  ", "canonical": "  ",
        }).json()
        assert body["ok"] is False

    def test_invalid_category_rejected(self, client: TestClient):
        """非法 category 由领域层的 `GlossaryEntry.__post_init__` 把关。"""
        body = client.post(f"{API}/glossary", json={
            "category": "not_a_category", "canonical": "x",
        }).json()
        assert body["ok"] is False
        # 不应 500（必须是业务信封）
        assert body["code"] != "E9001"

    @pytest.mark.parametrize("aliases", [["a|b"], "x|y", '["p","q"]', None, []])
    def test_alias_formats_accepted(self, client: TestClient, aliases):
        body = client.post(f"{API}/glossary", json={
            "category": "term", "canonical": "放管服", "aliases": aliases,
        }).json()
        assert body["ok"] is True, body
        assert isinstance(body["data"]["entry"]["aliases"], list)

    def test_enabled_defaults_true(self, client: TestClient):
        assert _add(client)["enabled"] is True

    def test_can_create_disabled(self, client: TestClient):
        assert _add(client, enabled=False)["enabled"] is False

    def test_colon_in_canonical_does_not_break(self, client: TestClient):
        """★ `canonical` 含 `:` 时 id 有歧义，但**只当不透明主键**用。

        我们从不反向解析 id 得到 category/canonical，
        所以拼出来的 id 丑一点没关系，关键是**能存能取**。
        """
        e = _add(client, category="term", canonical="关于：XX 事项")
        got = client.get(f"{API}/glossary").json()["data"]["entries"]
        assert any(x["entry_id"] == e["entry_id"] for x in got)
        assert any(x["canonical"] == "关于：XX 事项" for x in got)


# ---------------------------------------------------------------------------
# ④ 更新（★ 路径优先 + 只改 enabled 不重传）
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_full_update(self, client: TestClient):
        e = _add(client, aliases=["张建国"])
        body = client.put(f"{API}/glossary/{e['entry_id']}", json={
            "canonical": "张建军", "aliases": ["张建国", "建军"], "note": "改了",
        }).json()
        assert body["ok"] is True
        assert body["data"]["entry"]["aliases"] == ["张建国", "建军"]
        assert body["data"]["entry"]["note"] == "改了"

    def test_path_id_wins_over_body_id(self, client: TestClient):
        """★ 路径说 A、body 说 B → **以路径为准**（裁决 D5）。"""
        e = _add(client, canonical="张建军")
        body = client.put(f"{API}/glossary/{e['entry_id']}", json={
            "entry_id": "totally-different", "canonical": "张建军",
        }).json()
        assert body["ok"] is True
        assert body["data"]["entry"]["entry_id"] == e["entry_id"]
        # body 里那个假 id 不该在库里出现
        got = client.get(f"{API}/glossary").json()["data"]["entries"]
        assert all(x["entry_id"] != "totally-different" for x in got)

    def test_disable_only_does_not_wipe_content(self, client: TestClient):
        """★ 只传 `enabled` 时**不得**清空 aliases/note。

        这正是"停用该单独走 set_enabled"的理由：若把 `{enabled: false}`
        当全量替换，用户点一下"停用"就会把别名和备注抹掉。
        """
        e = _add(client, aliases=["张建国"], note="重要")
        body = client.put(f"{API}/glossary/{e['entry_id']}", json={"enabled": False}).json()
        assert body["ok"] is True
        assert body["data"]["action"] == "disable"
        entry = body["data"]["entry"]
        assert entry["enabled"] is False
        assert entry["aliases"] == ["张建国"], "停用把别名清空了！"
        assert entry["note"] == "重要", "停用把备注清空了！"

    def test_enable_again(self, client: TestClient):
        e = _add(client)
        client.put(f"{API}/glossary/{e['entry_id']}", json={"enabled": False})
        body = client.put(f"{API}/glossary/{e['entry_id']}", json={"enabled": True}).json()
        assert body["data"]["action"] == "enable"
        assert body["data"]["entry"]["enabled"] is True

    def test_partial_update_preserves_other_fields(self, client: TestClient):
        e = _add(client, aliases=["张建国"], note="原备注")
        body = client.put(f"{API}/glossary/{e['entry_id']}", json={
            "canonical": "张建军", "note": "新备注",
        }).json()
        entry = body["data"]["entry"]
        # 没传 aliases → 保留原值（因为是在 existing 上 merge）
        assert entry["aliases"] == ["张建国"]
        assert entry["note"] == "新备注"

    def test_unknown_entry_returns_business_error(self, client: TestClient):
        body = client.put(f"{API}/glossary/nope", json={"canonical": "x"}).json()
        assert body["ok"] is False
        assert body["code"] == "E1002"

    def test_unknown_entry_message_is_not_about_files(self, client: TestClient):
        """★ 错误文案不能是 E1002 的**文件**文案 —— 用户会去找文件。"""
        body = client.put(f"{API}/glossary/nope", json={"canonical": "x"}).json()
        assert "文件" not in body["message"]
        assert "术语" in body["message"]


# ---------------------------------------------------------------------------
# ⑤ 删除
# ---------------------------------------------------------------------------

class TestDelete:
    def test_deletes(self, client: TestClient):
        e = _add(client)
        body = client.delete(f"{API}/glossary/{e['entry_id']}").json()
        assert body["ok"] is True
        assert body["data"]["removed"] is True
        assert client.get(f"{API}/glossary").json()["data"]["count"] == 0

    def test_unknown_returns_business_error(self, client: TestClient):
        body = client.delete(f"{API}/glossary/nope").json()
        assert body["ok"] is False
        assert body["code"] == "E1002"
        assert "文件" not in body["message"]

    def test_delete_is_not_idempotent_silently(self, client: TestClient):
        """第二次删除要**明确报错**，不是静默成功（否则前端无法区分）。"""
        e = _add(client)
        client.delete(f"{API}/glossary/{e['entry_id']}")
        body = client.delete(f"{API}/glossary/{e['entry_id']}").json()
        assert body["ok"] is False


# ---------------------------------------------------------------------------
# ⑥ 导入
# ---------------------------------------------------------------------------

class TestImport:
    def test_import_via_csv_text(self, client: TestClient):
        body = client.post(f"{API}/glossary/import", json={
            "csv_text": VALID_CSV, "filename": "terms.csv",
        }).json()
        assert body["ok"] is True
        assert body["data"]["created"] == 2
        assert body["data"]["problems"] == []
        assert client.get(f"{API}/glossary").json()["data"]["count"] == 2

    def test_import_via_path(self, client: TestClient, tmp_path: Path):
        p = tmp_path / "terms.csv"
        p.write_text(VALID_CSV, encoding="utf-8")
        body = client.post(f"{API}/glossary/import", json={"path": str(p)}).json()
        assert body["ok"] is True
        assert body["data"]["created"] == 2

    def test_missing_path_file_rejected(self, client: TestClient, tmp_path: Path):
        body = client.post(f"{API}/glossary/import", json={
            "path": str(tmp_path / "nope.csv"),
        }).json()
        assert body["ok"] is False
        assert body["code"] == "E1002"

    def test_neither_path_nor_text_rejected(self, client: TestClient):
        body = client.post(f"{API}/glossary/import", json={}).json()
        assert body["ok"] is False
        assert body["code"] == "E1001"

    def test_problems_are_surfaced_not_swallowed(self, client: TestClient):
        """★★ 本端点最要紧的一条：`problems` 必须**如实带出**。

        "导入成功但有 1 行被跳过"是最容易被忽略的情况。
        只报 `created: 2` 而不报 `problems`，用户会以为全导进去了 ——
        那 1 行坏数据就永远查不出来。
        """
        body = client.post(f"{API}/glossary/import", json={
            "csv_text": BAD_CSV, "filename": "bad.csv",
        }).json()
        assert body["ok"] is True
        assert body["data"]["created"] == 2          # 好的两行进去了
        assert len(body["data"]["problems"]) == 1    # 坏的那行**被报出来**
        assert "bogus" in body["data"]["problems"][0]
        # message 也要提示有问题，否则用户只看 message 就错过了
        assert "问题" in body["message"]

    def test_directory_traversal_in_filename_is_neutralised(self, client: TestClient):
        """★ 前端可能传 `../../x.csv` —— 只取文件名，防穿越。"""
        body = client.post(f"{API}/glossary/import", json={
            "csv_text": VALID_CSV, "filename": "../../../../etc/evil.csv",
        }).json()
        assert body["ok"] is True
        assert body["data"]["created"] == 2
        # source 只说文件名，不带路径
        assert ".." not in str(body["data"]["source"])

    def test_import_updates_existing(self, client: TestClient):
        _add(client, category="org", canonical="市政府", aliases=[])
        body = client.post(f"{API}/glossary/import", json={
            "csv_text": VALID_CSV, "filename": "terms.csv",
        }).json()
        assert body["data"]["updated"] >= 1

    def test_empty_csv_text_rejected(self, client: TestClient):
        body = client.post(f"{API}/glossary/import", json={
            "csv_text": "", "filename": "empty.csv",
        }).json()
        assert body["ok"] is False

    def test_temp_dir_is_cleaned_up(self, client: TestClient, settings):
        """★ 临时文件必须删干净 —— 术语库可能含单位专有名词。

        落在 `data_dir/.tmp`，与 `filestore.plaintext()` 的约定一致
        （**不是**系统 /tmp）。
        """
        client.post(f"{API}/glossary/import", json={
            "csv_text": VALID_CSV, "filename": "terms.csv",
        })
        tmp_root = settings.resolve_path(settings.app.data_dir) / ".tmp"
        leftovers = list(tmp_root.glob("api-*")) if tmp_root.is_dir() else []
        assert leftovers == [], f"临时目录没清干净：{leftovers}"


# ---------------------------------------------------------------------------
# ⑦ 导出 + 往返幂等
# ---------------------------------------------------------------------------

class TestExport:
    def test_returns_csv_text(self, client: TestClient):
        _add(client)
        body = client.get(f"{API}/glossary/export").json()
        assert body["ok"] is True
        assert "category" in body["data"]["csv"]
        assert body["data"]["count"] == 1

    def test_empty_export(self, client: TestClient):
        body = client.get(f"{API}/glossary/export").json()
        assert body["ok"] is True
        assert body["data"]["count"] == 0

    def test_roundtrip_is_lossless(self, client: TestClient):
        """★★ 验收标准 #8：导出 → 清库 → 导入 → **条目完全一致**。

        含停用条目与中文别名 —— 这两样最容易在 CSV 往返里丢。
        """
        _add(client, category="person", canonical="张建军",
             aliases=["张建国", "建军"], note="常用人名")
        _add(client, category="org", canonical="市政府", aliases=["市府"], note="")
        _add(client, category="term", canonical="放管服", aliases=[],
             note="政策术语", enabled=False)

        before = client.get(f"{API}/glossary").json()["data"]["entries"]
        csv_text = client.get(f"{API}/glossary/export").json()["data"]["csv"]

        for e in before:
            client.delete(f"{API}/glossary/{e['entry_id']}")
        assert client.get(f"{API}/glossary").json()["data"]["count"] == 0

        client.post(f"{API}/glossary/import", json={
            "csv_text": csv_text, "filename": "roundtrip.csv",
        })
        after = client.get(f"{API}/glossary").json()["data"]["entries"]

        key = lambda x: (x["category"], x["canonical"])  # noqa: E731
        b = {key(x): (tuple(x["aliases"]), x["note"], x["enabled"]) for x in before}
        a = {key(x): (tuple(x["aliases"]), x["note"], x["enabled"]) for x in after}
        assert a == b, f"往返丢数据：only-before={b.keys() - a.keys()}"

    def test_roundtrip_preserves_entry_id(self, client: TestClient):
        """确定性 id 让往返后 entry_id 也一致（更利于外部引用）。"""
        e = _add(client, category="person", canonical="张建军")
        csv_text = client.get(f"{API}/glossary/export").json()["data"]["csv"]
        client.delete(f"{API}/glossary/{e['entry_id']}")
        client.post(f"{API}/glossary/import", json={"csv_text": csv_text,
                                                    "filename": "rt.csv"})
        after = client.get(f"{API}/glossary").json()["data"]["entries"]
        assert after[0]["entry_id"] == e["entry_id"]

    def test_temp_dir_cleaned_up_on_export(self, client: TestClient, settings):
        _add(client)
        client.get(f"{API}/glossary/export")
        tmp_root = settings.resolve_path(settings.app.data_dir) / ".tmp"
        leftovers = list(tmp_root.glob("api-*")) if tmp_root.is_dir() else []
        assert leftovers == []

    def test_export_is_audited(self, client: TestClient, db_path: Path):
        _add(client)
        client.get(f"{API}/glossary/export")
        actions = [r.detail.get("op") for r in _audits(db_path)]
        assert "export" in actions


# ---------------------------------------------------------------------------
# ⑧ 审计留痕（AUD-08 红线）
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_create_is_audited(self, client: TestClient, db_path: Path):
        before = len(_audits(db_path))
        _add(client)
        assert len(_audits(db_path)) == before + 1
        assert _audits(db_path)[-1].action == "glossary_change"

    def test_update_is_audited(self, client: TestClient, db_path: Path):
        e = _add(client)
        before = len(_audits(db_path))
        client.put(f"{API}/glossary/{e['entry_id']}", json={"canonical": "张建军",
                                                            "note": "改"})
        assert len(_audits(db_path)) == before + 1

    def test_disable_is_audited(self, client: TestClient, db_path: Path):
        e = _add(client)
        before = len(_audits(db_path))
        client.put(f"{API}/glossary/{e['entry_id']}", json={"enabled": False})
        assert len(_audits(db_path)) == before + 1

    def test_delete_is_audited_with_content(self, client: TestClient, db_path: Path):
        """★ 删除必须在**删之前**把内容写进审计 ——
        删了之后就再也查不到"删的是什么"了。"""
        e = _add(client, canonical="张建军")
        before = len(_audits(db_path))
        client.delete(f"{API}/glossary/{e['entry_id']}")
        after = _audits(db_path)
        assert len(after) == before + 1
        assert "张建军" in str(after[-1].detail)

    def test_import_is_audited(self, client: TestClient, db_path: Path):
        before = len(_audits(db_path))
        client.post(f"{API}/glossary/import", json={"csv_text": VALID_CSV,
                                                    "filename": "t.csv"})
        assert len(_audits(db_path)) == before + 1

    def test_operator_is_recorded(self, client: TestClient, db_path: Path):
        """审计的核心是"谁做的"—— operator 必须落到记录里。"""
        client.post(f"{API}/glossary", json={
            "category": "person", "canonical": "张建军", "operator": "刘秘书",
        })
        assert _audits(db_path)[-1].operator == "刘秘书"

    def test_operator_defaults_to_system(self, client: TestClient, db_path: Path):
        _add(client)
        assert _audits(db_path)[-1].operator == "system"

    def test_read_operations_are_not_audited(self, client: TestClient, db_path: Path):
        """只读操作不该往审计里灌噪音。"""
        _add(client)
        before = len(_audits(db_path))
        client.get(f"{API}/glossary")
        client.get(f"{API}/glossary?enabled_only=true")
        assert len(_audits(db_path)) == before

    def test_audit_detail_has_no_long_transcript_text(self, client: TestClient, db_path: Path):
        """AUD-05 的同类要求：detail 不该塞长正文。

        术语库的 `note` 可能被写得很长；审计只需记"改了哪个条目"。
        """
        long_note = "这是一段很长的备注" * 40
        client.post(f"{API}/glossary", json={
            "category": "person", "canonical": "张建军", "note": long_note,
        })
        raw = str(_audits(db_path)[-1].detail)
        assert long_note not in raw

    def test_minimum_coverage_flips_to_ok(self, client: TestClient, db_path: Path):
        """★ AUD-08：术语库变更这一核查项应从 `empty` 变成 `ok`。"""
        from mmwf.infra.audit import audit_minimum_coverage

        assert audit_minimum_coverage(db_path)["术语库/模板变更"] == "empty"
        _add(client)
        assert audit_minimum_coverage(db_path)["术语库/模板变更"] == "ok"


# ---------------------------------------------------------------------------
# ⑨ 其它
# ---------------------------------------------------------------------------

class TestMisc:
    def test_never_500_on_any_endpoint(self, client: TestClient):
        assert client.get(f"{API}/glossary").status_code == 200
        assert client.get(f"{API}/glossary/export").status_code == 200
        assert client.post(f"{API}/glossary", json={}).status_code == 200
        assert client.post(f"{API}/glossary/import", json={}).status_code == 200
        assert client.delete(f"{API}/glossary/nope").status_code == 200

    def test_chinese_content_survives_utf8(self, client: TestClient):
        """三条铁律第 1 条：UTF-8 无 BOM、不被转义。"""
        e = _add(client, canonical="张建军", aliases=["张建国", "建军"], note="备注")
        raw = client.get(f"{API}/glossary").content.decode("utf-8")
        assert "张建军" in raw
        assert "\\u5f20" not in raw
