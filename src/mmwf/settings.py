"""配置中心（T1-01）。

设计目标：所有配置可外部修改，代码不硬编码任何路径、阈值、模型名（N5.2）。
被 SAD §14 C11 约束：app.host 必须为回环地址。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 子配置模型
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    """应用级配置。"""

    name: str = "mmwf"
    version: str = "1.0.0"
    host: str = "127.0.0.1"
    port: int = 0
    data_dir: Path = Path("./data")
    log_dir: Path = Path("./data/logs")
    max_concurrent_tasks: int = 1
    audit_retention_days: int = 1095

    model_config = {"frozen": True}

    @field_validator("host")
    @classmethod
    def _host_must_be_loopback(cls, v: str) -> str:
        """SAD C11：服务只允许监听回环地址。

        这一条不是可以配置的偏好，是合规红线——一旦监听 0.0.0.0，
        系统就不再是"全程离线、数据不出设备"的形态。
        """
        allowed = {"127.0.0.1", "::1", "localhost"}
        if v not in allowed:
            raise ValueError(
                f"app.host 只允许回环地址 {sorted(allowed)}，当前为 {v!r}。"
                "（架构约束 C11：服务不得对外监听）"
            )
        return v

    @field_validator("port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        if not (0 <= v <= 65535):
            raise ValueError(f"端口必须在 0-65535 之间，当前为 {v}")
        return v


class AsrConfig(BaseModel):
    provider: str = "funasr_local"
    model: str = "paraformer-zh"
    model_dir: Path = Path("./models/funasr/paraformer-zh")
    device: Literal["auto", "cpu", "cuda"] = "auto"
    chunk_seconds: int = 1800
    chunk_overlap_seconds: int = 50

    model_config = {"frozen": True}

    @field_validator("chunk_seconds")
    @classmethod
    def _chunk_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("chunk_seconds 必须为正整数")
        return v

    @field_validator("chunk_overlap_seconds")
    @classmethod
    def _overlap_less_than_chunk(cls, v: int, info: Any) -> int:
        chunk = info.data.get("chunk_seconds")
        if chunk is not None and v >= chunk:
            raise ValueError(
                f"chunk_overlap_seconds({v}) 必须小于 chunk_seconds({chunk})，否则切分会死循环"
            )
        return v


class AudioConfig(BaseModel):
    """音频处理（ffmpeg）配置（T1-06）。

    `ffmpeg_path` / `ffprobe_path` 刻意允许为 None：
    本机装了 ffmpeg 并进了 PATH 时无需配置；涉密部署则必须指向受控目录，
    那时它们是绝对路径。适配器按"配置 -> PATH -> imageio-ffmpeg 兜底"的顺序找，
    这个配置就是第一优先级。
    """

    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    timeout_seconds: int = 600

    model_config = {"frozen": True}

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("audio.timeout_seconds 必须为正整数")
        return v


class LlmConfig(BaseModel):
    provider: str = "ollama_local"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:14b-instruct-q5_K_M"
    temperature: float = 0.2
    num_ctx: int = 8192
    timeout_seconds: int = 600

    model_config = {"frozen": True}

    @field_validator("base_url")
    @classmethod
    def _llm_must_be_local(cls, v: str) -> str:
        """本地大模型服务必须在本机，不允许指向外部地址。"""
        if not any(h in v for h in ("127.0.0.1", "localhost", "::1")):
            raise ValueError(f"llm.base_url 必须是本机地址，当前为 {v!r}（离线要求）")
        return v


class ExportConfig(BaseModel):
    provider: str = "docx_exporter"
    default_template: Path = Path("./config/templates/regular-meeting.yaml")
    font_fallback_policy: Literal["warn", "fallback", "fail"] = "warn"
    enable_doc_export: bool = True
    soffice_path: Path | None = Path("C:/Program Files/LibreOffice/program/soffice.exe")

    model_config = {"frozen": True}


class ExtractionConfig(BaseModel):
    class ChunkConfig(BaseModel):
        target_chars: int = 1500
        min_chars: int = 1000
        max_chars: int = 2000
        overlap_chars: int = 200
        model_config = {"frozen": True}

        @field_validator("overlap_chars")
        @classmethod
        def _overlap_less_than_min(cls, v: int, info: Any) -> int:
            mn = info.data.get("min_chars")
            if mn is not None and v >= mn:
                raise ValueError(f"overlap_chars({v}) 必须小于 min_chars({mn})")
            return v

    class MergeConfig(BaseModel):
        topic_similarity_threshold: float = 0.82
        resolution_dedup_threshold: float = 0.90
        model_config = {"frozen": True}

    class RetryConfig(BaseModel):
        max_schema_retries: int = 2
        model_config = {"frozen": True}

    class GlossaryConfig(BaseModel):
        """术语库段（T1-07）。

        `hotword_limit` 是**引擎侧**的硬约束，不是本项目的性能参数：
        FunASR 的热词对数量敏感，超限可能**整体不生效**（静默失效）。
        放在配置里是为了能在不重新打包的情况下按实际引擎版本调整
        （N5.2 可热改），默认取施工卡原文的 2000。
        """

        hotword_limit: int = 2000
        #: 是否在转写后自动做术语回改。关掉只影响"回改"，
        #: 热词注入照旧 —— 两者是独立的两条腿。
        auto_correct: bool = True

        @field_validator("hotword_limit")
        @classmethod
        def _limit_positive(cls, v: int) -> int:
            if v <= 0:
                raise ValueError(f"hotword_limit 必须为正数，当前 {v}")
            return v

        model_config = {"frozen": True}

    engine: Literal["naive", "llm"] = "naive"
    chunk: ChunkConfig = ChunkConfig()
    merge: MergeConfig = MergeConfig()
    retry: RetryConfig = RetryConfig()
    glossary: GlossaryConfig = GlossaryConfig()

    model_config = {"frozen": True}


class ThresholdsConfig(BaseModel):
    class AsrThresholds(BaseModel):
        low_confidence: float = 0.70
        warn_avg_confidence: float = 0.85
        warn_low_conf_ratio: float = 0.15
        model_config = {"frozen": True}

    class GateThresholds(BaseModel):
        approval_timeout_hours: int = 72
        model_config = {"frozen": True}

    class ValidationThresholds(BaseModel):
        min_owner_missing_ratio: float = 0.0
        model_config = {"frozen": True}

    asr: AsrThresholds = AsrThresholds()
    gate: GateThresholds = GateThresholds()
    validation: ValidationThresholds = ValidationThresholds()

    model_config = {"frozen": True}


class Settings(BaseModel):
    """全局配置聚合。frozen=True：配置在进程内不可变，避免运行中被意外改写。"""

    app: AppConfig
    asr: AsrConfig
    audio: AudioConfig
    llm: LlmConfig
    export: ExportConfig
    extraction: ExtractionConfig
    thresholds: ThresholdsConfig
    config_dir: Path

    model_config = {"frozen": True}

    def resolve_path(self, p: Path) -> Path:
        """把相对路径按 config_dir 的父目录（项目根）解析为绝对路径。

        为什么必须这么干：Path 类型会保留相对路径，子进程启动后 CWD 变了，
        相对路径的基准就变了。所有要用到路径的地方一律走这个方法。
        """
        if p.is_absolute():
            return p
        root = self.config_dir.parent if self.config_dir.name == "config" else self.config_dir
        return (root / p).resolve()


# ---------------------------------------------------------------------------
# 加载逻辑
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_DIRNAME = "config"

# 允许通过环境变量覆盖配置文件目录（部署时有用）
ENV_CONFIG_DIR = "MMWF_CONFIG_DIR"


def _find_config_dir(explicit: Path | None = None) -> Path:
    """定位配置目录。优先级：显式参数 > 环境变量 > 项目内默认位置。"""
    if explicit is not None:
        d = Path(explicit)
    elif os.environ.get(ENV_CONFIG_DIR):
        d = Path(os.environ[ENV_CONFIG_DIR])
    else:
        # src/mmwf/settings.py -> src/mmwf -> src -> <project root> -> config
        d = Path(__file__).resolve().parents[2] / DEFAULT_CONFIG_DIRNAME

    if not d.is_dir():
        raise FileNotFoundError(f"配置目录不存在：{d}")
    return d.resolve()


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML。显式 utf-8 —— Windows 默认 GBK，不指定必乱码。"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件缺失：{path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射：{path}")
    return data


def _require_key(data: dict[str, Any], key: str, source: Path) -> dict[str, Any]:
    if key not in data:
        raise ValueError(f"配置文件 {source} 缺少顶层键 {key!r}")
    value = data[key]
    if not isinstance(value, dict):
        raise ValueError(f"配置文件 {source} 的 {key!r} 必须是映射")
    return value


def load_settings(config_dir: Path | None = None) -> Settings:
    """从磁盘加载全部配置。每次调用都重新读盘，不做缓存。"""
    cfg_dir = _find_config_dir(config_dir)

    app_file = cfg_dir / "app.yaml"
    app_doc = _load_yaml(app_file)
    app_raw = _require_key(app_doc, "app", app_file)
    # audio 段与 app 同文件；允许缺失，以便"还没加这段"的旧配置文件仍能加载。
    audio_raw = app_doc.get("audio") or {}

    models_raw = _load_yaml(cfg_dir / "models.yaml")
    asr_raw = _require_key(models_raw, "asr", cfg_dir / "models.yaml")
    llm_raw = _require_key(models_raw, "llm", cfg_dir / "models.yaml")
    export_raw = _require_key(models_raw, "export", cfg_dir / "models.yaml")

    extraction_raw = _require_key(
        _load_yaml(cfg_dir / "extraction.yaml"), "extraction", cfg_dir / "extraction.yaml"
    )
    thresholds_raw = _require_key(
        _load_yaml(cfg_dir / "thresholds.yaml"), "thresholds", cfg_dir / "thresholds.yaml"
    )

    return Settings(
        app=AppConfig(**app_raw),
        asr=AsrConfig(**asr_raw),
        audio=AudioConfig(**audio_raw),
        llm=LlmConfig(**llm_raw),
        export=ExportConfig(**export_raw),
        extraction=ExtractionConfig(**extraction_raw),
        thresholds=ThresholdsConfig(**thresholds_raw),
        config_dir=cfg_dir,
    )


@lru_cache(maxsize=1)
def get_settings(config_dir: Path | None = None) -> Settings:
    """进程内单例。传入 config_dir 时不使用缓存，便于测试与热加载。"""
    if config_dir is not None:
        return load_settings(config_dir)
    return load_settings(None)


def reload_settings(config_dir: Path | None = None) -> Settings:
    """清缓存后重新加载，用于运行期热改配置（N5.2）。"""
    get_settings.cache_clear()
    return get_settings(config_dir)
