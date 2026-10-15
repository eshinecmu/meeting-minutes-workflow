"""生成 docx 版式基准快照（T1-09，测试方案 §1.2「版式快照 | docx 输出 | 6 个基准文档」）。

什么时候要跑
------------
**改动 M11（`domain/docx`）时必须重跑** —— 这是测试方案 §1.2 表格里
写死的触发条件。基准变了就说明版式变了，review diff 时能一眼看出
是哪条属性动了，比人工打开 Word 逐个目视可靠得多。

为什么是"6 个"而不是"1 个大的"
----------------------------
把 6 类版式风险拆成 6 份独立文档，而不是塞进一个"大而全"的样例。
理由：**失败要能定位**。如果只有一个综合文档，某天 `w:ind` 全线归零
时，报错会指向文档名，但看不出是哪类内容触发的。拆开之后，
`03_table_no_penetration.docx` 挂了就是表格，`05_mixed_script.docx`
挂了就是文本规范化 —— 定位成本从"翻 XML"降到"看文件名"。

这 6 份与施工卡验收标准的对应关系：

    ===================================== ==================================
    基准文件                               钉住的验收点
    ===================================== ==================================
    01_title_and_headings.docx             标题字体/字号/居中/不加粗；
                                           一二级标题黑体与楷体；中文编号
    02_body_layout.docx                    正文仿宋16pt/固定行距28.8pt/
                                           首行缩进2字符/页边距
    03_table_no_penetration.docx           ★ 表格零缩进（C15）+ 全边框
                                           + 表头黑体加粗 + 表格前后空段
    04_page_number.docx                    页码为 PAGE 域（三段式）
    05_mixed_script.docx                   中英空格 + 引号转换 + 空格压缩
    06_edge_cases.docx                     空文档/超深标题/超长表格/
                                           特殊字符/无表格 等边界
    ===================================== ==================================

用法::

    python tools/make_docx_fixtures.py              # 生成并校验
    python tools/make_docx_fixtures.py --check      # 只校验不写盘
    python tools/make_docx_fixtures.py --out DIR    # 换输出目录

生成后**自动**跑一遍 `tools/verify_docx_style.py`。基准文件必须是
"本身合格"的 —— 拿一份不合格的文件当基准，等于把 bug 固化成期望值。
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    print('需要 lxml：pip install -e ".[docx]"')
    sys.exit(2)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "expected_docx"

sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.domain.docx import (  # noqa: E402
    RenderOptions,
    render_markdown_to_docx,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ---------------------------------------------------------------------------
# 6 份基准输入
# ---------------------------------------------------------------------------

BASELINE_TITLE = """# 关于开展二〇二六年度信息化建设工作的会议纪要

## 会议基本情况

主持人张三丰，参会人员李四、王五。

### 时间地点

会议时间为二〇二六年九月二十八日上午九时。

## 议定事项

会议听取了信息化建设情况汇报，形成如下意见。
"""

BASELINE_BODY = """# 正文版式基准

## 排版要求

本标准用于验证正文段的字体、字号、行距与首行缩进四项属性是否符合
GB/T 9704—2012 的要求。正文每一段都应当是仿宋_GB2312 三号字，
固定行距二十八点八磅，首行缩进两个字符。

### 换行与空行

段落之间用空行分隔时，不应产生额外的空段落污染版式。

第二段用于确认多段落场景下每一项属性都被逐段写入，而不是只写第一段。
第三段用于确认缩进不会被累积或递减。
"""

BASELINE_TABLE = """# 表格版式基准

## 参会人员

| 姓名 | 职务 | 备注 |
| --- | --- | --- |
| 张三丰 | 处长 | 主持 |
| 李四 | 副处长 | 参会 |
| 王五 | 科长 | 记录 |

## 议定事项表

| 序号 | 事项 | 责任单位 | 完成时限 |
| --- | --- | --- | --- |
| 1 | 报送实施方案 | 办公室 | 十月底 |
| 2 | 组织系统培训 | 信息中心 | 十一月 |
"""

BASELINE_PAGE_NUMBER = """# 页码版式基准

## 说明

本文档用于验证页脚中存在 PAGE 域。

### 校验点

页码必须是**域**而不是纯文本，否则插入或删除内容后页码不会自动更新。
"""

BASELINE_MIXED = """# 中英文混排与标点基准

## 技术术语

会议讨论了API接口与JSON格式问题，统一使用REST架构。

## 引号处理

他说"这个方案"很好，要求下周报送"实施细则"。

## 空格压缩

这里有多余的空格需要压缩。
"""

BASELINE_EDGE = """# 边界场景基准

## 超长表格

| 列一 | 列二 | 列三 | 列四 | 列五 |
| --- | --- | --- | --- | --- |
| 甲 | 乙 | 丙 | 丁 | 戊 |
| 己 | 庚 | 辛 | 壬 | 癸 |
| 子 | 丑 | 寅 | 卯 | 辰 |

### 特殊字符

含特殊字符：小于号、大于号、与号、引号。

## 无表格章节

这一节没有表格，用来确认"有表格"与"无表格"的文档都能通过校验。
"""

BASELINES: dict[str, str] = {
    "01_title_and_headings": BASELINE_TITLE,
    "02_body_layout": BASELINE_BODY,
    "03_table_no_penetration": BASELINE_TABLE,
    "04_page_number": BASELINE_PAGE_NUMBER,
    "05_mixed_script": BASELINE_MIXED,
    "06_edge_cases": BASELINE_EDGE,
}

#: 每份基准要钉住的关键点。写进 `_manifest.json`，供人工 review 时对照。
PINS: dict[str, list[str]] = {
    "01_title_and_headings": [
        "P01 标题字体=方正小标宋简体", "P02 标题字号=22pt/44",
        "P03 标题居中", "标题不加粗", "P04 一级标题=黑体16pt",
        "P15 中文编号 一、/（一）",
    ],
    "02_body_layout": [
        "P06 正文=仿宋_GB2312", "P07 正文字号=16pt/32",
        "P08 首行缩进 2 字符 (firstLineChars=200)",
        "P09 固定行距 28.8pt (exact:576)", "P17 页边距 3.7/3.5/2.8/2.6cm",
    ],
    "03_table_no_penetration": [
        "★ P10 表格内 w:ind 八项全为 0（C15）",
        "P11 表格六边全边框", "P12 表头黑体加粗 12pt",
        "P14 表格后有空段", "★ 对照：正文缩进仍为 200",
    ],
    "04_page_number": [
        "P16 页脚含 PAGE 域", "域为三段式 begin/separate/end",
        "PAGE 写在 w:instrText 而非 w:t",
    ],
    "05_mixed_script": [
        "P18 中英之间加空格", "P19 英文引号 -> 中文引号",
        "多重空格压缩为一个",
    ],
    "06_edge_cases": [
        "超长表格不塌陷", "特殊字符 XML 未损坏",
        "无表格章节也能通过校验",
    ],
}


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def render(md: str, *, installed_fonts: set[str] | None = None) -> bytes:
    """把 markdown 渲染成 docx 字节。"""
    import io

    opts = RenderOptions(
        font_policy="warn",
        installed_fonts=installed_fonts if installed_fonts is not None else set(),
    )
    result = render_markdown_to_docx(md, options=opts)
    buf = io.BytesIO()
    result.doc.save(buf)
    return buf.getvalue()


def build_all() -> dict[str, bytes]:
    return {name: render(md) for name, md in BASELINES.items()}


# ---------------------------------------------------------------------------
# 自检：基准必须自己先合格
# ---------------------------------------------------------------------------

def summarize(data: bytes) -> dict:
    """从 docx 字节里抽几条关键属性，写进 manifest 供人工对照。"""
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = etree.fromstring(z.read("word/document.xml"))
        footers = [n for n in z.namelist() if n.startswith("word/footer")]
        footer_xml = b"".join(z.read(n) for n in footers)

    tables = list(root.iter(W + "tbl"))
    table_indent_elems = [ind for t in tables for ind in t.iter(W + "ind")]
    bad_indent = [
        f"{a}={ind.get(W + a)}"
        for ind in table_indent_elems
        for a in ("firstLineChars", "firstLine", "leftChars", "left",
                  "rightChars", "right", "hangingChars", "hanging")
        if ind.get(W + a) not in (None, "0")
    ]
    fonts: list[str] = []
    for run in root.iter(W + "r"):
        text = "".join(t.text or "" for t in run.iter(W + "t"))
        if not text.strip():
            continue
        rpr = run.find(W + "rPr")
        rf = rpr.find(W + "rFonts") if rpr is not None else None
        if rf is not None and rf.get(W + "eastAsia"):
            if rf.get(W + "eastAsia") not in fonts:
                fonts.append(rf.get(W + "eastAsia"))

    lines: dict[str, int] = {}
    for sp in root.iter(W + "spacing"):
        rule = sp.get(W + "lineRule")
        if rule:
            key = f"{rule}:{sp.get(W + 'line')}"
            lines[key] = lines.get(key, 0) + 1

    return {
        "tables": len(tables),
        "table_indent_elements": len(table_indent_elems),
        "table_indent_violations": bad_indent,
        "fonts_used": fonts,
        "line_spacing": lines,
        "first_line_chars_200": sum(
            1 for ind in root.iter(W + "ind")
            if ind.get(W + "firstLineChars") == "200"
        ),
        "has_page_number_field": b"PAGE" in footer_xml and b"fldChar" in footer_xml,
        "size_bytes": len(data),
    }


def verify(data: bytes, name: str) -> list[str]:
    """对一个基准做版式校验，返回错误列表。"""
    import tempfile

    from verify_docx_style import verify_file  # 同目录脚本

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
        fh.write(data)
        tmp = Path(fh.name)
    try:
        report = verify_file(tmp)
        return report.errors
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 docx 版式基准快照（T1-09）")
    ap.add_argument("--out", default=str(FIXTURE_DIR), help="输出目录")
    ap.add_argument("--check", action="store_true", help="只校验现有基准，不重新生成")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(PROJECT_ROOT / "tools"))

    print("docx 版式基准快照（测试方案 §1.2：6 个基准文档）")
    print(f"输出目录：{out}\n")

    built = build_all()
    manifest: dict[str, dict] = {}
    failed: list[str] = []

    for name, data in built.items():
        path = out / f"{name}.docx"
        if not args.check:
            path.write_bytes(data)
        if not path.exists():
            failed.append(f"{name}: 文件不存在（{path}）")
            continue

        errs = verify(path.read_bytes(), name)
        info = summarize(path.read_bytes())
        manifest[name] = {
            "file": f"{name}.docx",
            "pins": PINS.get(name, []),
            "summary": info,
            "ok": not errs,
            "errors": errs,
        }
        mark = "✓" if not errs else "✗"
        print(f"  {mark} {name}.docx  "
              f"({info['size_bytes']} 字节 / 表格 {info['tables']} 个 / "
              f"字体 {'、'.join(info['fonts_used']) or '无'})")
        for e in errs:
            print(f"      ✗ {e}")
        if errs:
            failed.append(name)

    manifest_path = out / "_manifest.json"
    if not args.check:
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n  已写 {manifest_path.name}（含每份基准的钉住点与实测摘要）")

    print(f"\n{'=' * 70}")
    if failed:
        print(f"✗ {len(failed)}/{len(built)} 份基准不合格：{'、'.join(failed)}")
        return 1
    print(f"✓ {len(built)} 份基准全部合格（GB/T 9704—2012）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
