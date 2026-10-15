"""FunASR 本地转写引擎（T1-05）。

本模块是**离线合规的关键落点**之一，也是 FunASR 各种坑的收容器。
上层完全不感知 funasr 的存在（`check_layers.py` 的 C01 就是靠这层隔离）。

四个必须处理的 FunASR 缺陷（施工卡「常见坑」原文 + 修法）
-----------------------------------------------------------
① **启动联网检查更新**
   `AutoModel` 构造时会尝试查版本。离线环境会卡住或报错 —— 这正是
   F8.1「断网可运行」的破坏点。
   **修法**：`disable_update=True` 必须显式传。本模块还额外
   `_offline_guard()` 在构造前检查环境变量，避免用户误设代理。

② **运行时从 HuggingFace 拉模型**
   **修法**：模型必须预先离线下载到 `models/`。本模块在 `warmup()` 前
   校验 `model_dir` 存在且非空，不存在就抛 E2001（明确告诉用户缺模型），
   而不是让 FunASR 去联网拉。

③ **热词参数名随版本漂移**（`hotword` / `hotwords`）
   **修法**：`_probe_hotword_param()` 用 `inspect.signature` 探测实际生效的
   参数名，并在启动日志里打印。传参时用探测到的名字。
   **并且**：传了不代表生效（上游会静默忽略，见 ④），所以还要回读验证。

④ **热词静默失效**
   引擎不报错、不告知，就当热词没传。
   **修法**：`hit_hotwords()` 在**转写结果文本里**统计实际命中的热词
   （判据是"结果里出现了这个词"，不是"引擎说它用了"），
   命中为 0 时置 `hotwords_applied=False`，由 `detect_silent_failures`
   产出 `hotwords supplied but zero hit` 告警。

⑤ **长音频 OOM**
   **修法**：**切分是 T1-06（ingest）的职责，本适配器不切**。
   本模块只在 `batch_size_s` 上做安全限幅，并在音频时长明显超过单块
   上限时给出 warning，提示上层应该先切分。

为什么 `transcribe()` 不做"引擎内部重试"
----------------------------------------
重试属于编排层（`graph/nodes`）的职责：它知道幂等键、知道任务状态机
当前在哪个节点、知道要不要写审计。适配器里偷偷重试会让"重试了几次"
这件事无法被记录，出问题时查不出来。
"""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from typing import Any

from mmwf.adapters.asr.base import AsrProviderError
from mmwf.domain.asr_types import (
    TranscriptResult,
    TranscriptSegmentResult,
    TranscriptWord,
    compute_avg_confidence,
    detect_silent_failures,
    hit_hotwords,
)
from mmwf.domain.errors import AsrNotReadyError

logger = logging.getLogger(__name__)

#: 引擎标识（与 config/models.yaml 的 asr.provider 对应）
PROVIDER_NAME = "funasr_local"

#: 上游模型版本。钉死版本号的原因：FunASR 不同 revision 的输出字段
#: 有差异（`timestamp` 是否为字级、`confidence` 是否存在），
#: 不钉版本会让"昨天能跑今天报错"变成常态。
DEFAULT_MODEL_REVISION = "v2.0.4"

#: 单批处理时长上限（秒）。FunASR 的 `batch_size_s` 是"一次喂多少秒音频"，
#: 官方示例常用 300。超过这个值显存/内存峰值显著上升。
DEFAULT_BATCH_SIZE_S = 300

#: 触发"你该切分了"告警的时长阈值（秒）。
#: 设为 1 小时：T1-06 的切分块是 1800s，留足余量说明"这里还没走切分流程"。
LONG_AUDIO_WARN_SECONDS = 3600

#: 离线保护：这些环境变量一旦被设置，FunASR/HF 可能尝试联网。
_OFFLINE_HOSTILE_ENVS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class FunASRLocalProvider:
    """FunASR / Paraformer 本地识别。

    注意 `device="auto"` 的决策方式：**本项目自己探测**，不交给 FunASR。
    原因：FunASR 的 auto 在"有 CUDA 但显存不足"时会先分配再失败，
    报错信息很不友好。自己探测能在 `health_check()` 里提前如实报告
    "本机无独显，将使用 CPU（预计 1:10 实时率）"，让用户在投递前就知道
    要等多久 —— 这是 F1.3 体验要求的一部分。
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        model_name: str = "paraformer-zh",
        model_dir: Path | str = "./models/funasr/paraformer-zh",
        device: str = "auto",
        *,
        model_revision: str = DEFAULT_MODEL_REVISION,
        batch_size_s: int = DEFAULT_BATCH_SIZE_S,
        vad_model: str | None = "fsmn-vad",
        punc_model: str | None = "ct-punc",
        require_model_dir: bool = True,
    ) -> None:
        self._model_name = model_name
        self._model_dir = Path(model_dir)
        self._device_requested = device
        self._model_revision = model_revision
        self._batch_size_s = batch_size_s
        self._vad_model = vad_model
        self._punc_model = punc_model
        self._require_model_dir = require_model_dir

        self._model: Any = None
        #: 探测到的热词参数名（None = 该版本不支持热词）
        self._hotword_param: str | None = None
        self._hotword_param_probed = False
        #: 实际使用的设备（解析 auto 之后）
        self._device_resolved: str | None = None
        self._last_hit_hotwords: list[str] = []

    # ------------------------------------------------------------------
    # 依赖与设备探测
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """依赖已装 + 模型目录就绪。

        **不加载模型**（见 base.py 的契约说明）。
        `require_model_dir=False` 时只检查 funasr 是否可 import，
        用于契约测试——它需要在不下载任何模型的前提下跑通接口一致性。
        """
        try:
            import funasr  # noqa: F401
        except ImportError:
            return False

        if not self._require_model_dir:
            return True

        return self._model_dir.is_dir() and any(self._model_dir.iterdir())

    def _resolve_device(self) -> str:
        """把 `auto` 解析成具体设备名。

        探测顺序：CUDA 可用性 → 是否够显存 → 退 CPU。
        显存门槛取 2GB：施工卡表格写"有独显 ≥6GB"是推荐档，
        但 Paraformer-Large(220M) 实际 1–2GB 即可，留一倍余量。
        """
        if self._device_resolved is not None:
            return self._device_resolved

        req = self._device_requested
        if req != "auto":
            self._device_resolved = req
            return req

        try:
            import torch

            if torch.cuda.is_available() and torch.cuda.device_count() > 0:
                props = torch.cuda.get_device_properties(0)
                total_gb = props.total_memory / (1024 ** 3)
                if total_gb >= 2.0:
                    self._device_resolved = "cuda:0"
                    logger.info(
                        "检测到 GPU：%s（%.1f GB），使用 cuda:0",
                        props.name, total_gb,
                    )
                    return self._device_resolved
                logger.warning(
                    "检测到 GPU 但显存仅 %.1f GB（<2GB），退回 CPU 以避免 OOM", total_gb
                )
        except ImportError:
            logger.info("未安装 torch，无法探测 GPU；按 CPU 运行")
        except Exception as e:  # noqa: BLE001 - 探测失败不该阻断转写
            logger.warning("GPU 探测失败（%s），按 CPU 运行", e)

        self._device_resolved = "cpu"
        return "cpu"

    def _offline_guard(self) -> None:
        """离线保护：发现代理环境变量时告警。

        为什么不直接清除环境变量：进程环境可能被用户有意设置
        （比如内网镜像代理），强行清除会让本来能连的内网源失效。
        告警 + 后续 `disable_update=True` 才是合适的组合。
        """
        for key in _OFFLINE_HOSTILE_ENVS:
            if os.environ.get(key):
                logger.warning(
                    "检测到 %s=%s —— 离线部署环境不应设置代理，"
                    "可能触发组件尝试联网（F8.1）。已启用 disable_update=True 兜底。",
                    key, os.environ[key],
                )

    # ------------------------------------------------------------------
    # 热词参数探测（施工卡坑 ③）
    # ------------------------------------------------------------------

    def hotword_param_name(self) -> str | None:
        """探测当前 FunASR 版本的热词参数名。

        不同版本用 `hotword` 或 `hotwords`（施工卡明写）。
        用 `inspect.signature` 读 `generate` 的形参表，不靠试错——
        试错会真跑一次推理，代价太高。

        返回 None 表示该版本**不支持**热词（或签名不可读）。
        结果会缓存，并在启动日志中打印实际生效的参数名。
        """
        if self._hotword_param_probed:
            return self._hotword_param

        self._hotword_param_probed = True
        try:
            import funasr

            fn = getattr(funasr.AutoModel, "generate", None)
            if fn is None:
                logger.warning("FunASR.AutoModel 无 generate 方法，无法探测热词参数")
                self._hotword_param = None
                return None
            params = set(inspect.signature(fn).parameters)
        except Exception as e:  # noqa: BLE001 - 探测失败按"不支持"处理
            logger.warning("热词参数探测失败（%s），按不支持处理", e)
            self._hotword_param = None
            return None

        for candidate in ("hotword", "hotwords", "hotword_list"):
            if candidate in params:
                self._hotword_param = candidate
                logger.info(
                    "FunASR 热词参数探测结果：%s（该版本 generate 形参含 %s）",
                    candidate, candidate,
                )
                return candidate

        logger.warning(
            "FunASR 版本不支持热词参数（generate 形参中未见 hotword/hotwords）。"
            "术语库的专名纠正将依赖后处理匹配，人名写法需人工核对。"
        )
        self._hotword_param = None
        return None

    # ------------------------------------------------------------------
    # 模型加载
    # ------------------------------------------------------------------

    def _check_model_dir(self) -> None:
        """模型目录必须预先离线就绪（施工卡坑 ②）。"""
        if not self._require_model_dir:
            return
        d = self._model_dir
        if not d.is_dir():
            raise AsrNotReadyError(
                detail=(
                    f"模型目录不存在：{d}。"
                    "离线部署必须预先下载模型并放入该目录，"
                    "运行时不允许从网络拉取（N4.4/F8.1）。"
                ),
                model=self._model_name,
                model_dir=str(d),
            )
        if not any(d.iterdir()):
            raise AsrNotReadyError(
                detail=f"模型目录为空：{d}",
                model=self._model_name,
                model_dir=str(d),
            )

    def warmup(self) -> None:
        """加载模型。可重复调用（第二次直接返回）。

        `disable_update=True` 与 `disable_pbar=True` **都是显式传的**：
        - `disable_update`：禁止联网查版本 —— 离线环境的关键（坑 ①）；
        - `disable_pbar`：关掉 tqdm 进度条。进度条会往 stderr 写大量
          ANSI 控制字符，在 Electron 子进程管道里会污染日志、
          甚至让日志解析出错。
        """
        if self._model is not None:
            return

        self._offline_guard()
        self._check_model_dir()
        self._resolve_device()
        self.hotword_param_name()   # 提前探测，让日志里能看到

        try:
            from funasr import AutoModel
        except ImportError as e:
            raise AsrNotReadyError(
                detail=f"未安装 funasr：{e}。请按部署手册 §4 安装运行环境。",
                model=self._model_name,
            ) from e

        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "model_revision": self._model_revision,
            "device": self._device_resolved,
            "disable_update": True,     # ← 坑 ①：禁止联网检查更新
            "disable_pbar": True,       # ← 避免 ANSI 污染子进程日志
        }
        # 本地模型目录存在时优先用本地路径（双保险：即使 disable_update 失效，
        # 也不会去 HF 拉；同时避免"本地已有模型却仍走网络缓存目录"）
        if self._require_model_dir and self._model_dir.is_dir():
            kwargs["model"] = str(self._model_dir)
        if self._vad_model:
            kwargs["vad_model"] = self._vad_model
        if self._punc_model:
            kwargs["punc_model"] = self._punc_model

        logger.info(
            "加载 FunASR 模型：model=%s revision=%s device=%s "
            "vad=%s punc=%s disable_update=True",
            kwargs["model"], self._model_revision, self._device_resolved,
            self._vad_model, self._punc_model,
        )
        try:
            self._model = AutoModel(**kwargs)
        except Exception as e:  # noqa: BLE001 - 上游异常类型不稳定，统一转业务码
            raise AsrNotReadyError(
                detail=f"FunASR 模型加载失败：{type(e).__name__}: {e}",
                model=self._model_name,
                model_dir=str(self._model_dir),
                device=self._device_resolved,
            ) from e

    # ------------------------------------------------------------------
    # 转写
    # ------------------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path | str,
        hotwords: list[str] | None = None,
    ) -> TranscriptResult:
        """转写音频（见 base.py 的契约要求）。"""
        path = Path(audio_path)
        if not path.exists():
            from mmwf.domain.errors import FileMissingError

            raise FileMissingError(detail=f"音频文件不存在：{path}", audio_path=str(path))

        self.warmup()

        hotwords = [w.strip() for w in (hotwords or []) if w and w.strip()]
        kwargs: dict[str, Any] = {
            "batch_size_s": self._batch_size_s,
            "sentence_timestamp": True,
        }

        # 坑 ③：用探测到的参数名，而不是猜一个
        param = self.hotword_param_name()
        if hotwords and param:
            kwargs[param] = self._format_hotwords(hotwords)

        warnings: list[str] = []
        duration_ms = _probe_duration_ms(path)
        if duration_ms and duration_ms > LONG_AUDIO_WARN_SECONDS * 1000:
            warnings.append(
                f"audio duration {duration_ms / 1000:.0f}s exceeds "
                f"{LONG_AUDIO_WARN_SECONDS}s; ingest should chunk it first"
                "（音频超过单块上限，应先经 ingest 切分后再投递，"
                "否则可能内存不足）"
            )
        if hotwords and not param:
            warnings.append(
                "engine version does not accept hotword param; "
                "glossary matching will rely on post-processing"
                "（当前引擎版本不支持热词，术语纠正只能依赖后处理匹配）"
            )

        logger.info(
            "开始转写：%s（%.1f MB）hotwords=%d param=%s",
            path.name, path.stat().st_size / 1024 / 1024, len(hotwords), param,
        )
        try:
            raw = self._model.generate(input=str(path), **kwargs)
        except Exception as e:  # noqa: BLE001
            from mmwf.domain.errors import CorruptAudioError

            raise CorruptAudioError(
                detail=f"FunASR 识别失败：{type(e).__name__}: {e}",
                audio_path=str(path),
                engine=self.name,
            ) from e

        segments = self._normalize(raw)
        # 坑 ④：以"结果文本里是否真的出现了热词"判定是否生效
        hits = hit_hotwords(segments, hotwords)
        self._last_hit_hotwords = hits
        hotwords_applied = bool(hotwords) and bool(hits)

        warnings.extend(
            detect_silent_failures(
                segments,
                hotwords=hotwords,
                hotwords_applied=hotwords_applied,
            )
        )

        result = TranscriptResult(
            segments=tuple(segments),
            engine=self.name,
            model=self._model_name,
            avg_confidence=compute_avg_confidence(segments),
            hotwords_applied=hotwords_applied,
            warnings=tuple(warnings),
            raw_meta={
                "device": self._device_resolved,
                "model_revision": self._model_revision,
                "hotword_param": param,
                "hotwords_supplied": len(hotwords),
                "hotwords_hit": hits,
            },
        )
        # 不在这里 validated()：让编排层决定"错位就停"还是"记录并继续"。
        # 但默认路径（run_transcribe）会调它 —— 见模块末尾的便捷函数。
        return result

    @staticmethod
    def _format_hotwords(hotwords: list[str]) -> str:
        """热词格式：空格分隔（FunASR 官方示例格式）。

        顺便去重并保序：术语库里同一个词可能出现多次（别名展开），
        重复传给引擎只会浪费上下文预算。
        """
        seen: dict[str, None] = {}
        for w in hotwords:
            seen.setdefault(w, None)
        return " ".join(seen)

    # ------------------------------------------------------------------
    # 输出规范化
    # ------------------------------------------------------------------

    def _normalize(self, raw: Any) -> list[TranscriptSegmentResult]:
        """把 FunASR 的返回翻译成**领域类型**。

        FunASR 的返回是 `[{"key": ..., "text": ..., "timestamp": [...]}]`，
        其中 `timestamp` 的形态随版本变化（可能是逐字、也可能是逐句）。

        **刻意直接产出 `TranscriptSegmentResult`，不产出中间字典**：
        早先的版本先转成 dict 再交给 `hit_hotwords`/`detect_silent_failures`，
        结果那两个函数按 `TranscriptSegmentResult` 实现、拿到 dict 就崩
        （`AttributeError: 'dict' object has no attribute 'text'`）。
        "两种表现同一个概念的数据形状" 是这类 bug 的温床 ——
        统一成一种，编译器/测试才帮得上忙。

        `seq` 在这里按原顺序递增赋值；真正的规范化（排序、丢空段、重排 seq）
        由调用方走 `TranscriptResult.from_raw_segments()` 做，避免两处各做一半。
        """
        # FunASR 返回 list（批量）也可能是 dict（单条）
        items: list[Any]
        if isinstance(raw, dict):
            items = [raw]
        elif isinstance(raw, list):
            items = raw
        else:
            raise AsrProviderError(
                f"FunASR 返回类型不可识别：{type(raw).__name__}（期望 list 或 dict）"
            )

        out: list[TranscriptSegmentResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text is None:
                continue
            ts = item.get("timestamp") or []
            start_ms, end_ms = _span_from_timestamp(ts)
            conf = item.get("confidence")
            words = tuple(
                TranscriptWord(
                    text=str(w["text"]),
                    start_ms=int(w["start_ms"]),
                    end_ms=int(w["end_ms"]),
                )
                for w in _words_from_timestamp(ts, str(text))
            )
            out.append(
                TranscriptSegmentResult(
                    seq=len(out) + 1,
                    text=str(text),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    confidence=None if conf is None else float(conf),
                    words=words,
                )
            )
        return out

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """结构化健康状况（SAD §9.2 的 `asr_quality` 字段直接用它）。"""
        available = self.available()
        device = self._device_resolved
        if device is None and available:
            try:
                device = self._resolve_device()
            except Exception:  # noqa: BLE001 - 自检不该因为探测失败而炸
                device = "unknown"

        # 热词支持性探测：只做签名读取，不加载模型
        hotword_supported = False
        if available:
            hotword_supported = self.hotword_param_name() is not None

        status = "ok" if available else "unavailable"
        return {
            "available": available,
            "model": self._model_name,
            "model_revision": self._model_revision,
            "model_dir": str(self._model_dir),
            "model_dir_ready": self._model_dir.is_dir() and any(self._model_dir.iterdir())
            if self._require_model_dir else True,
            "device": device or self._device_requested,
            "device_requested": self._device_requested,
            "hotword_supported": hotword_supported,
            "hotword_param": self._hotword_param,
            "loaded": self._model is not None,
            "last_hit_hotwords": list(self._last_hit_hotwords),
            "status": status,
            # 无独显时的预期速度（施工卡表格），让用户投递前就有心理预期
            "expected_speed": (
                "GPU 档位：约 1:30 实时率（2 小时音频 ≈ 4 分钟）"
                if (device or "").startswith("cuda")
                else "CPU 档位：约 1:10 实时率（2 小时音频 ≈ 70 分钟）"
            ),
        }

    def __repr__(self) -> str:
        return (
            f"FunASRLocalProvider(model={self._model_name!r}, "
            f"device={self._device_resolved or self._device_requested!r}, "
            f"loaded={self._model is not None})"
        )


# ---------------------------------------------------------------------------
# FunASR 返回格式解析（纯函数，独立可测）
# ---------------------------------------------------------------------------

def _span_from_timestamp(ts: Any) -> tuple[int, int]:
    """从 FunASR 的 timestamp 字段推出 [起, 止]（毫秒）。

    FunASR 的 timestamp 是 `[[start_ms, end_ms], ...]`（单位**毫秒**，
    早期版本用过别的单位）。若为空或形状异常，返回 (0, 0) ——
    交由上层的 `validate()` 去判"时间戳全 0"这个更明确的问题，
    本函数不自作聪明猜一个值。

    三种真实形状：
      ① `[[0, 180], [180, 460]]` —— 标准逐字；取首元素开头、末元素结尾。
      ② `[100, 250]` —— 有的版本单句直接给一对，**只出现一次**；
         此时首末是同一个数，不能当成"start=100, end=100"，
         而要认出这是 (start, end)。
      ③ 畸形（`[[1]]`、`[[]]`、`[["a","b"]]`、非序列）→ (0, 0)。
     """
    if not isinstance(ts, (list, tuple)) or not ts:
        return 0, 0

    # 形状 ②：整条就是一个长度 2 的数值对（且不是 [[s,e],...] 的嵌套形态）
    if len(ts) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ts):
        return int(ts[0]), int(ts[1])

    first, last = ts[0], ts[-1]
    # 每个元素必须是"长度 ≥2 的序列"，否则视为畸形 —— 不猜
    if not isinstance(first, (list, tuple)) or len(first) < 2:
        return 0, 0
    if not isinstance(last, (list, tuple)) or len(last) < 2:
        return 0, 0
    try:
        start = int(first[0])
        end = int(last[-1])
    except (TypeError, ValueError, IndexError):
        return 0, 0
    return start, end


def _words_from_timestamp(ts: Any, text: str) -> list[dict[str, Any]]:
    """把逐字时间戳与文本配对成 words 列表。

    这里有一个必须处理的**上游不一致**：`len(timestamp)` 与 `len(text)`
    经常不相等（施工卡 ① 说的就是这个）。本函数**不做裁剪也不报错**，
    只按较短的那个配对 —— 因为：
      · 报错会把"轻微不一致"（分词器差异）误判成严重故障；
      · 强行对齐（比如补 0）会伪造出不存在的时间戳。
    真实的不一致程度由 `TranscriptResult.validate()` 用**容差**判定，
    超容差才抛 E2003。职责分离：本函数负责"尽力还原"，
    validate 负责"判定严重性"。
    """
    if not isinstance(ts, (list, tuple)) or not ts or not text:
        return []
    n = min(len(ts), len(text))
    words: list[dict[str, Any]] = []
    for i in range(n):
        pair = ts[i]
        try:
            s, e = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        words.append({"text": text[i], "start_ms": s, "end_ms": e})
    return words


def _avg_of(segments: list[TranscriptSegmentResult]) -> float | None:
    """保留为薄封装，便于测试直接调用（实现已统一到领域层）。"""
    return compute_avg_confidence(segments)


def _probe_duration_ms(path: Path) -> int:
    """尽力探测音频时长。失败返回 0（不阻断转写）。

    这里不用 ffprobe：T1-05 阶段 ffprobe 还没接入（T1-06 才有 ingest）。
    只用标准库 `wave` 读 wav 头，其他格式返回 0。
    拿到时长只是为了给出"你该切分了"的告警，拿不到就不给 ——
    **不引入新依赖**比拿准这个数更重要。
    """
    if path.suffix.lower() != ".wav":
        return 0
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            return int(w.getnframes() / w.getframerate() * 1000)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def run_transcribe(
    audio_path: Path | str,
    hotwords: list[str] | None = None,
    *,
    model_name: str = "paraformer-zh",
    model_dir: Path | str = "./models/funasr/paraformer-zh",
    device: str = "auto",
    strict: bool = True,
) -> TranscriptResult:
    """一行完成转写（含静默失效校验）。

    `strict=True`（默认）会在结果未通过校验时抛 E2003（FATAL）。
    这是默认值，因为**放一份时间戳错位的稿子继续流转，比直接停下危险**。
    若编排层需要先拿到结果再决定（比如把问题记进审计后继续），传 `strict=False`。
    """
    provider = FunASRLocalProvider(model_name=model_name, model_dir=model_dir, device=device)
    result = provider.transcribe(audio_path, hotwords=hotwords)
    return result.validated() if strict else result
