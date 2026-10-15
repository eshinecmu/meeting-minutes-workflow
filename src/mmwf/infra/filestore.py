"""文件仓储（T1-03）。

职责
----
1. 按《架构设计说明书》§8.2 的目录约定创建与定位文件；
2. 所有读取对上层**透明解密**，返回明文 bytes 或受控临时明文路径；
3. 维护 `manifest.json`：每次产物落盘追加一条 `{path, sha256, created_at, stage}`。

目录约定（SAD §8.2，一期落地为加密子集）
----------------------------------------
    data/store/{meeting_id}/
    ├── audio/
    │   ├── original.{ext}            # 原始音频（加密）
    │   └── chunks/chunk_0001.wav     # 切分片段（加密）
    ├── transcript/
    │   ├── raw.json                  # 引擎原始输出（加密）
    │   └── segments.json             # 规范化分段（加密）
    ├── record/  v1.md ...            # 会议记录（加密）
    ├── minutes/
    │   ├── structured.json           # 结构化纪要（加密）
    │   ├── draft_v1.md               # 草稿（加密）
    │   └── final.md                  # 定稿（加密）
    ├── export/                       # 导出件（**明文**，需交付给人）
    └── manifest.json                 # 产物索引（明文，只记路径与哈希）

哪些加密、哪些不加密
--------------------
- 会话内产物（audio/transcript/record/minutes）：**加密**。
- export/：**明文**。SAD §11.2 明写"导出文件明文（需交付给人），但存放目录受访问控制"。
  不要"顺手也加密导出的 docx"——那会让用户拿到一个打不开的文件。
- manifest.json：**明文**。它只记录路径/哈希/时间/阶段，不含正文；
  且它是审计回溯与容错的依据，加密它反而是自找麻烦。

为什么"领域层不感知加密"
------------------------
上层永远只调用 `write(...)/read(...)`，传明文、拿明文。
加密与否是**本模块的实现选择**，通过构造参数决定，不泄漏给调用方。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from mmwf.infra.crypto import FileCipher, sha256_bytes, sha256_file
from mmwf.infra.keystore import load_or_create_key

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"

# 不加密的子目录（SAD §11.2）
PLAINTEXT_AREAS = frozenset({"export"})
# 加密的子目录
ENCRYPTED_AREAS = frozenset({"audio", "transcript", "record", "minutes"})

Stage = Literal["audio", "transcript", "record", "minutes", "export"]


class FileStoreError(RuntimeError):
    """文件仓储错误。"""


@dataclass(frozen=True)
class ManifestEntry:
    """manifest.json 的一条记录（SAD §8.2：path / sha256 / created_at / stage）。"""

    path: str            # 相对 store/{meeting_id}/ 的 POSIX 路径
    sha256: str          # **磁盘上那个文件**的哈希（密文则密文哈希）
    created_at: str      # ISO8601 UTC
    stage: str

    # 附加字段：不破坏 SAD 规定的四要素，但让 manifest 真正可用
    plain_sha256: str | None = None      # 明文哈希（便于校验"读回的字节与原文件一致"）
    encrypted: bool = False
    size_bytes: int = 0


@dataclass(frozen=True)
class StoredFile:
    """一次落盘的结果。"""

    rel_path: str
    abs_path: Path
    sha256: str            # 磁盘文件哈希
    plain_sha256: str      # 明文哈希
    encrypted: bool
    size_bytes: int


class FileStore:
    """单个会议的文件仓储。

    典型用法::

        store = FileStore(meeting_id, data_root)
        rec = store.write("audio/original.m4a", audio_bytes, stage="audio")
        got = store.read("audio/original.m4a")            # 透明解密
        with store.plaintext("audio/original.m4a") as p:  # 给 ffmpeg 用的临时明文
            subprocess.run(["ffmpeg", "-i", p, ...])
        store.manifest()                                  # 全部产物索引
    """

    def __init__(
        self,
        meeting_id: str,
        data_root: Path | str,
        *,
        cipher: FileCipher | None = None,
        encrypt_enabled: bool = True,
    ) -> None:
        if not meeting_id or "/" in meeting_id or "\\" in meeting_id:
            raise ValueError(f"meeting_id 非法：{meeting_id!r}")
        if meeting_id == ".tmp":
            raise ValueError("meeting_id 不允许为 '.tmp'（与内部临时目录冲突）")
        self.meeting_id = meeting_id
        self.data_root = Path(data_root)
        self.store_root = self.data_root / "store" / meeting_id

        self.encrypt_enabled = encrypt_enabled
        if not encrypt_enabled:
            self._cipher: FileCipher | None = None
        elif cipher is not None:
            self._cipher = cipher
        else:
            # 未显式传 cipher：自动从 data_root 取主密钥。
            # 走这条路的调用方不必知道密钥从哪来（这正是"领域层不感知加密"的延伸）。
            from mmwf.infra.keystore import load_or_create_key

            self._cipher = FileCipher(load_or_create_key(self.data_root).key)

    # ------------------------------------------------------------------
    # 路径与目录
    # ------------------------------------------------------------------

    def area_dir(self, stage: str) -> Path:
        """取某类产物的目录（不存在则创建）。"""
        if stage not in ENCRYPTED_AREAS and stage not in PLAINTEXT_AREAS:
            raise FileStoreError(
                f"未知产物类别 {stage!r}；允许："
                f"{sorted(ENCRYPTED_AREAS | PLAINTEXT_AREAS)}"
            )
        d = self.store_root / stage
        d.mkdir(parents=True, exist_ok=True)
        return d

    def resolve(self, rel_path: str) -> Path:
        """把相对路径（如 `audio/original.m4a`）解析为绝对路径，并防目录穿越。

        防穿越是必须的：rel_path 早晚会来自 API 请求参数。

        路径检查不能只依赖宿主平台：Windows 会把无盘符的根路径视为非绝对路径，
        POSIX 则不识别 Windows 盘符。必须同时检查两种平台的根与盘符语义，
        拒绝根路径、盘符相对路径和 UNC 路径，并在最后做一次
        "解析后的真实路径必须落在 store_root 之内"的兜底检查——
        这才是真正可靠的防线。
        """
        s = str(rel_path)
        if not s or s[0] in ("/", "\\"):
            raise FileStoreError(f"rel_path 必须是相对路径：{rel_path!r}")
        p = Path(s)
        if PurePosixPath(s).anchor or PureWindowsPath(s).anchor:
            raise FileStoreError(f"rel_path 必须是相对路径：{rel_path!r}")
        if ".." in p.parts:
            raise FileStoreError(f"rel_path 不允许包含 '..'：{rel_path!r}")

        target = (self.store_root / p)
        # 兜底：规范化后必须仍在 store_root 之下（防符号链接/盘符前缀等变体）
        root_resolved = self.store_root.resolve()
        target_resolved = target.resolve()
        if root_resolved != target_resolved and root_resolved not in target_resolved.parents:
            raise FileStoreError(
                f"rel_path 解析后越出会议目录：{rel_path!r} -> {target_resolved}"
            )
        return target

    def _is_encrypted_path(self, rel_path: str) -> bool:
        """按所在类别决定是否加密；export/ 与 manifest 恒为明文。"""
        if not self.encrypt_enabled:
            return False
        first = Path(rel_path).parts[0] if Path(rel_path).parts else ""
        return first not in PLAINTEXT_AREAS

    # ------------------------------------------------------------------
    # 读写（透明加解密）
    # ------------------------------------------------------------------

    def write(self, rel_path: str, data: bytes, *, stage: Stage | str | None = None) -> StoredFile:
        """写入产物。按目录类别自动决定是否加密，并登记 manifest。"""
        dst = self.resolve(rel_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._is_encrypted_path(rel_path)

        plain_digest = sha256_bytes(data)

        if encrypted:
            assert self._cipher is not None
            blob = self._cipher.encrypt_bytes(data)
            disk_digest = sha256_bytes(blob)
        else:
            blob = data
            disk_digest = plain_digest

        entry = ManifestEntry(
            path=rel_path,
            sha256=disk_digest,
            created_at=_now_iso(),
            stage=stage or (Path(rel_path).parts[0] if Path(rel_path).parts else "unknown"),
            plain_sha256=plain_digest,
            encrypted=encrypted,
            size_bytes=len(blob),
        )
        # Stage before replacing: ENOSPC may occur after a partial write. Keep
        # the old inode until the atomic manifest update has also succeeded.
        # A hard link keeps dst present until staged.replace(dst); renaming dst
        # away would leave it missing if killed between the two renames.
        # File + manifest are not a crash-atomic pair: a kill after installation
        # can leave the old manifest and a backup requiring recovery/rebuild.
        fd, name = tempfile.mkstemp(dir=dst.parent, suffix=".tmp")
        os.close(fd)
        staged = Path(name)
        backup = staged.with_suffix(".backup.tmp")
        installed = False
        try:
            staged.write_bytes(blob)
            if dst.exists():
                backup.hardlink_to(dst)
            staged.replace(dst)
            installed = True
            self._append_manifest(entry)
        except BaseException:
            if backup.exists():
                backup.replace(dst)
            elif installed:
                dst.unlink(missing_ok=True)
            raise
        finally:
            staged.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)

        return StoredFile(
            rel_path=rel_path,
            abs_path=dst,
            sha256=disk_digest,
            plain_sha256=plain_digest,
            encrypted=encrypted,
            size_bytes=len(blob),
        )

    def write_text(self, rel_path: str, text: str, *, stage: Stage | str | None = None,
                   encoding: str = "utf-8") -> StoredFile:
        """写入文本（统一 utf-8，遵守项目三条铁律第 1 条）。"""
        return self.write(rel_path, text.encode(encoding), stage=stage)

    def write_json(self, rel_path: str, obj: object, *, stage: Stage | str | None = None) -> StoredFile:
        """写入 JSON（utf-8、保留中文）。"""
        return self.write_text(
            rel_path, json.dumps(obj, ensure_ascii=False, indent=2), stage=stage
        )

    def read(self, rel_path: str) -> bytes:
        """读取产物明文。密文自动解密。"""
        src = self.resolve(rel_path)
        if not src.exists():
            raise FileStoreError(f"产物不存在：{rel_path}")
        blob = src.read_bytes()

        if self._is_encrypted_path(rel_path):
            assert self._cipher is not None
            return self._cipher.decrypt_bytes(blob)
        return blob

    def read_text(self, rel_path: str, encoding: str = "utf-8") -> str:
        return self.read(rel_path).decode(encoding)

    def read_json(self, rel_path: str) -> object:
        return json.loads(self.read_text(rel_path))

    @contextmanager
    def plaintext(self, rel_path: str) -> Iterator[Path]:
        """给外部程序（ffmpeg/pandoc 等）提供一个**受控的临时明文路径**。

        施工卡硬性要求：
          1. 临时明文**用完立即删除** → finally 里整目录删除；
          2. **不放系统 /tmp** → 用 `store_root/.tmp` 下的专用目录
             （受 data/ 的目录权限与磁盘加密保护，且随会议数据一起管理）。

        为什么不做"解密到内存再喂给 ffmpeg"：ffmpeg 只吃文件路径，
        没有 stdin 接口可用（除非走 pipe 协议，复杂且易错）。
        """
        if self._is_encrypted_path(rel_path):
            data = self.read(rel_path)
            suffix = Path(rel_path).suffix
        else:
            data = self.read(rel_path)
            suffix = Path(rel_path).suffix

        tmp_parent = self.store_root / ".tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="plain-", dir=tmp_parent))
        tmp_file = tmp_dir / f"{Path(rel_path).name}"
        try:
            tmp_file.write_bytes(data)
            logger.debug("临时明文已生成：%s（用完即删）", tmp_file)
            yield tmp_file
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def exists(self, rel_path: str) -> bool:
        return self.resolve(rel_path).exists()

    def delete(self, rel_path: str) -> None:
        """删除产物并从 manifest 移除对应条目。"""
        p = self.resolve(rel_path)
        if p.exists():
            p.unlink()
        self._remove_manifest(rel_path)

    # ------------------------------------------------------------------
    # manifest
    # ------------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.store_root / MANIFEST_NAME

    def manifest(self) -> list[ManifestEntry]:
        """读取 manifest。文件不存在时返回空列表（不抛错，便于容错）。"""
        p = self.manifest_path
        if not p.exists():
            return []
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise FileStoreError(f"manifest 损坏：{p}（{e}）") from e
        items = raw.get("entries", []) if isinstance(raw, dict) else raw
        return [ManifestEntry(**item) for item in items]

    def _append_manifest(self, entry: ManifestEntry) -> None:
        """追加一条记录（同 path 覆盖：同一路径重复落盘以最后一次为准）。"""
        self.store_root.mkdir(parents=True, exist_ok=True)
        entries = [e for e in self.manifest() if e.path != entry.path]
        entries.append(entry)
        self._flush_manifest(entries)

    def _remove_manifest(self, rel_path: str) -> None:
        entries = [e for e in self.manifest() if e.path != rel_path]
        self._flush_manifest(entries)

    def _flush_manifest(self, entries: list[ManifestEntry]) -> None:
        payload = {
            "meeting_id": self.meeting_id,
            "updated_at": _now_iso(),
            "entries": [asdict(e) for e in entries],
        }
        # 原子写：先写 .tmp 再 replace，避免断电留下半个 JSON
        tmp = self.manifest_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.manifest_path)
        finally:
            tmp.unlink(missing_ok=True)

    def rebuild_manifest(self) -> list[ManifestEntry]:
        """扫描目录重建 manifest（施工卡容错验收第 4 条）。

        什么时候用：manifest 被误删/损坏，但产物文件还在。
        重建时**无法知道原始明文哈希**（密文不可逆），因此：
          - 对密文文件：sha256 为密文哈希，plain_sha256 留空；
          - 对明文文件：两值相同。
        诚实标注比编造一个假哈希重要——manifest 是审计依据。

        加密判定不靠"文件读过能解密"（那要遍历解密、代价大）：
        按目录类别判断 + `is_encrypted` 魔数快速抽样确认。
        """
        entries: list[ManifestEntry] = []
        if not self.store_root.exists():
            return entries

        from mmwf.infra.crypto import is_encrypted

        for path in sorted(self.store_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.store_root)
            if rel.name == MANIFEST_NAME or rel.suffix == ".tmp":
                continue
            if rel.parts and rel.parts[0] == ".tmp":
                continue

            disk_digest = sha256_file(path)
            enc = is_encrypted(path)
            entries.append(
                ManifestEntry(
                    path=rel.as_posix(),
                    sha256=disk_digest,
                    created_at=_mtime_iso(path),
                    stage=rel.parts[0] if len(rel.parts) > 1 else "root",
                    plain_sha256=None if enc else disk_digest,
                    encrypted=enc,
                    size_bytes=path.stat().st_size,
                )
            )

        self.store_root.mkdir(parents=True, exist_ok=True)
        self._flush_manifest(entries)
        return entries

    def verify_manifest(self) -> list[str]:
        """校验 manifest 与实际文件是否一致，返回问题描述列表（空 = 一致）。

        检查三件事：文件是否还在、哈希是否吻合、是否有未登记的新文件。
        """
        problems: list[str] = []
        entries = self.manifest()
        recorded = {e.path for e in entries}

        for e in entries:
            p = self.resolve(e.path)
            if not p.exists():
                problems.append(f"manifest 记录的文件已丢失：{e.path}")
                continue
            actual = sha256_file(p)
            if actual != e.sha256:
                problems.append(
                    f"哈希不匹配：{e.path}（manifest={e.sha256[:12]}…，"
                    f"实际={actual[:12]}…）"
                )

        for path in sorted(self.store_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.store_root)
            if rel.name == MANIFEST_NAME or rel.suffix == ".tmp":
                continue
            if rel.parts and rel.parts[0] == ".tmp":
                continue
            if rel.as_posix() not in recorded:
                problems.append(f"存在未登记的文件：{rel.as_posix()}")

        return problems


def _now_iso() -> str:
    """当前时间 ISO8601（UTC，带 Z）。统一用 UTC，避免跨时区审计对不上。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mtime_iso(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_filestore(
    meeting_id: str,
    data_root: Path | str,
    *,
    encrypt_enabled: bool = True,
    allow_file_key_fallback: bool = True,
) -> FileStore:
    """构造带密钥的 FileStore（推荐的对外入口）。

    把"取密钥 → 建 cipher → 建 store"三步收在一处，
    避免调用方各自去拼装、漏掉某一步。
    """
    data_root = Path(data_root)
    if not encrypt_enabled:
        return FileStore(meeting_id, data_root, encrypt_enabled=False)

    key_source = load_or_create_key(data_root, allow_file_fallback=allow_file_key_fallback)
    cipher = FileCipher(key_source.key)
    return FileStore(meeting_id, data_root, cipher=cipher, encrypt_enabled=True)
