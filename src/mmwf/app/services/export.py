"""公文导出（T1-09 service 层，M11 `docx` + `adapters/exporter`）。

本模块把三样东西接起来：

    `domain/docx`       结构转换 + 版式补丁（纯渲染，不落盘）
    `infra/filestore`   `export/vN.docx` 落盘 + manifest 登记
    `infra/audit`       `export` 留痕（动作已由 T1-04 登记）

service 层做四件"领域层做不了"的事：

  1. **读输入**：`record/vN.md`（T1-08 落盘的那份）+ `meeting` 表
  2. **决定导出文件名**：与 manifest 里的记录稿版本对应
  3. **落盘 + 留痕**
  4. **幂等**：同一份记录稿重复导出，不产生第二个文件（C07、IT-09）

为什么导出稿**不加密**
----------------------
`filestore.PLAINTEXT_AREAS = {"export"}`（SAD §11.2）。这不是疏漏，
是必须的：导出的 docx 是**交付物** —— 要给领导看、要打印、要归档、
要发给上级单位。它一旦加密，Word 打不开，这份公文就废了。

于是有一条**别处没有的红线**，写在这里以免将来有人"顺手优化"掉：

    `export/` 是整个 data 目录里唯一不加密的地方。
    因此**绝不允许**把中间产物（转写稿、记录稿、抽取结果）落到这里。
    它们必须留在 `transcript/` / `record/` / `minutes/` 这些加密区。

源码里 `rec://` 之类的引用路径只有一条：从加密区读、往明文区写**最终产物**。

幂等键怎么算（C07：副作用节点必须幂等）
--------------------------------------
`IdempotencyGuard` 在 M03（graph）里统一管，本模块只提供**稳定的键**：

    key = f"export:{meeting_id}:{version}:{content_sha256[:16]}"

含 `content_sha256` 而不是只含 `version`：同一版本号理论上可能有两次
不同的渲染（比如改了 `config/models.yaml` 的字体策略），两份产物都该留下。
含 `version` 而不是只含内容哈希：便于人读，也便于"这份导出对应哪版记录稿"
这个问题能被直接回答。

同名覆盖 vs 新文件
------------------
`export/` 下的文件名固定为 `{meeting_id}-v{version}.docx`。
**同键重导出时覆盖同名文件**，不生成 `-1` / `-2` 副本 ——
理由是"交付物只有一个当前版本"，多副本会让"我发出去的是哪一份"变成
无法回答的问题。这**与记录稿的策略相反**（记录稿留版本便于回溯核对），
因为两者的用途不同：记录稿是"核对依据"要留痕，导出稿是"交付成品"要唯一。

`.doc` 导出（一期）
-------------------
SAD §6 / 施工卡：`.doc` 走 LibreOffice 无头模式转换，
且**必须作为独立进程调用**（GPL 隔离 —— 我们不链接 LO 的代码，
只是调用它，这样本项目的许可不会被 GPL 传染）。
`soffice` 缺失时**不报错**，返回 `skipped` 并说明原因：
一期 `.doc` 是可选兼容格式，主交付是 `.docx`。
"""

from __future__ import annotations

import errno
import hashlib
import logging
import tempfile
import threading
import shutil
import subprocess
from dataclasses import dataclass, field
from os.path import normcase
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from mmwf.domain.docx import (
    RenderOptions,
    RenderResult,
    build_check_result_json,
    check_fonts,
    policy_blocks_export,
    precheck_policy_error,
    render_markdown_to_docx,
)
from mmwf.domain.docx.writer import FontMissingError
from mmwf.domain.errors import MmwfError
from mmwf.infra.paths import relative_posix, export_paths, pdf_bindings
from mmwf.infra.crypto import CryptoError
from mmwf.infra.audit import AuditLogger
from mmwf.infra.filestore import FileStore, FileStoreError, StoredFile
from mmwf.infra.db import connect_ctx

logger = logging.getLogger(__name__)

#: 导出目录（**明文区**，见模块文档的红线说明）。
EXPORT_AREA = "export"

#: 记录稿目录（加密区，T1-08 的产物在这里读）。
RECORD_AREA = "record"

#: 导出文件名模板。与记录稿版本号一一对应。
EXPORT_NAME_TEMPLATE = "{meeting_id}-{artifact_label}-v{version}.docx"

#: `.doc` 转换的产物后缀。
DOC_SUFFIX = ".doc"

#: `.doc` 导出结果状态。
DOC_STATUS_OK = "ok"
DOC_STATUS_SKIPPED = "skipped"
DOC_STATUS_FAILED = "failed"

#: 默认字体策略（config/models.yaml `font_fallback_policy`）。
DEFAULT_FONT_POLICY = "warn"
PDF_REGISTRY = "archive/pdf-exports.json"
ARCHIVE_MANIFEST = "archive/manifest.json"
_PDF_LOCKS: dict[str, threading.RLock] = {}
_PDF_LOCKS_GUARD = threading.Lock()


def delivery_lock(store: FileStore) -> threading.RLock:
    """同会直接服务、下载、归档和撤销共用的可重入锁。"""
    with _PDF_LOCKS_GUARD:
        # Windows 的路径大小写别名必须共用锁，否则同一交付件可被并发覆盖。
        return _PDF_LOCKS.setdefault(normcase(str(store.store_root.resolve())), threading.RLock())



class ExportServiceError(MmwfError):
    """导出环节的使用错误（缺输入、状态不对等）。

    ★★ 继承 `MmwfError` 而不是裸 `RuntimeError`（T1-16 验收期修，坑 16）。

    第一版是 `RuntimeError`，后果很隐蔽：API 层只注册了
    `@app.exception_handler(MmwfError)` **和** `@app.exception_handler(Exception)`
    两个处理器。裸 `RuntimeError` 不属于前者，于是掉进后者 ——
    被渲染成 **E9001「程序遇到一个未预期的问题」+ 一个 trace_id**。

    这对用户是**双重误导**，而且它踩中的是**最常见**的那条路径：

      · 建完会议、还没生成记录稿就去点"导出" —— `latest_record_version()`
        抛本异常。这是**首次使用的正常操作顺序**，不是异常；
      · 界面却告诉用户"遇到了未预期的问题，请把编号告诉技术人员"，
        用户去报障，而正确答案只是"你还没生成记录稿"。

    继承 `MmwfError` 之后，它会被业务处理器接住，按 `code` 渲染成
    一句**能指向真因**的话。子类沿用同一个继承链即可
    （`ExportBlockedError` 已有 `code = "E5001"`，不受影响）。
    """

    code = "E1002"

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        """⚠️ 必须显式写 `__init__`：`MmwfError` 是 dataclass，
        第一个字段就是 `code` —— 直接 `ExportServiceError("一句话")`
        会把那句话**当成错误码**塞进 `code`，`to_dict()` 吐出来的
        `code` 就变成了用户那句话。

        这里沿用 `domain/errors.py` 里 `InputError` 系子类的同款签名
        （`detail` + `**ctx`），调用方现有的 `raise ExportServiceError("...")`
        一行都不用改。
        """
        super().__init__(code=type(self).code, detail=detail, context=ctx)


class ExportBlockedError(ExportServiceError):
    """`fail` 策略下字体缺失，导出中止（映射 E5001）。"""

    code = "E5001"


@dataclass
class DocConversion:
    """`.doc` 转换的结果。"""

    status: str = DOC_STATUS_SKIPPED
    rel_path: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.rel_path is not None:
            self.rel_path = relative_posix(self.rel_path)

    @property
    def ok(self) -> bool:
        return self.status == DOC_STATUS_OK

    def to_dict(self) -> dict:
        return {"status": self.status, "path": self.rel_path, "reason": self.reason}


@dataclass
class ExportResult:
    """一次导出的完整结果。"""

    meeting_id: str
    version: int
    rel_path: str
    sha256: str
    size_bytes: int
    encrypted: bool
    #: 幂等键（供 M03 的 IdempotencyGuard 使用）。
    idempotency_key: str
    #: 是否复用了已存在的同名文件（幂等命中）。
    reused: bool = False
    warnings: tuple[str, ...] = ()
    font_report: object | None = None
    doc_conversion: DocConversion = field(default_factory=DocConversion)

    format: str = "docx"
    source_docx: dict | None = None
    pdf_conversion: DocConversion | None = None

    def __post_init__(self) -> None:
        self.rel_path = relative_posix(self.rel_path)
        if self.source_docx is not None:
            self.source_docx = {**self.source_docx, "path": relative_posix(self.source_docx["path"])}

    @property
    def filename(self) -> str:
        return Path(self.rel_path).name

    @property
    def font_warnings(self) -> tuple[str, ...]:
        """只挑出字体相关的警告（E5002 的载荷）。"""
        return tuple(w for w in self.warnings if "font missing" in w)

    @property
    def warning_codes(self) -> tuple[str, ...]:
        """机器可读的警告码，保留既有字体提示文案。"""
        return ("E5002",) if self.font_warnings else ()

    def to_dict(self) -> dict:
        result = {
            "meeting_id": self.meeting_id,
            "version": self.version,
            "path": self.rel_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "encrypted": self.encrypted,
            "idempotency_key": self.idempotency_key,
            "reused": self.reused,
            "warnings": list(self.warnings),
            "font_warnings": list(self.font_warnings),
            "warning_codes": list(self.warning_codes),
            "doc": self.doc_conversion.to_dict(),
        }

        if self.format == "pdf":
            result.update(format="pdf", source_docx=self.source_docx,
                          pdf=self.pdf_conversion.to_dict() if self.pdf_conversion else None)
        return result

    def summary(self) -> str:
        state = "复用已有文件" if self.reused else "新生成"
        return (
            f"{self.filename}（v{self.version}，{state}，"
            f"{self.size_bytes} 字节，{len(self.font_warnings)} 条字体警告）"
        )


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------

def make_idempotency_key(meeting_id: str, version: int, content_sha256: str) -> str:
    """构造导出节点的幂等键（C07）。

    形状 `export:{meeting_id}:v{version}:{sha16}`。

    截断到 16 位十六进制：够用（64 bit，碰撞概率在"一个单位十年导出量"
    这个量级下可忽略），且比 64 位全串更适合放进日志与审计 detail。
    **不要**截断到 8 位 —— 那在批量任务里开始有可观测的碰撞风险。
    """
    digest = (content_sha256 or "").strip().lower()
    if len(digest) < 16:
        raise ValueError(
            f"content_sha256 长度不足（{len(digest)}），无法构造幂等键"
        )
    return f"export:{meeting_id}:v{version}:{digest[:16]}"


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class ExportService:
    """把记录稿导成符合 GB/T 9704—2012 的 docx。

    与 `ReviewService` 的分工：那个负责"生成本文"（记录稿），
    这个负责"把本文排成公文"。**本服务不读 `meeting` 表的业务字段** ——
    记录稿已经是完整的八项元信息 + 正文，导出只做版式。
    """

    def __init__(
        self,
        store: FileStore,
        *,
        db_path: Path | str,
        audit: AuditLogger | None = None,
        operator: str = "system",
        font_policy: str = DEFAULT_FONT_POLICY,
        soffice_path: str | None = None,
        enable_doc_export: bool = False,
        normalize: bool = True,
        auto_numbering: bool = True,
    ) -> None:
        self.store = store
        self.db_path = Path(db_path)
        self.audit = audit if audit is not None else AuditLogger(self.db_path)
        self.operator = operator
        self.font_policy = font_policy or DEFAULT_FONT_POLICY
        self.soffice_path = soffice_path
        self.enable_doc_export = enable_doc_export
        self.normalize = normalize
        self.auto_numbering = auto_numbering

    # --- 输入读取 -------------------------------------------------------

    def record_path(self, version: int) -> str:
        return f"{RECORD_AREA}/v{version}.md"

    def read_record(self, version: int) -> str:
        """读记录稿 Markdown。**从加密区读**（filestore 透明解密）。"""
        rel = self.record_path(version)
        if not self.store.exists(rel):
            raise ExportServiceError(
                f"记录稿不存在：{rel}。请先运行 review 生成记录稿（T1-08）。"
            )
        return self.store.read_text(rel)

    def latest_record_version(self) -> int:
        """最新的记录稿版本号（记录稿缺失时抛错）。

        与 T1-08 的 `next_version()` 一样取**目录 ∪ manifest** 的并集：
        manifest 记着"曾经有过 v2"，即使有人手动删了文件，
        我们也不该退回去覆盖 v1 的语义。
        """
        versions: list[int] = []
        directory = self.store.resolve(RECORD_AREA)
        if directory.exists():
            for p in directory.glob("v*.md"):
                n = _version_from_name(p.name)
                if n is not None:
                    versions.append(n)
        for entry in self.store.manifest():
            if entry.path.startswith(f"{RECORD_AREA}/v") and entry.path.endswith(".md"):
                n = _version_from_name(Path(entry.path).name)
                if n is not None:
                    versions.append(n)
        if not versions:
            raise ExportServiceError(
                "没有找到任何记录稿（record/v*.md）。请先运行 review 生成记录稿。"
            )
        return max(versions)

    # --- 字体预检 -------------------------------------------------------

    def font_report(self, *, installed: set[str] | None = None):
        """导出前的字体预检（F5.7 / R7）。"""
        return check_fonts(policy=self.font_policy, installed=installed)

    def system_fonts_payload(self) -> dict:
        """`GET /system/fonts` 的响应体（SAD §8.1）。"""
        return build_check_result_json(self.font_report())

    # --- 导出 -----------------------------------------------------------

    def export_docx(
        self,
        version: int | None = None,
        *,
        now: str | None = None,
        installed_fonts: set[str] | None = None,
        convert_doc: bool | None = None,
        write_audit: bool = True,
        artifact: str = "record",
        minutes_state: dict | None = None,
        minutes_snapshot: dict | None = None,
        approved: bool = False,
    ) -> ExportResult:
        """导出 docx。

        Args:
            version: 所选 artifact 的版本；`None` 取当前 / 最新。
            minutes_snapshot: 图显式传入的配对版本；服务独立复检后比对。
            installed_fonts: 显式指定已装字体（测试用）；`None` 走真实探测。
            convert_doc: 是否同时转 `.doc`；`None` 取实例配置。
            write_audit: 是否留痕（测试单测渲染时可在外面关掉）。
        """
        if not isinstance(artifact, str) or artifact not in {"record", "minutes"}:
            raise MmwfError(code="E4001", detail="artifact 必须为 record 或 minutes")
        snapshot = None
        if artifact == "minutes":
            if not hasattr(self, "minutes_service") or minutes_state is None:
                raise MmwfError(code="E4001", detail="纪要交付缺少校验上下文")
            snapshot = self.minutes_service.deliver(self.store.meeting_id, minutes_state,
                approved=approved, version=version, created_by=self.operator)
            if minutes_snapshot is not None and minutes_snapshot != snapshot:
                raise MmwfError(code="E4001", detail="待导出的纪要配对快照已变化")
            version = snapshot["version_no"]
            markdown = snapshot["markdown"]
        else:
            if version is None:
                version = self.latest_record_version()
            markdown = self.read_record(version)
        content_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        idem = make_idempotency_key(self.store.meeting_id + ":" + artifact, version, content_sha)

        # 字体策略 fail 时，在渲染之前就拦下（渲染里也会拦，这里是双保险）
        report = self.font_report(installed=installed_fonts)
        if policy_blocks_export(report):
            detail = precheck_policy_error(report)
            if write_audit:
                self._audit_export(version, None, "failure",
                                   extra={"reason": detail, "policy": self.font_policy})
            raise ExportBlockedError(detail)

        # --- 渲染 ---
        options = RenderOptions(
            font_policy=self.font_policy,
            normalize_text=self.normalize,
            auto_numbering=self.auto_numbering,
            font_report=report,
        )
        try:
            rendered = render_markdown_to_docx(markdown, options=options)
        except FontMissingError as exc:
            if write_audit:
                self._audit_export(version, None, "failure",
                                   extra={"reason": str(exc)})
            raise ExportBlockedError(str(exc)) from exc

        payload = _docx_bytes(rendered)
        label = "纪要" if artifact == "minutes" else "记录稿"
        rel_path = f"{EXPORT_AREA}/{EXPORT_NAME_TEMPLATE.format(meeting_id=self.store.meeting_id, artifact_label=label, version=version)}"

        # --- 幂等：同名且明文哈希一致 -> 复用，不重复登记 ---
        reused = self._valid_file(rel_path, hashlib.sha256(payload).hexdigest())
        if reused:
            stored = self._stored_of(rel_path)
        else:
            stored = self._write_export(rel_path, payload)

        warnings = list(rendered.warnings)

        # --- .doc 转换（可选） ---
        conv = DocConversion()
        want_doc = self.enable_doc_export if convert_doc is None else convert_doc
        if want_doc:
            conv = self.convert_to_doc(rel_path)
            if conv.status == DOC_STATUS_FAILED:
                warnings.append(f".doc 转换失败：{conv.reason}")

        result = ExportResult(
            meeting_id=self.store.meeting_id,
            version=version,
            rel_path=stored.rel_path,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            encrypted=stored.encrypted,
            idempotency_key=idem,
            reused=reused,
            warnings=tuple(warnings),
            font_report=report,
            doc_conversion=conv,
        )

        if write_audit:
            self._audit_export(
                version, rel_path, "success",
                extra={
                    "sha256": stored.sha256[:16],
                    "idempotency_key": idem,
                    "reused": reused,
                    "doc_status": conv.status,
                },
            )
        return result

    # --- .doc 转换（可选，独立进程） ------------------------------------

    def _convert_with_soffice(self, docx_rel_path: str, target: str) -> DocConversion:
        """内部进程接缝；PDF 使用会议明文区内的独立工作目录。"""
        soffice = self.soffice_path or shutil.which("soffice")
        if not soffice or not Path(soffice).exists():
            return DocConversion(status="skipped", reason="未找到 soffice（LibreOffice），请安装或配置后重试。")
        source = self.store.resolve(docx_rel_path)
        if not source.exists():
            return DocConversion(status="failed", reason="源 DOCX 不存在")
        work = None
        try:
            out_dir = source.parent
            if target == "pdf":
                work = tempfile.TemporaryDirectory(prefix=".pdf-", dir=out_dir)
                out_dir = Path(work.name)
                data = source.read_bytes()
                copied = out_dir / source.name
                copied.write_bytes(data)
                if hashlib.sha256(copied.read_bytes()).digest() != hashlib.sha256(data).digest():
                    return DocConversion(status="failed", reason="转换输入复制校验失败")
                source = copied
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", target,
                 "--outdir", str(out_dir), str(source)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180,
            )
            produced = out_dir / (source.stem + "." + target)
            if proc.returncode != 0 or not produced.is_file():
                return DocConversion(status="failed", reason=f"soffice 返回 {proc.returncode}，转换未完成")
            payload = produced.read_bytes()
            if target == "pdf" and not payload.startswith(b"%PDF-"):
                return DocConversion(status="failed", reason="转换输出为空或不是 PDF")
            rel = relative_posix(PurePosixPath(relative_posix(docx_rel_path)).with_suffix("." + target))
            self._write_export(rel, payload)
            return DocConversion(status="ok", rel_path=rel)
        except (subprocess.TimeoutExpired, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise
            return DocConversion(status="failed", reason="调用 soffice 失败：" + type(exc).__name__)
        finally:
            if work is not None:
                work.cleanup()

    def convert_to_doc(self, docx_rel_path: str) -> DocConversion:
        """保留一期可选 DOC 的 skipped/failed 软失败合同。"""
        return self._convert_with_soffice(docx_rel_path, "doc")

    def convert_to_pdf(self, docx_rel_path: str) -> DocConversion:
        """内部转换接缝；交付必须通过 export_pdf 的审定与来源检查。"""
        return self._convert_with_soffice(docx_rel_path, "pdf")

    def export_pdf(self, version: int | None = None, *, artifact: str = "minutes",
                   minutes_state: dict | None = None, approved: bool = False,
                   minutes_snapshot: dict | None = None, installed_fonts: set[str] | None = None,
                   convert_doc: bool = False) -> ExportResult:
        """仅交付审定纪要；当前稿硬检、字体策略均先于派生缓存。"""
        if artifact != "minutes" or type(convert_doc) is not bool or convert_doc:
            raise MmwfError(code="E1001", detail="PDF 仅支持纪要，且不能同时请求 DOC")
        if version is not None and (type(version) is not int or version < 1):
            raise MmwfError(code='E1001', detail='version 必须为正整数')
        with delivery_lock(self.store):
            try:
                if not hasattr(self, "minutes_service") or minutes_state is None:
                    raise MmwfError(code="E4001", detail="纪要交付缺少校验上下文")
                snapshot = self.minutes_service.deliver(self.store.meeting_id, minutes_state,
                    approved=approved, version=version, created_by=self.operator)
                if minutes_snapshot is not None and minutes_snapshot != snapshot:
                    raise MmwfError(code="E4001", detail="待导出的纪要配对快照已变化")
                source = self.export_docx(snapshot['version_no'], artifact="minutes",
                    minutes_state=minutes_state, minutes_snapshot=snapshot, approved=approved,
                    installed_fonts=installed_fonts, convert_doc=False, write_audit=False)
                source.rel_path = relative_posix(source.rel_path)
                version = source.version
                rel = relative_posix(PurePosixPath(relative_posix(source.rel_path)).with_suffix('.pdf'))
                binding = dict(version=version, pair_fingerprint=snapshot['note']['pair_fingerprint'],
                    source_docx_path=source.rel_path, source_docx_sha256=source.sha256)
                from mmwf.graph.idempotency import IdempotencyStore, make_idempotency_key
                key = make_idempotency_key(self.store.meeting_id, 'export', {
                    'meeting_id': self.store.meeting_id, 'artifact': 'minutes', 'version': version,
                    'pair_fingerprint': binding['pair_fingerprint'],
                    'source_docx_sha256': source.sha256, 'format': 'pdf'})
                entries = self.pdf_entries()
                entry = entries.get(rel)
                archived = minutes_state.get('stage') == 'archived' or self.current_meeting_status() == 'archived'
                archive = self._archive_for_pdf(archived)
                if archived and archive['export_paths'].get('docx') != source.rel_path:
                    raise MmwfError(code="E4001", detail="PDF 与归档纪要版本不一致")
                reused = bool(source.reused and isinstance(entry, dict) and entry and all(entry.get(k) == v for k, v in binding.items())
                    and self._valid_pdf_entry(rel, entry)
                    and (not archived or (archive.get('export_paths') or {}).get('pdf', {}).get(rel) == entry))
                if not reused:
                    conv = self.convert_to_pdf(source.rel_path)
                    if not conv.ok:
                        raise MmwfError(code="E5003", detail=conv.reason,
                            user_message=("未生成 PDF：请安装或配置既有 LibreOffice 后重试。"
                                          if conv.status == 'skipped' else "PDF 转换失败，请重试或联系技术人员。"))
                    if not self._valid_file(source.rel_path, source.sha256):
                        raise MmwfError(code='E5003', detail='转换期间同源 DOCX 已变化，请重试')
                    stored = self._stored_of(rel)
                    entry = {**binding, 'pdf_sha256': stored.sha256, 'size_bytes': stored.size_bytes}
                    entries[rel] = entry
                    self._write_pdf_entries(entries)
                    if archived:
                        paths = dict(archive.get('export_paths') or {})
                        paths['pdf'] = {**self.valid_pdf_entries(source.rel_path), rel: entry}
                        paths['artifact_sha256'] = self.artifact_hashes(paths)
                        archive['export_paths'] = paths
                        self._write_json(ARCHIVE_MANIFEST, archive)
                stored = self._stored_of(rel)
                IdempotencyStore(self.db_path).put(key, self.store.meeting_id, 'export', rel)
                result = ExportResult(self.store.meeting_id, version, rel, stored.sha256,
                    stored.size_bytes, False, key, reused=reused, warnings=source.warnings,
                    font_report=source.font_report, format='pdf',
                    source_docx={'path': source.rel_path, 'sha256': source.sha256},
                    pdf_conversion=DocConversion(status='ok', rel_path=rel),
                    doc_conversion=DocConversion(reason='本请求未要求 DOC'))
                self._audit_export(version, rel, 'success', extra={'format': 'pdf', 'sha256': stored.sha256,
                    'source_sha256': source.sha256, 'reused': reused, 'archive_supplement': archived})
                return result
            except (MmwfError, OSError, FileStoreError, CryptoError) as exc:
                self._audit_export(version or 0, None, 'failure', extra={
                    'format': 'pdf', 'reason': getattr(exc, 'code', type(exc).__name__)})
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise MmwfError(code='E9002', detail='PDF 导出存储空间不足') from exc
                if isinstance(exc, (FileStoreError, CryptoError)):
                    raise MmwfError(code='E5003', detail='PDF 存储或登记失败') from exc
                raise

    def _write_json(self, rel: str, value: dict) -> None:
        """内部登记也沿统一写入错误语义，满盘交上层处理。"""
        if rel == ARCHIVE_MANIFEST and isinstance(value.get("export_paths"), dict):
            value = {**value, "export_paths": export_paths(value["export_paths"])}
        try:
            self.store.write_json(rel, value, stage="archive")
        except PermissionError as exc:
            raise MmwfError(code="E5004", detail="PDF 登记文件无法写入") from exc
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise
            raise MmwfError(code="E5003", detail="PDF 登记写入失败") from exc
        except (FileStoreError, CryptoError) as exc:
            raise MmwfError(code="E5003", detail="PDF 登记写入失败") from exc

    def pdf_entries(self) -> dict:
        if not self.store.exists(PDF_REGISTRY):
            return {}
        try:
            payload = self.store.read(PDF_REGISTRY)
            if hashlib.sha256(payload).hexdigest() != self._existing_plain_sha(PDF_REGISTRY):
                return {}
            import json
            data = json.loads(payload)
            if (isinstance(data, dict) and data['format_version'] == 1 and data['meeting_id'] == self.store.meeting_id
                    and isinstance(data['entries'], dict)):
                return pdf_bindings(data['entries'])
        except (ValueError, KeyError, TypeError, OSError, MmwfError, FileStoreError, CryptoError):
            pass
        return {}  # 损坏登记不作为缓存证据；重试须重新转换并登记。

    def _write_pdf_entries(self, entries: dict) -> None:
        self._write_json(PDF_REGISTRY, {'format_version': 1,
            'meeting_id': self.store.meeting_id, 'entries': pdf_bindings(entries)})

    def _archive_for_pdf(self, archived: bool) -> dict:
        if not archived:
            return {}
        try:
            import json
            payload = self.store.read(ARCHIVE_MANIFEST)
            data = json.loads(payload)
            paths = data['export_paths']
            if (data['meeting_id'] == self.store.meeting_id and data.get('archived_at')
                    and isinstance(paths, dict) and isinstance(paths.get('pdf', {}), dict)
                    and hashlib.sha256(payload).hexdigest() == self._existing_plain_sha(ARCHIVE_MANIFEST)):
                data['export_paths'] = export_paths(paths)
                return data
        except (OSError, ValueError, KeyError, TypeError, MmwfError, FileStoreError, CryptoError):
            pass
        raise MmwfError(code="E5003", detail="已归档会议缺少有效归档清单",
                        user_message="PDF 留档未完成：请恢复该会议的归档清单后重试。")

    def artifact_hashes(self, paths: dict) -> dict:
        """归档集合使用实际路径/明文哈希，PDF 绑定另在 pdf 映射中参与键。"""
        paths = export_paths(paths)
        hashes = {}
        for rel in [paths.get('docx'), (paths.get('doc') or {}).get('rel_path'), *(paths.get('pdf') or {})]:
            if rel:
                if not self.store.exists(rel):
                    raise MmwfError(code='E1002', detail='归档产物缺失')
                hashes[rel] = hashlib.sha256(self.store.read(rel)).hexdigest()
        return dict(sorted(hashes.items()))

    def _valid_file(self, rel: str, sha: str, *, pdf: bool = False, size: int | None = None) -> bool:
        rel = relative_posix(rel)
        try:
            data = self.store.read(rel)
            entry = next((e for e in self.store.manifest() if e.path == rel), None)
            return (entry is not None and not entry.encrypted and entry.size_bytes == len(data)
                and entry.sha256 == sha and bool(data) and (not pdf or data.startswith(b'%PDF-'))
                and (size is None or len(data) == size)
                and hashlib.sha256(data).hexdigest() == sha and self._existing_plain_sha(rel) == sha)
        except (OSError, MmwfError, FileStoreError, CryptoError):
            return False

    def _valid_pdf_entry(self, rel: str, entry: dict) -> bool:
        if not isinstance(entry, dict):
            return False
        try:
            expected = f"export/{self.store.meeting_id}-纪要-v{entry['version']}"
            return (rel == expected + '.pdf' and entry['source_docx_path'] == expected + '.docx'
                and self._valid_file(rel, entry['pdf_sha256'], pdf=True, size=entry['size_bytes'])
                and self._valid_file(entry['source_docx_path'], entry['source_docx_sha256']))
        except (KeyError, TypeError, ValueError):
            return False

    def require_pdf_source(self, rel: str, final: dict) -> dict:
        rel = relative_posix(rel)
        entry = self.pdf_entries().get(rel)
        if not entry or not isinstance(entry, dict) or entry.get('version') != final['version_no'] or entry.get('pair_fingerprint') != final['note']['pair_fingerprint']:
            raise MmwfError(code="E4001", detail="PDF 没有匹配的审定来源登记")
        if not self._valid_pdf_entry(rel, entry):
            raise MmwfError(code="E1002", detail="PDF 或同源 DOCX 缺失、损坏或未登记")
        return entry

    def valid_pdf_entries(self, docx: str) -> dict:
        """归档前核对 final、来源及实际字节，清理失效来源登记。"""
        docx = relative_posix(docx)
        entries = self.pdf_entries()
        valid = {}
        retained = {}
        for rel, entry in entries.items():
            try:
                final = self.minutes_service.require_final(self.store.meeting_id, rel)
                self.require_pdf_source(rel, final)
            except MmwfError:
                continue
            retained[rel] = entry
            if entry['source_docx_path'] == docx:
                valid[rel] = entry
        if entries != retained:
            self._write_pdf_entries(retained)
        return valid

    # --- 撤销导出（F5.8） -----------------------------------------------

    def undo_export(self, rel_path: str) -> bool:
        with delivery_lock(self.store):
            try:
                return self._undo_export(rel_path)
            except (MmwfError, OSError, FileStoreError, CryptoError) as exc:
                if not rel_path.endswith('.pdf'):
                    raise
                self._audit_export(0, rel_path, 'failure', extra={
                    'format': 'pdf', 'reason': getattr(exc, 'code', type(exc).__name__), 'undo': True})
                if isinstance(exc, MmwfError):
                    raise
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    code = 'E9002'
                else:
                    code = 'E5004' if isinstance(exc, PermissionError) else 'E5003'
                raise MmwfError(code=code, detail='PDF 撤销未完成，请重试') from exc

    def _undo_export(self, rel_path: str) -> bool:
        """撤销一次导出（删除产物 + 留痕）。

        这是 F5.8"可撤销"的落点。删文件本身由 `store.delete()`
        同时清 manifest，所以撤销后 `verify_manifest()` 仍然干净。
        """
        if not rel_path.startswith(f"{EXPORT_AREA}/"):
            raise ExportServiceError(
                f"只能撤销 {EXPORT_AREA}/ 下的文件，收到：{rel_path}"
            )
        if Path(rel_path).parent != Path(EXPORT_AREA) or "\\" in rel_path:
            raise MmwfError(code="E1001", detail="非法导出路径")
        existed = self.store.exists(rel_path)
        if rel_path.endswith('.pdf'):
            entries = self.pdf_entries()
            existed = existed or rel_path in entries
            entries.pop(rel_path, None)
            # 先清理归档再清来源；任一步失败都不返回完整撤销。
            if self.store.exists(ARCHIVE_MANIFEST):
                archive = self._archive_for_pdf(True)
                paths = archive['export_paths']
                pdfs = paths.get('pdf', {})
                existed = existed or rel_path in pdfs
                pdfs.pop(rel_path, None)
                paths['artifact_sha256'] = self.artifact_hashes(paths)
                self._write_json(ARCHIVE_MANIFEST, archive)
            self._write_pdf_entries(entries)
        if existed:
            self.store.delete(rel_path)
        self.audit.export_event(
            self.operator, self.store.meeting_id,
            files=[rel_path], undo=True,
            detail={"removed": existed},
        )
        return existed

    # --- 内部工具 -------------------------------------------------------

    def _write_export(self, rel_path: str, payload: bytes) -> StoredFile:
        try:
            return self.store.write(rel_path, payload, stage="export")
        except PermissionError as exc:
            raise MmwfError(code="E5004", detail="导出目标文件无法写入") from exc
        except OSError as exc:
            # 满盘保留系统错误语义，交给上层统一转换 E9002。
            if exc.errno == errno.ENOSPC:
                raise
            raise MmwfError(code="E5003", detail="导出文件写入失败") from exc

    def _existing_plain_sha(self, rel_path: str) -> str | None:
        rel_path = relative_posix(rel_path)
        for entry in self.store.manifest():
            if entry.path == rel_path:
                return entry.plain_sha256
        return None

    def _stored_of(self, rel_path: str) -> StoredFile:
        """幂等命中时，从磁盘与 manifest 重建 StoredFile。"""
        rel_path = relative_posix(rel_path)
        abs_path = self.store.resolve(rel_path)
        data = abs_path.read_bytes()
        plain = hashlib.sha256(data).hexdigest()
        entry = next((e for e in self.store.manifest() if e.path == rel_path), None)
        return StoredFile(
            rel_path=rel_path,
            abs_path=abs_path,
            sha256=entry.sha256 if entry else plain,
            plain_sha256=entry.plain_sha256 if entry else plain,
            encrypted=False,          # export 是明文区
            size_bytes=abs_path.stat().st_size,
        )

    def _audit_export(self, version: int, rel_path: str | None,
                      result: str, *, extra: Mapping[str, Any] | None = None) -> None:
        """导出留痕。**只记路径与统计，绝不记正文**（部署手册 §5.3）。"""
        rel_path = relative_posix(rel_path) if rel_path else None
        detail: dict[str, Any] = {"version": version}
        if rel_path:
            detail["path"] = rel_path
        if extra:
            detail.update({k: v for k, v in extra.items() if v is not None})
        self.audit.export_event(
            self.operator, self.store.meeting_id,
            files=[rel_path] if rel_path else None,
            detail=detail, result=result,
        )

    def current_meeting_status(self) -> str | None:
        with connect_ctx(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM meeting WHERE meeting_id = ?",
                (self.store.meeting_id,),
            ).fetchone()
        return row["status"] if row else None


def _docx_bytes(rendered: RenderResult) -> bytes:
    """把 Document 序列化成 bytes，**结果可复现**。

    python-docx 的 `save()` 只接受路径或类文件对象 —— 没有"返回 bytes"
    的 API。用 `io.BytesIO` 而不是先写临时文件：导出目录是明文区，
    多一步临时文件就多一处可能忘记清理的**未加密残留**。

    ⚠️ 必须走 `_docx_backend.save_bytes` 而不是直接 `doc.save(buf)`：
    后者写进 ZIP 条目的时间戳是**当前本地时间**，导致内容相同的两次
    渲染产出的字节不同，`reused` 判定随之失效（详见该函数的注释）。
    """
    from mmwf.domain.docx._docx_backend import save_bytes

    return save_bytes(rendered.doc)


def _version_from_name(name: str) -> int | None:
    """`v3.md` -> `3`；不是这个形状返回 None。"""
    stem = Path(name).stem
    if not stem.startswith("v"):
        return None
    try:
        return int(stem[1:])
    except ValueError:
        return None


def judge_docx(
    docx_bytes: bytes,
    *,
    checks: tuple[str, ...] = (),
) -> list[str]:
    """对 docx 字节做一轮快速自检，返回问题清单（空 = 通过）。

    这是给 service 层用的**轻量**校验：导出后立刻确认"我写出去的这份
    文件确实是合规的"。完整的逐项对照在 `tools/verify_docx_style.py`，
    这个函数只做最要紧的三条（表格零缩进、中文字体、表格边框），
    因为它们在导出链路上最容易因为"某个补丁没被调用"而静默失效。
    """
    problems: list[str] = []
    try:
        import io
        import zipfile
        from lxml import etree
    except ImportError as exc:  # pragma: no cover
        return [f"无法自检（缺 lxml）：{exc}"]

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml = z.read("word/document.xml")
    except Exception as exc:  # noqa: BLE001 - 自检不该抛
        return [f"文档无法解压：{exc}"]

    root = etree.fromstring(xml)

    for tbl in root.iter(W + "tbl"):
        for ind in tbl.iter(W + "ind"):
            for attr in ("firstLineChars", "firstLine", "leftChars", "left"):
                v = ind.get(W + attr)
                if v and v != "0":
                    problems.append(f"表格内段落缩进未清零：w:{attr}={v}")
        tbl_pr = tbl.find(W + "tblPr")
        if tbl_pr is None or tbl_pr.find(W + "tblBorders") is None:
            problems.append("表格缺少 w:tblBorders（全边框未注入）")

    if "w:eastAsia" not in xml.decode("utf-8"):
        problems.append("文档中没有 w:eastAsia（中文字体未生效）")

    return problems


def make_export_service(
    meeting_id: str,
    settings,
    *,
    audit: AuditLogger | None = None,
    operator: str = "system",
    encrypt_enabled: bool = True,
) -> ExportService:
    """从配置构造导出服务（与 `make_review_service` 同款签名）。

    读的配置项（都在 `Settings` 的顶层字段上）：
        `settings.app.data_dir`                  —— 数据根目录
        `settings.export.font_fallback_policy`   —— warn|fallback|fail
        `settings.export.soffice_path`           —— LibreOffice 路径
        `settings.export.enable_doc_export`      —— 是否转 .doc

    注意配置的形状：`app.yaml` / `models.yaml` 在**加载时被拍平**了 ——
    `models.yaml` 的 `export:` 段成了 `settings.export`，**不是**
    `settings.models.export`（`Settings` 里没有 `models` 这个字段）。
    `soffice_path` 是 `Path` 类型，要 `str()` 一下再交给 subprocess。
    """
    from mmwf.infra.filestore import make_filestore

    data_dir = settings.resolve_path(settings.app.data_dir)
    store = make_filestore(meeting_id, data_dir, encrypt_enabled=encrypt_enabled)

    export_cfg = settings.export
    soffice = getattr(export_cfg, "soffice_path", None)
    service = ExportService(
        store,
        db_path=data_dir / "mmwf.db",
        audit=audit,
        operator=operator,
        font_policy=getattr(export_cfg, "font_fallback_policy", DEFAULT_FONT_POLICY),
        soffice_path=str(soffice) if soffice else None,
        enable_doc_export=bool(getattr(export_cfg, "enable_doc_export", False)),
    )
    from .minutes import MinutesService
    service.minutes_service = MinutesService(settings)
    return service



__all__ = [
    "DEFAULT_FONT_POLICY",
    "DOC_STATUS_FAILED",
    "DOC_STATUS_OK",
    "DOC_STATUS_SKIPPED",
    "EXPORT_AREA",
    "EXPORT_NAME_TEMPLATE",
    "DocConversion",
    "ExportBlockedError",
    "ExportResult",
    "ExportService",
    "ExportServiceError",
    "judge_docx",
    "make_export_service",
    "make_idempotency_key",
]
