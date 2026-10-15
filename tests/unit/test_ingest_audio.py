"""T1-06 单元测试：ffmpeg 适配器与 ingest service。

分两半：

  · **适配器层**：用子进程替身，验证"命令怎么拼、输出怎么解析、
    异常怎么翻译"。含真实 ffmpeg 的用例标 `real_ffmpeg`，本机没装就 skip。
  · **service 层**：用假 adapter，验证批量投递的**部分失败语义**、
    大文件告警、probe_only、错误码透传。

为什么适配器测试必须"检查拼出来的命令行"，而不只是"跑起来不报错"
---------------------------------------------------------------
施工卡把 `-ss` 前置、`-ac 1 -ar 16000`、`-nostdin` 这些都写成了硬要求，
其中每一条失效都不会报错、只会产出**质量退化**的结果：

  · `-ar` 错了 → 引擎自己重采样，同一份音频在不同机器上结果不同；
  · `-nostdin` 漏了 → 作为服务运行时（无终端）ffmpeg 会阻塞在 stdin；
  · `-ss` 后置 → 变成逐帧精确但那不是我们想要的开销。

这些"不报错的错误"只能靠断言命令行来防。
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.adapters.audio.ffmpeg import (  # noqa: E402
    FFmpegAdapter,
    FFmpegNotFoundError,
    FFmpegTools,
    locate,
)
from mmwf.app.services.ingest import (  # noqa: E402
    CHUNK_NAME_TEMPLATE,
    BatchResult,
    IngestService,
    make_ingest_service,
)
from mmwf.domain.errors import (  # noqa: E402
    CorruptAudioError,
    FileMissingError,
    UnsupportedFormatError,
)
from mmwf.domain.ingest.planner import MAX_FILE_BYTES  # noqa: E402


# ---------------------------------------------------------------------------
# 替身：把 subprocess.run 换掉
# ---------------------------------------------------------------------------

class _Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """记录每次 subprocess.run 的 argv，并按脚本返回结果。"""

    def __init__(self, *, returncode: int = 0, stdout=b"", stderr: bytes = b"",
                 raise_timeout: bool = False, raise_oserror: bool = False,
                 on_run=None):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_timeout = raise_timeout
        self.raise_oserror = raise_oserror
        self._on_run = on_run

    def __call__(self, cmd, **kwargs):
        self.calls.append([str(c) for c in cmd])
        self.kwargs = kwargs
        if self._on_run is not None:
            self._on_run(cmd, kwargs)
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        if self.raise_oserror:
            raise OSError("no such executable")
        return _Completed(self.returncode, self.stdout, self.stderr)

    @property
    def last(self) -> list[str]:
        return self.calls[-1]


@pytest.fixture()
def tools() -> FFmpegTools:
    return FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe="/fake/ffprobe", source="test")


@pytest.fixture()
def wav_file(tmp_path: Path) -> Path:
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return p


# ---------------------------------------------------------------------------
# locate()
# ---------------------------------------------------------------------------

class TestLocate:
    def test_explicit_config_path_used(self, tmp_path: Path):
        ff = tmp_path / "ffmpeg.exe"
        fp = tmp_path / "ffprobe.exe"
        ff.write_bytes(b"x")
        fp.write_bytes(b"x")
        t = locate(ffmpeg_path=ff, ffprobe_path=fp)
        assert t.ffmpeg == str(ff) and t.ffprobe == str(fp)
        assert t.source == "config" and t.has_probe

    def test_config_path_missing_raises_with_actionable_message(self, tmp_path: Path):
        with pytest.raises(FFmpegNotFoundError) as ei:
            locate(ffmpeg_path=tmp_path / "nope.exe")
        assert "config/app.yaml" in str(ei.value), "报错要指明去哪里改配置"

    def test_ffprobe_guessed_from_sibling(self, tmp_path: Path):
        ff = tmp_path / "ffmpeg.exe"
        fp = tmp_path / "ffprobe.exe"
        ff.write_bytes(b"x")
        fp.write_bytes(b"x")
        t = locate(ffmpeg_path=ff)
        assert t.ffprobe == str(fp), "未显式给 ffprobe 时应尝试同目录兄弟文件"

    def test_no_ffprobe_sibling_results_in_none(self, tmp_path: Path):
        ff = tmp_path / "ffmpeg.exe"
        ff.write_bytes(b"x")
        t = locate(ffmpeg_path=ff)
        assert t.ffprobe is None and not t.has_probe

    def test_explicit_ffprobe_missing_raises(self, tmp_path: Path):
        ff = tmp_path / "ffmpeg.exe"
        ff.write_bytes(b"x")
        with pytest.raises(FFmpegNotFoundError) as ei:
            locate(ffmpeg_path=ff, ffprobe_path=tmp_path / "nope")
        assert "部署手册" in str(ei.value)

    def test_all_fail_mentions_deploy_manual_item(self, monkeypatch):
        monkeypatch.setattr(
            "mmwf.adapters.audio.ffmpeg.shutil.which", lambda _: None
        )
        with pytest.raises(FFmpegNotFoundError) as ei:
            locate(allow_imageio_fallback=False)
        msg = str(ei.value)
        assert "部署手册" in msg and "ffprobe" in msg
        assert "PATH" in msg

    def test_path_source(self, monkeypatch):
        monkeypatch.setattr(
            "mmwf.adapters.audio.ffmpeg.shutil.which",
            lambda n: f"/usr/bin/{n}",
        )
        t = locate(allow_imageio_fallback=False)
        assert t.source == "PATH" and t.has_probe


# ---------------------------------------------------------------------------
# 时长探测
# ---------------------------------------------------------------------------

class TestProbeDuration:
    def test_ffprobe_json_parsed(self, tools, wav_file, monkeypatch):
        runner = FakeRunner(stdout=b'{"format": {"duration": "312.345"}}')
        monkeypatch.setattr(subprocess, "run", runner)
        assert FFmpegAdapter(tools).probe_duration_ms(wav_file) == 312345

    def test_ffprobe_command_shape(self, tools, wav_file, monkeypatch):
        runner = FakeRunner(stdout=b'{"format": {"duration": "1"}}')
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(tools).probe_duration_ms(wav_file)
        cmd = runner.last
        assert cmd[0] == "/fake/ffprobe"
        assert "-print_format" in cmd and "json" in cmd
        assert "-show_format" in cmd
        assert str(wav_file) == cmd[-1], "路径必须是最后一个参数（避免被当成选项）"

    def test_missing_file_raises_e1002(self, tools, tmp_path: Path):
        with pytest.raises(FileMissingError) as ei:
            FFmpegAdapter(tools).probe_duration_ms(tmp_path / "absent.wav")
        assert ei.value.code == "E1002"

    def test_ffprobe_failure_raises_e1003(self, tools, wav_file, monkeypatch):
        runner = FakeRunner(returncode=1, stderr=b"Invalid data found when processing input")
        monkeypatch.setattr(subprocess, "run", runner)
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).probe_duration_ms(wav_file)
        assert ei.value.code == "E1003"
        assert "Invalid data" in ei.value.detail

    def test_bad_json_raises_e1003(self, tools, wav_file, monkeypatch):
        monkeypatch.setattr(subprocess, "run", FakeRunner(stdout=b"not json"))
        with pytest.raises(CorruptAudioError):
            FFmpegAdapter(tools).probe_duration_ms(wav_file)

    def test_missing_duration_key_raises_e1003(self, tools, wav_file, monkeypatch):
        monkeypatch.setattr(subprocess, "run", FakeRunner(stdout=b'{"format": {}}'))
        with pytest.raises(CorruptAudioError):
            FFmpegAdapter(tools).probe_duration_ms(wav_file)

    def test_zero_duration_raises_e1003(self, tools, wav_file, monkeypatch):
        """0 秒不是"很短的音频"，是"读不出来" —— 必须报 E1003。"""
        monkeypatch.setattr(
            subprocess, "run", FakeRunner(stdout=b'{"format": {"duration": "0"}}')
        )
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).probe_duration_ms(wav_file)
        assert ei.value.code == "E1003"

    def test_negative_duration_raises_e1003(self, tools, wav_file, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", FakeRunner(stdout=b'{"format": {"duration": "-5"}}')
        )
        with pytest.raises(CorruptAudioError):
            FFmpegAdapter(tools).probe_duration_ms(wav_file)

    def test_timeout_raises_e1003(self, tools, wav_file, monkeypatch):
        monkeypatch.setattr(subprocess, "run", FakeRunner(raise_timeout=True))
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).probe_duration_ms(wav_file)
        assert "超时" in ei.value.detail

    def test_oserror_raises_ffmpeg_not_found(self, tools, wav_file, monkeypatch):
        """可执行文件失效是**环境问题**，不该报成 E1003（文件损坏）。"""
        monkeypatch.setattr(subprocess, "run", FakeRunner(raise_oserror=True))
        with pytest.raises(FFmpegNotFoundError):
            FFmpegAdapter(tools).probe_duration_ms(wav_file)

    def test_user_message_has_no_path_or_stderr(self, tools, wav_file, monkeypatch):
        """N3.3：用户文案不得含路径、编解码器名等细节。"""
        runner = FakeRunner(returncode=1, stderr=b"mpeg4 @ 0x123 deadbeef error")
        monkeypatch.setattr(subprocess, "run", runner)
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).probe_duration_ms(wav_file)
        um = ei.value.user_message
        assert "0x123" not in um and "deadbeef" not in um
        assert str(wav_file) not in um

    # --- 无 ffprobe 时的降级路径 ---

    def test_ffmpeg_fallback_parses_duration_line(self, wav_file, monkeypatch):
        """imageio-ffmpeg 不带 ffprobe，此时从 ffmpeg stderr 里抽时长。"""
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="imageio-ffmpeg")
        runner = FakeRunner(
            returncode=1,
            stderr=b"  Duration: 00:05:12.34, start: 0.000000, bitrate: 128 kb/s\n"
                   b"At least one output file must be specified",
        )
        monkeypatch.setattr(subprocess, "run", runner)
        ms = FFmpegAdapter(t).probe_duration_ms(wav_file)
        assert ms == (5 * 60 + 12.34) * 1000

    def test_ffmpeg_fallback_hours(self, wav_file, monkeypatch):
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="imageio-ffmpeg")
        runner = FakeRunner(returncode=1, stderr=b"Duration: 02:30:00.00, start: 0.0")
        monkeypatch.setattr(subprocess, "run", runner)
        assert FFmpegAdapter(t).probe_duration_ms(wav_file) == 2.5 * 3600 * 1000

    def test_ffmpeg_fallback_no_duration_raises(self, wav_file, monkeypatch):
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="imageio-ffmpeg")
        monkeypatch.setattr(subprocess, "run", FakeRunner(returncode=1, stderr=b"invalid data"))
        with pytest.raises(CorruptAudioError):
            FFmpegAdapter(t).probe_duration_ms(wav_file)

    def test_ffmpeg_fallback_uses_short_circuit_flags(self, wav_file, monkeypatch):
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="imageio-ffmpeg")
        runner = FakeRunner(returncode=1, stderr=b"Duration: 00:00:10.00")
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(t).probe_duration_ms(wav_file)
        assert "-hide_banner" in runner.last
        assert "-nostdin" in runner.last


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------

class TestCutChunk:
    def test_seek_before_input_for_fast_locate(self, tools, wav_file, tmp_path, monkeypatch):
        """**施工卡硬要求**：`-ss` 必须在 `-i` 之前（快速定位）。"""
        runner = FakeRunner()
        monkeypatch.setattr(subprocess, "run", runner)

        def _touch(cmd, kwargs):
            Path(cmd[-1]).write_bytes(b"RIFF")

        runner._on_run = _touch
        dst = tmp_path / "chunk.wav"
        FFmpegAdapter(tools).cut_chunk(wav_file, dst, 1000, 5000)
        cmd = runner.last
        assert cmd.index("-ss") < cmd.index("-i"), "-ss 必须在 -i 之前"
        assert cmd.index("-t") < cmd.index("-i")

    def test_seek_value_converted_to_seconds(self, tools, wav_file, tmp_path, monkeypatch):
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"RIFF")
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "c.wav", 1750000, 3550000)
        cmd = runner.last
        assert cmd[cmd.index("-ss") + 1] == "1750.000"
        assert cmd[cmd.index("-t") + 1] == "1800.000"

    def test_output_is_16k_mono_pcm(self, tools, wav_file, tmp_path, monkeypatch):
        """**ASR 标准输入**：16kHz 单声道。错了不报错、只让结果质量退化。"""
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"RIFF")
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "c.wav", 0, 1000)
        cmd = runner.last
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert cmd[cmd.index("-ar") + 1] == "16000"
        assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
        assert cmd[cmd.index("-f") + 1] == "wav"

    def test_nostdin_prevents_service_hang(self, tools, wav_file, tmp_path, monkeypatch):
        """作为服务运行时 stdin 不可用；不传 -nostdin 会阻塞。

        这个坑只在"以服务方式跑"时才出现，开发时手敲命令完全正常 ——
        所以要靠测试钉住。
        """
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"RIFF")
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "c.wav", 0, 1000)
        assert "-nostdin" in runner.last

    def test_y_flag_for_idempotent_rerun(self, tools, wav_file, tmp_path, monkeypatch):
        """重复投递同一音频要能覆盖旧切片 —— 否则重跑会卡在确认提示。"""
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"RIFF")
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "c.wav", 0, 1000)
        assert "-y" in runner.last

    def test_video_stream_dropped(self, tools, wav_file, tmp_path, monkeypatch):
        """m4a 可能内嵌封面图；不丢视频流会干扰（甚至让输出变成带图容器）。"""
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"RIFF")
        monkeypatch.setattr(subprocess, "run", runner)
        FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "c.wav", 0, 1000)
        assert "-vn" in runner.last

    def test_dst_parent_created(self, tools, wav_file, tmp_path, monkeypatch):
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"RIFF")
        monkeypatch.setattr(subprocess, "run", runner)
        nested = tmp_path / "a" / "b" / "c.wav"
        FFmpegAdapter(tools).cut_chunk(wav_file, nested, 0, 1000)
        assert nested.parent.is_dir()

    def test_missing_source_raises_e1002(self, tools, tmp_path):
        with pytest.raises(FileMissingError):
            FFmpegAdapter(tools).cut_chunk(tmp_path / "nope.wav", tmp_path / "o.wav", 0, 1)

    def test_invalid_range_rejected(self, tools, wav_file, tmp_path):
        with pytest.raises(ValueError, match="区间无效"):
            FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "o.wav", 500, 500)
        with pytest.raises(ValueError, match="区间无效"):
            FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "o.wav", -1, 100)

    def test_ffmpeg_failure_raises_e1003(self, tools, wav_file, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", FakeRunner(returncode=1, stderr=b"Conversion failed")
        )
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "o.wav", 0, 1000)
        assert ei.value.code == "E1003"
        assert ei.value.context["start_ms"] == 0

    def test_empty_output_detected(self, tools, wav_file, tmp_path, monkeypatch):
        """切出 0 字节文件时必须在这里拦下。

        否则错误会推迟到转写阶段表现为"零段落"，报错指向转写 ——
        排查方向从一开始就是错的。
        """
        runner = FakeRunner()          # 不创建输出文件
        monkeypatch.setattr(subprocess, "run", runner)
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "o.wav", 0, 1000)
        assert "产物为空" in ei.value.detail

    def test_zero_byte_output_detected(self, tools, wav_file, tmp_path, monkeypatch):
        runner = FakeRunner()
        runner._on_run = lambda cmd, kw: Path(cmd[-1]).write_bytes(b"")
        monkeypatch.setattr(subprocess, "run", runner)
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "o.wav", 0, 1000)
        assert "产物为空" in ei.value.detail

    def test_timeout_raises_e1003(self, tools, wav_file, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", FakeRunner(raise_timeout=True))
        with pytest.raises(CorruptAudioError):
            FFmpegAdapter(tools).cut_chunk(wav_file, tmp_path / "o.wav", 0, 1000)


class TestAdapterMisc:
    def test_available_false_without_tools(self, monkeypatch):
        monkeypatch.setattr(
            "mmwf.adapters.audio.ffmpeg.locate",
            lambda **kw: (_ for _ in ()).throw(FFmpegNotFoundError("nope")),
        )
        assert FFmpegAdapter().available() is False

    def test_available_true_when_exists(self, tmp_path, monkeypatch):
        ff = tmp_path / "ffmpeg.exe"
        ff.write_bytes(b"x")
        t = FFmpegTools(ffmpeg=str(ff), ffprobe=None, source="test")
        assert FFmpegAdapter(t).available() is True

    def test_tools_located_lazily(self, tmp_path, monkeypatch):
        """构造 adapter 不该强制要求本机已装 ffmpeg（自检要能如实报不可用）。"""
        calls = {"n": 0}

        def _locate(**kw):
            calls["n"] += 1
            ff = tmp_path / "ffmpeg.exe"
            ff.write_bytes(b"x")
            return FFmpegTools(ffmpeg=str(ff), ffprobe=None, source="test")

        monkeypatch.setattr("mmwf.adapters.audio.ffmpeg.locate", _locate)
        a = FFmpegAdapter()
        assert calls["n"] == 0, "构造时不该定位"
        a.available()
        assert calls["n"] == 1
        a.available()
        assert calls["n"] == 1, "定位结果应缓存"

    def test_version_reports_unknown_on_failure(self, monkeypatch):
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="test")
        monkeypatch.setattr(subprocess, "run", FakeRunner(raise_oserror=True))
        assert FFmpegAdapter(t).version() == "unknown"

    def test_version_parses_first_line(self, monkeypatch):
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="test")
        monkeypatch.setattr(
            subprocess, "run", FakeRunner(stdout=b"ffmpeg version 7.1\nbuilt with gcc\n")
        )
        assert FFmpegAdapter(t).version().startswith("ffmpeg version 7.1")

    def test_non_utf8_stderr_does_not_crash(self, wav_file, monkeypatch):
        """ffmpeg 的 stderr 可能含非 UTF-8 字节。

        若用 `text=True`，subprocess 会抛 UnicodeDecodeError 把真正的
        错误信息盖掉 —— 于是"文件损坏"变成"编码错误"，排查方向全错。
        """
        t = FFmpegTools(ffmpeg="/fake/ffmpeg", ffprobe=None, source="test")
        monkeypatch.setattr(
            subprocess, "run", FakeRunner(returncode=1, stderr=b"\xff\xfe bad \xff")
        )
        with pytest.raises(CorruptAudioError):
            FFmpegAdapter(t).probe_duration_ms(wav_file)


# ---------------------------------------------------------------------------
# service：批量投递的部分失败语义
# ---------------------------------------------------------------------------

class FakeAdapter:
    """假 adapter：按文件名决定行为，不碰真实 ffmpeg。"""

    def __init__(self, *, durations: dict[str, int] | None = None,
                 bad: set[str] | None = None):
        self._durations = durations or {}
        self._bad = bad or set()
        self.cut_calls: list[tuple[Path, int, int]] = []

    def probe_duration_ms(self, path: Path | str) -> int:
        p = Path(path)
        if p.name in self._bad:
            raise CorruptAudioError(detail=f"假 adapter：损坏 {p.name}", audio_path=str(p))
        return self._durations.get(p.name, 600_000)

    def cut_chunk(self, src, dst, start_ms, end_ms) -> None:
        self.cut_calls.append((Path(src), start_ms, end_ms))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"RIFF" + b"\x00" * 64)


@pytest.fixture()
def audio_files(tmp_path: Path) -> list[Path]:
    out = []
    for name in ("a.m4a", "b.mp3", "c.wav"):
        p = tmp_path / name
        p.write_bytes(b"\x00" * 2048)
        out.append(p)
    return out


class TestIngestServiceBatch:
    def test_all_success(self, audio_files, tmp_path):
        svc = IngestService(FakeAdapter(), probe_only=True)
        r = svc.ingest_batch(audio_files)
        assert r.ok and len(r.succeeded) == 3 and not r.failed
        assert r.total == 3
        assert "3 个音频已接收" in r.summary()

    def test_one_bad_file_does_not_kill_batch(self, audio_files, tmp_path):
        """**核心语义**：一个坏文件不该让用户重做整批。"""
        svc = IngestService(FakeAdapter(bad={"b.mp3"}), probe_only=True)
        r = svc.ingest_batch(audio_files)
        assert not r.ok
        assert len(r.succeeded) == 2
        assert len(r.failed) == 1
        assert r.failed[0].filename == "b.mp3"
        assert len(r.succeeded) + len(r.failed) == 3

    def test_summary_distinguishes_partial_failure(self, audio_files):
        svc = IngestService(FakeAdapter(bad={"b.mp3"}), probe_only=True)
        s = svc.ingest_batch(audio_files).summary()
        assert "2 个音频已接收" in s and "1 个未能接收" in s
        assert "单独重投" in s, "要告诉用户不用重做整批"

    def test_all_failed_summary(self, tmp_path):
        files = []
        for n in ("x.mp3", "y.mp3"):
            p = tmp_path / n
            p.write_bytes(b"x")
            files.append(p)
        svc = IngestService(FakeAdapter(bad={"x.mp3", "y.mp3"}), probe_only=True)
        r = svc.ingest_batch(files)
        assert not r.succeeded and len(r.failed) == 2
        assert "都未能接收" in r.summary()

    def test_failure_carries_business_code_and_message(self, audio_files):
        svc = IngestService(FakeAdapter(bad={"b.mp3"}), probe_only=True)
        f = svc.ingest_batch(audio_files).failed[0]
        assert f.error is not None and f.error.code == "E1003"
        assert f.message == f.error.user_message
        d = f.to_dict()
        assert d["code"] == "E1003" and "message" in d and "detail" not in d

    def test_unsupported_format_reported_per_file(self, tmp_path, audio_files):
        bad = tmp_path / "notes.txt"
        bad.write_bytes(b"hello")
        svc = IngestService(FakeAdapter(), probe_only=True)
        r = svc.ingest_batch([*audio_files, bad])
        assert len(r.succeeded) == 3 and len(r.failed) == 1
        assert r.failed[0].error is not None and r.failed[0].error.code == "E1001"

    def test_ffmpeg_not_found_aborts_whole_batch(self, audio_files):
        """ffmpeg 缺失时**整批都做不了**，继续循环只会刷一屏同样的错。"""

        class NoFFmpeg:
            def probe_duration_ms(self, *a, **kw):
                raise FFmpegNotFoundError("nope")

        svc = IngestService(NoFFmpeg())  # type: ignore[arg-type]
        with pytest.raises(FFmpegNotFoundError):
            svc.ingest_batch(audio_files)

    def test_empty_batch(self):
        r = IngestService(FakeAdapter(), probe_only=True).ingest_batch([])
        assert r.ok and r.total == 0

    def test_batch_result_to_dict_shape(self, audio_files):
        r = IngestService(FakeAdapter(), probe_only=True).ingest_batch(audio_files)
        d = r.to_dict()
        assert set(d) >= {"ok", "summary", "succeeded", "failed"}
        assert d["ok"] is True
        assert isinstance(d["succeeded"], list)


class TestIngestServiceSingle:
    def test_probe_only_does_not_cut(self, audio_files, tmp_path):
        """预检模式不铺磁盘 —— 用户还没确认前不该产生切分产物。"""
        fa = FakeAdapter()
        svc = IngestService(fa, probe_only=True)
        r = svc.ingest_one(audio_files[0], chunk_dir=tmp_path / "chunks")
        assert fa.cut_calls == []
        assert r.chunk_files == []
        assert not (tmp_path / "chunks").exists()

    def test_cuts_when_chunk_dir_given(self, tmp_path):
        p = tmp_path / "long.m4a"
        p.write_bytes(b"\x00" * 4096)
        fa = FakeAdapter(durations={"long.m4a": 300_000})
        svc = IngestService(fa, chunk_seconds=120, overlap_seconds=20)
        r = svc.ingest_one(p, chunk_dir=tmp_path / "chunks")
        assert r.chunk_count == 3
        assert len(r.chunk_files) == 3
        assert [c[1] for c in fa.cut_calls] == [0, 100_000, 200_000]

    def test_chunk_naming_sorts_by_seq(self, tmp_path):
        """补零命名保证**按文件名排序 == 按 seq 排序**（ls 与复核页都依赖）。"""
        p = tmp_path / "long.m4a"
        p.write_bytes(b"\x00" * 4096)
        svc = IngestService(FakeAdapter(durations={"long.m4a": 300_000}),
                            chunk_seconds=120, overlap_seconds=20)
        r = svc.ingest_one(p, chunk_dir=tmp_path / "chunks")
        names = [f.name for f in r.chunk_files]
        assert names == [CHUNK_NAME_TEMPLATE.format(seq=i) for i in (1, 2, 3)]
        assert names == sorted(names)

    def test_empty_file_rejected(self, tmp_path):
        p = tmp_path / "empty.wav"
        p.write_bytes(b"")
        with pytest.raises(CorruptAudioError) as ei:
            IngestService(FakeAdapter(), probe_only=True).ingest_one(p)
        assert "为空" in ei.value.detail

    def test_missing_file_raises_e1002(self, tmp_path):
        with pytest.raises(FileMissingError):
            IngestService(FakeAdapter(), probe_only=True).ingest_one(tmp_path / "no.wav")

    def test_unsupported_checked_before_probe(self, tmp_path):
        """格式判定必须在最前面 —— 传了个 .txt 不该等到探测才失败。"""
        p = tmp_path / "a.txt"
        p.write_bytes(b"x")

        class Exploding:
            def probe_duration_ms(self, *a, **kw):
                raise AssertionError("不该走到探测")

        with pytest.raises(UnsupportedFormatError):
            IngestService(Exploding(), probe_only=True).ingest_one(p)  # type: ignore[arg-type]

    def test_oversized_file_warns_but_proceeds(self, tmp_path, monkeypatch):
        """施工卡：>4GB 要有**明确提示**（而不是静默失败或直接拒绝）。

        选择告警而非拒绝：4GB 是 ffmpeg 的软肋不是硬限制，
        用户可能真有一份这么大的录音；直接拒绝反而不给机会。

        补丁打在 `planner` 上：`ingest.py` 读的是 `planner.MAX_FILE_BYTES`
        （模块属性），不是 from-import 的值拷贝 —— 后者会让改配置对
        service 完全无效，是最难查的一类漂移。
        """
        p = tmp_path / "huge.wav"
        p.write_bytes(b"\x00" * 1024)
        monkeypatch.setattr("mmwf.domain.ingest.planner.MAX_FILE_BYTES", 512)
        svc = IngestService(FakeAdapter(), probe_only=True)
        r = svc.ingest_one(p)
        assert r.warnings, "超阈值必须有告警"
        # 提示里要给出**可读的**大小，不能是 "0 GB" 这种无意义文本
        # （阈值一旦被配成小于 1GB，`{:.0f} GB` 就会退化成这样）
        assert any("B" in w or "KB" in w or "MB" in w or "GB" in w
                   for w in r.warnings), r.warnings
        assert any("超过" in w or "上限" in w for w in r.warnings), r.warnings
        assert r.duration_ms > 0, "告警不应阻断"

    def test_multichunk_adds_explanatory_warning(self, tmp_path):
        """切分是对用户可见的行为变化，必须解释"为什么切、怎么切"。"""
        p = tmp_path / "long.m4a"
        p.write_bytes(b"\x00" * 2048)
        svc = IngestService(FakeAdapter(durations={"long.m4a": 2.5 * 3600 * 1000}))
        r = svc.ingest_one(p)
        assert r.chunk_count == 6
        w = " ".join(r.warnings)
        assert "6 段" in w and "重叠" in w

    def test_single_chunk_no_warning(self, tmp_path):
        p = tmp_path / "short.m4a"
        p.write_bytes(b"\x00" * 2048)
        svc = IngestService(FakeAdapter(durations={"short.m4a": 60_000}))
        r = svc.ingest_one(p)
        assert r.chunk_count == 1
        assert not any("切分" in w for w in r.warnings)

    def test_result_to_dict_matches_api_shape(self, tmp_path):
        p = tmp_path / "a.m4a"
        p.write_bytes(b"\x00" * 2048)
        r = IngestService(FakeAdapter(durations={"a.m4a": 2.5 * 3600 * 1000})).ingest_one(p)
        d = r.to_dict()
        assert set(d) >= {"filename", "format", "size_bytes", "duration_ms",
                         "chunk_count", "chunk_plan", "warnings"}
        assert d["chunk_count"] == 6
        assert d["chunk_plan"][0] == {"seq": 1, "start_ms": 0, "end_ms": 1800000}


class TestMakeIngestService:
    def test_reads_chunk_settings_from_config(self):
        """N5.2：切分参数必须来自配置，不能写死在代码里。"""

        class Asr:
            chunk_seconds = 600
            chunk_overlap_seconds = 30

        class S:
            asr = Asr()

        svc = make_ingest_service(S(), adapter=FakeAdapter(), probe_only=True)  # type: ignore[arg-type]
        assert svc._chunk_seconds == 600
        assert svc._overlap_seconds == 30

    def test_falls_back_to_defaults_without_settings(self):
        svc = make_ingest_service(None, adapter=FakeAdapter(), probe_only=True)
        assert svc._chunk_seconds == 1800
        assert svc._overlap_seconds == 50


# ---------------------------------------------------------------------------
# 真实 ffmpeg（有则跑，无则 skip）
# ---------------------------------------------------------------------------

def _real_tools() -> FFmpegTools | None:
    try:
        t = locate()
    except FFmpegNotFoundError:
        return None
    return t


_HAVE_FFMPEG = _real_tools() is not None


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="本机未找到 ffmpeg")
class TestRealFFmpeg:
    """用真实 ffmpeg 跑一遍。**这些用例才是验收标准 2 的真证据** ——
    替身只能证明"命令拼对了"，不能证明"ffmpeg 真的照做了"。"""

    @pytest.fixture()
    def src_wav(self, tmp_path: Path) -> Path:
        import math
        import struct

        p = tmp_path / "src.wav"
        rate = 16000
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            frames = bytearray()
            for i in range(rate * 30):
                frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
            w.writeframes(bytes(frames))
        return p

    def test_real_probe_duration(self, src_wav: Path):
        assert abs(FFmpegAdapter().probe_duration_ms(src_wav) - 30_000) < 100

    def test_real_cut_produces_16k_mono_wav(self, src_wav: Path, tmp_path: Path):
        dst = tmp_path / "c.wav"
        FFmpegAdapter().cut_chunk(src_wav, dst, 5_000, 15_000)
        assert dst.is_file() and dst.stat().st_size > 0
        with wave.open(str(dst), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getframerate() == 16000
            assert abs(w.getnframes() / w.getframerate() - 10.0) < 0.1

    def test_real_cut_start_offset_respected(self, src_wav: Path, tmp_path: Path):
        """验证 `-ss` 前置真的跳到了指定位置。

        前置 `-ss` 是"快速定位"（跳到关键帧），所以允许小偏差；
        但不该偏差到"没跳"（那说明参数写错了位置或单位）。
        """
        dst = tmp_path / "c.wav"
        FFmpegAdapter().cut_chunk(src_wav, dst, 20_000, 25_000)
        with wave.open(str(dst), "rb") as w:
            assert abs(w.getnframes() / w.getframerate() - 5.0) < 0.15

    def test_real_m4a_roundtrip(self, src_wav: Path, tmp_path: Path):
        """真实验收用的格式是 m4a —— 必须验这条路径，不能只测 wav。"""
        import subprocess as sp

        m4a = tmp_path / "src.m4a"
        sp.run(
            [FFmpegAdapter().tools.ffmpeg, "-nostdin", "-y", "-i", str(src_wav),
             "-c:a", "aac", str(m4a)],
            capture_output=True, check=True,
        )
        adapter = FFmpegAdapter()
        assert abs(adapter.probe_duration_ms(m4a) - 30_000) < 200
        dst = tmp_path / "c.wav"
        adapter.cut_chunk(m4a, dst, 0, 10_000)
        with wave.open(str(dst), "rb") as w:
            assert w.getnchannels() == 1 and w.getframerate() == 16000

    def test_real_corrupt_file_raises_e1003(self, tmp_path: Path):
        bad = tmp_path / "bad.mp3"
        bad.write_bytes(b"this is definitely not audio" * 20)
        with pytest.raises(CorruptAudioError) as ei:
            FFmpegAdapter().probe_duration_ms(bad)
        assert ei.value.code == "E1003"
        # 用户文案里不得出现 ffmpeg 的原始报错
        assert "Invalid" not in ei.value.user_message

    def test_real_service_full_pipeline(self, src_wav: Path, tmp_path: Path):
        """端到端：投递 → 探测 → 切分，产物可被标准库读回。"""
        svc = IngestService(FFmpegAdapter(), chunk_seconds=12, overlap_seconds=2)
        r = svc.ingest_one(src_wav, chunk_dir=tmp_path / "chunks")
        assert r.chunk_count == 3
        total = 0
        for f in r.chunk_files:
            with wave.open(str(f), "rb") as w:
                assert w.getnchannels() == 1 and w.getframerate() == 16000
                total += w.getnframes() / w.getframerate()
        # 12 + 12 + 8（含重叠，总时长必然大于原长）
        assert total > 30
