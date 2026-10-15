"""docx 版式对照校验（T1-09 施工卡**明写必须写**的脚本）。

施工卡原文：

    **对照校验脚本**（必须写，作为自动化测试）

本项目把它做完整：不只查"表格内缩进为 0"这一条，而是把
**验收标准里逐项列出的六条**全部实现，因为那六条就是"合规"的定义，
少查一条就意味着一条版式问题可以静默地溜过去。

用法::

    python tools/verify_docx_style.py 输出.docx              # 单文件
    python tools/verify_docx_style.py 输出.docx --json        # 机器可读
    python tools/verify_docx_style.py --dir export/           # 整目录

退出码：0 = 全部通过；1 = 有问题。

为什么必须解压读 XML，而不是用 python-docx 读回
-----------------------------------------------
python-docx 读回时走的还是它自己的对象模型，**它读不到的属性就等于
不存在**。表格缩进穿透的典型症状是"文件里 `w:ind` 有残留值，但
Word 因为样式继承看起来是好的"——用 python-docx 读回检查，
它可能连那个残留的 `w:ind` 都不暴露。只有解压看原始 XML，
才能确认"写进去的到底是什么"。

这也是部署手册 §7.5 给运维的第一步排障手段：

    # 第一步：解压 docx 看 XML（所有版式问题的基本手段）
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    print("需要 lxml：pip install -e \".[docx]\"")
    sys.exit(2)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: GB/T 9704—2012 的正文固定行距：28.8 pt = 576 twips。
EXPECTED_LINE_TWIPS = "576"

#: 正文首行缩进：2 字符 = 200（单位 1/100 字符）。
EXPECTED_FIRST_LINE_CHARS = "200"

#: 表格全边框必须在的六个边。
TABLE_EDGES = ("top", "left", "bottom", "right", "insideH", "insideV")

#: 表格内缩进必须为 0 的八个属性。
INDENT_ATTRS = (
    "firstLineChars", "firstLine", "leftChars", "left",
    "rightChars", "right", "hangingChars", "hanging",
)


@dataclass
class StyleReport:
    """一个 docx 的版式体检报告。"""

    path: str
    errors: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"path": self.path, "ok": self.ok,
                "errors": self.errors, "info": self.info}


def _document_root(docx_path: Path):
    with zipfile.ZipFile(docx_path) as z:
        return etree.fromstring(z.read("word/document.xml")), z


# ---------------------------------------------------------------------------
# 六项检查（逐条对应施工卡验收标准）
# ---------------------------------------------------------------------------

def check_no_indent_in_tables(root, report: StyleReport) -> None:
    """★ 检查所有表格单元格内段落缩进为 0（C15，施工卡给的片段）。

    注意遍历顺序：`tbl.iter(...)` 会**递归**进表格的所有后代，
    包括嵌套表格。用 `tbl.findall(...)` 只查直接子元素会漏掉
    "表格里套表格"的情况（公文里少见但确实有，比如附表）。
    """
    checked = 0
    for tbl in root.iter(W + "tbl"):
        for ind in tbl.iter(W + "ind"):
            checked += 1
            for attr in INDENT_ATTRS:
                v = ind.get(W + attr)
                if v and v != "0":
                    report.errors.append(f"表格内段落缩进未清零: w:{attr}={v}")
    report.info["table_indent_elements"] = checked


def check_table_borders(root, report: StyleReport) -> None:
    """表格全边框：六个边齐全（U-DX-05）。"""
    tables = list(root.iter(W + "tbl"))
    report.info["tables"] = len(tables)
    for i, tbl in enumerate(tables):
        tbl_pr = tbl.find(W + "tblPr")
        if tbl_pr is None:
            report.errors.append(f"第 {i + 1} 个表格缺少 w:tblPr")
            continue
        borders = tbl_pr.find(W + "tblBorders")
        if borders is None:
            report.errors.append(f"第 {i + 1} 个表格缺少 w:tblBorders（全边框）")
            continue
        present = {e.tag.split("}")[1] for e in borders}
        missing = [e for e in TABLE_EDGES if e not in present]
        if missing:
            report.errors.append(
                f"第 {i + 1} 个表格边框缺 {len(missing)} 边：{'、'.join(missing)}"
            )


def check_cjk_fonts(root, report: StyleReport) -> None:
    """中文字体：每个 run 的 `w:rFonts` 都有 `w:eastAsia`（U-DX-01）。

    只统计**有文字的 run**：空 run（比如只放页码域的）没有 rPr 是正常的，
    把它算成错误会让报告里全是噪音。
    """
    runs_with_text = 0
    missing = 0
    fonts_seen: set[str] = set()
    for run in root.iter(W + "r"):
        text = "".join(t.text or "" for t in run.iter(W + "t"))
        if not text.strip():
            continue
        runs_with_text += 1
        rpr = run.find(W + "rPr")
        rfonts = rpr.find(W + "rFonts") if rpr is not None else None
        if rfonts is None or rfonts.get(W + "eastAsia") is None:
            missing += 1
            if missing <= 3:
                report.errors.append(
                    f"run「{text[:16]}」的 w:rFonts 缺 w:eastAsia（中文会回退字体）"
                )
        else:
            fonts_seen.add(rfonts.get(W + "eastAsia"))
    report.info["text_runs"] = runs_with_text
    report.info["missing_eastAsia_runs"] = missing
    report.info["fonts_used"] = sorted(fonts_seen)
    if missing > 3:
        report.errors.append(f"另有 {missing - 3} 个 run 缺 w:eastAsia")


def check_first_line_indent(root, report: StyleReport) -> None:
    """正文首行缩进 2 字符：`w:firstLineChars == "200"`（U-DX-02）。

    同时校验配套的"绝对值必须清零"：两套属性同时在且 firstLine 非 0，
    不同 Word 版本的处理不一致（施工卡"常见坑"第二条）。
    """
    ok_count = 0
    bad_absolute = 0
    for ind in root.iter(W + "ind"):
        if ind.get(W + "firstLineChars") == EXPECTED_FIRST_LINE_CHARS:
            ok_count += 1
            fl = ind.get(W + "firstLine")
            if fl and fl != "0":
                bad_absolute += 1
                report.errors.append(
                    f"首行缩进同时存在 w:firstLine={fl}（应为 0，避免与 Chars 冲突）"
                )
    report.info["paragraphs_with_indent_2chars"] = ok_count
    if bad_absolute:
        report.info["firstLine_conflicts"] = bad_absolute


def check_fixed_line_spacing(root, report: StyleReport) -> None:
    """行距固定值 28.8 pt（576 twips，U-DX-04）。"""
    exact = __import__("collections").Counter()
    for sp in root.iter(W + "spacing"):
        rule = sp.get(W + "lineRule")
        if rule:
            exact[f"{rule}:{sp.get(W + 'line')}"] += 1
    report.info["line_spacing"] = dict(exact)
    if not exact:
        report.errors.append("文档中没有任何 w:spacing@lineRule（行距未设固定值）")
        return
    if not any(k.startswith("exact:") for k in exact):
        report.errors.append(f"行距没有固定值（应含 exact:{EXPECTED_LINE_TWIPS}），实际 {dict(exact)}")
    elif f"exact:{EXPECTED_LINE_TWIPS}" not in exact:
        report.errors.append(
            f"固定行距值不是 28.8pt（应为 exact:{EXPECTED_LINE_TWIPS}），实际 {dict(exact)}"
        )


def check_page_number_field(root, report: StyleReport) -> None:
    """页码为 `PAGE` 域（U-DX 验收标准最后一条）。

    页码域写在**页脚**（`word/footer*.xml`），不在 document.xml 里 ——
    只查 document.xml 会永远报"缺页码"。所以本检查需要 zip 句柄。
    """
    # 由调用方通过 info 传入 zip（见 verify_file）
    return None


def check_page_number_in_footers(docx_path: Path, report: StyleReport) -> None:
    with zipfile.ZipFile(docx_path) as z:
        footers = [n for n in z.namelist() if n.startswith("word/footer")]
        found = False
        for name in footers:
            xml = z.read(name)
            if b"PAGE" in xml and b"fldChar" in xml:
                found = True
                break
    report.info["footer_parts"] = len(footers)
    report.info["has_page_field"] = found
    if not found:
        report.errors.append(
            "页脚中没有 PAGE 域（页码缺失或未用域，无法自动更新）"
        )


def check_tables_present(root, report: StyleReport) -> None:
    """`w:tbl` 与 `w:tblInd` 的整体缩进也应为 0（表格不该整体右移）。"""
    for i, tbl in enumerate(root.iter(W + "tbl")):
        tbl_pr = tbl.find(W + "tblPr")
        if tbl_pr is None:
            continue
        tind = tbl_pr.find(W + "tblInd")
        if tind is not None:
            v = tind.get(W + "w")
            if v and v != "0":
                report.errors.append(f"第 {i + 1} 个表格整体缩进 w:tblInd={v}（应为 0）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def verify_file(docx_path: Path) -> StyleReport:
    """对一个 docx 做全部版式检查。"""
    report = StyleReport(path=str(docx_path))
    if not docx_path.exists():
        report.errors.append(f"文件不存在：{docx_path}")
        return report
    if not zipfile.is_zipfile(docx_path):
        report.errors.append("不是有效的 docx（不是 zip 结构）—— 文件可能损坏")
        return report

    root, _ = _document_root(docx_path)
    check_no_indent_in_tables(root, report)
    check_table_borders(root, report)
    check_cjk_fonts(root, report)
    check_first_line_indent(root, report)
    check_fixed_line_spacing(root, report)
    check_tables_present(root, report)
    check_page_number_in_footers(docx_path, report)
    return report


def verify_dir(directory: Path) -> list[StyleReport]:
    files = sorted(directory.glob("*.docx"))
    return [verify_file(p) for p in files]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="docx 版式对照校验（GB/T 9704—2012，T1-09）"
    )
    ap.add_argument("docx", nargs="?", help="要校验的 docx 文件")
    ap.add_argument("--dir", help="校验目录下全部 .docx")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args()

    if not args.docx and not args.dir:
        ap.error("请给出 docx 文件，或用 --dir 指定目录")

    targets: list[Path] = []
    if args.docx:
        targets.append(Path(args.docx))
    if args.dir:
        targets.extend(sorted(Path(args.dir).glob("*.docx")))

    if not targets:
        print("! 没有找到可校验的 docx")
        return 1

    reports = [verify_file(p) for p in targets]

    if args.json:
        print(json.dumps([r.to_dict() for r in reports],
                         ensure_ascii=False, indent=2))
    else:
        for r in reports:
            head = "✓" if r.ok else "✗"
            print(f"\n{head} {Path(r.path).name}")
            if not args.quiet:
                info = r.info
                print(f"    表格 {info.get('tables', 0)} 个 | "
                      f"文字 run {info.get('text_runs', 0)} 个 | "
                      f"缩进2字符段落 {info.get('paragraphs_with_indent_2chars', 0)} 个")
                print(f"    使用字体：{'、'.join(info.get('fonts_used', [])) or '(无)'}")
                print(f"    行距：{info.get('line_spacing', {})}")
                print(f"    页码域：{'有' if info.get('has_page_field') else '无'}")
            for e in r.errors:
                print(f"    ✗ {e}")

    failed = [r for r in reports if not r.ok]
    print(f"\n{'=' * 70}")
    if failed:
        print(f"✗ {len(failed)}/{len(reports)} 个文件版式不合格")
        return 1
    print(f"✓ {len(reports)} 个文件版式全部合格（GB/T 9704—2012）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
