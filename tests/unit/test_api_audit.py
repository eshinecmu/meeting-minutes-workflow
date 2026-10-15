"""`/system/fonts`、`/audit/logs`、`/audit/export` 端点单测（T1-14 A 档）。

三个端点都是"只差接线"——底层服务（`font_check` / `audit.query` /
`audit.build_report`）在 T1-09 / T1-04 就已完备。所以这里测的重点
**不是业务逻辑**，而是三件接线特有的事：

  ① 信封形状与受保护（无令牌 → E6001）
  ② **参数契约**（尤其 `limit` 的夹取、条件组合）
  ③ **不撒谎的字段命名**（`returned` 不是 `total`）与
     **口径切换**（完整性 fail 时 message 必须变）

三重反向验证（施工卡 §3.2）也在这里落地：证明"能检出篡改"，
而不是只证明"正常时报 pass"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmwf.api import deps as api_deps

API = "/api/v1"


# ---------------------------------------------------------------------------
# fixtures（与 test_api.py 同构，刻意保持一致的隔离手法）
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

    app = create_app(settings)
    c = TestClient(app, client=("127.0.0.1", 51234))
    c.headers.update({"X-MMWF-Token": api_deps.create_session_token()})
    return c


@pytest.fixture
def anon_client(settings, isolated_appdir, db_path) -> TestClient:
    from mmwf.main import create_app

    return TestClient(create_app(settings), client=("127.0.0.1", 51234))


def _seed_audit(db_path: Path, n: int = 5) -> None:
    """往库里灌 n 条审计（走正规 AuditLogger，保证链是对的）。"""
    from mmwf.infra.audit import AuditLogger

    logger = AuditLogger(db_path)
    actions = ["login", "import_audio", "transcribe_done", "gate_decision", "export"]
    for i in range(n):
        logger.log(
            "刘秘书" if i % 2 == 0 else "张主任",
            actions[i % len(actions)],
            target_type="meeting",
            target_id=f"m-{i}",
            detail={"n": i},
        )


# ---------------------------------------------------------------------------
# ① 受保护（三个端点无令牌都要 E6001）
# ---------------------------------------------------------------------------

class TestProtected:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", f"{API}/system/fonts"),
            ("GET", f"{API}/audit/logs"),
            ("POST", f"{API}/audit/export"),
        ],
    )
    def test_rejects_without_token(self, anon_client: TestClient, method: str, path: str):
        r = anon_client.request(method, path, json={} if method == "POST" else None)
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "E6001"
        # 统一 200 + 信封（本项目的约定）
        assert r.status_code == 200

    def test_envelope_shape(self, client: TestClient):
        body = client.get(f"{API}/audit/logs").json()
        assert set(body) == {"ok", "code", "message", "data", "trace_id"}


# ---------------------------------------------------------------------------
# ② /system/fonts
# ---------------------------------------------------------------------------

class TestSystemFonts:
    def test_shape(self, client: TestClient):
        """形状契约。

        ⚠️ 实测 `build_check_result_json` 给的键是
        `available / all_installed / missing / policy / detail`
        （**不是** `required` / `checks` / `verdict` —— 那是我照 SAD
        §9.2 示例猜的）。`required` 由端点补上（SAD 明确要求），
        `detail[].font/status/resolved` 是逐项状态。
        这里按**实际实现**断言，不按文档示例 —— 文档示例漏了字段。
        """
        body = client.get(f"{API}/system/fonts").json()
        assert body["ok"] is True
        data = body["data"]
        assert "required" in data           # 端点补的
        assert "missing" in data
        assert "available" in data          # 探测是否可用
        assert "all_installed" in data
        assert "detail" in data
        assert isinstance(data["required"], list)
        assert isinstance(data["missing"], list)

    def test_required_fonts_match_spec(self, client: TestClient):
        """SAD §12.4 点名的四种字体必须在 required 里。"""
        data = client.get(f"{API}/system/fonts").json()["data"]
        joined = "".join(data["required"])
        for name in ("仿宋", "小标宋", "楷体", "黑体"):
            assert name in joined, f"required 里缺 {name}"

    def test_distinguishes_unknown_from_missing(self, client: TestClient):
        """★ 探测不可用时**不能**把结果说成"字体缺失"。

        这条是本端点最要紧的语义（施工卡 §4 坑 1 的另一半）：
        把 `UNKNOWN` 当 `MISSING` 报出去，用户会去装一个本来就装着的字体。

        `check_fonts(installed=set())` 是"预检不可用"的信号 →
        它会返回 `precheck_available=False`，且每项状态为 `unknown`。
        """
        from mmwf.domain.docx import font_check

        report = font_check.check_fonts(installed=set())
        assert report.precheck_available is False
        statuses = {c.status for c in report.checks}
        assert statuses == {font_check.STATUS_UNKNOWN}
        assert font_check.STATUS_MISSING not in statuses

    def test_precheck_unavailable_message_does_not_claim_missing(
        self, client: TestClient, monkeypatch
    ):
        """★ 端到端：探测不可用时 message **不得**说"缺少字体"。"""
        from mmwf.domain.docx import font_check

        real = font_check.check_fonts

        def fake(fonts=None, *, policy="warn", installed=None):
            return real(fonts=["仿宋_GB2312"], policy=policy, installed=set())

        monkeypatch.setattr(font_check, "check_fonts", fake)
        body = client.get(f"{API}/system/fonts").json()
        assert "缺少" not in body["message"]
        assert "无法读取" in body["message"]

    def test_precheck_available_is_reported(self, client: TestClient):
        """无论探测成功与否，`available` 都要如实出现。"""
        data = client.get(f"{API}/system/fonts").json()["data"]
        assert "available" in data
        assert isinstance(data["available"], bool)

    def test_message_mentions_missing_fonts(self, client: TestClient, monkeypatch):
        """缺字体时 message 要点名 —— 用户得知道去装什么。

        ⚠️ 必须**先把原函数存起来再 patch**。直接
        `monkeypatch.setattr(mod, "check_fonts", fake)` 然后在 fake 里
        调 `mod.check_fonts` 会**无限递归**（fake 调自己），
        报 `RecursionError` —— 第一版就是这么写的。
        """
        from mmwf.domain.docx import font_check

        real = font_check.check_fonts

        def fake(fonts=None, *, policy="warn", installed=None):
            return real(
                fonts=["仿宋_GB2312", "黑体"], policy=policy, installed={"黑体"}
            )

        monkeypatch.setattr(font_check, "check_fonts", fake)
        body = client.get(f"{API}/system/fonts").json()
        assert "仿宋_GB2312" in body["message"]
        # 装了的那一个不该出现在 message 里
        assert "黑体" not in body["message"]

    def test_never_500(self, client: TestClient):
        assert client.get(f"{API}/system/fonts").status_code == 200


# ---------------------------------------------------------------------------
# ③ /audit/logs
# ---------------------------------------------------------------------------

class TestAuditLogs:
    def test_empty(self, client: TestClient):
        body = client.get(f"{API}/audit/logs").json()
        assert body["ok"] is True
        assert body["data"]["returned"] == 0
        assert body["data"]["records"] == []

    def test_returns_all_by_default(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        data = client.get(f"{API}/audit/logs").json()["data"]
        assert data["returned"] == 5

    def test_field_is_returned_not_total(self, client: TestClient, db_path: Path):
        """★ 字段命名契约：`returned`（本页条数），**不是** `total`。

        起名 `total` 是错的 —— `LIMIT` 作用于 SQL，拿不到全库总数，
        用户会以为"全库就这么多"。这是 T1-14 裁决 D3。
        """
        _seed_audit(db_path, 5)
        data = client.get(f"{API}/audit/logs?limit=2").json()["data"]
        assert "returned" in data
        assert "total" not in data
        assert data["returned"] == 2
        assert data["limit"] == 2

    def test_has_more_true_when_page_full(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        data = client.get(f"{API}/audit/logs?limit=2").json()["data"]
        assert data["has_more"] is True

    def test_has_more_false_when_last_page(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 3)
        data = client.get(f"{API}/audit/logs?limit=10").json()["data"]
        assert data["has_more"] is False

    def test_limit_zero_clamped_to_one(self, client: TestClient, db_path: Path):
        """★ `limit=0` 会静默返回空 —— 必须夹到 1（施工卡 §4 坑 4）。"""
        _seed_audit(db_path, 3)
        data = client.get(f"{API}/audit/logs?limit=0").json()["data"]
        assert data["limit"] == 1
        assert data["returned"] == 1

    def test_limit_has_ceiling(self, client: TestClient, db_path: Path):
        """★ `limit` 不设上限 = 给了一个"把整库拉进内存"的接口。"""
        from mmwf.api.routes import AUDIT_MAX_LIMIT

        data = client.get(f"{API}/audit/logs?limit=999999").json()["data"]
        assert data["limit"] == AUDIT_MAX_LIMIT

    def test_negative_values_clamped(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 3)
        data = client.get(f"{API}/audit/logs?limit=-5&offset=-9").json()["data"]
        assert data["limit"] == 1
        assert data["offset"] == 0

    # -- AUD-07：按时间/人员/类型检索 --------------------------------------

    def test_filter_by_action(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        data = client.get(f"{API}/audit/logs?action=export").json()["data"]
        assert data["returned"] >= 1
        assert all(r["action"] == "export" for r in data["records"])

    def test_filter_by_operator(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        data = client.get(f"{API}/audit/logs?operator=张主任").json()["data"]
        assert data["returned"] >= 1
        assert all(r["operator"] == "张主任" for r in data["records"])

    def test_filter_by_target(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        data = client.get(
            f"{API}/audit/logs?target_type=meeting&target_id=m-2"
        ).json()["data"]
        assert data["returned"] == 1
        assert data["records"][0]["target_id"] == "m-2"

    def test_filter_combination_is_and(self, client: TestClient, db_path: Path):
        """多条件必须是 **AND**，不是 OR。"""
        _seed_audit(db_path, 10)
        data = client.get(
            f"{API}/audit/logs?action=login&operator=张主任"
        ).json()["data"]
        for r in data["records"]:
            assert r["action"] == "login"
            assert r["operator"] == "张主任"

    def test_filter_by_time_window(self, client: TestClient, db_path: Path):
        from mmwf.infra.audit import AuditLogger

        logger = AuditLogger(db_path)
        logger.log("刘秘书", "login", ts="2026-01-01T00:00:00Z")
        logger.log("刘秘书", "login", ts="2026-06-01T00:00:00Z")

        data = client.get(
            f"{API}/audit/logs?from_ts=2026-05-01T00:00:00Z"
        ).json()["data"]
        assert data["returned"] == 1
        data = client.get(
            f"{API}/audit/logs?to_ts=2026-02-01T00:00:00Z"
        ).json()["data"]
        assert data["returned"] == 1

    def test_filter_no_match(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 3)
        data = client.get(f"{API}/audit/logs?action=logout").json()["data"]
        assert data["returned"] == 0
        assert data["has_more"] is False

    def test_filter_by_result(self, client: TestClient, db_path: Path):
        from mmwf.infra.audit import AuditLogger

        logger = AuditLogger(db_path)
        logger.log("刘秘书", "export", result="success")
        logger.log("刘秘书", "export", result="failure")
        data = client.get(f"{API}/audit/logs?result=failure").json()["data"]
        assert data["returned"] == 1
        assert data["records"][0]["result"] == "failure"

    def test_offset_paging_is_stable(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        p1 = client.get(f"{API}/audit/logs?limit=2&offset=0").json()["data"]
        p2 = client.get(f"{API}/audit/logs?limit=2&offset=2").json()["data"]
        ids1 = {r["log_id"] for r in p1["records"]}
        ids2 = {r["log_id"] for r in p2["records"]}
        assert not (ids1 & ids2)

    def test_records_are_ordered_by_log_id(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        records = client.get(f"{API}/audit/logs").json()["data"]["records"]
        ids = [r["log_id"] for r in records]
        assert ids == sorted(ids)

    def test_record_has_chain_fields(self, client: TestClient, db_path: Path):
        """审计检索要给出链式哈希字段（保密检查会看）。"""
        _seed_audit(db_path, 1)
        rec = client.get(f"{API}/audit/logs").json()["data"]["records"][0]
        for key in ("log_id", "ts", "operator", "action", "detail",
                    "result", "prev_hash", "row_hash"):
            assert key in rec

    def test_unknown_filter_value_returns_empty_not_error(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 3)
        body = client.get(f"{API}/audit/logs?action=no_such_action").json()
        assert body["ok"] is True
        assert body["data"]["returned"] == 0

    def test_never_500(self, client: TestClient):
        assert client.get(f"{API}/audit/logs").status_code == 200


# ---------------------------------------------------------------------------
# ④ /audit/export（AUD-06）
# ---------------------------------------------------------------------------

class TestAuditExport:
    def test_shape(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 3)
        body = client.post(f"{API}/audit/export", json={}).json()
        assert body["ok"] is True
        data = body["data"]
        for key in ("generated_at", "period", "integrity", "summary",
                    "minimum_check_items", "records"):
            assert key in data

    def test_integrity_conclusion_present(self, client: TestClient, db_path: Path):
        """★ AUD-06 的核心：报告必须含**完整性校验结论**。"""
        _seed_audit(db_path, 3)
        data = client.post(f"{API}/audit/export", json={}).json()["data"]
        assert data["integrity"]["result"] in ("pass", "fail")
        assert data["integrity"]["result"] == "pass"

    def test_no_action_required_when_healthy(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 3)
        data = client.post(f"{API}/audit/export", json={}).json()["data"]
        assert "action_required" not in data

    def test_summary_counts(self, client: TestClient, db_path: Path):
        _seed_audit(db_path, 5)
        data = client.post(f"{API}/audit/export", json={}).json()["data"]
        assert data["summary"]["total"] == 5

    def test_minimum_check_items_included(self, client: TestClient, db_path: Path):
        """AUD-08：报告要给出 8 类最小核查项的覆盖情况。"""
        _seed_audit(db_path, 5)
        data = client.post(f"{API}/audit/export", json={}).json()["data"]
        items = data["minimum_check_items"]
        assert len(items) == 8
        assert set(items.values()) <= {"ok", "empty", "missing"}

    def test_period_filter(self, client: TestClient, db_path: Path):
        from mmwf.infra.audit import AuditLogger

        logger = AuditLogger(db_path)
        logger.log("刘秘书", "login", ts="2026-01-01T00:00:00Z")
        logger.log("刘秘书", "login", ts="2026-06-01T00:00:00Z")
        data = client.post(
            f"{API}/audit/export", json={"from_ts": "2026-05-01T00:00:00Z"}
        ).json()["data"]
        assert data["summary"]["total"] == 1

    def test_empty_db_is_still_pass(self, client: TestClient):
        """空库的链是**完整的**（没有行可校验）—— 不该报 fail。"""
        body = client.post(f"{API}/audit/export", json={}).json()
        assert body["ok"] is True
        assert body["data"]["integrity"]["result"] == "pass"

    # -- 三重反向验证（施工卡 §3.2）----------------------------------------

    def test_detects_tampering_and_switches_message(self, client: TestClient, db_path: Path):
        """★ 集成层反向验证：篡改后 `integrity.result == "fail"`，
        **且 message 必须换口径**（这时还说"报告已生成"是误导），
        并且 `action_required` 要带出来。

        注意这段手法与 `test_selfcheck.py` 里那条一致：
        **先 `DROP TRIGGER`** 才能改 —— 否则 append-only 触发器会
        直接拒绝 UPDATE。这正是"模拟离线攻击者"的正确姿势。
        """
        _seed_audit(db_path, 3)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
            conn.execute("UPDATE audit_log SET operator = '攻击者' WHERE log_id = 2")
            conn.commit()
        finally:
            conn.close()

        body = client.post(f"{API}/audit/export", json={}).json()
        # ok 仍为 True：报告本身是有效且必要的（里面带着问题清单），
        # 用户需要**看到**哪一行坏了，而不是只看到一个错误码。
        assert body["ok"] is True
        assert body["data"]["integrity"]["result"] == "fail"
        assert body["data"]["integrity"]["problems"]
        assert "action_required" in body["data"]
        assert "停止使用" in body["data"]["action_required"]
        # 口径切换：不能再说"报告已生成"
        assert "未通过" in body["message"]

    def test_preserves_valid_chain_when_untampered(self, client: TestClient, db_path: Path):
        """正向对照：同一段代码在**未篡改**的库上必须报 pass。

        没有这一条，"能检出篡改"就无法排除"永远报 fail"这个假阳性。
        """
        _seed_audit(db_path, 3)
        data = client.post(f"{API}/audit/export", json={}).json()["data"]
        assert data["integrity"]["result"] == "pass"
        assert data["integrity"]["problems"] == []

    def test_audit_export_itself_is_not_logged_as_failure(self, client: TestClient, db_path: Path):
        """导出报告是**只读**操作，不该往审计里写东西。"""
        from mmwf.infra.audit import AuditQuery, query

        _seed_audit(db_path, 3)
        before = len(query(db_path, AuditQuery()))
        client.post(f"{API}/audit/export", json={})
        assert len(query(db_path, AuditQuery())) == before

    def test_never_500(self, client: TestClient):
        assert client.post(f"{API}/audit/export", json={}).status_code == 200


def test_real_gate_summary_search_and_export(gate_env):
    """C3：真实审批的完整摘要可检索、导出，正文与自由备注不外泄。"""
    e = gate_env
    response = e.client.post(e.base + "/gate/G2/decide", json={
        "checkpoint_id": e.pending["checkpoint_id"], "decision": "edit", "operator": "刘秘书",
        "payload": {"topics": e.pending["payload"]["topics"]}, "comment": "不进入审计的自由备注",
    }).json()
    assert response["ok"], response
    result = e.client.get(f"{API}/audit/logs", params={"operator": "刘秘书", "action": "gate_decision",
        "target_id": e.mid, "from_ts": "2020-01-01T00:00:00Z", "to_ts": "2099-01-01T00:00:00Z"}).json()["data"]
    assert result["returned"] == 1
    record = result["records"][0]
    detail = record["detail"]
    assert detail["changed_keys"] == [] and detail["changed"] == {}
    assert detail["auto"] is False and detail["comment_present"] is True
    exported = e.client.post(f"{API}/audit/export", json={}).json()["data"]
    assert exported["integrity"]["result"] == "pass"
    found = [r for r in exported["records"] if r["log_id"] == record["log_id"]]
    assert found[0]["detail"] == detail
    assert "不进入审计的自由备注" not in str(exported)
