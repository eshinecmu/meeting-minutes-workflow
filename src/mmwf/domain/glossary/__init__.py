"""术语库领域模块（T1-07）。

本模块是**纯业务规则**：类型定义、别名匹配、文件号保护、热词导出。
零 IO、零框架依赖（`check_layers.py` 的 C01 会强制这一点）。

为什么"M12 glossary"被放在领域层
--------------------------------
SAD §4 的分层表把 M12 列为领域层，但同时写"不做识别调用"。
换句话说：术语库的**规则**（什么算别名、替换顺序、保护什么）是业务知识，
而"从 CSV 读"、"写数据库"、"写审计"是实现细节。规则放这里，
三处能力天然可复用：

  · T1-07 转写前 —— 导出热词给引擎（`export_hotwords`）
  · T1-08 复核页 —— 转写后回改错字（`apply_corrections`）
  · T1-11 接口   —— 术语库增删改查（仓储层调用本模块做校验）

术语库为什么必须存在
--------------------
ASR 对**专有名词**几乎必然出错：机构简称、人名同音字、项目代号。
热词能让引擎"往这些词上靠"，但引擎的热词能力有三个已知问题：
数量敏感（超限整体静默失效）、参数名随版本漂移、失败时不报错。
所以术语库不能只靠热词——还要有**转写后回改**这条兜底路径。
两条路径都在本模块里定义规则。

三类替换必须按"长优先"排序
--------------------------
无脑按字典序替换会互相破坏。典型：

    「中办」   -> 「中共中央办公厅」
    「中办发」 -> 「中共中央办公厅发」      ← 这不是文件号

若先替换短的「中办」，「中办发〔2026〕1 号」会被改成
「中共中央办公厅发〔2026〕1 号」——把文件号彻底毁掉。
按别名长度**倒序**替换能让「中办发」先命中，但这还不够：
真加了这个别名本身就是错的。所以还有第二道防线（见下）。

文件号保护：为什么单靠"长优先"不够
----------------------------------
施工卡原文："替换后需再过一遍白名单校验，或对文件号格式做保护。"
本模块选择**两者都做**：

  1. **长优先**（`build_alias_index` 返回按长度倒序的键序）；
  2. **占位符遮蔽**（`_mask_protected`）：替换前把所有「文件号」形状的
     片段抽出来换成不可被替换的占位符，替换完再放回。

只做 ① 的话，只要别名表里出现一个短词的**子串**，就仍有被吃掉的路径。
② 是结构性的：不管别名表怎么写，文件号都不会被碰。

遮蔽用占位符而不是"替换后回滚"，因为回滚需要定位"哪一段变了"，
而文件号本身可能就含有别名（"中办发"里的"中办"）——切不出来。
"""

from mmwf.domain.glossary.matcher import (
    ALIAS_INDEX_TYPE,
    CATEGORIES,
    CATEGORY_LABELS,
    CSV_HEADER,
    CSV_HEADER_OPTIONAL,
    DEFAULT_HOTWORD_LIMIT,
    CorrectionHit,
    GlossaryEntry,
    GlossaryFormatError,
    HotwordExport,
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

__all__ = [
    "ALIAS_INDEX_TYPE",
    "CATEGORIES",
    "CATEGORY_LABELS",
    "CSV_HEADER",
    "CSV_HEADER_OPTIONAL",
    "DEFAULT_HOTWORD_LIMIT",
    "CorrectionHit",
    "GlossaryEntry",
    "GlossaryFormatError",
    "HotwordExport",
    "apply_corrections",
    "build_alias_index",
    "detect_alias_conflicts",
    "export_hotwords",
    "iter_alias_pairs",
    "parse_aliases",
    "parse_csv_rows",
    "sort_aliases_longest_first",
    "validate_entry",
]
