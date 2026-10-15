"""流水线节点集合（T1-10，SAD §5.1 的 11 个节点）。

节点清单与所属卡
----------------
    ============== ========================================== ===========
    节点             做什么                                      领域卡
    ============== ========================================== ===========
    ingest          音频校验 / 探测 / 切分计划 / 切分            T1-06
    transcribe      本地 ASR + 热词 + 术语回改                   T1-05/T1-07
    review          低置信分流 + 生成记录稿                      T1-08
    extract         议题 / 决议 / 待办抽取                       二期 T2-01
    gate_g2         ★ 待确认议题（中断点）                       T1-10
    generate        议题 -> 结构化纪要                          二期 T2-02
    validate        自检，产出问题清单                           二期 T2-04
    assemble        结构化 -> 成稿 Markdown                      二期 T2-02
    gate_g3         ★ 待审定纪要（中断点）                       T1-10
    export          导出 docx（幂等）                            T1-09
    archive         归档并写台账（幂等）                         T1-10
    ============== ========================================== ===========

工厂约定
--------
每个 `make_*` 都接受 **`deps`**（依赖容器，`build.py` 传）和一组
`**` 关键字覆盖（测试传替身）。两种入口并存的理由：

    · `deps` 让 `build.py` 用一行就把全部依赖串起来；
    · 关键字覆盖让单测**只替换要替的那一个**（比如只换 ASR 引擎），
      不必伪造整个 deps 容器。

`deps` 的形状约定（`build.py` 有 `Deps` 数据类）::

    deps.settings       Settings
    deps.idem_store     IdempotencyStore | None
    deps.asr_provider   AsrProvider | None      （None -> 按配置构造）
    deps.llm            Any | None              （二期用）
    deps.audit          AuditLogger | None

为什么节点都是**工厂**而不是普通函数
----------------------------------
节点需要"外部世界"（服务、引擎、配置、幂等表）。写成普通函数就只能
靠模块级单例或全局变量，那样单测无法替换任何一个依赖 ——
而本卡有一半价值在于"能确定性地验证编排语义"，必须可替换。
"""

from __future__ import annotations

from typing import Any

from mmwf.graph.nodes._base import (
    DependencyMissingError,
    NodeInputError,
    append_error,
    initial_state,
    read_task_state,
    session_of,
    touch_task_state,
)
from mmwf.graph.nodes.extraction import (
    MAX_VALIDATE_RETRY,
    make_assemble,
    make_extract,
    make_generate,
    make_llm_extractor,
    make_validate,
    naive_assemble,
    naive_extract,
    naive_generate,
    naive_validate,
)
from mmwf.graph.nodes.export import make_archive, make_export
from mmwf.graph.nodes.ingest import make_ingest
from mmwf.graph.nodes.review import make_review
from mmwf.graph.nodes.transcribe import make_transcribe

__all__ = [
    "DependencyMissingError",
    "MAX_VALIDATE_RETRY",
    "NodeInputError",
    "append_error",
    "initial_state",
    "make_archive",
    "make_assemble",
    "make_export",
    "make_extract",
    "make_generate",
    "make_gate_g2",
    "make_gate_g3",
    "make_ingest",
    "make_llm_extractor",
    "make_review",
    "make_transcribe",
    "make_validate",
    "naive_assemble",
    "naive_extract",
    "naive_generate",
    "naive_validate",
    "read_task_state",
    "session_of",
    "touch_task_state",
]


def make_gate_g2(deps: Any = None):
    """G2「待确认议题」闸口节点。

    实现在 `graph.interrupts`（那里有完整的三种决策语义）。
    本函数只是把它挂到 `nodes` 命名空间下，与施工卡骨架
    （`nodes.make_gate_g2(deps)`）一致。
    """
    from mmwf.graph import interrupts as I

    return I.make_gate_g2()


def make_gate_g3(deps: Any = None):
    """G3「待审定纪要」闸口节点（实现在 `graph.interrupts`）。"""
    from mmwf.graph import interrupts as I

    return I.make_gate_g3()
