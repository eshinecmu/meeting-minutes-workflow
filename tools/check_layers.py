"""层次依赖检查（SAD C01）。

领域层禁止 import 框架与网络库。退出码 0 = 通过；1 = 违规。
用法：python tools/check_layers.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_DIR = PROJECT_ROOT / "src" / "mmwf" / "domain"

FORBIDDEN_IN_DOMAIN = {
    "fastapi", "starlette", "uvicorn", "sqlite3", "sqlalchemy",
    "httpx", "requests", "torch", "funasr", "ollama", "langgraph",
    "pydub", "docx", "psutil",
}

# domain/docx 是唯一例外：它需要 python-docx 做样式映射，
# 但必须通过 domain/docx/_docx_backend.py 单点隔离
DOMAIN_DOCX_ALLOW = {"docx"}

# graph/ 是编排层（SAD §5）：它**允许** import langgraph，
# 但 langgraph 是惰性导入（只有 gate 节点与 build.py 真正用到）。
# 这里不做"允许清单"而是保持 FORBIDDEN_IN_DOMAIN 只作用于 domain/ ——
# graph 不在 DOMAIN_DIR 下，天然不受约束。列出该常量仅为可读性。
GRAPH_DIR = PROJECT_ROOT / "src" / "mmwf" / "graph"

#: API 层（T1-11 新增）。
#:
#: ⚠️ 为什么要把 api/ 纳入检查：T1-11 之前这个脚本**只扫 domain/ 与
#: graph/**，于是 api/ 与 main.py 完全在 C01 的视野之外 ——
#: `api/routes.py` 里 `import docx` 或 `import torch` 不会有任何东西报警。
#: 而 API 层恰恰是"最容易顺手 import 一切"的地方（要序列化、要落库、
#: 要调服务）。不检查等于给 C01 开了个后门。
API_DIR = PROJECT_ROOT / "src" / "mmwf" / "api"
MAIN_PY = PROJECT_ROOT / "src" / "mmwf" / "main.py"

#: API 层**禁止** import 的东西。
#:
#: 与领域层名单的差别，正是"应用层可以做什么"的边界：
#:   · fastapi / starlette / uvicorn  —— 它是 API 层，本来就要用（放行）；
#:   · sqlite3 / sqlalchemy           —— ❌ 落库必须走 infra/db.py 的
#:     `connect_ctx`（那里统一处理编码、外键、事务与路径解析）。
#:     绕过它直接 connect，会在别的机器上因为相对路径而产生第二份库；
#:   · httpx / requests               —— ❌ 本项目**离线运行，运行期一个
#:     外部请求都不发**。httpx 只允许出现在 tests/ 里做 TestClient；
#:   · torch / funasr / ollama        —— ❌ 重依赖必须藏在 adapters/ 后面，
#:     由 config 切换。API 层直接碰它们等于把引擎焊死；
#:   · docx / langgraph               —— ❌ 各有唯一的合法入口
#:     （domain/docx 后端单点 / graph.build_pipeline），API 层只调服务。
#:
#: ⚠️ `psutil` 与上面几条**不是一类**，所以这里要单独说明它为什么**放行**：
#:   T1-13 的 `/system/selfcheck` 就是要在 API 层"问操作系统：我这个进程
#:   有没有外发连接"。psutil 是**纯本地**系统信息库，自身不发网络请求 ——
#:   它不违反"API 层不发外部请求"那条（那条禁的是 httpx/requests
#:   这类**联网客户端**）。把它挡在 API 层外，等于让自检无处落脚。
#:   但**领域层仍然禁止** import 它（见 `FORBIDDEN_IN_DOMAIN`）——
#:   领域层只认 dataclasses 与标准库，这条没有例外。
FORBIDDEN_IN_API = {
    "sqlite3", "sqlalchemy",
    "httpx", "requests",
    "torch", "funasr", "ollama",
    "docx", "langgraph",
}

#: 编排层的硬约束：**不许 import 领域层的内部实现**，
#: 只能走各领域包的对外的 `__init__`（SAD C01 的组合根规则）。
LAYER_VIOLATION_PATTERNS: tuple[tuple[str, str], ...] = (
    # (正则片段, 说明) —— 编排层直接摸领域层私有模块
    ("mmwf.domain.docx._docx_backend", "编排层不得直接触碰 docx 后端单点"),
)

#: API 层**必须**遵守的额外约束（T1-11）。
#:
#: 这三条都是"漏了就出事、但很难靠人眼发现"的类型：
API_LAYER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "0.0.0.0",
        "API 层出现 0.0.0.0 —— C11 只允许监听 127.0.0.1",
        "禁止 0.0.0.0",
    ),
    (
        "http://localhost",
        "API 层出现 http://localhost —— 拼接回环地址请用 127.0.0.1",
        "禁止 localhost",
    ),
)


def check_graph_layer(path: Path) -> list[str]:
    """编排层的额外约束：不许越过领域包对外的 `__init__`。

    为什么单独查这一条：`graph` 与 `domain` 之间如果允许随意 import
    私有模块，那么"领域层可独立测试"这条设计就名存实亡 ——
    编排代码会把领域内部结构焊死，后续换实现要动两处。
    """
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    for needle, why in LAYER_VIOLATION_PATTERNS:
        if needle in text:
            errors.append(
                f"{path.relative_to(PROJECT_ROOT)} 编排层违规 import '{needle}'：{why}"
            )
    return errors


def _top_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name.split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module.split(".")[0]]
    return []


def check_api_layer(path: Path) -> list[str]:
    """API 层的额外约束（T1-11）。

    两类检查：
      ① **import 黑名单** —— 见 `FORBIDDEN_IN_API`。API 层是最容易
         "顺手 import 一切"的地方，不检查等于给 C01 开后门。
      ② **文本黑名单** —— `0.0.0.0` / `http://localhost`。
         这是 C11（只允许回环）在**代码层**的兜底：配置校验能挡住
         配置文件被改，但挡不住有人在代码里写死一个对外地址。
         用文本匹配而不是 AST，因为这两个值通常出现在**字符串字面量**
         里（`uvicorn.run(host="0.0.0.0")`），AST 只能看到常量节点，
         判不出它是地址还是别的字符串。
    """
    errors: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return [f"{path}: 语法错误，无法检查：{e}"]

    rel = path.relative_to(PROJECT_ROOT)
    for node in ast.walk(tree):
        for m in _top_modules(node):
            if m in FORBIDDEN_IN_API:
                lineno = getattr(node, "lineno", "?")
                errors.append(
                    f"{rel}:{lineno} API 层禁止 import '{m}'"
                    "（落库走 infra/db，网络请求只在 tests/，重依赖藏在 adapters/）"
                )

    text = path.read_text(encoding="utf-8")
    for needle, why, _label in API_LAYER_PATTERNS:
        if needle in text:
            lineno = next(
                (i for i, line in enumerate(text.splitlines(), 1) if needle in line), "?"
            )
            errors.append(f"{rel}:{lineno} {why}")
    return errors


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return [f"{path}: 语法错误，无法检查：{e}"]

    posix = path.as_posix()
    in_domain_docx = "/domain/docx/" in posix

    for node in ast.walk(tree):
        for m in _top_modules(node):
            if m not in FORBIDDEN_IN_DOMAIN:
                continue
            if m in DOMAIN_DOCX_ALLOW and in_domain_docx:
                continue
            lineno = getattr(node, "lineno", "?")
            errors.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno} 领域层禁止 import '{m}'")
    return errors


def main() -> int:
    if not DOMAIN_DIR.is_dir():
        print(f"! 领域层目录不存在，跳过：{DOMAIN_DIR}")
        return 0

    all_errors: list[str] = []
    checked = 0
    for py in DOMAIN_DIR.rglob("*.py"):
        checked += 1
        all_errors.extend(check_file(py))

    # 编排层（graph/）：只看"越过领域包对外的 __init__"这一类违规
    if GRAPH_DIR.is_dir():
        for py in GRAPH_DIR.rglob("*.py"):
            checked += 1
            all_errors.extend(check_graph_layer(py))

    # API 层（api/ 与 main.py，T1-11 起纳入）
    api_files = sorted(API_DIR.rglob("*.py")) if API_DIR.is_dir() else []
    if MAIN_PY.is_file():
        api_files.append(MAIN_PY)
    for py in api_files:
        checked += 1
        all_errors.extend(check_api_layer(py))

    if all_errors:
        print("\n".join(all_errors))
        print(f"\n✗ 层次依赖检查未通过，共 {len(all_errors)} 处违规")
        return 1
    print(f"✓ 层次依赖检查通过（检查 {checked} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
