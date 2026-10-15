"""每个领域模块都必须能在全新解释器中独立导入。"""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src" / "mmwf" / "domain"


def _domain_modules():
    # 从文件系统遍历，避免收集时导入包而掩盖循环导入。
    for path in sorted(DOMAIN.rglob("*.py")):
        parts = path.relative_to(ROOT / "src").with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        yield ".".join(parts)


@pytest.mark.parametrize("module", list(_domain_modules()))
def test_domain_import_in_fresh_interpreter(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"{module} failed to import:\n{result.stdout}{result.stderr}"
    )
