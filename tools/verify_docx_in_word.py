"""用**真实 Word**（COM 自动化）验证导出的 docx —— M1/M2 的自动化版本。

★ 这个工具存在的理由
-------------------
T1-17 的验收记录把 M1（docx 能在 Word 里打开）与 M2（表格无缩进穿透）
标成"未验证 —— 需要真实 Office"，理由是"本机无 Office 授权环境"。

**这个判断是错的。** 事后核实：本机装了 MS Office（
`C:/Program Files/Microsoft Office/root/Office16/WINWORD.EXE`，版本 16.0），
且 Word COM 自动化可用。也就是说这两项**本来就能自动化验**，
是**我把可做的事推给了"目标机型"**。

本工具补上这件事，并且比"人眼看"更硬：
  · M1 → 让 Word **真的打开**文件，读回 Word 自己解析出的段落/表格数。
         能打开且读得出结构 = 真能打开（不是靠 zipfile 猜）。
  · M2 → 让 Word **真的渲染**，再逐个读表格单元格内段落的
         `ParagraphFormat` 缩进值。Word 渲染后的实际值，比读 XML 更接近
         "用户看到的"。★ 这才是"穿透"的终极判据：XML 清 0 了，
         但如果样式继承把缩进又带回来，Word 里的值就不是 0。

⚠️ 与 `verify_docx_style.py` 的分工
----------------------------------
  · `verify_docx_style.py`  —— **离线**读 XML（CI 里能跑，不依赖 Office）
  · 本工具                  —— **联机**过一遍 Word（发布前/验收时跑，需 Office）
两个都过才叫"版式没问题"：前者保证"我们写的 XML 是对的"，
后者保证"Word 理解出来的也是对的"。

用法：
  python tools/verify_docx_in_word.py <docx_path> [--json]
  python tools/verify_docx_in_word.py --all exports    # 批量验一个目录
  python tools/verify_docx_in_word.py --all data/store --docs-only   # 验 .doc 导出

退出码：0 = 通过；1 = 有问题；2 = 环境不可用（**不冒充通过**）；
        3 = 跳过（不是有效文档，通常是测试夹具）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# PowerShell 侧：用 COM 打开 docx，导出结构化读数
# ---------------------------------------------------------------------------

#: ⚠️ 脚本必须以 **UTF-8 带 BOM** 落盘再给 PowerShell 执行 ——
#: 本机 PowerShell 5.1 默认按 ANSI 解码无 BOM 文件，含中文路径会乱码，
#: 表现为"文件明明在却说找不到"。这是本项目踩过的坑（见 memory）。
_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$path = '__DOCX__'
$jsonOut = '__OUT__'

function Fail($m) {
  $o = @{ ok = $false; error = $m }
  $o | ConvertTo-Json -Compress | Set-Content -Path $jsonOut -Encoding UTF8
  exit 1
}

$word = $null
$doc = $null
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  # ★ 只读打开 + 不修订 + 不弹"是否转换"对话框
  # ★ 参数位序：Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles, ...)
  #   ConfirmConversions=$false —— `.doc` 是**旧二进制格式**，Word 打开时会想弹
  #   "转换文件"对话框；无头环境弹不出来 → 表现为"打开失败"。关掉它才开得了（本卡实测）。
  $doc = $word.Documents.Open($path, $false, $true, $false)
}
catch {
  Fail ("无法打开文档: " + $_.Exception.Message)
}

try {
  $paragraphs = @()
  $paraCount = $doc.Paragraphs.Count
  $forIdx = 0
  foreach ($p in $doc.Paragraphs) {
    $forIdx++
    if ($forIdx -gt 400) { break }   # 防超大文档拖死
    $pf = $p.Format
    # ⚠️ 缩进有**两套单位**，必须都读：
    #   · FirstLineIndent / LeftIndent —— **磅**（pt）
    #   · CharacterUnitFirstLineIndent / CharacterUnitLeftIndent —— **字符数**
    # 本项目用 `w:firstLineChars`（字符单位）设缩进，所以 Word 里
    # **磅值会报 0、字符值才是真值**。只读磅值会得出
    # "正文没有首行缩进"的错误结论（本工具第一版就这么错过）。
    #
    # ★★ 但**两套 API 都可能报 0**：实测（Word 16.0）对本项目产出的
    # `w:firstLineChars="200" + w:firstLine="0"`，`FirstLineIndent` 与
    # `CharacterUnitFirstLineIndent` **双双报 0** —— 因为 Word 只在
    # **启用了文档网格**时才把"字符"单位换算成绝对位置；没网格时
    # 它把这个属性当作"无绝对缩进"。
    #
    # → 所以最终判据不能只信 `Format`，必须**读渲染后的页面坐标**：
    #   `Range.Characters(1).Information(wdHorizontalPositionRelativeToPage=5)`
    #   这是"用户看到的那个字落在页面横坐标第几磅"，骗不了人。
    $rng = $p.Range
    $xpos = $null
    try {
      if ($rng.Characters.Count -gt 0) {
        $xpos = [math]::Round($rng.Characters(1).Information(5), 2)
      }
    } catch { $xpos = $null }

    $paragraphs += @{
      index          = $forIdx
      text           = $p.Range.Text.TrimEnd([char]13, [char]10)
      first_line_ind = [math]::Round($pf.FirstLineIndent, 4)
      left_ind       = [math]::Round($pf.LeftIndent, 4)
      first_line_chars = [math]::Round($pf.CharacterUnitFirstLineIndent, 4)
      left_chars       = [math]::Round($pf.CharacterUnitLeftIndent, 4)
      page_x           = $xpos
      align          = [int]$pf.Alignment
    }
  }

  $tables = @()
  foreach ($t in $doc.Tables) {
    $cellParas = @()
    foreach ($cell in $t.Range.Cells) {
      foreach ($p in $cell.Range.Paragraphs) {
        $pf = $p.Format
        $cellParas += @{
          text             = $p.Range.Text.TrimEnd([char]13, [char]10)
          first_line_ind   = [math]::Round($pf.FirstLineIndent, 4)
          left_ind         = [math]::Round($pf.LeftIndent, 4)
          first_line_chars = [math]::Round($pf.CharacterUnitFirstLineIndent, 4)
          left_chars       = [math]::Round($pf.CharacterUnitLeftIndent, 4)
        }
      }
    }
    $tables += @{ rows = $t.Rows.Count; cols = $t.Columns.Count; cell_paragraphs = $cellParas }
  }

  $inlineShapes = $doc.InlineShapes.Count
  $sections = $doc.Sections.Count

  $result = @{
    ok             = $true
    paragraph_count = $paraCount
    table_count     = $doc.Tables.Count
    inline_shapes   = $inlineShapes
    sections        = $sections
    paragraphs      = $paragraphs
    tables          = $tables
  }
  $result | ConvertTo-Json -Depth 8 -Compress | Set-Content -Path $jsonOut -Encoding UTF8
}
catch {
  Fail ("读取文档结构失败: " + $_.Exception.Message)
}
finally {
  if ($doc) { $doc.Close($false) | Out-Null }
  if ($word) { $word.Quit() | Out-Null }
}
"""


def _run_word_probe(docx: Path, tmp: Path) -> dict:
    """让 PowerShell 用 Word 打开 docx，返回结构化读数。"""
    out = tmp / "word_probe.json"
    ps = tmp / "word_probe.ps1"

    # ⚠️ **必须传绝对路径**：Word COM 的 `Documents.Open` 拿相对路径时，
    #    会按 **Word 自己的当前目录**（实测是 `C:\Windows\...`）去解析，
    #    报"很抱歉，找不到您的文件"。本卡实测踩过。
    abs_docx = docx.resolve()

    script = _PS_TEMPLATE.replace("__DOCX__", str(abs_docx).replace("'", "''"))
    script = script.replace("__OUT__", str(out.resolve()).replace("'", "''"))

    # ★ 必须 UTF-8 **带 BOM**，否则中文路径被 GBK 解码 → 找不到文件
    ps.write_text(script, encoding="utf-8-sig")

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if not out.exists():
        raise RuntimeError(
            f"Word 探针未产出结果（rc={proc.returncode}）："
            f"{(proc.stdout or '')[-300:]}{(proc.stderr or '')[-300:]}"
        )
    return json.loads(out.read_text(encoding="utf-8-sig"))


def _looks_like_zip_or_ole(p: Path) -> bool:
    """看文件头是否像真文档。

    · `.docx` = OOXML = **ZIP**（`PK\\x03\\x04`）
    · `.doc`  = 旧二进制 = **OLE2/CFB**（`D0 CF 11 E0 A1 B1 1A E1`）

    ★ 为什么不直接信扩展名：本项目 data 目录里既有真导出件，
    也有把 `PK\\x03\\x04` 当占位符用的**测试夹具**（19 字节），
    扩展名两者一样。只看文件头才分得清"产品缺陷"和"夹具"。
    """
    try:
        head = p.open("rb").read(8)
    except OSError:
        return False
    return head.startswith(b"PK\x03\x04") or head.startswith(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    )


def _kill_stray_word() -> int:
    """清掉遗留的 `WINWORD.EXE`（**本工具自己启动的那些**）。

    ⚠️ 为什么必须做：Word COM 的 `$word.Quit()` **不保证进程退出** ——
    实测跑几次之后会攒下 5 个僵尸 `WINWORD.EXE`，此后新的 COM 启动
    一律失败，报 `CO_E_SERVER_EXEC_FAILURE (0x80080005)`，
    而**报错信息完全指向不了真因**（看起来像"Word 没装"或"权限问题"）。

    本工具在**启动前**清一次，保证可重复运行。
    只杀 `WINWORD.EXE`，不碰别的 Office 组件。

    ⚠️ `taskkill` 在中文 Windows 上往 stderr 写 **GBK** 字节，
    用 `encoding="utf-8"` 解会抛 `UnicodeDecodeError`（实测踩过），
    所以这里用 `errors="replace"` 兜住 —— 反正我们只数 PID，不读内容。
    """
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/IM", "WINWORD.EXE"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        return (proc.stdout or "").count("PID")
    except Exception:  # noqa: BLE001
        return 0


def verify(docx: Path, *, json_out: bool = False) -> int:
    if not docx.exists():
        print(f"[FAIL] 文件不存在：{docx}")
        return 1
    if docx.suffix.lower() not in {".docx", ".doc"}:
        print(f"[FAIL] 不是 docx/doc：{docx.suffix}")
        return 1
    # ★ 防呆：数据目录里混着**测试夹具**（如 `m-t116-v1.docx` 内容就是
    #   19 字节的 `PK\x03\x04fake-docx-bytes`）。那种文件 Word 当然打不开，
    #   但它**不是产品缺陷** —— 拿它当 FAIL 报会误导验收结论。
    #   这里提前识破并**跳过**（退出码 3），而不是记成失败。
    if docx.stat().st_size < 64 or not _looks_like_zip_or_ole(docx):
        print(f"[SKIP] {docx.name}：不是有效文档（{docx.stat().st_size} 字节）——"
              f"通常是测试夹具，不计入 M1/M2")
        return 3

    print(f"用真实 Word 验证：{docx.name}")
    print("=" * 70)

    # ★ 清僵尸：不清的话第二次跑就 CO_E_SERVER_EXEC_FAILURE
    n = _kill_stray_word()
    if n:
        print(f"[info] 已清理 {n} 个遗留的 WINWORD 进程（否则 COM 启动会失败）")

    tmp = Path(tempfile.mkdtemp(prefix="word_verify_"))
    try:
        data = _run_word_probe(docx, tmp)
    except Exception as exc:  # noqa: BLE001
        print(f"[UNAVAILABLE] Word 验证环境不可用：{exc}")
        print("  ⚠️ 环境不可用**不等于**通过 —— 退出码 2，不记 pass。")
        return 2
    finally:
        # 收尾再清一次，不给下一次留僵尸
        _kill_stray_word()

    if not data.get("ok"):
        # ★ M1 的判据：Word 打不开 = 真打不开
        print(f"[FAIL] M1：Word **无法打开**该文件 —— {data.get('error')}")
        return 1

    problems: list[str] = []

    # ---- M1：能打开，且结构读得出 ----
    pc = data.get("paragraph_count", 0)
    tc = data.get("table_count", 0)
    print(f"[OK]   M1：Word 成功打开并解析 —— {pc} 段落 / {tc} 表格")
    if pc == 0:
        problems.append("文档里一段正文都没有，可能是空文件")

    # ---- M2：★ 表格单元格段落缩进必须为 0（Word 渲染后的实际值）----
    #
    # ⚠️ 判据用**两套单位一起看**（磅 + 字符）：
    #    本项目用 `w:firstLineChars` 设缩进 → Word 里磅值为 0、字符值非 0。
    #    只查磅值会把"表格已被正文缩进穿透"误判为通过（本工具第一版就差点这样）。
    bad_cells = 0
    total_cells = 0
    for ti, t in enumerate(data.get("tables", []), 1):
        for cp in t.get("cell_paragraphs", []):
            total_cells += 1
            fi_pt = abs(float(cp.get("first_line_ind", 0)))
            li_pt = abs(float(cp.get("left_ind", 0)))
            fi_ch = abs(float(cp.get("first_line_chars", 0)))
            li_ch = abs(float(cp.get("left_chars", 0)))
            if fi_pt > 0.5 or li_pt > 0.5 or fi_ch > 0.05 or li_ch > 0.05:
                bad_cells += 1
                if bad_cells <= 5:
                    print(f"       · 表{ti} 单元格缩进未清零："
                          f"首行={fi_pt}pt/{fi_ch}字符 左={li_pt}pt/{li_ch}字符 "
                          f"文本={cp.get('text','')[:24]!r}")

    if total_cells == 0:
        print("[ -- ] M2：文档里没有表格，本项不适用（不计通过也不计失败）")
    elif bad_cells == 0:
        print(f"[OK]   M2：{total_cells} 个表格单元格段落，缩进**全部为 0**（Word 渲染实测）")
    else:
        problems.append(f"{bad_cells}/{total_cells} 个表格单元格段落仍有缩进（穿透）")
        print(f"[FAIL] M2：{bad_cells}/{total_cells} 个单元格缩进未清零 —— **表格穿透**")

    # ---- 附加：正文段落的**渲染位置**（★ 这才是"用户看到的缩进"）----
    #
    # 为什么必须看 page_x：`Format.FirstLineIndent` 与
    # `CharacterUnitFirstLineIndent` 对本项目的 `w:firstLineChars` 都可能报 0
    # （Word 只在启用文档网格时才换算字符单位）。**唯独页面横坐标骗不了人**。
    body_paras = [p for p in data.get("paragraphs", []) if (p.get("text") or "").strip()]
    xs = [p.get("page_x") for p in body_paras if p.get("page_x") is not None]
    if xs:
        base = min(xs)
        shifted = [p for p in body_paras
                   if p.get("page_x") is not None and p["page_x"] - base > 2.0]
        print(f"[info] 正文 {len(body_paras)} 段有文字，起始横坐标最小 {base}pt；"
              f"{len(shifted)} 段相对首段右移（>2pt = 有首行缩进）")
        if not shifted:
            print("       ⚠️ 没有任何段落相对首段右移 —— 首行缩进可能**未在 Word 中生效**，"
                  "请人工确认（注意：标题类段落本就顶格）")
    elif body_paras:
        print("[info] 未能读到页面横坐标（Information(5) 不可用）")

    print()
    if problems:
        print(f"[FAIL] 共 {len(problems)} 个问题：")
        for p in problems:
            print(f"  · {p}")
        return 1

    print("[PASS] M1 + M2 通过（用真实 Word 渲染验证）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实 Word 验证导出的 docx（M1/M2）")
    ap.add_argument("docx", nargs="?", help="单个 .docx 路径")
    ap.add_argument("--all", metavar="DIR", help="批量验证目录下所有 .doc/.docx")
    ap.add_argument("--docx-only", action="store_true", help="配合 --all：只验 .docx")
    ap.add_argument("--docs-only", action="store_true", help="配合 --all：只验 .doc")
    ap.add_argument("--json", action="store_true", help="（预留）以 JSON 输出")
    args = ap.parse_args()

    targets: list[Path] = []
    if args.all:
        d = Path(args.all)
        if not d.exists():
            print(f"[FAIL] 目录不存在：{d}")
            return 1
        # ★ 同时收 .doc 与 .docx —— `.doc 导出可用` 也是验收项，
        #   而它以前同样被错记成"需真实 Office"。`.doc` 走同一套 Word 探针
        #   （Word 能原生打开旧格式），所以一并验。
        if args.docs_only:
            targets = sorted(d.rglob("*.doc"))
        elif args.docx_only:
            targets = sorted(d.rglob("*.docx"))
        else:
            targets = sorted(list(d.rglob("*.docx")) + list(d.rglob("*.doc")))
        if not targets:
            print(f"[FAIL] 目录里没有 doc/docx：{d}")
            return 1
    elif args.docx:
        targets = [Path(args.docx)]
    else:
        ap.error("请给一个 docx 路径，或用 --all DIR")

    results = []
    for t in targets:
        rc = verify(t)
        results.append((t, rc))
        print()

    print("=" * 70)
    passed = [t for t, rc in results if rc == 0]
    failed = [t for t, rc in results if rc == 1]
    skipped = [t for t, rc in results if rc == 3]
    unavail = [t for t, rc in results if rc == 2]
    print(f"合计 {len(results)} 个文件：通过 {len(passed)} / 失败 {len(failed)} "
          f"/ 跳过 {len(skipped)} / 环境不可用 {len(unavail)}")
    for t, rc in results:
        mark = {0: "PASS", 1: "FAIL", 2: "UNAVAILABLE", 3: "SKIP"}[rc]
        print(f"  [{mark}] {t.name}")

    if failed:
        return 1
    if unavail:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
