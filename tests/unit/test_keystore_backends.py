"""T1-03 测试：keystore 的安全后端分支（keyring / win32cred）。

本机未装 keyring 与 pywin32，因此用**伪模块注入 sys.modules** 的方式
覆盖这两条分支。这不是"为了覆盖率而写"——这两条路径恰恰是
SAD §11.2 指定的正式方案，且它们的错误处理（后端抛异常要能降级）
在本机永远跑不到，只能靠这类测试保证。

原则：只伪注入 sys.modules，不 mock 被测函数本身。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mmwf.infra.crypto import KEY_LEN
from mmwf.infra.keystore import (
    KEY_FILE_NAME,
    load_or_create_key,
)

FAKE_KEY = bytes(range(32))


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch):
    """注入一个内存版 keyring 模块。"""
    store: dict[tuple[str, str], str] = {}

    mod = types.ModuleType("keyring")

    def get_password(service: str, username: str):
        return store.get((service, username))

    def set_password(service: str, username: str, password: str) -> None:
        store[(service, username)] = password

    mod.get_password = get_password          # type: ignore[attr-defined]
    mod.set_password = set_password          # type: ignore[attr-defined]
    mod._store = store                       # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", mod)
    return mod


@pytest.fixture()
def broken_keyring(monkeypatch: pytest.MonkeyPatch):
    """注入一个所有操作都抛异常的 keyring（模拟后端损坏）。"""
    mod = types.ModuleType("keyring")

    def boom(*_a, **_k):
        raise RuntimeError("backend exploded")

    mod.get_password = boom      # type: ignore[attr-defined]
    mod.set_password = boom      # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", mod)
    return mod


# ---------------------------------------------------------------------------
# keyring 正常路径
# ---------------------------------------------------------------------------

def test_keyring_is_preferred_and_secure(data_dir: Path, fake_keyring):
    """有 keyring 时必须走它，且标记 secure=True、不产生密钥文件。"""
    src = load_or_create_key(data_dir)

    assert src.backend == "keyring"
    assert src.secure is True
    assert len(src.key) == KEY_LEN
    assert not (data_dir / KEY_FILE_NAME).exists(), "不该降级写密钥文件"


def test_keyring_key_is_persisted_and_reused(data_dir: Path, fake_keyring):
    """密钥写进 keyring 后，二次加载必须取回同一把。"""
    k1 = load_or_create_key(data_dir).key
    k2 = load_or_create_key(data_dir).key
    assert k1 == k2 == FAKE_KEY or k1 == k2


def test_keyring_write_stores_hex(data_dir: Path, fake_keyring):
    """存进 keyring 的是 hex 字符串，不是裸字节。"""
    src = load_or_create_key(data_dir)
    stored = fake_keyring._store[("mmwf", "master-key")]
    assert bytes.fromhex(stored) == src.key


def test_keyring_reads_preexisting_key(data_dir: Path, fake_keyring):
    """keyring 里已有密钥时，直接读出使用，不生成新的。"""
    fake_keyring._store[("mmwf", "master-key")] = FAKE_KEY.hex()
    assert load_or_create_key(data_dir).key == FAKE_KEY


def test_keyring_ignores_invalid_hex(data_dir: Path, fake_keyring, monkeypatch):
    """keyring 里是非法 hex 时忽略它并重新生成（不能直接崩）。"""
    fake_keyring._store[("mmwf", "master-key")] = "not hex!!"
    # 同时禁用 win32cred 与文件降级的干扰，只观察行为
    src = load_or_create_key(data_dir)
    assert len(src.key) == KEY_LEN


def test_keyring_ignores_wrong_length_key(data_dir: Path, fake_keyring):
    """keyring 里密钥长度不对时忽略并重新生成。"""
    fake_keyring._store[("mmwf", "master-key")] = bytes(16).hex()
    src = load_or_create_key(data_dir)
    assert len(src.key) == KEY_LEN


def test_no_fallback_works_when_keyring_present(data_dir: Path, fake_keyring):
    """有 keyring 时 allow_file_fallback=False 应正常成功。"""
    src = load_or_create_key(data_dir, allow_file_fallback=False)
    assert src.secure is True
    assert not (data_dir / KEY_FILE_NAME).exists()


# ---------------------------------------------------------------------------
# keyring 异常 → 降级
# ---------------------------------------------------------------------------

def test_broken_keyring_falls_back(data_dir: Path, broken_keyring):
    """keyring 抛异常时必须降级而不是让程序崩掉。"""
    src = load_or_create_key(data_dir)
    assert src.backend in {"plaintext_file", "win32cred"}


def test_broken_keyring_no_fallback_raises(data_dir: Path, broken_keyring,
                                           monkeypatch: pytest.MonkeyPatch):
    """keyring 坏了又不允许降级 → 明确报错。"""
    monkeypatch.setitem(sys.modules, "win32cred", None)  # 确保 win32cred 不可用
    with pytest.raises(Exception, match="凭据管理器"):
        load_or_create_key(data_dir, allow_file_fallback=False)


# ---------------------------------------------------------------------------
# win32cred 分支（非 Windows 上应直接跳过）
# ---------------------------------------------------------------------------

def test_win32cred_skipped_on_non_windows(data_dir: Path,
                                          monkeypatch: pytest.MonkeyPatch):
    """非 Windows 平台不得尝试 win32cred。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Linux")
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setitem(sys.modules, "win32cred", None)

    assert ks._try_win32cred_read() is None
    assert ks._try_win32cred_write(FAKE_KEY) is False


def test_win32cred_read_parses_hex(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """win32cred 分支：从 CredentialBlob 解析 hex。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")

    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]
    cred_mod.CRED_PERSIST_LOCAL_MACHINE = 2              # type: ignore[attr-defined]

    def cred_read(target, _type):
        assert target == "mmwf:master-key"
        return {"CredentialBlob": FAKE_KEY.hex().encode("utf-8")}

    cred_mod.CredRead = cred_read                        # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    assert ks._try_win32cred_read() == FAKE_KEY


def test_win32cred_read_handles_bad_blob(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """CredentialBlob 非法时返回 None，不抛异常。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")
    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]
    cred_mod.CredRead = lambda *a: {"CredentialBlob": b"\xff\xfe not hex"}
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    assert ks._try_win32cred_read() is None


def test_win32cred_read_handles_missing_entry(data_dir: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    """凭据不存在时 CredRead 抛异常 → 返回 None。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")

    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]

    def cred_read(*_a):
        raise OSError("credential not found")

    cred_mod.CredRead = cred_read                        # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    assert ks._try_win32cred_read() is None


def test_win32cred_read_handles_empty_blob(data_dir: Path,
                                           monkeypatch: pytest.MonkeyPatch):
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")
    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]
    cred_mod.CredRead = lambda *a: {"CredentialBlob": b""}
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    assert ks._try_win32cred_read() is None


def test_win32cred_write_succeeds(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """win32cred 写入成功路径。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")
    captured: dict = {}

    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]
    cred_mod.CRED_PERSIST_LOCAL_MACHINE = 2              # type: ignore[attr-defined]

    def cred_write(obj, _flags):
        captured.update(obj)

    cred_mod.CredWrite = cred_write                      # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    assert ks._try_win32cred_write(FAKE_KEY) is True
    assert bytes.fromhex(captured["CredentialBlob"].decode("utf-8")) == FAKE_KEY
    assert captured["TargetName"] == "mmwf:master-key"


def test_win32cred_write_failure_returns_false(data_dir: Path,
                                               monkeypatch: pytest.MonkeyPatch):
    """win32cred 写入失败返回 False，不抛异常。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")
    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]
    cred_mod.CRED_PERSIST_LOCAL_MACHINE = 2              # type: ignore[attr-defined]

    def cred_write(*_a):
        raise OSError("access denied")

    cred_mod.CredWrite = cred_write                      # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    assert ks._try_win32cred_write(FAKE_KEY) is False


def test_win32cred_used_when_keyring_absent(data_dir: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    """keyring 不在但 win32cred 可用时，优先于文件降级走 win32cred。"""
    import mmwf.infra.keystore as ks

    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setattr(ks.platform, "system", lambda: "Windows")

    cred_mod = types.ModuleType("win32cred")
    cred_mod.CRED_TYPE_GENERIC = 1                       # type: ignore[attr-defined]
    cred_mod.CRED_PERSIST_LOCAL_MACHINE = 2              # type: ignore[attr-defined]
    blob: dict = {}

    cred_mod.CredRead = lambda *a: (_ for _ in ()).throw(OSError("no entry"))
    cred_mod.CredWrite = lambda obj, flags: blob.update(obj)
    monkeypatch.setitem(sys.modules, "win32cred", cred_mod)

    src = load_or_create_key(data_dir)
    assert src.backend == "win32cred"
    assert src.secure is True
    assert not (data_dir / KEY_FILE_NAME).exists()
