"""`graph.nodes` 单元测试（T1-10）。

这一层测的不是"抽取准不准"（那是二期 T2-01 的事），而是
**编排侧的契约**：

  · `naive_*` 四件套是**确定性**的 —— 同输入同输出。
    这是"approve 不重跑"能被断言的前提：结果随机的话，
    "没重跑"和"重跑了但碰巧一样"就分不开。
  · 返回值是**状态增量**（只含本节点该写的键），不是完整状态。
  · `_base` 的入口校验、异常翻译、幂等包装行为正确。
  · 少输入时**报错**（`NodeInputError`），而不是拿 None 硬跑出脏数据。

这些性质都属于"不写测试就一定会悄悄坏掉"的类型：
坏了以后图还能跑完，只是跑出来的东西不对。
"""

from __future__ import annotations

import json

import pytest

from mmwf.graph import idempotency as idem
from mmwf.graph import nodes as N
from mmwf.graph import state as S
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes import extraction as E

# 记录稿放在 fixtures 里（编排测试共用同一份，避免两处漂移）
import harness

RECORD = harness.RECORD_MARKDOWN
EMPTY_RECORD = harness.EMPTY_RECORD

#: `generate` 声明了三项前置输入，缺一不可 —— 只给 topics 会被
#: 入口校验拦下（这是设计如此：图上的连边顺序变了要立刻发现）。
#:
#: ⚠️ `action_items` 也**不能给空列表**：空列表按"缺输入"处理
#: （见 `_base._is_blank`）—— "一个待办都没抽出来"和"没跑过 extract"
#: 都该让 generate 停下。所以这里给一条真实的待办。
GEN_INPUTS: dict = {
    "topics": [{"title": "甲", "refs": [{"segment_id": "s"}]}],
    "resolutions": [{"text": "乙", "refs": [{"segment_id": "s"}]}],
    "action_items": [{"text": "丙", "owner": "丁",
                      "refs": [{"segment_id": "s"}]}],
}


# ---------------------------------------------------------------------------
# 确定性 —— "不重跑"断言的前提
# ---------------------------------------------------------------------------

class TestNaiveDeterminism:
    """四个朴素实现必须是纯函数。"""

    def test_naive_extract_is_deterministic(self):
        a = E.naive_extract(RECORD)
        b = E.naive_extract(RECORD)
        assert a == b, "同一份记录稿两次抽取结果不同 —— 不重跑的断言会失效"

    def test_naive_generate_is_deterministic(self):
        ex = E.naive_extract(RECORD)
        assert E.naive_generate(ex) == E.naive_generate(ex)

    def test_naive_validate_is_deterministic(self):
        gen = E.naive_generate(E.naive_extract(RECORD))
        assert E.naive_validate(gen) == E.naive_validate(gen)

    def test_naive_assemble_is_deterministic(self):
        gen = E.naive_generate(E.naive_extract(RECORD))
        iss = E.naive_validate(gen)
        assert E.naive_assemble(gen, iss) == E.naive_assemble(gen, iss)

    def test_naive_extract_does_not_mutate_input(self):
        payload = E.naive_extract(RECORD)
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        E.naive_generate(payload)
        E.naive_validate(payload)
        assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == snapshot

    def test_naive_chain_is_json_serialisable(self):
        """产物要进检查点，所以必须能过 JSON。

        过不了的话 `SqliteSaver` 会在**保存检查点**时报错 ——
        离真正的原因（某个节点返回了不可序列化的对象）很远。
        """
        ex = E.naive_extract(RECORD)
        json.dumps(ex, ensure_ascii=False)
        json.dumps(E.naive_generate(ex), ensure_ascii=False)


# ---------------------------------------------------------------------------
# naive_extract：分类与来源依据
# ---------------------------------------------------------------------------

class TestNaiveExtract:
    def test_splits_into_three_categories(self):
        got = E.naive_extract(RECORD)
        assert len(got["topics"]) == 1
        assert len(got["resolutions"]) == 1
        assert len(got["action_items"]) == 1

    def test_returns_three_known_keys(self):
        """只返回约定的三个键 —— 多点东西下游会当垃圾数据带下去。"""
        got = E.naive_extract(RECORD)
        assert set(got) == {"topics", "resolutions", "action_items"}

    def test_every_item_carries_refs_with_real_timestamps(self):
        """⚠️ 回归：`start_ms` 曾经被硬编码成 None。

        修 `_parse_timecode` 前，每条 ref 的 `start_ms` 都是 None，
        表面看"有 refs"，但溯源要的"这句话在第几分钟"给不出来。
        """
        got = E.naive_extract(RECORD)
        expected = [12000, 30000, 65000]
        actual = [
            got["topics"][0]["refs"][0]["start_ms"],
            got["resolutions"][0]["refs"][0]["start_ms"],
            got["action_items"][0]["refs"][0]["start_ms"],
        ]
        assert actual == expected

    def test_refs_have_segment_id_and_raw_text(self):
        ref = E.naive_extract(RECORD)["topics"][0]["refs"][0]
        assert ref["segment_id"] == "seg-1"
        # 来源原文要保留完整（含时间码与说话人）—— 审定人要能回看原话
        assert ref["text"].startswith("[00:00:12]")
        assert "张三" in ref["text"]

    def test_speaker_prefix_is_stripped_from_content(self):
        """⚠️ 回归：说话人前缀 `张三：` 曾留在正文里。

        留着的话渲染成纪要就是"张三：张三：……"式的重复。
        """
        got = E.naive_extract(RECORD)
        assert got["topics"][0]["title"] == "今天研究季度工作安排。"
        assert not got["topics"][0]["title"].startswith("张三")
        assert got["resolutions"][0]["text"] == "决定追加五十万元用于设备采购。"

    def test_resolution_wins_over_action_when_both_match(self):
        """分类是互斥的：同时像决议又像待办的句子归决议。

        判据顺序在 `naive_extract` 里是"先决议后待办"，改顺序会
        让"决定由张三分工"这类句子跑去待办，破坏章节结构。
        """
        md = "# 记录\n\n[00:00:01] 甲：决定由负责推进此事。\n"
        got = E.naive_extract(md)
        assert len(got["resolutions"]) == 1
        assert got["action_items"] == []

    def test_plain_sentence_without_cues_becomes_topic(self):
        got = E.naive_extract("# 记录\n\n[00:00:01] 甲：今天天气不错。\n")
        assert len(got["topics"]) == 1
        assert got["resolutions"] == []

    def test_empty_record_yields_nothing(self):
        got = E.naive_extract(EMPTY_RECORD)
        assert got == {"topics": [], "resolutions": [], "action_items": []}

    def test_empty_string_is_handled(self):
        assert E.naive_extract("") == {
            "topics": [], "resolutions": [], "action_items": [],
        }

    def test_multi_sentence_line_is_split_into_several_items(self):
        """一行里多个句号要切开 —— 否则一个议题吞掉整段会议内容。"""
        md = "# 记录\n\n[00:00:01] 甲：第一件事。第二件事。\n"
        got = E.naive_extract(md)
        assert len(got["topics"]) == 2

    def test_sentences_share_line_level_ref(self):
        """同一行切出的多句**共用**该行的时间码。

        粒度只能到行（朴素实现不做说话人切分），
        共用比"给后面的句子编个假时间"要诚实。
        """
        md = "# 记录\n\n[00:00:07] 甲：第一件事。第二件事。\n"
        got = E.naive_extract(md)
        a, b = got["topics"]
        assert a["refs"][0]["start_ms"] == 7000
        assert b["refs"][0]["start_ms"] == 7000

    def test_ungrounded_record_has_empty_refs(self):
        """没时间码的行**不给假依据** —— 空 refs 让 validate 能报出来。"""
        got = E.naive_extract("# 记录\n\n有人提了一句。\n")
        assert got["topics"][0]["refs"] == []

    def test_seq_is_one_based_and_ascending(self):
        got = E.naive_extract(RECORD)
        seqs = [got["topics"][0]["seq"], got["resolutions"][0]["seq"],
                got["action_items"][0]["seq"]]
        assert seqs == [1, 2, 3]

    def test_title_is_clipped(self):
        """超长句子要截断 —— 议题标题进的是纪要正文，不能整段搬。"""
        long_line = "甲：" + "很长的内容" * 30 + "。"
        got = E.naive_extract(f"# 记录\n\n[00:00:01] {long_line}\n")
        assert len(got["topics"][0]["title"]) <= 40
        assert got["topics"][0]["title"].endswith("…")

    def test_many_topics_are_merged(self):
        """议题太碎会合并成一条 —— 20 条以上触发。"""
        lines = [f"[00:0{i // 60}:{i % 60:02d}] 甲：第{i}件事。" for i in range(25)]
        got = E.naive_extract("# 记录\n\n" + "\n".join(lines) + "\n")
        assert len(got["topics"]) == 1
        assert got["topics"][0]["title"] == "会议主要内容"
        assert got["topics"][0]["merged_from"] == 25

    def test_merge_collects_all_refs(self):
        lines = [f"[00:0{i // 60}:{i % 60:02d}] 甲：第{i}件事。" for i in range(25)]
        got = E.naive_extract("# 记录\n\n" + "\n".join(lines) + "\n")
        assert len(got["topics"][0]["refs"]) == 25


# ---------------------------------------------------------------------------
# 时间码解析
# ---------------------------------------------------------------------------

class TestParseTimecode:
    @pytest.mark.parametrize("head,expected", [
        ("[00:00:12]", 12_000),
        ("[00:01:05]", 65_000),
        ("[01:02:03]", 3_723_000),
        ("[00:00:00]", 0),
        ("[02:03]", 123_000),        # 分:秒
    ])
    def test_parses_valid_timecodes(self, head, expected):
        assert E._parse_timecode(head) == expected

    @pytest.mark.parametrize("head", ["nope", "[]", "[12]", "[a:b:c]", ""])
    def test_unparsable_returns_none(self, head):
        """解析不出来给 None，**不给 0**。

        0 是合法时间点（会议刚开始），拿它当"没有时间码"会让
        后端无法区分这两种情况。
        """
        assert E._parse_timecode(head) is None

    def test_zero_is_distinguishable_from_none(self):
        assert E._parse_timecode("[00:00:00]") == 0
        assert E._parse_timecode("nope") is None
        assert E._parse_timecode("[00:00:00]") is not None

    def test_takes_last_index_in_head(self):
        """head 里带内容时也要能取到 —— 回归：`partition` 丢右括号。

        `"[00:00:01] 甲" .partition("]")` 得到 `head="[00:00:01"`（无右括号），
        直接传给 regex 会匹配失败。调用方会补回 `sep`。
        """
        assert E._parse_timecode("[00:00:01" + "]") == 1000


class TestStripSpeaker:
    @pytest.mark.parametrize("line,expected", [
        ("张三：今天研究季度工作安排。", "今天研究季度工作安排。"),
        ("李四:决定追加", "决定追加"),
        ("王五 ： 有空", "有空"),
        ("王五：  有空", "有空"),
        ("没有冒号的一句话。", "没有冒号的一句话。"),
    ])
    def test_strips_speaker(self, line, expected):
        """⚠️ 回归：冒号前带空格的写法曾剥不掉。

        记录稿经转写/人工编辑后常见 `王五 ： 有空`，
        正则要求冒号紧贴名字的话，说话人会留在正文里。
        """
        assert E._strip_speaker(line) == expected

    def test_only_strips_first_colon(self):
        """只剥第一处冒号 —— 正文里的冒号是内容的一部分。"""
        assert E._strip_speaker("张三：议题一：讨论") == "议题一：讨论"

    def test_does_not_strip_single_char_name(self):
        """1 个汉字不算姓名 —— 否则"甲：内容"的正文会被吃掉。"""
        assert E._strip_speaker("甲：内容") == "甲：内容"


# ---------------------------------------------------------------------------
# 责任人抽取与待办渲染
# ---------------------------------------------------------------------------

class TestExtractOwner:
    @pytest.mark.parametrize("text,expected", [
        ("请赵六负责落实设备验收工作。", "赵六"),
        ("由张三牵头推进此事。", "张三"),
        ("王五负责跟进。", "王五"),
        ("李四同志落实整改。", "李四"),
        ("让周七完成材料报送。", "周七"),
        ("赵六负责", "赵六"),
        ("会议讨论了预算问题。", ""),
        ("请于周五前提交材料。", ""),
    ])
    def test_extract_owner(self, text, expected):
        """⚠️ 回归：贪婪正则曾把引导动词吃进名字里。

        第一版 `([\u4e00-\u9fa5]{2,4})(?:同志)?(?:负责|牵头)` 因为
        `{2,4}` 贪婪，匹配"请赵六负责落实"得到名字 `赵六负责`。
        """
        assert E._extract_owner(text) == expected

    def test_owner_never_contains_verb_head(self):
        """名字里不得残留责任动词首字。"""
        for text in ("请赵六负责落实设备验收工作。", "由张三牵头推进此事。"):
            owner = E._extract_owner(text)
            assert owner
            assert not any(h in owner for h in E._OWNER_VERB_HEADS)

    def test_returns_empty_rather_than_guessing(self):
        """抽不到就留空 —— 猜错一个名字比留空危险得多。"""
        assert E._extract_owner("某人要办点事。") == ""


class TestFormatAction:
    @pytest.mark.parametrize("item,expected", [
        ({"text": "请赵六负责落实设备验收工作。", "owner": "赵六"},
         "赵六负责：落实设备验收工作。"),
        ({"text": "由张三牵头推进此事。", "owner": "张三"},
         "张三负责：推进此事。"),
        ({"text": "王五负责跟进。", "owner": "王五"},
         "王五负责：跟进。"),
        ({"text": "李四同志落实整改。", "owner": "李四"},
         "李四负责：整改。"),
    ])
    def test_renders_owner_once(self, item, expected):
        """⚠️ 回归：曾是"赵六负责负责：…"（双份负责），
        后来又变成"赵六负责：请赵六负责落实…"（同一件事说两遍）。"""
        assert E._format_action(item) == expected

    def test_owner_appears_exactly_once(self):
        got = E._format_action({"text": "请赵六负责落实设备验收工作。", "owner": "赵六"})
        assert got.count("赵六") == 1
        assert got.count("负责") == 1

    def test_no_owner_returns_text_unchanged(self):
        """无责任人时不加"负责：" —— 让 validate 报的问题在稿上看得见。"""
        got = E._format_action({"text": "请于周五前提交材料。", "owner": ""})
        assert got == "请于周五前提交材料。"
        assert "负责：" not in got

    def test_unstrippable_text_falls_back_to_raw(self):
        """剥不动就退回原文 —— 宁可有重复，也不能把整句弄丢。"""
        got = E._format_action({"text": "赵六负责", "owner": "赵六"})
        assert "赵六" in got

    def test_handles_missing_keys(self):
        assert E._format_action({}) == ""
        assert E._format_action({"owner": "赵六"}) == "赵六负责："


class TestStripOwnerPrefix:
    def test_strips_cue_and_verb(self):
        assert E._strip_owner_prefix("请赵六负责落实设备验收工作。", "赵六") == "落实设备验收工作。"

    def test_returns_empty_when_nothing_stripped(self):
        assert E._strip_owner_prefix("今天天气不错。", "赵六") == ""

    def test_returns_empty_on_empty_owner(self):
        assert E._strip_owner_prefix("赵六负责落实。", "") == ""

    def test_returns_empty_rather_than_emptying_text(self):
        """剥完啥都不剩时返回空串，让调用方退回原文。"""
        assert E._strip_owner_prefix("赵六负责", "赵六") == ""


# ---------------------------------------------------------------------------
# naive_generate
# ---------------------------------------------------------------------------

class TestNaiveGenerate:
    def test_builds_three_sections(self):
        gen = E.naive_generate(E.naive_extract(RECORD))
        headings = [s["heading"] for s in gen["sections"]]
        assert headings == ["一、会议主要内容", "二、会议决定事项", "三、待办事项"]

    def test_passes_through_raw_categories(self):
        """⚠️ 回归：`topics` 曾经被丢掉。

        丢掉后 `naive_validate` 只能从 sections 反推议题，
        反推出来的议题**没有 refs**，于是"无依据"这条规则在
        **完全正常的记录稿**上也会误报。
        """
        ex = E.naive_extract(RECORD)
        gen = E.naive_generate(ex)
        assert "topics" in gen
        assert gen["topics"] == ex["topics"]
        assert gen["resolutions"] == ex["resolutions"]
        assert gen["action_items"] == ex["action_items"]

    def test_passthrough_is_a_copy_not_the_same_object(self):
        """透传要给副本 —— 下游改了会污染上游状态。"""
        ex = E.naive_extract(RECORD)
        gen = E.naive_generate(ex)
        assert gen["topics"] is not ex["topics"]

    def test_omits_empty_sections(self):
        gen = E.naive_generate({"topics": [{"title": "甲"}]})
        assert [s["heading"] for s in gen["sections"]] == ["一、会议主要内容"]

    def test_empty_payload_gives_no_sections(self):
        gen = E.naive_generate({})
        assert gen["sections"] == []
        assert gen["title"]

    def test_has_title(self):
        assert E.naive_generate({})["title"] == "会议纪要"

    def test_action_items_rendered_with_owner(self):
        gen = E.naive_generate(E.naive_extract(RECORD))
        items = gen["sections"][2]["items"]
        assert items == ["赵六负责：落实设备验收工作。"]


# ---------------------------------------------------------------------------
# naive_validate
# ---------------------------------------------------------------------------

class TestNaiveValidate:
    def test_clean_record_has_no_issues(self):
        """⚠️ 这是本卡最重要的一条：**正常记录稿不得误报**。

        修 `_parse_timecode` 与 `naive_generate` 透传之前，
        这条一直是红的（topic_without_refs）。
        """
        gen = E.naive_generate(E.naive_extract(RECORD))
        assert E.naive_validate(gen) == []

    def test_empty_record_reports_no_topics(self):
        gen = E.naive_generate(E.naive_extract(EMPTY_RECORD))
        issues = E.naive_validate(gen)
        assert [i["rule"] for i in issues] == ["no_topics"]

    def test_ungrounded_topic_reports_missing_refs(self):
        gen = E.naive_generate(E.naive_extract("# 记录\n\n有人提了一句。\n"))
        rules = [i["rule"] for i in E.naive_validate(gen)]
        assert "topic_without_refs" in rules

    def test_action_without_owner_is_reported(self):
        gen = E.naive_generate({"action_items": [{"text": "请于周五前提交。", "owner": ""}]})
        rules = [i["rule"] for i in E.naive_validate(gen)]
        assert "action_without_owner" in rules

    def test_empty_topic_title_is_reported(self):
        gen = E.naive_generate({"topics": [{"title": "  ", "refs": [{"segment_id": "s"}]}]})
        rules = [i["rule"] for i in E.naive_validate(gen)]
        assert "empty_topic_title" in rules

    def test_resolution_without_refs_is_reported(self):
        gen = E.naive_generate({"resolutions": [{"text": "决定了某事。"}]})
        rules = [i["rule"] for i in E.naive_validate(gen)]
        assert "resolution_without_refs" in rules

    def test_every_issue_has_rule_and_detail(self):
        gen = E.naive_generate(E.naive_extract(EMPTY_RECORD))
        for issue in E.naive_validate(gen):
            assert issue["rule"]
            assert issue["detail"]
            assert set(issue) == {"rule", "detail"}

    def test_all_rules_are_from_the_declared_table(self):
        known = {name for name, _ in E.VALIDATION_RULES}
        for payload in (
            E.naive_extract(EMPTY_RECORD),
            E.naive_extract("# 记录\n\n有人说了句话。\n"),
            {"topics": [{"title": "", "refs": []}],
             "resolutions": [{"text": "x"}],
             "action_items": [{"text": "y", "owner": ""}]},
        ):
            for issue in E.naive_validate(E.naive_generate(payload)):
                assert issue["rule"] in known

    def test_does_not_invent_content_judgements(self):
        """只判结构，不判内容对错 —— 假装能判内容比不判更危险。"""
        known = {name for name, _ in E.VALIDATION_RULES}
        assert known == {
            "no_topics", "topic_without_refs", "resolution_without_refs",
            "empty_topic_title", "action_without_owner",
        }

    def test_tolerates_garbage_payload(self):
        """⚠️ 回归：畸形列表元素曾让自检抛 AttributeError。

        `validate` 抛异常的后果是**整个任务失败**，而自检存在的意义
        恰恰是"发现问题后照样成文" —— 自己先炸掉就完全反了。
        """
        for bad in ({}, {"topics": None}, {"topics": "字符串"},
                    {"action_items": [None]}, {"sections": None},
                    {"sections": [None, "x"]}, {"topics": [None, 1, "s"]},
                    {"resolutions": 42}, {"sections": "不是列表"}):
            assert isinstance(E.naive_validate(bad), list), f"输入 {bad} 炸了"

    def test_garbage_payload_does_not_hide_real_issues(self):
        """过滤畸形条目的同时，合法条目仍要被照常检查。"""
        gen = {"topics": [None, {"title": "合法但无依据"}]}
        rules = [i["rule"] for i in E.naive_validate(gen)]
        assert "topic_without_refs" in rules

    def test_falls_back_to_sections_when_topics_missing(self):
        """老格式（只给 sections）仍要有产出，但会报无依据。"""
        gen = {"sections": [{"heading": "一、会议主要内容", "items": ["某事。"]}]}
        rules = [i["rule"] for i in E.naive_validate(gen)]
        assert "topic_without_refs" in rules


# ---------------------------------------------------------------------------
# naive_assemble
# ---------------------------------------------------------------------------

class TestNaiveAssemble:
    def test_starts_with_title_heading(self):
        md = E.naive_assemble(E.naive_generate(E.naive_extract(RECORD)), [])
        assert md.startswith("# 会议纪要")

    def test_renders_section_headings(self):
        md = E.naive_assemble(E.naive_generate(E.naive_extract(RECORD)), [])
        for h in ("## 一、会议主要内容", "## 二、会议决定事项", "## 三、待办事项"):
            assert h in md

    def test_items_are_numbered_from_one(self):
        md = E.naive_assemble(E.naive_generate(E.naive_extract(RECORD)), [])
        assert "1. 今天研究季度工作安排。" in md

    def test_issues_are_appended_to_the_document(self):
        """⚠️ C03 配套：问题清单必须**进正文**，不能只记日志。

        审定人要能在稿子上直接看到"机器觉得这里不确定"。
        """
        gen = E.naive_generate(E.naive_extract(EMPTY_RECORD))
        md = E.naive_assemble(gen, E.naive_validate(gen))
        assert "## 待核对清单" in md
        assert "没有任何议题" in md

    def test_no_issue_list_when_clean(self):
        md = E.naive_assemble(E.naive_generate(E.naive_extract(RECORD)), [])
        assert "待核对清单" not in md

    def test_ends_with_single_newline(self):
        md = E.naive_assemble(E.naive_generate(E.naive_extract(RECORD)), [])
        assert md.endswith("\n")
        assert not md.endswith("\n\n")

    def test_handles_empty_structured(self):
        md = E.naive_assemble({}, [])
        assert md.startswith("# ")

    def test_tolerates_issue_missing_detail(self):
        md = E.naive_assemble({}, [{"rule": "no_topics"}])
        assert "no_topics" in md


# ---------------------------------------------------------------------------
# _base：入口校验 / 异常翻译 / 增量返回
# ---------------------------------------------------------------------------

class TestMissingInputs:
    def test_reports_missing_declared_inputs(self):
        assert B.missing_inputs(S.NODE_EXTRACT, {}) == ["record_markdown"]

    def test_present_input_is_not_missing(self):
        assert B.missing_inputs(S.NODE_EXTRACT, {"record_markdown": "x"}) == []

    @pytest.mark.parametrize("value", [None, "", "   ", "\n\t ", [], {}])
    def test_blank_values_count_as_missing(self, value):
        """空值等同于缺值 —— 让节点在入口就停，而不是拿空值硬跑。

        ⚠️ 回归：空白串 `"   "` 曾被判为"有输入"。后果是节点拿着
        三个空格继续跑，产出一篇空纪要，而不是在入口报"缺少记录稿"。
        """
        assert "record_markdown" in B.missing_inputs(
            S.NODE_EXTRACT, {"record_markdown": value})

    def test_unknown_node_declares_nothing(self):
        assert B.missing_inputs("不存在的节点", {}) == []

    def test_ingest_is_root_and_needs_nothing(self):
        assert B.missing_inputs(S.NODE_INGEST, {}) == []


class TestRequireInputs:
    def test_passes_when_inputs_present(self):
        B.require_inputs(S.NODE_EXTRACT, {"record_markdown": "x"})   # 不抛

    def test_raises_node_input_error_when_missing(self):
        with pytest.raises(B.NodeInputError) as ei:
            B.require_inputs(S.NODE_EXTRACT, {})
        assert "record_markdown" in str(ei.value)

    def test_error_names_the_node(self):
        with pytest.raises(B.NodeInputError) as ei:
            B.require_inputs(S.NODE_ASSEMBLE, {})
        assert S.NODE_ASSEMBLE in str(ei.value) or "assemble" in str(ei.value)


class TestTranslateException:
    def test_mmwf_error_passes_through_unchanged(self):
        from mmwf.domain.errors import MmwfError

        original = MmwfError(code="E4003", detail="待确认项为空")
        got = B.translate_exception(S.NODE_EXTRACT, original)
        assert got is original

    def test_other_exception_becomes_e9001(self):
        got = B.translate_exception(S.NODE_EXTRACT, RuntimeError("炸了"))
        assert got.code == "E9001"
        assert "炸了" in str(got)

    def test_translated_error_names_the_node(self):
        got = B.translate_exception(S.NODE_EXTRACT, ValueError("x"))
        assert S.NODE_EXTRACT in str(got)

    def test_translated_error_records_exception_type(self):
        """`detail` 里要留原始异常类型 —— 排查时是最有用的一条线索。"""
        got = B.translate_exception(S.NODE_EXTRACT, KeyError("k"))
        assert "KeyError" in str(got)
        assert got.context.get("exc_type") == "KeyError"


class TestGuard:
    def test_returns_function_result(self):
        assert B.guard(S.NODE_EXTRACT, lambda: 42) == 42

    def test_translates_exception(self):
        def boom():
            raise KeyError("k")

        with pytest.raises(Exception) as ei:
            B.guard(S.NODE_EXTRACT, boom)
        assert getattr(ei.value, "code", None) == "E9001"


class TestAppendError:
    def test_appends_to_empty(self):
        got = B.append_error({}, "E4003", S.NODE_EXTRACT, "详")
        assert len(got) == 1
        assert got[0]["code"] == "E4003"

    def test_accumulates_across_calls(self):
        """⚠️ 回归：曾直接把上一轮的 errors 覆盖掉，问题清单会丢。"""
        state = {"errors": []}
        first = B.append_error(state, "E1", S.NODE_EXTRACT, "a")
        second = B.append_error({"errors": first}, "E2", S.NODE_EXTRACT, "b")
        assert [e["code"] for e in second] == ["E1", "E2"]

    def test_entry_has_expected_fields(self):
        got = B.append_error({}, "E4003", S.NODE_EXTRACT, "详")[0]
        assert set(got) == {"code", "node", "detail"}

    def test_does_not_mutate_input_list(self):
        original = [{"code": "E1", "node": "x", "detail": "d"}]
        before = list(original)
        B.append_error({"errors": original}, "E2", "y", "e")
        assert original == before


class TestSessionAndCfg:
    def test_initial_state_is_not_empty(self):
        """空 dict 会被 LangGraph 拒绝（InvalidUpdateError），所以初始状态
        至少要写一个键。"""
        got = B.initial_state("m-1")
        assert got == B.INITIAL_STATE or set(got) >= set(B.INITIAL_STATE)

    def test_initial_state_records_meeting_id(self):
        assert B.initial_state("m-1").get("meeting_id") == "m-1"

    def test_initial_state_carries_route_key(self):
        assert "_gate_route" in B.initial_state("m-1")

    def test_initial_state_accepts_extra(self):
        assert B.initial_state("m-1", operator="张三").get("operator") == "张三"

    def test_meeting_id_of_reads_state(self):
        assert B.meeting_id_of({"meeting_id": "m-9"}) == "m-9"

    def test_meeting_id_of_falls_back_to_task_id(self):
        """一期 task_id 与 meeting_id 同值，但取值要两条都认。"""
        assert B.meeting_id_of({"task_id": "m-9"}) == "m-9"

    def test_meeting_id_of_raises_when_both_missing(self):
        """定位不到任务就必须报错 —— 不能返回空串。

        空串往下走会变成"所有任务共用一个目录"，比报错危险得多。
        """
        with pytest.raises(Exception) as ei:
            B.meeting_id_of({})
        assert "meeting_id" in str(ei.value) or "task_id" in str(ei.value)

    def test_meeting_id_of_coerces_to_str(self):
        assert B.meeting_id_of({"meeting_id": 123}) == "123"


# ---------------------------------------------------------------------------
# 节点增量返回契约
# ---------------------------------------------------------------------------

class TestNodeIncrements:
    """节点返回**状态增量**，不是完整状态（施工卡常见坑 ③）。"""

    def test_extract_returns_only_its_own_keys(self):
        node = N.make_extract(None)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                "record_markdown": RECORD,
                "_data_dir": td,
                "_db_path": str(Path(td) / "x.db"),
            })
        assert set(out) <= {
            "topics", "resolutions", "action_items", "errors", "_gate_route",
        }

    def test_generate_increments_gen_retry(self):
        node = N.make_generate(None)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            base = {
                "meeting_id": "m-t",
                **GEN_INPUTS,
                "_data_dir": td,
                "_db_path": str(Path(td) / "x.db"),
            }
            first = node(dict(base))
            assert first["gen_retry"] == 1
            second = node({**base, "gen_retry": 1})
            assert second["gen_retry"] == 2

    def test_generate_requires_all_three_categories(self):
        """只给 topics 会报缺 `resolutions` / `action_items`。

        这条看起来"太严"，但它正是"图连错了立刻发现"的机制：
        如果哪天有人把 generate 接到了 extract 的错误分支上，
        这里会直接炸，而不是安静地生成一篇缺章节的纪要。
        """
        node = N.make_generate(None)
        with pytest.raises(B.NodeInputError) as ei:
            node({"meeting_id": "m-t", "topics": [{"title": "甲"}]})
        assert "resolutions" in str(ei.value)
        assert "action_items" in str(ei.value)

    def test_generate_clears_route_marker(self):
        node = N.make_generate(None)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                **GEN_INPUTS,
                "_gate_route": "redo",
                "_data_dir": td,
                "_db_path": str(Path(td) / "x.db"),
            })
        assert out["_gate_route"] is None

    def test_extract_clears_route_marker(self):
        """extract 是 G2 reject 的落点，不清标记会让条件边再次走 redo。"""
        node = N.make_extract(None)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                "record_markdown": RECORD,
                "_gate_route": "redo",
                "_data_dir": td,
                "_db_path": str(Path(td) / "x.db"),
            })
        assert out["_gate_route"] is None

    def test_assemble_clears_route_marker(self):
        node = N.make_assemble(None)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                "minutes_structured": E.naive_generate(E.naive_extract(RECORD)),
                "_gate_route": "redo",
                "_data_dir": td,
                "_db_path": str(Path(td) / "x.db"),
            })
        assert out["_gate_route"] is None

    def test_extract_records_when_nothing_found(self):
        node = N.make_extract(None)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                "record_markdown": EMPTY_RECORD,
                "_data_dir": td,
                "_db_path": str(Path(td) / "x.db"),
            })
        assert out["topics"] == []
        assert any(e["code"] == "E4003" for e in out["errors"])

    def test_extract_missing_input_raises(self):
        node = N.make_extract(None)
        with pytest.raises(B.NodeInputError):
            node({"meeting_id": "m-t"})


class TestNodeFactories:
    def test_all_factories_are_callable(self):
        for name in ("make_ingest", "make_transcribe", "make_review", "make_extract",
                     "make_generate", "make_validate", "make_assemble",
                     "make_export", "make_archive", "make_gate_g2", "make_gate_g3"):
            assert callable(getattr(N, name)), f"{name} 不可调用"

    def test_factories_name_their_nodes(self):
        expected = {
            "make_ingest": S.NODE_INGEST,
            "make_transcribe": S.NODE_TRANSCRIBE,
            "make_review": S.NODE_REVIEW,
            "make_extract": S.NODE_EXTRACT,
            "make_generate": S.NODE_GENERATE,
            "make_validate": S.NODE_VALIDATE,
            "make_assemble": S.NODE_ASSEMBLE,
            "make_export": S.NODE_EXPORT,
            "make_archive": S.NODE_ARCHIVE,
        }
        for factory_name, node_name in expected.items():
            fn = getattr(N, factory_name)(None)
            assert fn.__name__ == node_name

    def test_every_graph_node_has_a_factory(self):
        """`NODE_SEQUENCE` 里每个节点都要能从 `nodes` 造出来 ——
        少一个只在跑图时才暴露。"""
        factories = {
            S.NODE_INGEST: N.make_ingest, S.NODE_TRANSCRIBE: N.make_transcribe,
            S.NODE_REVIEW: N.make_review, S.NODE_EXTRACT: N.make_extract,
            S.NODE_GENERATE: N.make_generate, S.NODE_VALIDATE: N.make_validate,
            S.NODE_ASSEMBLE: N.make_assemble, S.NODE_GATE_G2: N.make_gate_g2,
            S.NODE_GATE_G3: N.make_gate_g3, S.NODE_EXPORT: N.make_export,
            S.NODE_ARCHIVE: N.make_archive,
        }
        assert set(factories) == set(S.NODE_SEQUENCE)

    def test_gate_nodes_have_no_downstream_products(self):
        """闸口不产生产物，只做决策 —— 产物表里不该有它。"""
        assert S.NODE_OUTPUTS.get(S.NODE_GATE_G2) == ()
        assert S.NODE_OUTPUTS.get(S.NODE_GATE_G3) == ()


class TestInjectors:
    """注入点必须真的被用上 —— 二期替换实现的唯一入口。"""

    def test_extract_uses_injected_extractor(self):
        calls = []

        def fake(md: str) -> dict:
            calls.append(md)
            return {"topics": [{"title": "注入的议题", "refs": [{"segment_id": "s"}]}],
                    "resolutions": [], "action_items": []}

        node = N.make_extract(None, extractor=fake)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t", "record_markdown": "whatever",
                "_data_dir": td, "_db_path": str(Path(td) / "x.db"),
            })
        assert calls == ["whatever"]
        assert out["topics"][0]["title"] == "注入的议题"

    def test_generate_uses_injected_generator(self):
        node = N.make_generate(None, generator=lambda p: {"title": "注入", "sections": []})
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                **GEN_INPUTS,
                "_data_dir": td, "_db_path": str(Path(td) / "x.db"),
            })
        assert out["minutes_structured"]["title"] == "注入"

    def test_validate_uses_injected_validator(self):
        node = N.make_validate(None, validator=lambda s: [{"rule": "custom", "detail": "d"}])
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                "minutes_structured": {"title": "甲", "sections": []},
                "_data_dir": td, "_db_path": str(Path(td) / "x.db"),
            })
        assert out["validation_issues"] == [{"rule": "custom", "detail": "d"}]

    def test_assemble_uses_injected_assembler(self):
        node = N.make_assemble(None, assembler=lambda s, i: "# 注入的稿子\n")
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            out = node({
                "meeting_id": "m-t",
                "minutes_structured": {"title": "甲", "sections": []},
                "_data_dir": td, "_db_path": str(Path(td) / "x.db"),
            })
        assert out["minutes_markdown"] == "# 注入的稿子\n"

    def test_assemble_passes_issues_to_injected_assembler(self):
        """问题清单必须传进去 —— C03 要求它出现在正文里。"""
        seen = {}
        node = N.make_assemble(None, assembler=lambda s, i: (seen.update(issues=i), "x")[1])
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            node({
                "meeting_id": "m-t",
                "minutes_structured": {"title": "甲"},
                "validation_issues": [{"rule": "r", "detail": "d"}],
                "_data_dir": td, "_db_path": str(Path(td) / "x.db"),
            })
        assert seen["issues"] == [{"rule": "r", "detail": "d"}]


# ---------------------------------------------------------------------------
# 幂等包装
# ---------------------------------------------------------------------------

class TestIdempotentWrapper:
    def test_second_run_is_served_from_cache(self, tmp_path):
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        calls = []

        def work() -> dict:
            calls.append(1)
            return {"rel_path": "out/a.docx"}

        state = {"meeting_id": "m-1", "minutes_markdown": "# 稿子\n"}
        r1, cached1 = B.idempotent(store, S.NODE_EXPORT, state, work)
        r2, cached2 = B.idempotent(store, S.NODE_EXPORT, state, work)

        assert calls == [1], "第二次不该真的干活"
        assert cached1 is False
        assert cached2 is True

    def test_cache_hit_returns_the_persisted_ref(self, tmp_path):
        """命中时返回的是**登记的产物引用**，不是上次的返回值。

        这是刻意的取舍：产物要跨进程能重新取回，内存对象不行。
        所以两次调用的返回**类型可能不同**（首次是 `fn` 的返回值，
        命中是引用字符串）—— 这条测试把这个契约钉下来，
        免得以后有人误以为"两次返回的对象相等"。
        """
        store = idem.IdempotencyStore(tmp_path / "idem.db")

        state = {"meeting_id": "m-1", "minutes_markdown": "# 稿子\n"}
        r1, cached1 = B.idempotent(
            store, S.NODE_EXPORT, state, lambda: {"rel_path": "out/a.docx"})
        r2, cached2 = B.idempotent(
            store, S.NODE_EXPORT, state, lambda: {"rel_path": "out/a.docx"})

        assert cached1 is False
        assert r1 == {"rel_path": "out/a.docx"}     # 首次：原样返回 fn 的结果
        assert cached2 is True
        assert r2 == "out/a.docx"                   # 命中：返回持久化的引用

    def test_custom_result_ref_is_honoured_on_hit(self, tmp_path):
        """自定义 `result_ref_of` 要真的被用来登记。"""
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        state = {"meeting_id": "m-1", "minutes_markdown": "# 稿子\n"}

        B.idempotent(store, S.NODE_EXPORT, state, lambda: {"rel_path": "x"},
                     result_ref_of=lambda r: "自定义引用")
        _, cached = B.idempotent(store, S.NODE_EXPORT, state, lambda: {"rel_path": "x"})
        hit = store.get(idem.make_idempotency_key(
            "m-1", S.NODE_EXPORT, idem.node_input_from_state(S.NODE_EXPORT, state)))
        assert cached is True
        assert hit.result_ref == "自定义引用"

    def test_different_input_reruns(self, tmp_path):
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        calls = []

        def work() -> dict:
            calls.append(1)
            return {"rel_path": "out/a.docx"}

        B.idempotent(store, S.NODE_EXPORT, {"meeting_id": "m", "minutes_markdown": "A"}, work)
        B.idempotent(store, S.NODE_EXPORT, {"meeting_id": "m", "minutes_markdown": "B"}, work)
        assert len(calls) == 2

    def test_missing_task_id_raises(self, tmp_path):
        """没有 task_id 就没法构造幂等键 —— 必须报错，不能拿空串当键。

        空串会让**所有**任务的导出撞进同一条记录，第二个任务会
        拿到第一个任务的产物路径。
        """
        store = idem.IdempotencyStore(tmp_path / "idem.db")
        with pytest.raises(Exception) as ei:
            B.idempotent(store, S.NODE_EXPORT, {}, lambda: {"x": 1})
        assert "task_id" in str(ei.value) or "meeting_id" in str(ei.value)

    def test_none_store_runs_unprotected(self):
        """没传 store 时不拦 —— 让 T1-10 之外的单测不必搭库。"""
        calls = []

        def work() -> dict:
            calls.append(1)
            return {"x": 1}

        r1, c1 = B.idempotent(None, S.NODE_EXPORT, {}, work)
        r2, c2 = B.idempotent(None, S.NODE_EXPORT, {}, work)
        assert len(calls) == 2
        assert (c1, c2) == (False, False)
        assert r1 == r2 == {"x": 1}
