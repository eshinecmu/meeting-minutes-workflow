"""API 层依赖：会话令牌与启动文件（T1-11）。

本服务是**本机服务**，没有用户登录、没有账户体系。那靠什么防止
"别的程序/别的页面也能调我的接口"？三道门，本模块负责其中两道：

    ① 只听回环  —— `app.host` 校验（settings.py）+ 请求来源中间件（main.py）
    ② 启动令牌  —— 本模块：`secrets.token_urlsafe(32)`，每次启动重生成
    ③ 关闭文档  —— `main.py` 里 `docs_url=None` 等（否则浏览器一开就能探接口）

为什么令牌要写文件而不是留在内存
--------------------------------
后端是 Electron 用 `spawn` 拉起来的**子进程**，前端拿不到它的内存。
所以令牌与端口都必须落到约定路径的文件里，前端轮询读 —— 见
`02-开发施工文档` T1-12 的 `startBackend()`。

文件放哪：`%LOCALAPPDATA%/mmwf/`
-----------------------------
用 `LOCALAPPDATA` 而不是项目目录：项目目录可能是只读的安装目录
（部署手册 §2 的受控路径），也可能是多用户共享的。本机用户私有目录
才是"只有当前登录用户能读"的合理位置。

⚠️ **令牌文件是敏感文件**：它等同"本机后端的管理口令"。
所以 `create_session_token()` 会 `chmod 0600`；Windows 上
`chmod` 基本无效（ACL 决定权限），失败时**不抛错但也不声张** ——
真正的保护来自"只有回环能连"与"每次启动换令牌"，
文件权限是第三层加固，不是唯一防线。
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from mmwf.domain.errors import TokenInvalidError

logger = logging.getLogger(__name__)

#: 启动目录名（位于 LOCALAPPDATA 之下）
APP_DIR_NAME = "mmwf"

#: 令牌文件名。启动时重写，进程退出时**不删** —— 删掉反而让
#: "读不到文件"和"令牌不对"两种情况混在一起，前端不好区分。
TOKEN_FILENAME = "session.token"

#: 端口文件名。前端读它来拼 base URL。
PORT_FILENAME = "port"

#: 令牌长度（字节）。`token_urlsafe(32)` 产出约 43 个字符的 URL 安全串。
#: 32 字节 = 256 bit，穷举不可行；再长对前端没意义。
TOKEN_NBYTES = 32


def app_dir() -> Path:
    """启动目录：`%LOCALAPPDATA%/mmwf`（非 Windows 退到用户主目录）。

    目录不存在时**自动创建** —— 前端会在后端启动前就轮询这个目录，
    早点建出来能避免"目录不存在"和"后端还没启动"两种状态混淆。
    """
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home()
    d = root / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def token_path() -> Path:
    """令牌文件路径。"""
    return app_dir() / TOKEN_FILENAME


def port_path() -> Path:
    """端口文件路径。"""
    return app_dir() / PORT_FILENAME


def create_session_token(*, path: Path | None = None) -> str:
    """生成并写入新的会话令牌，返回令牌串。

    每次进程启动调用一次。**覆盖写**（不是追加/轮换历史）——
    上次启动的令牌必须立刻失效，否则旧令牌在重启后依然可用，
    令牌就退化成"一次设置、长期有效"的静态口令了。
    """
    token = secrets.token_urlsafe(TOKEN_NBYTES)
    p = path or token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 铁律 1：所有文件 I/O 显式指定 encoding（Windows 默认 GBK）
    p.write_text(token, encoding="utf-8")
    _harden(p)
    logger.info("已生成本次会话令牌（路径=%s）", p)
    return token


def read_session_token(*, path: Path | None = None) -> str | None:
    """读回当前令牌；文件不存在或读不出来时返回 `None`。

    返回 `None` 而不是抛错，是因为调用方（`require_token`）要把它
    转成面向用户的 E6001，而不是让一个 `FileNotFoundError` 冒出去。
    """
    p = path or token_path()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_port(port: int, *, path: Path | None = None) -> Path:
    """把实际监听端口写入文件，供 Electron 读取。"""
    p = path or port_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(port)), encoding="utf-8")
    return p


def read_port(*, path: Path | None = None) -> int | None:
    """读回端口；缺失或内容不是数字时返回 `None`。"""
    p = path or port_path()
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_session_files(*, token: Path | None = None, port: Path | None = None) -> None:
    """删除令牌与端口文件（进程退出时清理，或测试里复位）。

    `missing_ok=True`：并发退出、或测试里重复调用都不该报错。
    """
    for p in (token or token_path(), port or port_path()):
        try:
            p.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 权限异常时不影响退出流程
            logger.debug("清理启动文件失败：%s", p)


def verify_token(provided: str | None, *, path: Path | None = None) -> str:
    """校验令牌；不通过时抛 `TokenInvalidError`（E6001）。

    用 `secrets.compare_digest` 做**定长比较**，不用 `==`：
    字符串 `==` 会在第一个不同字符处短路返回，比较耗时随
    "前多少位相同"变化，理论上可被逐位试探（时序攻击）。
    本机场景实际风险很低，但保密检查会看这一条，而且改成
    `compare_digest` 的成本是零。
    """
    expected = read_session_token(path=path)
    if not expected:
        raise TokenInvalidError("本机尚未生成会话令牌（后端可能未就绪）")
    if not provided or not secrets.compare_digest(expected, provided):
        raise TokenInvalidError("令牌不匹配或已因重启失效")
    return provided


def _harden(p: Path) -> None:
    """把文件权限收紧到"仅属主可读写"。

    Windows 上 `os.chmod` 只影响只读位，**基本不生效** —— 所以这里
    `except OSError: pass` 而不是打警告刷屏。真正生效的是 POSIX；
    Windows 的隔离由"用户私有目录 + 仅回环可连"提供。
    刻意**不**用 `icacls` 之类的子进程去改 ACL：那会引入
    平台特定的外部命令依赖，与"最小依赖面"的部署要求相悖。
    """
    try:
        os.chmod(p, 0o600)
    except OSError:  # pragma: no cover - Windows 常态
        pass


__all__ = [
    "APP_DIR_NAME",
    "PORT_FILENAME",
    "TOKEN_FILENAME",
    "app_dir",
    "clear_session_files",
    "create_session_token",
    "port_path",
    "read_port",
    "read_session_token",
    "token_path",
    "verify_token",
    "write_port",
]
