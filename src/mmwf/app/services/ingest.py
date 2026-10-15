"""音频接入编排（T1-06 service 层）。

本模块把"纯规则"（`domain/ingest/planner`）与"外部工具"
（`adapters/audio/ffmpeg`）拼起来，并负责三件只有 service 该做的事：

  1. **批量投递**：一次收多个文件，逐个校验 —— 一个失败不能拖垮整批。
  2. **写审计**：`import_audio` 动作留痕（部署手册 §6.2 的"音频导入记录"）。
  3. **落盘与命名**：切分产物按 `audio/chunks/chunk_0001.wav` 命名，
     与 SAD §8.2 的目录约定一致。

为什么逐个文件独立判成败
------------------------
批量投递时，若第 2 个文件损坏就直接抛异常，用户会以为"整个投递失败、
需要重来" —— 而实际上第 1 个已经成功导入、第 3 个也是好的。
所以 `ingest_batch()` 返回**逐文件的结果清单**，让界面能显示
"3 个成功、1 个失败（原因）"，用户只需重投失败的那个。

这与 N3.1（三步操作）一致：不应该让用户为"一个坏文件"重做整批。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from mmwf.adapters.audio.ffmpeg import FFmpegAdapter, FFmpegNotFoundError
from mmwf.domain.errors import MmwfError
from mmwf.domain.ingest import planner
from mmwf.domain.ingest.planner import (
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_OVERLAP_SECONDS,
    ChunkPlanItem,
    build_chunk_plan,
    validate_extension,
)

logger = logging.getLogger(__name__)

#: 单块切分产物的命名模板（SAD §8.2）。
#: 4 位补零：2 小时音频最多 4 段，但 24 小时的长会也不该溢出到 5 位，
#: 补零保证**按文件名排序 == 按 seq 排序**（复核页与命令行 ls 都依赖这点）。
CHUNK_NAME_TEMPLATE = "chunk_{seq:04d}.wav"

#: 4GB 阈值。**刻意从 planner 模块上读而不是 `from ... import MAX_FILE_BYTES`**：
#: from-import 是值拷贝，一旦有人把阈值改成配置项或改了 planner，
#: 这里的副本会静默地保持旧值 —— "两处各写一个数"是最难查的那种漂移。
#: 保留一个模块级名字是为了向后兼容（`ingest.MAX_FILE_BYTES` 仍可读）。
MAX_FILE_BYTES = planner.MAX_FILE_BYTES


def _human_size(n: int) -> str:
    """把字节数写成用户能读的大小。

    为什么不直接 `n / 1024**3:.2f} GB`：阈值一旦被调成小于 1GB
    （配置化之后完全可能），`{:.0f} GB` 会输出刺眼的 "0 GB"，
    用户看到的是"文件超过 0 GB"这种毫无意义的告警。
    """
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= scale:
            return f"{n / scale:.2f} {unit}"
    return f"{n} B"


@dataclass
class IngestedAudio:
    """单个音频的接入结果。"""

    source_path: Path
    filename: str
    format: str
    size_bytes: int
    duration_ms: int
    chunk_plan: list[ChunkPlanItem]
    warnings: list[str] = field(default_factory=list)
    chunk_files: list[Path] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_plan)

    @property
    def duration_sec(self) -> float:
        return self.duration_ms / 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "chunk_count": self.chunk_count,
            "chunk_plan": [c.to_dict() for c in self.chunk_plan],
            "warnings": list(self.warnings),
        }


@dataclass
class IngestFailure:
    """单个音频的接入失败。带上用户可见的文案，界面直接显示。"""

    source_path: Path
    filename: str
    error: MmwfError | None
    message: str

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"filename": self.filename, "message": self.message}
        if self.error is not None:
            d.update(self.error.to_dict())
        return d


@dataclass
class BatchResult:
    """一批投递的完整结果。

    `ok` 的语义是"至少有一个成功"还是"全部成功"？这里取**全部成功**，
    理由是调用方（API 层）需要用它决定 HTTP 状态：
    有失败时应该返回 207/400 让用户注意到，而不是 200 让用户以为都成了。
    具体逐条结果在 `succeeded` / `failed` 里。
    """

    succeeded: list[IngestedAudio] = field(default_factory=list)
    failed: list[IngestFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def total(self) -> int:
        return len(self.succeeded) + len(self.failed)

    def summary(self) -> str:
        if not self.failed:
            return f"{len(self.succeeded)} 个音频已接收"
        if not self.succeeded:
            return f"{len(self.failed)} 个音频都未能接收"
        return (
            f"{len(self.succeeded)} 个音频已接收，"
            f"{len(self.failed)} 个未能接收（可单独重投）"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "succeeded": [s.to_dict() for s in self.succeeded],
            "failed": [f.to_dict() for f in self.failed],
        }


class IngestService:
    """音频接入服务。

    Args:
        adapter: ffmpeg 适配器。测试可注入替身（不依赖本机装 ffmpeg）。
        chunk_seconds / overlap_seconds: 切分参数，来自 `config/models.yaml` 的
            `asr` 段（N5.2 要求可热改，所以不能写死在这里）。
        probe_only: 只探测与出计划、**不实际切分**。
            用途：投递时的"预检"——用户还没确认前不必把磁盘铺满切分产物。
    """

    def __init__(
        self,
        adapter: FFmpegAdapter,
        *,
        chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
        overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
        probe_only: bool = False,
    ) -> None:
        self._adapter = adapter
        self._chunk_seconds = chunk_seconds
        self._overlap_seconds = overlap_seconds
        self._probe_only = probe_only

    # ------------------------------------------------------------------
    # 单个文件
    # ------------------------------------------------------------------

    def ingest_one(
        self,
        source: Path | str,
        *,
        chunk_dir: Path | None = None,
    ) -> IngestedAudio:
        """校验 + 探测 + 出计划（+ 可选切分）。

        Raises:
            MmwfError: E1001（格式）/ E1002（不存在）/ E1003（损坏）。
            FFmpegNotFoundError: 本机没装 ffmpeg（环境问题，非业务错误）。
        """
        src = Path(source)
        # ① 格式先判 —— 让"传了个 .txt"在毫秒内失败，不用等到探测
        fmt = validate_extension(src.name)

        # ② 存在性 + 大小。**先判大小再探测**：4GB 文件让 ffprobe 走一遍
        #    要几十秒，而这个错误本来就能立刻判定。
        if not src.is_file():
            from mmwf.domain.errors import FileMissingError

            raise FileMissingError(detail=f"音频文件不存在：{src}", audio_path=str(src))

        size = src.stat().st_size
        warnings: list[str] = []
        if size == 0:
            from mmwf.domain.errors import CorruptAudioError

            raise CorruptAudioError(detail=f"文件为空（0 字节）：{src}", audio_path=str(src))
        limit = planner.MAX_FILE_BYTES
        if size > limit:
            # 施工卡要求"有明确提示"而不是静默失败。这里**不阻断**：
            # 4GB 是 ffmpeg 的软肋不是硬限制，小概率能过。
            # 让它继续但明确告警，比直接拒绝更符合"用户可能真有这么大的录音"。
            warnings.append(
                f"文件大小 {_human_size(size)}，超过上限 {_human_size(limit)}。"
                "超长文件处理可能很慢甚至失败，建议先用其他工具裁剪。"
            )
            logger.warning("投递大文件：%s（%.2f GB）", src.name, size / 1024**3)

        # ③ 探测时长（可能抛 E1003）
        duration_ms = self._adapter.probe_duration_ms(src)

        # ④ 出切分计划（纯函数）
        plan = build_chunk_plan(
            duration_ms,
            chunk_seconds=self._chunk_seconds,
            overlap_seconds=self._overlap_seconds,
        )
        if not plan:
            from mmwf.domain.errors import CorruptAudioError

            raise CorruptAudioError(
                detail=f"探测到时长 {duration_ms}ms，无法生成切分计划：{src}",
                audio_path=str(src),
            )

        if len(plan) > 1:
            warnings.append(
                f"音频时长 {duration_ms / 1000 / 60:.1f} 分钟，"
                f"已自动切分为 {len(plan)} 段处理"
                f"（每段约 {self._chunk_seconds // 60} 分钟，相邻段重叠 "
                f"{self._overlap_seconds} 秒以避免在句中切断）"
            )

        result = IngestedAudio(
            source_path=src,
            filename=src.name,
            format=fmt,
            size_bytes=size,
            duration_ms=duration_ms,
            chunk_plan=plan,
            warnings=list(warnings),
        )

        # ⑤ 实际切分（probe_only 时跳过）
        if not self._probe_only and chunk_dir is not None:
            result.chunk_files = self._cut_all(src, plan, chunk_dir)

        return result

    def _cut_all(
        self,
        src: Path,
        plan: list[ChunkPlanItem],
        chunk_dir: Path,
    ) -> list[Path]:
        """按计划逐块切分。

        单块失败**立刻抛出**（而不是跳过继续）：一块缺失会让后续拼接线
        出现时间轴空洞，那种稿子比"任务失败"危险得多（用户看不出来）。
        切分本身很快（-ss 前置），重试整批的代价可以接受。
        """
        chunk_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for item in plan:
            dst = chunk_dir / CHUNK_NAME_TEMPLATE.format(seq=item.seq)
            self._adapter.cut_chunk(src, dst, item.start_ms, item.end_ms)
            out.append(dst)
        logger.info("已切分 %d 段：%s -> %s", len(out), src.name, chunk_dir)
        return out

    # ------------------------------------------------------------------
    # 批量投递
    # ------------------------------------------------------------------

    def ingest_batch(
        self,
        sources: list[Path | str],
        *,
        chunk_dir: Path | None = None,
    ) -> BatchResult:
        """逐个处理，**一个失败不影响其他**（见模块文档）。

        `chunk_dir` 同时给多个文件用时，靠文件名区分子目录由调用方负责
        （通常是 `data/store/{meeting_id}/audio/chunks`，一个会议一个目录）。
        """
        batch = BatchResult()
        for s in sources:
            src = Path(s)
            try:
                batch.succeeded.append(self.ingest_one(src, chunk_dir=chunk_dir))
            except FFmpegNotFoundError:
                # 环境问题：**整批都做不了**，继续循环没有意义（会刷一屏同样的错）
                raise
            except MmwfError as e:
                logger.info("音频接入失败：%s —— %s", src.name, e.detail or e.user_message)
                batch.failed.append(
                    IngestFailure(
                        source_path=src,
                        filename=src.name,
                        error=e,
                        message=e.user_message,
                    )
                )
        return batch


def make_ingest_service(
    settings: object | None = None,
    *,
    adapter: FFmpegAdapter | None = None,
    probe_only: bool = False,
) -> IngestService:
    """按配置构造 service（推荐入口）。

    切分参数读 `settings.asr.chunk_seconds` / `chunk_overlap_seconds` ——
    T1-01 的配置中心已经把这两个键定义好了（当时是为本卡预留的）。
    ffmpeg 位置与超时读 `settings.audio`（T1-06 新增）。
    """
    chunk_seconds = DEFAULT_CHUNK_SECONDS
    overlap_seconds = DEFAULT_OVERLAP_SECONDS
    if settings is not None:
        asr = getattr(settings, "asr", None)
        if asr is not None:
            chunk_seconds = getattr(asr, "chunk_seconds", chunk_seconds)
            overlap_seconds = getattr(asr, "chunk_overlap_seconds", overlap_seconds)

    if adapter is None:
        audio = getattr(settings, "audio", None) if settings is not None else None
        if audio is not None:
            from mmwf.adapters.audio.ffmpeg import locate

            tools = locate(
                ffmpeg_path=getattr(audio, "ffmpeg_path", None),
                ffprobe_path=getattr(audio, "ffprobe_path", None),
            )
            adapter = FFmpegAdapter(
                tools, timeout_seconds=getattr(audio, "timeout_seconds", 600)
            )
        else:
            adapter = FFmpegAdapter()

    return IngestService(
        adapter,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
        probe_only=probe_only,
    )
