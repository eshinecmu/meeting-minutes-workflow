"""T1-03 验收脚本（人可读版）。

施工卡 T1-03 的四条验收标准，逐条跑一遍并打印结论。
用法：python tools/accept_t1_03.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PASS = "✓"
FAIL = "✗"

MEETING_ID = "b7c1e2a0-3f4d-4e5b-8a6c-9d0e1f2a3b4c"

# 由 main() 填入实际临时根目录，供 _sub() 使用
_TMP_BASE: list[Path] = [Path(".")]

# 伪造带真实 m4a/MP4 格式头的音频
FAKE_M4A = (
    b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00M4A mp42isom"
    + os.urandom(400_000)
)


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def _hexdump_scan(blob: bytes, needles: list[bytes]) -> list[tuple[bytes, int]]:
    """在字节流里找"格式头"特征，返回 [(特征, 首次出现位置)]。"""
    hits = []
    for n in needles:
        idx = blob.find(n)
        if idx != -1:
            hits.append((n, idx))
    return hits


# ---------------------------------------------------------------------------
# 标准 1：落盘不是明文
# ---------------------------------------------------------------------------

def check_standard_1(tmp: Path) -> bool:
    """标准 1：投递音频后，store 下文件不是明文。"""
    banner("标准 1：音频落盘非明文（hexdump 看不到 ftyp 等格式头）")

    from mmwf.infra.crypto import is_encrypted
    from mmwf.infra.filestore import make_filestore

    store = make_filestore(MEETING_ID, tmp)
    store.write("audio/original.m4a", FAKE_M4A, stage="audio")

    disk = store.resolve("audio/original.m4a")
    blob = disk.read_bytes()

    print(f"  原文明文长度：{len(FAKE_M4A):,} 字节")
    print(f"  磁盘密文长度：{len(blob):,} 字节")
    print(f"  磁盘文件路径：{disk}")
    print(f"  前 16 字节（hexdump）：{' '.join(f'{b:02x}' for b in blob[:16])}")

    markers = [b"ftyp", b"M4A ", b"isom", b"mp42"]
    hits = _hexdump_scan(blob, markers)

    ok = True
    if hits:
        for n, idx in hits:
            print(f"  {FAIL} 在偏移 {idx} 处发现明文字符串 {n!r} —— 未加密！")
        ok = False
    else:
        print(f"  {PASS} 未检出任何格式头特征 {[m.decode() for m in markers]}")

    enc = is_encrypted(disk)
    ok &= enc
    print(f"  {PASS if enc else FAIL} 密文带 MMWF 魔数（可被自动识别为密文）")

    # 顺带验证转写稿与纪要也加密了
    store.write_json("transcript/segments.json", [{"text": "涉密内容"}])
    store.write_text("minutes/final.md", "# 会议纪要\n研究部署重点工作")
    for rel in ["transcript/segments.json", "minutes/final.md"]:
        b = store.resolve(rel).read_bytes()
        good = is_encrypted(store.resolve(rel))
        ok &= good
        print(f"  {PASS if good else FAIL} {rel} 已加密（{len(b):,} 字节）")

    return ok


# ---------------------------------------------------------------------------
# 标准 2：读回字节与原文件 sha256 一致
# ---------------------------------------------------------------------------

def check_standard_2(tmp: Path) -> bool:
    """标准 2：filestore 读回字节与原文件 sha256 一致。"""
    banner("标准 2：读回字节与原文件 sha256 一致（透明解密）")

    from mmwf.infra.crypto import sha256_bytes
    from mmwf.infra.filestore import make_filestore

    src = tmp / "input.m4a"
    src.write_bytes(FAKE_M4A)
    original = sha256_bytes(src.read_bytes())

    store = make_filestore(MEETING_ID, tmp)
    stored = store.write("audio/original.m4a", src.read_bytes(), stage="audio")
    got = store.read("audio/original.m4a")
    back = sha256_bytes(got)

    print(f"  原文件 sha256    ：{original}")
    print(f"  manifest 明文哈希：{stored.plain_sha256}")
    print(f"  读回字节 sha256  ：{back}")

    ok = original == stored.plain_sha256 == back and got == src.read_bytes()
    print(f"  {PASS if ok else FAIL} 三方哈希一致，且逐字节相同")

    # 边界：空文件、跨块大文件
    store.write("audio/empty.wav", b"", stage="audio")
    empty_ok = store.read("audio/empty.wav") == b""
    print(f"  {PASS if empty_ok else FAIL} 空文件往返正常")

    big = os.urandom(1_500_000)
    store.write("audio/big.wav", big, stage="audio")
    big_ok = store.read("audio/big.wav") == big
    ok &= empty_ok and big_ok
    print(f"  {PASS if big_ok else FAIL} 1.5MB（跨块）往返正常")

    return ok


# ---------------------------------------------------------------------------
# 标准 3：manifest 条目数一致
# ---------------------------------------------------------------------------

def check_standard_3(tmp: Path) -> bool:
    """标准 3：manifest 条目数与实际产物数一致。"""
    banner("标准 3：manifest 条目数 == 实际产物数")

    from mmwf.infra.filestore import MANIFEST_NAME, make_filestore

    store = make_filestore(MEETING_ID, tmp)
    store.write("audio/original.m4a", b"a" * 1000, stage="audio")
    store.write("audio/chunks/chunk_0001.wav", b"b" * 1000, stage="audio")
    store.write("transcript/raw.json", b"{}", stage="transcript")
    store.write("record/v1.md", "# 记录".encode(), stage="record")
    store.write("minutes/final.md", "# 纪要".encode(), stage="minutes")
    store.write("export/out.docx", b"PK\x03\x04" + b"x" * 100, stage="export")

    entries = store.manifest()
    real = [
        p for p in store.store_root.rglob("*")
        if p.is_file()
        and p.name != MANIFEST_NAME
        and p.suffix != ".tmp"
        and ".tmp" not in p.relative_to(store.store_root).parts
    ]

    print(f"  manifest 路径：{store.manifest_path}")
    print(f"  manifest 条目数：{len(entries)}")
    print(f"  磁盘产物文件数：{len(real)}")
    for e in entries:
        print(f"    · {e.path:34} encrypted={e.encrypted!s:5} "
              f"sha256={e.sha256[:12]}…")

    ok = len(entries) == len(real) == 6
    print(f"  {PASS if ok else FAIL} 条目数一致（6 条）")

    problems = store.verify_manifest()
    ok &= not problems
    print(f"  {PASS if not problems else FAIL} 自校验通过"
          + (f"：{problems}" if problems else ""))

    return ok


# ---------------------------------------------------------------------------
# 标准 4：删 manifest 后可重建
# ---------------------------------------------------------------------------

def check_standard_4(tmp: Path) -> bool:
    """标准 4：删除 manifest 后能扫描目录重建。"""
    banner("标准 4：删除 manifest 后可扫描重建（容错）")

    from mmwf.infra.filestore import make_filestore

    store = make_filestore(MEETING_ID, tmp)
    store.write("audio/original.m4a", b"a" * 2000, stage="audio")
    store.write("transcript/segments.json", b"[]", stage="transcript")
    store.write("minutes/final.md", "# 纪要".encode(), stage="minutes")
    before = {e.path: e.sha256 for e in store.manifest()}
    print(f"  重建前：manifest {len(before)} 条")

    store.manifest_path.unlink()
    after_delete = store.manifest()
    print(f"  删除 manifest 后读取：{len(after_delete)} 条（预期 0，容错不报错）")

    rebuilt = store.rebuild_manifest()
    after = {e.path: e.sha256 for e in rebuilt}
    print(f"  重建后：{len(after)} 条")

    ok = len(after_delete) == 0 and after == before
    for path in sorted(before):
        match = after.get(path) == before[path]
        ok &= match
        print(f"    {PASS if match else FAIL} {path} 哈希一致")

    problems = store.verify_manifest()
    ok &= not problems
    print(f"  {PASS if not ok or not problems else FAIL} 重建后自校验"
          + (f"：{problems}" if problems else " 通过"))

    # 诚实标注：密文重建后 plain_sha256 必须留空，不得编造
    by_path = {e.path: e for e in rebuilt}
    honest = by_path["audio/original.m4a"].plain_sha256 is None
    ok &= honest
    print(f"  {PASS if honest else FAIL} 密文重建时 plain_sha256 诚实留空（不可逆）")

    return ok


# ---------------------------------------------------------------------------
# 附加：临时明文受控
# ---------------------------------------------------------------------------

def check_temp_plaintext(tmp: Path) -> bool:
    """附加：临时明文放受控目录、用完即删。"""
    banner("附加检查：临时明文的生命周期与位置")

    from mmwf.infra.filestore import make_filestore

    store = make_filestore(MEETING_ID, tmp)
    payload = b"realaudio" * 1000
    store.write("audio/a.m4a", payload, stage="audio")

    ok = True
    with store.plaintext("audio/a.m4a") as p:
        controlled = (store.store_root / ".tmp").resolve()
        in_place = controlled in p.resolve().parents
        content_ok = p.read_bytes() == payload
        captured = p.parent
        ok &= in_place and content_ok
        print(f"  {PASS if in_place else FAIL} 临时明文在受控目录 {controlled}")
        print(f"  {PASS if content_ok else FAIL} 临时明文内容正确（{len(payload):,} 字节）")

    gone = not captured.exists()
    ok &= gone
    print(f"  {PASS if gone else FAIL} 上下文退出后临时目录已删除")

    # 异常路径
    captured2 = None
    try:
        with store.plaintext("audio/a.m4a") as p:
            captured2 = p.parent
            raise RuntimeError("模拟 ffmpeg 失败")
    except RuntimeError:
        pass
    gone2 = captured2 is not None and not captured2.exists()
    ok &= gone2
    print(f"  {PASS if gone2 else FAIL} 异常退出时临时目录仍被清理")

    return ok


# ---------------------------------------------------------------------------
# 附加：密钥来源如实标注
# ---------------------------------------------------------------------------

def check_key_source(tmp: Path) -> bool:
    """附加：密钥来源必须如实标注（未加固要显式暴露）。"""
    banner("附加检查：密钥来源与加固状态")

    from mmwf.infra.keystore import (
        KEY_FILE_NAME,
        WARN_FILE_NAME,
        describe_key_source,
        load_or_create_key,
    )

    src = load_or_create_key(tmp)
    print(f"  后端：{src.backend}")
    print(f"  已加固（Secure）：{src.secure}")
    print(f"  说明：{src.detail}")
    print(f"  人类可读描述：{describe_key_source(tmp)}")

    ok = len(src.key) == 32
    print(f"  {PASS if ok else FAIL} 主密钥长度 32 字节")

    if src.secure:
        print(f"  {PASS} 密钥托管在操作系统凭据管理器")
    else:
        warn = tmp / WARN_FILE_NAME
        has_warn = warn.exists()
        print(f"  {PASS} 如实标记为「待加固」，未伪装成安全方案")
        print(f"  {PASS if has_warn else FAIL} 已生成待加固说明文件 {warn.name}")
        print(f"  {PASS if (tmp / KEY_FILE_NAME).exists() else FAIL} "
              f"密钥文件 {KEY_FILE_NAME} 存在（权限已收紧）")
        ok &= has_warn

    return ok


# ---------------------------------------------------------------------------
# 附加：全量用例
# ---------------------------------------------------------------------------

def check_tests() -> bool:
    banner("附加检查：T1-03 全量用例")

    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/unit/test_crypto.py", "tests/unit/test_filestore.py",
         "tests/unit/test_keystore.py", "tests/unit/test_keystore_backends.py",
         "-q", "--no-header"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else "(无输出)"
    ok = result.returncode == 0
    print(f"  {PASS if ok else FAIL} {last}")
    if not ok:
        print(result.stdout[-3000:])
    return ok


def main() -> int:
    print("T1-03 验收：文件仓储与加密")
    print(f"项目根：{PROJECT_ROOT}")

    def _sub(name: str) -> Path:
        """每个检查用独立的子目录，避免互相污染（也避免重复建库/建密钥）。"""
        d = _TMP_BASE[0] / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        _TMP_BASE[0] = Path(td)
        results = {
            "标准 1 落盘非明文": check_standard_1(_sub("s1")),
            "标准 2 读回 sha256 一致": check_standard_2(_sub("s2")),
            "标准 3 manifest 条目一致": check_standard_3(_sub("s3")),
            "标准 4 删 manifest 可重建": check_standard_4(_sub("s4")),
            "附加 临时明文受控": check_temp_plaintext(_sub("s5")),
            "附加 密钥来源如实标注": check_key_source(_sub("s6")),
            "附加 全量用例通过": check_tests(),
        }

    banner("验收结论")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL} {name}")

    all_ok = all(results.values())
    print(f"\n{'=' * 72}")
    print(f"{PASS} T1-03 验收通过" if all_ok else f"{FAIL} T1-03 验收未通过")
    print(f"{'=' * 72}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
