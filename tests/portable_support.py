"""跨平台故障测试接缝；权限不足只影响符号链接子例。"""
import sys

import pytest


def directory_symlink(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows 创建符号链接需要管理员权限或开发者模式（WinError 1314）")
        raise
