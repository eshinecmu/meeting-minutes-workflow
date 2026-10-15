"""审计日志（T1-04）。

目标（施工卡原文）：**所有关键操作可留痕、可检索、可导出、不可篡改。**

四个词对应四种机制，缺一不可
----------------------------
| 目标 | 机制 | 落地位置 |
|:---|:---|:---|
| 可留痕 | `AuditLogger.log()` 唯一写入口 | 本模块 |
| 可检索 | `query()` / `summarize()` | 本模块 |
| 可导出 | `build_report()` 生成含完整性结论的报告 | 本模块 + `tools/verify_audit_chain.py` |
| 不可篡改 | ① 触发器拦 UPDATE/DELETE ② 链式哈希 | `001_init.sql` + 本模块 |

不可篡改是**双保险**，两层各自成立、互不依赖
--------------------------------------------
第一层：**SQL 层的触发器**。`audit_log_no_update` / `audit_log_no_delete`
（001_init.sql 末尾）让任何 `UPDATE`/`DELETE` 直接 `RAISE(ABORT)`。
它挡的是"用程序或 sqlite3 命令行顺手改一下"这类最常见的篡改。

第二层：**链式哈希**。`row_hash = sha256(prev|ts|operator|action|tt|tid|detail|result)`。
它挡的是"绕过 SQLite 直接改 .db 文件字节"——**触发器对这种情况完全无效**，
因为文件被改后重新打开时，SQLite 根本不认为发生过 UPDATE。
这时唯一能发现问题的就是"每一行的哈希都依赖上一行的哈希"这个结构：
改任意一行 → 该行 row_hash 对不上 → 从该行起后续所有行的 prev_hash 也对不上。

所以：**只做触发器 = 假安全，只做哈希链 = 日常操作能顺手改**。两个都要。

哈希公式里的分隔符
------------------
用 `|` 分隔各字段，不是"拼接"（SAD §8.3 的散文描述写成相加，
施工卡给出的 `_compute_hash` 才是实现权威）。
为什么必须分隔：拼接会让
`("ab","c")` 和 `("a","bc")` 生成同一个哈希；分隔符消除了这种歧义。
字段值本身若含 `|` 也无需转义——因为比较的是"整串哈希"，
攻击者要构造碰撞仍然要求 SHA-256 碰撞，`|` 只负责让不同字段组合不撞车。

并发
----
`prev_hash` 必须取"上一条的 row_hash"。若两条日志同时读到最后一行，
就会拿到同一个 prev_hash，生成两条并行的链 —— 整条链从此断裂。
单机单任务场景下用一把 `threading.Lock` 覆盖"读末尾 + 插入"即可（施工卡要求）。
跨进程（Electron 壳 + 服务）另有 `BEGIN IMMEDIATE` 兜底：它会立刻抢写锁，
让第二个进程排队而不是读到脏的 prev_hash。

`detail` 里绝不能放的内容
-------------------------
**转写稿原文、纪要正文、人名清册**。原因见部署手册 §5.3：
审计日志要**导出提交保密检查**——把正文放进去，等于把涉密内容的扩散面
从"受加密保护的数据目录"扩大到"审计报告、检查记录、备份介质"。
需要引用时只记 `segment_id` / `topic_id` 这类标识符。

本模块提供 `check_detail_safety()` 做主动拦截：写之前扫一遍详情里
有没有明显像"长段正文"的字段，命中就抛错。这不是万能的（它挡不住
精心构造的短句），但能挡住"顺手把整段 text 塞进 detail"这类高频错误。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from mmwf.infra.db import connect_ctx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 已登记的审计动作（施工卡原文，一字不改）
#:
#: 这份清单与部署手册 §6.2「审计最小核查项集」的 8 项一一对应：
#:   1 登录      -> login / logout
#:   2 音频导入  -> import_audio / import_text
#:   3 转写      -> transcribe_start / transcribe_done
#:   4 文稿修改  -> segment_edit
#:   5 闸口审批  -> gate_decision
#:   6 导出      -> export / export_undo
#:   7 配置变更  -> glossary_change / template_change
#:   8 自检      -> selfcheck
#:
#: 单位保密机构若要增项，**必须在这里加条目并重新跑验收**，
#: 不允许在调用处直接传一个没登记的动作字符串——那样审计就失去可枚举性了。
#:
#: `record_generate`（T1-08 新增）
#: ----------------------------
#: 记录稿生成是 SAD §5.1 状态机里 `reviewing -> record_ready` 这一步的产物。
#: 它不给最小核查项集新增类别（记录稿属于"文稿"，与 `segment_edit` 同族），
#: 但**必须单独留痕**：记录稿是后续所有抽取与生成的输入，
#: 若它被人改动过，追溯出的问题会指向错误的方向。
#: 归到"文稿修改记录"这一类下，`MINIMUM_CHECK_ITEM_ACTIONS` 因此把
#: 它一并列入 `文稿修改记录`。
ACTIONS: frozenset[str] = frozenset({
    "login", "logout", "import_audio", "import_text",
    "transcribe_start", "transcribe_done",
    "segment_edit", "record_generate",
    "gate_decision", "export", "export_undo",
    #: ★ T1-16 新增：**下载**导出文件。
    #: 为什么"生成"之外还要单独记"下载"：`export` 只说明"有一份文件被生成"，
    #: 而纪要正文是敏感内容 —— 部署手册 §6.2 的「导出记录」关心的是
    #: **"谁在什么时候把纪要取走了"**。只记生成不记取走，
    #: 审计里就只能看到"文件存在过"，看不到"文件被谁拿走了"。
    #: 归到「导出记录」同一类下（同属导出的行为谱系）。
    #:
    #: ⚠️ **改这个集合必须同步改两处**：
    #:   ① `tests/unit/test_audit.py::test_action_whitelist_exactly_matches_construction_card`
    #:      的 `sanctioned_extensions`（并写明理由）—— 那条测试是专门
    #:      防"悄悄加动作没登记"的**回归锁**，本卡实测它会立刻红并指名报出。
    #:   ② `MINIMUM_CHECK_ITEM_ACTIONS`（若新动作属于某个核查项）。
    "export_download",
    "glossary_change", "template_change", "selfcheck",
})

#: 部署手册 §6.2 的 8 类最小核查项 -> 覆盖它的动作集合。
#: 供 `audit_minimum_coverage()` 判定"核查项齐不齐"。
MINIMUM_CHECK_ITEM_ACTIONS: dict[str, frozenset[str]] = {
    "登录记录": frozenset({"login", "logout"}),
    "音频导入记录": frozenset({"import_audio", "import_text"}),
    "转写记录": frozenset({"transcribe_start", "transcribe_done"}),
    "文稿修改记录": frozenset({"segment_edit", "record_generate"}),
    "闸口审批记录": frozenset({"gate_decision"}),
    "导出记录": frozenset({"export", "export_undo", "export_download"}),
    "术语库/模板变更": frozenset({"glossary_change", "template_change"}),
    "自检记录": frozenset({"selfcheck"}),
}

RESULTS: frozenset[str] = frozenset({"success", "failure"})

#: detail 中禁止出现的键名。命中即拒绝——这些键天然会装正文。
#:
#: 维护原则：只放"**作为键名时必然装正文**"的词。
#: 不要放 `count` / `segments` / `chars` / `len` 这类既可能装数量、
#: 也可能装内容的词——把 `segments=428`（正文条数）也拦掉会误伤正常审计，
#: 反而逼着调用方去伪造键名，削弱审计可信度。
#: 内容侧的防线由「字符串长度上限」+「禁止容器类值」承担。
FORBIDDEN_DETAIL_KEYS: frozenset[str] = frozenset({
    "text", "raw_text", "original_text", "content", "body", "full_text",
    "transcript", "transcript_text", "paragraph", "minutes",
    "minutes_text", "plain", "plain_text", "原文", "正文", "全文",
    "转写稿", "纪要正文",
})

#: detail 中禁止出现的**容器类**键名：值为 list/dict 即拒绝。
#:
#: 为什么单列一组：这些键名在"装标识符"时是合法的（`files=["a.docx"]`），
#: 但一旦值是嵌套结构，就极可能是把分段列表／议题树整体搬进来了。
#: 用"键名 + 值类型"双重条件，比单纯封禁键名精确得多。
#:
#: `files` / `changed` / `changed_keys` / `fields` 等受控白名单不在其中——
#: 它们是本模块自己的领域封装写入的，结构由代码保证。
FORBIDDEN_CONTAINER_KEYS: frozenset[str] = frozenset({
    "segments", "segment_list", "paragraphs", "topics", "resolutions",
    "action_items", "lines", "sentences", "chunks", "记录", "分段",
})

#: detail 中单个字符串值的长度上限。超过就不再是"标识符"而是"正文摘要"。
#: 256 的依据：UUID 36 字符、相对路径通常 <100、错误信息通常 <200，
#: 留一倍余量。真需要放长文本，说明审计设计错了——该改用 filestore 存产物。
MAX_DETAIL_STRING_LEN = 256

#: 递归扫描 detail 时允许的最大结点数，防止构造超深嵌套拖垮审计写入。
MAX_DETAIL_NODES = 500

#: 链式哈希的初值：第一行的 prev_hash 用全 0（表示"链开始"）。
GENESIS_HASH = "0" * 64

_CHECK_ITEM = Literal["ok", "empty", "missing"]


class AuditError(RuntimeError):
    """审计日志使用错误（动作未登记、detail 不合规等）。"""


class AuditChainError(AuditError):
    """链式哈希断裂——**这是篡改信号，不是普通错误**。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录（与 audit_log 表一行对应）。"""

    log_id: int
    ts: str
    operator: str
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, Any]     # 解析自 detail 列的 JSON
    detail_raw: str
    result: str
    prev_hash: str | None
    row_hash: str

    @property
    def detail_text(self) -> str:
        return self.detail_raw


@dataclass(frozen=True)
class ChainProblem:
    """链式校验发现的一处问题。"""

    log_id: int
    kind: str          # row_hash_mismatch / prev_hash_mismatch / link_broken
    expected: str
    actual: str
    explanation: str

    def __str__(self) -> str:
        return (
            f"log_id={self.log_id} [{self.kind}] {self.explanation}\n"
            f"        期望 {self.expected[:20]}…  实际 {self.actual[:20]}…"
        )


@dataclass
class ChainVerification:
    """链式校验结果。"""

    db_path: Path
    total_rows: int = 0
    checked_rows: int = 0
    problems: list[ChainProblem] = field(default_factory=list)
    head_hash: str | None = None
    first_bad_log_id: int | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def tampered(self) -> bool:
        return bool(self.problems)

    def summary(self) -> str:
        if self.ok:
            return (
                f"链式哈希完好：{self.checked_rows}/{self.total_rows} 行通过，"
                f"链头 {self.head_hash[:16] if self.head_hash else '(空)'}…"
            )
        return (
            f"链式哈希破损：{len(self.problems)} 处问题，"
            f"首个出错行 log_id={self.first_bad_log_id}"
        )


@dataclass(frozen=True)
class AuditQuery:
    """检索条件（全部可选，AND 组合）。"""

    from_ts: str | None = None      # ISO8601，闭区间
    to_ts: str | None = None        # ISO8601，闭区间
    operator: str | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    result: str | None = None
    limit: int | None = None
    offset: int = 0


# ---------------------------------------------------------------------------
# 哈希计算与校验（纯函数，便于单测）
# ---------------------------------------------------------------------------

def compute_row_hash(
    prev_hash: str | None,
    ts: str,
    operator: str,
    action: str,
    target_type: str | None,
    target_id: str | None,
    detail_json: str,
    result: str,
) -> str:
    """链式哈希：`sha256(prev|ts|operator|action|tt|tid|detail|result)`。

    None 统一渲染为空串（而不是 Python 的 "None"）——否则 'None' 这个
    **字符串字面量**与实际 None 会算出同一个哈希，审计语义上不干净。
    """
    raw = "|".join([
        prev_hash or GENESIS_HASH,
        ts,
        operator,
        action,
        target_type or "",
        target_id or "",
        detail_json,
        result,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def detail_to_json(detail: dict[str, Any] | None) -> str:
    """detail 的规范序列化。

    `sort_keys=True` 有两个作用：一是同一份 detail 无论字典插入顺序如何
    都得到同一个哈希（否则重算会误报篡改）；二是导出报告时行与行之间可比。
    `ensure_ascii=False` 保留中文——审计报告要给人看。
    `separators` 去掉多余空格，让哈希计算不受格式化影响。
    """
    return json.dumps(
        detail or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def detail_from_json(raw: str | None) -> dict[str, Any]:
    """反序列化 detail。损坏时返回带 `_corrupt` 标记的字典而非抛错——
    审计检索不应该因为一行坏数据就整个不可用。"""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("audit_log.detail 不是合法 JSON，原样返回：%r", raw[:80])
        return {"_corrupt": raw}
    return obj if isinstance(obj, dict) else {"_raw": obj}


def check_detail_safety(detail: dict[str, Any] | None) -> None:
    """写入前拦截 detail 中的违规内容（施工卡「常见坑」第二条）。

    三条规则，逐条对应一类真实失误：

      1. **禁止的键名**（`text` / `原文` / `transcript` …）——它们天然装正文。
      2. **容器类键名装了 list/dict**（`segments=[{...}, {...}]`）——
         把分段列表整体搬进来。键名本身合法（`segments=428` 是正常的），
         所以判定条件是"键名 + 值类型"，不是光看键名。
      3. **任何字符串值超过 256 字符**——那就不是标识符而是正文片段。

    为什么做成"写前抛错"而不是"写后告警"：审计一旦落盘就**删不掉**
    （触发器在那儿摆着），放进去就永久在里面了。必须在写之前拦。

    为什么"拦不住"是可以接受的：本函数挡的是**顺手把整段 text 塞进来**
    这类高频错误，不是挡一个刻意规避的对手。真正的合规保证来自
    「审计日志可被保密机构调阅 + 调用方代码评审 + 部署手册 §5.3 明令」，
    本函数只是把最后一道自动化的门装上。
    """
    violations: list[str] = []
    nodes = 0

    def scan_str(value: str, path: str) -> None:
        if len(value) > MAX_DETAIL_STRING_LEN:
            violations.append(
                f"{path} 字符串长度 {len(value)} > {MAX_DETAIL_STRING_LEN}，"
                "疑似正文（审计只记 id/路径/短说明）"
            )

    def walk(node: Any, path: str) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_DETAIL_NODES:
            raise AuditError(
                f"detail 结构过大（>{MAX_DETAIL_NODES} 个结点），"
                "审计详情只应放标识符，不应放数据体"
            )
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                key_l = key.lower()
                child = f"{path}.{key}"
                if key_l in FORBIDDEN_DETAIL_KEYS:
                    violations.append(f"{child} 是禁止的键名（会装正文）")
                elif key_l in FORBIDDEN_CONTAINER_KEYS and isinstance(v, (list, dict)):
                    violations.append(
                        f"{child} 是容器类键名且值为 "
                        f"{type(v).__name__}，疑似把正文列表整体写入"
                        "（只记数量/标识符）"
                    )
                walk(v, child)
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            scan_str(node, path)

    walk(detail or {}, "detail")
    if violations:
        raise AuditError(
            "detail 含疑似正文内容，已拒绝写入审计日志（部署手册 §5.3）：\n  - "
            + "\n  - ".join(violations)
            + "\n如需记录正文，请写入 filestore 并在 detail 中只留其路径或 id。"
        )


def verify_chain_rows(rows: Sequence[sqlite3.Row | dict[str, Any]]) -> list[ChainProblem]:
    """校验一串按 log_id 升序的记录，返回问题列表（空 = 完好）。

    两两比对相邻行（不是"拿本行哈希去续链"——那是个会掩盖篡改的写法）：
      a. `row_hash` 是否等于按**本行字段**重算的结果     -> 本行内容有没有被改
      b. 本行 `prev_hash` 是否等于**上一行记录的 row_hash** -> 行之间的链有没有断

    为什么 b 必须比对"上一行**存储的** row_hash"，而不是用"我自己重算的上一行哈希"
    ----------------------------------------------------------------
    如果校验时把重算值当作链的延续，那么：
        第 3 行被篡改 -> 第 3 行报 hash 不匹配 -> 但校验器接着用重算出的
        "正确的第 3 行哈希"去比对第 4 行，于是第 4 行的 prev_hash 反倒对不上，
        报出一串"第 4/5/6 行都有问题"的**假警报**。
    改为比对存储值时，结论干净且准确：**只有被改的那一行报错**，
    后面的行无辜（它们的 prev_hash 指向的确实是当时那一行）。

    这也正是链式哈希的实际效果——篡改无法"隐身"，但也不会误伤。
    """
    problems: list[ChainProblem] = []
    prev_stored_hash: str | None = None

    for r in rows:
        row = dict(r)
        actual_hash = row["row_hash"]
        recomputed = compute_row_hash(
            row.get("prev_hash"),
            row["ts"],
            row["operator"],
            row["action"],
            row.get("target_type"),
            row.get("target_id"),
            row.get("detail") or "",
            row["result"],
        )
        if recomputed != actual_hash:
            problems.append(ChainProblem(
                log_id=int(row["log_id"]),
                kind="row_hash_mismatch",
                expected=recomputed,
                actual=actual_hash,
                explanation="本行内容与 row_hash 不匹配（该行字段被人改过）",
            ))

        if prev_stored_hash is not None and (row.get("prev_hash") or GENESIS_HASH) != prev_stored_hash:
            problems.append(ChainProblem(
                log_id=int(row["log_id"]),
                kind="prev_hash_mismatch",
                expected=prev_stored_hash,
                actual=row.get("prev_hash") or GENESIS_HASH,
                explanation=(
                    "本行记录的 prev_hash 与上一行实际存储的 row_hash 不符"
                    "（中间有行被替换、删除或插入）"
                ),
            ))

        prev_stored_hash = actual_hash

    return problems


# ---------------------------------------------------------------------------
# 写入口
# ---------------------------------------------------------------------------

class AuditLogger:
    """审计日志写入口。**只提供 INSERT**，没有任何 update/delete 方法。

    刻意不提供删除接口的原因：对外"能删"就等于对内"迟早被删"。
    保留期清理（1095 天）如果是真需求，应当由**独立的归档流程**处理，
    而不是给日常代码递一把橡皮擦。

    典型用法::

        audit = AuditLogger(db_path)
        audit.log("zhangsan", "login", detail={"client": "desktop"})
        audit.log("zhangsan", "gate_decision", "meeting", mid,
                  detail={"decision": "approve", "from": "G2", "to": "extracting"})
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._ensure_table()

    # -- 内部 ----------------------------------------------------------

    def _ensure_table(self) -> None:
        """确认 audit_log 表存在。

        不自动建表（那是迁移器 T1-02 的职责），但给出**明确可执行的错误**——
        直接抛 sqlite3.OperationalError: no such table 对使用者毫无帮助。
        """
        with connect_ctx(self.db_path, readonly=self.db_path.exists()) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
            ).fetchone()
        if row is None:
            raise AuditError(
                f"审计表不存在：{self.db_path}\n"
                "请先执行：python tools/migrate.py"
            )

    @contextmanager
    def _write_conn(self) -> Iterator[sqlite3.Connection]:
        """写连接。`BEGIN IMMEDIATE` 立刻抢写锁，避免两个进程读到同一 prev_hash。"""
        with connect_ctx(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # -- 读末尾 --------------------------------------------------------

    def last_row(self) -> AuditRecord | None:
        """最后一条记录（链头）。空表返回 None。"""
        with connect_ctx(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM audit_log ORDER BY log_id DESC LIMIT 1"
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def last_hash(self) -> str:
        """链头 row_hash。空表返回 GENESIS_HASH。

        注意不要写成 `SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1`
        然后判空——用 `COUNT(*)` 或 `MAX(log_id)` 也一样慢，
        直接取一行即可，AUTOINCREMENT 保证最大 log_id 就是最新。
        """
        rec = self.last_row()
        return rec.row_hash if rec is not None else GENESIS_HASH

    def count(self) -> int:
        with connect_ctx(self.db_path, readonly=True) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])

    # -- 主入口 --------------------------------------------------------

    def log(
        self,
        operator: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
        result: str = "success",
        *,
        ts: str | None = None,
        _conn: sqlite3.Connection | None = None,
    ) -> AuditRecord:
        """写一条审计记录，返回落盘后的记录（含 log_id 与 row_hash）。

        参数
        ----
        ts : 指定时间戳（测试用）。生产调用不要传——时间戳必须由
             **唯一的一处**产生，否则"操作时间"就不可信了。

        为什么返回记录而不是 None：调用方（尤其是闸口）经常需要
        把 log_id 带回去给前端做"我这条审批已留痕"的凭据。
        """
        if action not in ACTIONS:
            raise AuditError(
                f"未登记的审计动作：{action!r}\n"
                f"已登记：{sorted(ACTIONS)}\n"
                "新增动作必须先在 infra/audit.py 的 ACTIONS 中登记"
                "（部署手册 §6.2：增项需重新验收）。"
            )
        if result not in RESULTS:
            raise AuditError(f"result 只能是 {sorted(RESULTS)}，当前 {result!r}")
        if not operator:
            raise AuditError("operator 不能为空——审计的核心就是'谁做的'")

        check_detail_safety(detail)

        stamp = ts or _now_iso()
        detail_json = detail_to_json(detail)

        # 锁只覆盖"读末尾 + 插入"，不覆盖校验和序列化（那些是纯计算）
        if _conn is not None and not _conn.in_transaction:
            raise AuditError("外部审计连接必须已开启写事务")
        with self._lock, (nullcontext(_conn) if _conn is not None else self._write_conn()) as conn:
            row = conn.execute(
                "SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row is not None else GENESIS_HASH
            row_hash = compute_row_hash(
                prev_hash, stamp, operator, action,
                target_type, target_id, detail_json, result,
            )
            cur = conn.execute(
                "INSERT INTO audit_log"
                " (ts, operator, action, target_type, target_id,"
                "  detail, result, prev_hash, row_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (stamp, operator, action, target_type, target_id,
                 detail_json, result, prev_hash, row_hash),
            )
            log_id = int(cur.lastrowid or 0)

        return AuditRecord(
            log_id=log_id,
            ts=stamp,
            operator=operator,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=dict(detail or {}),
            detail_raw=detail_json,
            result=result,
            prev_hash=prev_hash,
            row_hash=row_hash,
        )

    def log_many(self, entries: Iterable[dict[str, Any]]) -> list[AuditRecord]:
        """批量写入（**同一条链、同一个事务**）。

        为什么需要：闸口一次决策可能同时触发"记录决策 + 推进状态"两条，
        分两次 log() 会拿两次锁、开两个事务；批量写法只锁一次。
        更重要的是：**要么都有、要么都没有**，不会出现"决策记了、状态没记"。
        """
        out: list[AuditRecord] = []
        items = list(entries)
        if not items:
            return out

        for e in items:  # 先全部校验，避免写到一半才炸
            if e.get("action") not in ACTIONS:
                raise AuditError(f"未登记的审计动作：{e.get('action')!r}")
            if e.get("result", "success") not in RESULTS:
                raise AuditError(f"result 非法：{e.get('result')!r}")
            if not e.get("operator"):
                raise AuditError("operator 不能为空")
            check_detail_safety(e.get("detail"))

        with self._lock, self._write_conn() as conn:
            row = conn.execute(
                "SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row is not None else GENESIS_HASH

            for e in items:
                stamp = e.get("ts") or _now_iso()
                detail_json = detail_to_json(e.get("detail"))
                row_hash = compute_row_hash(
                    prev_hash, stamp, e["operator"], e["action"],
                    e.get("target_type"), e.get("target_id"), detail_json,
                    e.get("result", "success"),
                )
                cur = conn.execute(
                    "INSERT INTO audit_log"
                    " (ts, operator, action, target_type, target_id,"
                    "  detail, result, prev_hash, row_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (stamp, e["operator"], e["action"], e.get("target_type"),
                     e.get("target_id"), detail_json, e.get("result", "success"),
                     prev_hash, row_hash),
                )
                out.append(AuditRecord(
                    log_id=int(cur.lastrowid or 0),
                    ts=stamp,
                    operator=e["operator"],
                    action=e["action"],
                    target_type=e.get("target_type"),
                    target_id=e.get("target_id"),
                    detail=dict(e.get("detail") or {}),
                    detail_raw=detail_json,
                    result=e.get("result", "success"),
                    prev_hash=prev_hash,
                    row_hash=row_hash,
                ))
                prev_hash = row_hash  # 链内传递

        return out

    # -- 领域语义封装（让调用点不必记忆 detail 的字段名）--------------

    def gate_decision(
        self,
        operator: str,
        meeting_id: str,
        decision: str,
        *,
        gate: str | None = None,
        from_node: str | None = None,
        to_node: str | None = None,
        changed: dict[str, Any] | None = None,
        comment: str | None = None,
        auto: bool = False,
        result: str = "success",
    ) -> AuditRecord:
        """闸口决策留痕（F4.5：detail 中可看出改了什么）。

        `changed` 只放**字段级差异**，不放改动后的正文。例如::

            changed={"title": {"old": "关于…的通知（草），合", "new": "关于…的通知"}}

        注意即便这样也要小心：如果某段正文本身很短（< 256 字符），
        `check_detail_safety` 拦不住它。**闸口实现方有责任只传摘要**，
        本方法在文档里说清楚，并用 `changed_keys` 一并记录"改了哪几个字段"，
        让导出报告即使不展开正文也能说明问题。
        """
        detail: dict[str, Any] = {
            "decision": decision,
            "meeting_id": meeting_id,
        }
        if gate:
            detail["gate"] = gate
        if from_node:
            detail["from_node"] = from_node
        if to_node:
            detail["to_node"] = to_node
        if changed:
            detail["changed_keys"] = sorted(changed.keys())
            detail["changed"] = changed
        if comment:
            detail["comment"] = comment
        if auto:
            detail["auto"] = True
        return self.log(
            operator=operator,
            action="gate_decision",
            target_type="meeting",
            target_id=meeting_id,
            detail=detail,
            result=result,
        )

    def segment_edit(
        self,
        operator: str,
        meeting_id: str,
        segment_id: str,
        *,
        fields: Sequence[str] | None = None,
        before_len: int | None = None,
        after_len: int | None = None,
        reason: str | None = None,
        result: str = "success",
    ) -> AuditRecord:
        """转写稿段落修改留痕。

        **只记 segment_id 与长度，不记文本**（施工卡「常见坑」第二条）。
        长度变化已经足够说明"改动量"，而正文留在加密的 transcript/ 里。
        """
        detail: dict[str, Any] = {"segment_id": segment_id}
        if fields:
            detail["fields"] = list(fields)
        if before_len is not None:
            detail["before_len"] = before_len
        if after_len is not None:
            detail["after_len"] = after_len
        if reason:
            detail["reason"] = reason
        return self.log(
            operator=operator,
            action="segment_edit",
            target_type="segment",
            target_id=segment_id,
            detail={**detail, "meeting_id": meeting_id},
            result=result,
        )

    def record_generate(
        self,
        operator: str,
        meeting_id: str,
        *,
        version: int | None = None,
        rel_path: str | None = None,
        segment_count: int | None = None,
        review_count: int | None = None,
        review_ratio: float | None = None,
        metadata_missing: Sequence[str] | None = None,
        result: str = "success",
    ) -> AuditRecord:
        """记录稿生成留痕（T1-08，状态机 `reviewing -> record_ready`）。

        **只记统计与路径，绝不记正文**（部署手册 §5.3 红线）。
        这也正是本方法存在的价值：`record_ready` 之后若发现纪要内容有问题，
        审计能立刻回答"当时这份记录稿是基于几段、多少段待核对生成的" ——
        若 `review_count` 异乎寻常地高（比如几十段都没核过），
        问题根源就锁定了，不用去猜。

        Args:
            version: 记录稿版本号（与 manifest.json / 文件名一致）。
            rel_path: 相对 store 的路径（如 `record/v1.md`），只放路径不放内容。
            segment_count / review_count / review_ratio: 分流统计。
            metadata_missing: 缺项**标签**（如 `["会议地点"]`），不是值。
        """
        detail: dict[str, Any] = {"meeting_id": meeting_id}
        if version is not None:
            detail["version"] = version
        if rel_path:
            detail["path"] = rel_path
        if segment_count is not None:
            detail["segments"] = segment_count
        if review_count is not None:
            detail["review_segments"] = review_count
        if review_ratio is not None:
            detail["review_ratio"] = round(float(review_ratio), 4)
        if metadata_missing:
            detail["metadata_missing"] = list(metadata_missing)
        return self.log(
            operator=operator,
            action="record_generate",
            target_type="meeting",
            target_id=meeting_id,
            detail=detail,
            result=result,
        )

    def export_event(
        self,
        operator: str,
        meeting_id: str,
        *,
        files: Sequence[str] | None = None,
        classification: str | None = None,
        undo: bool = False,
        detail: dict[str, Any] | None = None,
        result: str = "success",
    ) -> AuditRecord:
        """导出/撤销导出留痕。`files` 只放**相对路径**，不放文件内容。"""
        d: dict[str, Any] = {"meeting_id": meeting_id}
        if files:
            d["files"] = list(files)
        if classification:
            d["classification"] = classification
        if detail:
            d.update(detail)
        return self.log(
            operator=operator,
            action="export_undo" if undo else "export",
            target_type="meeting",
            target_id=meeting_id,
            detail=d,
            result=result,
        )


# ---------------------------------------------------------------------------
# 检索与导出
# ---------------------------------------------------------------------------

def query(db_path: Path | str, q: AuditQuery | None = None) -> list[AuditRecord]:
    """按条件检索（AUD-07：按时间/人员/类型检索）。

    时间比较用字符串比较即可——本项目所有 `ts` 都是
    UTC ISO8601 `YYYY-MM-DDTHH:MM:SSZ`，**定长字典序 == 时间序**。
    这也是选这个格式的原因之一（file: 不用为时间建函数索引）。
    """
    q = q or AuditQuery()
    where: list[str] = []
    params: list[Any] = []

    if q.from_ts:
        where.append("ts >= ?")
        params.append(q.from_ts)
    if q.to_ts:
        where.append("ts <= ?")
        params.append(q.to_ts)
    if q.operator:
        where.append("operator = ?")
        params.append(q.operator)
    if q.action:
        where.append("action = ?")
        params.append(q.action)
    if q.target_type:
        where.append("target_type = ?")
        params.append(q.target_type)
    if q.target_id:
        where.append("target_id = ?")
        params.append(q.target_id)
    if q.result:
        where.append("result = ?")
        params.append(q.result)

    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY log_id ASC"
    if q.limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(q.limit), int(q.offset)])
    elif q.offset:
        sql += " LIMIT -1 OFFSET ?"
        params.append(int(q.offset))

    with connect_ctx(db_path, readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(r) for r in rows]


def all_records(db_path: Path | str) -> list[AuditRecord]:
    return query(db_path, AuditQuery())


def audit_minimum_coverage(db_path: Path | str) -> dict[str, str]:
    """检查部署手册 §6.2 的 8 类最小核查项覆盖情况（AUD-08）。

    返回值三种状态：
      `ok`      —— 已有记录
      `empty`   —— 动作已登记但库里还没有（功能可用、只是没用到）
      `missing` —— 动作连 ACTIONS 都没登记（**真正的配置缺失**）

    区分 empty 与 missing 很重要：验收时"还没用过"和"根本没做"是两件事，
    混在一起报会让人误以为系统有缺口。
    """
    present_actions = {
        r.action for r in query(db_path, AuditQuery())
    }
    out: dict[str, str] = {}
    for item, actions in MINIMUM_CHECK_ITEM_ACTIONS.items():
        registered = actions & ACTIONS
        if not registered:
            out[item] = "missing"
        elif registered & present_actions:
            out[item] = "ok"
        else:
            out[item] = "empty"
    return out


def verify_chain(db_path: Path | str) -> ChainVerification:
    """全库链式校验（`tools/verify_audit_chain.py` 的核心）。

    **只读打开**（`mode=ro`）：校验程序本身绝不能有写权限——
    否则"校验脚本改坏了审计库"会成为新的风险面。
    """
    db_path = Path(db_path)
    result = ChainVerification(db_path=db_path)
    if not db_path.exists():
        result.problems.append(ChainProblem(
            log_id=0, kind="db_missing", expected="(存在)", actual="(不存在)",
            explanation=f"库文件不存在：{db_path}",
        ))
        return result

    with connect_ctx(db_path, readonly=True) as conn:
        result.total_rows = int(
            conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        )
        rows = conn.execute("SELECT * FROM audit_log ORDER BY log_id ASC").fetchall()

    result.checked_rows = len(rows)
    problems = verify_chain_rows(rows)
    result.problems = problems
    if problems:
        result.first_bad_log_id = min(p.log_id for p in problems)
    if rows:
        result.head_hash = dict(rows[-1])["row_hash"]
    return result


def build_report(
    db_path: Path | str,
    *,
    from_ts: str | None = None,
    to_ts: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """生成审计报告（AUD-06，对应 `POST /audit/export`）。

    报告结构（部署手册 §6.3 要求"含完整性校验结论"）：

        {
          "generated_at": ...,
          "period": {"from": ..., "to": ...},
          "integrity": {"result": "pass"|"fail", "checked_rows": N, ...},
          "summary": {"total": N, "operators": {...}, "actions": {...}},
          "minimum_check_items": {...},
          "records": [ ...逐条明细... ]
        }

    **`integrity.result == "fail"` 时必须立即停止使用并上报**（部署手册
    §6.3 与 §7.7）——报告里给出 `action_required` 字段把这句话写进数据里，
    避免报告被转发几手之后没人知道该干什么。
    """
    q = AuditQuery(from_ts=from_ts, to_ts=to_ts)
    records = query(db_path, q)
    chain = verify_chain(db_path)

    operators: dict[str, int] = {}
    actions: dict[str, int] = {}
    results: dict[str, int] = {}
    for r in records:
        operators[r.operator] = operators.get(r.operator, 0) + 1
        actions[r.action] = actions.get(r.action, 0) + 1
        results[r.result] = results.get(r.result, 0) + 1

    report: dict[str, Any] = {
        "generated_at": generated_at or _now_iso(),
        "period": {"from": from_ts, "to": to_ts},
        "integrity": {
            "result": "pass" if chain.ok else "fail",
            "checked_rows": chain.checked_rows,
            "total_rows": chain.total_rows,
            "head_hash": chain.head_hash,
            "problems": [
                {
                    "log_id": p.log_id,
                    "kind": p.kind,
                    "explanation": p.explanation,
                    "expected": p.expected,
                    "actual": p.actual,
                }
                for p in chain.problems
            ],
        },
        "summary": {
            "total": len(records),
            "operators": operators,
            "actions": actions,
            "results": results,
        },
        "minimum_check_items": audit_minimum_coverage(db_path),
        "records": [
            {
                "log_id": r.log_id,
                "ts": r.ts,
                "operator": r.operator,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail,
                "result": r.result,
                "prev_hash": r.prev_hash,
                "row_hash": r.row_hash,
            }
            for r in records
        ],
    }

    if not chain.ok:
        report["action_required"] = (
            "完整性校验未通过：立即停止使用本系统、封存设备，"
            "上报保密机构，不得自行修复（部署手册 §7.7）。"
        )
    return report


def prune_expired(
    db_path: Path | str,
    retention_days: int,
    *,
    dry_run: bool = True,
) -> list[int]:
    """保留期清理（默认 3 年 = 1095 天，部署手册 §5.3）。

    ⚠️ **必须 `dry_run=False` 且触发器被临时移除才能真正删除**——
    这正是设计意图。审计日志的删除本就不是常规操作，应当是：
      1. 先导出归档（`build_report` / 备份）；
      2. 人工确认；
      3. 显式带 `--apply` 执行；
      4. 即使要真删，也**必须同时重建后续所有行的 prev_hash 链**，
         否则链必然断裂、所有验签全部失败。

    本函数因此**只做 dry-run 列出待清理行**，真删留给独立的归档流程
    （T1-13 合规自检一并考虑）。给出这个函数是为了让"保留期"这件事
    在系统里有落脚点，而不是只写在配置文件里没人管。

    返回待清理的 log_id 列表。
    """
    if retention_days <= 0:
        raise AuditError(f"retention_days 必须 > 0，当前 {retention_days}")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=retention_days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with connect_ctx(db_path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT log_id FROM audit_log WHERE ts < ? ORDER BY log_id ASC",
            (cutoff,),
        ).fetchall()
    expired = [int(r[0]) for r in rows]

    if expired and dry_run:
        logger.warning(
            "保留期 %d 天：%d 条记录（log_id %s…）已到期，"
            "但**未删除**——审计日志的删除需走独立归档流程（见函数文档）",
            retention_days, len(expired), expired[0],
        )
    return expired


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """当前时间 ISO8601（UTC，秒级，带 Z）。

    刻意与 filestore 用**同一个格式**（`replace(microsecond=0)` + `Z`）：
    定长字典序 == 时间序，检索时可以直接字符串比较（见 query 的说明）。
    微秒级会破坏定长性——`...:01Z` 与 `...:01.5Z` 排序会出错。
    """
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _row_to_record(row: sqlite3.Row) -> AuditRecord:
    d = dict(row)
    return AuditRecord(
        log_id=int(d["log_id"]),
        ts=d["ts"],
        operator=d["operator"],
        action=d["action"],
        target_type=d["target_type"],
        target_id=d["target_id"],
        detail=detail_from_json(d.get("detail")),
        detail_raw=d.get("detail") or "",
        result=d["result"],
        prev_hash=d.get("prev_hash"),
        row_hash=d["row_hash"],
    )
