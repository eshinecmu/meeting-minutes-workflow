"""T1-07 验收脚本（人可读版）。

施工卡 T1-07 的四条验收标准，逐条跑一遍并打印结论。
用法：python tools/accept_t1_07.py
      python tools/accept_t1_07.py --keep-tmp     # 保留临时目录便于排查

四条标准（施工卡原文）：
  [ ] config/glossary.csv 可被导入，格式 category,canonical,aliases(|分隔),note
  [ ] 别名替换按长度倒序：测试用例「张三」与「张三丰」同时存在时，
      文本「张三丰」不被错误替换成「张三丰丰」
  [ ] 热词数量 > 2000 时截断并告警（避免超出引擎上限反而全部失效）
  [ ] 命中数写入审计日志的 detail（可追溯术语库生效情况）

关于本卡"两条独立的防线"
------------------------
施工卡只要求"按长度倒序"，但纯长度倒序**单独不够**：

    别名表里有  中办  -> 中共中央办公厅
    正文是      中办发〔2026〕1 号

长度倒序解决的是"张三"吃掉"张三丰"的**前缀**问题；但文件号是另一种
伤害——「中办发」是「中办」的超集，倒序替换会把文件号拆成
「中共中央办公厅发〔2026〕1 号」，一个真文件号被改成不存在的名字。

所以实现上有**两道独立防线**（u-GL-02 与 U-GL-05）：
  ① 别名按长度倒序（施工卡明示的要求，本脚本标准 2 直接验）
  ② 替换前先把文件号用占位符遮蔽、替换完再还原（附加 4 验）
两道都对才敢上生产：防线①漏掉的场景防线②兜得住，反之亦然。
本脚本把两条都跑，并额外演示"关掉防线②会怎样"——让验收人亲眼
看到这条防线不是过度设计。
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.app.services.glossary import GlossaryService  # noqa: E402
from mmwf.domain.glossary import (  # noqa: E402
    CSV_HEADER,
    GlossaryEntry,
    apply_corrections,
    build_alias_index,
    export_hotwords,
    parse_csv_rows,
    sort_aliases_longest_first,
)
from mmwf.infra.audit import AuditLogger, verify_chain  # noqa: E402
from mmwf.infra.glossary_store import GlossaryRepository, import_csv, read_csv  # noqa: E402
from mmwf.infra.migrations.runner import migrate  # noqa: E402

PASS = "✓"
FAIL = "✗"
SKIP = "·"

#: 施工卡原文的热词上限
HOTWORD_LIMIT = 2000

#: 施工卡原文要求的 CSV 表头（前四列，顺序固定）
REQUIRED_HEADER = ("category", "canonical", "aliases", "note")


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


def _e(eid: str, cat: str, canonical: str, aliases: tuple[str, ...] = ()) -> GlossaryEntry:
    return GlossaryEntry(entry_id=eid, category=cat, canonical=canonical, aliases=aliases)


# ---------------------------------------------------------------------------
# 标准 1：config/glossary.csv 可被导入
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：仓库自带的 config/glossary.csv 能直接导入，格式合规。

    验的不只是"能跑通"，而是**格式契约**本身：
    表头前四列必须一字不差是 category,canonical,aliases,note，
    因为这是交付给使用单位的模板，他们照着填。
    """
    banner("标准 1  config/glossary.csv 可被导入，格式 category,canonical,aliases(|分隔),note")
    ok = True

    csv_path = PROJECT_ROOT / "config" / "glossary.csv"
    ok &= _assert(csv_path.is_file(), f"仓库自带样例存在：config/glossary.csv")

    # 表头契约（剥 BOM 后比）
    raw = csv_path.read_bytes()
    header_line = raw.decode("utf-8-sig").splitlines()[0]
    header = tuple(h.strip() for h in header_line.split(","))
    ok &= _assert(
        header[:4] == REQUIRED_HEADER,
        f"表头前四列为 {REQUIRED_HEADER}，实际 {header[:4]}",
    )

    # 带注释行也要能导入（样例文件里就有 `#` 说明行）
    entries, problems = read_csv(csv_path)
    ok &= _assert(not problems, f"解析无问题（实际 {len(problems)} 条：{problems[:3]}）")
    ok &= _assert(len(entries) >= 4, f"解析出 >=4 条条目（实际 {len(entries)}）")
    ok &= _assert(
        all(e.category in {"org", "person", "term", "codename"} for e in entries),
        "所有条目类别合法",
    )

    # 真走一遍入库（临时库，不动仓库数据）
    db = tmp / "data" / "mmwf.db"
    migrate(db)
    repo = GlossaryRepository(db)
    rep = import_csv(repo, csv_path)
    ok &= _assert(rep.ok, f"import_csv 成功（problems={list(rep.problems)[:3]}）")
    ok &= _assert(rep.upserted == len(entries), f"入库 {rep.upserted} 条")
    ok &= _assert(repo.count() == len(entries), f"库里确有 {repo.count()} 条")

    # 幂等：再导一次不该翻倍
    rep2 = import_csv(repo, csv_path)
    ok &= _assert(
        repo.count() == len(entries) and rep2.created == 0,
        f"重复导入幂等：仍 {repo.count()} 条，新增 0（实际 created={rep2.created}）",
    )

    # aliases 的 `|` 分隔写法确实被拆开了
    with_alias = [e for e in entries if e.aliases]
    ok &= _assert(
        bool(with_alias) and any(len(e.aliases) >= 2 for e in with_alias),
        "`|` 分隔的多个别名被正确拆分",
    )
    if with_alias:
        sample = with_alias[0]
        print(f"      样例：{sample.canonical} -> aliases={sample.aliases}")
    return ok


# ---------------------------------------------------------------------------
# 标准 2：别名替换按长度倒序（「张三」vs「张三丰」）
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：长别名先替换，短别名不得吃掉别人规范写法的前缀。

    施工卡给的用例是「张三」与「张三丰」。这里要留意用例的**真实形状**：

        「张三」  是**别名**（指向李四）
        「张三丰」是另一条术语的**规范写法**（没有别名）

    所以它不是"两个别名互吃"，而是"别名切碎了别人的规范写法"。
    「张三丰」压根不在别名索引里，光靠"别名按长度倒序"救不了它 ——
    没有任何东西在跟「张三」比长度。解法是把**规范写法也遮蔽起来**。
    本检查项同时验"倒序"和"遮蔽"两个机制，并把两种形状都跑一遍。
    """
    banner("标准 2  别名替换按长度倒序（「张三」/「张三丰」用例）")
    ok = True

    # 形状 A（施工卡原用例）：别名切碎别人的规范写法
    entries = [
        _e("gl-1", "person", "张三丰"),                       # 规范写法，无别名
        _e("gl-2", "person", "李四", ("张三",)),                # 别名，短
    ]
    index = build_alias_index(entries)

    text = "会议请张三丰同志介绍经验"
    out, hits, hit_list = apply_corrections(text, index)

    print(f"      原文：{text}")
    print(f"      结果：{out}")
    print(f"      命中：{hits} 处 {[h.alias for h in hit_list]}")

    ok &= _assert("张三丰" in out, "「张三丰」完整保留（未被拆开）")
    ok &= _assert("李四丰" not in out, "没有出现「李四丰」这种被短别名截断的产物")
    ok &= _assert(hits == 0, "这段文本本来就写对了，不该产生任何修改")
    ok &= _assert(
        "张三" not in out.replace("张三丰", ""),
        "（反向）文本里没有游离的「张三」需要改，所以不该有别的改动",
    )

    # 反过来也要对：真出现孤立「张三」时它得换上
    out2, hits2, _ = apply_corrections("张三同志请假", index)
    print(f"      「张三同志请假」-> {out2}")
    ok &= _assert("李四" in out2, "孤立的「张三」被替换成「李四」")
    ok &= _assert(hits2 == 1, f"命中 1 处（实际 {hits2}）")

    # 混合句：两种形状同句共存
    out3, hits3, _ = apply_corrections("张三丰和张三都来了", index)
    print(f"      「张三丰和张三都来了」-> {out3}")
    ok &= _assert(out3 == "张三丰和李四都来了", "混合句里长保护、短替换各就各位")

    # 幂等：跑两遍结果必须一样（遮蔽规范写法带来的免费好处）
    twice = apply_corrections(out3, index)[0]
    ok &= _assert(twice == out3, f"幂等：再跑一遍结果不变（{twice}）")

    # 形状 B：两个**别名**互吃（这才是"长度倒序"直接管的场景）
    alias_vs_alias = [
        _e("a1", "person", "王五", ("王五",)),          # 占位，让 a2 有对照
        _e("a2", "person", "赵六", ("赵",)),             # 短别名
        _e("a3", "person", "钱七", ("赵六",)),           # 长别名，"赵" 是它的前缀
    ]
    idx_b = build_alias_index(alias_vs_alias)
    out_b, _, _ = apply_corrections("赵六来了", idx_b)
    print(f"      别名互吃：「赵六来了」-> {out_b}")
    ok &= _assert(out_b == "钱七来了", "长别名「赵六」优先，没被「赵」切碎")

    # 三重前缀链：甲乙 / 甲乙丙 / 甲乙丙丁
    chain = [
        _e("c1", "term", "甲乙", ("甲乙戊",)),
        _e("c2", "term", "甲乙丙"),
        _e("c3", "term", "甲乙丙丁"),
    ]
    idx_c = build_alias_index(chain)
    for src in ("甲乙丙丁", "甲乙丙", "甲乙"):
        got = apply_corrections(src, idx_c)[0]
        ok &= _assert(got == src, f"三重前缀链：{src} 不被污染（{got}）")

    # 别名包含自己的规范写法时，替换必须照常发生（不能被保护机制挡住）
    own = build_alias_index([_e("o1", "org", "某单位", ("某单位简称",))])
    out_o, hits_o, _ = apply_corrections("某单位简称发来通知", own)
    print(f"      别名含自身规范写法：「某单位简称发来通知」-> {out_o}")
    ok &= _assert(out_o == "某单位发来通知", "「某单位简称」仍被正确回改")
    ok &= _assert(hits_o == 1, f"命中 1 处（实际 {hits_o}）")

    # 输出里绝不能残留占位符
    ok &= _assert("\x00" not in out3 and "\x00" not in out_o, "输出无占位符残留")

    # 索引里长别名确实排在前（直接看构建结果，不靠行为推断）
    order = sort_aliases_longest_first(index.keys())
    ok &= _assert(
        all(len(order[i]) >= len(order[i + 1]) for i in range(len(order) - 1)),
        "sort_aliases_longest_first 结果确实长度递减",
    )

    return ok


# ---------------------------------------------------------------------------
# 标准 3：热词 > 2000 时截断并告警
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    """标准 3：超限截断，且告警必须说明**后果**，不能只报"超限"。

    为什么告警内容也算验收项：施工卡写的是"避免超出引擎上限反而全部失效"。
    如果只打印一句"已截断"，用户会以为是性能优化、无所谓；
    必须点明"整体不生效"这个后果，用户才会真的去精简术语库。
    """
    banner("标准 3  热词数量 > 2000 时截断并告警（避免超限反而全部失效）")
    ok = True

    # 边界内：正好 2000，不该截断
    at_limit = [
        _e(f"p{i:04d}", "term", f"术语{i:04d}") for i in range(HOTWORD_LIMIT)
    ]
    ex_ok = export_hotwords(at_limit, limit=HOTWORD_LIMIT)
    ok &= _assert(
        not ex_ok.truncated and len(ex_ok.words) == HOTWORD_LIMIT,
        f"正好 {HOTWORD_LIMIT} 个：不截断不放告警（truncated={ex_ok.truncated}）",
    )
    ok &= _assert(ex_ok.warnings == (), "边界内无告警")

    # 超限：2500 个
    over = [
        _e(f"t{i:04d}", "term", f"术语{i:04d}") for i in range(HOTWORD_LIMIT + 500)
    ]
    ex = export_hotwords(over, limit=HOTWORD_LIMIT)
    print(f"      输入 {len(over)} 个 -> 输出 {len(ex.words)} 个，丢弃 {ex.dropped} 个")
    ok &= _assert(ex.truncated is True, "超限时 truncated=True")
    ok &= _assert(len(ex.words) == HOTWORD_LIMIT, f"截断到 {HOTWORD_LIMIT} 个")
    ok &= _assert(ex.dropped == 500, f"丢弃数正确（实际 {ex.dropped}）")
    ok &= _assert(len(ex.warnings) >= 1, "产生了告警（不是静默截断）")

    blob = " ".join(ex.warnings)
    print(f"      告警：{blob[:100]}…")
    ok &= _assert(
        "不生效" in blob,
        "告警说明了**后果**（整体不生效），而不只是说超限",
    )
    ok &= _assert(
        str(ex.dropped) in blob, "告警里写明了丢弃了多少个"
    )

    # 优先级：截断时先丢 term，保住 person/org
    mixed = (
        [_e(f"t{i:04d}", "term", f"术语{i:04d}") for i in range(HOTWORD_LIMIT + 100)]
        + [_e("pp", "person", "张建国"), _e("oo", "org", "中共中央办公厅")]
    )
    ex2 = export_hotwords(mixed, limit=HOTWORD_LIMIT)
    ok &= _assert(
        "张建国" in ex2.words and "中共中央办公厅" in ex2.words,
        "截断时人名/机构名被保住（优先丢通用术语）",
    )
    ok &= _assert(
        ex2.words.index("张建国") < 10, "人名排在热词表最前面"
    )

    # 停用条目不进热词
    dis = [_e("d1", "person", "停用人名", (), )]
    dis[0] = GlossaryEntry(
        entry_id="d1", category="person", canonical="停用人名", enabled=False
    )
    ex3 = export_hotwords(dis, limit=100)
    ok &= _assert(ex3.count == 0, "停用条目不进热词表")

    # limit 非法要炸，而不是悄悄当成"不限制"
    try:
        export_hotwords([], limit=0)
        ok &= _assert(False, "limit=0 应报错")
    except Exception:
        ok &= _assert(True, "limit<=0 报错（而不是当成不限量）")
    return ok


# ---------------------------------------------------------------------------
# 标准 4：命中数写入审计 detail
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：回改命中数进审计 detail，可追溯术语库生效情况。

    这里同时验合规红线：detail **不得含正文原文**（部署手册 §6.2）。
    只记计数与截断过的别名，不记整段话。
    """
    banner("标准 4  命中数写入审计日志的 detail（可追溯术语库生效情况）")
    ok = True

    db = tmp / "data" / "mmwf.db"
    migrate(db)
    repo = GlossaryRepository(db)
    audit = AuditLogger(db)
    svc = GlossaryService(repo, audit=audit, operator="zhangsan")

    for e in [
        _e("gl-1", "org", "中共中央办公厅", ("中办", "中央办公厅")),
        _e("gl-2", "term", "新型基础设施建设", ("新基建",)),
        _e("gl-3", "person", "张建国", ("建军",)),
    ]:
        svc.upsert(e)

    sentence = "中办印发了关于新基建的推进方案，建军同志牵头落实"
    result = svc.correct_text(
        sentence, target_type="meeting", target_id="M-2026-001"
    )
    print(f"      原文：{sentence}")
    print(f"      结果：{result.text}")
    print(f"      命中：{result.hits} 处")

    ok &= _assert(result.hits == 3, f"命中 3 处（实际 {result.hits}）")
    ok &= _assert("中共中央办公厅" in result.text, "机构别名已回改")
    ok &= _assert("新型基础设施建设" in result.text, "术语别名已回改")

    # 审计里查得到
    from mmwf.infra.audit import query as audit_query

    rows = [r for r in audit_query(db) if r.action == "glossary_change"]
    correct_rows = [r for r in rows if r.detail.get("op") == "correct"]
    ok &= _assert(bool(correct_rows), "审计里有一条 op=correct 的 glossary_change")

    if correct_rows:
        d = correct_rows[-1].detail
        print(f"      detail：{d}")
        ok &= _assert(d.get("hits") == 3, f"detail.hits == 3（实际 {d.get('hits')}）")
        ok &= _assert(d.get("entries") == 3, f"detail.entries == 3（实际 {d.get('entries')}）")
        ok &= _assert(
            bool(d.get("aliases")), "detail.aliases 记下了命中的别名"
        )
        ok &= _assert(
            d.get("chars_after", 0) > d.get("chars_before", 0),
            "detail 记了改写字数变化（规范写法更长）",
        )
        # 合规红线：不得含正文
        blob = str(d)
        ok &= _assert("牵头落实" not in blob, "detail 不含正文原文（合规红线）")
        ok &= _assert(
            correct_rows[-1].target_id == "M-2026-001",
            "审计能指回顾问对象（哪场会议）",
        )
        ok &= _assert(
            correct_rows[-1].operator == "zhangsan", "审计记了操作者"
        )

    # 没命中就不写审计（否则每段一条"改了 0 处"，把链刷爆）
    before = len(audit_query(db))
    r2 = svc.correct_text("这是一句完全正常、不含别名的话")
    after = len(audit_query(db))
    ok &= _assert(r2.hits == 0, "无别名的句子命中 0")
    ok &= _assert(after == before, "命中 0 时不写审计（不刷日志）")

    # 写操作也要留痕
    ops = {r.detail.get("op") for r in rows}
    print(f"      已见 op 集合：{sorted(x for x in ops if x)}")
    ok &= _assert(
        {"created", "correct"} <= ops,
        "新建/回改都留了痕",
    )

    # 链完整性
    v = verify_chain(db)
    ok &= _assert(v.ok, f"审计链完整（{v.summary()}）")
    return ok


# ---------------------------------------------------------------------------
# 附加 1：文件号保护（U-GL-05）——长度倒序之外的第二道防线
# ---------------------------------------------------------------------------

def check_file_number_guard(tmp: Path) -> bool:
    """附加 1：文件号不被拆开，且关掉保护后确实会坏（证明不是过度设计）。"""
    banner("附加 1  文件号保护（第二道防线：遮蔽 + 还原）")
    ok = True

    entries = [_e("gl-1", "org", "中共中央办公厅", ("中办",))]
    index = build_alias_index(entries)

    cases = [
        ("中办发〔2026〕1 号", "中办发〔2026〕1 号"),        # 六角括号
        ("国办发[2026]15号", "国办发[2026]15号"),           # 半角方括号
        ("京政办字（2026）3 号", "京政办字（2026）3 号"),    # 全角圆括号
    ]
    for src, expect in cases:
        out, _, _ = apply_corrections(src, index)
        ok &= _assert(out == expect, f"文件号原样保留：{src}")

    # 跟别的别名共处一句时，两边都要对
    mixed = "中办印发了中办发〔2026〕1 号"
    out, _, _ = apply_corrections(mixed, index)
    print(f"      {mixed}  ->  {out}")
    ok &= _assert("中办发〔2026〕1 号" in out, "同一句里文件号完好")
    ok &= _assert(out.startswith("中共中央办公厅印发了"), "同一句里机关名被正确回改")

    # 反证：关掉保护会真的坏
    broken, _, _ = apply_corrections(mixed, index, protect_file_numbers=False)
    print(f"      关掉保护：{broken}")
    ok &= _assert(
        broken != out and "中办发〔2026〕1 号" not in broken,
        "关掉保护后文件号确实被破坏（证明这道防线是必要的）",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 2：别名冲突要暴露，不能静默
# ---------------------------------------------------------------------------

def check_alias_conflicts(tmp: Path) -> bool:
    """附加 2：同一别名指向多个规范写法时报警，且结果与录入顺序无关。"""
    banner("附加 2  别名冲突检测 + 替换结果可复现")
    ok = True

    # 「建军」既是张建国的别名，又是张建军本人的别名——录入错误
    a = [
        _e("gl-1", "person", "张建国", ("建军",)),
        _e("gl-2", "person", "张建军", ("建军",)),
    ]
    db = tmp / "data" / "mmwf.db"
    migrate(db)
    repo = GlossaryRepository(db)
    svc = GlossaryService(repo, audit=None)
    for e in a:
        svc.upsert(e)

    conflicts = svc.conflicts()
    print(f"      冲突：{conflicts}")
    ok &= _assert("建军" in conflicts, "冲突别名被检出")
    ok &= _assert(
        set(conflicts.get("建军", [])) == {"张建国", "张建军"},
        "冲突记下了全部涉及的规范写法",
    )

    bundle = svc.prepare_hotwords()
    ok &= _assert(
        any("冲突" in w or "多个" in w for w in bundle.warnings),
        f"冲突进入了热词告警（warnings={bundle.warnings[:1]}）",
    )

    # 结果与录入顺序无关（这是可复现性的硬要求：同输入必同输出）
    idx_fwd = build_alias_index(a)
    idx_rev = build_alias_index(list(reversed(a)))
    ok &= _assert(idx_fwd["建军"] == idx_rev["建军"], "正序/逆序构建的索引一致")

    o1 = apply_corrections("建军同志", idx_fwd)[0]
    o2 = apply_corrections("建军同志", idx_rev)[0]
    ok &= _assert(o1 == o2, f"替换结果可复现（{o1}）")

    # 无冲突时 clean
    clean = [_e("c1", "person", "张建国", ("建国同志",))]
    ok &= _assert(
        GlossaryService(
            _fresh_repo(tmp / "clean.db"), audit=None
        ).conflicts() == {},
        "干净术语库无冲突报告",
    )
    return ok


def _fresh_repo(path: Path) -> GlossaryRepository:
    migrate(path)
    return GlossaryRepository(path)


# ---------------------------------------------------------------------------
# 附加 3：CSV 往返无损 + 导入不删库
# ---------------------------------------------------------------------------

def check_csv_roundtrip(tmp: Path) -> bool:
    """附加 3：导出再导入无损；导入是 upsert 不是"以文件为准覆盖"。"""
    banner("附加 3  CSV 往返无损 + 导入不删已有条目")
    ok = True

    from mmwf.infra.glossary_store import write_csv

    db = tmp / "data" / "mmwf.db"
    migrate(db)
    repo = GlossaryRepository(db)

    src = [
        _e("gl-1", "org", "中共中央办公厅", ("中办", "中央办公厅")),
        _e("gl-2", "person", "张建国", ("建国同志",)),
        _e("gl-3", "term", "复合,含逗号的术语", ("逗号术语",)),  # 故意带逗号
    ]
    for e in src:
        repo.upsert(e)

    out = write_csv(tmp / "export.csv", repo.list_all())
    back, problems = read_csv(out)
    ok &= _assert(not problems, f"导出再读无问题（{problems[:2]}）")
    ok &= _assert(len(back) == len(src), f"条目数一致（{len(back)}/{len(src)}）")

    for s in src:
        got = next((b for b in back if b.entry_id == s.entry_id), None)
        ok &= _assert(got is not None, f"{s.canonical} 往返存在")
        if got is not None:
            ok &= _assert(
                got.aliases == s.aliases and got.note == s.note,
                f"{s.canonical} 别名字段无损",
            )

    # 导入一份"只含 1 条"的补丁文件，不该删掉另外两条
    patch = tmp / "patch.csv"
    patch.write_text(
        "category,canonical,aliases,note\nterm,新增术语,新词,\n",
        encoding="utf-8",
    )
    rep = import_csv(repo, patch)
    ok &= _assert(rep.created == 1, f"补丁新增 1 条（实际 {rep.created}）")
    ok &= _assert(
        repo.count() == len(src) + 1,
        f"原有 {len(src)} 条未被删除（现共 {repo.count()} 条）",
    )
    ok &= _assert(repo.get("gl-1") is not None, "原条目仍在")

    # 表头契约（剥 BOM）
    first = out.read_bytes().decode("utf-8-sig").splitlines()[0]
    ok &= _assert(
        tuple(h.strip() for h in first.split(","))[:4] == REQUIRED_HEADER,
        "导出表头与施工卡格式一致",
    )
    ok &= _assert(
        tuple(CSV_HEADER)[:4] == REQUIRED_HEADER, "CSV_HEADER 常量与施工卡一致"
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 4：parse_csv_rows 的健壮性
# ---------------------------------------------------------------------------

def check_csv_robustness() -> bool:
    """附加 4：脏数据被逐行报出来，而不是崩掉或静默丢。"""
    banner("附加 4  CSV 脏数据逐行报告（不崩、不静默）")
    ok = True

    rows = [
        {"category": "org", "canonical": "中共中央办公厅", "aliases": "中办", "note": ""},
        {"category": "BAD", "canonical": "类别错", "aliases": "", "note": ""},
        {"category": "org", "canonical": "", "aliases": "", "note": ""},
        {"category": "org", "canonical": "中共中央办公厅", "aliases": "", "note": ""},
        {"category": "term", "canonical": "某术语", "aliases": "某术语", "note": ""},
    ]
    entries, problems = parse_csv_rows(rows)
    print(f"      接收 {len(entries)} 条，报告 {len(problems)} 个问题")
    for p in problems:
        print(f"        · {p}")

    # 最终行为（实测）：
    #   第 1 行 通过
    #   第 2 行 拒（类别非法）
    #   第 3 行 拒（缺规范写法）
    #   第 4 行 拒（与第 1 行重复）
    #   第 5 行 **通过但告警**（别名与规范写法相同 —— 这条派生不出任何
    #           有效替换规则，属于"没用的条目"而不是"坏数据"，所以留着
    #           但报出来让用户自己清。这是刻意的：直接拒掉会让用户
    #           在"我明明只是多写了一个同名字段"时莫名其妙丢条目）
    ok &= _assert(len(entries) == 2, f"2 条入册（实际 {len(entries)}）")
    ok &= _assert(len(problems) == 4, f"4 个问题被逐条报出（实际 {len(problems)}）")
    ok &= _assert(
        any("无意义替换" in p for p in problems),
        "「别名与规范写法相同」被标为问题（但不丢条目）",
    )
    ok &= _assert(
        all("行" in p for p in problems), "每个问题都指出行号（用户能定位）"
    )

    # 空行与注释行不算问题
    e2, p2 = parse_csv_rows([
        {"category": "", "canonical": "", "aliases": "", "note": ""},
        {"category": "# 注释", "canonical": "", "aliases": "", "note": ""},
        {"category": "org", "canonical": "中共中央办公厅", "aliases": "", "note": ""},
    ])
    ok &= _assert(len(e2) == 1 and not p2, f"空行/注释行被跳过且不报问题（{p2}）")

    # JSON 对象别名必须报错，不能当成单个别名收下
    e3, p3 = parse_csv_rows([
        {"category": "org", "canonical": "某机构", "aliases": '{"a": 1}', "note": ""},
    ])
    ok &= _assert(bool(p3), f"JSON 对象别名被拒（problems={p3[:1]}）")
    return ok


# ---------------------------------------------------------------------------
# 附加 5：分层与依赖闸口 + 全量用例
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8"
    )


def check_gates() -> bool:
    banner("附加 5  分层闸口 / 依赖闸口 / T1-07 全量用例")
    ok = True
    for label, cmd in [
        ("分层闸口 check_layers.py", [sys.executable, "tools/check_layers.py"]),
        ("依赖闸口 check_deps.py", [sys.executable, "tools/check_deps.py"]),
        (
            "T1-07 用例",
            [
                sys.executable, "-m", "pytest",
                "tests/unit/test_glossary_matcher.py",
                "tests/unit/test_glossary_store.py",
                "-q", "--no-header",
            ],
        ),
    ]:
        proc = _run(cmd)
        lines = [ln for ln in (proc.stdout + proc.stderr).strip().splitlines() if ln.strip()]
        print(f"  {PASS if proc.returncode == 0 else FAIL} {label}")
        print(f"      {lines[-1] if lines else '(无输出)'}")
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr)[-2000:])
        ok &= proc.returncode == 0
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T1-07 验收")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    print("T1-07 验收：术语库与热词注入（glossary）")
    print(f"项目根：{PROJECT_ROOT}")

    # 附加 2 故意用 `audit=None`（只验冲突检测，不关心留痕），
    # 这会让 service 按设计打一条"未写审计"的 warning。它是对的，
    # 但对验收输出是噪音——压到 ERROR 以上，免得盖住真正的结论。
    logging.disable(logging.WARNING)

    tmp_root = Path(tempfile.mkdtemp(prefix="mmwf_t107_"))
    try:
        results: dict[str, bool] = {
            "标准 1 config/glossary.csv 可导入": check_standard_1(tmp_root / "s1"),
            "标准 2 别名替换按长度倒序": check_standard_2(tmp_root / "s2"),
            "标准 3 热词 >2000 截断并告警": check_standard_3(tmp_root / "s3"),
            "标准 4 命中数写入审计 detail": check_standard_4(tmp_root / "s4"),
            "附加 1 文件号保护（第二道防线）": check_file_number_guard(tmp_root / "a1"),
            "附加 2 别名冲突检测 + 可复现": check_alias_conflicts(tmp_root / "a2"),
            "附加 3 CSV 往返无损 + 导入不删库": check_csv_roundtrip(tmp_root / "a3"),
            "附加 4 CSV 脏数据逐行报告": check_csv_robustness(),
            "附加 5 闸口与全量用例": check_gates(),
        }

        banner("验收结论")
        for name, ok in results.items():
            print(f"  {PASS if ok else FAIL} {name}")

        all_ok = all(results.values())
        print(f"\n{'=' * 78}")
        print(f"{PASS} T1-07 验收通过" if all_ok else f"{FAIL} T1-07 验收未通过")
        print(f"{'=' * 78}")
        return 0 if all_ok else 1
    finally:
        if args.keep_tmp:
            print(f"\n临时目录已保留：{tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
