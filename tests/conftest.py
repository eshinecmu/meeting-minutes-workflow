"""pytest 全局配置。

做三件事：
  ① 把 `tools/` 加进 `sys.path`；
  ② 把 `tests/fixtures/graph/` 加进 `sys.path`（T1-10 的编排替身）。
  ③ 把 `tests/` 加进 `sys.path`（W2 跨平台测试辅助函数）。

为什么要这样做
-------------
`pyproject.toml` 里 `pythonpath = ["src"]` 只把 `src/` 放进路径，
`tools/` 既不是包也不在路径上。而 T1-09 有**两个** `tools/` 下的脚本
需要被测试直接 import：

    · `tools/verify_docx_style.py`  —— 施工卡要求"必须写"的对照校验脚本，
      基准快照测试要**调用它本身**来判定基准合格（而不是在测试里
      重新实现一遍校验逻辑，那样两边会漂移）。
    · `tools/make_docx_fixtures.py` —— 基准的输入与钉住点定义。
      快照测试必须用**与生成时完全相同**的输入，否则测的是别的东西。

`tests/fixtures/graph/harness.py` 是 T1-10 的编排测试替身。
沿用 `tests/fixtures/asr/fake_engine.py` 的做法（把所在目录塞进
`sys.path` 后直接 import 模块名）：`tests/` 下没有 `__init__.py`，
按包路径 import 会失败。在 conftest 里统一加一次，
比在每个测试文件里重复 `sys.path.insert` 更不容易漏。

放在 `tests/conftest.py` 而不是在每个测试文件里 `sys.path.insert`：
后者容易漏、顺序敏感，而且每个文件都要重复一遍 sys / Path 的样板。
conftest 是 pytest 的官方扩展点，在收集测试之前就会执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

for _extra in (_HERE, _HERE.parent / "tools", _HERE / "fixtures" / "graph"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))


# C3 contract / integration 共用真实 HTTP、SQLite 与节点回调，不替换业务节点。
import pytest


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from mmwf.main import create_app
    from mmwf.settings import load_settings
    from mmwf.api.deps import create_session_token
    from mmwf.graph.runner import GraphRunner

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = load_settings(_HERE.parent / "config")
    cfg = cfg.model_copy(update={"app": cfg.app.model_copy(update={"data_dir": tmp_path / "data", "log_dir": tmp_path / "logs"})})
    calls = []
    bump = GraphRunner._bump_node

    def counted(self, name):
        calls.append((self.meeting_id, name))
        bump(self, name)

    monkeypatch.setattr(GraphRunner, "_bump_node", counted)
    app = create_app(cfg)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.headers["X-MMWF-Token"] = create_session_token()
        created = client.post("/api/v1/meetings", json={"title": "预算会", "record_markdown": "张三：研究预算执行。\n\n李四：讨论年度计划。"}).json()
        assert created["ok"], created
        mid = created["data"]["meeting_id"]
        base = f"/api/v1/meetings/{mid}"
        started = client.post(base + "/start").json()
        assert started["ok"], started
        yield SimpleNamespace(client=client, app=app, rt=app.state.runtime, mid=mid, base=base,
                              pending=started["data"]["pending_gate"], calls=calls)
