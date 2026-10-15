"""T2-02 离线验收：逐项验证施工卡 §4；仅写临时模板副本和临时库。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import logging
import os
import re
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml

from mmwf.domain.extraction.schemas import MinutesSchema
from mmwf.domain.minutes import (
    apply_template, check_template, check_required_elements,
    check_forbidden_expressions, check_reading_levels,
    from_graph_payload, render_template_markdown,
)
from mmwf.domain.template import parse_template, TemplateFormatError
from mmwf.graph import state as S
from mmwf.graph.nodes.extraction import make_generate, make_validate, make_assemble
from mmwf.infra.db import connect_ctx
from mmwf.infra.migrations.runner import migrate, apply_migration, discover_migrations
from mmwf.infra.template_store import TemplateStore

NAMES = ("四模板成文", "必填规则", "禁用表达", "外改即生效", "DB 历史与事务", "领读语层级", "节点接缝与状态", "编号及选择")


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'} {label}")
    return bool(cond)


def _store(tmp: Path) -> TemplateStore:
    # 每项独立副本；热改不污染其余标准，也不接触仓库配置或用户库。
    config = tmp / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    db = tmp / "mmwf.db"
    migrate(db)
    return TemplateStore(db, config_dir=config)


def _minutes() -> dict:
    def item(text):
        return {"content": text, "source_quote": "大家觉得差不多", "source_start_ms": None,
                "source_end_ms": None, "owner": "乙", "deadline": "周五"}
    return MinutesSchema.model_validate({
        "title": "项目工作会议纪要", "meeting_time": "2026-10-14", "location": "会议室",
        "host": "甲", "attendees": ["甲", "乙"], "recorder": "乙", "topics": [
            {"name": "方案甲", "discussion": "总体形势稳定", "readings": ["会议认为"],
             "resolutions": [item("同意方案甲"), item("同意方案乙")],
             "action_items": [item("报送材料")],
             "pending_decisions": [{**item("经费方案"), "decision_maker": "甲"}]},
            {"name": "方案乙", "discussion": "工作已完成", "resolutions": [item("同意方案丙")]}],
    }).model_dump()


def _template(store):
    return store.select()


def _headings(md):
    return [line[3:] for line in md.splitlines() if line.startswith("## ")]


def _bound_content(structured, template):
    # 独立按配置来源核对正文，不能只断言标题换了位置。
    markers = {"meeting_meta": "时间：2026-10-14", "discussions": "总体形势稳定",
               "resolutions": "同意方案甲", "actions": "报送材料"}
    for section in structured["sections"]:
        source = template.style["section_bindings"][section["heading"]]
        md = render_template_markdown({"sections": [section]}, [])
        if markers[source] not in md:
            return False
    return True


def check_standard_1(tmp: Path) -> bool:
    store = _store(tmp)
    loaded, problems = store.load_builtin()
    ok = _assert(not problems and {t.meeting_kind for t in loaded} == {
        "party_group", "special_topic", "coordination", "regular_meeting"}, "真实四 YAML 经 store / parse 加载")
    minutes = _minutes()
    for template in loaded:
        structured = apply_template(minutes, template)
        md = render_template_markdown(structured, [])
        ok &= _assert([s["heading"] for s in structured["sections"]] == list(template.section_order)
                      and _headings(md) == list(template.section_order)
                      and _bound_content(structured, template) and not check_template(minutes, template),
                      f"{template.meeting_kind}：章节、内容绑定及 Markdown 顺序")
    return ok


def check_standard_2(tmp: Path) -> bool:
    template = _template(_store(tmp))
    ok = True
    for value in (None, [], [" "], ["待补充"], ["人员：待补充"]):
        data = _minutes()
        if value is None:
            del data["attendees"]
        else:
            data["attendees"] = value
        issues = check_required_elements(data, template)
        ok &= any(i["rule"] == "missing_required_element" and i["detail"] == "参会人员缺失" for i in issues)
    ok = _assert(ok, "缺键、空数组、空白及包含待补充均报告参会人员缺失")
    body = template.to_dict()
    body["required_elements"] = [{"key": "attendees", "label": "参会人员", "min_count": 2},
                                 {"key": "resolutions", "label": "决议", "min_count": 3},
                                 {"key": "title", "label": "标题", "pattern": "^.{4,30}$"}]
    custom = parse_template(body)
    data = _minutes()
    ok &= _assert(check_required_elements(data, custom) == [], "人数与嵌套决议等于 min_count 通过")
    data["attendees"] = ["甲"]
    data["topics"][1]["resolutions"] = []
    data["title"] = "短"
    issues = check_required_elements(data, custom)
    ok &= _assert([(i["rule"], i["field"]) for i in issues] == [
        ("required_min_count", "attendees"), ("required_min_count", "resolutions"),
        ("required_pattern", "title")], "小于阈值失败；pattern 错值定位 title")
    return ok


def check_standard_3(tmp: Path) -> bool:
    template = _template(_store(tmp))
    data = {"topics": [{"discussion": "大家觉得这个方案差不多可以"}]}
    issues = check_forbidden_expressions(data, template)
    ok = _assert([i["matched"] for i in issues] == ["大家觉得", "差不多"]
                 and all(i["field"] == "topics[0].discussion" for i in issues), "discussion 两条禁词命中")
    template = replace(template, style={**template.style, "readings_forbidden": ["大家觉得", "差不多", "ABC", "ａｂｃ"]})
    data["topics"][0]["discussion"] = "大 家觉得差 不 多，差不多 ABC ａｂｃ"
    original = deepcopy(data)
    issues = check_forbidden_expressions(data, template)
    ok &= _assert([i["matched"] for i in issues] == ["大家觉得", "差不多", "abc"] and data == original,
                  "NFKC / casefold / Unicode 空白与字段内去重，不改原文")
    ok &= _assert(check_forbidden_expressions({"topics": [{"discussion": "差、不、多",
        "source_quote": "大家觉得差不多", "source_refs": ["差不多"]}],
        "sections": [{"text": "差不多"}], "issues": [{"detail": "差不多"}]}, template) == [],
        "仅引文、依据、派生 sections、issues 不误报；标点不删除")
    return ok


def check_standard_4(tmp: Path) -> bool:
    store = _store(tmp)
    before = store.select()
    old = apply_template(_minutes(), before)
    path = store.templates_dir / "regular-meeting.yaml"
    text = path.read_text(encoding="utf-8")
    a, b = before.section_order[:2]
    # 仅交换两行并升版本，保留其余 YAML 字节。
    text = text.replace(f"  - {a}\n  - {b}", f"  - {b}\n  - {a}")
    text = text.replace(f"  version: {before.version_no}", f"  version: {before.version_no + 1}")
    path.write_text(text, encoding="utf-8")
    after = store.select()
    new = apply_template(_minutes(), after)
    expected = [b, a, *before.section_order[2:]]
    def without_numbers(value):
        if isinstance(value, dict):
            return {k: without_numbers(v) for k, v in value.items() if k != "number"}
        if isinstance(value, list):
            return [without_numbers(v) for v in value]
        return value
    old_items = {s["heading"]: without_numbers(s["items"]) for s in old["sections"]}
    return _assert(after.version_no == before.version_no + 1
        and [s["heading"] for s in new["sections"]] == expected
        and _headings(render_template_markdown(new, [])) == expected
        and all(without_numbers(s["items"]) == old_items[s["heading"]] for s in new["sections"][:2])
        and _bound_content(new, after), "同一 store 热读：仅换两行及版本，标题和正文一起调序")


def check_standard_5(tmp: Path) -> bool:
    from fastapi.testclient import TestClient
    from mmwf.main import create_app
    from mmwf.settings import load_settings
    from mmwf.api import deps
    from mmwf.api.routes import build_router

    store = _store(tmp)
    cfg = load_settings(tmp / "config")
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={
        "data_dir": tmp, "log_dir": tmp / "logs"})})
    api = "/api/v1/templates"
    expected = []
    # TestClient 是进程内 ASGI 传输，不监听端口、不访问网络；令牌也只写临时目录。
    with patch.dict(os.environ, {"LOCALAPPDATA": str(tmp)}):
        token = deps.create_session_token()
        with TestClient(create_app(cfg), client=("127.0.0.1", 51234)) as client:
            client.headers.update({"X-MMWF-Token": token})
            for version in range(1, 4):
                body = {"template_id": "history", "name": f"正文{version}", "meeting_kind": "regular_meeting",
                        "version": 999, "version_no": 999, "required_elements": [{"key": "title", "label": f"标题{version}"}],
                        "section_order": [f"章节{version}"], "style": {"readings_allowed": [f"标签{version}"]}}
                response = (client.post(api, json=body) if version == 1 else client.put(api + "/history", json=body)).json()
                assert response["ok"], response
                expected.append(response["data"]["template"])
            ok = _assert([t["version_no"] for t in expected] == [1, 2, 3]
                and client.get(api + "/history/versions").json()["data"]["versions"] == [1, 2, 3]
                and all(client.get(api + f"/history/versions/{v}").json()["data"]["template"] == expected[v-1]
                        for v in (1, 2, 3)), "POST / PUT 忽略伪造版本；两个 GET 返回三版不同完整正文")
            for suffix in ("versions", "versions/1"):
                response = client.get(api + "/tpl-regular-meeting-v1/" + suffix).json()
                ok &= response["code"] == "E1001" and "内置模板历史由 git 和 meta.version 管理" in response["message"]
            routes = [r for r in build_router(cfg).routes if "/versions" in getattr(r, "path", "")]
            ok &= _assert(len(routes) == 2 and all(not r.dependant.query_params for r in routes),
                          "历史 GET 无 source 参数；builtin 返回 git + meta.version 中文提示")
    reopened = TemplateStore(store.db_path, config_dir=tmp / "config")
    ok &= _assert(reopened.list_versions("history") == [1, 2, 3]
        and all(reopened.get_version("history", v).to_dict() == expected[v-1] for v in (1, 2, 3)), "重开 store 仍可查询三版完整快照")
    before = reopened.get("history").to_dict()
    with connect_ctx(store.db_path) as conn:
        conn.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON template_revision BEGIN SELECT RAISE(ABORT, '验收故障'); END")
    for create in (True, False):
        failed = False
        try:
            tpl = replace(reopened.get("history"), name="不得残留")
            if create:
                reopened.create(tpl, template_id="rollback")
            else:
                reopened.update(tpl, template_id="history")
        except sqlite3.IntegrityError:
            failed = True
        ok &= _assert(failed and reopened.get("rollback") is None
            and reopened.get("history").to_dict() == before and reopened.list_versions("history") == [1, 2, 3]
            and all(reopened.get_version("history", v).to_dict() == expected[v-1] for v in (1, 2, 3)),
            f"{'POST' if create else 'PUT'} 快照写入失败，模板行与历史整体回滚")
    with connect_ctx(store.db_path) as conn:
        ok &= conn.execute("SELECT count(*) FROM template_revision WHERE template_id='rollback'").fetchone()[0] == 0
    # builtin 同版热改不得登记快照，也不得报错。
    path = store.templates_dir / "regular-meeting.yaml"
    builtin = store.select()
    path.write_text(path.read_text(encoding="utf-8").replace("工作例会议纪要", "热改例会议纪要"), encoding="utf-8")
    hot = store.select()
    store.list_templates()
    with connect_ctx(store.db_path) as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("template", "template_revision")]
    ok &= _assert(hot.name != builtin.name and hot.version_no == builtin.version_no and counts == [1, 3],
                  "builtin 同版改内容可热读，加载不写当前表或历史表")
    legacy = tmp / "legacy.db"
    tpl = parse_template({**before, "version": 7})
    row = tpl.to_row_payload()
    with connect_ctx(legacy) as conn:
        apply_migration(conn, discover_migrations()[0])
        conn.execute("INSERT INTO template VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("old", tpl.name, tpl.meeting_kind, row["schema_json"], row["style_json"], 7, 1, "2026-10-14"))
    migrate(legacy)
    migrate(legacy)
    old_store = TemplateStore(legacy)
    ok &= _assert(old_store.list_versions("old") == [7]
        and old_store.get_version("old", 7).to_dict() == old_store.get("old").to_dict(), "迁移仅保存现存 v7，不伪造旧版；重跑不增版")
    return ok


def check_standard_6(tmp: Path) -> bool:
    template = _template(_store(tmp))
    cases = [
        ("请完成材料报送", ["会议认为"], ["reading_level_mismatch"]),
        ("请完成材料报送", ["会议要求"], []),
        ("形势总体稳定，必须落实责任", ["会议要求"], []),
        ("必须落实，同意方案", ["会议决定"], []),
        ("重要意义，必须落实", ["会议要求"], []),
        ("总体形势稳定。必须落实责任。", ["会议认为"], []),
        ("总体形势稳定。必须落实责任。", ["会议要求"], ["reading_level_mismatch"]),
        ("会议要求，工作已完成。必须落实责任。", ["会议认为"], []),
        ("总体形势稳定", ["会议听取"], ["reading_ambiguous"]),
        ("工作是稳定的", ["会议听取"], []),
        ("随手记下", ["会议认为"], []), ("", [], []),
        ("必须办理", [], ["reading_missing"]),
        ("同意方案", ["会议审议通过"], ["reading_not_allowed"]),
        ("总体稳定", ["会议审议通过"], ["reading_not_allowed", "reading_level_mismatch"]),
        ("随手记下", [" "], ["reading_not_allowed"]),
        ("已于前天下午完成", ["会议认为"], ["reading_level_mismatch"]),
    ]
    cases += [(text, ["会议认为"], []) for text in (
        "工作已完成", "工作已经推进", "任务均已落实", "材料已于昨日完成", "正在抓紧办理")]
    ok = True
    for text, readings, rules in cases:
        data = {"topics": [{"discussion": text, "readings": readings}]}
        original, config = deepcopy(data), deepcopy(template)
        issues = check_reading_levels(data, template)
        ok &= ([i["rule"] for i in issues] == rules and issues == check_reading_levels(data, template)
               and data == original and template == config)
    data = {"topics": [{"discussion": "大家觉得总体差不多", "readings": ["会议要求"]}]}
    original = deepcopy(data)
    issues = check_template(data, template)
    ok &= issues == check_template(data, template) and data == original
    return _assert(ok, f"{len(cases)} 个层级边界及聚合重复调用：确定、纯函数、不升格语气")


def check_standard_7(tmp: Path) -> bool:
    template = _template(_store(tmp))
    expected_keys = set("task_id meeting_id stage audio_paths chunk_plan transcript transcript_meta review_result record_markdown topics resolutions action_items minutes issues pending_decisions minutes_structured minutes_markdown validation_issues gen_retry export_paths idem_keys errors operator _gate_route _gate_rejects".split())
    # 按 T2-04 D01 的两键与产物差量迁移固定合同，仍严格检查未授权扩张。
    outputs = {"ingest": ("audio_paths", "chunk_plan"), "transcribe": ("transcript", "transcript_meta"),
        "review": ("review_result", "record_markdown"), "extract": ("topics", "resolutions", "action_items", "minutes", "issues"),
        "gate_g2": (), "generate": ("minutes_structured", "gen_retry"), "validate": ("validation_issues", "issues", "minutes_structured"),
        "assemble": ("minutes_markdown",), "gate_g3": (), "export": ("export_paths", "idem_keys"), "archive": ()}
    requires = {"ingest": (), "transcribe": ("audio_paths",), "review": ("transcript",), "extract": ("record_markdown",),
        "gate_g2": ("topics", "resolutions", "action_items"), "generate": ("topics", "resolutions", "action_items"),
        "validate": ("minutes_structured",), "assemble": ("minutes_structured",), "gate_g3": ("minutes_markdown",),
        "export": ("minutes_markdown",), "archive": ("export_paths",)}
    ok = _assert(set(S.PipelineState.__annotations__) == expected_keys and S.NODE_OUTPUTS == outputs
                 and S.NODE_REQUIRES == requires, "PipelineState / NODE_REQUIRES / NODE_OUTPUTS 与固定合同完全相同")
    payload = {"topics": [{"title": "方案甲", "discussion": "大家觉得这个方案差不多可以", "readings": ["会议认为"],
                           "resolutions": [{"content": "旧事项"}]}],
        "resolutions": [{"topic": "方案甲", "text": "编辑后决议"}],
        "action_items": [{"topic": "无匹配", "text": "孤立事项"}]}
    adapted = from_graph_payload(payload, meeting_meta={})
    ok &= _assert(adapted["title"] == "" and adapted["attendees"] == []
        and adapted["topics"][0]["readings"] == ["会议认为"]
        and adapted["topics"][0]["resolutions"] == [{"topic": "方案甲", "content": "编辑后决议"}]
        and adapted["topics"][-1]["name"] == "待补充"
        and "source_quote" not in adapted["topics"][-1]["action_items"][0], "三列表编辑权威、readings 保留；缺失元信息和依据不臆造；孤立事项不丢")
    generate = make_generate(generator=lambda p: apply_template(from_graph_payload(p, meeting_meta={}), template))
    validate = make_validate(validator=lambda m: check_template(m, template))
    assemble = make_assemble(assembler=render_template_markdown)
    state = {**payload, "meeting_id": "seam", "_data_dir": str(tmp), "_db_path": str(tmp / "mmwf.db")}
    generated = generate(state)
    state.update(generated)
    checked = validate(state)
    direct = check_template(state["minutes_structured"], template)
    state.update(checked)
    assembled = assemble(state)
    ok &= _assert(set(generated) == {"minutes_structured", "gen_retry", "_gate_route"}
        and set(checked) == {"validation_issues"} and checked["validation_issues"] == direct
        and set(assembled) == {"minutes_markdown", "_gate_route"}
        and "参会人员缺失" in assembled["minutes_markdown"] and "含禁用表述：差不多" in assembled["minutes_markdown"],
        "显式 generate → validate → assemble：issues 入既有字段，中文 detail 入 Markdown")
    return ok


def check_standard_8(tmp: Path) -> bool:
    store = _store(tmp)
    template = store.select()
    numbered = replace(template, section_order=("决议", "待办"), style={"section_bindings": {"决议": "resolutions", "待办": "actions"}})
    sections = apply_template(_minutes(), numbered)["sections"]
    ok = _assert(sections[0]["number"] == "1" and sections[0]["items"][0]["number"] == "1.1"
        and sections[0]["items"][1]["items"][0]["number"] == "1.2.1"
        and sections[1]["items"][0]["items"][0]["number"] == "2.1.1", "三级编号 1 / 1.1 / 1.2.1，换父级重置")
    ok &= _assert(template.template_id == "tpl-regular-meeting-v1"
        and store.select(template.template_id, meeting_kind="不存在") == template
        and store.select(meeting_kind="regular_meeting") == template, "缺省例会、显式 id 优先、kind 单匹配")
    store.create(replace(template, template_id="custom", name="自建例会", source="db"))
    store.create(replace(template, template_id="disabled", name="停用例会", source="db", enabled=False))
    cases = [lambda: store.select(meeting_kind="regular_meeting"), lambda: store.select(meeting_kind="不存在"),
             lambda: store.select("disabled"), lambda: store.select("不存在"), lambda: store.select("")]
    failures = 0
    for call in cases:
        try:
            call()
        except TemplateFormatError:
            failures += 1
    ok &= _assert(failures == len(cases), "kind 多 / 零匹配、停用、不存在、空 id 均拒绝静默回退")
    # 只验收当前四模板的通用中文词汇快照，不声称能识别任意机构专名。
    generic = set("党组会议纪要 专题会议纪要 协调会议纪要 工作例会议纪要 标题 会议时间 会议地点 主持人 参会人员 议题 议项 议定事项 会议基本情况 会议听取和审议的事项 会议决定的事项 会议要求 专题研究事项 工作要求 协调事项及各方意见 责任分工与时限 工作汇报与讨论 下阶段工作安排 会议认为 会议指出 会议强调 会议决定 会议同意 会议听取 会议审议 会议明确 会议研究 会议议定 经协调 会议部署 大家觉得 差不多 可能吧 应该是 反正 搞定 弄一下 有点问题".split())

    def chinese_strings(value):
        if isinstance(value, dict):
            return set().union(*(chinese_strings(k) | chinese_strings(v) for k, v in value.items()))
        if isinstance(value, list):
            return set().union(*(chinese_strings(v) for v in value))
        return {value} if isinstance(value, str) and re.search("[\u4e00-\u9fff]", value) else set()

    texts = set().union(*(chinese_strings(yaml.safe_load(p.read_text(encoding="utf-8")))
                          for p in store.templates_dir.glob("*.yaml")))
    ok &= _assert(bool(texts) and texts <= generic, "四模板全部中文配置均为已审阅通用词汇，无单位专名")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="T2-02 模板库与语体对齐离线验收")
    parser.add_argument("--only", type=int, choices=range(1, 9), help="只运行指定标准（1-8）")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="t202_") as directory:
        print("临时模板副本与 DB：每项隔离，结束自动清理；无模型 / 无网络")
        for number, name in enumerate(NAMES, 1):
            if args.only and args.only != number:
                continue
            banner(f"标准 {number}：{name}")
            tmp = Path(directory) / str(number)
            tmp.mkdir()
            try:
                ok = globals()[f"check_standard_{number}"](tmp)
            except Exception as exc:  # 单项异常也必须计失败，继续报告其余标准。
                ok = _assert(False, f"执行异常：{type(exc).__name__}: {exc}")
            results.append((number, name, ok))
    banner("T2-02 验收结果")
    for number, name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} 标准 {number}  {name}")
    passed = sum(bool(ok) for _, _, ok in results)
    print(f"\n  共 {len(results)} 项，通过 {passed} 项")
    success = passed == len(results)
    print(f"{'PASS' if success else 'FAIL'} T2-02 验收{'通过' if success else '未通过'}（{passed}/{len(results)}）")
    return 0 if success else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    raise SystemExit(main())
