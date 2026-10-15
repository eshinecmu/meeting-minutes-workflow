"""T1-09 验收脚本（人可读版）。

施工卡 T1-09 的验收标准逐条跑一遍。这张卡的验收是**全项目最重要的一组**，
因为它验的是"合规"——一份版式不合规的公文发出去，后果不是 bug 而是事故。

四条标准（施工卡原文）：
  [ ] 用固定输入生成 docx，与 tests/fixtures/expected_docx/ 下的基准逐项比对：
      标题=方正小标宋简体/22pt/居中；一级标题=黑体/16pt；
      正文=仿宋_GB2312/16pt/固定行距28.8pt/首行缩进2字符；
      **表格内段落 w:ind 四项全为 0**（解压 docx 读 XML 校验）；
      表格全边框存在；页码为 PAGE 域
  [ ] 中英文混排 `会议讨论了 API 接口，使用 JSON 格式`（中英间有空格）
  [ ] 英文引号 `"test"` -> 中文引号
  [ ] 缺失 `仿宋_GB2312` 的环境下，触发回退并返回 E5002 警告，**不崩溃**

常见坑（施工卡原文，也一并验证）：
  坑1  只设 run.font.name 中文不生效 —— 必须同时设 w:rFonts/@w:eastAsia
  坑2  w:ind 的 firstLine 与 firstLineChars 同时存在有兼容问题 ——
       统一用 Chars 并显式清零 firstLine
  坑3  表格缩进穿透（C15）—— 除清 w:ind 外还要给表格内段落用不继承的设定
  坑4  公文字体是商业字体不能随包分发 —— 只引用字体名，缺失时回退且必须提示

附加验证（本卡特有的、容易被漏掉的）：
  幂等导出（C07/IT-09）、审计不含正文（红线）、export/ 不加密（交付物前提）、
  撤销导出（F5.8）、分层与依赖闸口。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from mmwf.app.services.export import (  # noqa: E402
    EXPORT_AREA,
    ExportBlockedError,
    ExportService,
    judge_docx,
)
from mmwf.domain.docx import (  # noqa: E402
    GB_STYLES,
    RenderOptions,
    normalize_text,
    render_markdown_to_docx,
)
from mmwf.domain.docx import patches as P  # noqa: E402
from mmwf.infra.audit import ACTIONS, AuditLogger, query, verify_chain  # noqa: E402
from mmwf.infra.db import connect_ctx  # noqa: E402
from mmwf.infra.filestore import ENCRYPTED_AREAS, make_filestore  # noqa: E402
from mmwf.infra.migrations.runner import migrate  # noqa: E402

PASS = "✓"
FAIL = "✗"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MEETING_ID = "acc-t109"
OPERATOR = "acc_tester"

#: 施工卡验收标准里逐个点名的版式要求。
EXPECTED = {
    "title": ("方正小标宋简体", "44", "center"),     # 22pt = 44 半磅
    "h1": ("黑体", "32", None),                     # 16pt
    "body": ("仿宋_GB2312", "32", None),
    "table_head": ("黑体", "24", None),              # 12pt
}

FONTS_OK = {"方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312",
            "宋体", "Times New Roman"}

#: 固定基准输入。**所有断言都基于这一份**，这样"逐项比对"才有意义。
BASELINE_MD = """# 关于信息化建设工作的会议纪要

会议于 2026 年 9 月 27 日在三楼第一会议室召开，讨论了API接口与JSON格式问题。

## 会议基本情况

主持人张三丰，参会人员李四、王五。

### 时间地点

会议时间为 9 月 27 日 9 时，地点为三楼第一会议室。

| 姓名 | 职务 | 备注 |
| --- | --- | --- |
| 张三丰 | 处长 | 主持 |
| 李四 | 副处长 |  |
| 王五 | 科长 | 记录 |

## 议定事项

- 各单位要高度重视
- [ ] 下周报送方案

会议认为，要抓好落实。
"""


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

def _make_env(tmp: Path, name: str = "e2e"):
    data_root = tmp / name / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    db = data_root / "mmwf.db"
    migrate(db)
    store = make_filestore(MEETING_ID, data_root, encrypt_enabled=False)
    return data_root, db, store


def _insert_meeting(db: Path, *, status: str = "record_ready") -> None:
    row = {
        "meeting_id": MEETING_ID,
        "title": "信息化建设会议纪要",
        "meeting_time": "2026-09-27T09:00:00Z",
        "location": "三楼第一会议室",
        "host": "张三丰",
        "attendees": json.dumps(["李四", "王五"], ensure_ascii=False),
        "absentees": json.dumps([], ensure_ascii=False),
        "recorder": "孙八",
        "classification": "internal",
        "status": status,
        "created_by": OPERATOR,
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
    }
    cols = ", ".join(row)
    with connect_ctx(db) as conn:
        conn.execute(
            f"INSERT INTO meeting ({cols}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )


def _render_baseline(**kw) -> bytes:
    kw.setdefault("installed_fonts", FONTS_OK)
    result = render_markdown_to_docx(BASELINE_MD, options=RenderOptions(**kw))
    buf = io.BytesIO()
    result.doc.save(buf)
    return buf.getvalue()


def _xml_of(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml").decode("utf-8")


def _root_of(data: bytes):
    return etree.fromstring(_xml_of(data).encode("utf-8"))


def _text_runs(root):
    """所有带文字的 run。"""
    for r in root.iter(W + "r"):
        text = "".join(t.text or "" for t in r.iter(W + "t"))
        if text.strip():
            yield r, text


def _fonts_of(run) -> dict:
    rpr = run.find(W + "rPr")
    rfonts = rpr.find(W + "rFonts") if rpr is not None else None
    return {k.split("}")[1]: v for k, v in rfonts.attrib.items()} if rfonts is not None else {}


def _size_of(run) -> str | None:
    sz = run.find(f"{W}rPr/{W}sz")
    return sz.get(W + "val") if sz is not None else None


def _para_of_run(run):
    el = run
    while el is not None and el.tag != W + "p":
        el = el.getparent()
    return el


# ---------------------------------------------------------------------------
# 标准 1：基准 docx 逐项比对（六条）
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：固定输入 -> docx，与基准逐项比对六条版式要求。"""
    banner("标准 1  固定输入生成 docx，逐项比对各版式要求（本卡最重要）")
    ok = True

    data = _render_baseline()
    root = _root_of(data)

    # --- ① 标题 = 方正小标宋简体，22pt，居中 ---
    title_runs = [(r, t) for r, t in _text_runs(root) if "会议纪要" in t]
    ok &= _assert(bool(title_runs), "找到标题文字")
    if title_runs:
        tr, tt = title_runs[0]
        f = _fonts_of(tr)
        font, size, align = EXPECTED["title"]
        ok &= _assert(f.get("eastAsia") == font, f"标题字体 = {font}（实际 {f.get('eastAsia')}）")
        ok &= _assert(_size_of(tr) == size, f"标题字号 = 22pt/{size}（实际 {_size_of(tr)}）")
        tp = _para_of_run(tr)
        jc = tp.find(f"{W}pPr/{W}jc") if tp is not None else None
        ok &= _assert(
            jc is not None and jc.get(W + "val") == align,
            f"标题对齐 = {align}（实际 {jc.get(W + 'val') if jc is not None else None}）",
        )
        b = tr.find(f"{W}rPr/{W}b")
        ok &= _assert(
            b is None or b.get(W + "val") in (None, "0", "false"),
            "标题**不加粗**（GB/T 9704：标题靠字号与字体区隔，不加粗）",
        )

    # --- ② 一级标题 = 黑体，16pt ---
    h1_runs = [(r, t) for r, t in _text_runs(root) if t.startswith("一、")]
    ok &= _assert(bool(h1_runs), "找到一级标题（以「一、」开头）")
    if h1_runs:
        r, t = h1_runs[0]
        f = _fonts_of(r)
        font, size, _ = EXPECTED["h1"]
        ok &= _assert(f.get("eastAsia") == font, f"一级标题字体 = {font}（实际 {f.get('eastAsia')}）")
        ok &= _assert(_size_of(r) == size, f"一级标题字号 = 16pt/{size}（实际 {_size_of(r)}）")

    # --- ③ 正文 = 仿宋_GB2312，16pt，固定行距 28.8pt，首行缩进 2 字符 ---
    body_runs = [(r, t) for r, t in _text_runs(root) if "三楼第一会议室召开" in t]
    ok &= _assert(bool(body_runs), "找到正文段")
    if body_runs:
        r, _t = body_runs[0]
        f = _fonts_of(r)
        font, size, _ = EXPECTED["body"]
        ok &= _assert(f.get("eastAsia") == font, f"正文字体 = {font}（实际 {f.get('eastAsia')}）")
        ok &= _assert(_size_of(r) == size, f"正文字号 = 16pt/{size}（实际 {_size_of(r)}）")
        bp = _para_of_run(r)
        sp = bp.find(f"{W}pPr/{W}spacing") if bp is not None else None
        ok &= _assert(
            sp is not None and sp.get(W + "lineRule") == "exact"
            and sp.get(W + "line") == "576",
            f"正文固定行距 = 28.8pt（exact/576），实际 "
            f"{sp.get(W + 'lineRule') if sp is not None else None}/"
            f"{sp.get(W + 'line') if sp is not None else None}",
        )
        ind = bp.find(f"{W}pPr/{W}ind") if bp is not None else None
        ok &= _assert(
            ind is not None and ind.get(W + "firstLineChars") == "200",
            f"正文首行缩进 2 字符（firstLineChars=200），实际 "
            f"{ind.get(W + 'firstLineChars') if ind is not None else None}",
        )

    # --- ④ ★ 表格内段落 w:ind 四项全为 0（C15，解压读 XML） ---
    tbl = next(root.iter(W + "tbl"), None)
    ok &= _assert(tbl is not None, "文档里有表格")
    if tbl is not None:
        bad = []
        n_ind = 0
        for ind in tbl.iter(W + "ind"):
            n_ind += 1
            for attr in ("firstLineChars", "firstLine", "leftChars", "left"):
                v = ind.get(W + attr)
                if v and v != "0":
                    bad.append(f"{attr}={v}")
        ok &= _assert(n_ind > 0, f"表格内有 w:ind 元素（{n_ind} 个，显式零缩进）")
        ok &= _assert(bad == [], f"★ 表格内缩进四项全为 0（C15），实际异常 {bad[:4]}")
        # 正文缩进不能被一起清掉（防止"全清"式的假实现）
        body_ind = body_runs[0][0]
        bp = _para_of_run(body_ind)
        bind = bp.find(f"{W}pPr/{W}ind") if bp is not None else None
        ok &= _assert(
            bind is not None and bind.get(W + "firstLineChars") == "200",
            "★ 对照：正文缩进仍是 200（没有为了修表格而把全文清零）",
        )

    # --- ⑤ 表格全边框存在 ---
    if tbl is not None:
        borders = tbl.find(f"{W}tblPr/{W}tblBorders")
        ok &= _assert(borders is not None, "表格有 w:tblBorders")
        if borders is not None:
            present = {e.tag.split("}")[1] for e in borders}
            ok &= _assert(
                present == set(P.TABLE_EDGES),
                f"表格六边齐全（实际 {sorted(present)}）",
            )

    # --- ⑥ 页码为 PAGE 域 ---
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        footers = [n for n in z.namelist() if n.startswith("word/footer")]
        merged = b"".join(z.read(n) for n in footers)
    ok &= _assert(bool(footers), "文档有页脚部件")
    ok &= _assert(b"PAGE" in merged, "页脚含 PAGE 域")
    ok &= _assert(b"fldChar" in merged, "PAGE 是**域**（三段式）而不是纯文本")
    if b"fldChar" in merged:
        froot = etree.fromstring(merged)
        types = [f.get(W + "fldCharType") for f in froot.iter(W + "fldChar")]
        ok &= _assert("begin" in types and "end" in types, "域有 begin/end 两端")

    ok &= _assert(len(list(_text_runs(root))) > 0, "基准输入含文字 run")
    print(f"      基准输入共 {len(list(_text_runs(root)))} 个文字 run")

    # 顺便跑一遍施工卡要求"必须写"的对照校验脚本
    script = PROJECT_ROOT / "tools" / "verify_docx_style.py"
    ok &= _assert(script.exists(), "tools/verify_docx_style.py 存在（施工卡要求必须写）")
    if script.exists():
        out = tmp / "s1" / "baseline.docx"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        proc = subprocess.run(
            [sys.executable, str(script), str(out)],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        ok &= _assert(proc.returncode == 0, "对照校验脚本对基准文件判定合格")
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
    return ok


# ---------------------------------------------------------------------------
# 标准 2：中英空格
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：中英文混排加空格。"""
    banner("标准 2  中英文混排 `会议讨论了API接口，使用JSON格式` -> 中英间有空格")
    ok = True

    src = "会议讨论了API接口，使用JSON格式"
    expect = "会议讨论了 API 接口，使用 JSON 格式"
    got = normalize_text(src)
    print(f"      输入：{src}")
    print(f"      输出：{got}")
    ok &= _assert(got == expect, f"规范化结果 == 施工卡期望（{expect}）")

    # 端到端：docx 里真的是加过空格的文本
    data = _render_baseline()
    xml = _xml_of(data)
    ok &= _assert("讨论了 API 接口" in xml, "docx 正文里中英之间确实有空格")
    ok &= _assert("与 JSON 格式问题" in xml, "多组中英混排都处理了")

    # 中文标点与西文之间不加空格（反向断言，防过度处理）
    ok &= _assert(normalize_text("（API）") == "（API）", "中文标点与西文之间不加空格")
    return ok


# ---------------------------------------------------------------------------
# 标准 3：引号转换
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    """标准 3：英文引号 -> 中文引号。"""
    banner("标准 3  英文引号 `\"test\"` -> 中文引号")
    ok = True

    got = normalize_text('"test"')
    expect = "\u201ctest\u201d"
    print(f"      '\"test\"' -> {got!r}")
    ok &= _assert(got == expect, f'输出为中文弯引号（{expect!r}）')
    ok &= _assert('"' not in got, "输出里不再有半角直引号")

    # 复杂场景
    ok &= _assert(
        normalize_text('他说"这个方案"很好') == "他说\u201c这个方案\u201d很好",
        "中文句子里的引号正确转换",
    )
    ok &= _assert(
        normalize_text("meeting's agenda") == "meeting's agenda",
        "英文撇号不被误转（`it's` 类）",
    )
    ok &= _assert(
        normalize_text("「直角引号」") == "「直角引号」",
        "已有的直角引号原样保留",
    )

    # 端到端
    md = '# 关于"信息化"建设的会议纪要\n'
    result = render_markdown_to_docx(
        md, options=RenderOptions(installed_fonts=FONTS_OK)
    )
    buf = io.BytesIO()
    result.doc.save(buf)
    xml = _xml_of(buf.getvalue())
    ok &= _assert("\u201c信息化\u201d" in xml, "docx 里引号已转成中文形态")
    return ok


# ---------------------------------------------------------------------------
# 标准 4：字体回退 + E5002，不崩溃
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：缺 `仿宋_GB2312` 时回退并返回 E5002 警告，**不崩溃**。"""
    banner("标准 4  缺失「仿宋_GB2312」时触发回退 + E5002 警告，不崩溃")
    ok = True

    # 模拟一台"装了宋体、黑体，但没装公文字体"的机器
    partial = {"宋体", "黑体", "Times New Roman"}

    # ① 渲染层不崩，且随结果返回警告
    result = render_markdown_to_docx(
        BASELINE_MD, options=RenderOptions(installed_fonts=partial)
    )
    warns = [w for w in result.warnings if "font missing" in w]
    print(f"      警告 {len(warns)} 条：")
    for w in warns:
        print(f"        ! {w}")
    ok &= _assert(len(warns) >= 1, "返回了 font missing 警告（E5002 的载荷）")
    ok &= _assert(
        any("仿宋_GB2312" in w for w in warns),
        "警告里点名了缺失的「仿宋_GB2312」",
    )
    ok &= _assert(
        any("宋体" in w for w in warns),
        "警告里说明回退到「宋体」（使用者要知道实际用了什么）",
    )

    # ② 产出的 docx 里真的用了回退字体
    buf = io.BytesIO()
    result.doc.save(buf)
    xml = _xml_of(buf.getvalue())
    ok &= _assert('w:eastAsia="宋体"' in xml, "★ 回退体现在产出文件里（不只是警告）")
    ok &= _assert('w:eastAsia="黑体"' in xml, "已装的字体照常使用，不被无谓回退")

    # ③ 服务层：完整导出流程不崩，且把警告带到结果里
    _, db, store = _make_env(tmp, "s4")
    _insert_meeting(db)
    store.write_text("record/v1.md", BASELINE_MD, stage="record")
    svc = ExportService(store, db_path=db, audit=AuditLogger(db), operator=OPERATOR)
    r = svc.export_docx(installed_fonts=partial)      # 不抛异常 = 不崩溃
    ok &= _assert(bool(r.rel_path), "导出**成功**（不因缺字体失败）")
    ok &= _assert(bool(r.font_warnings), "结果里带字体警告（上层可提示使用者）")
    ok &= _assert(store.exists(r.rel_path), "docx 文件确实落盘")

    # ④ 连回退链末端也没有时不崩（极端环境）
    result2 = render_markdown_to_docx(
        BASELINE_MD, options=RenderOptions(installed_fonts={"某个无关字体"})
    )
    joined = " ".join(result2.warnings)
    ok &= _assert("未能回退" in joined, "连替代字体都没有时，警告说明情况（仍不崩）")

    # ⑤ 预检不可用（非 Windows）时放行，不误报
    result3 = render_markdown_to_docx(
        BASELINE_MD, options=RenderOptions(installed_fonts=set())
    )
    ok &= _assert(
        not [w for w in result3.warnings if "font missing" in w],
        "预检不可用时不误报缺失（容器/CI 里不该因此失败）",
    )
    return ok


# ---------------------------------------------------------------------------
# 坑1：只设 run.font.name 中文不生效
# ---------------------------------------------------------------------------

def check_pit_1(tmp: Path) -> bool:
    """坑 1：`w:rFonts` 的 `w:eastAsia` 必须被设置。"""
    banner("坑 1  只设 run.font.name 中文不生效 —— 必须同时设 w:eastAsia")
    ok = True

    from mmwf.domain.docx._docx_backend import new_document

    # 演示错误做法：只设 run.font.name
    doc = new_document()
    para = doc.add_paragraph()
    run = para.add_run("中文字符")
    run.font.name = "仿宋_GB2312"          # ← 只做这一步
    buf = io.BytesIO()
    doc.save(buf)
    bad_xml = _xml_of(buf.getvalue())
    bad_east = 'w:eastAsia="仿宋_GB2312"' in bad_xml
    print(f"      只设 run.font.name -> eastAsia 已设？{bad_east}")
    ok &= _assert(
        not bad_east,
        "确认「只设 run.font.name」时 w:eastAsia **确实没被写**（这就是坑 1）",
    )

    # 正确做法
    doc2 = new_document()
    para2 = doc2.add_paragraph()
    run2 = para2.add_run("中文字符")
    P.set_cjk_font(run2, "仿宋_GB2312", 16)
    buf2 = io.BytesIO()
    doc2.save(buf2)
    good_xml = _xml_of(buf2.getvalue())
    ok &= _assert(
        'w:eastAsia="仿宋_GB2312"' in good_xml,
        "set_cjk_font 之后 w:eastAsia 被正确写入",
    )

    # 四个属性都要有
    all_four = all(f'w:{a}=' in good_xml for a in ("ascii", "hAnsi", "eastAsia", "cs"))
    ok &= _assert(all_four, "四个字体属性（ascii/hAnsi/eastAsia/cs）都写了")

    # 真实基准文件里每个文字 run 都有 eastAsia
    root = _root_of(_render_baseline())
    missing = [t[:12] for r, t in _text_runs(root) if "eastAsia" not in _fonts_of(r)]
    ok &= _assert(missing == [], f"基准文件里全部 run 都有 eastAsia（异常 {missing[:3]}）")
    return ok


# ---------------------------------------------------------------------------
# 坑2：firstLine 与 firstLineChars 并存
# ---------------------------------------------------------------------------

def check_pit_2(tmp: Path) -> bool:
    """坑 2：统一用 Chars 并显式清零 firstLine。"""
    banner("坑 2  w:ind 的 firstLine 与 firstLineChars 并存有兼容问题")
    ok = True

    root = _root_of(_render_baseline())
    conflicts = 0
    total = 0
    for ind in root.iter(W + "ind"):
        chars = ind.get(W + "firstLineChars")
        if chars is not None:
            total += 1
            fl = ind.get(W + "firstLine")
            if fl is None or fl != "0":
                conflicts += 1
    print(f"      带 firstLineChars 的 w:ind {total} 个")
    ok &= _assert(total > 0, "文档里确实用了 firstLineChars")
    ok &= _assert(
        conflicts == 0,
        f"★ 全部显式清零 firstLine（冲突 {conflicts} 处）",
    )

    # 表格单元格的缩进八项全写
    tbl = next(root.iter(W + "tbl"), None)
    if tbl is not None:
        incomplete = 0
        for ind in tbl.iter(W + "ind"):
            if len(ind.attrib) < len(P.INDENT_ATTRS):
                incomplete += 1
        ok &= _assert(
            incomplete == 0,
            f"表格内 w:ind 属性齐全（8 项），不齐 {incomplete} 处",
        )
    return ok


# ---------------------------------------------------------------------------
# 坑3：表格缩进穿透（C15）
# ---------------------------------------------------------------------------

def check_pit_3(tmp: Path) -> bool:
    """坑 3：表格缩进穿透 —— 清 w:ind + 关自适应 + 显式对齐。"""
    banner("坑 3  表格缩进穿透（C15）—— 根因是继承了正文段落样式")
    ok = True

    from mmwf.domain.docx._docx_backend import new_document

    # 演示"只清 firstLine 而留着 left"的漏改写法
    doc = new_document()
    table = doc.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    P.set_first_line_indent_chars(cell.paragraphs[0], 2)
    # 错误修复：只把 firstLineChars 设回 0，没有动 left
    ind = cell.paragraphs[0]._p.find(f"{W}pPr/{W}ind")
    ind.set(W + "leftChars", "200")              # 模拟残留的左侧缩进
    ind.set(W + "firstLineChars", "0")
    residual = ind.get(W + "leftChars")
    print(f"      只清 firstLineChars 时 leftChars 仍为 {residual} -> 表格仍会右移")
    ok &= _assert(residual == "200", "演示漏改的后果（leftChars 残留）")

    # 正确修复：八项全清 + 关自适应
    P.fix_table_indent_penetration(table)
    after = cell.paragraphs[0]._p.find(f"{W}pPr/{W}ind")
    all_zero = all(
        (after.get(W + a) or "0") == "0"
        for a in ("firstLineChars", "firstLine", "leftChars", "left",
                  "rightChars", "right", "hangingChars", "hanging")
    )
    ok &= _assert(all_zero, "★ fix_table_indent_penetration 把八项全清零")
    ok &= _assert(table.autofit is False, "自适应被关掉（避免 Word 重算列宽）")

    # 多段落单元格
    doc2 = new_document()
    t2 = doc2.add_table(rows=1, cols=1)
    c2 = t2.cell(0, 0)
    c2.paragraphs[0].add_run("甲")
    c2.add_paragraph("乙")
    c2.add_paragraph("丙")
    touched = P.fix_table_indent_penetration(t2)
    ok &= _assert(touched >= 3, f"多段落单元格逐段处理（处理了 {touched} 段）")

    # 真实文件里表格整体缩进也是 0
    root = _root_of(_render_baseline())
    tbl = next(root.iter(W + "tbl"), None)
    if tbl is not None:
        tind = tbl.find(f"{W}tblPr/{W}tblInd")
        ok &= _assert(
            tind is not None and tind.get(W + "w") == "0",
            "表格整体缩进 w:tblInd = 0",
        )
        # 单元格内对齐为 left
        jcs = {p.find(f"{W}pPr/{W}jc").get(W + "val")
               for tc in tbl.iter(W + "tc") for p in tc.iter(W + "p")
               if p.find(f"{W}pPr/{W}jc") is not None}
        ok &= _assert(jcs == {"left"}, f"单元格内段落全部左对齐（实际 {jcs}）")
    return ok


# ---------------------------------------------------------------------------
# 坑4：商业字体不分发，只引用字体名
# ---------------------------------------------------------------------------

def check_pit_4(tmp: Path) -> bool:
    """坑 4：公文字体是商业字体 —— 只引用字体名，不随包分发。"""
    banner("坑 4  商业字体不随包分发 —— 只引用字体名，缺失时回退并提示")
    ok = True

    # ① 仓库里不能有字体文件
    font_files: list[str] = []
    for pat in ("*.ttf", "*.ttc", "*.otf", "*.TTF", "*.TTC"):
        font_files += [str(p.relative_to(PROJECT_ROOT))
                       for p in PROJECT_ROOT.rglob(pat)
                       if ".venv" not in str(p) and "node_modules" not in str(p)]
    ok &= _assert(font_files == [], f"仓库内无字体文件（发现 {font_files[:3]}）")

    # ② 配置里只出现字体**名**
    models = (PROJECT_ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
    ok &= _assert(
        ".ttf" not in models and ".ttc" not in models,
        "models.yaml 不含字体文件路径（只引用名字）",
    )

    # ③ FONT_FALLBACK 覆盖全部商业字体
    from mmwf.domain.docx import FONT_FALLBACK, GB_STYLES

    commercial = {"方正小标宋简体", "仿宋_GB2312", "楷体_GB2312"}
    ok &= _assert(
        commercial <= set(FONT_FALLBACK),
        f"三个商业字体都有回退链（缺 {commercial - set(FONT_FALLBACK)}）",
    )

    # ④ 每个用到的字体都能查到（装了就不回退，没装就有链）
    used = {s.font for s in GB_STYLES.values()}
    non_system = used - {"黑体", "宋体"}
    ok &= _assert(
        non_system <= set(FONT_FALLBACK),
        f"所有非系统字体都在回退表里（缺 {non_system - set(FONT_FALLBACK)}）",
    )

    # ⑤ 部署手册的字体清单与代码一致
    deploy = (PROJECT_ROOT.parent.parent / "outputs" / "开发文档"
              / "04-部署运维与保密合规手册.md")
    if not deploy.exists():
        deploy = PROJECT_ROOT.parent / "开发文档" / "04-部署运维与保密合规手册.md"
    if deploy.exists():
        text = deploy.read_text(encoding="utf-8")
        listed = [f for f in used if f in text]
        ok &= _assert(
            len(listed) >= 4,
            f"代码用到的字体在部署手册 §2.2 清单里（命中 {len(listed)} 个）",
        )
    else:
        print("      (跳过：未找到部署手册)")
    return ok


# ---------------------------------------------------------------------------
# 附加 1：幂等导出（C07 / IT-09）
# ---------------------------------------------------------------------------

def check_idempotency(tmp: Path) -> bool:
    """附加 1：同输入重复导出，路径相同、文件只有一个（C07/IT-09）。"""
    banner("附加 1  幂等导出：重复导出不产生第二个文件（C07 / IT-09）")
    ok = True

    _, db, store = _make_env(tmp, "a1")
    _insert_meeting(db)
    store.write_text("record/v1.md", BASELINE_MD, stage="record")
    svc = ExportService(store, db_path=db, audit=AuditLogger(db), operator=OPERATOR)

    r1 = svc.export_docx(installed_fonts=FONTS_OK)
    r2 = svc.export_docx(installed_fonts=FONTS_OK)
    r3 = svc.export_docx(installed_fonts=FONTS_OK)

    ok &= _assert(r1.reused is False, "首次导出是新建")
    ok &= _assert(r2.reused is True and r3.reused is True, "重复导出命中幂等")
    ok &= _assert(r1.rel_path == r2.rel_path == r3.rel_path, "三次导出路径相同")
    ok &= _assert(r1.sha256 == r3.sha256, "三次导出内容哈希相同")
    ok &= _assert(
        r1.idempotency_key == r3.idempotency_key,
        f"幂等键稳定：{r1.idempotency_key}",
    )

    files = list(store.resolve(EXPORT_AREA).glob("*.docx"))
    ok &= _assert(len(files) == 1, f"磁盘上只有一个 docx（实际 {[f.name for f in files]}）")
    entries = [e for e in store.manifest() if e.path.startswith(f"{EXPORT_AREA}/")]
    ok &= _assert(len(entries) == 1, "manifest 只有一条导出记录")
    ok &= _assert(store.verify_manifest() == [], "manifest 与实际文件一致")

    # 内容变了 -> 幂等键变
    store.write_text("record/v2.md", BASELINE_MD + "\n补充一句。\n", stage="record")
    r4 = svc.export_docx(version=2, installed_fonts=FONTS_OK)
    ok &= _assert(r4.idempotency_key != r1.idempotency_key, "内容/版本变化时键也变")
    return ok


# ---------------------------------------------------------------------------
# 附加 2：合规红线
# ---------------------------------------------------------------------------

def check_compliance(tmp: Path) -> bool:
    """附加 2：审计不含正文、export/ 不加密、审计链完好。"""
    banner("附加 2  合规红线：审计不含正文 + export/ 不加密 + 审计链完好")
    ok = True

    _, db, store = _make_env(tmp, "a2")
    _insert_meeting(db)
    store.write_text("record/v1.md", BASELINE_MD, stage="record")
    svc = ExportService(store, db_path=db, audit=AuditLogger(db), operator=OPERATOR)
    r = svc.export_docx(installed_fonts=FONTS_OK)

    # ① 交付物必须是可直接打开的明文（否则 Word 打不开，公文就废了）
    ok &= _assert(EXPORT_AREA not in ENCRYPTED_AREAS, "export/ 属于**非**加密区（SAD §11.2）")
    ok &= _assert(r.encrypted is False, "导出结果标记为未加密")
    raw = store.resolve(r.rel_path).read_bytes()
    ok &= _assert(zipfile.is_zipfile(io.BytesIO(raw)), "产出的是可直接打开的 docx")

    # ② 加密区没有被顺手改成明文
    for area in ("record", "transcript", "audio", "minutes"):
        ok &= _assert(area in ENCRYPTED_AREAS, f"{area}/ 仍是加密区")

    # ③ 审计 detail 不含正文（部署手册 §5.3 红线）
    recs = [x for x in query(db) if x.action == "export"]
    detail_raw = json.dumps([x.detail for x in recs], ensure_ascii=False)
    ok &= _assert("会议纪要" not in detail_raw, "审计 detail 不含标题正文")
    ok &= _assert("抓好落实" not in detail_raw, "审计 detail 不含正文句子")
    ok &= _assert("张三丰" not in detail_raw, "审计 detail 不含人名")
    ok &= _assert("idempotency_key" in detail_raw, "审计记了幂等键（便于追溯）")

    # ④ 审计动作已登记
    ok &= _assert("export" in ACTIONS and "export_undo" in ACTIONS, "export/export_undo 已登记")

    # ⑤ 审计链完好
    ok &= _assert(verify_chain(db).ok, "审计链完好")

    # ⑥ 撤销导出（F5.8）
    ok &= _assert(svc.undo_export(r.rel_path) is True, "撤销导出成功")
    ok &= _assert(not store.exists(r.rel_path), "撤销后文件已删除")
    ok &= _assert(store.verify_manifest() == [], "撤销后 manifest 干净")
    ok &= _assert(
        len([x for x in query(db) if x.action == "export_undo"]) == 1,
        "撤销动作留痕",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 3：编号与结构
# ---------------------------------------------------------------------------

def check_structure(tmp: Path) -> bool:
    """附加 3：中文标题编号不跳号重号；表格不粘连；文本转义。"""
    banner("附加 3  中文标题编号（一、/（一）/1.）与文档结构")
    ok = True

    md = ("# 大标题\n\n"
          "## 会议基本情况\n\n### 时间地点\n\n### 参会人员\n\n"
          "## 议定事项\n\n### 第一项\n\n## 下阶段安排\n")
    result = render_markdown_to_docx(md, options=RenderOptions(installed_fonts=FONTS_OK))
    buf = io.BytesIO()
    result.doc.save(buf)
    root = _root_of(buf.getvalue())
    texts = ["".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p")]
    texts = [t for t in texts if t.strip()]

    def find(prefix: str) -> list[str]:
        return [t for t in texts if t.startswith(prefix)]

    h1 = [t for t in texts if len(t) > 1 and t[0] in "一二三四五六七八九十" and t[1] == "、"]
    print(f"      一级标题：{h1}")
    ok &= _assert(h1 == ["一、会议基本情况", "二、议定事项", "三、下阶段安排"],
                  f"一级标题为「一、二、三、」（实际 {h1}）")

    h2 = find("（")
    print(f"      二级标题：{h2}")
    ok &= _assert(
        h2 == ["（一）时间地点", "（二）参会人员", "（一）第一项"],
        f"二级标题在每节内重新数（实际 {h2}）",
    )

    # 自带编号的标题不会被重复编号
    md2 = "# 大标题\n\n## 一、总体情况\n"
    r2 = render_markdown_to_docx(md2, options=RenderOptions(installed_fonts=FONTS_OK))
    b2 = io.BytesIO()
    r2.doc.save(b2)
    xml2 = _xml_of(b2.getvalue())
    ok &= _assert("一、总体情况" in xml2, "自带编号的标题剥掉后重新编号")
    ok &= _assert("一、一、" not in xml2, "★ 不出现「一、一、」重复编号")

    # 两张表之间有空段，不粘连
    two_tables = ("# 大标题\n\n"
                  "| 甲 | 乙 |\n| --- | --- |\n| 1 | 2 |\n\n"
                  "中间一段话\n\n"
                  "| 丙 | 丁 |\n| --- | --- |\n| 3 | 4 |\n")
    r3 = render_markdown_to_docx(two_tables, options=RenderOptions(installed_fonts=FONTS_OK))
    b3 = io.BytesIO()
    r3.doc.save(b3)
    root3 = _root_of(b3.getvalue())
    ok &= _assert(len(list(root3.iter(W + "tbl"))) == 2, "两张表各自独立，没被粘成一张")

    # 特殊字符转义（不破坏 XML）
    r4 = render_markdown_to_docx(
        "正文含 & 与 <标签> 与 \"引号\"\n", options=RenderOptions(installed_fonts=FONTS_OK)
    )
    b4 = io.BytesIO()
    r4.doc.save(b4)
    root4 = _root_of(b4.getvalue())      # 能解析 = XML 没坏
    joined = "".join(t.text or "" for t in root4.iter(W + "t"))
    ok &= _assert("&" in joined and "<标签>" in joined, "特殊字符原样呈现且 XML 未损坏")

    # 空输入不崩
    r5 = render_markdown_to_docx("", options=RenderOptions(installed_fonts=FONTS_OK))
    ok &= _assert(r5.block_count == 0, "空输入不崩")
    return ok


# ---------------------------------------------------------------------------
# 附加 4：闸口（分层、依赖、用例）
# ---------------------------------------------------------------------------

def check_gates() -> bool:
    """附加 4：分层检查、依赖白名单、T1-09 用例、既有回归。"""
    banner("附加 4  分层检查 + 依赖白名单 + T1-09 用例 + 回归")
    ok = True

    def _run(cmd):
        return subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    for label, cmd in [
        ("分层检查 check_layers.py", [sys.executable, "tools/check_layers.py"]),
        ("依赖白名单 check_deps.py", [sys.executable, "tools/check_deps.py"]),
        # 基准必须能**重新生成**且自检合格 —— 这是测试方案 §1.2
        # 「改动 M11 时必跑」的那一步。
        ("版式基准快照生成+自检 make_docx_fixtures.py",
         [sys.executable, "tools/make_docx_fixtures.py", "--check"]),
        ("T1-09 用例",
         [sys.executable, "-m", "pytest",
          "tests/unit/test_docx_style.py", "tests/unit/test_docx_text.py",
          "tests/unit/test_export_service.py", "tests/unit/test_docx_snapshot.py",
          "-q", "--no-header"]),
        ("T1-08 回归（记录稿仍可生成）",
         [sys.executable, "-m", "pytest", "tests/unit/test_review_record.py",
          "-q", "--no-header"]),
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
# 附加 5：docx 依赖隔离（C01 / M11 约束）
# ---------------------------------------------------------------------------

def _real_imports(path: Path) -> set[str]:
    """取出文件里**真正生效**的顶层模块名。

    只看 AST 的 Import / ImportFrom 节点，这样注释与文档字符串里出现的
    "import docx" 之类字样不会被误判（本项目已经踩过一次：
    `domain/docx/__init__.py` 的文档字符串里为了解释 C01 约束，
    写了一句"领域层不 import sqlite3 等基础设施"，结果被字符串匹配抓到）。
    """
    import ast

    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level>0）没有顶层模块名，跳过
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def check_docx_isolation() -> bool:
    """附加 5：`python-docx` 只出现在 `domain/docx/` 下，且只在一个文件里。"""
    banner("附加 5  python-docx 单点隔离（SAD §4：须经 DocxWriter 抽象隔离）")
    ok = True

    violations: list[str] = []
    for py in (PROJECT_ROOT / "src").rglob("*.py"):
        rel = py.relative_to(PROJECT_ROOT).as_posix()
        if "docx" in _real_imports(py) and "/domain/docx/" not in rel:
            violations.append(rel)
    ok &= _assert(violations == [], f"只有 domain/docx/ 下 import docx（违规 {violations}）")

    # 且必须集中在 _docx_backend.py 一个文件里。
    # 注意：patches.py / writer.py 里那种"函数体内 import docx.enum..."也是
    # 真·依赖（运行时才解析），同样算违约——正是靠 AST 才抓得出来。
    allowed = "src/mmwf/domain/docx/_docx_backend.py"
    in_pkg: list[str] = sorted(
        py.relative_to(PROJECT_ROOT).as_posix()
        for py in (PROJECT_ROOT / "src" / "mmwf" / "domain" / "docx").rglob("*.py")
        if "docx" in _real_imports(py)
    )
    ok &= _assert(
        in_pkg == [allowed],
        f"★ 只有 _docx_backend.py 直接 import docx（实际 {in_pkg}）",
    )

    # 领域层不得 import 基础设施（同样走 AST，避免文档字符串误判）
    banned = ("sqlite3", "fastapi", "httpx", "torch", "funasr")
    leaks: list[str] = []
    for py in (PROJECT_ROOT / "src" / "mmwf" / "domain").rglob("*.py"):
        got = _real_imports(py) & set(banned)
        for b in sorted(got):
            leaks.append(f"{py.name}:{b}")
    ok &= _assert(leaks == [], f"领域层不含基础设施依赖（违约 {leaks}）")

    # _docx_backend 提供可执行的安装提示
    backend = (PROJECT_ROOT / allowed).read_text(encoding="utf-8")
    ok &= _assert("pip install" in backend, "缺少 python-docx 时给出可执行的安装指引")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="T1-09 验收")
    ap.add_argument("--keep-tmp", action="store_true", help="保留临时数据")
    args = ap.parse_args()

    print("T1-09 验收：公文版式映射与导出（docx，GB/T 9704—2012）")
    print(f"项目根：{PROJECT_ROOT}")

    # 缺字体场景会打一堆 warning，对验收输出是噪音
    logging.disable(logging.WARNING)

    tmp_root = Path(tempfile.mkdtemp(prefix="mmwf_t109_"))
    try:
        results: dict[str, bool] = {
            "标准 1 基准 docx 逐项比对（六条）": check_standard_1(tmp_root / "s1"),
            "标准 2 中英文混排加空格": check_standard_2(tmp_root / "s2"),
            "标准 3 英文引号 -> 中文引号": check_standard_3(tmp_root / "s3"),
            "标准 4 缺字体回退 + E5002 不崩": check_standard_4(tmp_root / "s4"),
            "坑 1 w:rFonts/@w:eastAsia 必须设": check_pit_1(tmp_root / "p1"),
            "坑 2 firstLine 与 Chars 不并存": check_pit_2(tmp_root / "p2"),
            "坑 3 表格缩进穿透（C15）": check_pit_3(tmp_root / "p3"),
            "坑 4 商业字体不分发": check_pit_4(tmp_root / "p4"),
            "附加 1 幂等导出（C07/IT-09）": check_idempotency(tmp_root / "a1"),
            "附加 2 合规红线 + 撤销导出": check_compliance(tmp_root / "a2"),
            "附加 3 编号与文档结构": check_structure(tmp_root / "a3"),
            "附加 4 分层 + 依赖 + 用例": check_gates(),
            "附加 5 docx 依赖单点隔离": check_docx_isolation(),
        }

        banner("验收结论")
        for name, ok in results.items():
            print(f"  {PASS if ok else FAIL} {name}")

        all_ok = all(results.values())
        print(f"\n{'=' * 78}")
        print(f"{PASS} T1-09 验收通过" if all_ok else f"{FAIL} T1-09 验收未通过")
        print(f"{'=' * 78}")
        return 0 if all_ok else 1
    finally:
        if args.keep_tmp:
            print(f"\n临时数据已保留：{tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
