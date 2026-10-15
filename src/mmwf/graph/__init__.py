"""流水线编排层（SAD §5，T1-10）。

这一层是"谁先谁后、在哪停、停了怎么接着跑"的**唯一**描述处。

在整体架构里的位置
------------------
    api/  ← HTTP 入口，只管收参数与回响应
    app/services/  ← 用例编排（一次"投递音频"这类动作）
    **graph/**  ← 端到端流水线（一次"从投递到成文"的全过程）★本层
    domain/  ← 业务规则（纯逻辑，可独立测试）
    adapters/ infra/  ← 外部世界的边界

`app/services` 与 `graph` 的分工容易被混淆，这里说清：

    `app/services` 负责**单个动作**（投递音频、生成核对稿、导出 docx），
    它可以被人单独调用、可以失败后重试一次，是无状态的。
    `graph` 负责**动作之间的顺序与状态推进**，它必须持久化
    （所以有 `task_state` / 检查点），且必须能从中断处续跑。

判定方法：**"这个函数需要知道上一个动作做没做吗？"**
需要 → 它是 graph 的；不需要 → 它是 service 的。

模块地图
--------
    state.py        —— 状态词表（16 个状态 / 11 个节点 / 界面文案 / 路由表）
    idempotency.py  —— 幂等键与幂等记录表（重复执行同一个节点不产生第二份副作用）
    interrupts.py   —— 闸口契约（G2/G3 的中断点、三种决策、恢复路由）
    nodes/          —— 11 个节点函数（每个节点一个薄封装）
    build.py        —— StateGraph 组装 + 检查点持久化
    runner.py       —— 运行入口（启动 / 恢复 / 查询）

依赖策略
--------
`langgraph` 是**惰性依赖**：`state` / `idempotency` / `interrupts` 三个模块
在导入时不碰它，所以测试"状态词表""幂等键计算""路由函数"这些纯逻辑
不需要装 langgraph。真正 import 它的只有 `build.py` 与
`interrupts._interrupt()` 那一行。

这与 T1-09 把 `python-docx` 收在 `_docx_backend.py` 是同一种做法：
**把重依赖收在尽可能少的文件里**，让绝大部分逻辑保持可裸测。
"""

from __future__ import annotations

__all__: list[str] = []
