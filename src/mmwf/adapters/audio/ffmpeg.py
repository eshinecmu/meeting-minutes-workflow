"""ffmpeg 定位与调用（T1-06 适配层）。

为什么把 ffmpeg 调用单独放一层
-----------------------------
`subprocess` 是"外部能力"，领域层不允许出现（`check_layers.py` 的 C01）。
更重要的是：**ffmpeg 是不可信外部输入**。它的 stderr 里会带
文件路径、编解码器名、甚至原始报错，直接透给用户既违规（N3.3）
也难懂。所有调用都在这里包一层，把失败翻译成项目的业务错误码。

ffmpeg 从哪来
-------------
部署手册 §2.1 交付包清单第 7 项：`ffmpeg.exe` + `ffprobe.exe`
**由部署方提供**（附 SHA256），不是 pip 依赖。所以本适配器的核心
职责之一是 `locate()`：按"配置 -> PATH -> imageio-ffmpeg 兜底"的顺序
找到它们，找不到时给出**可执行的**排查指引，而不是一句"ffmpeg not found"。

三种来源的优先级与理由
----------------------
1. **配置显式指定**（`app.yaml` 的 `ffmpeg_path`）—— 涉密环境常见做法：
   ffmpeg 放在受控目录，不放 PATH。
2. **PATH** —— 开发机常见。
3. **`imageio_ffmpeg` 兜底** —— 该包自带一份静态 ffmpeg（7.1）。
   注意它**只带 ffmpeg，不带 ffprobe**，所以走这条路时
   时长探测要退到 ffmpeg 自己的输出解析（见 `_probe_via_ffmpeg`）。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mmwf.domain.errors import CorruptAudioError, FileMissingError
from mmwf.domain.ingest.planner import TARGET_CHANNELS, TARGET_SAMPLE_RATE

logger = logging.getLogger(__name__)

#: 调用超时（秒）。切成小片段后单次调用很快；这个值是防"进程卡死拖垮整个任务"。
DEFAULT_TIMEOUT_SECONDS = 600


class FFmpegNotFoundError(RuntimeError):
    """ffmpeg/ffprobe 不可用。**不是业务错误** —— 是部署问题。

    刻意不继承 MmwfError：业务错误有面向用户的文案与错误码，
    而"ffmpeg 没装"需要技术人员按部署手册处理，属于环境问题。
    上层（service）会把它转成系统级提示。
    """


@dataclass(frozen=True)
class FFmpegTools:
    """一次定位的结果。`ffprobe` 可能为 None（imageio-ffmpeg 兜底路径）。"""

    ffmpeg: str
    ffprobe: str | None
    source: str          # 便于日志与自检报告说明"从哪儿找到的"

    @property
    def has_probe(self) -> bool:
        return self.ffprobe is not None


def locate(
    *,
    ffmpeg_path: Path | str | None = None,
    ffprobe_path: Path | str | None = None,
    allow_imageio_fallback: bool = True,
) -> FFmpegTools:
    """按优先级定位 ffmpeg / ffprobe。

    Raises:
        FFmpegNotFoundError: 全部途径都失败，附带排查指引。
    """
    # ① 显式配置
    if ffmpeg_path:
        p = Path(ffmpeg_path)
        if not p.is_file():
            raise FFmpegNotFoundError(
                f"配置中指定的 ffmpeg 不存在：{p}。"
                "请修正 config/app.yaml 的 ffmpeg_path，或清空该配置改用 PATH。"
            )
        probe: str | None = None
        if ffprobe_path:
            pp = Path(ffprobe_path)
            if not pp.is_file():
                raise FFmpegNotFoundError(
                    f"配置中指定的 ffprobe 不存在：{pp}。"
                    "它与 ffmpeg 通常在同一目录（部署手册 §2.1 第 7 项）。"
                )
            probe = str(pp)
        else:
            guess = p.with_name(
                "ffprobe.exe" if p.suffix.lower() == ".exe" else "ffprobe"
            )
            probe = str(guess) if guess.is_file() else None
        return FFmpegTools(ffmpeg=str(p), ffprobe=probe, source="config")

    # ② PATH
    ff = shutil.which("ffmpeg")
    if ff:
        fp = shutil.which("ffprobe")
        return FFmpegTools(ffmpeg=ff, ffprobe=fp, source="PATH")

    # ③ imageio-ffmpeg 兜底
    if allow_imageio_fallback:
        try:
            import imageio_ffmpeg  # type: ignore

            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and Path(exe).is_file():
                logger.info(
                    "经 PATH 未找到 ffmpeg，改用 imageio-ffmpeg 自带版本：%s"
                    "（注意：该包不含 ffprobe，时长探测将改用 ffmpeg 输出解析）",
                    exe,
                )
                return FFmpegTools(ffmpeg=exe, ffprobe=None, source="imageio-ffmpeg")
        except Exception as e:  # noqa: BLE001 - 兜底失败不该盖住主错误
            logger.debug("imageio-ffmpeg 兜底不可用：%s", e)

    raise FFmpegNotFoundError(
        "未找到 ffmpeg。请按部署手册 §2.1 的交付包清单第 7 项，"
        "把 ffmpeg.exe 与 ffprobe.exe 放入受控目录后在 config/app.yaml "
        "的 ffmpeg_path 指向它；或将其加入系统 PATH。\n"
        "（开发环境可 pip install imageio-ffmpeg 临时兜底，"
        "但该包不含 ffprobe，且不适用于涉密部署。）"
    )


class FFmpegAdapter:
    """ffprobe 探测时长 + ffmpeg 切分。

    本类**无状态**（除了工具路径），可以安全地在多任务间共享。
    """

    def __init__(
        self,
        tools: FFmpegTools | None = None,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._tools = tools
        self._timeout = timeout_seconds

    @property
    def tools(self) -> FFmpegTools:
        """延迟定位 —— 构造 adapter 时不该强制要求本机已装 ffmpeg。

        这样自检接口可以在没装 ffmpeg 的机器上如实报告
        "ffmpeg 不可用"，而不是在 import 阶段就炸掉。
        """
        if self._tools is None:
            self._tools = locate()
        return self._tools

    def available(self) -> bool:
        """探测可用性（不抛错，供 `/system/check` 调用）。"""
        try:
            t = self.tools
        except FFmpegNotFoundError:
            return False
        return Path(t.ffmpeg).is_file()

    # ------------------------------------------------------------------
    # 时长探测
    # ------------------------------------------------------------------

    def probe_duration_ms(self, audio_path: Path | str) -> int:
        """探测音频时长（毫秒）。

        两条路径：
          · 有 ffprobe → 解析 `-show_format` 的 JSON（首选，准确、含元数据）；
          · 无 ffprobe（imageio-ffmpeg 兜底）→ 解析 ffmpeg 的 stderr。

        Raises:
            FileMissingError: 文件不存在（E1002）。
            CorruptAudioError: 探测不出时长（E1003）—— 对上层而言
                "读不出时长"和"文件损坏"是同一件事：这个文件用不了。
        """
        path = Path(audio_path)
        if not path.is_file():
            raise FileMissingError(detail=f"音频文件不存在：{path}", audio_path=str(path))

        tools = self.tools
        if tools.has_probe:
            return self._probe_via_ffprobe(path, tools.ffprobe)  # type: ignore[arg-type]
        return self._probe_via_ffmpeg(path, tools.ffmpeg)

    def _probe_via_ffprobe(self, path: Path, ffprobe: str) -> int:
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    str(path),
                ],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise CorruptAudioError(
                detail=f"ffprobe 探测超时（>{self._timeout}s）：{path}",
                audio_path=str(path),
            ) from e
        except OSError as e:
            # ffprobe 路径失效/无执行权限 —— 环境问题，不是文件问题
            raise FFmpegNotFoundError(f"无法执行 ffprobe（{ffprobe}）：{e}") from e

        if proc.returncode != 0:
            stderr = _decode(proc.stderr)
            raise CorruptAudioError(
                detail=(
                    f"ffprobe 无法解析该音频（退出码 {proc.returncode}）：{path}\n"
                    f"stderr: {stderr.strip()[:400]}"
                ),
                audio_path=str(path),
            )

        try:
            info = json.loads(_decode(proc.stdout) or "{}")
            duration = float(info["format"]["duration"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            raise CorruptAudioError(
                detail=f"ffprobe 输出无法解析出时长：{e}；原始输出首 200 字：{_decode(proc.stdout)[:200]}",
                audio_path=str(path),
            ) from e

        if duration <= 0:
            raise CorruptAudioError(
                detail=f"探测到时长 {duration}s（<=0），文件可能为空或损坏：{path}",
                audio_path=str(path),
            )
        return int(round(duration * 1000))

    #: 从 ffmpeg stderr 里抽时长。形如 `Duration: 00:05:12.34, ...`
    _DURATION_RE = re.compile(
        r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)"
    )

    def _probe_via_ffmpeg(self, path: Path, ffmpeg: str) -> int:
        """无 ffprobe 时退而求其次：让 ffmpeg 读一遍头并解析 stderr。

        `-i <file>` 不带输出文件时，ffmpeg 会把元信息打到 stderr 后报错退出 ——
        这是它的既定行为，不是异常，所以 `check=False`。
        """
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise CorruptAudioError(
                detail=f"ffmpeg 探测超时（>{self._timeout}s）：{path}",
                audio_path=str(path),
            ) from e
        except OSError as e:
            raise FFmpegNotFoundError(f"无法执行 ffmpeg（{ffmpeg}）：{e}") from e

        m = self._DURATION_RE.search(_decode(proc.stderr))
        if not m:
            raise CorruptAudioError(
                detail=(
                    f"ffmpeg 未能报告时长（可能不是有效音频）：{path}\n"
                    f"stderr 首 400 字：{_decode(proc.stderr).strip()[:400]}"
                ),
                audio_path=str(path),
            )
        hours, minutes, seconds = m.group(1), m.group(2), m.group(3)
        total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if total <= 0:
            raise CorruptAudioError(
                detail=f"探测到时长 {total}s（<=0）：{path}", audio_path=str(path)
            )
        return int(round(total * 1000))

    # ------------------------------------------------------------------
    # 切分
    # ------------------------------------------------------------------

    def cut_chunk(
        self,
        src: Path | str,
        dst: Path | str,
        start_ms: int,
        end_ms: int,
    ) -> None:
        """把 `src` 的 `[start_ms, end_ms)` 切出来，统一转成 16kHz 单声道 wav。

        参数顺序的三个专业细节（每一个都是坑）：

        1. **`-ss` 放在 `-i` 之前**：这是"快速定位"——ffmpeg 直接跳到关键帧，
           不做完整解码。放在 `-i` 之后是精确到帧但要从头解一遍。
           施工卡明确："转写场景对精度要求不高，用前置即可"。
           代价是切点可能偏几十毫秒 —— 对 ASR 无影响，且重叠区覆盖了这点漂移。

        2. **`-t` 而非 `-to`**：`-t` 是时长，`-to` 是结束时间。用 `-ss` 前置时
           为了明确语义，一律给时长（`end-start`），避免"相对于哪里"的歧义。

        3. **`-ac 1 -ar 16000`**：ASR 模型的标准输入。提前转换让引擎不必自己重采样，
           也避免引擎用不同算法重采样导致质量差异（同一份音频在不同机器上结果不同）。

        Raises:
            FileMissingError / CorruptAudioError
        """
        src_path = Path(src)
        dst_path = Path(dst)
        if not src_path.is_file():
            raise FileMissingError(detail=f"待切分音频不存在：{src_path}", audio_path=str(src_path))
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError(f"切分区间无效：start={start_ms}, end={end_ms}")

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        duration_s = (end_ms - start_ms) / 1000.0

        cmd = [
            self.tools.ffmpeg,
            "-nostdin",           # 禁止读取 stdin：作为服务运行时 stdin 不可用会挂
            "-y",                 # 覆盖已存在的目标（幂等重跑要求）
            "-ss", f"{start_ms / 1000:.3f}",
            "-t", f"{duration_s:.3f}",
            "-i", str(src_path),
            "-vn",                # 丢掉视频流：有些 m4a 带封面图，不丢会干扰
            "-ac", str(TARGET_CHANNELS),
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le",  # 显式指定编码器，不依赖目标扩展名推断
            "-f", "wav",
            str(dst_path),
        ]
        logger.debug("切分片段：%s [%d, %d) -> %s", src_path.name, start_ms, end_ms, dst_path.name)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise CorruptAudioError(
                detail=f"切分超时（>{self._timeout}s）：{src_path} [{start_ms}, {end_ms})",
                audio_path=str(src_path),
            ) from e
        except OSError as e:
            raise FFmpegNotFoundError(f"无法执行 ffmpeg：{e}") from e

        if proc.returncode != 0:
            raise CorruptAudioError(
                detail=(
                    f"切分失败（退出码 {proc.returncode}）："
                    f"{src_path} [{start_ms}, {end_ms})\n"
                    f"stderr 首 400 字：{_decode(proc.stderr).strip()[:400]}"
                ),
                audio_path=str(src_path),
                start_ms=start_ms,
                end_ms=end_ms,
            )

        # 切完必须存在且非空，否则后面对着一个空 wav 转写会得到"零段落"，
        # 报出来的错会指向转写而不是切分 —— 在这里挡住能省一次排查。
        if not dst_path.is_file() or dst_path.stat().st_size == 0:
            raise CorruptAudioError(
                detail=f"切分产物为空：{dst_path}（源 {src_path} [{start_ms}, {end_ms})）",
                audio_path=str(src_path),
            )

    def version(self) -> str:
        """取 ffmpeg 版本串（自检报告用）。失败返回 'unknown'，不抛错。"""
        try:
            proc = subprocess.run(
                [self.tools.ffmpeg, "-version"],
                capture_output=True, timeout=30, check=False,
            )
        except Exception:  # noqa: BLE001 - 自检不该因为拿版本失败而炸
            return "unknown"
        first = _decode(proc.stdout).splitlines()[0] if proc.stdout else ""
        return first.strip() or "unknown"


def _decode(raw: bytes | str | None) -> str:
    """把子进程输出解成文本。

    为什么不用 `text=True` 直接让 subprocess 解码：ffmpeg 的 stderr 在
    某些编解码器下会混入非 UTF-8 字节，`text=True` 会直接抛
    UnicodeDecodeError 把异常盖住。这里显式 `errors="replace"` ——
    排查信息里出现几个 `?` 可以接受，"拿不到错误信息"不可以。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")
