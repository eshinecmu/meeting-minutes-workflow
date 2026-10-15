"""Step 4 全局补全（SAD §10.1 Step 4）。

    Step 4  全局补全：
            - 检查模板要求的必填要素是否齐备，缺失项显式标记为"待补充"
            - 不编造缺失内容（红旗项：无依据编造）

★ 本模块只做"**标记**"，不做"**拦截**"
--------------------------------------
缺失的必填要素在这里被标成 `"待补充"` 并产出一条 issue；
**要不要因为缺要素而阻止成文，是 T2-04（后置校验）的决定**。
本卡越界去拦截，会把 T2-04 的职责提前吃掉，而且拦截策略（硬拦还是送人工）
本来就要和问题清单一起设计。

★ 为什么标 `"待补充"` 而不是留空
--------------------------------
留空的表现是"纪要里那栏什么都没有"—— 用户看不出是"会上没提"还是"系统漏了"。
标成 `"待补充"` 是一个**显式的、可见的**信号，符合"不静默降级"。
而且它与 `owner` 的 `"暂无"`（§10.4 规则 1）**语义不同**，别混用：
`owner="暂无"` 是"确实没有责任人"（合法终态），
`"待补充"` 是"这里本该有信息但我们没拿到"（待办态）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "MISSING_MARKER",
    "REQUIRED_ELEMENTS",
    "complete_missing",
]

#: 缺失标记。**用这个词，别改成"无"/"未知"** ——
#: `accept_t2_01.py` 与 T2-04 的问题清单都按它识别。
MISSING_MARKER = "待补充"

#: 纪要的必填要素（SAD §10.2 `MinutesSchema` 里非 list 的标量字段）。
#:
#: ⚠️ **不含 `topics`**：议题为空是"这次会议没内容"，
#: 不是"我们没拿到信息"，两者的处置完全不同（前者该报 E4003，后者才是待补充）。
#: 也不含 `attendees`（list 型，空列表本身有语义："没记录出席人"）。
REQUIRED_ELEMENTS: tuple[str, ...] = (
    "title", "meeting_time", "location", "host", "recorder",
)

#: 中文名，给问题清单用（界面不该出现英文字段名）。
_ELEMENT_LABELS: dict[str, str] = {
    "title": "标题",
    "meeting_time": "会议时间",
    "location": "会议地点",
    "host": "主持人",
    "recorder": "记录人",
}


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip() or v.strip() == MISSING_MARKER
    if isinstance(v, (list, tuple, dict, set)):
        return len(v) == 0
    return False


def complete_missing(
    minutes: Mapping[str, Any] | None,
    *,
    required: Iterable[str] = REQUIRED_ELEMENTS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """检查必填要素，缺失的显式标 `"待补充"`。

    Args:
        minutes: 结构化纪要（dict）。`None` / 非映射 → 视为全缺。
        required: 必填字段名。

    Returns:
        `(补齐后的 dict, issues)`。**不抛异常**（U-EX-10：空输入要能处理）。

    ★ **只填标记，不填内容。** 这里绝不"猜一个会议地点"之类的操作 ——
    那是"无依据编造"（红旗项）。标记的作用恰恰是**让人看见这里缺**。
    """
    src: Mapping[str, Any] = minutes if isinstance(minutes, Mapping) else {}
    out: dict[str, Any] = dict(src)
    issues: list[dict[str, Any]] = []

    for field_name in required:
        if not _is_blank(out.get(field_name)):
            continue
        out[field_name] = MISSING_MARKER
        issues.append({
            "rule": "missing_required_element",
            "field": field_name,
            "detail": f"必填要素「{_ELEMENT_LABELS.get(field_name, field_name)}」缺失，"
                      f"已标记为「{MISSING_MARKER}」，请人工补充",
        })

    # 出席人为空单独报一条（它是 list，不进 REQUIRED_ELEMENTS，见上）
    if _is_blank(out.get("attendees")):
        out["attendees"] = []
        issues.append({
            "rule": "missing_required_element",
            "field": "attendees",
            "detail": f"未记录出席人员，已标记为「{MISSING_MARKER}」，请人工补充",
        })

    return out, issues
