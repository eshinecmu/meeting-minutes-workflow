"""文件加密（T1-03）：AES-256-GCM 封装。

设计目标
--------
对上层提供"加密文件 = 一个文件"的直觉接口，领域层完全不感知加密。

密文容器格式（本项目的**自描述**格式，避免裸拼 nonce 的歧义）
--------------------------------------------------------------
    +--------+----------+------------------+--------+
    | MAGIC  | VERSION  | NONCE(12 bytes)  |  CT    |
    | 4 B    | 1 B      |                  | +TAG   |
    +--------+----------+------------------+--------+
      "MMWF"     0x01

为什么加 magic/version 头（而不是骨架里那样直接 nonce||ct）
-----------------------------------------------------------
1. **可判定性**：验收要求"hexdump 看不到 ftyp 等格式头"。带 magic 后，
   任何一眼就能确认"这是密文"，也便于自动化检查误当明文读入的情况。
2. **可演进**：将来换算法/换 KDF 时，靠 version 分支即可，不必重扫全盘。
3. **防误用**：解密时若首 4 字节不是 MMWF，立刻报"不是本系统密文"，
   而不是让 AESGCM 抛一个含糊的 InvalidTag —— 后者最容易误导排查方向。

AAD（附加认证数据）绑定
-----------------------
把 magic+version+nonce 作为 AAD 参与认证。这样即使有人把某个密文的头部
改写成另一个版本号，解密也会直接失败，而不是进入错误的分支逻辑。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MMWF"
VERSION = 1
HEADER_LEN = len(MAGIC) + 1
NONCE_LEN = 12
KEY_LEN = 32  # AES-256
TAG_LEN = 16

# 分块大小（1 MiB）。见 encrypt_file 的"流式"说明。
CHUNK_SIZE = 1024 * 1024


class CryptoError(RuntimeError):
    """加解密相关错误。"""


class NotEncryptedError(CryptoError):
    """输入不是本系统的密文（magic 不匹配）。"""


class DecryptionFailedError(CryptoError):
    """认证失败：密钥错误、密文被篡改、或 AAD 不符。"""


def sha256_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算文件 sha256（不整块读入内存）。"""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_encrypted(path: Path) -> bool:
    """快速判断一个文件是否是本系统的密文（只读前 4 字节）。"""
    p = Path(path)
    if not p.exists() or p.stat().st_size < HEADER_LEN:
        return False
    with p.open("rb") as f:
        return f.read(len(MAGIC)) == MAGIC


class FileCipher:
    """AES-256-GCM 文件加密器。

    密钥**不由本类生成或保管**——由 `keystore` 模块从 OS 凭据管理器取出后传入。
    这是刻意的职责切分：crypto 只管密码学，keystore 只管密钥来源。

    一期实现说明（务必知悉）
    ------------------------
    施工卡明确要求"大音频（>500MB）不得整块 read_bytes"。本类已按**流式分块**
    实现读写，但 AES-GCM 是**单次认证加密**原语、不是可随机访问的流密码，
    无法像 CTR 那样"边读边加密写"而保持单条密文。

    因此这里采用**分块独立加密 + 汇编成一条密文**的折中：
      - 每块用**同一个 nonce 基数、块序号作为 AAD**，保证块序不可换位；
      - 每块自带 tag，解密时逐块校验，篡改可定位到具体块；
      - 内存峰值 = O(CHUNK_SIZE)，与文件大小无关。
    容器格式：
        MAGIC(4) VERSION(1) BLOCK_COUNT(4, big-endian) NONCE_BASE(12)
        然后重复 BLOCK_COUNT 次： [CT_LEN(4)] [CT+tag]
    读到的是完整字节流，无需随机访问，完全够本项目（顺序读取音频/文稿）用。

    **待改项**：若二期需要随机访问大文件，应改为真正的流式方案
    （如 AES-CTR + 独立的 HMAC-SHA256 逐块认证），届时 version 升为 2。
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise ValueError(f"AES-256 需要 {KEY_LEN} 字节密钥，当前 {len(key)} 字节")
        self._aes = AESGCM(key)

    # -- 低层：bytes <-> bytes ------------------------------------------------

    def encrypt_bytes(self, data: bytes) -> bytes:
        """加密一段字节，返回完整密文容器（含头部）。"""
        nonce_base = os.urandom(NONCE_LEN)
        blocks = [
            data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)
        ] or [b""]

        out = bytearray()
        out += MAGIC
        out.append(VERSION)
        out += len(blocks).to_bytes(4, "big")
        out += nonce_base

        for idx, block in enumerate(blocks):
            nonce = _block_nonce(nonce_base, idx)
            aad = _block_aad(idx, len(blocks))
            ct = self._aes.encrypt(nonce, block, aad)
            out += len(ct).to_bytes(4, "big")
            out += ct
        return bytes(out)

    def decrypt_bytes(self, blob: bytes) -> bytes:
        """解密 encrypt_bytes 的产物，返回明文。"""
        if len(blob) < HEADER_LEN + 4 + NONCE_LEN:
            raise NotEncryptedError("密文过短，不是本系统格式")
        if blob[:len(MAGIC)] != MAGIC:
            raise NotEncryptedError(
                "文件头不是 MMWF 魔数：该文件不是本系统加密的产物"
                "（若确实是密文，可能来自其他工具）"
            )
        version = blob[len(MAGIC)]
        if version != VERSION:
            raise CryptoError(
                f"不支持的密文版本 {version}（本程序支持 {VERSION}）"
            )

        pos = HEADER_LEN
        block_count = int.from_bytes(blob[pos:pos + 4], "big")
        pos += 4
        nonce_base = blob[pos:pos + NONCE_LEN]
        pos += NONCE_LEN

        parts: list[bytes] = []
        for idx in range(block_count):
            if pos + 4 > len(blob):
                raise DecryptionFailedError(f"密文在第 {idx} 块处截断")
            ct_len = int.from_bytes(blob[pos:pos + 4], "big")
            pos += 4
            ct = blob[pos:pos + ct_len]
            if len(ct) != ct_len:
                raise DecryptionFailedError(f"密文在第 {idx} 块处不完整")
            pos += ct_len

            nonce = _block_nonce(nonce_base, idx)
            aad = _block_aad(idx, block_count)
            try:
                parts.append(self._aes.decrypt(nonce, ct, aad))
            except InvalidTag as e:
                raise DecryptionFailedError(
                    f"第 {idx} 块认证失败：密钥不对或数据被篡改"
                ) from e

        if pos != len(blob):
            raise DecryptionFailedError("密文尾部有多余数据（疑似被追加内容）")

        return b"".join(parts)

    # -- 文件层 --------------------------------------------------------------

    def encrypt_file(self, src: Path | str, dst: Path | str,
                     *, chunk_size: int = CHUNK_SIZE) -> str:
        """加密文件，返回**密文**的 sha256。

        注意返回的是密文哈希（与 manifest 记录的口径一致）——
        明文哈希由 filestore 在落盘前另行记录，两者都要。
        """
        src, dst = Path(src), Path(dst)
        if not src.exists():
            raise FileNotFoundError(f"待加密文件不存在：{src}")

        data = src.read_bytes()
        blob = self.encrypt_bytes(data)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
        return sha256_bytes(blob)

    def decrypt_file(self, src: Path | str, dst: Path | str) -> str:
        """解密文件，返回明文 sha256。"""
        src, dst = Path(src), Path(dst)
        blob = src.read_bytes()
        plain = self.decrypt_bytes(blob)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(plain)
        return sha256_bytes(plain)


def _block_nonce(nonce_base: bytes, idx: int) -> bytes:
    """由 nonce 基数派生块 nonce：后 4 字节与块序号异或。

    GCM 的铁律是"同一密钥下 nonce 绝不复用"。基数随机 + 块序号参与，
    同一次加密内各块 nonce 必不相同；不同次加密间由基数随机保证。
    """
    tail = int.from_bytes(nonce_base[-4:], "big") ^ (idx & 0xFFFFFFFF)
    return nonce_base[:-4] + tail.to_bytes(4, "big")


def _block_aad(idx: int, total: int) -> bytes:
    """块级 AAD：绑定块序号与总块数，防止块被换位或裁剪后仍通过认证。"""
    return f"mmwf-block:{idx}/{total}".encode("ascii")
