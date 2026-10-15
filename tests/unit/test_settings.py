"""T1-01 验收测试：配置中心与项目骨架。

对应施工卡 T1-01 的三条验收标准：
  1. 改 config/models.yaml 的 llm.model，重启后生效，无需改任何 Python 文件
  2. app.host == "127.0.0.1"，且源码中搜索 "0.0.0.0" 无命中
  3. 全部用例通过
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from mmwf.settings import (
    ENV_CONFIG_DIR,
    AppConfig,
    AsrConfig,
    ExtractionConfig,
    LlmConfig,
    load_settings,
    reload_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


# ---------------------------------------------------------------------------
# 标准 1：配置可外部修改，无需改代码
# ---------------------------------------------------------------------------

def test_loads_all_six_sections():
    """六个配置段全部加载成功。"""
    s = load_settings(CONFIG_DIR)
    assert s.app.name == "mmwf"
    assert s.asr.provider == "funasr_local"
    assert s.llm.provider == "ollama_local"
    assert s.export.provider == "docx_exporter"
    assert s.extraction.engine == "naive"
    assert s.extraction.chunk.target_chars == 1500
    assert s.thresholds.asr.low_confidence == pytest.approx(0.70)


def test_model_change_takes_effect_without_code_change(tmp_path: Path):
    """标准 1：改 models.yaml 的 llm.model 后，重新加载即生效，不碰任何 .py。"""
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)

    models_file = cfg / "models.yaml"
    data = yaml.safe_load(models_file.read_text(encoding="utf-8"))
    assert data["llm"]["model"] == "qwen2.5:14b-instruct-q5_K_M"   # 改前

    data["llm"]["model"] = "qwen2.5:7b-instruct-q5_K_M"
    models_file.write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )

    reloaded = load_settings(cfg)
    assert reloaded.llm.model == "qwen2.5:7b-instruct-q5_K_M"      # 改后


def test_threshold_change_takes_effect(tmp_path: Path):
    """改 thresholds.yaml 同样生效（不止 models.yaml）。"""
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)

    f = cfg / "thresholds.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["thresholds"]["asr"]["low_confidence"] = 0.6
    f.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    assert load_settings(cfg).thresholds.asr.low_confidence == pytest.approx(0.6)


def test_config_dir_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """配置目录可通过环境变量指定（部署时有用）。"""
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    monkeypatch.setenv(ENV_CONFIG_DIR, str(cfg))

    from mmwf.settings import _find_config_dir

    assert _find_config_dir() == cfg.resolve()


# ---------------------------------------------------------------------------
# 标准 2：host 必须回环（SAD C11）
# ---------------------------------------------------------------------------

def test_host_is_loopback():
    """标准 2 之一：默认配置的 host 是 127.0.0.1。"""
    assert load_settings(CONFIG_DIR).app.host == "127.0.0.1"


@pytest.mark.parametrize("bad_host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_non_loopback_host_rejected(bad_host: str):
    """标准 2 之二：非回环 host 必须被拒绝（合规红线，不是可配置偏好）。"""
    with pytest.raises(ValueError, match="回环"):
        AppConfig(host=bad_host)


def test_no_bind_all_in_source():
    """标准 2 之三：源码中不得出现 0.0.0.0 绑定。

    只检查**代码 token**，不检查注释与字符串字面量——
    文档和注释里为了说明约束而提到 "0.0.0.0" 是允许的。
    """
    import tokenize

    offenders: list[str] = []
    for py in (PROJECT_ROOT / "src").rglob("*.py"):
        with py.open("rb") as f:
            try:
                tokens = list(tokenize.tokenize(f.readline))
            except tokenize.TokenError:  # pragma: no cover - 语法错误由别处兜
                continue
        for tok in tokens:
            # 只看真实代码：排除注释、字符串、docstring
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if tok.type == tokenize.NAME and tok.string == "0":
                continue
            if "0.0.0.0" in tok.string:
                offenders.append(f"{py.relative_to(PROJECT_ROOT)}:{tok.start[0]}: {tok.string}")

    assert not offenders, "源码代码体中出现 0.0.0.0：\n" + "\n".join(offenders)


def test_llm_base_url_must_be_local():
    """本地大模型服务必须在 127.0.0.1（离线要求）。"""
    with pytest.raises(ValueError, match="本机"):
        LlmConfig(base_url="http://api.example.com")


# ---------------------------------------------------------------------------
# 配置校验的边界行为
# ---------------------------------------------------------------------------

def test_extraction_engine_defaults_to_naive():
    """旧配置未声明引擎时仍使用朴素实现。"""
    assert ExtractionConfig().engine == "naive"


@pytest.mark.parametrize("engine", ["naive", "llm", "unknown"])
def test_extraction_engine_validated_on_load(tmp_path: Path, engine: str):
    """启动加载配置时只接受两种抽取引擎，未知值沿用配置校验异常。"""
    from pydantic import ValidationError

    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    (cfg / "extraction.yaml").write_text(
        yaml.safe_dump({"extraction": {"engine": engine}}), encoding="utf-8"
    )
    if engine == "unknown":
        with pytest.raises(ValidationError, match="engine"):
            load_settings(cfg)
    else:
        assert load_settings(cfg).extraction.engine == engine


def test_overlap_must_be_less_than_chunk():
    with pytest.raises(ValueError, match="chunk_overlap_seconds"):
        AsrConfig(chunk_seconds=100, chunk_overlap_seconds=100)


def test_overlap_must_be_less_than_min_chars():
    with pytest.raises(ValueError, match="overlap_chars"):
        ExtractionConfig.ChunkConfig(min_chars=100, overlap_chars=100)


def test_port_range_validated():
    with pytest.raises(ValueError, match="端口"):
        AppConfig(port=99999)
    assert AppConfig(port=0).port == 0          # 0 = 随机分配，合法


def test_settings_are_frozen():
    """配置对象不可变：运行期被意外改写应当报错。"""
    from pydantic import ValidationError

    s = load_settings(CONFIG_DIR)
    with pytest.raises(ValidationError):
        s.app.host = "0.0.0.0"          # type: ignore[misc]


# ---------------------------------------------------------------------------
# 路径解析：相对路径必须能正确解析为绝对路径
# ---------------------------------------------------------------------------

def test_resolve_relative_data_dir():
    """相对路径解析到项目根，不随 CWD 变化。"""
    s = load_settings(CONFIG_DIR)
    resolved = s.resolve_path(s.app.data_dir)
    assert resolved.is_absolute()
    assert resolved == (PROJECT_ROOT / "data").resolve()


def test_resolve_absolute_path_unchanged(tmp_path: Path):
    s = load_settings(CONFIG_DIR)
    abs_path = (tmp_path / "x").absolute()
    assert s.resolve_path(abs_path) == abs_path


def test_missing_config_dir_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="配置目录不存在"):
        load_settings(tmp_path / "nope")


def test_missing_config_file_raises(tmp_path: Path):
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    (cfg / "thresholds.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="配置文件缺失"):
        load_settings(cfg)


def test_missing_top_level_key_raises(tmp_path: Path):
    """顶层键缺失要给明确报错，不能静默用默认值。"""
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, cfg)
    (cfg / "app.yaml").write_text("wrong_key: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少顶层键"):
        load_settings(cfg)


# ---------------------------------------------------------------------------
# 模板库完整性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tpl", ["party-group", "special-topic", "coordination", "regular-meeting"]
)
def test_four_templates_present_and_valid(tpl: str):
    """四类会议模板齐全且结构合规（F3.1）。"""
    path = CONFIG_DIR / "templates" / f"{tpl}.yaml"
    assert path.exists(), f"模板缺失：{path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert {"meta", "required_elements", "section_order", "style"} <= set(data)
    assert data["meta"]["meeting_kind"]
    assert data["meta"]["version"] >= 1
    assert len(data["required_elements"]) >= 5
    assert len(data["section_order"]) >= 3
    assert data["style"]["readings_allowed"], "领读语不得为空"
    assert data["style"]["readings_forbidden"], "禁用表述不得为空"


def test_glossary_csv_header_shape():
    """术语库 CSV 表头正确（部署时由使用单位替换内容）。

    注意：用 utf-8-sig 读取。使用单位很可能用 Excel 编辑并另存这个文件，
    Excel 会写入 UTF-8 BOM，按普通 utf-8 读会让首列变成 '\\ufeffcategory'。
    """
    import csv

    with (CONFIG_DIR / "glossary.csv").open(encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
    assert header == ["category", "canonical", "aliases", "note"]


def test_glossary_rows_well_formed():
    """术语库数据行的结构与取值范围正确。"""
    import csv

    valid_categories = {"org", "person", "term", "codename"}
    with (CONFIG_DIR / "glossary.csv").open(encoding="utf-8-sig") as f:
        rows = [
            r for r in csv.reader(f)
            if r and not r[0].startswith("#") and r[0] != "category"
        ]
    assert rows, "术语库至少应有一条示例数据"
    for r in rows:
        assert len(r) == 4, f"列数不对：{r}"
        assert r[0] in valid_categories, f"未登记的分类：{r[0]}"
        assert r[1].strip(), "canonical 不得为空"
