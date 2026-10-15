"""T1-03 验收测试：文件仓储（filestore.py）。

对应施工卡 T1-03 的四条验收标准：
  1. 投递音频后 data/store/{mid}/audio/ 下文件**不是明文**（看不到 ftyp 等格式头）
  2. 通过 filestore 读回的字节与原文件 sha256 一致
  3. manifest.json 条目数与实际产物数一致
  4. 删除 manifest 后能通过扫描目录重建
另加：临时明文用完即删且不在系统 /tmp、目录穿越防护、export 明文。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mmwf.infra.crypto import FileCipher, is_encrypted, sha256_bytes
from mmwf.infra.filestore import (
    MANIFEST_NAME,
    FileStore,
    FileStoreError,
    make_filestore,
)

MEETING_ID = "aaaa1111-2222-4333-8444-555566667777"
KEY = bytes(range(32))

# 伪造一个带真实 MP4/m4a 格式头的音频（验收要求"看不到 ftyp"）
FAKE_M4A = (
    b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00M4A mp42isom"
    + os.urandom(300_000)
)


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def store(data_root: Path) -> FileStore:
    return FileStore(MEETING_ID, data_root, cipher=FileCipher(KEY))


# ---------------------------------------------------------------------------
# 标准 1：落盘不是明文
# ---------------------------------------------------------------------------

def test_audio_on_disk_is_not_plaintext(store: FileStore, data_root: Path):
    """标准 1：音频落盘后，磁盘文件看不到 ftyp 格式头。"""
    store.write("audio/original.m4a", FAKE_M4A, stage="audio")

    disk = data_root / "store" / MEETING_ID / "audio" / "original.m4a"
    assert disk.exists()

    blob = disk.read_bytes()
    assert b"ftyp" not in blob, "磁盘文件里仍能读到 ftyp —— 未加密！"
    assert b"M4A " not in blob
    assert b"isom" not in blob
    assert is_encrypted(disk), "磁盘文件不带 MMWF 魔数"


def test_transcript_and_minutes_encrypted_on_disk(store: FileStore, data_root: Path):
    """转写稿与纪要同样加密（SAD §11.2）。"""
    store.write_json("transcript/segments.json", [{"text": "秘密内容"}] * 50)
    store.write_text("minutes/final.md", "# 会议纪要\n研究部署重点工作")

    base = data_root / "store" / MEETING_ID
    for rel in ["transcript/segments.json", "minutes/final.md"]:
        blob = (base / rel).read_bytes()
        assert b"\xe7\xa7\x98\xe5\xaf\x86" not in blob  # "秘密" 的 utf-8
        assert b"\xe4\xbc\x9a\xe8\xae\xae" not in blob  # "会议" 的 utf-8
        assert is_encrypted(base / rel)


def test_export_area_stays_plaintext(store: FileStore, data_root: Path):
    """导出目录刻意保持明文（SAD §11.2：导出件要交付给人）。"""
    docx_bytes = b"PK\x03\x04" + b"fake docx content" * 100
    store.write("export/会议纪要.docx", docx_bytes, stage="export")

    disk = data_root / "store" / MEETING_ID / "export" / "会议纪要.docx"
    assert disk.read_bytes() == docx_bytes, "导出件被加密了 —— 用户会打不开文件"
    assert not is_encrypted(disk)


# ---------------------------------------------------------------------------
# 标准 2：读回字节与原文件 sha256 一致
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_original_sha256(store: FileStore, tmp_path: Path):
    """标准 2：投递时写的明文哈希 == 读回后的哈希 == 原文件哈希。"""
    src = tmp_path / "input.m4a"
    src.write_bytes(FAKE_M4A)
    original = sha256_bytes(src.read_bytes())

    stored = store.write("audio/original.m4a", src.read_bytes(), stage="audio")
    assert stored.plain_sha256 == original

    got = store.read("audio/original.m4a")
    assert sha256_bytes(got) == original

    # 与源文件逐字节一致
    assert got == src.read_bytes()


def test_roundtrip_json_preserves_chinese(store: FileStore):
    obj = {"议题": "研究部署四季度工作", "责任人": "暂无", "items": [1, 2, 3]}
    store.write_json("minutes/structured.json", obj)
    assert store.read_json("minutes/structured.json") == obj


def test_roundtrip_text_uses_utf8(store: FileStore):
    text = "# 会议纪要\n\n一、研究部署重点工作（责任人：暂无）\n"
    store.write_text("record/v1.md", text)
    assert store.read_text("record/v1.md") == text


def test_roundtrip_large_file(store: FileStore, tmp_path: Path):
    """1.5MB（跨两个 1MiB 块）也要完整往返。"""
    payload = os.urandom(1_500_000)
    store.write("audio/big.wav", payload, stage="audio")
    assert store.read("audio/big.wav") == payload


def test_stored_sha256_is_ciphertext_digest(store: FileStore, data_root: Path):
    """manifest 里记的 sha256 是**磁盘密文**哈希，且与磁盘实际一致。"""
    stored = store.write("audio/a.m4a", b"x" * 1000, stage="audio")
    disk_digest = sha256_bytes(
        (data_root / "store" / MEETING_ID / "audio" / "a.m4a").read_bytes()
    )
    assert stored.sha256 == disk_digest
    assert stored.sha256 != stored.plain_sha256


# ---------------------------------------------------------------------------
# 标准 3：manifest 条目数一致
# ---------------------------------------------------------------------------

def test_manifest_entry_count_matches_files(store: FileStore):
    """标准 3：manifest 条目数 == 实际产物数。"""
    store.write("audio/original.m4a", b"a" * 100, stage="audio")
    store.write("audio/chunks/chunk_0001.wav", b"b" * 100, stage="audio")
    store.write("transcript/segments.json", b"[]", stage="transcript")
    store.write("minutes/final.md", b"# x", stage="minutes")
    store.write("export/out.docx", b"c" * 100, stage="export")

    entries = store.manifest()
    assert len(entries) == 5

    # 数一遍磁盘上的产物文件（排除 manifest 自身与 .tmp）
    real = [
        p for p in (store.store_root).rglob("*")
        if p.is_file()
        and p.name != MANIFEST_NAME
        and p.suffix != ".tmp"
        and ".tmp" not in p.relative_to(store.store_root).parts
    ]
    assert len(real) == len(entries) == 5


def test_manifest_has_required_four_fields(store: FileStore):
    """每条记录必须含 SAD §8.2 规定的四要素：path/sha256/created_at/stage。"""
    store.write("audio/a.m4a", b"x", stage="audio")
    e = store.manifest()[0]

    assert e.path == "audio/a.m4a"
    assert len(e.sha256) == 64
    assert e.created_at.endswith("Z")
    assert e.stage == "audio"


def test_manifest_verify_reports_no_problems(store: FileStore):
    store.write("audio/a.m4a", b"x" * 500, stage="audio")
    store.write("minutes/f.md", b"# m", stage="minutes")
    assert store.verify_manifest() == []


def test_manifest_same_path_overwrites_not_duplicates(store: FileStore):
    """同一路径重复落盘：manifest 以最后一次为准，不产生重复条目。"""
    store.write("record/v1.md", b"first", stage="record")
    store.write("record/v1.md", b"second", stage="record")

    entries = store.manifest()
    assert len(entries) == 1
    assert store.read("record/v1.md") == b"second"


def test_manifest_persisted_as_readable_json(store: FileStore):
    """manifest 本身是明文可读 JSON（它是审计依据，加密是自找麻烦）。"""
    store.write("audio/a.m4a", b"x", stage="audio")

    raw = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert raw["meeting_id"] == MEETING_ID
    assert len(raw["entries"]) == 1
    assert raw["entries"][0]["path"] == "audio/a.m4a"


def test_manifest_reflects_delete(store: FileStore):
    store.write("audio/a.m4a", b"x", stage="audio")
    store.write("audio/b.m4a", b"y", stage="audio")
    store.delete("audio/a.m4a")

    paths = {e.path for e in store.manifest()}
    assert paths == {"audio/b.m4a"}
    assert not store.exists("audio/a.m4a")


# ---------------------------------------------------------------------------
# 标准 4：删 manifest 后可重建
# ---------------------------------------------------------------------------

def test_rebuild_manifest_after_deletion(store: FileStore):
    """标准 4：删除 manifest 后，扫描目录能重建出完整索引。"""
    store.write("audio/original.m4a", b"a" * 2000, stage="audio")
    store.write("transcript/raw.json", b"{}", stage="transcript")
    store.write("minutes/final.md", "# 纪要".encode("utf-8"), stage="minutes")
    before = {e.path for e in store.manifest()}

    store.manifest_path.unlink()
    assert store.manifest() == []

    rebuilt = store.rebuild_manifest()
    after = {e.path for e in rebuilt}

    assert after == before
    assert len(rebuilt) == 3


def test_rebuilt_manifest_is_then_consistent(store: FileStore):
    """重建后的 manifest 能通过自校验。"""
    store.write("audio/a.m4a", b"x" * 500, stage="audio")
    store.write("export/o.docx", b"y" * 500, stage="export")
    store.manifest_path.unlink()

    store.rebuild_manifest()
    assert store.verify_manifest() == []


def test_rebuilt_marks_encrypted_flag_correctly(store: FileStore):
    """重建时按魔数正确标记 encrypted（密文 True、导出明文 False）。"""
    store.write("audio/a.m4a", b"x" * 500, stage="audio")
    store.write("export/o.docx", b"y" * 500, stage="export")
    store.manifest_path.unlink()

    by_path = {e.path: e for e in store.rebuild_manifest()}
    assert by_path["audio/a.m4a"].encrypted is True
    assert by_path["export/o.docx"].encrypted is False


def test_rebuilt_ciphertext_entry_has_no_plain_digest(store: FileStore):
    """重建时密文的明文哈希无法恢复 —— 必须诚实留空，不得编造。"""
    store.write("audio/a.m4a", b"x" * 500, stage="audio")
    store.manifest_path.unlink()

    e = store.rebuild_manifest()[0]
    assert e.encrypted is True
    assert e.plain_sha256 is None


def test_rebuilt_plaintext_entry_keeps_digest(store: FileStore):
    """明文文件的 sha256 与 plain_sha256 相同。"""
    store.write("export/o.docx", b"y" * 500, stage="export")
    store.manifest_path.unlink()

    e = store.rebuild_manifest()[0]
    assert e.encrypted is False
    assert e.plain_sha256 == e.sha256


def test_rebuild_on_empty_store(store: FileStore):
    """没有产物的会议，重建得到空列表而非报错。"""
    assert store.rebuild_manifest() == []


def test_rebuild_skips_tmp_dir(store: FileStore):
    """重建时必须跳过 .tmp 临时目录（否则会把明文临时文件当产物登记）。"""
    store.write("audio/a.m4a", b"x", stage="audio")
    tmpdir = store.store_root / ".tmp" / "plain-abc"
    tmpdir.mkdir(parents=True)
    (tmpdir / "ghost.wav").write_bytes(b"temporary plaintext")

    paths = {e.path for e in store.rebuild_manifest()}
    assert paths == {"audio/a.m4a"}


def test_rebuild_overwrites_broken_manifest(store: FileStore):
    """manifest 内容损坏时，rebuild 能覆盖修复。"""
    store.write("audio/a.m4a", b"x" * 300, stage="audio")
    store.manifest_path.write_text("{ this is not json", encoding="utf-8")

    # 读取会抛错
    with pytest.raises(FileStoreError, match="损坏"):
        store.manifest()

    # 重建后恢复正常
    assert store.rebuild_manifest() != []
    assert store.verify_manifest() == []


# ---------------------------------------------------------------------------
# 临时明文（给 ffmpeg 用）
# ---------------------------------------------------------------------------

def test_plaintext_tempfile_is_decrypted(store: FileStore):
    """临时明文文件内容 == 原始明文。"""
    payload = b"realaudio" * 1000
    store.write("audio/a.m4a", payload, stage="audio")

    with store.plaintext("audio/a.m4a") as p:
        assert p.exists()
        assert p.read_bytes() == payload


def test_plaintext_tempfile_deleted_after_use(store: FileStore):
    """施工卡硬性要求：临时明文用完立即删除。"""
    store.write("audio/a.m4a", b"secret" * 100, stage="audio")

    with store.plaintext("audio/a.m4a") as p:
        captured_dir = p.parent
        assert captured_dir.exists()
    assert not captured_dir.exists(), "临时明文目录未被清理！"


def test_plaintext_tempfile_deleted_on_exception(store: FileStore):
    """即使上下文内抛异常，临时明文也必须删除。"""
    store.write("audio/a.m4a", b"secret" * 100, stage="audio")

    captured = None
    with pytest.raises(RuntimeError):
        with store.plaintext("audio/a.m4a") as p:
            captured = p.parent
            raise RuntimeError("模拟 ffmpeg 失败")

    assert captured is not None
    assert not captured.exists()


def test_plaintext_tempfile_not_in_system_tmp(store: FileStore):
    """施工卡要求：临时明文放受控目录，不放系统 /tmp。

    注意：pytest 的 tmp_path 本身就位于系统临时目录之下，所以不能简单断言
    "不在系统 temp 之下"。真正要验证的是：**没有直接用 tempfile.mkdtemp() 的
    默认行为**（即不传 dir 参数），而是落在了 store 内部的 .tmp 目录。
    做法：确认临时文件位于 <store_root>/.tmp 之下。
    """
    store.write("audio/a.m4a", b"secret" * 100, stage="audio")

    with store.plaintext("audio/a.m4a") as p:
        controlled_tmp = (store.store_root / ".tmp").resolve()
        assert controlled_tmp in p.resolve().parents, (
            f"临时明文未落在受控目录 {controlled_tmp}：{p}"
        )


def test_plaintext_works_for_plaintext_area(store: FileStore):
    """export 区的文件也能得到临时路径（内容原样）。"""
    store.write("export/o.docx", b"PK\x03\x04docx", stage="export")
    with store.plaintext("export/o.docx") as p:
        assert p.read_bytes() == b"PK\x03\x04docx"


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

def test_rejects_path_traversal(store: FileStore):
    """rel_path 早晚来自 API 参数，必须防目录穿越。"""
    for bad in ["../escape.txt", "audio/../../escape.txt", ".."]:
        with pytest.raises(FileStoreError):
            store.resolve(bad)


def test_rejects_absolute_path(store: FileStore):
    with pytest.raises(FileStoreError, match="相对路径"):
        store.resolve("/etc/passwd")


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",           # POSIX 根路径
        "\\Windows\\System32",   # Windows 根路径（反斜杠）
        "/Windows/System32",     # Windows 根路径（正斜杠）
        "C:/Windows/System32",   # 带盘符
        r"C:\Windows\System32",  # 带盘符（反斜杠）
        "c:x",                   # 盘符相对路径
        r"\\server\share\x",     # UNC 路径（反斜杠）
        "//server/share/x",      # UNC 路径（正斜杠）
    ],
)
def test_rejects_rooted_paths_on_any_platform(store: FileStore, bad: str):
    """宿主平台的 Path 语义不足以识别另一平台的根路径与盘符。

    本用例在所有平台钉住根路径、盘符相对路径与 UNC 路径的拦截行为。
    """
    with pytest.raises(FileStoreError, match="rel_path 必须是相对路径"):
        store.resolve(bad)


def test_rejects_path_escaping_via_normalization(store: FileStore):
    """即使不含 '..'，解析后越出 store 目录也必须被拦。"""
    with pytest.raises(FileStoreError):
        store.resolve("audio/" + "../" * 5 + "escape.txt")


def test_rejects_bad_meeting_id(data_root: Path):
    for bad in ["", "a/b", "a\\b", ".tmp"]:
        with pytest.raises(ValueError):
            FileStore(bad, data_root, cipher=FileCipher(KEY))


def test_rejects_unknown_stage(store: FileStore):
    with pytest.raises(FileStoreError, match="未知产物类别"):
        store.area_dir("nonsense")


def test_read_missing_file_raises(store: FileStore):
    with pytest.raises(FileStoreError, match="不存在"):
        store.read("audio/nope.m4a")


# ---------------------------------------------------------------------------
# 加密开关与自动化入口
# ---------------------------------------------------------------------------

def test_encrypt_disabled_writes_plaintext(data_root: Path):
    """加密可关闭（便于调试与无密钥环境）。"""
    s = FileStore(MEETING_ID, data_root, encrypt_enabled=False)
    s.write("audio/a.m4a", b"SEKRET" * 100, stage="audio")

    disk = data_root / "store" / MEETING_ID / "audio" / "a.m4a"
    assert b"SEKRET" in disk.read_bytes()
    assert s.read("audio/a.m4a") == b"SEKRET" * 100


def test_make_filestore_auto_loads_key(data_root: Path):
    """make_filestore 自动取密钥，返回值可用（无 keyring 时走文件降级）。"""
    s = make_filestore(MEETING_ID, data_root)
    s.write("audio/a.m4a", b"payload" * 100, stage="audio")
    assert s.read("audio/a.m4a") == b"payload" * 100
    assert is_encrypted(s.resolve("audio/a.m4a"))


def test_make_filestore_key_is_stable_across_instances(data_root: Path):
    """密钥必须持久，否则重启后旧数据全废。"""
    s1 = make_filestore(MEETING_ID, data_root)
    s1.write("audio/a.m4a", b"payload" * 100, stage="audio")

    s2 = make_filestore(MEETING_ID, data_root)
    assert s2.read("audio/a.m4a") == b"payload" * 100


def test_auto_cipher_store_can_read_explicit_cipher_store(data_root: Path):
    """显式传 cipher 与自动取密钥两条路径必须互通。"""
    s_auto = make_filestore(MEETING_ID, data_root)
    s_auto.write("minutes/f.md", b"# shared", stage="minutes")

    # 用同一 data_root 自动取的密钥应能解开
    s2 = make_filestore(MEETING_ID, data_root)
    assert s2.read_text("minutes/f.md") == "# shared"


# ---------------------------------------------------------------------------
# 目录约定
# ---------------------------------------------------------------------------

def test_follows_sad_directory_convention(store: FileStore, data_root: Path):
    """路径必须是 data/store/{meeting_id}/{area}/...（SAD §8.2）。"""
    store.write("audio/original.m4a", b"x", stage="audio")
    store.write("audio/chunks/chunk_0001.wav", b"y", stage="audio")
    store.write("transcript/raw.json", b"{}", stage="transcript")
    store.write("record/v1.md", b"# r", stage="record")
    store.write("minutes/draft_v1.md", b"# d", stage="minutes")
    store.write("export/out.docx", b"z", stage="export")

    base = data_root / "store" / MEETING_ID
    for rel in [
        "audio/original.m4a", "audio/chunks/chunk_0001.wav",
        "transcript/raw.json", "record/v1.md",
        "minutes/draft_v1.md", "export/out.docx",
    ]:
        assert (base / rel).exists(), f"未按约定落盘：{rel}"


def test_store_root_path(data_root: Path):
    s = FileStore(MEETING_ID, data_root, cipher=FileCipher(KEY))
    assert s.store_root == data_root / "store" / MEETING_ID


def test_two_meetings_are_isolated(data_root: Path):
    """不同会议的数据互不干扰。"""
    a = FileStore("meeting-a", data_root, cipher=FileCipher(KEY))
    b = FileStore("meeting-b", data_root, cipher=FileCipher(KEY))

    a.write("audio/x.m4a", b"AAA" * 100, stage="audio")
    b.write("audio/x.m4a", b"BBB" * 100, stage="audio")

    assert a.read("audio/x.m4a") == b"AAA" * 100
    assert b.read("audio/x.m4a") == b"BBB" * 100
    assert len(a.manifest()) == 1 and len(b.manifest()) == 1
