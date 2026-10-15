"""依赖白名单检查（SAD C09/C10）。

禁止引入红线依赖：SenseVoice、Whisper-large-v3、云 SDK、遥测组件等。
用法：python tools/check_deps.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# (正则, 说明)
BANNED: list[tuple[str, str]] = [
    (r"\bsensevoice\b", "SenseVoice 官方已标注停止服务，禁止引入"),
    (r"\bwhisper[-_]?large[-_]?v3\b", "Whisper-large-v3 中文字错率 20.02%，禁止作主引擎"),
    (r"\bpyannote\b", "需 HuggingFace 令牌申请，内网构成流程障碍"),
    (r"\bdify\b", "重平台，与离线要求冲突"),
    (r"\bopenai\b", "云 SDK，禁止引入"),
    (r"\bazure[-_]", "云 SDK，禁止引入"),
    (r"\bdashscope\b", "云 SDK，禁止引入"),
    (r"\baliyun\b", "云 SDK，禁止引入"),
    (r"\btencentcloud\b|\bboto3\b|\bgoogle[-_]cloud\b", "云 SDK，禁止引入"),
    (r"\bposthog\b|\bsentry[-_]sdk\b|\bsegment\b", "遥测 SDK，禁止引入"),
    (r"\bmixpanel\b|\bamplitude\b", "遥测 SDK，禁止引入"),
]

# 要扫描的依赖清单文件
DEP_FILES = [
    "requirements.lock",
    "requirements.in",
    "requirements.txt",
    "pyproject.toml",
]


def main() -> int:
    scanned: list[str] = []
    hits: list[str] = []

    for name in DEP_FILES:
        path = PROJECT_ROOT / name
        if not path.exists():
            continue
        scanned.append(name)
        text = path.read_text(encoding="utf-8-sig").lower()
        for pattern, reason in BANNED:
            for m in re.finditer(pattern, text):
                line_no = text[: m.start()].count("\n") + 1
                hits.append(f"✗ {name}:{line_no} 检出禁用依赖 {pattern!r}：{reason}")

    if not scanned:
        print("! 未找到任何依赖清单文件，跳过检查")
        return 0

    if hits:
        print("\n".join(hits))
        print(f"\n✗ 依赖白名单检查未通过，共 {len(hits)} 处")
        return 1

    print(f"✓ 依赖白名单检查通过（扫描 {', '.join(scanned)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
