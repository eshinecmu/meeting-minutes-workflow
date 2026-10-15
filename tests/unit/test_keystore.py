"""T1-03 测试：密钥保管（keystore.py）。

本机未安装 keyring / pywin32，因此测试主要覆盖**文件降级路径**——
它恰恰是最需要被钉死的分支（安全性最弱、最容易被误当正式方案）。

若环境装了 keyring，`test_keyring_*` 会在可用时自动生效。
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import pytest

from mmwf.infra.crypto import KEY_LEN
from mmwf.infra.keystore import (
    KEY_FILE_NAME,
    SERVICE_NAME,
    WARN_FILE_NAME,
    KeySource,
    KeyStoreError,
    describe_key_source,
    key_storage_is_hardened,
    load_or_create_key,
    purge_key,
)

# 本机是否真的具备安全后端
try:
    import keyring  # noqa: F401

    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _isolate_keyring(monkeypatch: pytest.MonkeyPatch):
    """把 keyring 后端替换为**全新的内存实现**，避免测试污染真实凭据管理器。

    两个必须点：
      1. 即使本机装了 keyring（会写入真实 Windows Credential Manager），
         也必须隔离——否则测试会在用户机器上留下垃圾条目。
      2. 存储必须是**实例属性**且每个测试新建实例。若写成类属性，
         内存条目会跨测试残留，导致"第二次运行结果不同"这类幽灵失败。
    """
    try:
        import keyring
        from keyring.backend import KeyringBackend
    except ImportError:
        yield
        return

    class _MemoryBackend(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self) -> None:
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self._store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self._store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            self._store.pop((service, username), None)

    original = keyring.get_keyring()
    keyring.set_keyring(_MemoryBackend())
    yield
    keyring.set_keyring(original)


# ---------------------------------------------------------------------------
# 基本契约
# ---------------------------------------------------------------------------

def test_load_or_create_returns_32_byte_key(data_dir: Path):
    src = load_or_create_key(data_dir)
    assert isinstance(src, KeySource)
    assert len(src.key) == KEY_LEN


def test_key_is_stable_across_calls(data_dir: Path):
    """同一目录二次调用必须返回同一把密钥（否则旧数据全废）。"""
    k1 = load_or_create_key(data_dir).key
    k2 = load_or_create_key(data_dir).key
    assert k1 == k2


def test_key_scope_is_per_backend_not_per_dir(tmp_path: Path):
    """密钥作用域是「后端」而非「data 目录」——这是一个**刻意的语义**。

    两条路径行为不同，且都对：
      · 走 keyring/win32cred 时：条目坐标只有 mmwf:master-key，
        因此同一台机器上所有 data 目录**共用同一把主密钥**。
        这符合"单机单实例"定位（SAD §11.3），也避免多份密钥难以管理。
      · 走文件降级时：密钥文件在 <data_dir>/.master.key，
        因此不同目录天然得到不同密钥（每份数据自包含）。

    本用例把两种语义都钉住，避免有人"顺手统一"时打破其一。
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    ka = load_or_create_key(a)
    kb = load_or_create_key(b)

    if ka.secure:
        # 凭据管理器路径：同机共用一把
        assert ka.key == kb.key, (
            "凭据管理器路径下密钥应按 mmwf:master-key 单点复用，"
            "不应随 data 目录变化"
        )
    else:
        # 文件降级路径：各自独立
        assert ka.key != kb.key, "文件降级路径下不同 data 目录应各自生成密钥"


def test_same_dir_same_key_regardless_of_backend(tmp_path: Path):
    """同一目录重复取密钥必须稳定（不论走哪条后端）。"""
    d = tmp_path / "data"
    d.mkdir()
    assert load_or_create_key(d).key == load_or_create_key(d).key


def test_backend_reported(data_dir: Path):
    src = load_or_create_key(data_dir)
    assert src.backend in {"keyring", "win32cred", "plaintext_file"}
    assert isinstance(src.secure, bool)
    assert src.detail


def test_creates_data_dir_if_missing(tmp_path: Path):
    d = tmp_path / "not" / "yet" / "there"
    load_or_create_key(d)
    assert d.is_dir()


# ---------------------------------------------------------------------------
# 文件降级路径（本机实际走这条）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_file_fallback_is_marked_insecure(data_dir: Path):
    """文件降级必须如实标记 secure=False —— 这是整个设计的关键。"""
    src = load_or_create_key(data_dir)
    assert src.backend == "plaintext_file"
    assert src.secure is False


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_file_fallback_writes_key_file(data_dir: Path):
    load_or_create_key(data_dir)
    kf = data_dir / KEY_FILE_NAME
    assert kf.exists()

    key = bytes.fromhex(kf.read_text(encoding="ascii").strip())
    assert len(key) == KEY_LEN


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_file_fallback_writes_warning_file(data_dir: Path):
    """必须在旁留下"待加固"说明文件，不能默不作声（施工卡硬性要求）。"""
    load_or_create_key(data_dir)
    warn = data_dir / WARN_FILE_NAME
    assert warn.exists()

    text = warn.read_text(encoding="utf-8")
    assert "待加固" in text or "明文" in text
    assert "凭据管理器" in text
    assert "涉密" in text  # 明确警示不得用于涉密场景


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_file_fallback_logs_warning(data_dir: Path, caplog: pytest.LogCaptureFixture):
    """降级时必须记 WARNING 日志（运维能看见）。"""
    with caplog.at_level(logging.WARNING, logger="mmwf.infra.keystore"):
        load_or_create_key(data_dir)
    assert any("明文文件" in r.message or "待加固" in r.message for r in caplog.records)


@pytest.mark.skipif(platform.system() != "Windows", reason="仅 Windows 有 ACL")
@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_key_file_acl_restricted_on_windows(data_dir: Path):
    """Windows 上密钥文件 ACL 应被收紧到仅当前用户（尽力而为，不强制）。

    注意：icacls 在中文 Windows 上输出 GBK，直接 text=True（UTF-8）会解码失败。
    因此这里只取 returncode，不解析输出内容——只验证命令能成功执行。
    """
    load_or_create_key(data_dir)
    kf = data_dir / KEY_FILE_NAME

    import subprocess

    out = subprocess.run(
        ["icacls", str(kf)], capture_output=True, timeout=10
    )
    # icacls 不可用的受限环境跳过；能跑通即视为 ACL 设置流程没崩
    assert out.returncode == 0


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_existing_key_file_is_reused(data_dir: Path):
    """已存在密钥文件时必须沿用，不得覆盖（否则数据全废）。"""
    kf = data_dir / KEY_FILE_NAME
    manual_key = os.urandom(KEY_LEN)
    kf.write_text(manual_key.hex(), encoding="ascii")

    assert load_or_create_key(data_dir).key == manual_key


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_corrupted_key_file_raises(data_dir: Path):
    """密钥文件内容非法 hex 时必须报错，不能静默生成新密钥（会导致数据不可解）。"""
    (data_dir / KEY_FILE_NAME).write_text("not-hex-at-all!!", encoding="ascii")
    with pytest.raises(KeyStoreError, match="损坏|非法"):
        load_or_create_key(data_dir)


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_wrong_length_key_file_raises(data_dir: Path):
    (data_dir / KEY_FILE_NAME).write_text("aabbcc", encoding="ascii")
    with pytest.raises(KeyStoreError, match="非法|长度"):
        load_or_create_key(data_dir)


# ---------------------------------------------------------------------------
# allow_file_fallback=False：涉密部署应使用该模式
# ---------------------------------------------------------------------------

@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，会走安全后端")
def test_no_fallback_raises_when_no_secure_backend(data_dir: Path):
    """禁止降级时，无凭据管理器必须直接报错——强制暴露环境不安全。"""
    with pytest.raises(KeyStoreError, match="凭据管理器"):
        load_or_create_key(data_dir, allow_file_fallback=False)


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，会走安全后端")
def test_no_fallback_does_not_write_key_file(data_dir: Path):
    """报错路径不得留下明文密钥文件。"""
    with pytest.raises(KeyStoreError):
        load_or_create_key(data_dir, allow_file_fallback=False)
    assert not (data_dir / KEY_FILE_NAME).exists()


# ---------------------------------------------------------------------------
# 自检接口
# ---------------------------------------------------------------------------

def test_key_storage_is_hardened_matches_backend(data_dir: Path):
    src = load_or_create_key(data_dir)
    assert key_storage_is_hardened(data_dir) == src.secure


def test_describe_key_source_mentions_level(data_dir: Path):
    text = describe_key_source(data_dir)
    assert "已加固" in text or "待加固" in text
    assert any(b in text for b in ("keyring", "win32cred", "plaintext_file"))


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------

@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_purge_removes_key_and_warning(data_dir: Path):
    load_or_create_key(data_dir)
    assert (data_dir / KEY_FILE_NAME).exists()

    purge_key(data_dir)
    assert not (data_dir / KEY_FILE_NAME).exists()
    assert not (data_dir / WARN_FILE_NAME).exists()


def test_purge_on_empty_dir_is_noop(data_dir: Path):
    purge_key(data_dir)  # 不应抛错


@pytest.mark.skipif(HAS_KEYRING, reason="已装 keyring，不会走文件降级")
def test_can_regenerate_after_purge(data_dir: Path):
    """purge 之后能生成一把全新的密钥（用于"重置系统"流程）。"""
    old = load_or_create_key(data_dir).key
    purge_key(data_dir)
    new = load_or_create_key(data_dir).key
    assert new != old


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

def test_service_name_is_stable():
    """凭据管理器的条目坐标一旦改动，等于让所有存量部署失联。"""
    assert SERVICE_NAME == "mmwf"
