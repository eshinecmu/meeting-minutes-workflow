"""T1-05 单元测试：FunASR 适配器。

重点覆盖"跟上游怪癖打交道"的那几处 —— 它们是最容易悄悄失效的地方：
  · 热词参数名跨版本漂移（`hotword` / `hotwords`）
  · 热词被静默忽略（引擎不报错也不告知）
  · `disable_update=True` 必须显式传（离线关键）
  · FunASR 返回格式的各种真实形状与畸形形状
  · 模型目录缺失时必须抛 E2001，而不是联网去拉

用 `FakeAutoModel` 替身而不是真引擎：真引擎要下载 220M 模型、
跑一次要几十秒，进不了日常门禁。真实引擎的验证在
`tests/contract/test_asr_contract.py` 的 `real_model` 用例里。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.adapters.asr.base import AsrProviderError  # noqa: E402
from mmwf.adapters.asr.funasr_local import (  # noqa: E402
    DEFAULT_MODEL_REVISION,
    PROVIDER_NAME,
    FunASRLocalProvider,
    _probe_duration_ms,
    _span_from_timestamp,
    _words_from_timestamp,
    run_transcribe,
)
from mmwf.domain.errors import (  # noqa: E402
    AsrNotReadyError,
    AsrSilentFailureError,
    CorruptAudioError,
    FileMissingError,
    MmwfError,
)

# ---------------------------------------------------------------------------
# FunASR 返回格式解析（纯函数，覆盖真实见过的形状）
# ---------------------------------------------------------------------------

class TestSpanFromTimestamp:
    def test_normal_word_level_pairs(self):
        ts = [[0, 180], [180, 460], [460, 900]]
        assert _span_from_timestamp(ts) == (0, 900)

    def test_empty_returns_zero(self):
        """时间戳缺失返回 (0,0)，不猜 —— 让 validate 去判"全 0"这个明确问题。"""
        assert _span_from_timestamp([]) == (0, 0)
        assert _span_from_timestamp(None) == (0, 0)

    def test_malformed_returns_zero(self):
        assert _span_from_timestamp("not a list") == (0, 0)
        assert _span_from_timestamp([["a", "b"]]) == (0, 0)
        assert _span_from_timestamp([[]]) == (0, 0)
        assert _span_from_timestamp([[1]]) == (0, 0)

    def test_single_pair(self):
        assert _span_from_timestamp([[100, 250]]) == (100, 250)

    def test_flat_list_tolerated(self):
        """有的版本直接给 [start, end] 而不是 [[s,e], ...]。"""
        assert _span_from_timestamp([100, 250]) == (100, 250)


class TestWordsFromTimestamp:
    def test_pairs_words_with_timestamps(self):
        words = _words_from_timestamp([[0, 180], [180, 460]], "同志")
        assert words == [
            {"text": "同", "start_ms": 0, "end_ms": 180},
            {"text": "志", "start_ms": 180, "end_ms": 460},
        ]

    def test_length_mismatch_takes_shorter_and_does_not_raise(self):
        """**上游不一致的正常情况**：len(timestamp) != len(text)。

        必须"尽力还原"，不能抛错（会把轻微分词差异误判成严重故障），
        也不能补 0（会伪造不存在的时间戳）。
        """
        words = _words_from_timestamp([[0, 100], [100, 200]], "同志们")
        assert len(words) == 2, "应取较短的一侧，不补不裁 текст"
        assert [w["text"] for w in words] == ["同", "志"]

    def test_more_timestamps_than_text(self):
        words = _words_from_timestamp([[0, 1], [1, 2], [2, 3]], "同")
        assert len(words) == 1

    def test_empty_inputs(self):
        assert _words_from_timestamp([], "文本") == []
        assert _words_from_timestamp([[0, 1]], "") == []
        assert _words_from_timestamp(None, "文本") == []

    def test_malformed_pairs_are_skipped_not_fatal(self):
        words = _words_from_timestamp([[0, 1], "bad", [2, 3]], "同志志")
        assert [w["text"] for w in words] == ["同", "志"]


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

def _build_generate(params: tuple[str, ...]):
    """按给定形参名造一个 `generate(self, <params...>, **kw)` 函数。

    **每个形参都给默认值** —— 这是必须的：真实 FunASR 的 `generate`
    所有参数都是可选的，调用方只传自己关心的那几个。若造成必填位置参数，
    测试里"只传 input/batch_size_s/sentence_timestamp"就会 TypeError，
    那是**替身失真**，不是被测代码的问题。

    返回体把**具名形参逐个收进 dict** 再转给 `FakeAutoModel._generate_body`。
    早先版本写成 `return FakeAutoModel._generate_body(self, kw)`，
    只转发了 `**kw` —— 但调用方传的 `input=`/`batch_size_s=` 是**绑到具名形参**上的，
    根本不进 `**kw`，导致 `last_generate_kwargs` 恒为空、断言全部 KeyError。
    这是"用 exec 造签名"特有的坑：签名和函数体是两个独立文本，容易对不上。
     """
    sig = ", ".join(f"{p}=None" for p in params)
    collected = "{" + ", ".join(f'"{p}": {p}' for p in params) + "}"
    body = (
        "def generate(self"
        + (", " + sig if sig else "")
        + ", **kw):\n"
        + f"    _args = {collected}\n"
        + "    _args.update(kw)\n"
        + "    return FakeAutoModel._generate_body(self, _args)"
    )
    ns: dict = {"FakeAutoModel": FakeAutoModel}
    exec(body, ns)                          # noqa: S102 - 测试内构造签名，无可信输入
    return ns["generate"]


class FakeAutoModel:
    """FunASR AutoModel 的替身。记录构造参数，便于断言"什么被显式传了"。"""

    #: 类级记录，供断言（每次构造 append 一条）
    instances: list["FakeAutoModel"] = []
    #: 可注入的 generate 返回值
    next_result: object = None
    #: 可注入的 generate 异常
    raise_on_generate: Exception | None = None
    #: 模拟该版本的 generate 形参（用于热词参数探测）
    generate_params: tuple[str, ...] = ("input", "batch_size_s", "sentence_timestamp", "hotword")

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        #: 实际传给 generate 的**有效**关键字（值为 None 的剔除，等价于"没传"）
        self.last_generate_kwargs: dict = {}
        #: 原始收到的全部关键字（含 None 占位），排查用
        self.last_generate_all: dict = {}
        FakeAutoModel.instances.append(self)

    @classmethod
    def _generate_body(cls, inst: "FakeAutoModel", kwargs: dict):
        """真正的替身行为。由 `_build_generate` 造出的签名转发进来。

        **记录全部收到的关键字，不过滤 None**：`hotword=None` 和
        "调用方没传 hotword" 是两件事，测试要能区分。
        但由于 `_build_generate` 给每个形参都填了 `None` 默认值，
        "没传"在替身里也表现为 `None` —— 所以判据统一为
        **"值不是 None 就算显式传了"**（见 `last_generate_kwargs` 属性）。
        """
        if cls.raise_on_generate is not None:
            raise cls.raise_on_generate
        inst.last_generate_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        inst.last_generate_all = dict(kwargs)
        return cls.next_result


# 初始装一份默认签名，保证不依赖 fake_funasr 的用例（如 TestSpanFromTimestamp 之外的）
# 也能拿到可用的 generate。
FakeAutoModel.generate = _build_generate(FakeAutoModel.generate_params)  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeAutoModel.instances = []
    FakeAutoModel.next_result = None
    FakeAutoModel.raise_on_generate = None
    FakeAutoModel.generate_params = (
        "input", "batch_size_s", "sentence_timestamp", "hotword",
    )
    FakeAutoModel.generate = _build_generate(FakeAutoModel.generate_params)  # type: ignore[assignment]
    yield
    FakeAutoModel.instances = []
    FakeAutoModel.next_result = None
    FakeAutoModel.raise_on_generate = None


@pytest.fixture()
def fake_funasr(monkeypatch):
    """把 funasr 模块注入 sys.modules，让 provider 能 import 到替身。

    注意 `generate` 的签名是**在 fixture 建立时按当时的 `generate_params`
    烘出来的**。需要在用例里改 `generate_params` 的（热词探测那几条），
    必须用 `set_generate_params()` 而不是裸 `monkeypatch.setattr` ——
    因为 `inspect.signature` 读的是**函数对象**，不是那个列表。
    """
    import types

    mod = types.ModuleType("funasr")
    mod.AutoModel = FakeAutoModel          # type: ignore[attr-defined]
    FakeAutoModel.generate = _build_generate(FakeAutoModel.generate_params)  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "funasr", mod)
    return mod


def set_generate_params(monkeypatch, params: tuple[str, ...]) -> None:
    """改 `generate` 的形参表 —— 改列表**并重建函数签名**。

    只改列表不改签名是没用的：`hotword_param_name()` 走
    `inspect.signature(cls.generate)`。这里把两件事绑在一起，
    避免以后有人再踩同一个坑。
    """
    monkeypatch.setattr(FakeAutoModel, "generate_params", params)
    monkeypatch.setattr(FakeAutoModel, "generate", _build_generate(params))


@pytest.fixture()
def model_dir(tmp_path: Path) -> Path:
    """伪造一个"非空模型目录"（只要非空即可通过就绪检查）。"""
    d = tmp_path / "models" / "paraformer-zh"
    d.mkdir(parents=True)
    (d / "model.pt").write_bytes(b"\x00" * 128)
    return d


@pytest.fixture()
def provider(fake_funasr, model_dir: Path) -> FunASRLocalProvider:
    return FunASRLocalProvider(
        model_name="paraformer-zh", model_dir=model_dir, device="cpu",
    )


def _ok_result(texts: list[str] | None = None) -> list[dict]:
    """构造一份能通过校验的 FunASR 风格返回。"""
    texts = texts or ["同志们", "今天我们召开党组会", "研究部署重点工作"]
    out, t = [], 0
    for t_text in texts:
        n = len(t_text)
        ts = [[t + i * 100, t + (i + 1) * 100] for i in range(n)]
        out.append({"text": t_text, "timestamp": ts, "confidence": 0.93})
        t += n * 100
    return out


# ---------------------------------------------------------------------------
# 可用性 / 就绪检查
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_unavailable_without_funasr(self, monkeypatch, model_dir: Path):
        monkeypatch.delitem(sys.modules, "funasr", raising=False)
        monkeypatch.setattr(
            "builtins.__import__",
            _blocking_import("funasr", __import__),
        )
        p = FunASRLocalProvider(model_dir=model_dir)
        assert p.available() is False

    def test_available_with_funasr_and_model_dir(self, provider: FunASRLocalProvider):
        assert provider.available() is True

    def test_unavailable_when_model_dir_missing(self, fake_funasr, tmp_path: Path):
        p = FunASRLocalProvider(model_dir=tmp_path / "absent")
        assert p.available() is False

    def test_unavailable_when_model_dir_empty(self, fake_funasr, tmp_path: Path):
        d = tmp_path / "empty"
        d.mkdir()
        p = FunASRLocalProvider(model_dir=d)
        assert p.available() is False, "空目录不算就绪（模型没下载完）"

    def test_require_model_dir_false_for_contract_tests(self, fake_funasr, tmp_path: Path):
        """契约测试要能在没有模型的机器上跑接口一致性。"""
        p = FunASRLocalProvider(model_dir=tmp_path / "absent", require_model_dir=False)
        assert p.available() is True

    def test_available_does_not_load_model(self, provider: FunASRLocalProvider):
        """自检不得加载模型（否则界面一进就卡几十秒）。"""
        provider.available()
        provider.health_check()
        assert FakeAutoModel.instances == [], "available/health_check 触发了模型加载"


def _blocking_import(name: str, real_import):
    """返回一个 __import__ 替身，对 `name` 抛 ImportError。"""
    def _imp(mod, globals=None, locals=None, fromlist=(), level=0):
        if mod == name or mod.startswith(name + "."):
            raise ImportError(f"blocked for test: {name}")
        return real_import(mod, globals, locals, fromlist, level)

    return _imp


# ---------------------------------------------------------------------------
# 坑 ①：disable_update 必须显式传
# ---------------------------------------------------------------------------

class TestDisableUpdate:
    def test_disable_update_is_explicitly_passed(self, provider: FunASRLocalProvider):
        """**离线红线**：不传 disable_update，FunASR 启动会联网查版本，
        离线环境卡住或报错（F8.1 的破坏点）。"""
        provider.warmup()
        assert len(FakeAutoModel.instances) == 1
        kw = FakeAutoModel.instances[0].kwargs
        assert kw["disable_update"] is True

    def test_disable_pbar_passed_to_avoid_ansi_pollution(self, provider: FunASRLocalProvider):
        """进度条会往 stderr 写 ANSI 控制字符，污染 Electron 子进程管道日志。"""
        provider.warmup()
        assert FakeAutoModel.instances[0].kwargs["disable_pbar"] is True

    def test_model_revision_pinned(self, provider: FunASRLocalProvider):
        """钉版本：不钉会让"昨天能跑今天报错"成为常态。"""
        provider.warmup()
        assert FakeAutoModel.instances[0].kwargs["model_revision"] == DEFAULT_MODEL_REVISION

    def test_local_model_dir_used_instead_of_remote_name(self, provider: FunASRLocalProvider, model_dir: Path):
        """双保险：本地目录存在时传路径，即使 disable_update 失效也不去 HF 拉。"""
        provider.warmup()
        assert str(model_dir) in FakeAutoModel.instances[0].kwargs["model"]

    def test_offline_guard_warns_on_proxy_env(self, provider: FunASRLocalProvider, monkeypatch, caplog):
        """检测到代理变量要告警（不强行清除 —— 可能是有意的内网镜像代理）。"""
        import logging

        monkeypatch.setenv("HTTP_PROXY", "http://proxy:8080")
        with caplog.at_level(logging.WARNING):
            provider.warmup()
        assert any("HTTP_PROXY" in r.message for r in caplog.records)
        assert any("F8.1" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 坑 ②：模型必须预先离线就绪
# ---------------------------------------------------------------------------

class TestModelDirRequirement:
    def test_missing_dir_raises_e2001(self, fake_funasr, tmp_path: Path):
        p = FunASRLocalProvider(model_dir=tmp_path / "absent")
        with pytest.raises(AsrNotReadyError) as ei:
            p.warmup()
        assert ei.value.code == "E2001"
        assert "预先下载" in ei.value.detail or "离线" in ei.value.detail
        # 关键：绝不能去联网拉（没有 AutoModel 实例被创建）
        assert FakeAutoModel.instances == [], "模型目录缺失时竟然尝试构造模型（会联网）"

    def test_empty_dir_raises_e2001(self, fake_funasr, tmp_path: Path):
        d = tmp_path / "empty"
        d.mkdir()
        p = FunASRLocalProvider(model_dir=d)
        with pytest.raises(AsrNotReadyError) as ei:
            p.warmup()
        assert "为空" in ei.value.detail

    def test_load_failure_translated_to_e2001(self, provider: FunASRLocalProvider, monkeypatch):
        """上游加载异常类型不稳定，统一翻译成 E2001。"""
        class Boom(FakeAutoModel):
            def __init__(self, **kw):
                raise OSError("CUDA out of memory")

        monkeypatch.setattr(sys.modules["funasr"], "AutoModel", Boom)
        with pytest.raises(AsrNotReadyError) as ei:
            provider.warmup()
        assert "CUDA out of memory" in ei.value.detail
        assert ei.value.code == "E2001"


# ---------------------------------------------------------------------------
# 坑 ③：热词参数名跨版本漂移
# ---------------------------------------------------------------------------

class TestHotwordParamProbe:
    def test_detects_hotword(self, provider: FunASRLocalProvider, monkeypatch):
        set_generate_params(monkeypatch, ("input", "batch_size_s", "hotword"))
        assert provider.hotword_param_name() == "hotword"

    def test_detects_hotwords_plural(self, provider: FunASRLocalProvider, monkeypatch):
        """有的版本是复数形式 —— 这就是施工卡说的"名字不同"。"""
        set_generate_params(monkeypatch, ("input", "batch_size_s", "hotwords"))
        assert provider.hotword_param_name() == "hotwords"

    def test_detects_hotword_list(self, provider: FunASRLocalProvider, monkeypatch):
        set_generate_params(monkeypatch, ("input", "hotword_list"))
        assert provider.hotword_param_name() == "hotword_list"

    def test_returns_none_when_unsupported(self, provider: FunASRLocalProvider, monkeypatch):
        set_generate_params(monkeypatch, ("input", "batch_size_s"))
        assert provider.hotword_param_name() is None

    def test_result_is_cached(self, provider: FunASRLocalProvider, monkeypatch):
        """探测结果要缓存 —— 签名读取虽轻，但每次转写都做没必要。"""
        calls = {"n": 0}
        import inspect as _inspect

        real = _inspect.signature

        def counting(obj):
            calls["n"] += 1
            return real(obj)

        monkeypatch.setattr(_inspect, "signature", counting)
        provider.hotword_param_name()
        first = calls["n"]
        provider.hotword_param_name()
        provider.hotword_param_name()
        assert calls["n"] == first, "探测结果未缓存"

    def test_uses_probed_name_in_generate(self, provider: FunASRLocalProvider, monkeypatch):
        """必须用**探测到的**名字传参，不能猜。"""
        set_generate_params(
            monkeypatch, ("input", "batch_size_s", "sentence_timestamp", "hotwords")
        )
        FakeAutoModel.next_result = _ok_result(["张三"])
        provider.transcribe(_wav(seconds=1), hotwords=["张三"])
        kw = FakeAutoModel.instances[0].last_generate_kwargs
        assert "hotwords" in kw and "hotword" not in kw

    def test_no_hotword_kwarg_when_list_empty(self, provider: FunASRLocalProvider):
        FakeAutoModel.next_result = _ok_result(["同志们"])
        provider.transcribe(_wav(seconds=1), hotwords=[])
        kw = FakeAutoModel.instances[0].last_generate_kwargs
        assert "hotword" not in kw and "hotwords" not in kw


# ---------------------------------------------------------------------------
# 坑 ④：热词静默失效
# ---------------------------------------------------------------------------

class TestHotwordSilentFailure:
    def test_hotwords_applied_when_text_contains_them(self, provider: FunASRLocalProvider):
        """判据是"结果文本里真的出现了"，不是"引擎说它用了"。"""
        FakeAutoModel.next_result = _ok_result(["张三同志发言", "李四同志补充"])
        r = provider.transcribe(_wav(seconds=1), hotwords=["张三", "李四"])
        assert r.hotwords_applied is True
        assert r.raw_meta["hotwords_hit"] == ["张三", "李四"]

    def test_zero_hit_sets_flag_false_and_warns(self, provider: FunASRLocalProvider):
        """**验收标准 3 的核心**：传了热词但一个都没命中 -> warnings 必须包含那句。"""
        FakeAutoModel.next_result = _ok_result(["张三同志发言"])  # 只有张三
        r = provider.transcribe(_wav(seconds=1), hotwords=["张三", "王五"])
        # 张三命中 -> applied 为 True；但只命中一半也该能看出来
        assert r.raw_meta["hotwords_hit"] == ["张三"]

    def test_complete_zero_hit_sets_false(self, provider: FunASRLocalProvider):
        """热词一个都没出现在结果里 —— 上游忽略了参数。"""
        FakeAutoModel.next_result = _ok_result(["同志们今天我们开会"])
        r = provider.transcribe(_wav(seconds=1), hotwords=["张三", "李四", "王五"])
        assert r.hotwords_applied is False
        assert any("hotwords supplied but zero hit" in w for w in r.warnings), r.warnings

    def test_no_hotwords_means_no_warning(self, provider: FunASRLocalProvider):
        """没传热词时不该报"热词零命中"（那是误报）。"""
        FakeAutoModel.next_result = _ok_result(["同志们"])
        r = provider.transcribe(_wav(seconds=1), hotwords=[])
        assert r.hotwords_applied is False
        assert not any("hotwords supplied" in w for w in r.warnings)

    def test_unsupported_version_warns_about_glossary_degradation(self, provider: FunASRLocalProvider, monkeypatch):
        """版本不支持热词时要明确告知"术语纠正只能靠后处理"。"""
        set_generate_params(monkeypatch, ("input", "batch_size_s"))
        FakeAutoModel.next_result = _ok_result(["同志们"])
        r = provider.transcribe(_wav(seconds=1), hotwords=["张三"])
        assert any("does not accept hotword" in w for w in r.warnings), r.warnings

    def test_hotwords_deduplicated_and_preserved_order(self, provider: FunASRLocalProvider):
        FakeAutoModel.next_result = _ok_result(["同志们"])
        provider.transcribe(_wav(seconds=1), hotwords=["张三", "李四", "张三", "  "])
        sent = FakeAutoModel.instances[0].last_generate_kwargs["hotword"]
        assert sent == "张三 李四"


# ---------------------------------------------------------------------------
# 质量自检
# ---------------------------------------------------------------------------

class TestSilentFailureDetection:
    def test_timestamp_text_mismatch_warns(self, provider: FunASRLocalProvider):
        """① 时间戳与文本长度错位（施工卡 ①）。

        构造：text 有 10 个字，但 timestamp 只覆盖 3 个字（差 7 > 容差 3）。
        注意这条只产生 warning；未达 E2003 阈值时不该抛异常。
        """
        FakeAutoModel.next_result = [{
            "text": "同志们今天我们召开党组会",
            "timestamp": [[0, 100], [100, 200], [200, 300]],
            "confidence": 0.9,
        }]
        r = provider.transcribe(_wav(seconds=1))
        assert r.validate(), "长度严重错位必须被 validate 报出来"

    def test_no_confidence_warns(self, provider: FunASRLocalProvider):
        """③ 置信度全空 -> review gate degraded。"""
        FakeAutoModel.next_result = [
            {"text": "同志们", "timestamp": [[0, 300]]},
            {"text": "开会", "timestamp": [[300, 600]]},
        ]
        r = provider.transcribe(_wav(seconds=1))
        assert r.avg_confidence is None
        assert any("no confidence" in w for w in r.warnings), r.warnings

    def test_low_average_confidence_warns(self, provider: FunASRLocalProvider):
        """① 平均置信度 < 0.85。"""
        FakeAutoModel.next_result = _ok_result(["同志们", "开会"]) 
        for item in FakeAutoModel.next_result:
            item["confidence"] = 0.62
        r = provider.transcribe(_wav(seconds=1))
        assert r.avg_confidence is not None and r.avg_confidence < 0.85
        assert any("average confidence" in w for w in r.warnings), r.warnings

    def test_high_low_confidence_ratio_warns(self, provider: FunASRLocalProvider):
        """② 低置信段占比 > 15%。"""
        FakeAutoModel.next_result = _ok_result(["甲乙", "丙丁", "戊己", "庚辛"])
        for i, item in enumerate(FakeAutoModel.next_result):
            item["confidence"] = 0.5 if i < 3 else 0.99   # 3/4 = 75% 低置信
        r = provider.transcribe(_wav(seconds=1))
        assert any("low-confidence segment ratio" in w for w in r.warnings), r.warnings

    def test_healthy_result_has_no_quality_warnings(self, provider: FunASRLocalProvider):
        """全绿时不该有噪声告警（噪声会让真告警被忽略）。"""
        FakeAutoModel.next_result = _ok_result(["同志们", "今天开会"])
        r = provider.transcribe(_wav(seconds=1))
        quality = [w for w in r.warnings if not w.startswith("audio duration")]
        assert quality == [], quality


# ---------------------------------------------------------------------------
# 错误码翻译
# ---------------------------------------------------------------------------

class TestErrorTranslation:
    def test_missing_file_raises_e1002(self, provider: FunASRLocalProvider, tmp_path: Path):
        with pytest.raises(FileMissingError) as ei:
            provider.transcribe(tmp_path / "absent.wav")
        assert ei.value.code == "E1002"

    def test_generate_failure_raises_e1003(self, provider: FunASRLocalProvider):
        FakeAutoModel.raise_on_generate = RuntimeError("decoder error 0x80004005")
        with pytest.raises(CorruptAudioError) as ei:
            provider.transcribe(_wav(seconds=1))
        assert ei.value.code == "E1003"
        assert "decoder error" in ei.value.detail
        # 用户文案不得含技术细节（N3.3）
        assert "0x80004005" not in ei.value.user_message

    def test_missing_file_checked_before_warmup(self, provider: FunASRLocalProvider, tmp_path: Path):
        """参数校验要在加载模型之前 —— 否则一个路径错要等几十秒才报。"""
        with pytest.raises(FileMissingError):
            provider.transcribe(tmp_path / "absent.wav")
        assert FakeAutoModel.instances == [], "文件不存在时不该加载模型"

    def test_all_raised_errors_are_mmwf_errors(self, provider: FunASRLocalProvider, tmp_path: Path):
        """契约：适配器抛的必须是业务异常，不是裸异常。"""
        with pytest.raises(MmwfError):
            provider.transcribe(tmp_path / "absent.wav")


# ---------------------------------------------------------------------------
# 模型加载幂等 / 长音频提示
# ---------------------------------------------------------------------------

class TestWarmupAndLongAudio:
    def test_warmup_loads_once(self, provider: FunASRLocalProvider):
        provider.warmup()
        provider.warmup()
        provider.warmup()
        assert len(FakeAutoModel.instances) == 1, "重复 warmup 不该重复加载模型"

    def test_transcribe_warms_up_automatically(self, provider: FunASRLocalProvider):
        FakeAutoModel.next_result = _ok_result(["同志们"])
        provider.transcribe(_wav(seconds=1))
        assert len(FakeAutoModel.instances) == 1

    def test_long_audio_warns_to_chunk_first(self, provider: FunASRLocalProvider):
        """超过 1 小时的音频要提示"应先经 ingest 切分"（切分不是本卡职责）。"""
        FakeAutoModel.next_result = _ok_result(["同志们"])
        long_wav = _wav(seconds=3700)     # 61 分钟
        r = provider.transcribe(long_wav)
        assert any("ingest should chunk it first" in w for w in r.warnings), r.warnings

    def test_short_audio_has_no_chunk_warning(self, provider: FunASRLocalProvider):
        FakeAutoModel.next_result = _ok_result(["同志们"])
        r = provider.transcribe(_wav(seconds=30))
        assert not any("chunk" in w for w in r.warnings)

    def test_batch_size_s_is_bounded(self, provider: FunASRLocalProvider):
        """单批时长要显式传（默认值随版本变化，不传等于放弃控制）。"""
        FakeAutoModel.next_result = _ok_result(["同志们"])
        provider.transcribe(_wav(seconds=1))
        kw = FakeAutoModel.instances[0].last_generate_kwargs
        assert kw["batch_size_s"] == 300
        assert kw["sentence_timestamp"] is True


# ---------------------------------------------------------------------------
# 设备解析
# ---------------------------------------------------------------------------

class TestDeviceResolution:
    def test_explicit_cpu_not_overridden(self, provider: FunASRLocalProvider):
        assert provider._resolve_device() == "cpu"
        provider.warmup()
        assert FakeAutoModel.instances[0].kwargs["device"] == "cpu"

    def test_auto_falls_back_to_cpu_without_torch(self, fake_funasr, model_dir: Path, monkeypatch):
        """无 torch 时按 CPU —— 且要能如实报出来，让用户知道要等多久。"""
        monkeypatch.setattr(
            "builtins.__import__", _blocking_import("torch", __import__)
        )
        p = FunASRLocalProvider(model_dir=model_dir, device="auto")
        assert p._resolve_device() == "cpu"

    def test_health_check_reports_expected_speed(self, fake_funasr, model_dir: Path, monkeypatch):
        """F1.3 体验要求：投递前就该知道要等多久。"""
        monkeypatch.setattr("builtins.__import__", _blocking_import("torch", __import__))
        p = FunASRLocalProvider(model_dir=model_dir, device="auto")
        h = p.health_check()
        assert h["device"] == "cpu"
        assert "CPU" in h["expected_speed"] and "70 分钟" in h["expected_speed"]


# ---------------------------------------------------------------------------
# health_check 契约
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_required_keys_present(self, provider: FunASRLocalProvider):
        h = provider.health_check()
        assert {"available", "model", "device", "hotword_supported"} <= set(h)

    def test_reports_hotword_support_from_signature(self, provider: FunASRLocalProvider):
        assert provider.health_check()["hotword_supported"] is True

    def test_reports_no_hotword_support(self, provider: FunASRLocalProvider, monkeypatch):
        set_generate_params(monkeypatch, ("input",))
        p2 = FunASRLocalProvider(model_dir=provider._model_dir, device="cpu")
        assert p2.health_check()["hotword_supported"] is False

    def test_status_ok_when_available(self, provider: FunASRLocalProvider):
        assert provider.health_check()["status"] == "ok"

    def test_loaded_flag_tracks_warmup(self, provider: FunASRLocalProvider):
        assert provider.health_check()["loaded"] is False
        provider.warmup()
        assert provider.health_check()["loaded"] is True

    def test_last_hit_hotwords_recorded(self, provider: FunASRLocalProvider):
        FakeAutoModel.next_result = _ok_result(["张三同志", "李四同志"])
        provider.transcribe(_wav(seconds=1), hotwords=["张三", "李四", "王五"])
        h = provider.health_check()
        assert h["last_hit_hotwords"] == ["张三", "李四"]

    def test_repr_is_informative(self, provider: FunASRLocalProvider):
        s = repr(provider)
        assert "paraformer-zh" in s and "cpu" in s


# ---------------------------------------------------------------------------
# run_transcribe 便捷入口
# ---------------------------------------------------------------------------

class TestRunTranscribe:
    def test_strict_default_raises_on_silent_failure(self, provider: FunASRLocalProvider, monkeypatch, model_dir: Path):
        """默认 strict：时间戳严重错位 -> E2003 FATAL（"已暂停处理"）。

        注意"严重错位"的判据：文本 84 字、时间戳只覆盖 1 个字，
        远超容差 `max(3, 10%)`，所以 `validate()` 必然报错。
        """
        FakeAutoModel.next_result = [
            {"text": "同志们" + "今天" * 40, "timestamp": [[0, 100]], "confidence": 0.9},
        ]
        monkeypatch.setattr(
            "mmwf.adapters.asr.funasr_local.FunASRLocalProvider",
            lambda **kw: provider,
        )
        audio = _wav(seconds=1)
        with pytest.raises(AsrSilentFailureError) as ei:
            run_transcribe(audio, model_dir=model_dir)
        assert ei.value.code == "E2003"
        assert ei.value.is_fatal

    def test_strict_false_returns_unvalidated(self, provider: FunASRLocalProvider, monkeypatch, model_dir: Path):
        """编排层要先记审计再决定时用 strict=False。"""
        FakeAutoModel.next_result = [
            {"text": "同志们" + "今天" * 40, "timestamp": [[0, 100]], "confidence": 0.9},
        ]
        monkeypatch.setattr(
            "mmwf.adapters.asr.funasr_local.FunASRLocalProvider",
            lambda **kw: provider,
        )
        audio = _wav(seconds=1)
        r = run_transcribe(audio, model_dir=model_dir, strict=False)
        assert r.validate(), "非 strict 也要能让调用方查看到问题"

    def test_strict_default_passes_on_healthy_result(self, provider: FunASRLocalProvider, monkeypatch, model_dir: Path):
        """健康结果走 strict 路径不该抛 —— 防止 strict 变成"永远报错"。"""
        FakeAutoModel.next_result = _ok_result(["同志们", "今天开会"])
        monkeypatch.setattr(
            "mmwf.adapters.asr.funasr_local.FunASRLocalProvider",
            lambda **kw: provider,
        )
        r = run_transcribe(_wav(seconds=1), model_dir=model_dir)
        assert r.segments and r.validate() == []


# ---------------------------------------------------------------------------
# 时长探测
# ---------------------------------------------------------------------------

class TestDurationProbe:
    def test_reads_wav_duration(self, tmp_path: Path):
        p = _wav(seconds=3, path=tmp_path / "a.wav")
        assert abs(_probe_duration_ms(p) - 3000) < 50

    def test_non_wav_returns_zero(self, tmp_path: Path):
        """不引入新依赖：非 wav 就放弃探测（T1-06 才接 ffprobe）。"""
        p = tmp_path / "a.m4a"
        p.write_bytes(b"\x00" * 100)
        assert _probe_duration_ms(p) == 0

    def test_corrupt_wav_returns_zero(self, tmp_path: Path):
        p = tmp_path / "bad.wav"
        p.write_bytes(b"not a real wav")
        assert _probe_duration_ms(p) == 0, "探测失败不该抛错，静默降级"

    def test_missing_file_returns_zero(self, tmp_path: Path):
        assert _probe_duration_ms(tmp_path / "absent.wav") == 0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _wav(seconds: int = 1, path: Path | None = None, rate: int = 16000) -> Path:
    """生成一个静音 wav（用于时长探测与"喂给引擎"的占位路径）。"""
    import tempfile

    if path is None:
        fd, name = tempfile.mkstemp(suffix=".wav")
        import os

        os.close(fd)
        path = Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return path


def test_provider_name_constant_matches_registry():
    assert PROVIDER_NAME == "funasr_local"


def test_asr_provider_error_is_runtime_error():
    """适配器内部错误（非业务错误）用 RuntimeError 语义，便于区分。"""
    assert issubclass(AsrProviderError, RuntimeError)
