"""版式基准快照回归测试（T1-09，测试方案 §1.2「版式快照 | 6 个基准文档」）。

测试方案把这一组定义为**独立的一档**：

    | 版式快照 | docx 输出 | 6 个基准文档 | — | 改动 M11 时必跑 |

也就是说它不是"随手加的回归"，而是一条有明确触发条件的门禁：
**只要动了 `domain/docx`，这组就必须跑**。本文件把它固化成 pytest，
这样"必须跑"不依赖人的记性 —— CI 跑全量时自动覆盖。

快照测试的两种做法，以及这里为什么选后者
--------------------------------------
1. **字节比对**：把期望的 .docx 二进制提交进仓库，新产出与之逐字节比较。
   优点是不会漏；缺点是 python-docx 每次升级、甚至只是 zip 条目顺序变化
   都会让测试红掉，而版式其实没变 —— 这种"狼来了"会让人很快学会
   无视这条测试。
2. **属性比对**（本文件采用）：只钉**版式语义**——字体、字号、行距、
   缩进、边框、页码域。产物内部结构变了但版式没变时，测试保持绿。

会议纪要导出是**交付物**，它的"正确"由 GB/T 9704 定义，不由
python-docx 的序列化细节定义。所以选属性比对。

基准文件由 `tools/make_docx_fixtures.py` 生成。本文件同时校验
"基准文件本身合格"与"重新渲染的结果与基准属性一致"——
前者防止把 bug 固化成期望值，后者才是真正的快照回归。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from mmwf.domain.docx import RenderOptions, render_markdown_to_docx
from make_docx_fixtures import BASELINES, PINS

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "expected_docx"

#: 基准生成时用的字体集合。**必须与 `make_docx_fixtures.py` 一致**，
#: 否则重新渲染会走字体回退，字体断言就会对不上（那是假失败）。
FONTS_OK = {
    "方正小标宋简体", "黑体", "楷体_GB2312", "仿宋_GB2312", "宋体",
    "Times New Roman",
}

INDENT_ATTRS = (
    "firstLineChars", "firstLine", "leftChars", "left",
    "rightChars", "right", "hangingChars", "hanging",
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _render(md: str) -> bytes:
    opts = RenderOptions(font_policy="warn", installed_fonts=FONTS_OK)
    result = render_markdown_to_docx(md, options=opts)
    buf = io.BytesIO()
    result.doc.save(buf)
    return buf.getvalue()


def _fingerprint(data: bytes) -> dict:
    """把一份 docx 的**版式语义**抽成可比对的指纹。

    刻意不含字节数、时间戳、zip 顺序 —— 那些不是版式。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = etree.fromstring(z.read("word/document.xml"))
        footers = [n for n in z.namelist() if n.startswith("word/footer")]
        footer_xml = b"".join(z.read(n) for n in footers)

    # 中文字体（按首次出现顺序，去重）
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

    # 行距
    lines: dict[str, int] = {}
    for sp in root.iter(W + "spacing"):
        rule = sp.get(W + "lineRule")
        if rule:
            key = f"{rule}:{sp.get(W + 'line')}"
            lines[key] = lines.get(key, 0) + 1

    tables = list(root.iter(W + "tbl"))
    table_ind = [ind for t in tables for ind in t.iter(W + "ind")]

    # 字号
    sizes: dict[str, int] = {}
    for sz in root.iter(W + "sz"):
        v = sz.get(W + "val")
        if v:
            sizes[v] = sizes.get(v, 0) + 1

    return {
        "tables": len(tables),
        "fonts": fonts,
        "line_spacing": lines,
        "sizes": sizes,
        # ★ C15：表格内缩进的**违规**值（非 0 的），必须是空的
        "table_indent_violations": sorted({
            f"{a}={ind.get(W + a)}"
            for ind in table_ind
            for a in INDENT_ATTRS
            if ind.get(W + a) not in (None, "0")
        }),
        "table_indent_elements": len(table_ind),
        "first_line_chars_200": sum(
            1 for ind in root.iter(W + "ind")
            if ind.get(W + "firstLineChars") == "200"
        ),
        "has_page_field": b"PAGE" in footer_xml and b"fldChar" in footer_xml,
    }


def _existing(name: str) -> bytes:
    path = FIXTURE_DIR / f"{name}.docx"
    if not path.exists():
        pytest.skip(f"基准文件缺失：{path}（先跑 tools/make_docx_fixtures.py）")
    return path.read_bytes()


ALL_NAMES = sorted(BASELINES)


# ---------------------------------------------------------------------------
# 1. 基准文件本身合格（防止把 bug 固化成期望值）
# ---------------------------------------------------------------------------

class TestBaselineFilesExist:
    def test_six_baselines_present(self):
        """测试方案 §1.2 要求 6 个基准文档，一个不能少。"""
        missing = [n for n in ALL_NAMES if not (FIXTURE_DIR / f"{n}.docx").exists()]
        assert missing == [], (
            f"缺基准文件 {missing}；跑 tools/make_docx_fixtures.py 生成"
        )
        assert len(ALL_NAMES) == 6, f"基准数量应为 6，实际 {len(ALL_NAMES)}"

    def test_manifest_present_and_covers_all(self):
        mp = FIXTURE_DIR / "_manifest.json"
        if not mp.exists():
            pytest.skip("_manifest.json 缺失（先跑 tools/make_docx_fixtures.py）")
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        assert sorted(manifest) == ALL_NAMES

    def test_fixture_dir_has_no_stray_docx(self):
        """基准目录只该有这 6 份（+manifest）。

        多余的文件多半是调试时忘了删的产物 —— 它们不会被测试引用，
        却会出现在 `verify_docx_style.py --dir` 的输出里，让人以为
        "基准有 7 份"。
        """
        found = sorted(p.stem for p in FIXTURE_DIR.glob("*.docx"))
        assert found == ALL_NAMES, f"基准目录有非预期文件：{found}"

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_baseline_itself_passes_style_check(self, name):
        """基准必须**自己先合格**。拿不合格的文件当基准等于固化 bug。"""
        import tempfile

        from verify_docx_style import verify_file

        data = _existing(name)
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
            fh.write(data)
            tmp = Path(fh.name)
        try:
            report = verify_file(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        assert report.ok, f"{name} 基准版式不合格：{report.errors}"


# ---------------------------------------------------------------------------
# 2. 快照回归：重新渲染 → 版式指纹与基准一致
# ---------------------------------------------------------------------------

class TestSnapshotRegression:
    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_rerender_matches_baseline_fingerprint(self, name):
        """★ 核心断言：同一份输入重新渲染，版式语义必须与基准逐项一致。

        这条挂了就说明"改了 M11 导致版式漂移"，正是测试方案要求
        「改动 M11 时必跑」要拦住的事。
        """
        baseline = _fingerprint(_existing(name))
        fresh = _fingerprint(_render(BASELINES[name]))

        assert fresh == baseline, (
            f"{name} 版式指纹与基准不一致：\n"
            f"  仅基准有/值不同："
            f"{ {k: (baseline.get(k), fresh.get(k)) for k in set(baseline) | set(fresh) if baseline.get(k) != fresh.get(k)} }"
        )

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_baseline_has_no_table_indent_violation(self, name):
        """★ 所有基准（含 2 个带表格的）都不得有表格缩进穿透（C15）。

        单独拎出来，是因为这是本卡最高频的坑 —— 指纹整体不等时
        看 diff 要费点眼神，这一条能立刻指出"是不是又穿透了"。
        """
        fp = _fingerprint(_existing(name))
        assert fp["table_indent_violations"] == [], (
            f"{name} 表格缩进穿透：{fp['table_indent_violations']}"
        )


# ---------------------------------------------------------------------------
# 3. 每份基准确实钉住了它该钉的点
# ---------------------------------------------------------------------------

class TestPinsAreActuallyExercised:
    """`_manifest.json` 里给每份基准写了"钉住点"。

    但如果某份基准其实**没有**覆盖到它声称的那条属性（比如
    `03_table_no_penetration` 里一个表格都没渲染出来），PIN 就成了一句
    空话。这一组断言"声称钉住的，实际真的出现了"。
    """

    def test_table_baseline_really_has_tables(self):
        fp = _fingerprint(_existing("03_table_no_penetration"))
        assert fp["tables"] == 2, "表格基准应有 2 张表（参会人员 + 议定事项）"
        assert fp["table_indent_elements"] > 0, (
            "表格基准里没有 w:ind 元素（说明零缩进根本没被写入，"
            "校验会假通过）"
        )

    def test_body_baseline_has_indent_and_line_spacing(self):
        fp = _fingerprint(_existing("02_body_layout"))
        assert fp["first_line_chars_200"] > 0, "正文基准没有 2 字符缩进段落"
        assert "exact:576" in fp["line_spacing"], "正文基准没有固定行距 28.8pt"

    def test_page_number_baseline_has_field(self):
        fp = _fingerprint(_existing("04_page_number"))
        assert fp["has_page_field"] is True

    def test_title_baseline_has_title_font_and_sizes(self):
        fp = _fingerprint(_existing("01_title_and_headings"))
        assert "方正小标宋简体" in fp["fonts"], "标题基准缺标题字体"
        assert "44" in fp["sizes"], "标题基准缺 22pt（sz=44）字号"

    def test_all_baselines_use_fangsong_body(self):
        """除纯标题基准外，其余基准都该出现正文字体仿宋_GB2312。"""
        for name in ALL_NAMES:
            fp = _fingerprint(_existing(name))
            assert "仿宋_GB2312" in fp["fonts"], f"{name} 缺正文字体 仿宋_GB2312"

    def test_pins_defined_for_every_baseline(self):
        assert sorted(PINS) == ALL_NAMES, "每份基准都应有 PINS 说明"
        for name, pins in PINS.items():
            assert pins, f"{name} 的 PINS 为空"
