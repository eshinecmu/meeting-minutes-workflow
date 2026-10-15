"""T1-03 验收测试：加密原语（crypto.py）。

覆盖：加解密往返、密文不含明文特征、篡改检测、块边界、版本/魔数校验。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mmwf.infra.crypto import (
    HEADER_LEN,
    MAGIC,
    VERSION,
    CryptoError,
    DecryptionFailedError,
    FileCipher,
    NotEncryptedError,
    is_encrypted,
    sha256_bytes,
    sha256_file,
)

KEY_A = bytes(range(32))
KEY_B = bytes(reversed(range(32)))


@pytest.fixture()
def cipher() -> FileCipher:
    return FileCipher(KEY_A)


# ---------------------------------------------------------------------------
# 密钥校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_len", [0, 16, 24, 31, 33, 64])
def test_rejects_wrong_key_length(bad_len: int):
    """AES-256 只接受 32 字节密钥，其它长度必须报错而非静默降级。"""
    with pytest.raises(ValueError, match="32 字节"):
        FileCipher(os.urandom(bad_len))


def test_accepts_32_byte_key():
    FileCipher(os.urandom(32))


# ---------------------------------------------------------------------------
# 往返
# ---------------------------------------------------------------------------

def test_roundtrip_empty(cipher: FileCipher):
    """空内容也要能往返（边界：blocks 为空时补一个空块）。"""
    assert cipher.decrypt_bytes(cipher.encrypt_bytes(b"")) == b""


@pytest.mark.parametrize(
    "size",
    [1, 15, 16, 1024, 1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1],
)
def test_roundtrip_various_sizes(cipher: FileCipher, size: int):
    """覆盖块边界：正好 1MiB、1MiB±1 是最容易出 off-by-one 的位置。"""
    data = os.urandom(size)
    assert cipher.decrypt_bytes(cipher.encrypt_bytes(data)) == data


def test_roundtrip_unicode_text(cipher: FileCipher):
    """中文文本往返（本项目的主要载荷）。"""
    text = "会议纪要：研究部署第四季度重点工作，责任人为「暂无」。"
    assert cipher.decrypt_bytes(cipher.encrypt_bytes(text.encode("utf-8"))).decode(
        "utf-8"
    ) == text


def test_ciphertext_is_longer_than_plaintext(cipher: FileCipher):
    """密文必须比明文长（头部 + 每块 16 字节 tag）。"""
    data = b"x" * 100
    assert len(cipher.encrypt_bytes(data)) > len(data)


def test_nonce_differs_per_call(cipher: FileCipher):
    """同一明文两次加密必须得到不同密文（nonce 随机，不是确定性加密）。"""
    data = b"same content"
    assert cipher.encrypt_bytes(data) != cipher.encrypt_bytes(data)


# ---------------------------------------------------------------------------
# 密文特征：不可明文读出
# ---------------------------------------------------------------------------

def test_ciphertext_does_not_leak_plaintext(cipher: FileCipher):
    """密文里不得出现明文片段（验收标准：hexdump 看不到格式头）。"""
    fake_mp4_header = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"
    payload = fake_mp4_header + b"A" * 4096
    blob = cipher.encrypt_bytes(payload)

    assert b"ftyp" not in blob
    assert b"isom" not in blob
    assert payload not in blob


def test_ciphertext_starts_with_magic(cipher: FileCipher):
    """密文以 MMWF 魔数开头，便于识别与自动化判定。"""
    blob = cipher.encrypt_bytes(b"hello")
    assert blob[: len(MAGIC)] == MAGIC
    assert blob[len(MAGIC)] == VERSION


def test_all_zero_plaintext_does_not_produce_all_zero_ciphertext(cipher: FileCipher):
    """全零明文不能产出全零密文（防止"空块"看起来像未加密）。"""
    blob = cipher.encrypt_bytes(b"\x00" * 4096)
    body = blob[HEADER_LEN + 4 + 12:]
    assert any(b != 0 for b in body)


# ---------------------------------------------------------------------------
# 篡改检测
# ---------------------------------------------------------------------------

def test_tampered_body_is_detected(cipher: FileCipher):
    """翻转密文正文任意一位，必须认证失败。"""
    blob = bytearray(cipher.encrypt_bytes(b"important meeting minutes" * 100))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionFailedError, match="认证失败"):
        cipher.decrypt_bytes(bytes(blob))


def test_tampered_header_is_detected(cipher: FileCipher):
    """改写 nonce 一个字节，必须失败（AAD 绑定头部）。"""
    blob = bytearray(cipher.encrypt_bytes(b"payload"))
    nonce_pos = HEADER_LEN + 4  # magic(4)+ver(1)+blockcount(4)
    blob[nonce_pos] ^= 0xFF
    with pytest.raises(DecryptionFailedError):
        cipher.decrypt_bytes(bytes(blob))


def test_wrong_key_fails(cipher: FileCipher):
    """换一把密钥解密，必须失败而不是返回垃圾数据。"""
    blob = cipher.encrypt_bytes(b"secret")
    with pytest.raises(DecryptionFailedError):
        FileCipher(KEY_B).decrypt_bytes(blob)


def test_block_reorder_is_detected(cipher: FileCipher):
    """块换位必须被发现（AAD 绑定块序号）。

    构造两个块，交换它们在密文中的顺序，然后用正确密钥解密——必须失败。
    """
    one_mib = 1024 * 1024
    data = b"A" * one_mib + b"B" * one_mib
    blob = cipher.encrypt_bytes(data)

    # 解析出两块的位置
    pos = HEADER_LEN + 4 + 12
    ranges = []
    for _ in range(2):
        ct_len = int.from_bytes(blob[pos:pos + 4], "big")
        ranges.append((pos, pos + 4 + ct_len))
        pos += 4 + ct_len

    (s0, e0), (s1, e1) = ranges
    tampered = blob[:s0] + blob[s1:e1] + blob[s0:e0] + blob[e1:]
    with pytest.raises(DecryptionFailedError):
        cipher.decrypt_bytes(tampered)


def test_truncated_ciphertext_is_detected(cipher: FileCipher):
    """截断密文必须报错。"""
    blob = cipher.encrypt_bytes(b"x" * 100)
    with pytest.raises((DecryptionFailedError, NotEncryptedError)):
        cipher.decrypt_bytes(blob[:-5])


def test_appended_garbage_is_detected(cipher: FileCipher):
    """尾部追加数据必须被发现（防止在密文后悄悄夹带内容）。"""
    blob = cipher.encrypt_bytes(b"x" * 100) + b"extra"
    with pytest.raises(DecryptionFailedError, match="多余数据"):
        cipher.decrypt_bytes(blob)


# ---------------------------------------------------------------------------
# 格式校验
# ---------------------------------------------------------------------------

def test_plain_data_raises_not_encrypted(cipher: FileCipher):
    """喂普通文本进去，要报"不是本系统密文"，而不是含糊的认证失败。"""
    with pytest.raises(NotEncryptedError, match="MMWF"):
        cipher.decrypt_bytes(b"this is just plain text, not encrypted at all")


def test_too_short_blob_raises(cipher: FileCipher):
    with pytest.raises(NotEncryptedError, match="过短"):
        cipher.decrypt_bytes(b"MM")


def test_unsupported_version_raises(cipher: FileCipher):
    """版本号不匹配要报明确错误（便于将来格式演进）。"""
    blob = bytearray(cipher.encrypt_bytes(b"data"))
    blob[len(MAGIC)] = 99
    with pytest.raises(CryptoError, match="版本"):
        cipher.decrypt_bytes(bytes(blob))


# ---------------------------------------------------------------------------
# 文件层
# ---------------------------------------------------------------------------

def test_encrypt_file_returns_ciphertext_digest(cipher: FileCipher, tmp_path: Path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"A" * 5000)

    digest = cipher.encrypt_file(src, tmp_path / "out.enc")

    assert digest == sha256_file(tmp_path / "out.enc")
    assert b"ftyp" not in (tmp_path / "out.enc").read_bytes()


def test_decrypt_file_roundtrip(cipher: FileCipher, tmp_path: Path):
    src = tmp_path / "orig.bin"
    payload = os.urandom(300_000)
    src.write_bytes(payload)

    cipher.encrypt_file(src, tmp_path / "enc.bin")
    plain_digest = cipher.decrypt_file(tmp_path / "enc.bin", tmp_path / "back.bin")

    assert (tmp_path / "back.bin").read_bytes() == payload
    assert plain_digest == sha256_bytes(payload)
    assert plain_digest == sha256_file(src)


def test_encrypt_file_missing_src_raises(cipher: FileCipher, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        cipher.encrypt_file(tmp_path / "nope.bin", tmp_path / "out.bin")


def test_encrypt_file_creates_parent_dirs(cipher: FileCipher, tmp_path: Path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 10)
    cipher.encrypt_file(src, tmp_path / "deep" / "nested" / "out.enc")
    assert (tmp_path / "deep" / "nested" / "out.enc").exists()


# ---------------------------------------------------------------------------
# 判定与哈希工具
# ---------------------------------------------------------------------------

def test_is_encrypted_true_for_ciphertext(cipher: FileCipher, tmp_path: Path):
    p = tmp_path / "c.bin"
    p.write_bytes(cipher.encrypt_bytes(b"hello"))
    assert is_encrypted(p) is True


def test_is_encrypted_false_for_plaintext(tmp_path: Path):
    p = tmp_path / "p.bin"
    p.write_bytes(b"plain text here")
    assert is_encrypted(p) is False


def test_is_encrypted_false_for_missing_file(tmp_path: Path):
    assert is_encrypted(tmp_path / "nope.bin") is False


def test_is_encrypted_false_for_tiny_file(tmp_path: Path):
    p = tmp_path / "tiny.bin"
    p.write_bytes(b"MM")
    assert is_encrypted(p) is False


def test_sha256_file_matches_sha256_bytes(tmp_path: Path):
    data = os.urandom(200_000)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    assert sha256_file(p) == sha256_bytes(data)


def test_sha256_file_streams_consistently(tmp_path: Path):
    """不同分块大小必须得到同一个哈希（流式读取正确性）。"""
    data = os.urandom(50_000)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    assert sha256_file(p, chunk_size=1) == sha256_file(p, chunk_size=8192)
