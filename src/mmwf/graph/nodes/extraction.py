"""`extract` / `generate` / `validate` / `assemble` 四个节点 ——
议题抽取、纪要起草、自检、成文（二期领域模块的编排侧封装）。

⚠️ 这四个节点现在是**委托壳**，不是完整实现
--------------------------------------------
它们对应的领域模块 —— `domain/extraction`（分块分层抽取）、
`domain/minutes`（体例化成文）、`domain/grounding`（溯源校验）——
在施工排期里属于**二期**（T2-01 / T2-02 / T2-04），当前是空包
（只剩 docstring）。

但 **T1-10 必须现在做完**，因为它要交付的是"编排能力"：
一次触发走完全程、闸口能停能恢复、断点能续、副作用幂等。
这些能力**不依赖**抽取质量好不好 —— 用一份确定性的
"朴素实现"就能把整条链路跑通并验收。

所以本模块的取舍是：

    · 提供一份**确定性的朴素实现**（`naive_extract` / `naive_generate`
      / `naive_validate` / `naive_assemble`）：真的产出议题、真的渲染
      Markdown，只是质量是"能用"级别，不是二期那种"分块抽取 + 语体对齐"。
    · 留出 `extractor` / `generator` / `assembler` / `validator` 四个
      注入点：二期把真正的领域实现插进来，**图不用改一行**。
    · `naive_*` 的产物结构与二期实现**约定成同一套形状**
      （议题 = `{"title", "refs", "owner"}`），这样二期替换实现时
      下游（闸口送审、导出、溯源）的代码不用跟着改。

这条"先用朴素实现跑通编排、后替换实现"的路径是有意选的：
如果等二期领域模块做完再做编排，整个一期的编排能力就无从验收，
而"编排"恰恰是本卡唯一要证明的东西。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Sequence

from mmwf.graph import idempotency as idem
from mmwf.graph import state as S
from mmwf.graph.nodes import _base as B
from mmwf.graph.nodes.ingest import _Cfg, _resolve

logger = logging.getLogger(__name__)

#: 议题抽取时认的"决议/待办"引导词。
#:
#: 朴素实现靠这些词把段落分类。二期会换成 LLM 抽取，但这份词表
#: 仍有价值 —— 它是**离线兜底**（LLM 不可用时至少还能产出结构）。
RESOLUTION_CUES: tuple[str, ...] = (
    "决定", "决议", "确定", "通过", "同意", "原则同意", "议定",
)
ACTION_CUES: tuple[str, ...] = (
    "负责", "落实", "推进", "完成", "跟进", "牵头", "配合", "于", "前",
)

#: 责任人抽取的正则（"由张三负责" / "李四处落实" / "请赵六牵头"）。
#:
#: ⚠️ 引导动词**必须放在捕获组外面**。第一版写成了
#: `(?:由|请)?([\u4e00-\u9fa5]{2,4})(?:同志)?(?:负责|牵头|落实|推进)`，
#: 因为 `[\u4e00-\u9fa5]{2,4}` 是**贪婪**的，它会把"负责"也吃进名字里 ——
#: 匹配"请赵六负责落实"时得到名字 `赵六负责`（见 2026-09-27 记忆日志）。
#:
#: 修法是显式排除引导词：用负向断言限制"名字里不能出现这些动词"。
#: 中文没有词边界，所以只能这样约束。同时把动词表提到外面，
#: 让 `_strip_owner_verb` 能把名字里残留的动词剥掉（双保险）。
_OWNER_VERB = "负责|牵头|落实|推进|跟进|完成|配合"
_OWNER_RE = re.compile(
    rf"(?:由|请|让)?([\u4e00-\u9fa5]{{2,4}}?)(?:同志)?(?:{_OWNER_VERB})"
)

#: 名字里不允许出现的字（引导动词的首字）。
#: 出现在名字里说明正则贪婪没被限制住，用它在抽取后做一次清理。
_OWNER_VERB_HEADS: tuple[str, ...] = ("负", "牵", "落", "推", "跟", "完", "配")

#: 自检规则：(规则名, 判定函数, 说明)。命中即产出一条问题。
#:
#: 只做"结构性"检查，不判内容对错 —— 内容对错要溯源（二期 grounding）。
#: 这里的检查都能在离线、无模型的情况下确定性地判出来。
VALIDATION_RULES: tuple[tuple[str, str], ...] = (
    ("no_topics", "没有任何议题，纪要将无从展开"),
    ("topic_without_refs", "有议题没有来源依据，无法追溯"),
    ("resolution_without_refs", "有决议没有来源依据，无法追溯"),
    ("empty_topic_title", "有议题标题为空"),
    ("action_without_owner", "有待办事项未指定责任人"),
)

#: 自检最多重试几次（SAD §5.1 的 `retry` 边）。
#:
#: 超过后带问题清单成文（`give_up`）而不是静默失败 ——
#: 施工卡明写"2 次重试后带问题清单进入 assemble，不静默丢弃"（C03 配套）。
MAX_VALIDATE_RETRY = 2


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def make_extract(
    deps: Any = None,
    *,
    extractor: Callable[[str], dict[str, Any]] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `extract` 节点（议题 / 决议 / 待办抽取）。

    抽取实现的解析顺序（T2-01 接真引擎）：

        显式 `extractor` 参数  >  `deps.extractor`  >  `naive_extract`

    ★ **默认仍是 `naive_extract`**。只有显式注入才用真引擎 ——
    这样一期已验收的编排测试（"approve 后 extract 不重跑"等）
    **行为完全不变**。本卡只加能力，不改既有语义。
    要真引擎用 `make_llm_extractor(llm, settings)` 构造后注入
    （见 `Deps.extractor`）。

    Args:
        extractor: 抽取实现。`record_markdown -> {topics, resolutions,
            action_items}`。默认按上面的顺序解析。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)
    impl = extractor or getattr(deps, "extractor", None) or naive_extract

    def extract(state: dict) -> dict:
        # 幂等字段表里 `extract` 的输入是 `record_markdown`，
        # 所以这里不用 idempotent()（它要求输入字段已存在且非空），
        # 只在节点入口做前置校验。
        B.require_inputs(S.NODE_EXTRACT, state)
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_EXTRACT)

        md = str(state.get("record_markdown") or "")
        got = B.guard(S.NODE_EXTRACT, lambda: impl(md))
        topics = list(got.get("topics") or [])
        resolutions = list(got.get("resolutions") or [])
        actions = list(got.get("action_items") or [])

        errors = B.append_error(
            state, "E4003", S.NODE_EXTRACT,
            f"未抽取到任何议题（记录稿 {len(md)} 字）",
        ) if not topics else list(state.get("errors") or [])

        logger.info("extract 完成：%d 议题 / %d 决议 / %d 待办",
                    len(topics), len(resolutions), len(actions))
        out: dict[str, Any] = {
            "topics": topics,
            "resolutions": resolutions,
            "action_items": actions,
            # 清掉上一轮闸口的路由标记（见 interrupts.clear_route_marker）
            "_gate_route": None,
        }
        if not topics:
            out["errors"] = errors
        return out

    extract.__name__ = S.NODE_EXTRACT
    return extract


def naive_extract(record_markdown: str) -> dict[str, Any]:
    """朴素抽取：按引导词分句，产出议题 / 决议 / 待办。

    **确定性**：同样的输入永远得到同样的输出。这是刻意要求的 ——
    编排测试要断言"approve 后 extract 不重跑"，如果抽取结果随机，
    那个断言就没法区分"没重跑"和"重跑了但结果碰巧一样"。
    """
    sentences = _split_sentences(record_markdown)
    topics: list[dict] = []
    resolutions: list[dict] = []
    actions: list[dict] = []

    for idx, (text, seg_ref) in enumerate(sentences, start=1):
        if not text.strip():
            continue
        refs = [seg_ref] if seg_ref else []
        if _has_any(text, RESOLUTION_CUES):
            resolutions.append({"text": text, "refs": refs, "seq": idx})
        elif _looks_like_action(text):
            owner = _extract_owner(text)
            actions.append({
                "text": text,
                "owner": owner,
                "refs": refs,
                "seq": idx,
            })
        else:
            topics.append({"title": _clip(text, 40), "refs": refs, "seq": idx})

    # 议题太碎没有可读性：把前若干条合并成一条"会议主要内容"。
    if len(topics) > 20:
        topics = [{
            "title": "会议主要内容",
            "refs": [r for t in topics for r in t.get("refs") or []],
            "seq": 1,
            "merged_from": len(topics),
        }]

    return {"topics": topics, "resolutions": resolutions, "action_items": actions}


# ---------------------------------------------------------------------------
# 真引擎工厂（T2-01）
# ---------------------------------------------------------------------------

def make_llm_extractor(llm: Any, settings: Any = None,
                       *, max_workers: int | None = None) -> Callable[[str], dict]:
    """用**二期分块分层抽取引擎**构造一个 `extractor`。

    用法::

        deps = Deps(settings=settings, llm=client,
                    extractor=make_llm_extractor(client, settings))

    为什么做成工厂而不是让节点自己 new
    ----------------------------------
    `extract` 节点**不该知道**大模型怎么来（SAD C01：编排层不碰引擎）。
    工厂把"用哪个 llm、什么参数"收在一个地方，节点只拿到一个
    `Callable[[str], dict]`。换引擎时节点一行不用改。

    ⚠️ 只返回**图契约的三个键**（`topics` / `resolutions` / `action_items`）。
    真引擎还会产出 `minutes`（SAD §10.2 形状）与 `issues`，但
    `PipelineState` 里没有这两个字段 —— TypedDict 里未声明的键会被
    **静默丢弃**。本卡**不动状态契约**（动它要改 `INPUT_FIELDS`/
    `OUTPUT_FIELDS`，波及检查点兼容与一期已验收的编排测试）。
    所以那两项留给**直接调用 `extract_minutes` 的调用方**
    （验收脚本、将来的问题清单功能），不走图。
    """
    from mmwf.domain.extraction import DEFAULT_MAX_WORKERS, extract_minutes

    workers = DEFAULT_MAX_WORKERS if max_workers is None else max_workers

    def _extract(record_markdown: str) -> dict[str, Any]:
        res = extract_minutes(
            record_markdown or "", llm=llm, settings=settings, max_workers=workers
        )
        # 抽取期的问题（块失败、无时间码…）记进日志 —— 图状态里没有地方放它们
        for issue in res.issues:
            logger.warning("抽取提示：%s", issue.get("detail") or issue)
        return res.to_graph_dict()

    _extract.__name__ = "llm_extract"
    return _extract


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def make_generate(
    deps: Any = None,
    *,
    generator: Callable[[dict], dict[str, Any]] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `generate` 节点（议题 -> 结构化纪要）。

    Args:
        generator: 二期注入点。`{topics, resolutions, action_items} ->
            minutes_structured`。默认 `naive_generate`。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)
    impl = generator or naive_generate

    def generate(state: dict) -> dict:
        B.require_inputs(S.NODE_GENERATE, state)
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_GENERATE)

        payload = {
            "topics": list(state.get("topics") or []),
            "resolutions": list(state.get("resolutions") or []),
            "action_items": list(state.get("action_items") or []),
        }
        got = B.guard(S.NODE_GENERATE, lambda: impl(payload))

        # 每次进入 generate 都递增重试计数（validate 不通过会回来）。
        retry = int(state.get("gen_retry") or 0) + 1
        logger.info("generate 完成（第 %d 次）：%d 个章节",
                    retry, len(got.get("sections") or []))
        return {
            "minutes_structured": got,
            "gen_retry": retry,
            # generate 是 G2 的下游第一个节点，负责清路由标记
            "_gate_route": None,
        }

    generate.__name__ = S.NODE_GENERATE
    return generate


def naive_generate(payload: dict[str, Any]) -> dict[str, Any]:
    """朴素成文：把三项结构化数据组装成带章节的 dict。

    产出形状（与二期实现约定一致）::

        {
          "title": str,
          "sections": [{"heading": str, "items": [str, ...]}],
          "topics": [...],          # 原样带上，见下
          "resolutions": [...],
          "action_items": [...],
        }

    ⚠️ **必须把 `topics` / `resolutions` / `action_items` 原样带出去**。
    第一版只返回 `sections` + `action_items`，把 `topics` 丢了。后果是
    `naive_validate` 拿不到 `topics`，只能退到 `_topics_from_sections`
    从章节标题反推 —— 那条路推出来的议题**没有 `refs`**，
    于是"有议题没有来源依据"这条规则在**完全正常的记录稿**上也会命中。
    测得再准的规则，接错了输入就是误报（2026-09-27 记忆日志）。

    带上原始三项的另一个好处：G3 用 `edit` 改了措辞后，
    `validate` 仍能基于同一份结构化数据复核，不必再从 Markdown 反解。
    """
    topics = payload.get("topics") or []
    resolutions = payload.get("resolutions") or []
    actions = payload.get("action_items") or []

    sections: list[dict] = []
    if topics:
        sections.append({
            "heading": "一、会议主要内容",
            "items": [str(t.get("title") or "") for t in topics if t.get("title")],
        })
    if resolutions:
        sections.append({
            "heading": "二、会议决定事项",
            "items": [str(r.get("text") or "") for r in resolutions],
        })
    if actions:
        sections.append({
            "heading": "三、待办事项",
            "items": [
                _format_action(a) for a in actions
            ],
        })
    return {
        "title": "会议纪要",
        "sections": sections,
        # 原始三项原样透传（见 docstring）
        "topics": list(topics),
        "resolutions": list(resolutions),
        "action_items": list(actions),
    }


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def make_validate(
    deps: Any = None,
    *,
    validator: Callable[[dict], list[dict]] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `validate` 节点（自检，产出问题清单）。

    Args:
        validator: 二期注入点。`minutes_structured -> [issue, ...]`。
            默认 `naive_validate`。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)
    impl = validator or naive_validate

    def validate(state: dict) -> dict:
        B.require_inputs(S.NODE_VALIDATE, state)
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_VALIDATE)
        issues = list(B.guard(
            S.NODE_VALIDATE,
            lambda: impl(dict(state.get("minutes_structured") or {})),
        ) or [])

        if issues:
            logger.info("validate 发现 %d 个问题（第 %d 次）",
                        len(issues), int(state.get("gen_retry") or 1))
        return {"validation_issues": issues}

    validate.__name__ = S.NODE_VALIDATE
    return validate


def naive_validate(structured: dict[str, Any]) -> list[dict]:
    """朴素自检：逐条跑 `VALIDATION_RULES`，命中就产出一条问题。

    只判**结构性**问题（有没有议题、有没有依据、有没有责任人）。
    内容对不对要溯源（二期），这里不做判断 —— 假装能判内容对错
    比不判更危险（会给人"已核对"的错觉）。

    ⚠️ **对畸形输入要宽容**。上游是 LLM 或二期实现，形态不完全可控：
    `topics` 可能不是 list、元素可能不是 dict。第一版直接 `.get()`
    会抛 `AttributeError`，而 `validate` 抛异常的后果是**整个任务失败**
    —— 自检本来是为了"发现问题后照样成文"，自己先炸掉就完全反了。
    所以这里统一用 `_mappings()` 过滤，只对"确实是字典"的条目做检查。
    """
    issues: list[dict] = []
    topics = _mappings(structured.get("topics"))
    resolutions = _mappings(structured.get("resolutions"))
    actions = _mappings(structured.get("action_items"))

    # 议题可能被 generate 折叠成 sections，这里从两个来源取
    if not topics:
        topics = _topics_from_sections(structured)

    if not topics:
        issues.append(_issue("no_topics", "没有任何议题，纪要将无从展开"))
    for t in topics:
        if not str(t.get("title") or "").strip():
            issues.append(_issue("empty_topic_title", "有议题标题为空"))
        elif not t.get("refs"):
            issues.append(_issue(
                "topic_without_refs",
                f"议题「{_clip(str(t.get('title')), 20)}」没有来源依据，无法追溯",
            ))
    for r in resolutions:
        if not r.get("refs"):
            issues.append(_issue(
                "resolution_without_refs",
                f"决议「{_clip(str(r.get('text')), 20)}」没有来源依据，无法追溯",
            ))
    for a in actions:
        if not str(a.get("owner") or "").strip():
            issues.append(_issue(
                "action_without_owner",
                f"待办「{_clip(str(a.get('text')), 20)}」未指定责任人",
            ))
    return issues


def _mappings(value: Any) -> list[dict]:
    """把可能是任意东西的输入收敛成"字典列表"。

    非列表 -> 空列表；列表里的非字典元素 -> 丢掉。
    丢掉而不是报错：自检的职责是报告问题，不是拒绝数据。
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [x for x in value if isinstance(x, dict)]


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------

def make_assemble(
    deps: Any = None,
    *,
    assembler: Callable[[dict, list[dict]], str] | None = None,
    settings: Any = None,
    idem_store: idem.IdempotencyStore | None = None,
) -> Callable[[dict], dict]:
    """构造 `assemble` 节点（结构化数据 -> 成稿 Markdown）。

    Args:
        assembler: 二期注入点。`(structured, issues) -> markdown`。
            默认 `naive_assemble`。
    """
    cfg = _resolve(deps, settings=settings, idem_store=idem_store)
    impl = assembler or naive_assemble

    def assemble(state: dict) -> dict:
        B.require_inputs(S.NODE_ASSEMBLE, state)
        B.touch_task_state(cfg.db_path_of(state), B.meeting_id_of(state),
                           S.NODE_ASSEMBLE)
        structured = dict(state.get("minutes_structured") or {})
        issues = list(state.get("validation_issues") or [])
        md = str(B.guard(S.NODE_ASSEMBLE, lambda: impl(structured, issues)) or "")

        logger.info("assemble 完成：%d 字（带 %d 条待核对项）", len(md), len(issues))
        return {
            "minutes_markdown": md,
            # assemble 是 G3 的下游第一个节点，负责清路由标记
            "_gate_route": None,
        }

    assemble.__name__ = S.NODE_ASSEMBLE
    return assemble


def naive_assemble(structured: dict[str, Any], issues: Sequence[dict]) -> str:
    """朴素成文：章节拼成 Markdown，问题清单附在末尾。

    **问题清单必须进正文**（而不是只记在日志里）：
    施工卡要求"带问题清单成文，不静默丢弃"（C03 配套）。
    审定人要能在稿子上直接看到"机器觉得这里不确定"。
    """
    lines: list[str] = []
    title = str(structured.get("title") or "会议纪要")
    lines.append(f"# {title}")
    lines.append("")

    for sec in _mappings(structured.get("sections")):
        heading = str(sec.get("heading") or "")
        items = list(sec.get("items") or [])
        if not heading and not items:
            continue
        if heading:
            lines.append(f"## {heading}")
            lines.append("")
        for i, item in enumerate(items, start=1):
            lines.append(f"{i}. {item}")
        lines.append("")

    if issues:
        lines.append("## 待核对清单")
        lines.append("")
        lines.append("以下问题由系统自检发现，请核对后再定稿：")
        lines.append("")
        for i, issue in enumerate(_mappings(issues), start=1):
            detail = str(issue.get("detail") or issue.get("rule") or "")
            lines.append(f"{i}. {detail}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 闸口包装
# ---------------------------------------------------------------------------

def make_gate_nodes():
    """返回两个闸口节点（薄包装，真正的实现在 `interrupts`）。

    单独包一层的理由：施工卡的骨架里节点都从 `nodes` 取
    （`nodes.make_gate_g2(deps)`），保持一致。同时让"闸口实现在
    `interrupts`、图上的名字在 `nodes`"这件事有个明确的落点。
    """
    from mmwf.graph import interrupts as I

    return I.make_gate_g2(), I.make_gate_g3()


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _issue(rule: str, detail: str) -> dict:
    return {"rule": rule, "detail": detail}


def _topics_from_sections(structured: dict[str, Any]) -> list[dict]:
    """从 `sections` 里反推议题（generate 可能把议题折叠成章节）。

    用 `_mappings` 过滤畸形章节 —— 与 `naive_validate` 同理，
    自检不该因为上游给了个非字典而炸掉。
    """
    out: list[dict] = []
    for sec in _mappings(structured.get("sections")):
        heading = str(sec.get("heading") or "")
        if "议题" in heading or "主要内容" in heading:
            for item in sec.get("items") or []:
                out.append({"title": str(item)})
    return out


def _split_sentences(md: str) -> list[tuple[str, dict | None]]:
    """把记录稿切成 (句子, 来源片段) 列表。

    朴素实现按中文句末标点切分，并尝试从"带时间码的行"里取
    段落标识**与时间码**作为来源依据（`refs`）。取不到依据的句子
    `refs` 为空 —— 让 `validate` 能据此报"无依据"，
    而不是伪造一个假依据（那会让溯源形同虚设）。

    ⚠️ **时间码必须真的解出来**。第一版把 `start_ms` 硬编码成
    `None`，于是每条 ref 都缺时间戳。表面看"有 refs"，但溯源要的
    "这句话在第几分钟"根本给不出来，`validate` 也就无法据此判断
    依据是否可用（2026-09-27 记忆日志）。

    另外要**剥掉说话人前缀**：记录稿形如 `[00:00:30] 李四：决定……`，
    若整行原样当句子，抽出来的议题标题会带 `李四：`，
    渲染到纪要里就成了"李四：李四：……"式的重复。
    """
    out: list[tuple[str, dict | None]] = []
    seg_seq = 0
    for raw_line in md.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ref = None
        # 记录稿里带时间码的行形如：`[00:01:23] 张三：……`
        if line.startswith("["):
            seg_seq += 1
            # ⚠️ `partition("]")` 返回的 `head` **不含**右括号，
            # 而 `_TIMECODE_RE` 要求成对的中括号 —— 直接把 head 传进去
            # 永远匹配不到，`start_ms` 会静默变成 None（2026-09-27 记忆日志）。
            # 所以这里补回右括号再解析。
            head, sep, rest = line.partition("]")
            ref = {
                "segment_id": f"seg-{seg_seq}",
                "start_ms": _parse_timecode(head + sep),
                "text": line,
            }
            line = rest.strip()
        # 剥掉"说话人："前缀（只剥第一处，保留正文里的冒号）
        body = _strip_speaker(line)
        for part in re.split(r"(?<=[。！？；;])", body):
            part = part.strip()
            if part:
                out.append((part, ref))
    return out


#: `[01:02:03]` / `[02:03]` 两种时间码写法。
_TIMECODE_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")

#: 说话人前缀：行首 2-4 个汉字后跟中文/英文冒号（`张三：` / `李四:`）。
#:
#: ⚠️ 冒号前**允许有空白**（`\s*`）。记录稿经转写/人工编辑后常见
#: `王五 ： 有空` 这种带空格写法，正则要求冒号紧贴名字的话就剥不掉，
#: 说话人会留在正文里（与"剥前缀"的目的正好相反）。
_SPEAKER_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4})\s*[：:]\s*")


def _parse_timecode(head: str) -> int | None:
    """`[00:01:23]` -> 83000（毫秒）；`[02:03]` -> 123000；解析不出返回 `None`。

    返回 `None` 而不是 0：0 是个**合法时间点**（会议刚开始），
    用 0 代替"没解析出来"会让"第一分钟说的话"和"没时间码的行"
    在后端无法区分。
    """
    m = _TIMECODE_RE.search(head)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:                      # [02:03] -> 分:秒
        return (int(a) * 60 + int(b)) * 1000
    hours, minutes, seconds = int(a), int(b), int(c)   # [01:02:03]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def _strip_speaker(line: str) -> str:
    """剥掉行首的"说话人："前缀。"""
    return _SPEAKER_RE.sub("", line, count=1).strip()


def _has_any(text: str, cues: Sequence[str]) -> bool:
    return any(c in text for c in cues)


def _looks_like_action(text: str) -> bool:
    """待办句的判据：含责任人引导词，或含明确的"于某时前"。

    刻意比 `ACTION_CUES` 严一些 —— `ACTION_CUES` 里有"于"这种
    高频字，只看它会把大量陈述句误判成待办（"会议于上午九点召开"）。
    """
    if _OWNER_RE.search(text):
        return True
    return bool(re.search(r"于[^，。]{1,10}前", text))


def _extract_owner(text: str) -> str:
    """从待办句里抽责任人姓名，抽不到返回空串。

    抽不到就返回空串（而不是猜一个），这样 `validate` 能报
    "待办未指定责任人"让人补 —— 猜错一个名字比留空危险得多。
    """
    m = _OWNER_RE.search(text)
    if not m:
        return ""
    name = m.group(1) or ""
    # 剥掉可能残留的引导动词（正则贪婪时的兜底，见 `_OWNER_RE` 注释）
    for head in _OWNER_VERB_HEADS:
        idx = name.find(head)
        if idx > 0:          # idx == 0 说明整个名字就是动词，不动
            name = name[:idx]
    return name.strip()


def _format_action(a: dict) -> str:
    """把待办渲染成一行：`{责任人}负责：{事项}`。

    ⚠️ 事项正文里的**责任人前缀要剥掉**。`text` 抽的是整句
    （`请赵六负责落实设备验收工作。`），如果原样拼在"赵六负责："后面，
    出来就是"赵六负责：请赵六负责落实设备验收工作。" —— 同一件事说了两遍。
    `_strip_owner_prefix` 负责剥掉这句里的 `请赵六负责` / `由赵六牵头`
    之类引导语，只留动作本身（2026-09-27 记忆日志）。

    抽不到责任人时**不加"负责："**，直接给原文 —— 这样
    `validate` 报的"待办未指定责任人"在稿子上肉眼可见（正文没有前缀），
    而不是被一个空的前缀掩饰过去。
    """
    owner = str(a.get("owner") or "").strip()
    text = str(a.get("text") or "").strip()
    if not owner:
        return text
    body = _strip_owner_prefix(text, owner) or text
    return f"{owner}负责：{body}"


def _strip_owner_prefix(text: str, owner: str) -> str:
    """从待办句里剥掉"请{责任人}负责"这类引导语，返回动作本身。

    只在**确实剥掉了东西**时才返回结果；剥不动就返回空串，
    让调用方（`_format_action`）退回原文 —— 宁可有重复，
    也不能把整句话弄丢。
    """
    if not owner:
        return ""
    # 引导词 + 责任人 + 责任动词，可后缀"同志"，前面可带 由/请/让
    pat = re.compile(
        rf"^(?:由|请|让)?{re.escape(owner)}(?:同志)?(?:{_OWNER_VERB})?"
    )
    out = pat.sub("", text, count=1).strip()
    # 剥完后若只剩残句（太短或没变），视为没剥动
    if not out or out == text:
        return ""
    return out


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


__all__ = [
    "ACTION_CUES",
    "MAX_VALIDATE_RETRY",
    "RESOLUTION_CUES",
    "VALIDATION_RULES",
    "make_assemble",
    "make_extract",
    "make_gate_nodes",
    "make_generate",
    "make_llm_extractor",
    "make_validate",
    "naive_assemble",
    "naive_extract",
    "naive_generate",
    "naive_validate",
]
