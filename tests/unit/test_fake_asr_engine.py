"""T1-05 单元测试：离线假引擎夹具自身。

为什么夹具也要测
----------------
`tests/fixtures/asr/fake_engine.py` 是**验收标准 1/2/3/5 的测量仪器**。
仪器不准，量出来的"通过"就没有意义。特别是：

· 夹具必须真的复现"热词被静默忽略"这个上游缺陷。
  如果 `simulate_hotword_ignored=True` 其实没生效，
  验收标准 3 会**永远通过**（因为永远命中），从而完全失去把关能力。
· 夹具产出的时间戳必须**真的**单调递增。
  如果夹具自己就产乱序时间戳，那标准 1 是在验夹具的 bug，
  而不是验适配器的校验逻辑。

这两条都是"测试的测试"（meta-test）：它们不验证产品代码，
而是保证验证装置本身可信。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "fixtures" / "asr"))

from fake_engine import (  # noqa: E402
    PROVIDER_NAME,
    SCRIPT_LINES,
    FakeOfflineProvider,
)
from mmwf.domain.errors import CorruptAudioError, FileMissingError, MmwfError  # noqa: E402


@pytest.fixture()
def audio(tmp_path: Path) -> Path:
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 3)
    return p


# ---------------------------------------------------------------------------
# 测量仪器可信性（meta-test）
# ---------------------------------------------------------------------------

class TestFixtureCredibility:
    def test_output_is_deterministic(self, tmp_path: Path, audio: Path):
        """同一输入两次调用必须完全一致 —— 否则验收断言无法精确。"""
        a = FakeOfflineProvider(model_dir=tmp_path).transcribe(audio)
        b = FakeOfflineProvider(model_dir=tmp_path).transcribe(audio)
        assert a.text == b.text
        assert [s.start_ms for s in a.segments] == [s.start_ms for s in b.segments]
        assert a.to_dict() == b.to_dict()

    def test_timestamps_are_monotonic(self, tmp_path: Path, audio: Path):
        """夹具自己不能产乱序时间戳，否则标准 1 是在验夹具的 bug。"""
        r = FakeOfflineProvider(model_dir=tmp_path).transcribe(audio)
        starts = [s.start_ms for s in r.segments]
        assert starts == sorted(starts)
        prev_end = -1
        for s in r.segments:
            assert s.start_ms >= prev_end, "夹具产出了重叠分段"
            prev_end = s.end_ms
        assert r.validate() == []

    def test_segments_are_nonempty_and_seq_contiguous(self, tmp_path: Path, audio: Path):
        r = FakeOfflineProvider(model_dir=tmp_path).transcribe(audio)
        assert len(r.segments) == len(SCRIPT_LINES)
        assert [s.seq for s in r.segments] == list(range(1, len(SCRIPT_LINES) + 1))

    def test_per_char_words_match_text_length(self, tmp_path: Path, audio: Path):
        """逐字模式下 words 拼起来必须等于 text —— 不违反容差，标准 1 才有意义。"""
        r = FakeOfflineProvider(model_dir=tmp_path, words_mode="per_char").transcribe(audio)
        for s in r.segments:
            assert s.word_text_joined() == s.text

    def test_per_segment_mode_still_validates(self, tmp_path: Path, audio: Path):
        """整段一对时间戳（真实也见过的形状）不能被误判为失效。

        注意：`per_segment` 下 words 只有一个元素（长度 1），
        而 text 长度可能是十几 —— 这会触发容差告警。
        这里只断言"不抛 E2003"和"分段结构正确"，
        不断言 validate() 为空，因为那本来就是会报警的形状。
        """
        r = FakeOfflineProvider(model_dir=tmp_path, words_mode="per_segment").transcribe(audio)
        assert len(r.segments) == len(SCRIPT_LINES)
        assert all(len(s.words) == 1 for s in r.segments)


# ---------------------------------------------------------------------------
# 复现上游缺陷：热词被静默忽略
# ---------------------------------------------------------------------------

class TestSimulatedHotwordIgnored:
    """这是验收标准 3 的**前提条件**。若它失效，标准 3 就永远通过。"""

    HOTWORDS = ["张建国", "李国强", "王振华"]

    def test_normal_mode_actually_hits_hotwords(self, tmp_path: Path, audio: Path):
        r = FakeOfflineProvider(model_dir=tmp_path).transcribe(audio, hotwords=self.HOTWORDS)
        assert r.hotwords_applied is True
        assert r.raw_meta["hotwords_hit"] == self.HOTWORDS

    def test_ignored_mode_produces_zero_hits(self, tmp_path: Path, audio: Path):
        """核心断言：ignored 模式下命中清单**必须为空**。"""
        r = FakeOfflineProvider(
            model_dir=tmp_path, simulate_hotword_ignored=True
        ).transcribe(audio, hotwords=self.HOTWORDS)
        assert r.raw_meta["hotwords_hit"] == []
        assert r.hotwords_applied is False

    def test_ignored_mode_emits_the_exact_required_warning(self, tmp_path: Path, audio: Path):
        """验收标准 3 要求的字面串。夹具不产它，标准 3 就验不到东西。"""
        r = FakeOfflineProvider(
            model_dir=tmp_path, simulate_hotword_ignored=True
        ).transcribe(audio, hotwords=self.HOTWORDS)
        assert any("hotwords supplied but zero hit" in w for w in r.warnings), r.warnings

    def test_ignored_mode_body_really_lacks_the_names(self, tmp_path: Path, audio: Path):
        """不能只在元数据上造假说"没命中"，正文里也必须真的没有。

        否则是"报告与实际不符"—— 那会让基于文本的断言变得不可信。
        """
        r = FakeOfflineProvider(
            model_dir=tmp_path, simulate_hotword_ignored=True
        ).transcribe(audio, hotwords=self.HOTWORDS)
        for name in self.HOTWORDS:
            assert name not in r.text, f"ignored 模式正文里仍出现 {name}"

    def test_no_hotwords_no_warning_even_in_ignored_mode(self, tmp_path: Path, audio: Path):
        """没传热词时不该报零命中（误报会淹没真告警）。"""
        r = FakeOfflineProvider(
            model_dir=tmp_path, simulate_hotword_ignored=True
        ).transcribe(audio, hotwords=[])
        assert not any("hotwords supplied" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# 契约一致性：夹具也必须满足 AsrProvider
# ---------------------------------------------------------------------------

class TestFixtureSatisfiesContract:
    def test_health_check_required_keys(self, tmp_path: Path):
        h = FakeOfflineProvider(model_dir=tmp_path).health_check()
        assert {"available", "model", "device", "hotword_supported"} <= set(h)

    def test_available_does_not_load(self, tmp_path: Path):
        p = FakeOfflineProvider(model_dir=tmp_path)
        p.available()
        p.health_check()
        assert p.health_check()["loaded"] is False

    def test_warmup_then_loaded(self, tmp_path: Path):
        p = FakeOfflineProvider(model_dir=tmp_path)
        p.warmup()
        assert p.health_check()["loaded"] is True

    def test_transcribe_warms_up_automatically(self, tmp_path: Path, audio: Path):
        p = FakeOfflineProvider(model_dir=tmp_path)
        p.transcribe(audio)
        assert p.health_check()["loaded"] is True

    def test_missing_audio_raises_e1002(self, tmp_path: Path):
        p = FakeOfflineProvider(model_dir=tmp_path)
        with pytest.raises(FileMissingError) as ei:
            p.transcribe(tmp_path / "absent.wav")
        assert ei.value.code == "E1002"

    def test_corrupt_injection_raises_e1003(self, tmp_path: Path, audio: Path):
        p = FakeOfflineProvider(model_dir=tmp_path, fail_with="corrupt")
        with pytest.raises(CorruptAudioError) as ei:
            p.transcribe(audio)
        assert ei.value.code == "E1003"

    def test_all_errors_are_mmwf_errors(self, tmp_path: Path):
        with pytest.raises(MmwfError):
            FakeOfflineProvider(model_dir=tmp_path).transcribe(tmp_path / "absent.wav")

    def test_no_confidence_mode(self, tmp_path: Path, audio: Path):
        """模拟"引擎未提供置信度"—— 用于验 review gate 降级分支。"""
        r = FakeOfflineProvider(model_dir=tmp_path, simulate_no_confidence=True).transcribe(audio)
        assert r.avg_confidence is None
        assert any("no confidence" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# 注册表可扩展性
# ---------------------------------------------------------------------------

class TestInstallIntoRegistry:
    def test_install_and_uninstall(self):
        from fake_engine import install
        from mmwf.adapters.asr import available_providers, build, unregister

        try:
            install()
            assert PROVIDER_NAME in available_providers()
            p = build(PROVIDER_NAME)
            assert p.name == PROVIDER_NAME
        finally:
            unregister(PROVIDER_NAME)
        assert PROVIDER_NAME not in available_providers()

    def test_double_install_is_idempotent(self):
        """override=True 让重复 install 安全 —— 多个测试文件共用夹具时不炸。"""
        from fake_engine import install
        from mmwf.adapters.asr import unregister

        try:
            install()
            install()
            install()
        finally:
            unregister(PROVIDER_NAME)
