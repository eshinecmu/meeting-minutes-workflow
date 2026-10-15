"""转写引擎接口契约测试（T1-05）。

**目的**（测试方案 §3 原文）：
> 新增任何识别引擎…都必须自动满足同一套接口契约，**不靠人肉遵守**。

本文件对注册表里的**全部 provider 参数化**。新增引擎只要注册了，
就自动进入这里被检查 —— 这就是"不靠人肉遵守"的落点。

契约分两层
----------
**A. 离线契约**（不需要引擎安装、不需要模型、不需要音频）
   类型、字段、schema、时间戳单调性、错误码、幂等性 ——
   用 `FakeProvider` 与 `FakeFunASR` 覆盖。**任何机器上都必须全绿。**

**B. 真实引擎契约**（`@pytest.mark.real_model`）
   真正跑一次转写。需要 funasr + 模型 + 5 分钟音频。
   不装就不跑（`pytest.mark.skipif`），但在**有引擎的交付机上必须全绿**。

为什么把契约测试放在 `tests/contract/` 而不是 `tests/unit/`
-----------------------------------------------------------
因为它的断言对象是**接口**而不是实现。将来 `funasr_local.py` 整个重写，
本文件一行都不该改。放在 `contract/` 是在用目录位置表达这个约束。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.adapters.asr import (  # noqa: E402
    AsrProvider,
    AsrProviderError,
    available_providers,
    build,
    health_of_all,
    register,
    unregister,
)
from mmwf.domain.asr_types import TranscriptResult  # noqa: E402
from mmwf.domain.errors import MmwfError  # noqa: E402

#: 契约规定的 health_check 必含键（测试方案 §3 原文）
REQUIRED_HEALTH_KEYS = {"available", "model", "device", "hotword_supported"}

#: 判定"真实引擎可用"的条件（用于 real_model 标记）
_FUNASR_INSTALLED = False
try:  # pragma: no cover - 取决于机器
    import funasr  # noqa: F401

    _FUNASR_INSTALLED = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

def _make_result() -> TranscriptResult:
    raw = [
        {"text": "同志们", "start_ms": 0, "end_ms": 900, "confidence": 0.96},
        {"text": "今天我们召开党组会", "start_ms": 900, "end_ms": 2600, "confidence": 0.94},
        {"text": "研究部署重点工作", "start_ms": 2600, "end_ms": 4200, "confidence": 0.55},
    ]
    return TranscriptResult.from_raw_segments(
        raw, engine="fake", model="fake-model", hotwords_applied=True
    )


class FakeProvider:
    """最小合法实现——用来验证"契约本身可满足"。

    它是**样板**：新引擎照这个形状写就能过契约测试。
    刻意不继承任何基类，以此证明 Protocol 方案不需要继承
    （SAD 附录 C 第 6 条：不得修改上层代码）。
    """

    name = "fake"

    def __init__(self, *, broken: str | None = None) -> None:
        self._broken = broken

    def available(self) -> bool:
        return True

    def warmup(self) -> None:
        return None

    def transcribe(self, audio_path, hotwords=None) -> TranscriptResult:
        if self._broken == "empty":
            return TranscriptResult.from_raw_segments(
                [], engine="fake", model="fake-model"
            )
        if self._broken == "nonmonotonic":
            return TranscriptResult(
                segments=(
                    _make_result().segments[1],
                    _make_result().segments[0],
                    _make_result().segments[2],
                ),
                engine="fake",
                model="fake-model",
            )
        return _make_result()

    def health_check(self) -> dict[str, Any]:
        return {
            "available": True,
            "model": "fake-model",
            "device": "cpu",
            "hotword_supported": True,
        }


def _params() -> list[str]:
    """参数化范围 = 注册表里的全部引擎。"""
    return available_providers()


@pytest.fixture(params=_params())
def provider_name(request) -> str:
    return request.param


@pytest.fixture()
def provider(provider_name: str):
    """按名字构造。FunASR 需要 model_dir，用不存在的路径即可 ——
    `available()` 只做探测、不加载模型，所以构造是安全的。"""
    return build(provider_name, model_dir=PROJECT_ROOT / "models" / "nonexistent",
                 device="cpu")


# ---------------------------------------------------------------------------
# A1. 协议一致性（全部 provider）
# ---------------------------------------------------------------------------

def test_provider_satisfies_protocol(provider):
    """`runtime_checkable` 的结构检查 —— 四个方法都得在。

    注意：它能通过**不代表签名正确**（Protocol 的 runtime check 只看名字）。
    真正的签名一致性由下面的行为断言保证。
    """
    assert isinstance(provider, AsrProvider)


def test_available_returns_bool(provider):
    """契约原文：`assert isinstance(provider.available(), bool)`。"""
    assert isinstance(provider.available(), bool)


def test_name_is_nonempty_string(provider):
    assert isinstance(provider.name, str) and provider.name


def test_warmup_is_idempotent_when_available(provider):
    """契约：`warmup()` 可重复调用，第二次应快速返回。

    这里只断言"不抛异常"，不断言耗时（耗时受机器影响，会变 flaky）。
    真正的"第二次快"由 funasr 单测用 mock 计数验证。
    """
    if not provider.available():
        pytest.skip("引擎不可用，跳过 warmup")
    provider.warmup()
    provider.warmup()


def test_health_check_schema(provider):
    """契约原文：`REQUIRED_HEALTH_KEYS <= set(h)`。"""
    h = provider.health_check()
    assert isinstance(h, dict)
    missing = REQUIRED_HEALTH_KEYS - set(h)
    assert not missing, f"{provider.name}.health_check() 缺少必需键：{missing}"
    assert isinstance(h["available"], bool)
    assert isinstance(h["model"], str)
    assert isinstance(h["device"], str)
    assert isinstance(h["hotword_supported"], bool)


def test_health_check_is_json_serializable(provider):
    """自检结果要嵌进 `/system/selfcheck` 的 JSON 响应，必须可序列化。"""
    h = provider.health_check()
    assert json.loads(json.dumps(h, ensure_ascii=False)) == h


def test_health_check_does_not_load_model(provider):
    """**关键契约**：`health_check()` 与 `available()` 都不得加载模型。

    为什么必须钉住：`/system/selfcheck` 会被频繁调用（界面一进就调），
    若这里加载 220M 模型，自检会卡几十秒，用户以为程序死了。
    """
    if not provider.available():
        pytest.skip("引擎不可用")
    h = provider.health_check()
    assert h.get("loaded") in (False, None) or "loaded" not in h, (
        "health_check() 不应触发模型加载（应以 loaded=False 如实报告）"
    )


# ---------------------------------------------------------------------------
# A2. 返回值契约（用 FakeProvider，任何机器都跑）
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake() -> FakeProvider:
    return FakeProvider()


def test_transcribe_returns_contract(fake: FakeProvider):
    """契约原文形状。"""
    r = fake.transcribe("whatever.wav", hotwords=["测试词"])
    assert isinstance(r, TranscriptResult)
    assert r.segments, "分段不得为空"
    assert all(s.start_ms <= s.end_ms for s in r.segments)
    assert [s.seq for s in r.segments] == sorted(s.seq for s in r.segments)


def test_timestamps_monotonic(fake: FakeProvider):
    """契约原文：时间戳必须单调不减 —— 上游错位是已知静默失效症状。"""
    r = fake.transcribe("whatever.wav")
    starts = [s.start_ms for s in r.segments]
    assert starts == sorted(starts), "时间戳非单调，疑似上游时间戳错位"


def test_seq_starts_at_one_and_is_contiguous(fake: FakeProvider):
    r = fake.transcribe("whatever.wav")
    assert [s.seq for s in r.segments] == list(range(1, len(r.segments) + 1))


def test_engine_and_model_are_reported(fake: FakeProvider):
    """engine/model 必须非空 —— 交付要能说明"谁识别的"。"""
    r = fake.transcribe("whatever.wav")
    assert r.engine and isinstance(r.engine, str)
    assert r.model and isinstance(r.model, str)


def test_transcribe_is_deterministic(fake: FakeProvider):
    """同输入同输出 —— 幂等重试才不会产生不同结果。"""
    a = fake.transcribe("x.wav", hotwords=["甲"])
    b = fake.transcribe("x.wav", hotwords=["甲"])
    assert a.to_json() == b.to_json()


def test_empty_result_fails_validation(fake: FakeProvider):
    """契约：segments 不得为空。空结果必须被判为无效（走 E2003）。"""
    r = fake.transcribe("x.wav")
    r_bad = TranscriptResult.from_raw_segments([], engine="fake", model="m")
    assert r_bad.validate(), "空分段必须被 validate 判为问题"
    assert r.validate() == []


def test_nonmonotonic_result_fails_validation():
    """乱序时间戳必须被 validate 抓到（不依赖引擎自己纠正）。"""
    broken = FakeProvider(broken="nonmonotonic").transcribe("x.wav")
    problems = broken.validate()
    assert any("非单调" in p or "重叠" in p for p in problems), problems


# ---------------------------------------------------------------------------
# A3. 错误码契约
# ---------------------------------------------------------------------------

def test_errors_are_business_errors_not_raw_exceptions(provider):
    """契约：失败时抛 `mmwf.domain.errors` 的业务异常，**不是裸的 ImportError**。

    为什么这条重要（N3.3）：裸异常的信息会顺着 `str(e)` 一路流到界面，
    把堆栈和英文报错暴露给非技术用户。
    """
    if provider.available():
        pytest.skip("引擎可用，无法构造失败场景")
    with pytest.raises(MmwfError) as ei:
        provider.transcribe(PROJECT_ROOT / "models" / "nonexistent" / "a.wav")
    assert ei.value.code.startswith("E")


def test_missing_audio_raises_e1002(provider):
    """不存在的音频文件 -> E1002（文件不存在），而不是引擎层的奇怪报错。"""
    if not provider.available():
        pytest.skip("引擎不可用时不校验具体码")
    with pytest.raises(MmwfError) as ei:
        provider.transcribe(PROJECT_ROOT / "no_such_audio.wav")
    assert ei.value.code == "E1002"


def test_missing_model_dir_raises_e2001():
    """模型目录不存在 -> E2001（引擎未就绪），且**不得联网去拉**（坑 ②）。"""
    from mmwf.adapters.asr.funasr_local import FunASRLocalProvider

    p = FunASRLocalProvider(model_dir=PROJECT_ROOT / "models" / "definitely_absent")
    problems = p.health_check()
    assert problems["model_dir_ready"] is False
    with pytest.raises(MmwfError) as ei:
        p.warmup()
    assert ei.value.code == "E2001"


def test_error_to_dict_hides_technical_detail():
    """**N3.3 红线**：给用户的响应体不得含技术细节。

    构造一个 detail 里有堆栈感的异常，验证 to_dict() 里没有它。
    """
    from mmwf.domain.errors import CorruptAudioError

    err = CorruptAudioError(
        detail="Traceback (most recent call last): File \"C:\\x.py\", line 42, "
               "RuntimeError: ffmpeg exit code 1",
        audio_path="/secret/path.wav",
    )
    d = err.to_dict()
    blob = json.dumps(d, ensure_ascii=False)
    for leak in ("Traceback", "C:\\x.py", "line 42", "ffmpeg", "secret"):
        assert leak not in blob, f"to_dict() 泄漏了技术细节：{leak}"
    assert d["message"] and d["code"] and d["severity"]


def test_all_error_codes_have_user_message_and_title():
    """每个错误码都必须有中文文案 —— 否则界面只能显示代码号。"""
    from mmwf.domain.errors import ALL_SPECS

    for code, spec in ALL_SPECS.items():
        assert spec.user_message, f"{code} 缺 user_message"
        assert spec.title, f"{code} 缺 title"
        # 文案必须是中文句子（含中文字符），不是英文技术描述
        assert any("\u4e00" <= ch <= "\u9fff" for ch in spec.user_message), (
            f"{code} 的 user_message 不像面向使用者的中文文案：{spec.user_message!r}"
        )


def test_e2003_is_fatal_severity():
    """E2003 必须是 FATAL：SAD §9.3 的文案是"已暂停处理"。"""
    from mmwf.domain.errors import ALL_SPECS, Severity

    assert ALL_SPECS["E2003"].severity is Severity.FATAL
    # 其余 E2xxx 不是 FATAL（可重试或仅告警）
    assert ALL_SPECS["E2001"].severity is Severity.ERROR
    assert ALL_SPECS["E2002"].severity is Severity.ERROR
    assert ALL_SPECS["E2004"].severity is Severity.WARNING


# ---------------------------------------------------------------------------
# A4. 注册表契约
# ---------------------------------------------------------------------------

def test_registry_contains_funasr():
    """一期必须有 FunASR（SAD §技术选型）。"""
    assert "funasr_local" in available_providers()


def test_build_unknown_provider_raises():
    with pytest.raises(AsrProviderError, match="未注册的转写引擎"):
        build("no_such_engine_xyz")


def test_double_register_raises_unless_override():
    """静默覆盖会让"谁生效"取决于 import 顺序，极难排查。"""
    register("fake", FakeProvider)
    try:
        # 第二次注册同名 -> 必须报错
        with pytest.raises(AsrProviderError, match="已注册"):
            register("fake", FakeProvider)

        # 显式 override 才允许替换
        class Another:
            name = "fake"
            available = warmup = health_check = transcribe = None  # 占位，只为区分身份

        register("fake", Another, override=True)
        assert build("fake") is not None
    finally:
        unregister("fake")
    assert "fake" not in available_providers()


def test_registering_the_builtin_name_is_rejected():
    """连内置的 funasr_local 也不许被无意覆盖（防 import 顺序陷阱）。"""
    from mmwf.adapters.asr.funasr_local import FunASRLocalProvider

    with pytest.raises(AsrProviderError, match="已注册"):
        register("funasr_local", FunASRLocalProvider)


def test_health_of_all_survives_broken_provider():
    """自检必须容错：一个引擎探测失败不该让整个自检崩掉。"""
    class Exploding:
        name = "exploding"

        def __init__(self, **kw):
            pass

        def available(self):
            raise RuntimeError("boom")

        def warmup(self):
            raise RuntimeError("boom")

        def transcribe(self, *a, **kw):
            raise RuntimeError("boom")

        def health_check(self):
            raise RuntimeError("boom")

    register("exploding", Exploding, override=True)
    try:
        out = health_of_all()
        assert out["exploding"]["available"] is False
        assert out["exploding"]["status"] == "probe_failed"
        # 其他引擎仍然正常被探测到
        assert "funasr_local" in out
    finally:
        unregister("exploding")


def test_build_from_settings_uses_yaml_not_code():
    """N5.2：换引擎只改 yaml。用真实 config 验证接线正确。"""
    from mmwf.adapters.asr import build_from_settings
    from mmwf.settings import load_settings

    s = load_settings(PROJECT_ROOT / "config")
    p = build_from_settings(s)
    assert p.name == s.asr.provider
    h = p.health_check()
    assert h["model"] == s.asr.model
    assert h["device_requested"] == s.asr.device


# ---------------------------------------------------------------------------
# B. 真实引擎契约（需要 funasr + 模型 + 音频）
# ---------------------------------------------------------------------------

SAMPLE_AUDIO = PROJECT_ROOT / "tests" / "fixtures" / "audio" / "sample_5min.m4a"

_real_unavailable = (
    not _FUNASR_INSTALLED
    or not SAMPLE_AUDIO.exists()
    or not (PROJECT_ROOT / "models" / "funasr" / "paraformer-zh").is_dir()
)
_reason = (
    "真实引擎契约需 funasr + 模型目录 + tests/fixtures/audio/sample_5min.m4a；"
    "当前环境不具备（离线交付机上必须全绿）"
)


@pytest.mark.real_model
@pytest.mark.skipif(_real_unavailable, reason=_reason)
def test_real_engine_full_contract():
    """真实引擎上跑一遍完整契约（在交付机上必须通过）。"""
    from mmwf.adapters.asr import build_from_settings
    from mmwf.settings import load_settings

    settings = load_settings(PROJECT_ROOT / "config")
    p = build_from_settings(settings)
    assert p.available(), "引擎自检为不可用"

    h = p.health_check()
    assert REQUIRED_HEALTH_KEYS <= set(h)

    p.warmup()
    r = p.transcribe(SAMPLE_AUDIO, hotwords=["测试词"]).validated()

    assert r.segments, "分段数为 0"
    starts = [s.start_ms for s in r.segments]
    assert starts == sorted(starts), "时间戳非单调"
    assert [s.seq for s in r.segments] == list(range(1, len(r.segments) + 1))
    assert r.engine == "funasr_local"
    # 5 分钟音频的转写结果不该只有一两句话
    assert len(r.segments) >= 10, f"分段数偏少（{len(r.segments)}），疑似转写不完整"
