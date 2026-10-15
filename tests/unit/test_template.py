"""模板领域层与仓储层单测（T1-16）。

覆盖两件事：

  A. **`domain/template.py` 的纯逻辑** —— 两种输入形状（yaml 的 `meta` 层 /
     API 的扁平体）、必填要素与 section_order 的校验、重复 key 拦截。

  B. **`infra/template_store.py` 的合并与写入** —— 内置优先、版本自增、
     内置不可改、`config_dir=None` 时不读 yaml。

★ 本文件里最该看的两条：

  ① `test_flat_api_body_parses` —— 第一版 `parse_template` 只认 `meta` 层，
     从 API 建模板时 `name` 报"缺失"。这条用例把它钉住。
  ② `test_builtin_cannot_be_updated` —— 内置不可改必须给 **E1003**，
     不是 E1001，也不是静默成功。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mmwf.domain.template import (
    MAX_NAME_LEN,
    SOURCE_BUILTIN,
    SOURCE_DB,
    TemplateBuiltinError,
    TemplateFormatError,
    parse_template,
)
from mmwf.infra.template_store import make_template_store

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


# ---------------------------------------------------------------------------
# A. 领域层：解析
# ---------------------------------------------------------------------------

def _flat_body(**over) -> dict:
    body = {
        "name": "站会议纪要",
        "meeting_kind": "regular_meeting",
        "required_elements": [{"key": "title", "label": "标题"}],
        "section_order": ["基本情况", "议定事项"],
        "style": {"readings_allowed": ["会议认为"]},
    }
    body.update(over)
    return body


class TestParseFlatBody:
    """★ API 请求体是**扁平**的（没有 `meta` 层）。"""

    def test_flat_api_body_parses(self):
        """★ 回归：第一版只认 `meta` 层，扁平体报 `name 缺失`。"""
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        assert tpl.name == "站会议纪要"
        assert tpl.meeting_kind == "regular_meeting"
        assert tpl.source == SOURCE_DB
        assert tpl.version_no == 1

    def test_flat_body_may_omit_template_id(self):
        """自建模板**可以**没有 id（服务端生成）。"""
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        assert tpl.template_id == ""

    def test_flat_body_with_template_id(self):
        tpl = parse_template(_flat_body(template_id="tpl-x"), source=SOURCE_DB)
        assert tpl.template_id == "tpl-x"

    def test_builtin_requires_template_id(self):
        """内置模板**必须**有 id —— 丢了找不回来。"""
        with pytest.raises(TemplateFormatError) as ei:
            parse_template(_flat_body(), source=SOURCE_BUILTIN)
        assert ei.value.code == "E1001"


class TestParseYamlShape:
    """yaml 是 `meta` 层 + 顶层键。"""

    def _yaml_like(self) -> dict:
        return {
            "meta": {
                "template_id": "tpl-demo-v1",
                "name": "演示纪要",
                "meeting_kind": "party_group",
                "version": 3,
            },
            "required_elements": [{"key": "title", "label": "标题"}],
            "section_order": ["一", "二"],
            "style": {"readings_forbidden": ["差不多"]},
        }

    def test_meta_layer_parses(self):
        tpl = parse_template(self._yaml_like(), source=SOURCE_BUILTIN)
        assert tpl.template_id == "tpl-demo-v1"
        assert tpl.meeting_kind == "party_group"
        assert tpl.version_no == 3

    def test_all_four_builtin_load(self):
        """4 个内置模板都要能解析（施工文档 §T2-02 的四类）。"""
        import yaml

        files = sorted((CONFIG_DIR / "templates").glob("*.yaml"))
        assert len(files) == 4, f"内置模板应是 4 个，实际 {len(files)}"
        kinds = set()
        for p in files:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            tpl = parse_template(data, source=SOURCE_BUILTIN)
            assert tpl.section_order, f"{p.name} 的 section_order 为空"
            assert tpl.required_elements, f"{p.name} 的 required_elements 为空"
            kinds.add(tpl.meeting_kind)
        assert len(kinds) == 4, f"四类会议的 kind 应互不相同，实际 {kinds}"


class TestValidation:
    def test_name_required(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(name="  "), source=SOURCE_DB)

    def test_name_too_long(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(name="字" * (MAX_NAME_LEN + 1)), source=SOURCE_DB)

    def test_meeting_kind_required(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(meeting_kind=""), source=SOURCE_DB)

    def test_required_elements_not_empty(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(required_elements=[]), source=SOURCE_DB)

    def test_required_elements_must_be_list(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(required_elements="title"), source=SOURCE_DB)

    def test_element_needs_label(self):
        """`label` 不能空 —— 界面上要显示它。"""
        with pytest.raises(TemplateFormatError):
            parse_template(
                _flat_body(required_elements=[{"key": "title", "label": " "}]),
                source=SOURCE_DB,
            )

    def test_duplicate_element_key_rejected(self):
        """★ 重复 key 会让"缺失即报"**漏报**（同一 key 查两遍）。静默，必须拦。"""
        with pytest.raises(TemplateFormatError) as ei:
            parse_template(
                _flat_body(required_elements=[
                    {"key": "title", "label": "标题"},
                    {"key": "title", "label": "标题2"},
                ]),
                source=SOURCE_DB,
            )
        assert "重复" in str(ei.value.detail)

    def test_bad_pattern_rejected(self):
        with pytest.raises(TemplateFormatError):
            parse_template(
                _flat_body(required_elements=[
                    {"key": "t", "label": "T", "pattern": "(["},
                ]),
                source=SOURCE_DB,
            )

    def test_min_count_must_be_int(self):
        with pytest.raises(TemplateFormatError):
            parse_template(
                _flat_body(required_elements=[
                    {"key": "t", "label": "T", "min_count": "2"},
                ]),
                source=SOURCE_DB,
            )

    def test_min_count_bool_rejected(self):
        """`True` 是 int 的子类 —— 显式拦掉，否则 min_count=True 变成 1。"""
        with pytest.raises(TemplateFormatError):
            parse_template(
                _flat_body(required_elements=[
                    {"key": "t", "label": "T", "min_count": True},
                ]),
                source=SOURCE_DB,
            )

    def test_section_order_not_empty(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(section_order=[]), source=SOURCE_DB)

    def test_section_order_blank_entry_rejected(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(section_order=["一", "  "]), source=SOURCE_DB)

    def test_style_must_be_mapping(self):
        with pytest.raises(TemplateFormatError):
            parse_template(_flat_body(style=["x"]), source=SOURCE_DB)

    def test_style_optional(self):
        body = _flat_body()
        body.pop("style")
        tpl = parse_template(body, source=SOURCE_DB)
        assert tpl.style == {}

    def test_to_dict_omits_absent_optionals(self):
        """可选字段（pattern/min_count）**没值时不该出现** ——
        否则前端要多一个无用分支。"""
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        el = tpl.to_dict()["required_elements"][0]
        assert "pattern" not in el and "min_count" not in el

    def test_to_dict_has_all_three_keys(self):
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        d = tpl.to_dict()
        for k in ("template_id", "name", "meeting_kind", "source", "version_no"):
            assert k in d


# ---------------------------------------------------------------------------
# B. 仓储层
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    from mmwf.infra.migrations.runner import migrate

    p = tmp_path / "mmwf.db"
    migrate(p)
    return p


@pytest.fixture
def store(db_path: Path):
    return make_template_store(db_path, config_dir=CONFIG_DIR)


class TestStoreRead:
    def test_builtin_loaded(self, store):
        lst, problems = store.list_templates()
        assert problems == []
        assert lst.builtin == 4
        assert lst.db == 0
        assert all(t.source == SOURCE_BUILTIN for t in lst.templates)

    def test_no_config_dir_returns_only_db(self, db_path: Path):
        """`config_dir=None` → 只读表（不读 yaml），便于隔离测试。"""
        st = make_template_store(db_path, config_dir=None)
        lst, _ = st.list_templates()
        assert lst.total == 0 and lst.builtin == 0

    def test_broken_yaml_does_not_break_list(self, db_path: Path, tmp_path: Path):
        """单个 yaml 坏了 → 其余照常返回，坏的那个进 problems。"""
        tdir = tmp_path / "mycfg" / "templates"
        tdir.mkdir(parents=True)
        good = (CONFIG_DIR / "templates" / "regular-meeting.yaml").read_text("utf-8")
        (tdir / "good.yaml").write_text(good, encoding="utf-8")
        (tdir / "bad.yaml").write_text("meta: [\n  broken", encoding="utf-8")
        st = make_template_store(db_path, config_dir=tmp_path / "mycfg")
        lst, problems = st.list_templates()
        assert lst.builtin == 1
        assert len(problems) == 1
        assert "bad.yaml" in problems[0]

    def test_filter_by_meeting_kind(self, store):
        lst, _ = store.list_templates(meeting_kind="party_group")
        assert lst.total == 1
        assert lst.templates[0].meeting_kind == "party_group"

    def test_get_builtin(self, store):
        tpl = store.get("tpl-regular-meeting-v1")
        assert tpl is not None and tpl.is_builtin

    def test_get_missing_returns_none(self, store):
        assert store.get("nope") is None


class TestStoreWrite:
    def test_create_and_list(self, store):
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        tid = store.new_template_id()
        created = store.create(tpl, template_id=tid)
        assert created.template_id == tid
        assert created.version_no == 1
        assert created.source == SOURCE_DB
        lst, _ = store.list_templates()
        assert lst.total == 5 and lst.builtin == 4 and lst.db == 1

    def test_update_increments_version(self, store):
        """★ F7.2：修改后 version 递增。"""
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        tid = store.new_template_id()
        store.create(tpl, template_id=tid)
        v2 = store.update(
            parse_template(_flat_body(name="改过的"), source=SOURCE_DB),
            template_id=tid,
        )
        assert v2.version_no == 2
        v3 = store.update(
            parse_template(_flat_body(name="再改"), source=SOURCE_DB),
            template_id=tid,
        )
        assert v3.version_no == 3
        assert store.get(tid).version_no == 3

    def test_client_supplied_version_is_ignored(self, store):
        """★ 客户端传 version_no=999 必须被忽略（服务端自增）。"""
        tid = store.new_template_id()
        store.create(parse_template(_flat_body(), source=SOURCE_DB), template_id=tid)
        # 请求体里塞 version —— parse_template 会读它，但 update 用表里的值 +1
        body = _flat_body(version=999)
        upd = store.update(parse_template(body, source=SOURCE_DB), template_id=tid)
        assert upd.version_no == 2, f"客户端传的 version 没被忽略：{upd.version_no}"

    def test_builtin_cannot_be_updated(self, store):
        """★ 内置模板不可改 → **E1003**（不是 E1001）。"""
        with pytest.raises(TemplateBuiltinError) as ei:
            store.update(
                parse_template(_flat_body(), source=SOURCE_DB),
                template_id="tpl-regular-meeting-v1",
            )
        assert ei.value.code == "E1003"
        # 表里不该多出一条同 id 的行
        lst, _ = store.list_templates(include_disabled=True)
        assert lst.db == 0

    def test_update_missing_raises_e1001(self, store):
        with pytest.raises(TemplateFormatError) as ei:
            store.update(
                parse_template(_flat_body(), source=SOURCE_DB), template_id="nope"
            )
        assert ei.value.code == "E1001"

    def test_new_template_id_unique(self, store):
        ids = {store.new_template_id() for _ in range(20)}
        assert len(ids) == 20

    def test_new_template_id_avoids_existing(self, store):
        """生成前要**真的检查**现有 id —— 否则偶发 UNIQUE 失败。"""
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        existing = store.new_template_id()
        store.create(tpl, template_id=existing)
        for _ in range(30):
            assert store.new_template_id() != existing

    def test_db_row_shadows_nothing_but_builtin_wins(self, store):
        """自建模板与内置**同名** → 列表以内置为准，自建的被屏蔽。"""
        tpl = parse_template(_flat_body(name="冒名顶替"), source=SOURCE_DB)
        store.create(tpl, template_id="tpl-regular-meeting-v1")
        lst, _ = store.list_templates()
        hit = [t for t in lst.templates if t.template_id == "tpl-regular-meeting-v1"]
        assert len(hit) == 1
        assert hit[0].source == SOURCE_BUILTIN, "内置必须优先"
        assert hit[0].name != "冒名顶替"

    def test_counts_include_disabled(self, store):
        assert store.count() == 4
        assert store.count(include_disabled=False) == 4


# T2-02 B3：旧格式可读，绑定缺失只在应用时拒绝；选择不写库。
@pytest.mark.parametrize("style", [
    {"readings_allowed": "会议认为"}, {"readings_forbidden": None},
    {"readings_allowed": [""]}, {"readings_forbidden": ["  "]},
    {"readings_allowed": [1]},
    {"section_bindings": []}, {"section_bindings": {}},
    {"section_bindings": {"基本情况": "meeting_meta"}},
    {"section_bindings": {"基本情况": "meeting_meta", "议定事项": "unknown"}},
    {"section_bindings": {"基本情况": "meeting_meta", "议定事项": "actions", "多余": "actions"}},
])
def test_style_config_errors(style):
    with pytest.raises(TemplateFormatError) as exc:
        parse_template(_flat_body(style=style), source=SOURCE_DB)
    assert exc.value.code == "E1001"


def test_style_normalization_compatibility_and_bindings():
    template = parse_template(_flat_body(style={"readings_forbidden": ["Ａ Ｂ", "ab", "差 不 多", "差不多"]}), source=SOURCE_DB)
    assert template.style["readings_forbidden"] == ["ab", "差不多"]
    assert "section_bindings" not in template.style
    template = parse_template(_flat_body(style={"section_bindings": {"基本情况": "meeting_meta", "议定事项": "resolutions"}}), source=SOURCE_DB)
    assert template.style["section_bindings"]["议定事项"] == "resolutions"


@pytest.mark.parametrize("elements", [[{"key": "other", "label": "未知"}], [{"key": "title", "label": "标题", "min_count": 1}]])
def test_invalid_required_config(elements):
    with pytest.raises(TemplateFormatError):
        parse_template(_flat_body(required_elements=elements), source=SOURCE_DB)


def test_duplicate_section_titles():
    with pytest.raises(TemplateFormatError):
        parse_template(_flat_body(section_order=["重复", "重复"]), source=SOURCE_DB)


def test_selection(store):
    assert store.select().template_id == "tpl-regular-meeting-v1"
    assert store.select(meeting_kind="party_group").template_id == "tpl-party-group-v1"
    assert store.select("tpl-party-group-v1", meeting_kind="unknown").meeting_kind == "party_group"
    for kwargs in ({"template_id": "unknown"}, {"template_id": ""}, {"meeting_kind": "unknown"}):
        with pytest.raises(TemplateFormatError):
            store.select(**kwargs)
    custom = parse_template(_flat_body(template_id="custom", meeting_kind="unique"), source=SOURCE_DB)
    store.create(custom)
    assert store.select(meeting_kind="unique").template_id == "custom"
    # 与内置同 kind 的自建模板也计入歧义。
    store.create(parse_template(_flat_body(template_id="second", name="第二模板"), source=SOURCE_DB))
    with pytest.raises(TemplateFormatError):
        store.select(meeting_kind="regular_meeting")
    disabled = parse_template(_flat_body(template_id="disabled", name="停用模板", enabled=False, meeting_kind="off"), source=SOURCE_DB)
    store.create(disabled)
    with pytest.raises(TemplateFormatError):
        store.select("disabled")
    with pytest.raises(TemplateFormatError):
        store.select(meeting_kind="off")


def test_hot_binding_order_and_builtin_load_has_no_writes(db_path, tmp_path):
    import shutil
    import yaml
    from mmwf.domain.minutes import apply_template, render_template_markdown
    from mmwf.infra.db import connect_ctx

    config = tmp_path / "config"
    shutil.copytree(CONFIG_DIR / "templates", config / "templates")
    store = make_template_store(db_path, config_dir=config)
    minutes = {"meeting_time": "独有时间", "topics": [{"name": "独有议题", "discussion": "独有讨论"}]}
    first = store.select()
    path = config / "templates" / "regular-meeting.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["section_order"][0], data["section_order"][1] = data["section_order"][1], data["section_order"][0]
    data["meta"]["version"] += 1
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    second = store.select()
    assert second.version_no == first.version_no + 1
    a, b = apply_template(minutes, first), apply_template(minutes, second)
    assert b["sections"][0]["heading"] == a["sections"][1]["heading"]
    assert b["sections"][0]["items"][0]["text"] == "独有讨论"
    md = render_template_markdown(b, [])
    assert md.index("独有讨论") < md.index("独有时间")
    assert first.section_order[0] == data["section_order"][1]
    # builtin 同版热改依旧可读，无历史登记或同版内容锁定。
    data["meta"]["name"] = "同版热改纪要"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert store.select().name == "同版热改纪要"
    with connect_ctx(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM template").fetchone()[0] == 0


class TestDbHistory:
    def test_full_snapshots_survive_reopen(self, store, db_path):
        """POST v1 / 两次 PUT：正文、顺序、规则和启用状态各版完整保留。"""
        from mmwf.infra.db import connect_ctx

        expected = []
        for i in range(1, 4):
            tpl = parse_template(_flat_body(
                name=f"第{i}版", version=999, enabled=i != 2,
                required_elements=[{"key": "attendees", "label": f"出席{i}", "min_count": i}],
                section_order=[f"章节{i}"], style={"readings_allowed": [f"标签{i}"]},
            ), source=SOURCE_DB)
            saved = (store.create(tpl, template_id="history") if i == 1
                     else store.update(tpl, template_id="history"))
            assert saved.version_no == i
            expected.append(saved.to_dict())
        reopened = make_template_store(db_path, config_dir=CONFIG_DIR)
        assert reopened.list_versions("history") == [1, 2, 3]
        assert [reopened.get_version("history", n).to_dict() for n in [1, 2, 3]] == expected
        with connect_ctx(db_path) as conn:
            rows = conn.execute("SELECT snapshot_json, captured_at FROM template_revision ORDER BY version_no").fetchall()
        import json
        assert [json.loads(r[0]) for r in rows] == expected
        assert all(r[1] for r in rows)

    @pytest.mark.parametrize("operation", ["create", "update"])
    def test_snapshot_failure_rolls_back_template_row(self, store, db_path, operation):
        """注入真实 DB 快照写入失败，不能留下半次 POST / PUT。"""
        from mmwf.infra.db import connect_ctx
        import sqlite3

        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        if operation == "update":
            before = store.create(tpl, template_id="rollback").to_dict()
        with connect_ctx(db_path) as conn:
            conn.execute("""CREATE TRIGGER fail_revision BEFORE INSERT ON template_revision
                BEGIN SELECT RAISE(ABORT, '注入快照失败'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="注入快照失败"):
            getattr(store, operation)(parse_template(_flat_body(name="不能留下"), source=SOURCE_DB),
                                      template_id="rollback")
        if operation == "create":
            assert store.get("rollback") is None
            with connect_ctx(db_path) as conn:
                assert conn.execute("SELECT count(*) FROM template_revision").fetchone()[0] == 0
        else:
            assert store.get("rollback").to_dict() == before
            assert store.list_versions("rollback") == [1]
            assert store.get_version("rollback", 1).to_dict() == before

    def test_concurrent_puts_read_version_under_write_lock(self, store):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        store.create(parse_template(_flat_body(), source=SOURCE_DB), template_id="concurrent")
        barrier = Barrier(2)

        def put(name):
            barrier.wait(timeout=5)
            return store.update(parse_template(_flat_body(name=name), source=SOURCE_DB),
                                template_id="concurrent")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(put, ["并发甲", "并发乙"]))
        assert sorted(t.version_no for t in results) == [2, 3]
        assert store.list_versions("concurrent") == [1, 2, 3]
        for tpl in results:
            assert store.get_version("concurrent", tpl.version_no).to_dict() == tpl.to_dict()
        assert store.get("concurrent").to_dict() == store.get_version("concurrent", 3).to_dict()

    def test_migration_only_snapshots_current_and_is_idempotent(self, tmp_path):
        import json
        from mmwf.infra.db import connect_ctx
        from mmwf.infra.migrations.runner import apply_migration, discover_migrations, migrate

        path = tmp_path / "old.db"
        first, second = discover_migrations()
        tpl = parse_template(_flat_body(version=7), source=SOURCE_DB)
        payload = tpl.to_row_payload()
        with connect_ctx(path) as conn:
            apply_migration(conn, first)
            conn.execute("INSERT INTO template VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("old", tpl.name, tpl.meeting_kind, payload["schema_json"],
                          payload["style_json"], 7, 1, "2026-10-14"))
        assert migrate(path) == migrate(path) == 2
        st = make_template_store(path)
        assert st.list_versions("old") == [7]
        assert st.get_version("old", 7).to_dict() == st.get("old").to_dict()
        with connect_ctx(path) as conn:
            original = conn.execute("SELECT * FROM template_revision").fetchall()
            # 即便显式重放 SQL，也不覆盖同版正文或捕获时间。
            apply_migration(conn, second)
            assert [tuple(r) for r in conn.execute("SELECT * FROM template_revision")] == [tuple(r) for r in original]
            assert json.loads(original[0][2]) == st.get("old").to_dict()
        with pytest.raises(TemplateFormatError):
            st.get_version("old", 1)

    def test_builtin_same_version_hot_edit_never_writes_db(self, db_path, tmp_path):
        from mmwf.infra.db import connect_ctx
        from mmwf.domain.errors import InputError

        directory = tmp_path / "cfg" / "templates"
        directory.mkdir(parents=True)
        path = directory / "regular.yaml"
        text = (CONFIG_DIR / "templates" / "regular-meeting.yaml").read_text("utf-8")
        path.write_text(text, encoding="utf-8")
        st = make_template_store(db_path, config_dir=directory.parent)
        before = st.get("tpl-regular-meeting-v1")
        path.write_text(text.replace("会议基本情况", "热改首段"), encoding="utf-8")
        after = st.get("tpl-regular-meeting-v1")
        assert after.version_no == before.version_no
        assert after.section_order != before.section_order
        st.list_templates()
        for query in [lambda: st.list_versions(before.template_id),
                      lambda: st.get_version(before.template_id, 1)]:
            with pytest.raises(InputError, match="内置模板历史由 git 和 meta.version 管理"):
                query()
        with connect_ctx(db_path) as conn:
            assert conn.execute("SELECT count(*) FROM template").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM template_revision").fetchone()[0] == 0

    @pytest.mark.parametrize("version", [0, -1, True, "1", 1.5])
    def test_invalid_version_is_e1001(self, store, version):
        with pytest.raises(TemplateFormatError) as exc:
            store.get_version("missing", version)
        assert exc.value.code == "E1001"

    def test_shadowed_db_snapshot_is_db_content(self, store, db_path):
        from mmwf.infra.db import connect_ctx
        import json

        saved = store.create(parse_template(_flat_body(name="撞名自建"), source=SOURCE_DB),
                             template_id="tpl-regular-meeting-v1")
        assert saved.source == SOURCE_DB and saved.name == "撞名自建"
        with connect_ctx(db_path) as conn:
            data = json.loads(conn.execute("SELECT snapshot_json FROM template_revision").fetchone()[0])
        assert data == saved.to_dict()


    def test_shadowed_builtin_put_is_rejected_and_history_is_unsupported(self, store, db_path):
        """撞名 DB 行不改变 builtin 优先语义，PUT 和历史查询均以 builtin 为准。"""
        from mmwf.domain.errors import InputError
        from mmwf.infra.db import connect_ctx

        tid = "tpl-regular-meeting-v1"
        saved = store.create(parse_template(_flat_body(name="撞名自建"), source=SOURCE_DB),
                             template_id=tid)
        with pytest.raises(TemplateBuiltinError) as exc:
            store.update(parse_template(_flat_body(name="不能改"), source=SOURCE_DB),
                         template_id=tid)
        assert exc.value.code == "E1003"
        assert store.get(tid).is_builtin
        for query in [lambda: store.list_versions(tid), lambda: store.get_version(tid, 1)]:
            with pytest.raises(InputError) as exc:
                query()
            assert exc.value.code == "E1001"
            assert "内置模板历史由 git 和 meta.version 管理" in exc.value.user_message
        # 绕过 builtin 读取 DB，确认拒绝操作没有改变行或历史。
        db_only = make_template_store(db_path)
        assert db_only.get(tid).to_dict() == saved.to_dict()
        assert db_only.list_versions(tid) == [1]
        assert db_only.get_version(tid, 1).to_dict() == saved.to_dict()
        with connect_ctx(db_path) as conn:
            assert conn.execute("SELECT count(*) FROM template_revision").fetchone()[0] == 1

    @pytest.mark.parametrize("missing", ["required_elements", "section_order", "both"])
    def test_migration_preserves_invalid_legacy_schema_without_inventing_content(self, tmp_path, missing):
        """旧坏行仍可迁移；缺失结构保留缺失，领域查询沿用 E1001，不伪造正文。"""
        import json
        from mmwf.infra.db import connect_ctx
        from mmwf.infra.migrations.runner import apply_migration, discover_migrations, migrate

        path = tmp_path / "legacy.db"
        tpl = parse_template(_flat_body(), source=SOURCE_DB)
        payload = tpl.to_row_payload()
        schema = json.loads(payload["schema_json"])
        for key in ["required_elements", "section_order"]:
            if missing in [key, "both"]:
                del schema[key]
        with connect_ctx(path) as conn:
            apply_migration(conn, discover_migrations()[0])
            conn.execute("INSERT INTO template VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("legacy", tpl.name, tpl.meeting_kind, json.dumps(schema),
                          payload["style_json"], 7, 1, "2026-10-14"))
        assert migrate(path) == migrate(path) == 2
        with connect_ctx(path) as conn:
            rows = conn.execute("SELECT version_no, snapshot_json FROM template_revision").fetchall()
        assert len(rows) == 1 and rows[0][0] == 7
        snapshot = json.loads(rows[0][1])
        for key in ["required_elements", "section_order"]:
            assert snapshot[key] == schema.get(key)
        store = make_template_store(path)
        for query in [lambda: store.get("legacy"), lambda: store.get_version("legacy", 7)]:
            with pytest.raises(TemplateFormatError) as exc:
                query()
            assert exc.value.code == "E1001"
