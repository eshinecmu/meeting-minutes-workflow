"""术语匹配与热词导出（T1-07 领域层核心）。

三个能力，全部是纯函数：

  · `build_alias_index()`   —— 条目 -> 「易错写法 -> 规范写法」索引（长优先）
  · `apply_corrections()`   —— 转写后回改错字，返回 (新文本, 命中数, 逐条明细)
  · `export_hotwords()`     —— 导出给 ASR 引擎的热词表（含截断告警）

设计上刻意**不做**的三件事（以及为什么）
----------------------------------------
1. **不做分词**。中文分词器会把"中共中央办公厅"切成若干词，
   而术语库要匹配的恰恰是完整专名。用最长优先的子串匹配更可控 ——
   代价是可能误伤（见文件号保护），但那是可控、可测的代价。
2. **不做模糊匹配**（编辑距离/拼音）。看似高级，实则危险：
   会议纪要里"张三"改成"李四"是不可接受的错误。术语库只做**确定性**替换，
   模糊的留给 T1-08 的低置信复核（人工看）。
3. **不静默修改**。每次替换都返回明细（哪个别名、命中几次、改成了什么），
   调用方必须把这些写进审计与复核页。用户看不到的修改等于没有修改。

热词上限 2000 的依据
-------------------
施工卡：`export_hotwords(entries, limit=2000)`，且"超限可能整体不生效（静默失效）"。
所以超限不是"少给几个词"，而是"一个都不生效"。这种失败模式决定了策略：
**必须截断 + 必须告警**，绝不能悄悄截断让用户以为热词生效了。
告警文案要说清"截了多少、为什么、有什么后果"，而不是一句"超限"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: 术语类别。与 `001_init.sql` 的 `glossary_entry.category` CHECK 约束一致 ——
#: 两处必须同步，否则 CSV 能导入但写库会失败（或在库层被静默拒绝）。
CATEGORIES: frozenset[str] = frozenset({"org", "person", "term", "codename"})

#: 类别 -> 中文说明。给界面用，避免前端再维护一份映射。
CATEGORY_LABELS: dict[str, str] = {
    "org": "机构名",
    "person": "人名职务",
    "term": "业务术语",
    "codename": "项目代号",
}

#: 导出给 ASR 的热词上限（施工卡原文 2000）。
#:
#: 这个数字不是"性能考虑"，而是**引擎的硬约束**：FunASR 的 hotword
#: 对数量敏感，超限可能整体不生效——静默失效。截断 + 告警是唯一正确的处理。
DEFAULT_HOTWORD_LIMIT = 2000

#: 别名索引的类型别名。值是 `(canonical, entry_id)`，
#: 带上 entry_id 是为了让命中明细能指回具体条目（复核页要显示"这条来自哪个术语"）。
ALIAS_INDEX_TYPE = dict[str, tuple[str, str]]


class GlossaryFormatError(ValueError):
    """术语库格式错误（CSV 缺列、类别非法等）。

    刻意**不继承 MmwfError**：这是"导入的文件格式不对"，
    属于输入数据问题，由导入接口转成 E1001 类的用户提示；
    领域层不该依赖错误码表（那在 errors.py 里，且 E1xxx 面向"音频/文字稿"）。
    """


# ---------------------------------------------------------------------------
# 文件号保护
# ---------------------------------------------------------------------------

#: 代字部分**必须定长上限**，这是整个保护机制的关键。
#:
#: 早先写成 `[^\s〔〕\[\]（）()]{2,20}`（2-20 个任意非空白非括号字符），
#: 正则贪婪匹配，于是
#:
#:     中办印发了中办发〔2026〕1 号
#:     └──────┬──────┘
#:      整段被当成"代字"吞掉
#:
#: 遮蔽结果把「中办印发了」也一起保护了，别名替换自然就碰不到它。
#: 正确做法是给代字加**上界**：机关代字（中办发 / 国办发 / 京政办字 /
#: 组通字）实际长度在 3-8 字之间，取 `{3,8}` 既能覆盖全部常见写法，
#: 又短到吞不下「中办印发了中办发」这种 9 字前缀。
#:
#: `{3,8}` 配合"紧贴左括号"的语义即可：正则从右往左回退到恰好 8 字时
#: 会停在「印发了中办发」上——仍然多吞 3 字。所以还要**排除叙事助词
#: 与指代词**：它们是中文里连接动词/名词的高频字（了/的/和/与/至/于/
#: 及/在/是/为/第/据/经/由/见/称/等/此/该/本/份/次/项/上/下/前/后），
#: 公文代字绝不会包含它们。两道约束叠加后，`中办印发了中办发` 会从
#: 右侧锚定到 `中办发`（3 字，且不含禁用字），`组通字`、`京政办字`、
#: `国办发电` 照样完整保留，而「这份〔2026〕1 号」也不会把"这份"吃进去。
#:
#: 注意这里**故意不做过度精细的边界判定**：万一多吞一个字，代价只是
#: 那一小段没被别名替换（原文照旧、不产生错误改写）；而少吞一个字的
#: 代价却是文件号被拆开、年份被替换成别的词——前者可容忍，后者不可。
#: 宁可略保守。
_FILE_NUMBER_STOP_CHARS = "了的和与至于及在是为第据经由见称等此该本份次项上下前后就都也又还再"
_PATTERN_FILE_NUMBER = (
    r"[^" + _FILE_NUMBER_STOP_CHARS + r"\s〔〕\[\]（）()]"
    r"{2,7}"
    r"[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*\d{1,4}\s*号"
)

#: 「文件号」形状。覆盖会议纪要里最常见的四种写法：
#:
#:   中办发〔2026〕1 号      —— 六角括号（党政机关公文标准用法）
#:   国办发[2026]15号        —— 半角方括号（口头语/早期文档常见）
#:   京政办字（2026）3 号    —— 全角圆括号
#:   组通字〔2026〕12 号     —— 其他机关代字组合
#:
#: 代字组合是**开放集合**（每个单位都有自己的代字），枚举必然漏。
#: 所以用"代字 + 括号 + 年份 + 序号 + 号"这个**结构**识别，
#: 而不是维护词表（详见 `_PATTERN_FILE_NUMBER` 的边界说明）。
_PROTECTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(_PATTERN_FILE_NUMBER),
)


#: 占位符的命名空间前缀。
#:
#: **为什么必须分命名空间**：遮蔽分两趟（文件号一趟、规范写法一趟），
#: 两趟各自一个 list、各自从 0 编号。如果都用 `\x00{0}\x00`，
#: 第一趟的 0 号与第二趟的 0 号长得一模一样，还原时先还原哪一趟
#: 都会把另一趟的占位符当成自己的，于是文件号变成了"张三丰"、
#: 规范写法变成了别的什么 —— 实测就是这样把一句好好的话
#: 变成「张三丰问张三丰」的。
#:
#: 用不同前缀（`\x00F{n}\x00` / `\x00C{n}\x00`）后两趟互不干扰，
#: 还原顺序也不再重要。`\x00` 仍然保证占位符不会被任何别名匹配到。
_NS_FILE = "\x00F"
_NS_CANON = "\x00C"


def _mask_values(
    text: str,
    values: Sequence[str],
    *,
    avoid: Sequence[str] = (),
    namespace: str = _NS_CANON,
) -> tuple[str, list[str]]:
    """把 `values` 里出现的片段换成占位符。返回 (遮蔽后文本, 被抽出的原件)。

    与 `_mask_protected` 共用占位符机制（`\\x00` 包夹序号），
    但这里的"要遮蔽什么"是动态的（规范写法表），不是正则模式。
    按**长度倒序**处理：如果先遮蔽"张三"，"张三丰" 就只剩"丰"能匹配了。

    Args:
        values: 要遮蔽的片段（规范写法）。
        avoid: 这些片段出现的位置**不许遮**。
        namespace: 占位符前缀，避免与别的遮蔽趟次撞号（见 `_NS_FILE`）。

    `avoid` 的存在理由（一个真实的踩坑）
    ----------------------------------
    有些条目的别名**包含**自己的规范写法：

        规范写法「某单位」  别名「某单位简称」  正文「某单位简称」

    如果无脑遮蔽「某单位」，正文变成「\\x000\\x00简称」，
    别名「某单位简称」就再也匹配不上了 —— 该改的没改。
    这不是理论问题，本卡第一次实现时就把它跑挂了。

    所以遮蔽前先算：这个规范写法在每个出现位置，是不是落在某个别名的
    覆盖范围内？是就跳过（那个位置交给别名替换，本来就该被改）。
    只遮蔽"不被任何别名覆盖"的出现位置。
    """
    if not values:
        return text, []

    # 先算出所有别名的覆盖区间，用来判断哪些位置"该让给别名替换"
    alias_spans: list[tuple[int, int]] = []
    for a in avoid:
        if not a:
            continue
        start = 0
        while True:
            i = text.find(a, start)
            if i < 0:
                break
            alias_spans.append((i, i + len(a)))
            start = i + 1

    def _covered(pos: int, length: int) -> bool:
        end = pos + length
        return any(s <= pos and end <= e for s, e in alias_spans)

    out = text
    protected: list[str] = []
    # 长优先：短的是长的前缀时才不会先被换掉
    for v in sort_aliases_longest_first(values):
        if not v or v not in out:
            continue
        # 逐位置处理：该给别名的位置留着，其余的换成占位符
        pieces: list[str] = []
        cursor = 0
        replaced_any = False
        while True:
            i = out.find(v, cursor)
            if i < 0:
                pieces.append(out[cursor:])
                break
            pieces.append(out[cursor:i])
            if _covered(i, len(v)):
                pieces.append(v)          # 让给别名替换
            else:
                pieces.append(f"{namespace}{len(protected)}\x00")
                protected.append(v)
                replaced_any = True
            cursor = i + len(v)
        if replaced_any:
            out = "".join(pieces)
    return out, protected


def _mask_protected(text: str) -> tuple[str, list[str]]:
    """把文件号抽出来，换成占位符。返回 (遮蔽后的文本, 被抽出的原件)。

    占位符用 `\\x00` 包夹（`\\x00` 在正常文本里不会出现，也不会被
    任何别名匹配到）——用可见字符（如「§」）有被别名表撞上的风险。
    前缀用 `_NS_FILE`，与规范写法那趟的占位符区分开（见 `_NS_FILE` 说明）。
    """
    protected: list[str] = []

    def _take(m: re.Match[str]) -> str:
        protected.append(m.group(0))
        return f"{_NS_FILE}{len(protected) - 1}\x00"

    masked = text
    for pat in _PROTECTED_PATTERNS:
        masked = pat.sub(_take, masked)
    return masked, protected


def _unmask(
    text: str,
    protected: Sequence[str],
    *,
    namespace: str = _NS_CANON,
) -> str:
    """把占位符换回原件。按序号倒序替换，避免 `\\x001\\x00` 与 `\\x0011\\x00` 混淆。

    `namespace` 必须与遮蔽时用的一致，否则会把另一趟的占位符
    误认成自己的（这就是 `_NS_FILE` 存在的理由）。
    """
    out = text
    for i in range(len(protected) - 1, -1, -1):
        out = out.replace(f"{namespace}{i}\x00", protected[i])
    return out


# ---------------------------------------------------------------------------
# 条目类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GlossaryEntry:
    """一条术语。

    `aliases` 用 tuple 而不是 list：条目要能放进 set / 当 dict key，
    并且**必须不可变**——术语库是长期资产，就地修改会绕过审计。
    改一条术语的正确做法是造一个新条目 + 写一条 `glossary_change`。

    字段与 `001_init.sql` 的 `glossary_entry` 表一一对应，
    多出的 `entry_id` 在表里是主键（`category + canonical` 有 UNIQUE 约束）。
    """

    entry_id: str
    category: str
    canonical: str
    aliases: tuple[str, ...] = ()
    note: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise GlossaryFormatError(
                f"未知类别 {self.category!r}，只允许 {sorted(CATEGORIES)}"
            )

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "category": self.category,
            "canonical": self.canonical,
            "aliases": list(self.aliases),
            "note": self.note,
            "enabled": self.enabled,
        }


def parse_aliases(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    """把别名写法的多种表示统一成 tuple。

    支持三种输入：
      · `"张建军|张建国同志"`  —— CSV 里的写法（`|` 分隔）
      · `["张建军", "张建国同志"]` —— 代码里直接给
      · `'["张建军","张建国同志"]'` —— 数据库里存的 JSON 数组

    以 `[` 或 `{` 开头的一律按 JSON 解析，**不是数组就报错**：不这么卡的话，
    `'{"a": 1}'` 会被 `|` 分支当成单个别名收下，垃圾静默入库。

    去重且**保序**（保序让"先写的优先"这个直觉成立，也让测试可断言）。
    空串与纯空白项被丢弃 —— 空白别名会匹配一切，是最危险的脏数据。

    ⚠️⚠️ **非字符串、非序列的输入必须显式拒绝**（T1-14 验收抓到）。
    原实现只有 `isinstance(raw, str)` 一个判断，然后就直接
    `for a in raw:`。于是：
      · `parse_aliases(12345)`  → `TypeError: 'int' object is not iterable`
        冒到路由层 → 被兜底处理器报成 **E9001（内部错误）**。
        但"用户把 aliases 填成数字"是个**可预期的输入问题**，该报 E1001；
        更糟的是它污染 E9001 这个信号（SAD §9.3 要求 E9 只用于
        真正的未预期故障）。
      · `parse_aliases({'a': 1})` → **静默返回 `('a',)`** ——
        遍历 dict 拿到的是**键**。这正是本函数文档里说要防的
        "垃圾静默入库"，只是从另一个门进来的。
    修法：先做一次**类型白名单**，白名单之外一律抛 `GlossaryFormatError`。
    用白名单而不是"黑名单排除 int/float/dict"—— 黑名单永远漏。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return ()
        if s.startswith("[") or s.startswith("{"):
            # 数据库存的是 JSON 数组。以 `{` 开头的一律走同一条分支，
            # 目的不是"试着解析看看"，而是**必须报错**——`{"a": 1}` 这种
            # JSON 对象若落到 `|` 分隔分支，会被当成单个别名原样收下，
            # 于是一条 `{"a": 1}` 的垃圾别名悄悄进了库，将来在正文里
            # 永远匹配不上，且没有任何报错。宁可现在炸，不要以后查不出。
            import json

            try:
                loaded = json.loads(s)
            except json.JSONDecodeError as e:
                raise GlossaryFormatError(f"aliases 不是合法 JSON 数组：{raw!r}") from e
            if not isinstance(loaded, list):
                raise GlossaryFormatError(f"aliases 的 JSON 必须是数组：{raw!r}")
            raw = [str(x) for x in loaded]
        else:
            raw = s.split("|")
    elif not isinstance(raw, (list, tuple, set, frozenset)):
        # ★ 白名单之外的一切（int / float / bool / dict / 任意对象）
        # 都到这里。报错而不是"尽力而为" —— 见上方类文档。
        raise GlossaryFormatError(
            f"aliases 类型不支持：{type(raw).__name__}（只接受字符串、"
            f"字符串序列，或 JSON 数组字符串）"
        )
    seen: dict[str, None] = {}
    for a in raw:
        a2 = str(a).strip()
        if a2:
            seen.setdefault(a2, None)
    return tuple(seen)


def validate_entry(entry: GlossaryEntry) -> list[str]:
    """校验一条术语，返回问题清单（空列表 = 通过）。

    为什么返回清单而不是抛错：批量导入时用户需要看到**全部**问题
    （"第 3 行和第 17 行都缺规范写法"），而不是修一个报一个。
    """
    problems: list[str] = []
    if not entry.canonical.strip():
        problems.append("规范写法（canonical）不能为空")
    if entry.canonical != entry.canonical.strip():
        problems.append(f"规范写法首尾有空白：{entry.canonical!r}")
    for a in entry.aliases:
        if a == entry.canonical:
            # 别名等于规范写法 = 无意义的自我替换，且会浪费热词名额
            problems.append(f"别名与规范写法相同（无意义替换）：{a!r}")
        if a.strip() != a:
            problems.append(f"别名首尾有空白：{a!r}")
    return problems


def iter_alias_pairs(entries: Iterable[GlossaryEntry]) -> Iterable[tuple[str, str, str]]:
    """展开为 `(alias, canonical, entry_id)`。

    停用条目（`enabled=False`）**不参与**（测试方案 U-GL-03）。
    这是术语库最重要的一个开关：某个人名暂时写错时，
    停用比删除好——删了就丢了"曾经有这个术语"的历史。
    """
    for e in entries:
        if not e.enabled:
            continue
        for a in e.aliases:
            # `a.strip()` 而不是 `a`：纯空白别名（"   "）和空串一样危险 ——
            # 它会匹配正文里所有空格，把句子切得稀碎。校验层
            # （`validate_entry`）虽然会报出来，但这一层必须自己兜住：
            # 索引可能被直接构造（不过校验），不能指望上游一定校验过。
            if a and a.strip():
                yield a, e.canonical, e.entry_id


def sort_aliases_longest_first(aliases: Iterable[str]) -> list[str]:
    """按长度倒序；同长按字典序（保证结果**确定**，测试才能断言）。

    为什么要确定性：同一份文本、同一个术语库，两次运行给出不同结果，
    会让"复核页的修改记录"无法复现。字典序兜底消除这个不确定性。
    """
    return sorted(set(aliases), key=lambda a: (-len(a), a))


def build_alias_index(
    entries: Iterable[GlossaryEntry],
    *,
    skip_when: "Sequence[str] | None" = None,
    on_conflict: str = "stable",
) -> ALIAS_INDEX_TYPE:
    """构建「易错写法 -> (规范写法, entry_id)」。

    Args:
        entries: 术语条目。
        skip_when: 这些**规范写法**不参与（用于"本次只想替换部分类别"）。
        on_conflict: 同一别名指向多个规范写法时怎么办。见下。

    冲突处理：为什么默认是 `"stable"` 而不是"先到先得"
    ------------------------------------------------
    两个条目把同一个别名指向不同规范写法，是**术语库维护错误**。
    最初实现是"先到的赢"，但这样结果是**不确定的**：
    `list_all()` 返回的顺序一变（换了个 ORDER BY、换了数据库、导入了新 CSV），
    同一个别名就会替换成不同的规范写法 —— 同一份音频、同一个术语库，
    两次跑出的稿子不一样。这会让复核页的修改记录无法复现，
    是比"选错一条"更严重的问题。

    所以默认取**确定性**策略：按 `(规范写法长度倒序, 规范写法字典序, entry_id)`
    选一个，与输入顺序无关。选"更长的规范写法"作为优先级是领域直觉 ——
    长写法通常更完整（「中共中央办公厅」优于「中办」）。

    另外提供两种模式供特殊场景：
      · `"first"`    —— 先到先得（保留手动控制顺序的能力，测试用）
      · `"longest"`  —— 只按规范写法长度，其他不管

    无论哪种模式，`detect_alias_conflicts()` 都能查出冲突，
    service 层会把它变成用户可见的告警 —— 选错一条不可怕，
    **悄悄选错**才可怕。
    """
    # 先按别名归组，再统一裁决 —— 这样结果与输入顺序彻底无关
    groups: dict[str, list[tuple[str, str]]] = {}
    for alias, canonical, eid in iter_alias_pairs(entries):
        if skip_when and canonical in skip_when:
            continue
        groups.setdefault(alias, []).append((canonical, eid))

    idx: ALIAS_INDEX_TYPE = {}
    for alias, cands in groups.items():
        if len(cands) == 1:
            idx[alias] = cands[0]
            continue
        if on_conflict == "first":
            idx[alias] = cands[0]
        elif on_conflict == "longest":
            idx[alias] = max(cands, key=lambda c: len(c[0]))
        else:  # "stable"（默认）
            idx[alias] = min(
                cands,
                key=lambda c: (-len(c[0]), c[0], c[1]),
            )

    # 把**规范写法自身**也登记成"恒等映射"（canonical -> canonical）。
    #
    # 为什么必须这么做：`apply_corrections` 要保护规范写法不被别的别名拆开
    # （施工卡「张三」/「张三丰」用例），但它只拿得到这一个 index。
    # 而一条**没有别名**的术语（张三丰）原本在 index 里毫无痕迹，
    # 于是「张三」把「张三丰」改成了「李四丰」—— 长度倒序救不了，
    # 因为被切碎的那个词压根没进索引。
    #
    # 登记成恒等映射后，`apply_corrections` 能看见全部规范写法。
    # 恒等条目在替换循环里会被显式跳过（不会产生"自己替换自己"的假命中）。
    for e in entries:
        if not e.enabled:
            continue
        c = e.canonical.strip()
        if not c:
            continue
        if skip_when and c in skip_when:
            continue
        # 只有当 c 不是别人的别名时才登记，避免覆盖真别名映射
        idx.setdefault(c, (c, e.entry_id))

    return idx


def detect_alias_conflicts(entries: Iterable[GlossaryEntry]) -> dict[str, list[str]]:
    """找出"同一别名指向多个规范写法"的维护错误。纯函数版（推荐）。

    返回 `{alias: [canonical_a, canonical_b, ...]}`，只含真冲突（>= 2 个目标）。
    """
    tmp: dict[str, list[str]] = {}
    for alias, canonical, _eid in iter_alias_pairs(entries):
        bucket = tmp.setdefault(alias, [])
        if canonical not in bucket:
            bucket.append(canonical)
    return {a: cs for a, cs in tmp.items() if len(cs) > 1}


# ---------------------------------------------------------------------------
# 替换
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrectionHit:
    """一次替换的明细。逐条返回，供审计与复核页展示。"""

    alias: str
    canonical: str
    entry_id: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "canonical": self.canonical,
            "entry_id": self.entry_id,
            "count": self.count,
        }


def apply_corrections(
    text: str,
    alias_index: Mapping[str, tuple[str, str]],
    *,
    protect_file_numbers: bool = True,
) -> tuple[str, int, list[CorrectionHit]]:
    """把文本里的易错写法改回规范写法。

    Returns:
        `(修正后文本, 命中总数, 逐条明细)`

    四步，顺序不能换
    ----------------
    1. **遮蔽文件号**（`protect_file_numbers=True` 时）——
       「中办发〔2026〕1 号」整段被换成占位符，别名匹配碰不到它。
    2. **遮蔽规范写法** —— 库里已经写对的词不能再被别的别名拆开。
       这一步是施工卡「张三」/「张三丰」用例的真正解法，见下。
    3. **按别名长度倒序替换** —— 长别名先替换。
       `alias_index` 本身是 dict（有插入序），所以这里显式排序，
       不依赖调用方的构建顺序。
    4. **还原**（先还原规范写法，再还原文件号；两者都是"按序号倒序"）。

    为什么只做"别名长度倒序"**不够**（第 2 步的存在理由）
    ----------------------------------------------------
    长度倒序解决的是**两个别名互吃**：
    「张三」和「张建国」都是别名时，长的先替，短的就不会把长的切碎。

    但施工卡给的用例不是这个形状 —— 它是
    「张三」是**别名**、「张三丰」是**规范写法**。
    「张三丰」根本不在别名索引里，没有任何"倒序"能保护它，
    于是「张三」照样把「张三丰」改成「李四丰」。

    唯一的解法是在替换前把**规范写法**也遮蔽起来：库里已经写对的字，
    就不该再被任何规则碰到（这也顺带保证了**幂等** ——
    同一段文本跑两遍结果一样，因为第二遍时第一遍的产物已被保护）。

    代价：规范写法本身是"错别字"时（不存在这种情况，规范写法就是标准答案）
    不会被改；以及多一层遮蔽-还原的开销（可忽略）。
    """
    if not text or not alias_index:
        return text, 0, []

    working = text
    file_numbers: list[str] = []
    if protect_file_numbers:
        working, file_numbers = _mask_protected(working)

    # 规范写法也要遮蔽。注意顺序：先文件号后规范写法——
    # 文件号里可能**包含**某个规范写法（如「中办发…」含「中办」），
    # 反过来的话遮蔽文件号时占位符已经把规范写法吃掉了，还原会错位。
    #
    # `avoid` 只放**真别名**（`alias != canonical`）。
    # 不能把整个 index 的 key 都塞进去：恒等条目（规范写法 -> 自己）
    # 会造成"自己覆盖自己"，于是 `_covered` 永远为真、遮蔽永远不生效 ——
    # 「张三丰」会被「张三」重新切碎，正是要修的那个 bug。
    # 别名出现的位置才该让给别名替换，恒等位置本来就该被保护。
    real_aliases = [a for a, (c, _) in alias_index.items() if a != c]
    canonicals = {canonical for canonical, _ in alias_index.values()}
    working, shielded = _mask_values(
        working, sorted(canonicals), avoid=real_aliases
    )

    hits: list[CorrectionHit] = []
    total = 0
    for alias in sort_aliases_longest_first(alias_index.keys()):
        canonical, entry_id = alias_index[alias]
        # 恒等映射（规范写法 -> 自己）只是用来让"规范写法清单"可见，
        # 不是真规则。跳过它们，否则每篇稿子都会多出一堆"改了 N 处"，
        # 而实际一个字都没动。
        if alias == canonical:
            continue
        if alias not in working:
            continue
        n = working.count(alias)
        if n == 0:
            continue
        working = working.replace(alias, canonical)
        total += n
        hits.append(
            CorrectionHit(alias=alias, canonical=canonical, entry_id=entry_id, count=n)
        )

    if shielded:
        working = _unmask(working, shielded, namespace=_NS_CANON)
    if file_numbers:
        working = _unmask(working, file_numbers, namespace=_NS_FILE)
    return working, total, hits


# ---------------------------------------------------------------------------
# 热词导出
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HotwordExport:
    """热词导出的结果。

    为什么返回对象而不是裸 list：施工卡要求"截断并**告警**"，
    告警必须有地方承载。裸 list 的接口逼着调用方自己算"截没截"，
    而那正是最容易漏的一步（漏了 = 静默失效）。
    """

    words: tuple[str, ...]
    total_before_limit: int
    limit: int
    truncated: bool
    warnings: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.words)

    @property
    def dropped(self) -> int:
        return max(0, self.total_before_limit - len(self.words))

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total": self.total_before_limit,
            "limit": self.limit,
            "truncated": self.truncated,
            "dropped": self.dropped,
            "warnings": list(self.warnings),
        }


def export_hotwords(
    entries: Iterable[GlossaryEntry],
    limit: int = DEFAULT_HOTWORD_LIMIT,
) -> HotwordExport:
    """导出给 ASR 的热词表。

    只取 `canonical`，**不含 aliases** —— 热词的作用是让引擎"往正确写法上靠"，
    把错误写法也喂进去会两头都激活，反而降低准确率（施工卡原文）。

    去重且保序：相同 canonical 只出现一次（`(category, canonical)` 在库里
    本来就有 UNIQUE 约束，但 CSV 导入时可能重复）。

    排序策略：**按类别优先级 + 原始顺序**，不按字典序。
    理由：截断时要保住"最可能被引擎读错的词"。领域知识上，
    人名/机构名（person/org）比通用术语（term）更依赖热词修正——
    通用术语（"党组会"）引擎本来就认得。所以 person/org 排前，
    这样一旦截断，先丢的是 term/codename。

    超限时**必须**产生告警（施工卡 + 测试方案 U-GL-04）。
    """
    if limit <= 0:
        raise GlossaryFormatError(f"热词上限必须为正数，当前 {limit}")

    seen: dict[str, None] = {}
    for e in entries:
        if not e.enabled:
            continue
        c = e.canonical.strip()
        if c:
            seen.setdefault(c, None)

    # 按"类别优先级"稳定排序：person/org 靠前，保留同类内的原始顺序
    priority = {"person": 0, "org": 1, "codename": 2, "term": 3}
    order: dict[str, int] = {}
    for e in entries:
        if not e.enabled:
            continue
        c = e.canonical.strip()
        if c and c not in order:
            order[c] = priority.get(e.category, 9)

    all_words = sorted(seen.keys(), key=lambda w: order.get(w, 9))
    total = len(all_words)
    truncated = total > limit
    words = tuple(all_words[:limit])

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"术语库共 {total} 个词，超过引擎热词上限 {limit} 个，"
            f"已截断为前 {limit} 个（本次丢弃 {total - limit} 个）。"
            "超限时引擎可能整体不生效，因此这里主动截断；"
            "被丢弃的词仍会在转写后由术语库回改纠正，但正确率可能下降。"
            "建议精简术语库，只保留最容易识别错的专名。"
        )
    return HotwordExport(
        words=words,
        total_before_limit=total,
        limit=limit,
        truncated=truncated,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# CSV 解析（纯函数部分；文件读写由仓储层负责）
# ---------------------------------------------------------------------------

#: CSV 表头。施工卡：`category,canonical,aliases(|分隔),note`。
#: `entry_id` 是可选的第 5 列——库里必须有，但手写 CSV 时让系统生成更省事。
CSV_HEADER: tuple[str, ...] = ("category", "canonical", "aliases", "note")
CSV_HEADER_OPTIONAL: tuple[str, ...] = ("entry_id", "enabled")


def parse_csv_rows(
    rows: Iterable[Mapping[str, str]],
    *,
    id_prefix: str = "gl",
) -> tuple[list[GlossaryEntry], list[str]]:
    """把已解析的 CSV 行（dict 序列）变成条目。返回 `(条目, 问题清单)`。

    之所以吃 `Mapping` 而不是原始文本：CSV 的**读取**（编码、BOM、引号）
    有一堆平台差异，交给标准库 `csv` 在仓储层做；这里只管**语义**。

    行号在问题里指不出来（`Mapping` 没有行号），所以调用方应该在
    传进来之前把行号拼进 `id_prefix`，或者干脆用返回的顺序对应行号。
    """
    entries: list[GlossaryEntry] = []
    problems: list[str] = []
    seen_keys: dict[tuple[str, str], int] = {}

    for i, row in enumerate(rows, start=1):
        category = (row.get("category") or "").strip()
        canonical = (row.get("canonical") or "").strip()
        if not category and not canonical:
            continue  # 空行
        if category.startswith("#") or canonical.startswith("#"):
            continue  # 注释行（CSV 没有官方注释语法，本项目用 `#` 开头）
        if not category:
            problems.append(f"第 {i} 行：缺 category")
            continue
        if category not in CATEGORIES:
            problems.append(
                f"第 {i} 行：未知 category {category!r}（只允许 {sorted(CATEGORIES)}）"
            )
            continue
        if not canonical:
            problems.append(f"第 {i} 行：缺 canonical")
            continue

        key = (category, canonical)
        if key in seen_keys:
            problems.append(
                f"第 {i} 行：与第 {seen_keys[key]} 行重复（category+canonical 必须唯一）"
            )
            continue
        seen_keys[key] = i

        try:
            aliases = parse_aliases(row.get("aliases"))
        except GlossaryFormatError as e:
            problems.append(f"第 {i} 行：{e}")
            continue

        eid = (row.get("entry_id") or "").strip() or f"{id_prefix}-{i:04d}"
        enabled_raw = (row.get("enabled") or "1").strip().lower()
        enabled = enabled_raw not in {"0", "false", "no", "否", "停用"}

        entry = GlossaryEntry(
            entry_id=eid,
            category=category,
            canonical=canonical,
            aliases=aliases,
            note=(row.get("note") or "").strip(),
            enabled=enabled,
        )
        for p in validate_entry(entry):
            problems.append(f"第 {i} 行：{p}")
        entries.append(entry)

    return entries, problems
