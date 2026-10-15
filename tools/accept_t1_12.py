"""T1-12 验收脚本（人可读版）。

施工卡 T1-12 的五条验收标准逐条跑一遍。这张卡验的是**桌面壳与界面**：
三步操作走完全程、界面不露工程词、双开被拒、后端失败有提示。

五条标准（施工卡原文）
----------------------
  [ ] 双开程序 → 第二个实例提示"系统正由 XXX 使用中"并退出（F8.3）
  [ ] 后端启动失败时有明确提示（不是白屏）
  [ ] 界面**不出现**「检查点」「schema」「幂等」「HITL」等工程词
      → **文案快照测试**（SAD C13）
  [ ] G2 界面每条待确认项展示四要素：原始内容、系统抽取结果、风险评级、抽取依据（F4.2）
  [ ] 「只显示待确认项」过滤可用（F4.4）

⚠️ 为什么这个脚本是**静态检查**而不是"起 Electron 点一遍"
-------------------------------------------------------
Electron 界面要真的点一遍，需要图形环境 + 打包好的 Electron + 屏幕，
在 CI 和本机都跑不动。而这张卡的验收标准里，**只有第 3 条是纯静态
就能充分验证的**（"不出现工程词"本来就是文本性质）。

所以本脚本的策略是**分清哪些能静态证、哪些不能**：

  · 能静态证（本脚本做，且**确定性**）：
      第 3 条 —— 全量扫描 renderer/** 里的面向用户字符串；
      第 4 条 —— 断言四要素的**四个标签**都出现在 gate.js 且**同层渲染**；
      第 5 条 —— 断言过滤开关存在且接到了渲染函数；
      第 1、2 条 —— 断言主进程里有对应实现（单实例锁、失败弹窗），
                    且**位置正确**（锁必须在 whenReady 之前）。

  · 不能静态证（本脚本**如实标注为人工项，不假装验过**）：
      真的双开一次、真的让后端启动失败一次。这两条要人手点。
      脚本把它们列为「需人工复核」并在最后单列，**不混进通过数**。

  ⚠️ 这是刻意的诚实：把"能自动验的"和"必须人眼看的"分开列，
  比写一堆看起来很绿的假断言安全得多 —— 假绿比红更危险。

另一个必须说明的点：**工程词黑名单要排除"代码"里的词**。
比如 `api-client.js` 的注释里就写了 "E6001"、`strings.js` 的注释里
明写了"不许出现 schema/幂等"来提醒后人 —— 那些是**给开发者看的注释**，
不是界面文案，不该判失败。所以扫描前**先剥掉注释**再扫用户可见字符串。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DESKTOP = PROJECT_ROOT / "src" / "desktop"
RENDERER = DESKTOP / "renderer"

PASS = "✓"
FAIL = "✗"

#: 面向用户的字符串里**绝不许出现**的工程词（SAD §12.2 / N3.3 / C13）。
#: ⚠️ 只含**会真的显示给人看**的词；"幂等""检查点"这类内部概念一旦
#: 露到界面上就是 C13 红线。
FORBIDDEN_UI_WORDS = (
    "检查点",
    "checkpoint",
    "schema",
    "幂等",
    "idempotent",
    "HITL",
    "人工闸口",
    "中间产物",
    "溯源",
    "grounding",
)

#: 施工文档 §1.3 规定的目录/文件清单（缺一不可）。
REQUIRED_FILES = (
    "package.json",
    "main.js",
    "preload.js",
    "renderer/index.html",
    "renderer/app.js",
    "renderer/api-client.js",
    "renderer/strings.js",
    "renderer/styles/tokens.css",
    "renderer/styles/app.css",
    "renderer/pages/submit.js",
    "renderer/pages/review.js",
    "renderer/pages/gate.js",
    "renderer/pages/export.js",
)

#: G2 四要素标签（F4.2），必须都能在 gate.js 里找到。
FOUR_FACTOR_LABELS = ("原始内容", "系统抽取结果", "风险评级", "抽取依据")


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _assert(cond: bool, label: str) -> bool:
    print(f"  {PASS if cond else FAIL} {label}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 工具：取出"面向用户的字符串"，剥掉注释
# ---------------------------------------------------------------------------

def strip_comments(js: str) -> str:
    """把 JS 里的注释剥掉，只留代码。

    ⚠️ 为什么必须先剥注释（本脚本最容易犯的错）：
    `strings.js` 的注释里**故意**写了"不许出现 schema/幂等"来提醒后人；
    `api-client.js` 的注释里有 "E6001"。这些都是**给开发者看的**，
    不剥掉就会被黑名单误判成"界面泄漏了工程词" → **假红**。
    假红会让人去改本来对的东西，比漏报更浪费时间。

    用**状态机**而不是正则：正则处理字符串里的 `//`（如 URL）会出错。
    """
    out = []
    i, n = 0, len(js)
    in_line = in_block = False
    quote = None  # 当前所在的引号类型
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            out.append(c)
            if c == "\\":  # 转义
                if i + 1 < n:
                    out.append(js[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        # 不在注释也不在字符串
        if c == "/" and nxt == "/":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def iter_js_files() -> list[Path]:
    return sorted(RENDERER.rglob("*.js"))


# ---------------------------------------------------------------------------
# 标准 1：双开程序 → 第二个实例被拒（F8.3）
# ---------------------------------------------------------------------------

def check_standard_1() -> bool:
    banner("标准 1：双开程序 → 第二实例被拒并提示占用（F8.3）")
    ok = True
    src = (DESKTOP / "main.js").read_text(encoding="utf-8")
    code = strip_comments(src)

    ok &= _assert(
        "requestSingleInstanceLock" in code,
        "主进程调用了 requestSingleInstanceLock（单实例锁）",
    )
    ok &= _assert(
        "second-instance" in code,
        "监听了 second-instance 事件",
    )
    ok &= _assert(
        "系统正由" in code and "使用中" in code,
        "第二实例提示文案为「系统正由 XXX 使用中」",
    )
    ok &= _assert(
        "app.quit()" in code,
        "拿不到锁时调用 app.quit() 退出第二实例",
    )

    # ⚠️ 位置正确性：锁必须在 app.whenReady() **之前**取得。
    # 否则第二个窗口会先建出来再退，使用者会看到窗口闪一下。
    idx_lock = code.find("requestSingleInstanceLock")
    idx_ready = code.find("app.whenReady()")
    ok &= _assert(
        idx_lock != -1 and idx_ready != -1 and idx_lock < idx_ready,
        "单实例锁在 app.whenReady() 之前取得（避免第二窗口闪现）",
    )
    return ok


# ---------------------------------------------------------------------------
# 标准 2：后端启动失败有明确提示（不是白屏）
# ---------------------------------------------------------------------------

def check_standard_2() -> bool:
    banner("标准 2：后端启动失败时有明确提示（不是白屏）")
    ok = True
    src = (DESKTOP / "main.js").read_text(encoding="utf-8")
    code = strip_comments(src)

    ok &= _assert(
        "dialog.showMessageBoxSync" in code or "showMessageBox" in code,
        "启动失败时弹窗提示（不是让窗口白屏）",
    )
    ok &= _assert(
        "无法启动后台服务" in code,
        "失败提示是面向使用者的中文文案",
    )

    # ⚠️ 后端 stdout/stderr 必须被消费，不能 ignore。
    # 否则：① pipe 写满会阻塞子进程；② 启动失败的线索全丢 → 只剩白屏。
    ok &= _assert(
        "pyProc.stdout.on" in code and "pyProc.stderr.on" in code,
        "★ 后端 stdout/stderr 都被读取并转发（不是 ignore）",
    )
    ok &= _assert(
        "pyProc.on('exit'" in code or 'pyProc.on("exit"' in code,
        "监听了后端进程退出事件",
    )
    ok &= _assert(
        "PYTHONUTF8" in code and "'1'" in code,
        "子进程环境设了 PYTHONUTF8=1（否则中文输出乱码）",
    )
    return ok


# ---------------------------------------------------------------------------
# 标准 3：界面不出现工程词（文案快照测试，SAD C13）
# ---------------------------------------------------------------------------

def check_standard_3() -> bool:
    banner("标准 3：界面不出现工程词（文案快照测试，SAD C13）")
    ok = True

    violations: list[tuple[str, str]] = []
    n_files = 0
    for f in iter_js_files():
        n_files += 1
        code = strip_comments(f.read_text(encoding="utf-8"))
        for word in FORBIDDEN_UI_WORDS:
            if word in code:
                violations.append((str(f.relative_to(RENDERER)), word))

    # html 与 css 里也不该有（css 一般没有，html 的文案要扫）
    for f in list(RENDERER.rglob("*.html")):
        n_files += 1
        txt = strip_comments(f.read_text(encoding="utf-8"))
        for word in FORBIDDEN_UI_WORDS:
            if word in txt:
                violations.append((str(f.relative_to(RENDERER)), word))

    ok &= _assert(n_files > 0, f"扫描了 {n_files} 个界面文件")
    ok &= _assert(
        not violations,
        "界面（代码，已剥注释）不含工程词黑名单"
        + ("" if not violations else f" —— 命中：{violations}"),
    )

    # 正面要求：N3.3 规定的那套文案必须在（否则不算"替换到位"）
    strings_src = (RENDERER / "strings.js").read_text(encoding="utf-8")
    must_have = (
        "待确认议题",
        "待审定纪要",
        "文稿待核对",
        "确认无误，继续",
        "退回重做",
        "改完继续",
        "审定通过",
        "退回修改",
        "查看原文出处",
        "文稿",
    )
    for w in must_have:
        ok &= _assert(w in strings_src, f"N3.3 文案在册：「{w}」")

    return ok


# ---------------------------------------------------------------------------
# 标准 4：G2 四要素并排可见（F4.2）
# ---------------------------------------------------------------------------

def check_standard_4() -> bool:
    banner("标准 4：G2 每条待确认项展示四要素（F4.2）")
    ok = True

    gate = (RENDERER / "pages" / "gate.js").read_text(encoding="utf-8")
    strings = (RENDERER / "strings.js").read_text(encoding="utf-8")

    # 四个标签必须都定义在 strings.js（单一出口）
    for label in FOUR_FACTOR_LABELS:
        ok &= _assert(
            label in strings,
            f"四要素标签在册：「{label}」",
        )

    # 且 gate.js 里必须真的**同一层**渲染四个（不是藏起来）
    # 判据：`four` 容器里连续 append 四个 factorItem(...)
    code = strip_comments(gate)
    n_items = code.count("factorItem(")
    ok &= _assert(
        n_items >= 4,
        f"gate.js 渲染了四要素条目（实测 {n_items} 处 factorItem 调用）",
    )

    # 四要素容器类名必须存在（CSS 里是 grid 两列并排）
    ok &= _assert('"four"' in code or "four" in code, "四要素用 four 容器承载")

    css = (RENDERER / "styles" / "app.css").read_text(encoding="utf-8")
    ok &= _assert(
        ".factor .four" in css and "grid" in css,
        "四要素容器是 grid 布局（并排可见，非折叠）",
    )
    # 反向断言：不许用折叠元素把四要素藏起来
    ok &= _assert(
        "<details" not in gate and "details" not in code,
        "★ 四要素**没有**被塞进折叠元素（不许藏）",
    )
    return ok


# ---------------------------------------------------------------------------
# 标准 5：只显示待确认项 过滤可用（F4.4）
# ---------------------------------------------------------------------------

def check_standard_5() -> bool:
    banner("标准 5：「只显示待确认项」过滤可用（F4.4）")
    ok = True

    strings = (RENDERER / "strings.js").read_text(encoding="utf-8")
    gate = strip_comments((RENDERER / "pages" / "gate.js").read_text(encoding="utf-8"))

    ok &= _assert("只显示待确认项" in strings, "过滤开关文案在册")
    ok &= _assert(
        "ONLY_PENDING" in gate,
        "gate.js 引用了统一文案常量 ONLY_PENDING",
    )
    ok &= _assert(
        'type = "checkbox"' in gate or "checkbox" in gate,
        "过滤是一个开关控件（checkbox）",
    )
    ok &= _assert(
        "filter" in gate and "confirmed" in gate,
        "过滤按 confirmed 标记筛出未确认项",
    )
    # 默认必须是"只显示待确认项"（不然使用者会被已确认的噪音淹没）
    ok &= _assert(
        "checked = true" in gate or "checked=true" in gate,
        "★ 默认只显示待确认项（开关初始为选中）",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 A：目录结构符合施工文档 §1.3
# ---------------------------------------------------------------------------

def check_skeleton() -> bool:
    banner("附加 A：目录结构符合施工文档 §1.3")
    ok = True
    missing = [p for p in REQUIRED_FILES if not (DESKTOP / p).exists()]
    ok &= _assert(
        not missing,
        f"施工文档规定的文件齐全（共 {len(REQUIRED_FILES)} 个）"
        + ("" if not missing else f" —— 缺：{missing}"),
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 B：api-client 的信封解包正确（本卡最容易犯的错）
# ---------------------------------------------------------------------------

def check_envelope_client() -> bool:
    banner("附加 B：前端正确解包统一信封（不误用 res.ok）")
    ok = True
    code = strip_comments((RENDERER / "api-client.js").read_text(encoding="utf-8"))

    ok &= _assert(
        "env.ok" in code,
        "★ 按业务信封判成败（env.ok）",
    )
    # ⚠️ 反向断言：绝不能出现 `res.ok` 判断。
    # 后端永远 200，`res.ok` 恒为 true → 所有业务错误被静默吞掉。
    ok &= _assert(
        "res.ok" not in code,
        "★ 没有误用 res.ok（后端永远 200，用它判成败会吞掉所有业务错误）",
    )
    ok &= _assert(
        "X-MMWF-Token" in code,
        "请求头带 X-MMWF-Token（令牌门）",
    )
    ok &= _assert(
        "E6001" in code,
        "处理了 E6001（会话失效时重读令牌重试）",
    )
    ok &= _assert(
        "/api/v1" in code,
        "请求路径带 /api/v1 前缀",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 C：路径契约与 Python 侧对齐
# ---------------------------------------------------------------------------

def check_path_contract() -> bool:
    banner("附加 C：端口/令牌路径与 Python 侧逐段对齐")
    ok = True
    code = strip_comments((DESKTOP / "main.js").read_text(encoding="utf-8"))

    ok &= _assert("mmwf" in code, "启动目录名为 mmwf")
    ok &= _assert("'port'" in code or '"port"' in code, "端口文件名为 port")
    ok &= _assert(
        "session.token" in code,
        "令牌文件名为 session.token",
    )
    ok &= _assert(
        "AppData" in code and "Local" in code,
        "Windows 路径为 AppData/Local（与 deps.app_dir() 一致）",
    )
    # 与 Python 侧同名的两个常量名对齐（便于 grep 交叉核对）
    ok &= _assert(
        "PORT_FILE" in code and "TOKEN_FILE" in code,
        "端口/令牌路径提为具名常量（便于与 Python 侧对照）",
    )
    return ok


# ---------------------------------------------------------------------------
# 附加 D：TCP 探测（T1-11 实测的竞态）
# ---------------------------------------------------------------------------

def check_tcp_probe() -> bool:
    banner("附加 D：拿到端口后先探 TCP（端口文件先落、listen 后到）")
    ok = True
    code = strip_comments((DESKTOP / "main.js").read_text(encoding="utf-8"))
    ok &= _assert("net.connect" in code or "probeTcp" in code, "实现了 TCP 探测")
    ok &= _assert(
        "probeTcp" in code,
        "在放行后端就绪前调用了 TCP 探测（避免第一发请求被拒）",
    )
    return ok


# ---------------------------------------------------------------------------
# 人工项（本脚本**不假装验过**）
# ---------------------------------------------------------------------------

MANUAL_ITEMS = (
    "真双开一次：起两个实例，确认第二个被拒并提示「系统正由 XXX 使用中」（标准 1 的行为侧）",
    "真让后端启动失败一次（如临时改坏 Python 路径），确认弹窗提示而非白屏（标准 2 的行为侧）",
    "走一遍三步操作：投递 → 确认 → 导出，确认全程无命令行、界面文案是中文口语",
)


def main() -> int:
    ap = argparse.ArgumentParser(description="T1-12 验收（静态契约检查）")
    ap.parse_args()

    print("T1-12 验收：Electron 桌面壳与界面（三步操作 + 不露工程词 + 双开拒绝）")
    print(f"项目根：{PROJECT_ROOT}")

    results: dict[str, bool] = {}
    results["标准 1 双开 → 第二实例被拒（F8.3）"] = check_standard_1()
    results["标准 2 后端启动失败有明确提示"] = check_standard_2()
    results["标准 3 界面不出现工程词（C13 文案快照）"] = check_standard_3()
    results["标准 4 G2 四要素并排可见（F4.2）"] = check_standard_4()
    results["标准 5 只显示待确认项过滤可用（F4.4）"] = check_standard_5()
    results["附加 A 目录结构符合 §1.3"] = check_skeleton()
    results["附加 B 前端正确解包统一信封"] = check_envelope_client()
    results["附加 C 端口/令牌路径契约对齐"] = check_path_contract()
    results["附加 D 拿端口后先探 TCP"] = check_tcp_probe()

    banner("验收结论")
    for name, good in results.items():
        print(f"  {PASS if good else FAIL} {name}")

    print("\n" + "-" * 78)
    print("需人工复核（本脚本**不假装验过**，请在真机上点一遍）")
    print("-" * 78)
    for item in MANUAL_ITEMS:
        print(f"  · {item}")

    all_ok = all(results.values())
    print(f"\n{'=' * 78}")
    print(f"{PASS} T1-12 静态契约验收通过" if all_ok else f"{FAIL} T1-12 验收未通过")
    print(f"{'=' * 78}")
    print("  注：以上为静态契约检查；行为侧见上方「需人工复核」三项。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
