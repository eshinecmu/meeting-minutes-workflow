"""Keep strict domain typing enforced by the pytest merge gate."""

import subprocess
import sys
from pathlib import Path


def test_domain_mypy_strict() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "src/mmwf/domain", "--strict"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
