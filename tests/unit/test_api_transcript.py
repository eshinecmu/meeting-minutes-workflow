"""语音稿 4 端点单测（T1-15）。

这 4 个端点是对 T1-14 已建好的 `transcript_store` / `ingest` 的**接线**，
仓储层本身已有 67 个单测覆盖。所以这里的重点是**接线契约**与
**几处"静默失效"的语义**：

  ① 受保护 + 信封形状
  ② 分页数学：`page.total` 必须是**全篇**段数（不是本页条数）——
     写成 `len(segments)` 会让前端以为"看完了"
  ③ 截断必须**显式告知**（`truncated` + 可读的 `message`）
  ④ `counts` 的 5 个键原样透传，`no_confidence` 不被并进 `low_confidence`
  ⑤ `PATCH`：`changed=False` 是 200 不是错误；`flags` 的 `None` 与 `[]`
     语义不同；**`flags` 传字符串必须被拦下**（否则逐字符入库）
  ⑥ 错误码：不存在 → E1002、参数不合法 → E1001，**都不许是 E9001**
  ⑦ `accept`：两条判据（分数 + flags）、`threshold` 越界拦截
  ⑧ `audio`：批量为空 → E1001；全批失败仍回 200（尽力而为语义）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmwf.api import deps as api_deps

API = "/api/v1"

MEETING = "m-t115"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_appdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path):
    """指向临时目录的配置（理由见 `test_api.py` 的同名 fixture）。"""
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

    c = TestClient(create_app(settings), client=("127.0.0.1", 51234),
                   raise_server_exceptions=False)
    c.headers.update({"X-MMWF-Token": api_deps.create_session_token()})
    return c


@pytest.fixture
def anon_client(settings, isolated_appdir, db_path) -> TestClient:
    from mmwf.main import create_app

    return TestClient(create_app(settings), client=("127.0.0.1", 51234))


@pytest.fixture
def meeting(db_path: Path) -> str:
    """造一个会议行 —— 所有端点都要先过 `_require_meeting`。"""
    from mmwf.infra.db import connect_ctx

    with connect_ctx(db_path) as conn:
        conn.execute(
            "INSERT INTO meeting (meeting_id, title, classification, status,"
            " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (MEETING, "语音稿测试会", "internal", "transcript_ready",
             "tester", "2026-09-29T00:00:00Z", "2026-09-29T00:00:00Z"),
        )
        conn.commit()
    return MEETING


def _seed(db_path: Path, meeting_id: str, segments: list[dict]) -> None:
    """用仓储层造分段（不经过 API —— 端点测的是"读"，不是"写"）。"""
    from mmwf.infra.transcript_store import make_transcript_store

    make_transcript_store(meeting_id, db_path).replace_segments(segments)


def _segs(n: int, *, confidence: float | None = 0.9, flags=None) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        item = {
            "seq": i,
            "start_ms": i * 1000,
            "end_ms": i * 1000 + 900,
            "text": f"第{i}句",
            "confidence": confidence,
        }
        if flags is not None:
            item["flags"] = flags
        out.append(item)
    return out


def _audits(db_path: Path, action: str = "segment_edit") -> list:
    from mmwf.infra.audit import AuditQuery, query

    return query(db_path, AuditQuery(action=action))


def _no_internal_error(r) -> None:
    """断言"没把内部错误暴露出去"。

    ⚠️ 只禁 `Traceback` / `site-packages` / `File "`，**不禁** `row_hash`
    这类字段名 —— 它们可能是合法的证据内容（T1-14 记下的教训）。
    """
    assert "Traceback" not in r.text, r.text[:500]
    assert "site-packages" not in r.text, r.text[:500]
    assert 'File "' not in r.text, r.text[:500]


# ---------------------------------------------------------------------------
# ① 受保护
# ---------------------------------------------------------------------------

class TestProtected:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", f"{API}/meetings/m-x/transcript"),
            ("PATCH", f"{API}/segments/m-x:00001"),
            ("POST", f"{API}/meetings/m-x/review/accept"),
            ("POST", f"{API}/meetings/m-x/audio"),
        ],
    )
    def test_rejects_without_token(self, anon_client: TestClient, method: str, path: str):
        body = anon_client.request(method, path, json={}).json()
        assert body["ok"] is False
        assert body["code"] == "E6001"

    def test_failure_envelope_has_trace_id(self, anon_client: TestClient):
        body = anon_client.get(f"{API}/meetings/m-x/transcript").json()
        assert set(body) == {"ok", "code", "message", "data", "trace_id"}
        assert body["trace_id"]


# ---------------------------------------------------------------------------
# ② GET /meetings/{id}/transcript —— 分页、截断、计数
# ---------------------------------------------------------------------------

class TestGetTranscript:
    def test_empty_meeting_is_200_not_error(self, client, meeting):
        """会议刚建、还没转写 —— 这是**正常的中间态**，不该弹红框。"""
        r = client.get(f"{API}/meetings/{meeting}/transcript")
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert body["data"]["segments"] == []
        assert body["data"]["counts"]["total"] == 0
        assert body["data"]["truncated"] is False
        assert "尚未生成" in body["message"]

    def test_missing_meeting_is_e1002_not_e9001(self, client):
        r = client.get(f"{API}/meetings/m-nope/transcript")
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "E1002", f"实际 {body['code']}"
        assert "会议" in body["message"]
        _no_internal_error(r)

    def test_page_total_is_whole_count_not_page_len(self, client, meeting, db_path):
        """★ **本端点最容易写错的一条**。

        `page.total` 若写成 `len(segments)`，前端会算出
        `has_more=false` → 用户以为看完了全篇。这是"静默少显示"。
        """
        _seed(db_path, meeting, _segs(12))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"limit": 5}).json()["data"]
        assert len(body["segments"]) == 5
        assert body["page"]["total"] == 12, "page.total 是**全篇**段数，不是本页条数"
        assert body["page"]["returned"] == 5
        assert body["page"]["has_more"] is True
        assert body["truncated"] is True

    def test_truncation_message_names_the_total(self, client, meeting, db_path):
        """截断必须**说得出口**：告诉用户总共多少段、还差多少段。"""
        _seed(db_path, meeting, _segs(12))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"limit": 5}).json()
        assert "12" in body["message"], f"message 没报总数：{body['message']!r}"
        assert "7" in body["message"], f"message 没报未显示数：{body['message']!r}"

    def test_last_page_is_not_truncated(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(12))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"limit": 5, "offset": 10}).json()["data"]
        assert [s["seq"] for s in body["segments"]] == [11, 12]
        assert body["page"]["has_more"] is False
        assert body["truncated"] is False

    def test_segments_ordered_by_seq(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(8))
        body = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]
        seqs = [s["seq"] for s in body["segments"]]
        assert seqs == sorted(seqs) == list(range(1, 9))

    def test_segment_dict_shape(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(1))
        seg = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]["segments"][0]
        for key in ("segment_id", "meeting_id", "seq", "start_ms", "end_ms",
                    "duration_ms", "text", "confidence", "flags", "review_flags",
                    "reviewed", "version"):
            assert key in seg, f"缺字段 {key}"

    def test_limit_clamped_to_max(self, client, meeting, db_path):
        """要 999999 段不能真的给 —— 那是个"把全库拉进内存"的口子。"""
        _seed(db_path, meeting, _segs(3))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"limit": 999999}).json()["data"]
        assert body["page"]["limit"] == 2000

    def test_limit_zero_clamped_to_one(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(3))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"limit": 0}).json()["data"]
        assert body["page"]["limit"] == 1
        assert len(body["segments"]) == 1

    def test_negative_offset_clamped(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(3))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"offset": -5}).json()["data"]
        assert body["page"]["offset"] == 0
        assert [s["seq"] for s in body["segments"]] == [1, 2, 3]

    def test_seq_range_filter(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(10))
        body = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"min_seq": 3, "max_seq": 5}).json()["data"]
        assert [s["seq"] for s in body["segments"]] == [3, 4, 5]

    def test_counts_keys_preserved_and_not_merged(self, client, meeting, db_path):
        """★ `no_confidence` 必须**单独**在。

        把它并进 `low_confidence`，那个数就失去意义（"低置信 121" 变成
        "低置信 + 干脆没分"，用户无法判断要不要整体人工过）。
        """
        segs = _segs(4, confidence=0.5)          # 4 段低置信
        segs += _segs(3, confidence=None)        # 3 段无置信（seq 会撞，重编）
        for i, s in enumerate(segs, start=1):
            s["seq"] = i
        _seed(db_path, meeting, segs)
        data = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]
        counts = data["counts"]
        assert set(counts) == {"total", "reviewed", "pending",
                               "low_confidence", "no_confidence"}
        assert counts["total"] == 7
        assert counts["low_confidence"] == 4, "无置信的 3 段**不能**算进低置信"
        assert counts["no_confidence"] == 3

    def test_flags_flag_appears_in_review_flags(self, client, meeting, db_path):
        """被标 `silence` 的段即使分数很高也要进人工 —— 界面上要看得见。"""
        segs = _segs(2)                      # 高分无标记
        segs[0]["flags"] = ["silence"]
        _seed(db_path, meeting, segs)
        got = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]["segments"]
        assert "silence" in got[0]["review_flags"]
        assert got[1]["review_flags"] == []

    def test_only_review_needed_filters(self, client, meeting, db_path):
        segs = _segs(6)
        for i in (1, 2):
            segs[i - 1]["flags"] = ["overlap"]
        _seed(db_path, meeting, segs)
        data = client.get(f"{API}/meetings/{meeting}/transcript",
                          params={"only_review_needed": True}).json()["data"]
        assert len(data["segments"]) == 2
        assert "筛选" in client.get(
            f"{API}/meetings/{meeting}/transcript",
            params={"only_review_needed": True}).json()["message"]

    def test_bad_limit_type_is_not_e9001(self, client, meeting, db_path):
        """`limit=abc` —— FastAPI 的类型校验会拦下，**不能**变成 E9001。"""
        _seed(db_path, meeting, _segs(3))
        r = client.get(f"{API}/meetings/{meeting}/transcript", params={"limit": "abc"})
        assert r.status_code in (200, 422)
        _no_internal_error(r)
        if r.status_code == 200:
            assert r.json().get("code") != "E9001"


# ---------------------------------------------------------------------------
# ③ PATCH /segments/{seg_id} —— 就地编辑
# ---------------------------------------------------------------------------

class TestEditSegment:
    def test_edit_changes_text_and_bumps_version(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(3))
        sid = f"{meeting}:00002"
        data = client.patch(f"{API}/segments/{sid}",
                            json={"text": "改过的第二句", "operator": "张三"}).json()["data"]
        assert data["changed"] is True
        assert data["before_text"] == "第2句"
        assert data["segment"]["text"] == "改过的第二句"
        assert data["segment"]["version"] == 2
        assert data["segment"]["reviewed"] is True
        assert data["segment"]["reviewed_by"] == "张三"

    def test_edit_writes_segment_edit_audit(self, client, meeting, db_path):
        """★ AUD-08 红线：逐段改动**必须**留痕。"""
        _seed(db_path, meeting, _segs(3))
        client.patch(f"{API}/segments/{meeting}:00002",
                     json={"text": "新文", "operator": "张三"})
        rows = _audits(db_path)
        assert len(rows) == 1
        assert rows[0].action == "segment_edit"
        assert rows[0].operator == "张三"

    def test_audit_detail_never_contains_transcript_text(self, client, meeting, db_path):
        """★ AUD-05：审计 detail **不含转写稿原文**，只记长度。"""
        _seed(db_path, meeting, _segs(2))
        secret = "这是一句不该出现在审计里的原话"
        client.patch(f"{API}/segments/{meeting}:00002", json={"text": secret})
        rows = _audits(db_path)
        raw = json.dumps(rows[0].detail, ensure_ascii=False)
        assert secret not in raw, f"审计 detail 泄漏了正文：{raw}"
        assert "after_len" in rows[0].detail

    def test_unchanged_text_is_200_not_error(self, client, meeting, db_path):
        """★ 看到原文没动就保存 —— 极常见，必须是 200 + `changed=False`。"""
        _seed(db_path, meeting, _segs(2))
        r = client.patch(f"{API}/segments/{meeting}:00001", json={"text": "第1句"})
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert body["data"]["changed"] is False
        assert body["data"]["warnings"], "无改动仍要告知用户『仍记了一次校订』"

    def test_empty_text_is_e1001_not_e9001(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(2))
        r = client.patch(f"{API}/segments/{meeting}:00001", json={"text": "   "})
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "E1001", f"实际 {body['code']}"
        assert "不能为空" in body["message"]
        _no_internal_error(r)

    def test_missing_text_key_is_e1001(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(2))
        body = client.patch(f"{API}/segments/{meeting}:00001", json={}).json()
        assert body["code"] == "E1001"

    def test_missing_segment_is_e1002_with_segment_wording(self, client, meeting, db_path):
        """★ 错误文案必须是**分段**的话，不能是 E1002 默认的『找不到文件』。

        实测踩到过：`FileMissingError` 会吞掉 `user_message`，
        于是用户看到「找不到要处理的文件」—— 而这里根本没有文件。
        """
        _seed(db_path, meeting, _segs(2))
        r = client.patch(f"{API}/segments/{meeting}:99999", json={"text": "x"})
        body = r.json()
        assert body["code"] == "E1002", f"实际 {body['code']}"
        assert "分段" in body["message"] or "转写稿" in body["message"], (
            f"文案指错方向：{body['message']!r}"
        )
        _no_internal_error(r)

    def test_missing_segment_id_in_other_meeting_is_e1002(self, client, meeting, db_path):
        """不存在的会议号 + 不存在的段号 → 仍是 E1002。"""
        body = client.patch(f"{API}/segments/m-nope:00001", json={"text": "x"}).json()
        assert body["code"] == "E1002"

    # -- flags 的三态 ------------------------------------------------------

    def test_flags_none_does_not_touch_existing(self, client, meeting, db_path):
        """★ `flags` 不传 = **不动**原有标记。

        如果端点写成 `payload.get("flags") or []`，这一条就会变成"清空"，
        静默抹掉 `silence` —— 于是两人抢话的废数据从此"看起来没问题"。
        """
        segs = _segs(1)
        segs[0]["flags"] = ["silence"]
        _seed(db_path, meeting, segs)
        data = client.patch(f"{API}/segments/{meeting}:00001",
                            json={"text": "改过"}).json()["data"]
        assert data["segment"]["flags"] == ["silence"], "不传 flags 不该改动它"

    def test_flags_empty_list_clears(self, client, meeting, db_path):
        """`flags: []` = **清空**（与"不传"不是一个语义）。"""
        segs = _segs(1)
        segs[0]["flags"] = ["silence"]
        _seed(db_path, meeting, segs)
        data = client.patch(f"{API}/segments/{meeting}:00001",
                            json={"text": "改过", "flags": []}).json()["data"]
        assert data["segment"]["flags"] == []

    def test_flags_list_replaces(self, client, meeting, db_path):
        segs = _segs(1)
        segs[0]["flags"] = ["silence"]
        _seed(db_path, meeting, segs)
        data = client.patch(f"{API}/segments/{meeting}:00001",
                            json={"text": "改过", "flags": ["overlap"]}).json()["data"]
        assert data["segment"]["flags"] == ["overlap"]

    @pytest.mark.parametrize("bad", ["silence", "overlap", 123, {"a": 1}])
    def test_flags_wrong_type_is_e1001_not_silent(self, client, meeting, db_path, bad):
        """★★ **本卡抓到的一个真缺陷**。

        `flags="silence"`（用户以为"给个标记名"就够）在原来会走到
        `_dump_flags()` 的 `for f in flags` —— **字符串是可迭代的** ——
        静默存成 `["s","i","l","e","n","c","e"]`：

          · 不报错、不告警；
          · 那一行的标记从此谁也不匹配（`REVIEW_FLAGS` 没有单个字母）；
          · 后果是"两人抢话"的分段**看起来干干净净**，复核直接放过。

        所以类型不对**必须**报 E1001。
        """
        _seed(db_path, meeting, _segs(1))
        r = client.patch(f"{API}/segments/{meeting}:00001",
                         json={"text": "改过", "flags": bad})
        body = r.json()
        assert body["ok"] is False, f"flags={bad!r} 被静默接受了"
        assert body["code"] == "E1001", f"实际 {body['code']}"
        _no_internal_error(r)

    def test_flags_string_does_not_corrupt_db(self, client, meeting, db_path):
        """反证：被拦下之后，库里那一行**没有**被拆成单字母。"""
        segs = _segs(1)
        segs[0]["flags"] = ["silence"]
        _seed(db_path, meeting, segs)
        client.patch(f"{API}/segments/{meeting}:00001",
                     json={"text": "改过", "flags": "overlap"})
        got = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]["segments"]
        assert got[0]["flags"] == ["silence"], f"被污染成 {got[0]['flags']}"

    def test_no_operator_defaults_to_system(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(1))
        client.patch(f"{API}/segments/{meeting}:00001", json={"text": "改"})
        rows = _audits(db_path)
        assert rows[0].operator == "system"

    def test_edit_does_not_touch_other_segments(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(4))
        client.patch(f"{API}/segments/{meeting}:00002", json={"text": "只改这段"})
        segs = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]["segments"]
        texts = [s["text"] for s in segs]
        assert texts == ["第1句", "只改这段", "第3句", "第4句"]

    def test_edit_marks_reviewed(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(3))
        client.patch(f"{API}/segments/{meeting}:00001", json={"text": "改了"})
        counts = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]["counts"]
        assert counts["reviewed"] == 1
        assert counts["pending"] == 2


# ---------------------------------------------------------------------------
# ④ POST /meetings/{id}/review/accept —— 批量接受
# ---------------------------------------------------------------------------

class TestAcceptReview:
    def _mixed(self, db_path, meeting):
        """造一组覆盖四种情形的分段。"""
        segs = []
        # 1-3：高分无标记 → 应被接受
        for i in (1, 2, 3):
            segs.append({"seq": i, "start_ms": i, "end_ms": i, "text": f"高{i}",
                         "confidence": 0.92})
        # 4：低分 → 保留
        segs.append({"seq": 4, "start_ms": 4, "end_ms": 4, "text": "低",
                     "confidence": 0.4})
        # 5：高分但带 overlap → 保留（**第二条判据的价值**）
        segs.append({"seq": 5, "start_ms": 5, "end_ms": 5, "text": "抢话",
                     "confidence": 0.98, "flags": ["overlap"]})
        _seed(db_path, meeting, segs)

    def test_only_high_confidence_without_flags_accepted(self, client, meeting, db_path):
        """★ 两条判据都要满足。只看分数会让"两人抢话"的高分废数据被放过。"""
        self._mixed(db_path, meeting)
        data = client.post(f"{API}/meetings/{meeting}/review/accept").json()["data"]
        assert data["accepted"] == 3
        assert data["skipped_low_confidence"] == 2, "低分 1 段 + 带 overlap 1 段"
        assert sorted(data["segment_ids"]) == [
            f"{meeting}:00001", f"{meeting}:00002", f"{meeting}:00003"]

    def test_high_confidence_with_silence_is_kept(self, client, meeting, db_path):
        """高分 + `silence` 也要留 —— 静音段不该进记录稿正文。"""
        _seed(db_path, meeting, [
            {"seq": 1, "start_ms": 1, "end_ms": 1, "text": "静", "confidence": 0.99,
             "flags": ["silence"]},
        ])
        data = client.post(f"{API}/meetings/{meeting}/review/accept").json()["data"]
        assert data["accepted"] == 0
        assert data["skipped_low_confidence"] == 1

    def test_already_reviewed_skipped(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(2))
        client.patch(f"{API}/segments/{meeting}:00001", json={"text": "已改"})
        data = client.post(f"{API}/meetings/{meeting}/review/accept").json()["data"]
        assert data["skipped_reviewed"] == 1
        assert data["accepted"] == 1

    def test_no_confidence_is_not_accepted(self, client, meeting, db_path):
        """`confidence=None`（引擎没给分）**必须**人工过。

        `None >= 0.85` 在 Python 里会 `TypeError`；若代码用
        `or 0.0` 兜底则恒为假 → 保留，恰好也是对的。但两者动机不同，
        这里锁住"行为正确"。
        """
        _seed(db_path, meeting, _segs(2, confidence=None))
        data = client.post(f"{API}/meetings/{meeting}/review/accept").json()["data"]
        assert data["accepted"] == 0
        assert data["skipped_low_confidence"] == 2

    def test_message_reports_three_numbers(self, client, meeting, db_path):
        """★ 只报"接受了 N 段"会让用户以为剩下的是"系统忘了处理"。"""
        self._mixed(db_path, meeting)
        msg = client.post(f"{API}/meetings/{meeting}/review/accept").json()["message"]
        assert "3" in msg and "2" in msg, f"没报全三个数：{msg!r}"

    def test_message_reports_when_nothing_acceptable(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(2, confidence=0.1))
        body = client.post(f"{API}/meetings/{meeting}/review/accept").json()
        assert "没有可接受" in body["message"]

    def test_accept_writes_single_audit(self, client, meeting, db_path):
        """★ 批量接受**只写一条**审计（不是每段一条）。

        300 段写 300 条会把审计冲垮，真正需要留意的逐段改动会被淹没。
        """
        _seed(db_path, meeting, _segs(20))
        client.post(f"{API}/meetings/{meeting}/review/accept", json={"operator": "张三"})
        rows = _audits(db_path)
        assert len(rows) == 1, f"批量接受写了 {len(rows)} 条审计"
        assert rows[0].detail["op"] == "accept_high_confidence"
        assert rows[0].detail["accepted"] == 20
        assert rows[0].operator == "张三"

    def test_accept_actually_marks_reviewed(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(5))
        client.post(f"{API}/meetings/{meeting}/review/accept")
        counts = client.get(f"{API}/meetings/{meeting}/transcript").json()["data"]["counts"]
        assert counts["reviewed"] == 5
        assert counts["pending"] == 0

    def test_segment_ids_limit(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(5))
        data = client.post(f"{API}/meetings/{meeting}/review/accept",
                           json={"segment_ids": [f"{meeting}:00002"]}).json()["data"]
        assert data["accepted"] == 1
        assert data["segment_ids"] == [f"{meeting}:00002"]

    def test_empty_segment_ids_is_not_error(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(3))
        r = client.post(f"{API}/meetings/{meeting}/review/accept",
                        json={"segment_ids": []})
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert body["data"]["accepted"] == 0
        _no_internal_error(r)

    def test_custom_threshold_respected(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(3, confidence=0.8))
        low = client.post(f"{API}/meetings/{meeting}/review/accept",
                          json={"threshold": 0.9}).json()["data"]
        assert low["accepted"] == 0
        high = client.post(f"{API}/meetings/{meeting}/review/accept",
                           json={"threshold": 0.7}).json()["data"]
        assert high["accepted"] == 3

    @pytest.mark.parametrize("bad", [0, -0.5, 1.5, 2])
    def test_threshold_out_of_range_is_e1001(self, client, meeting, db_path, bad):
        """★ `threshold=0` 会让**整篇**被接受（`>= 0` 恒真）——
        这是"参数静默失效"型缺陷，界面不报错，复核直接形同虚设。
        """
        _seed(db_path, meeting, _segs(3, confidence=0.1))
        r = client.post(f"{API}/meetings/{meeting}/review/accept",
                        json={"threshold": bad})
        body = r.json()
        assert body["ok"] is False, f"threshold={bad} 被接受了"
        assert body["code"] == "E1001"
        _no_internal_error(r)

    def test_threshold_boundary_one_is_ok(self, client, meeting, db_path):
        _seed(db_path, meeting, _segs(2, confidence=1.0))
        body = client.post(f"{API}/meetings/{meeting}/review/accept",
                           json={"threshold": 1}).json()
        assert body["ok"] is True
        assert body["data"]["accepted"] == 2

    @pytest.mark.parametrize("bad", ["abc", None, [], {}])
    def test_threshold_wrong_type_is_e1001(self, client, meeting, db_path, bad):
        _seed(db_path, meeting, _segs(2))
        r = client.post(f"{API}/meetings/{meeting}/review/accept",
                        json={"threshold": bad})
        body = r.json()
        assert body["code"] == "E1001", f"threshold={bad!r} → {body['code']}"
        _no_internal_error(r)

    def test_bool_threshold_rejected(self, client, meeting, db_path):
        """`True` 在 Python 里是 `1`（`isinstance(True, int)` 为真）。

        不显式排掉，`threshold: true` 会被当成 1.0 静默生效。
        """
        _seed(db_path, meeting, _segs(2, confidence=1.0))
        body = client.post(f"{API}/meetings/{meeting}/review/accept",
                           json={"threshold": True}).json()
        assert body["code"] == "E1001", "布尔被当成数字静默接受了"

    @pytest.mark.parametrize("bad", ["nope", 123, {"a": 1}])
    def test_segment_ids_wrong_type_is_e1001(self, client, meeting, db_path, bad):
        _seed(db_path, meeting, _segs(2))
        r = client.post(f"{API}/meetings/{meeting}/review/accept",
                        json={"segment_ids": bad})
        body = r.json()
        assert body["code"] == "E1001", f"segment_ids={bad!r} → {body['code']}"
        _no_internal_error(r)

    def test_missing_meeting_is_e1002(self, client):
        body = client.post(f"{API}/meetings/m-nope/review/accept").json()
        assert body["code"] == "E1002"

    def test_empty_meeting_accepts_zero(self, client, meeting, db_path):
        r = client.post(f"{API}/meetings/{meeting}/review/accept")
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert body["data"]["accepted"] == 0


# ---------------------------------------------------------------------------
# ⑤ POST /meetings/{id}/audio —— 追加音频
# ---------------------------------------------------------------------------

class TestAppendAudio:
    def test_empty_audio_paths_is_e1001(self, client, meeting):
        r = client.post(f"{API}/meetings/{meeting}/audio", json={"audio_paths": []})
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "E1001"
        assert "音频" in body["message"]
        _no_internal_error(r)

    def test_missing_audio_paths_key_is_e1001(self, client, meeting):
        r = client.post(f"{API}/meetings/{meeting}/audio", json={})
        assert r.json()["code"] == "E1001"
        _no_internal_error(r)

    @pytest.mark.parametrize("bad", ["D:/a.mp3", 123, {"p": "x"}])
    def test_audio_paths_wrong_type_is_e1001(self, client, meeting, bad):
        r = client.post(f"{API}/meetings/{meeting}/audio", json={"audio_paths": bad})
        assert r.json()["code"] == "E1001"
        _no_internal_error(r)

    def test_audio_paths_with_blank_item_is_e1001(self, client, meeting):
        r = client.post(f"{API}/meetings/{meeting}/audio", json={"audio_paths": ["  "]})
        assert r.json()["code"] == "E1001"
        _no_internal_error(r)

    def test_nonexistent_file_batch_still_200(self, client, meeting):
        """★ **全批失败也回 200**（`ok: true`）。

        批量入口的语义是"尽力而为"。回 4xx 会让前端把"一个文件坏了"
        显示成"这次操作失败"，而实际上别的文件可能已经成功落盘了。
        """
        r = client.post(f"{API}/meetings/{meeting}/audio",
                        json={"audio_paths": ["D:/nope/does-not-exist.mp3"]})
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is True
        assert body["data"]["succeeded"] == 0
        assert body["data"]["failed"] == 1
        assert body["data"]["audio"][0]["filename"] == "does-not-exist.mp3"
        _no_internal_error(r)

    def test_mixed_batch_reports_both(self, client, meeting):
        """一个坏文件不该拖垮另一个 —— 逐条结果都要给出来。"""
        r = client.post(f"{API}/meetings/{meeting}/audio",
                        json={"audio_paths": ["D:/nope/a.mp3", "D:/nope/b.mp3"]})
        data = r.json()["data"]
        assert data["succeeded"] + data["failed"] == 2
        assert len(data["audio"]) == 2

    def test_missing_meeting_is_e1002_and_does_not_ingest(self, client):
        r = client.post(f"{API}/meetings/m-nope/audio",
                        json={"audio_paths": ["D:/nope/a.mp3"]})
        assert r.json()["code"] == "E1002"
        _no_internal_error(r)

    def test_real_audio_via_injected_adapter(self, client, meeting, settings, monkeypatch):
        """真跑一次接入流程（用替身 adapter，不依赖本机装 ffmpeg）。"""
        import mmwf.api.routes as R

        wav = Path(settings.resolve_path(settings.app.data_dir)) / "fake.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF" + b"\x00" * 200)

        class _Tools:
            ffmpeg = "ffmpeg"
            ffprobe = "ffprobe"

        class _Adapter:
            tools = _Tools()

            def probe_duration_ms(self, src):
                return 90_000

            def cut_chunk(self, src, dst, start_ms, end_ms):
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(b"x")

        from mmwf.app.services.ingest import make_ingest_service
        import mmwf.app.services.ingest as ingest_mod
        monkeypatch.setattr(
            ingest_mod, "FFmpegAdapter", lambda *a, **k: _Adapter(), raising=False
        )
        monkeypatch.setattr(
            R, "_ingest_service",
            lambda mid: make_ingest_service(settings, adapter=_Adapter()),
            raising=False,
        )

        r = client.post(f"{API}/meetings/{meeting}/audio",
                        json={"audio_paths": [str(wav)]})
        body = r.json()
        assert body["ok"] is True, body
        assert body["data"]["succeeded"] == 1
        assert body["data"]["chunk_count"] > 0
        assert "不会自动转写" in body["message"], (
            f"必须告知能力边界，否则用户以为追加完就自动转写了：{body['message']!r}"
        )

    def test_warning_when_meeting_already_ran(self, client, meeting, db_path):
        """会议已跑过之后追加 → 必须提示"不会自动进已有转写稿"。"""
        from mmwf.infra.db import connect_ctx

        with connect_ctx(db_path) as conn:
            conn.execute("UPDATE meeting SET status = 'G2_awaiting' WHERE meeting_id = ?",
                         (meeting,))
            conn.commit()
        body = client.post(f"{API}/meetings/{meeting}/audio",
                           json={"audio_paths": ["D:/nope/a.mp3"]}).json()
        assert "warning" in body["data"], "已开跑的会议追加音频要给提示"
        assert "重新" in body["data"]["warning"]

    def test_no_warning_for_fresh_meeting(self, client, meeting, db_path):
        """刚建的会议（`status=created`）追加音频**不该**有那条提示。

        ⚠️ 本用例第一版写错了：`meeting` fixture 造的是
        `status="transcript_ready"`，那**本来就该**出提示。
        要验"不出提示"必须先把状态改成 `created` —— 否则测的是一个
        与名字不符的场景，而断言失败看着像产品 bug。
        """
        from mmwf.infra.db import connect_ctx

        with connect_ctx(db_path) as conn:
            conn.execute("UPDATE meeting SET status = 'created' WHERE meeting_id = ?",
                         (meeting,))
            conn.commit()
        body = client.post(f"{API}/meetings/{meeting}/audio",
                           json={"audio_paths": ["D:/nope/a.mp3"]}).json()
        warning = body["data"].get("warning", "")
        assert "重新发起" not in warning, (
            f"status=created 的会议不该有『需要重新发起』提示，实测：{warning!r}"
        )


# ---------------------------------------------------------------------------
# ⑥ 附加 A —— 任何用户填错都不许变成 E9001
# ---------------------------------------------------------------------------

class TestNoInternalErrorForBadInput:
    """★ 沿用 T1-14 的做法。

    E9001（"程序遇到一个未预期的问题"）本该留给**真的**系统故障。
    用户填错一个参数就得到 E9001，等于把排查方向指到完全错误的地方，
    而且用户拿到的追踪号也无意义。

    这一组用例是本卡最可能抓到真缺陷的地方（T1-14 靠它抓到两个）。
    """

    CASES = [
        ("GET", f"{API}/meetings/{{m}}/transcript", None),
        ("GET", f"{API}/meetings/{{m}}/transcript", {"limit": "abc"}),
        ("GET", f"{API}/meetings/{{m}}/transcript", {"limit": -1}),
        ("GET", f"{API}/meetings/{{m}}/transcript", {"offset": "x"}),
        ("GET", f"{API}/meetings/{{m}}/transcript", {"min_seq": "abc"}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {"text": ""}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {"text": 123}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {"text": None}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {"text": "x", "flags": "abc"}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {"text": "x", "flags": 123}),
        ("PATCH", f"{API}/segments/{{m}}:00001", {"text": "x", "flags": {"a": 1}}),
        ("POST", f"{API}/meetings/{{m}}/review/accept", {"threshold": 0}),
        ("POST", f"{API}/meetings/{{m}}/review/accept", {"threshold": "abc"}),
        ("POST", f"{API}/meetings/{{m}}/review/accept", {"threshold": True}),
        ("POST", f"{API}/meetings/{{m}}/review/accept", {"segment_ids": "nope"}),
        ("POST", f"{API}/meetings/{{m}}/review/accept", {"segment_ids": 5}),
        ("POST", f"{API}/meetings/{{m}}/audio", {"audio_paths": []}),
        ("POST", f"{API}/meetings/{{m}}/audio", {"audio_paths": "x"}),
        ("POST", f"{API}/meetings/{{m}}/audio", {"audio_paths": [1, 2]}),
    ]

    @pytest.mark.parametrize("method,path,payload", CASES)
    def test_never_e9001(self, client, meeting, method: str, path: str, payload):
        real = path.format(m=meeting)
        kw = {} if payload is None else {"json": payload}
        if method == "GET":
            kw = {"params": payload} if payload else {}
        r = client.request(method, real, **kw)
        assert r.json().get("code") != "E9001", (
            f"{method} {real} {payload} → E9001（用户填错被报成系统故障）："
            f"{r.json().get('message')}"
        )
        _no_internal_error(r)

    @pytest.mark.parametrize("method,path,payload", CASES)
    def test_no_e9001_for_missing_meeting_either(self, client, method, path, payload):
        """会议都不存在时也一样 —— 不许 E9001。"""
        real = path.format(m="m-does-not-exist")
        kw = {} if payload is None else {"json": payload}
        if method == "GET":
            kw = {"params": payload} if payload else {}
        r = client.request(method, real, **kw)
        assert r.json().get("code") != "E9001", f"{method} {real} → E9001"
        _no_internal_error(r)
