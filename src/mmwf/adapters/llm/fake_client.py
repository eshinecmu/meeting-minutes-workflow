"""测试用的假大模型客户端（T2-01）。

为什么必须有一个"假客户端"
--------------------------
本机**没有 LLM 运行环境**（施工卡 §2.4，6 条探测命令实测）。
但 T2-01 的核心命题是"**分块+归并不会丢信息**"，这个命题**必须**被验证 ——
否则整张卡就只剩"代码写完了"这一句空话。

★ `oracle` 模式：把"管道是否丢信息"与"模型质量"拆开
---------------------------------------------------
`oracle_items` 给出全部**已知**的正确条目（每条形如
`{"quote": "...", "kind": "resolution", ...}`）。假客户端对每块返回
"**该块文本里出现过的全部条目**" —— 即一个**全知、无损、不犯错的模型**。

于是：
  · 若最终结果召回了全部条目 → **管道无损**（分块/归并/溯源没丢东西）；
  · 若有条目丢失 → **管道的 bug**，且能被精确定位到是哪一环丢的。

这样就把"模型好不好"这个本卡管不了、也测不了的变量**隔离出去**了。
剩下的"真模型召回率 ≥95%"才如实记 `UNSUPPORTED`。

三种用法
--------
1. `FakeLlmClient(script=[...])` —— 按顺序返回预设字符串（测重试、测畸形输出）。
2. `FakeLlmClient(oracle_items=[...])` —— 全知模式（测管道无损）。
3. `FakeLlmClient(fn=callable)` —— 任意自定义（测特定分支）。
"""

from __future__ import annotations

import json
import re
import urllib.error
from typing import Any, Callable, Iterable, Sequence

from mmwf.adapters.llm.base import LlmError, LlmResponse

__all__ = ["FakeLlmClient", "build_oracle_minutes"]

PROVIDER_NAME = "fake"


class FakeLlmClient:
    """可脚本化的假客户端。实现 `LlmClient` 协议的全部方法。

    Args:
        script: 依次返回的字符串。用完后重复最后一个（避免测试里
            因为"块比预期多"而莫名其妙地 IndexError —— 那会掩盖真问题）。
        fn: 自定义 `fn(prompt) -> str`。与 `script` 二选一，`fn` 优先。
        oracle_items: 全知条目。每块返回"该块里出现过的"那些。
        fail_on_call: 第 N 次调用（**从 1 开始**）抛 `LlmError`，测"单块失败
            不拖垮全局"。
        available: `available()` 的返回值。
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        script: Sequence[str] | None = None,
        fn: Callable[[str], str] | None = None,
        oracle_items: Sequence[dict[str, Any]] | None = None,
        fail_on_call: int | None = None,
        available: bool = True,
    ) -> None:
        if fn is not None and script is not None:
            raise ValueError("fn 与 script 只能给一个")
        if fn is None and script is None and oracle_items is None:
            raise ValueError("必须给 script / fn / oracle_items 之一")

        self._fn = fn
        self._script = list(script or [])
        self._oracle = list(oracle_items or [])
        self._fail_on_call = fail_on_call
        self._available = available
        #: 每次调用的记录（prompt、system、是否传了 schema），供断言用。
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------

    def available(self) -> bool:
        return self._available

    def health_check(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "provider": self.name,
            "models_loaded": ["fake"],
            "note": "测试替身；不做真实推理",
        }

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        timeout_seconds: int | None = None,
    ) -> LlmResponse:
        n = len(self.calls) + 1
        self.calls.append({
            "n": n,
            "prompt": prompt,
            "system": system,
            "has_schema": json_schema is not None,
            "temperature": temperature,
        })

        if self._fail_on_call is not None and n == self._fail_on_call:
            raise LlmError(f"（测试注入）第 {n} 次调用失败")

        if self._fn is not None:
            try:
                return LlmResponse(self._fn(prompt), done_reason="stop")
            except TimeoutError as exc:
                raise LlmError(str(exc), code="E3003") from exc
            except urllib.error.URLError as exc:
                code = "E3003" if isinstance(exc.reason, TimeoutError) else "E3004"
                raise LlmError(str(exc), code=code) from exc

        if self._oracle:
            return LlmResponse(
                json.dumps(build_oracle_minutes(prompt, self._oracle),
                           ensure_ascii=False),
                done_reason="stop",
            )

        # script 模式：用完了就重复最后一个
        idx = min(n - 1, len(self._script) - 1)
        return LlmResponse(self._script[idx], done_reason="stop")


# ---------------------------------------------------------------------------
# oracle：全知模式
# ---------------------------------------------------------------------------

#: 从提示词里取回块正文。`prompts.render_prompt` 把正文放在
#: 「【本段文本】」之后 —— 这里按同一约定切回来。
_TEXT_MARK = "【本段文本】"


def extract_chunk_text(prompt: str) -> str:
    """从提示词里切出块正文（供假客户端判断"这块里有什么"）。"""
    if _TEXT_MARK in prompt:
        return prompt.split(_TEXT_MARK, 1)[1]
    return prompt


def _norm(s: str) -> str:
    """与 `grounding.normalize_punct` 同源的宽松归一化（去空白 + 常见标点）。"""
    out = re.sub(r"\s+", "", s or "")
    for k, v in (("，", ","), ("。", "."), ("！", "!"), ("？", "?"),
                 ("；", ";"), ("：", ":"), ("、", ",")):
        out = out.replace(k, v)
    return out


def build_oracle_minutes(
    prompt: str,
    oracle_items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """构造"全知"的 `MinutesSchema` 形状输出。

    对每一条 oracle 条目：**若它的 `quote` 出现在本块正文里**，就把它放进
    结果。这就是"一个永不犯错的模型"的行为 —— 不推断、不遗漏、不编造。

    条目字段（除 `quote` 外都可选）::

        {"quote": "...", "kind": "resolution|action_item|pending_decision|topic",
         "content": "...", "owner": "...", "deadline": "...",
         "name": "...", "discussion": "...", "topic": "所属议题名"}

    `kind` 缺省为 `resolution`。归属议题按条目的 `topic` 字段分组；
    没给 `topic` 的条目归到 `"会议主要内容"`。
    """
    chunk = _norm(extract_chunk_text(prompt))
    hits = [it for it in oracle_items
            if it.get("quote") and _norm(str(it["quote"])) in chunk]

    by_topic: dict[str, dict[str, Any]] = {}
    for it in hits:
        tname = str(it.get("topic") or "会议主要内容")
        bucket = by_topic.setdefault(tname, {
            "name": tname,
            "discussion": str(it.get("discussion") or ""),
            "readings": [],
            "resolutions": [],
            "action_items": [],
            "pending_decisions": [],
        })
        kind = str(it.get("kind") or "resolution")
        entry: dict[str, Any] = {
            "content": str(it.get("content") or it.get("quote") or ""),
            "owner": str(it.get("owner") or "暂无"),
            "deadline": it.get("deadline"),
            "source_quote": str(it.get("quote") or ""),
            "source_start_ms": it.get("start_ms"),
            "source_end_ms": it.get("end_ms"),
        }
        if kind == "topic":
            continue
        if kind == "action_item":
            bucket["action_items"].append(entry)
        elif kind == "pending_decision":
            entry["decision_maker"] = str(it.get("decision_maker") or "暂无")
            bucket["pending_decisions"].append(entry)
        else:
            bucket["resolutions"].append(entry)

    # 只有议题、没有任何事项的块也要能被"抽到"（否则议题会被整块漏掉）
    for it in hits:
        if str(it.get("kind") or "") != "topic":
            continue
        tname = str(it.get("name") or it.get("topic") or "会议主要内容")
        by_topic.setdefault(tname, {
            "name": tname,
            "discussion": str(it.get("discussion") or ""),
            "readings": [],
            "resolutions": [],
            "action_items": [],
            "pending_decisions": [],
        })

    return {
        "title": "会议纪要",
        "meeting_time": "",
        "location": "",
        "host": "",
        "attendees": [],
        "recorder": "",
        "topics": list(by_topic.values()),
        "schema_version": "1.0",
    }
