"""T1-07 单元测试：术语库领域层（术语匹配 / 文件号保护 / 热词导出）。

对照《测试与验收方案》§2.1 的 `domain/glossary` 用例矩阵：

    U-GL-01 别名精确匹配        「张三丰」不被「张三」的别名规则破坏
    U-GL-02 长别名优先          同时存在「中办」「中办发」时先替换长的
    U-GL-03 停用条目不参与      enabled=False 的条目不出现在 alias_index
    U-GL-04 热词截断            3000 个条目 -> 返回 2000 并触发告警
    U-GL-05 文件号保护          「中办发〔2026〕1 号」不被破坏
    U-GL-06 命中计数准确        同一别名出现 3 次 -> hits == 3

为什么这些用例值得写得这么细
----------------------------
术语替换是**无脑字符串替换**，它的危险不在于"不生效"，而在于
**生效错了还看不出来**：把文件号改成"中共中央办公厅发〔2026〕1 号"，
文本依然通顺，用户不会发现。这类静默损坏只能靠穷举形状的测试来防。
所以下面每个形状（六角括号/方括号/圆括号/无括号）都有独立用例。
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.domain.glossary import (  # noqa: E402
    CATEGORIES,
    CATEGORY_LABELS,
    DEFAULT_HOTWORD_LIMIT,
    GlossaryEntry,
    GlossaryFormatError,
    apply_corrections,
    build_alias_index,
    detect_alias_conflicts,
    export_hotwords,
    iter_alias_pairs,
    parse_aliases,
    parse_csv_rows,
    sort_aliases_longest_first,
    validate_entry,
)


def e(eid, category, canonical, aliases=(), note="", enabled=True):
    return GlossaryEntry(eid, category, canonical, tuple(aliases), note, enabled)


# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------

class TestGlossaryEntry:
    def test_category_labels_cover_all(self):
        """每个合法类别都要有中文标签，否则界面会显示英文 code。"""
        assert set(CATEGORY_LABELS) == CATEGORIES

    def test_reject_unknown_category(self):
        with pytest.raises(GlossaryFormatError, match="未知类别"):
            e("1", "unknown_cat", "某单位")

    @pytest.mark.parametrize("cat", sorted(CATEGORIES))
    def test_accepts_all_documented_categories(self, cat):
        assert e("1", cat, "某词").category == cat

    def test_frozen(self):
        """条目必须不可变 —— 就地改会绕过审计。"""
        entry = e("1", "person", "张三")
        with pytest.raises(Exception):
            entry.canonical = "李四"  # type: ignore[misc]

    def test_to_dict_shape(self):
        d = e("1", "person", "张三", ["张建军"], "备注").to_dict()
        assert d == {
            "entry_id": "1",
            "category": "person",
            "canonical": "张三",
            "aliases": ["张建军"],
            "note": "备注",
            "enabled": True,
        }

    def test_category_label_chinese(self):
        assert e("1", "org", "某单位").category_label == "机构名"


# ---------------------------------------------------------------------------
# 别名解析
# ---------------------------------------------------------------------------

class TestParseAliases:
    def test_pipe_separated(self):
        assert parse_aliases("张建军|张建国同志") == ("张建军", "张建国同志")

    def test_list_input(self):
        assert parse_aliases(["a", "b"]) == ("a", "b")

    def test_json_array_from_db(self):
        """数据库里 aliases 存的是 JSON 数组。"""
        assert parse_aliases('["张建军", "张建国同志"]') == ("张建军", "张建国同志")

    def test_json_not_array_rejected(self):
        with pytest.raises(GlossaryFormatError, match="必须是数组"):
            parse_aliases('{"a": 1}')

    def test_bad_json_rejected(self):
        with pytest.raises(GlossaryFormatError, match="不是合法 JSON"):
            parse_aliases("[张建军")

    def test_blank_items_dropped(self):
        """空白别名会匹配一切 —— 是最危险的脏数据，必须丢。"""
        assert parse_aliases("a||b|  |c") == ("a", "b", "c")

    @pytest.mark.parametrize("raw", [None, "", "   ", [], ["", "  "]])
    def test_empty_inputs(self, raw):
        assert parse_aliases(raw) == ()

    def test_dedupe_keeps_order(self):
        assert parse_aliases("b|a|b") == ("b", "a")

    def test_strips_whitespace(self):
        assert parse_aliases(" a | b ") == ("a", "b")

    # -- T1-14 补：非字符串 / 非序列输入必须显式拒绝 ----------------------
    #
    # ★ 这三条是验收脚本 `accept_t1_14.py` 抓到的**真 bug**。
    # 原实现只有 `isinstance(raw, str)` 一个判断，然后就 `for a in raw:`：
    #   · `parse_aliases(12345)` → `TypeError: 'int' object is not iterable`
    #     → 冒到路由层 → 被兜底处理器报成 E9001（内部错误）。
    #     但"用户把 aliases 填成数字"是**可预期的输入问题**，该报 E1001。
    #   · `parse_aliases({'a': 1})` → **静默返回 `('a',)`** ——
    #     遍历 dict 拿到的是**键**。这正是本函数要防的"垃圾静默入库"。
    # 修法：类型白名单，之外一律 `GlossaryFormatError`。

    @pytest.mark.parametrize("raw", [12345, 3.5, True, {"a": 1}, object()])
    def test_non_string_non_sequence_rejected(self, raw):
        """白名单之外的输入一律报 `GlossaryFormatError`，**不抛裸 TypeError**。

        判据是**异常类型**，不只是"抛了异常"：裸 `TypeError` 会被上层的
        `except Exception` 兜成 E9001（内部错误），把输入问题误报成系统故障。
        """
        with pytest.raises(GlossaryFormatError, match="类型不支持"):
            parse_aliases(raw)

    def test_dict_is_not_silently_iterated(self):
        """★ `{'a': 1}` 绝不能返回 `('a',)`。

        单独拎出来是因为它的**失败方式**与上面不同：
        dict 是可迭代的，所以它不会抛 TypeError，而是**静默**把键当别名收下 ——
        这种"不报错的错误"最危险，因为没人会发现。
        """
        with pytest.raises(GlossaryFormatError):
            parse_aliases({"a": 1})

    @pytest.mark.parametrize("raw", [["a"], ("a",), {"a"}, frozenset({"a"})])
    def test_sequence_types_still_accepted(self, raw):
        """白名单本身不能收得太紧 —— list/tuple/set/frozenset 都得照收。"""
        assert parse_aliases(raw) == ("a",)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

class TestValidateEntry:
    def test_valid_entry_has_no_problems(self):
        assert validate_entry(e("1", "person", "张三", ["张建军"])) == []

    def test_empty_canonical(self):
        problems = validate_entry(e("1", "person", "   "))
        assert any("不能为空" in p for p in problems)

    def test_alias_equals_canonical_is_flagged(self):
        """别名等于规范写法 = 无意义替换，还白占热词名额。"""
        problems = validate_entry(e("1", "person", "张三", ["张三"]))
        assert any("无意义替换" in p for p in problems)

    def test_canonical_with_surrounding_space(self):
        problems = validate_entry(e("1", "person", " 张三 "))
        assert any("首尾有空白" in p for p in problems)

    def test_returns_all_problems_not_just_first(self):
        """批量导入时用户要一次看全，不是修一个报一个。"""
        problems = validate_entry(e("1", "person", "  ", ["  "]))
        assert len(problems) >= 2


# ---------------------------------------------------------------------------
# U-GL-03 停用条目不参与
# ---------------------------------------------------------------------------

class TestIterAliasPairs:
    def test_disabled_entry_excluded(self):
        entries = [
            e("1", "person", "张三", ["张建军"], enabled=True),
            e("2", "person", "李四", ["李建军"], enabled=False),
        ]
        pairs = list(iter_alias_pairs(entries))
        assert pairs == [("张建军", "张三", "1")]

    def test_enabled_flag_does_not_hide_canonical_only_entries(self):
        """没有别名的启用条目不该出现在 alias_index（它本来就无处可匹配）。"""
        assert list(iter_alias_pairs([e("1", "person", "张三")])) == []


class TestBuildAliasIndex:
    """`build_alias_index` 的契约。

    索引里既有**真别名**（`alias != canonical`），也有**恒等条目**
    （`canonical -> canonical`）。后者不是冗余：没有别名的术语
    （如「张三丰」）必须能让 `apply_corrections` 看见，
    否则它的规范写法会被别的短别名切碎（施工卡「张三」/「张三丰」用例）。
    所以断言索引形状时要用 `real_alias_index()` 把恒等条目滤掉，
    下面的 `_aliases_only` 就是干这个的。
    """

    @staticmethod
    def _aliases_only(idx: dict) -> dict:
        return {a: v for a, v in idx.items() if a != v[0]}

    def test_simple_mapping(self):
        idx = build_alias_index([e("1", "person", "张三", ["张建军"])])
        assert self._aliases_only(idx) == {"张建军": ("张三", "1")}

    def test_canonicals_are_registered_as_identity(self):
        """没别名的术语也要进索引（否则保护不了它）。"""
        idx = build_alias_index([e("1", "person", "张三丰", [])])
        assert idx == {"张三丰": ("张三丰", "1")}
        assert self._aliases_only(idx) == {}

    def test_identity_entries_do_not_produce_hits(self):
        """恒等条目不是规则，不该产生"改了 N 处"的假命中。"""
        idx = build_alias_index([e("1", "person", "张三丰", [])])
        out, hits, details = apply_corrections("张三丰出席", idx)
        assert out == "张三丰出席"
        assert hits == 0
        assert details == []

    def test_ugl03_disabled_not_in_index(self):
        """U-GL-03：停用条目不出现在 alias_index。"""
        idx = build_alias_index([
            e("1", "person", "张三", ["张建军"], enabled=True),
            e("2", "person", "李四", ["李建军"], enabled=False),
        ])
        assert "李建军" not in idx
        assert "张建军" in idx
        assert "李四" not in idx      # 停用条目的规范写法也不该进
        assert "张三" in idx

    def test_skip_when_excludes_canonicals(self):
        idx = build_alias_index(
            [e("1", "person", "张三", ["张建军"]),
             e("2", "org", "某单位", ["某单位简称"])],
            skip_when=["张三"],
        )
        assert "张建军" not in idx
        assert "某单位简称" in idx

    def test_conflict_resolution_is_order_independent(self):
        """**关键**：同一别名指向两个规范写法时，结果不能随条目顺序变化。

        最初实现是"先到先得"，于是换一个 ORDER BY、重导一次 CSV，
        同一个别名就会替换成别的词 —— 同一份音频两次跑出不同的稿子。
        这种不确定性比"选错一条"危险得多。
        """
        a = e("gl-0002", "person", "张建国", ["张建军"])
        b = e("g-9002", "person", "张三", ["张建军"])
        assert build_alias_index([a, b]) == build_alias_index([b, a])

    def test_conflict_default_picks_longest_canonical(self):
        a = e("1", "org", "中办", ["某写法"])
        b = e("2", "org", "中共中央办公厅", ["某写法"])
        idx = build_alias_index([a, b])
        assert idx["某写法"][0] == "中共中央办公厅"

    def test_conflict_first_mode_reproduces_legacy_order_dependence(self):
        """`on_conflict="first"` 保留旧行为，供需要手工控制顺序的场景。"""
        a = e("1", "org", "中办", ["某写法"])
        b = e("2", "org", "中共中央办公厅", ["某写法"])
        assert build_alias_index([a, b], on_conflict="first")["某写法"][0] == "中办"
        assert build_alias_index([b, a], on_conflict="first")["某写法"][0] == "中共中央办公厅"

    def test_conflict_longest_mode(self):
        a = e("1", "org", "中办", ["某写法"])
        b = e("2", "org", "中共中央办公厅", ["某写法"])
        assert build_alias_index([a, b], on_conflict="longest")["某写法"][0] == "中共中央办公厅"

    def test_no_conflict_no_problem(self):
        idx = build_alias_index([
            e("1", "person", "张三", ["张建军"]),
            e("2", "person", "李四", ["李建军"]),
        ])
        assert len(self._aliases_only(idx)) == 2

    def test_alias_equal_to_own_canonical_is_not_overwritten(self):
        """条目自己的别名跟别的条目的规范写法同名时，真别名映射优先。"""
        idx = build_alias_index([
            e("1", "person", "张三", ["李四"]),   # 「李四」是张三的别名
            e("2", "person", "李四", []),          # 同时「李四」也是规范写法
        ])
        # 真别名映射不能被恒等条目覆盖掉
        assert idx["李四"] == ("张三", "1")


class TestDetectAliasConflicts:
    def test_finds_conflict(self):
        conflicts = detect_alias_conflicts([
            e("1", "person", "张三", ["张建军"]),
            e("2", "person", "李四", ["张建军"]),
        ])
        assert conflicts == {"张建军": ["张三", "李四"]}

    def test_no_conflict_when_same_target(self):
        """两个条目映射到**同一个**规范写法不是冲突（是重复录入，另说）。"""
        conflicts = detect_alias_conflicts([
            e("1", "person", "张三", ["张建军"]),
            e("2", "person", "张三", ["张建军"]),
        ])
        assert conflicts == {}

    def test_disabled_excluded(self):
        conflicts = detect_alias_conflicts([
            e("1", "person", "张三", ["张建军"], enabled=True),
            e("2", "person", "李四", ["张建军"], enabled=False),
        ])
        assert conflicts == {}

    def test_empty(self):
        assert detect_alias_conflicts([]) == {}


# ---------------------------------------------------------------------------
# U-GL-02 长别名优先
# ---------------------------------------------------------------------------

class TestSortAliasesLongestFirst:
    def test_ugl02_longest_first(self):
        """U-GL-02：长的排前面。"""
        assert sort_aliases_longest_first(["中办", "中办发", "中办发文"]) == [
            "中办发文", "中办发", "中办",
        ]

    def test_tie_break_by_dict_order_for_determinism(self):
        """同长度按字典序 —— 保证结果确定，否则两次运行可能不同。"""
        assert sort_aliases_longest_first(["b", "a", "c"]) == ["a", "b", "c"]

    def test_dedupes(self):
        assert sort_aliases_longest_first(["a", "a", "b"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# U-GL-01 / U-GL-06 替换与计数
# ---------------------------------------------------------------------------

class TestApplyCorrections:
    def test_empty_text(self):
        assert apply_corrections("", {"a": ("b", "1")}) == ("", 0, [])

    def test_empty_index(self):
        assert apply_corrections("原文", {}) == ("原文", 0, [])

    def test_ugl01_zhang_sanfeng_not_broken(self):
        """U-GL-01：「张三丰」不被「张三」的规则破坏。

        正确性来自"别名是完整的词"这一约定：条目 `张三` 的别名是
        `张建军`，所以「张三丰」压根不匹配任何别名，原样保留。
        """
        idx = build_alias_index([e("1", "person", "张三", ["张建军"])])
        out, hits, _ = apply_corrections("张三丰同志参加了会议，张建军也来了。", idx)
        assert out == "张三丰同志参加了会议，张三也来了。"
        assert "张三丰" in out          # 关键断言：没被写成「张三丰丰」
        assert hits == 1

    def test_ugl02_long_alias_wins(self):
        """U-GL-02：同时存在「中办」与「中办发」时，长别名先替换。"""
        idx = build_alias_index([
            e("1", "org", "中共中央办公厅", ["中办"]),
            e("2", "org", "中共中央办公厅发文", ["中办发"]),
        ])
        out, _, _ = apply_corrections("中办发出的文件", idx)
        # 长别名「中办发」先命中，替换后不再含有「中办」，所以短别名不会二次替换
        assert out == "中共中央办公厅发文出的文件"

    def test_ugl06_hit_count_exact(self):
        """U-GL-06：同一别名出现 3 次 -> hits == 3。"""
        idx = build_alias_index([e("1", "person", "张三", ["张建军"])])
        out, hits, details = apply_corrections("张建军、张建军和张建军", idx)
        assert hits == 3
        assert out == "张三、张三和张三"
        assert len(details) == 1
        assert details[0].count == 3

    def test_hit_count_counts_all_aliases(self):
        idx = build_alias_index([
            e("1", "person", "张三", ["张建军"]),
            e("2", "org", "某单位", ["某单位简称"]),
        ])
        _, hits, details = apply_corrections("张建军与某单位简称", idx)
        assert hits == 2
        assert len(details) == 2

    def test_details_carry_entry_id(self):
        """命中明细要能指回具体条目 —— 复核页要显示"这条来自哪个术语"。"""
        idx = build_alias_index([e("gl-0007", "person", "张三", ["张建军"])])
        _, _, details = apply_corrections("张建军", idx)
        assert details[0].entry_id == "gl-0007"
        assert details[0].to_dict()["alias"] == "张建军"

    def test_no_match_returns_original(self):
        idx = build_alias_index([e("1", "person", "张三", ["张建军"])])
        out, hits, details = apply_corrections("毫不相干的文本", idx)
        assert (out, hits, details) == ("毫不相干的文本", 0, [])

    def test_replacement_is_deterministic_across_calls(self):
        """同一输入两次结果必须相同（否则复核记录无法复现）。"""
        idx = build_alias_index([
            e("1", "person", "张三", ["张建军"]),
            e("2", "org", "某单位", ["某单位简称"]),
        ])
        text = "张建军和某单位简称"
        assert apply_corrections(text, idx) == apply_corrections(text, idx)


# ---------------------------------------------------------------------------
# 施工卡「张三」/「张三丰」用例：规范写法保护
# ---------------------------------------------------------------------------

class TestCanonicalProtection:
    """施工卡标准 2 的真实形状：别名切碎别人的**规范写法**。

    施工卡原文：「张三」与「张三丰」同时存在时，文本「张三丰」
    不被错误替换成「张三丰丰」。

    注意这个用例**不是**"两个别名互吃"。它的形状是：

        「张三」  是别名（指向李四）
        「张三丰」是另一条术语的规范写法（没有别名）

    「张三丰」压根不在别名索引里，所以"别名按长度倒序"救不了它 ——
    没有任何东西在跟「张三」比长度。唯一解法是把规范写法也遮蔽起来。
    这一组测试就是钉住这个机制。
    """

    def test_longer_canonical_survives_shorter_alias(self):
        idx = build_alias_index([
            e("1", "person", "张三丰"),
            e("2", "person", "李四", ["张三"]),
        ])
        out, hits, details = apply_corrections("会议请张三丰同志介绍经验", idx)
        assert "张三丰" in out
        assert "李四丰" not in out
        # 「张三丰」本来就写对了，不该产生任何修改记录
        assert hits == 0
        assert details == []

    def test_shorter_alias_still_replaced_when_standalone(self):
        """保护规范写法不能变成"干脆不替换了"。"""
        idx = build_alias_index([
            e("1", "person", "张三丰"),
            e("2", "person", "李四", ["张三"]),
        ])
        out, hits, _ = apply_corrections("张三同志请假", idx)
        assert out == "李四同志请假"
        assert hits == 1

    def test_mixed_sentence_handles_both(self):
        idx = build_alias_index([
            e("1", "person", "张三丰"),
            e("2", "person", "李四", ["张三"]),
        ])
        out, hits, _ = apply_corrections("张三丰和张三都来了", idx)
        assert out == "张三丰和李四都来了"
        assert hits == 1

    def test_correction_is_idempotent(self):
        """跑两遍结果必须一样。

        这是遮蔽规范写法带来的免费好处：第一遍的产物已经是规范写法，
        第二遍会被保护起来，不会被再改一次。
        """
        idx = build_alias_index([
            e("1", "person", "张三丰"),
            e("2", "person", "李四", ["张三"]),
        ])
        once = apply_corrections("会议请张三丰同志，张三负责记录", idx)[0]
        twice = apply_corrections(once, idx)[0]
        assert once == twice

    def test_three_level_prefix_chain(self):
        """甲乙 / 甲乙丙 / 甲乙丙丁 三层，谁也不许切谁。"""
        idx = build_alias_index([
            e("c1", "term", "甲乙", ["甲乙戊"]),
            e("c2", "term", "甲乙丙"),
            e("c3", "term", "甲乙丙丁"),
        ])
        assert apply_corrections("甲乙丙丁", idx)[0] == "甲乙丙丁"
        assert apply_corrections("甲乙丙", idx)[0] == "甲乙丙"
        assert apply_corrections("甲乙", idx)[0] == "甲乙"

    def test_alias_containing_own_canonical_still_works(self):
        """别名**包含**自己的规范写法时，替换必须照常发生。

        「某单位简称」-> 「某单位」：规范写法「某单位」是别名「某单位简称」
        的前缀。如果无脑遮蔽规范写法，别名就匹配不上了，该改的没改。
        实现上靠 `avoid`（别名覆盖的位置不让给保护）解决。
        """
        idx = build_alias_index([e("1", "org", "某单位", ["某单位简称"])])
        out, hits, _ = apply_corrections("某单位简称发来通知", idx)
        assert out == "某单位发来通知"
        assert hits == 1

    def test_alias_containing_canonical_plus_ambiguous_occurrence(self):
        """同一句里既有"该改的别处"、又有"已写对的规范写法"，两边都对。"""
        idx = build_alias_index([e("1", "org", "某单位", ["某单位简称"])])
        out, hits, _ = apply_corrections("某单位简称和某单位都要参会", idx)
        assert out == "某单位和某单位都要参会"
        assert hits == 1

    def test_file_number_and_canonical_protection_coexist(self):
        """两道防线同时生效，互不干扰。"""
        idx = build_alias_index([
            e("1", "org", "中共中央办公厅", ["中办"]),
            e("2", "person", "张三丰"),
            e("3", "person", "李四", ["张三"]),
        ])
        out, _, _ = apply_corrections("中办印发了中办发〔2026〕1 号，张三丰参会", idx)
        assert "中办发〔2026〕1 号" in out
        assert out.startswith("中共中央办公厅印发了")
        assert "张三丰参会" in out

    def test_masking_does_not_leak_placeholders(self):
        """还原必须彻底：输出里绝不能残留 \\x00 占位符。"""
        idx = build_alias_index([
            e("1", "org", "中共中央办公厅", ["中办"]),
            e("2", "person", "张三丰"),
        ])
        out, _, _ = apply_corrections("中办就中办发〔2026〕1 号问张三丰", idx)
        assert "\x00" not in out
        assert "〔2026〕1 号" in out


# ---------------------------------------------------------------------------
# U-GL-05 文件号保护
# ---------------------------------------------------------------------------

class TestFileNumberProtection:
    """U-GL-05：「中办发〔2026〕1 号」不被破坏。

    这一组是 T1-07 最重要的测试。文件号被改坏是**静默**的：
    改完的句子依然通顺，用户不会发现，但公文引用已经错了。
    """

    def _idx(self):
        # 故意构造最危险的情形：短别名是文件号代字的子串
        return build_alias_index([
            e("1", "org", "中共中央办公厅", ["中办"]),
            e("2", "org", "中共中央办公厅发文", ["中办发"]),
        ])

    @pytest.mark.parametrize("text", [
        "依据中办发〔2026〕1 号文件精神",
        "依据中办发[2026]15号文件",
        "依据中办发（2026）3 号文件",
        "中办发〔2026〕1号",
        "  中办发〔2026〕1 号  ",
    ])
    def test_file_number_untouched(self, text):
        out, _, _ = apply_corrections(text, self._idx())
        assert out == text, f"文件号被破坏：{text} -> {out}"

    def test_file_number_preserved_while_other_alias_still_replaced(self):
        """保护文件号不能顺带把正常替换也关掉。"""
        out, hits, _ = apply_corrections(
            "中办印发了中办发〔2026〕1 号", self._idx()
        )
        assert "中办发〔2026〕1 号" in out           # 文件号完好
        assert "中共中央办公厅印发了" in out          # 正常处已替换
        assert hits == 1

    def test_protection_can_be_turned_off(self):
        """关掉保护后应能看到破坏 —— 证明保护真的在起作用，不是巧合。"""
        out, _, _ = apply_corrections(
            "依据中办发〔2026〕1 号文件精神",
            self._idx(),
            protect_file_numbers=False,
        )
        assert out != "依据中办发〔2026〕1 号文件精神"
        assert "中共中央办公厅" in out

    def test_short_alias_alone_still_replaced(self):
        """没有文件号形状时，「中办」照常替换。"""
        out, hits, _ = apply_corrections("中办下发了通知", self._idx())
        assert out == "中共中央办公厅下发了通知"
        assert hits == 1

    def test_multiple_file_numbers_all_preserved(self):
        text = "中办发〔2026〕1 号、中办发〔2026〕2 号和国办发〔2025〕9 号"
        out, _, _ = apply_corrections(text, self._idx())
        assert out == text

    def test_file_number_at_start_and_end(self):
        text = "中办发〔2026〕1 号提到中办"
        out, hits, _ = apply_corrections(text, self._idx())
        assert out.startswith("中办发〔2026〕1 号")
        assert out.endswith("中共中央办公厅")
        assert hits == 1

    def test_unmask_handles_adjacent_numbers(self):
        """两个相邻文件号的占位符不能被搞混（\\x001\\x00 是 \\x0011\\x00 的前缀）。"""
        text = "中办发〔2026〕1 号中办发〔2026〕10 号"
        out, _, _ = apply_corrections(text, self._idx())
        assert out == text


# ---------------------------------------------------------------------------
# U-GL-04 热词导出与截断
# ---------------------------------------------------------------------------

class TestExportHotwords:
    def test_canonical_only_no_aliases(self):
        """施工卡：只取 canonical，不含 aliases。

        把错误写法也喂给引擎会让两头都激活，反而降低准确率。
        """
        r = export_hotwords([e("1", "person", "张三", ["张建军"])])
        assert r.words == ("张三",)
        assert "张建军" not in r.words

    def test_ugl04_truncation_and_warning(self):
        """U-GL-04：3000 个条目 -> 返回 2000 个并触发告警。"""
        entries = [e(f"{i:05d}", "term", f"词{i}") for i in range(3000)]
        r = export_hotwords(entries)
        assert r.count == DEFAULT_HOTWORD_LIMIT == 2000
        assert r.truncated is True
        assert r.dropped == 1000
        assert len(r.warnings) == 1
        assert "3000" in r.warnings[0] and "2000" in r.warnings[0]

    def test_ugl04_warning_explains_consequence(self):
        """告警不能只说"超限"，要说清"引擎可能整体不生效"这个后果。"""
        entries = [e(f"{i:05d}", "term", f"词{i}") for i in range(2001)]
        w = export_hotwords(entries).warnings[0]
        assert "整体不生效" in w
        assert "截断" in w
        # 还要给出可执行的下一步
        assert "建议" in w

    def test_no_truncation_no_warning(self):
        """不超限时不能报警 —— 误报会让真告警被淹没。"""
        r = export_hotwords([e("1", "term", "词")])
        assert r.truncated is False
        assert r.warnings == ()
        assert r.dropped == 0

    def test_exactly_at_limit_is_not_truncated(self):
        entries = [e(f"{i:05d}", "term", f"词{i}") for i in range(2000)]
        r = export_hotwords(entries)
        assert r.count == 2000 and r.truncated is False

    def test_disabled_excluded(self):
        r = export_hotwords([
            e("1", "term", "启用词", enabled=True),
            e("2", "term", "停用词", enabled=False),
        ])
        assert r.words == ("启用词",)

    def test_dedupe_same_canonical(self):
        r = export_hotwords([
            e("1", "term", "同一个词"),
            e("2", "term", "同一个词"),
        ])
        assert r.words == ("同一个词",)

    def test_person_and_org_rank_before_term(self):
        """截断时保住最依赖热词的词：人名/机构名优先于通用术语。

        「党组会」这类通用词引擎本来就认得；人名同音字才是真需要引导的。
        """
        entries = [e("1", "term", "党组会"), e("2", "person", "张建国"),
                   e("3", "org", "某机关"), e("4", "codename", "某项目")]
        r = export_hotwords(entries)
        assert r.words[0] == "张建国"
        assert r.words[-1] == "党组会"

    def test_truncation_keeps_high_priority_words(self):
        """2001 个 term + 1 个 person，截断后 person 必须在。"""
        entries = [e(f"t{i:05d}", "term", f"词{i}") for i in range(2001)]
        entries.append(e("p1", "person", "张建国"))
        r = export_hotwords(entries)
        assert "张建国" in r.words

    def test_limit_configurable(self):
        entries = [e(f"{i}", "term", f"词{i}") for i in range(50)]
        r = export_hotwords(entries, limit=10)
        assert r.count == 10 and r.limit == 10 and r.dropped == 40

    def test_nonpositive_limit_rejected(self):
        with pytest.raises(GlossaryFormatError, match="上限必须为正数"):
            export_hotwords([], limit=0)

    def test_to_dict_shape(self):
        r = export_hotwords([e("1", "term", "词")])
        d = r.to_dict()
        assert set(d) == {"count", "total", "limit", "truncated", "dropped", "warnings"}


# ---------------------------------------------------------------------------
# CSV 解析
# ---------------------------------------------------------------------------

class TestParseCsvRows:
    def _rows(self, text: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(text)))

    def test_basic_import(self):
        rows = self._rows(
            "category,canonical,aliases,note\n"
            "person,张建国,张建军|张建国同志,备注\n"
        )
        entries, problems = parse_csv_rows(rows)
        assert problems == []
        assert len(entries) == 1
        assert entries[0].category == "person"
        assert entries[0].canonical == "张建国"
        assert entries[0].aliases == ("张建军", "张建国同志")
        assert entries[0].note == "备注"

    def test_generated_entry_id(self):
        rows = self._rows("category,canonical,aliases,note\nterm,某词,,\n")
        entries, _ = parse_csv_rows(rows, id_prefix="gl")
        assert entries[0].entry_id == "gl-0001"

    def test_explicit_entry_id_used(self):
        rows = self._rows(
            "category,canonical,aliases,note,entry_id\nterm,某词,,,custom-1\n"
        )
        entries, _ = parse_csv_rows(rows)
        assert entries[0].entry_id == "custom-1"

    def test_comment_lines_skipped(self):
        """CSV 没有官方注释语法，本项目用 `#` 开头（示例文件就是这么写的）。"""
        rows = self._rows(
            "category,canonical,aliases,note\n"
            "# 这是注释,注释内容,,\n"
            "term,某词,,\n"
        )
        entries, problems = parse_csv_rows(rows)
        assert problems == []
        assert len(entries) == 1
        assert entries[0].canonical == "某词"

    def test_blank_lines_skipped(self):
        rows = self._rows("category,canonical,aliases,note\n,,,\nterm,某词,,\n")
        entries, problems = parse_csv_rows(rows)
        assert problems == []
        assert len(entries) == 1

    def test_unknown_category_reports_line_number(self):
        rows = self._rows("category,canonical,aliases,note\nbogus,某词,,\n")
        entries, problems = parse_csv_rows(rows)
        assert entries == []
        assert "第 1 行" in problems[0] and "bogus" in problems[0]

    def test_duplicate_category_canonical_reported(self):
        rows = self._rows(
            "category,canonical,aliases,note\n"
            "term,某词,,\n"
            "term,某词,,\n"
        )
        entries, problems = parse_csv_rows(rows)
        assert len(entries) == 1
        assert any("重复" in p for p in problems)

    def test_missing_canonical_reported(self):
        rows = self._rows("category,canonical,aliases,note\nterm,,,\n")
        entries, problems = parse_csv_rows(rows)
        assert entries == []
        assert any("缺 canonical" in p for p in problems)

    def test_all_problems_collected_not_just_first(self):
        """一次列全，避免"改一行再导一次"的来回。"""
        rows = self._rows(
            "category,canonical,aliases,note\n"
            "bogus1,词1,,\n"
            "bogus2,词2,,\n"
            "term,,,\n"
        )
        _, problems = parse_csv_rows(rows)
        assert len(problems) == 3

    def test_enabled_column_parsed(self):
        rows = self._rows(
            "category,canonical,aliases,note,enabled\n"
            "term,启用词,,,1\n"
            "term,停用词,,,0\n"
        )
        entries, _ = parse_csv_rows(rows)
        by_name = {x.canonical: x.enabled for x in entries}
        assert by_name == {"启用词": True, "停用词": False}

    def test_whitespace_stripped(self):
        rows = self._rows("category,canonical,aliases,note\n  term , 某词 ,,\n")
        entries, problems = parse_csv_rows(rows)
        assert problems == []
        assert entries[0].category == "term"
        assert entries[0].canonical == "某词"

    def test_real_config_file_imports_cleanly(self):
        """仓库里那份示例 glossary.csv 必须能无问题导入（施工卡验收标准 1）。"""
        path = PROJECT_ROOT / "config" / "glossary.csv"
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        entries, problems = parse_csv_rows(rows)
        assert problems == [], f"示例术语库有格式问题：{problems}"
        assert len(entries) >= 4
        assert {x.category for x in entries} <= CATEGORIES
