"""密钥保管（T1-03）。

职责**单一**：回答"32 字节主密钥从哪来"。加密算法在 crypto.py，不在本模块。

分层策略（按优先级尝试，谁的可用就用谁）
----------------------------------------
1. **`keyring`**（跨平台）：Windows 走 Credential Manager、macOS 走 Keychain、
   Linux 走 SecretService/kwallet。这是 SAD §11.2 指定的方式。
2. **Windows 原生 `win32cred`**：keyring 不可用但 pywin32 在时，直接调 Credential Manager。
3. **文件降级**（KEY_FILE_FALLBACK）：以上都不可用时的兜底。

⚠️ **关于第 3 档，必须明说的事实**
-----------------------------------
文件降级把密钥写在 `data/.master.key`（权限 600/仅本用户）。
**这不是"加密"，是"编码"**——任何能读到该文件的人都能解密全部数据。
它存在的唯一理由是：在没装可信凭据管理器的开发/演示环境里让系统能跑起来。

因此本模块做三件事，确保它**不会被误当成安全方案**：
  a. `KeySource` 返回值里带 `secure: bool` 标记，调用方可据此告警；
  b. 降级时写一条 WARNING 日志 + 在 keyfile 旁留 `KEY_STORAGE_UNHARDENED.txt`
     说明文件（部署手册 §6.5 加固项第 4/5 条会引用它）；
  c. `describe_key_source()` 供 `/system/selfcheck` 在界面上如实展示当前档位。

这样"当前版本密钥存储为待加固项"就不再是口头承诺，而是**可被程序读取的事实**。
"""

from __future__ import annotations

import getpass
import logging
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path

from mmwf.infra.crypto import KEY_LEN

logger = logging.getLogger(__name__)

# 凭据管理器里的条目坐标
SERVICE_NAME = "mmwf"
ACCOUNT_NAME = "master-key"

# 文件降级的文件名与警示文件名
KEY_FILE_NAME = ".master.key"
WARN_FILE_NAME = "KEY_STORAGE_UNHARDENED.txt"

_WARN_TEXT = """\
本文件的存在说明：mmwf 的主密钥当前以【明文文件】形式存放在同目录 .master.key。

这意味着：
  · 任何能读取该文件的账号都可以解密全部会议音频与文稿；
  · 它不满足《架构设计说明书》§11.2 对"密钥由操作系统凭据管理器保管"的要求。

请在正式部署前完成加固（部署运维与保密合规手册 §6.5）：
  1. 安装 keyring：pip install keyring
  2. 删除本文件与 .master.key
  3. 重新启动程序，密钥将改为写入操作系统凭据管理器
  4. 或在 unit 统一加密方案下直接依赖 BitLocker 并对 data/ 目录做访问控制

在此之前，请勿将该部署用于任何涉密或敏感场景。
"""


@dataclass(frozen=True)
class KeySource:
    """密钥来源描述。`secure=True` 表示密钥托管在 OS 凭据管理器。"""

    key: bytes
    backend: str
    secure: bool
    detail: str = ""


class KeyStoreError(RuntimeError):
    """无法获得主密钥。"""


# ---------------------------------------------------------------------------
# 后端探测
# ---------------------------------------------------------------------------

def _try_keyring_read() -> bytes | None:
    """从 keyring 读取密钥；不可用或未存过返回 None。"""
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        stored = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception as e:  # keyring 后端异常五花八门，统一降级
        logger.debug("keyring 读取失败，降级：%s", e)
        return None
    if not stored:
        return None
    try:
        key = bytes.fromhex(stored)
    except ValueError:
        logger.warning("keyring 中的密钥不是合法 hex，忽略")
        return None
    return key if len(key) == KEY_LEN else None


def _try_keyring_write(key: bytes) -> bool:
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, key.hex())
        return True
    except Exception as e:
        logger.debug("keyring 写入失败：%s", e)
        return False


def _try_win32cred_read() -> bytes | None:
    """Windows Credential Manager 直读（keyring 不在但 pywin32 在时）。"""
    if platform.system() != "Windows":
        return None
    try:
        import win32cred  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        cred = win32cred.CredRead(
            f"{SERVICE_NAME}:{ACCOUNT_NAME}", win32cred.CRED_TYPE_GENERIC
        )
    except Exception as e:
        logger.debug("win32cred 读取失败：%s", e)
        return None
    raw = cred.get("CredentialBlob")
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return key if len(key) == KEY_LEN else None


def _try_win32cred_write(key: bytes) -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import win32cred  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": f"{SERVICE_NAME}:{ACCOUNT_NAME}",
                "UserName": ACCOUNT_NAME,
                "CredentialBlob": key.hex().encode("utf-8"),
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "Comment": "mmwf master key (do not delete)",
            },
            0,
        )
        return True
    except Exception as e:
        logger.debug("win32cred 写入失败：%s", e)
        return False


def _key_file_path(data_dir: Path) -> Path:
    return Path(data_dir) / KEY_FILE_NAME


def _read_key_file(data_dir: Path) -> bytes | None:
    p = _key_file_path(data_dir)
    if not p.exists():
        return None
    try:
        key = bytes.fromhex(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError) as e:
        raise KeyStoreError(f"密钥文件损坏或不可读：{p}（{e}）") from e
    if len(key) != KEY_LEN:
        raise KeyStoreError(f"密钥文件长度非法：{p}（{len(key)} 字节，应为 {KEY_LEN}）")
    return key


def _write_key_file(data_dir: Path, key: bytes) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    p = _key_file_path(data_dir)

    # 先以最严权限创建，再写内容，避免"先写后改权限"的窗口期
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, key.hex().encode("ascii"))
    finally:
        os.close(fd)

    if platform.system() != "Windows":
        os.chmod(p, 0o600)
    else:
        _harden_windows_acl(p)

    warn = data_dir / WARN_FILE_NAME
    if not warn.exists():
        warn.write_text(_WARN_TEXT, encoding="utf-8")
        logger.warning(
            "主密钥以明文文件形式存放（%s）——这是待加固项，"
            "正式部署前必须改接 OS 凭据管理器", p
        )


def _harden_windows_acl(path: Path) -> None:
    """Windows 上收紧文件 ACL 到"仅当前用户"。

    与 POSIX 的 0600 等价。失败不中断（某些受限环境不允许改 ACL），
    但会告警——因为 ACL 没收紧意味着同机其他用户可能读到密钥。
    """
    user = getpass.getuser()
    try:
        import subprocess

        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            check=True, capture_output=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001 - 尽力而为，不阻断主流程
        logger.warning("收紧密钥文件 ACL 失败（%s）：%s；请人工确认文件权限", path, e)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def load_or_create_key(data_dir: Path, *, allow_file_fallback: bool = True) -> KeySource:
    """取得主密钥。已有则读出，没有则生成并持久化。

    查找顺序：keyring -> win32cred -> 密钥文件。
    生成时写入**第一个可用**的持久化后端。

    allow_file_fallback=False 时，若无凭据管理器则直接抛错——
    涉密部署应使用该模式，强制暴露"环境不具备安全密钥存储"这一事实。
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1) keyring（首选）
    key = _try_keyring_read()
    if key is not None:
        return KeySource(key=key, backend="keyring", secure=True,
                         detail=f"{SERVICE_NAME}:{ACCOUNT_NAME}")

    # 2) Windows 凭据管理器直连
    key = _try_win32cred_read()
    if key is not None:
        return KeySource(key=key, backend="win32cred", secure=True,
                         detail=f"{SERVICE_NAME}:{ACCOUNT_NAME}")

    # 3) 文件降级（若已存在则沿用，避免每次重启换密钥导致旧数据全废）
    file_key = _read_key_file(data_dir)

    # 生成新密钥并尝试写入安全后端
    new_key = file_key or os.urandom(KEY_LEN)

    if _try_keyring_write(new_key):
        return KeySource(key=new_key, backend="keyring", secure=True,
                         detail=f"{SERVICE_NAME}:{ACCOUNT_NAME}（本次新建）")
    if _try_win32cred_write(new_key):
        return KeySource(key=new_key, backend="win32cred", secure=True,
                         detail=f"{SERVICE_NAME}:{ACCOUNT_NAME}（本次新建）")

    if not allow_file_fallback:
        raise KeyStoreError(
            "无可用的操作系统凭据管理器（keyring / win32cred 均不可用），"
            "且未允许文件降级。请先安装 keyring，或显式开启文件降级"
            "（后者仅限非涉密环境，且必须登记为待加固项）。"
        )

    if file_key is None:
        _write_key_file(data_dir, new_key)

    return KeySource(
        key=new_key,
        backend="plaintext_file",
        secure=False,
        detail=(
            f"{_key_file_path(data_dir)} —— 明文文件，仅限非涉密环境，"
            f"部署手册 §6.5 加固项"
        ),
    )


def key_storage_is_hardened(data_dir: Path) -> bool:
    """供 /system/selfcheck 使用的判定：当前密钥是否托管在 OS 凭据管理器。"""
    src = load_or_create_key(data_dir)
    return src.secure


def describe_key_source(data_dir: Path) -> str:
    """人类可读的密钥来源描述（会写进自检结果）。"""
    src = load_or_create_key(data_dir)
    level = "已加固" if src.secure else "待加固"
    return f"[{level}] {src.backend}：{src.detail}"


def purge_key(data_dir: Path, *, from_file: bool = True) -> None:
    """删除持久化的密钥（用于测试与"重置系统"）。

    **不会**自动删除凭据管理器里的条目（那需要额外权限与显式意图），
    只删文件降级产生的密钥文件。凭据管理器条目的清理见部署手册。
    """
    if not from_file:
        return
    p = _key_file_path(Path(data_dir))
    if p.exists():
        p.unlink()
    warn = Path(data_dir) / WARN_FILE_NAME
    if warn.exists():
        warn.unlink()
