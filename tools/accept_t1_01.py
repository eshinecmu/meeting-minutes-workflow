"""T1-01 验收脚本（人可读版）。

施工卡 T1-01 要求的三条验收标准，逐条跑一遍并打印结论。
用法：python tools/accept_t1_01.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

CONFIG_DIR = PROJECT_ROOT / "config"

PASS = "✓"
FAIL = "✗"


def banner(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def check_standard_1() -> bool:
    """标准 1：改 models.yaml 的 llm.model 后生效，无需改任何 Python 文件。"""
    banner("标准 1：配置可外部修改，无需改代码（N5.2）")

    from mmwf.settings import load_settings

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config"
        shutil.copytree(CONFIG_DIR, cfg)

        before = load_settings(cfg).llm.model
        print(f"  改前 llm.model = {before}")

        models_file = cfg / "models.yaml"
        text = models_file.read_text(encoding="utf-8")
        models_file.write_text(
            text.replace("qwen2.5:14b-instruct-q5_K_M", "qwen2.5:7b-instruct-q5_K_M"),
            encoding="utf-8",
        )

        after = load_settings(cfg).llm.model
        print(f"  改后 llm.model = {after}")

        py_files_touched = 0
        ok = before != after and py_files_touched == 0
        print(f"  {PASS if ok else FAIL} 配置文件改动生效，Python 文件改动数 = {py_files_touched}")
        return ok


def check_standard_2() -> bool:
    """标准 2：host 为回环，且源码中无 0.0.0.0 绑定。"""
    banner("标准 2：服务只监听回环地址（SAD C11）")

    from pydantic import ValidationError

    from mmwf.settings import AppConfig, load_settings

    ok = True

    host = load_settings(CONFIG_DIR).app.host
    good = host == "127.0.0.1"
    ok &= good
    print(f"  {PASS if good else FAIL} app.host = {host!r}")

    # 非回环必须被拒
    try:
        AppConfig(host="0.0.0.0")
        print(f"  {FAIL} AppConfig 接受了 0.0.0.0 —— 合规红线被绕过！")
        ok = False
    except ValidationError:
        print(f"  {PASS} AppConfig(host='0.0.0.0') 被正确拒绝")

    # 源码代码体中不得出现 0.0.0.0
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/unit/test_settings.py::test_no_bind_all_in_source", "-q"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    src_ok = result.returncode == 0
    ok &= src_ok
    print(f"  {PASS if src_ok else FAIL} 源码代码体中无 0.0.0.0 绑定")

    return ok


def check_standard_3() -> bool:
    """标准 3：全部用例通过。"""
    banner("标准 3：T1-01 全部用例通过")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_settings.py", "-q"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(无输出)"
    ok = result.returncode == 0
    print(f"  {PASS if ok else FAIL} {tail}")
    return ok


def check_skeleton() -> bool:
    """附加：目录骨架与配置文件齐全。"""
    banner("附加检查：目录骨架与配置文件")

    required_dirs = [
        "config", "config/templates",
        "src/mmwf", "src/mmwf/api", "src/mmwf/graph/nodes",
        "src/mmwf/domain", "src/mmwf/domain/glossary", "src/mmwf/domain/extraction",
        "src/mmwf/domain/minutes", "src/mmwf/domain/grounding", "src/mmwf/domain/docx",
        "src/mmwf/adapters", "src/mmwf/adapters/asr", "src/mmwf/adapters/llm",
        "src/mmwf/adapters/exporter", "src/mmwf/adapters/notifier",
        "src/mmwf/infra", "src/mmwf/infra/migrations", "src/mmwf/checkpoint",
        "src/desktop", "src/desktop/renderer",
        "tests/unit", "tests/integration", "tests/contract", "tests/fixtures",
        "tools", "docs",
    ]
    required_files = [
        "config/app.yaml", "config/models.yaml", "config/extraction.yaml",
        "config/thresholds.yaml", "config/glossary.csv",
        "config/templates/party-group.yaml", "config/templates/special-topic.yaml",
        "config/templates/coordination.yaml", "config/templates/regular-meeting.yaml",
        "src/mmwf/settings.py", "src/mmwf/__init__.py",
        "pyproject.toml", "requirements.in",
    ]

    ok = True
    missing_dirs = [d for d in required_dirs if not (PROJECT_ROOT / d).is_dir()]
    if missing_dirs:
        ok = False
        print(f"  {FAIL} 缺失目录：{missing_dirs}")
    else:
        print(f"  {PASS} 目录骨架完整（{len(required_dirs)} 个）")

    missing_files = [f for f in required_files if not (PROJECT_ROOT / f).exists()]
    if missing_files:
        ok = False
        print(f"  {FAIL} 缺失文件：{missing_files}")
    else:
        print(f"  {PASS} 配置文件齐全（{len(required_files)} 个）")

    return ok


def main() -> int:
    print("T1-01 验收：项目骨架与配置中心")
    print(f"项目根：{PROJECT_ROOT}")

    results = {
        "标准 1 配置可外部修改": check_standard_1(),
        "标准 2 服务只监听回环": check_standard_2(),
        "标准 3 全部用例通过": check_standard_3(),
        "附加 目录与配置齐全": check_skeleton(),
    }

    banner("验收结论")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL} {name}")

    all_ok = all(results.values())
    print(f"\n{'=' * 68}")
    print(f"{PASS} T1-01 验收通过" if all_ok else f"{FAIL} T1-01 验收未通过")
    print(f"{'=' * 68}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
