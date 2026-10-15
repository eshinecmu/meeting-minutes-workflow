"""幂等键与幂等记录（T1-10，SAD §5.4）。

为什么编排层需要这个东西
------------------------
一次流水线跑下来，"有副作用"的动作有四个：写切片文件、写记录稿、
导出 docx、写归档。它们和纯计算的节点不同 —— **重复执行会留下痕迹**：
多一个文件、多一条归档记录、多一份 docx。

重复执行在三种情况下会真实发生，都不是 bug：

  1. **断点续跑**（F6.2）。进程被杀后重启，LangGraph 从检查点恢复，
     恢复点之前的节点会被"重放"（re-executed）。
  2. **闸口 reject**。SAD §5.3 规定 reject 要"清除当前节点产物、
     退回上游重规划"，上游一旦重跑，它下游的副作用节点也会重跑。
  3. **同一份内容重复导出**（F6.3 / IT-09）。用户点了两次导出按钮。

所以"防重"不能靠"约束调用方不要重复调用"，必须由被调用方自己判重。
这就是幂等键的职责。

键的构造（SAD §5.4 原文）
------------------------
    key = sha256(f"{task_id}:{node_id}:{node_input_hash}")[:32]
    node_input_hash = sha256(规范化 JSON 的哈希)

**关键在 `node_input` 的选取**：它必须恰好包含"决定产物内容"的字段。
选少了（比如导出节点只看 meeting_id）会让"内容改了但键没变"——
用户改了纪要再导出，拿回的还是旧文件，这是**静默错误**，比报错危险。
选多了（比如把时间戳、随机数放进去）则键每次都变，等于没有幂等。

见 `node_input_from_state()`：每个节点该用哪些字段是显式写死的，
不是 `hash(state)` —— 那样任何一个无关字段变化都会让幂等失效。

冲突时的行为
------------
SAD §5.4 明写"**冲突时返回既有结果而非报错**"。所以 `get_or_run()` 的
语义是"要么执行要么复用"，而不是"发现重复就抛异常"。后者会把一个
正常场景（用户点了两次导出）变成用户可见的报错。

落库
----
写入 `idempotency_record` 表（001_init.sql ⑫），`idem_key` 是主键，
唯一约束天然保证并发下只有一个成功。**用 INSERT OR IGNORE + 回读**
而不是"先查再插"：后者在并发下会两个进程同时查到"不存在"然后都插入，
其中一个撞主键报错。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mmwf.infra import db as dbmod

logger = logging.getLogger(__name__)

#: 键的长度（SAD §5.4 定死 32）。
KEY_LENGTH = 32


def canonical_json(payload: Any) -> str:
    """规范化 JSON —— 幂等键的输入必须与键序、空白无关。

    `sort_keys=True` 让 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 得到同一个键。
    不加的话，同一个字典换个构造顺序（比如从不同 SQL 查询拼出来）
    就会算出不同的键，幂等静默失效。
    """
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def content_hash(payload: Any) -> str:
    """规范化 JSON 的 sha256（十六进制）。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def make_idempotency_key(task_id: str, node_id: str, node_input: Any) -> str:
    """构造幂等键（SAD §5.4 的公式，逐字实现）。

    Args:
        task_id: 任务标识。一期 `task_id == meeting_id`（001_init.sql 备注）。
        node_id: 节点标识（`"export"` / `"archive"` / …）。
        node_input: **决定产物内容**的关键字段（见 `node_input_from_state`）。
            dict 会被规范化后再哈希；字符串/数字直接转成字符串。
    """
    if not task_id or not task_id.strip():
        # 空 task_id 会让所有任务的键撞在一起 —— 必须立刻拦住。
        # 这比"算出个错键"好得多：后者会让任务 A 导出时读到任务 B 的产物。
        # 只判 `not x` 会漏掉 `"   "`：空白串不是空串，但它同样不携带
        # 任何区分信息，两个不同会议都传 `"   "` 照样会撞。
        raise ValueError("task_id 不能为空（幂等键的第一段）")
    if not node_id or not node_id.strip():
        raise ValueError("node_id 不能为空（幂等键的第二段）")

    if isinstance(node_input, (dict, list, tuple)):
        input_hash = content_hash(list(node_input) if isinstance(node_input, tuple) else node_input)
    else:
        # 标量也要走规范化 JSON，**不能直接 `str(x)`**：
        # `str(1)` 和 `str("1")` 都是 `"1"`，会让"数字 1"与"字符串 '1'"
        # 算出同一个键。这两个值在业务上意味着不同的产物，
        # 撞键的后果是后一个节点错误复用了前一个节点的产物。
        # 规范化 JSON 保留了类型（`1` vs `"1"`），顺带把手写的英文
        # 中文在日志里也保持可读。
        input_hash = content_hash(node_input)

    raw = f"{task_id}:{node_id}:{input_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:KEY_LENGTH]


# ---------------------------------------------------------------------------
# 「这个节点该用哪些字段算键」——显式表，不是 hash(state)
# ---------------------------------------------------------------------------

#: 节点 -> 参与幂等键的状态字段。
#:
#: 只列**决定产物内容**的字段。刻意不列出 `operator` / `errors` /
#: `validation_issues` 这类过程数据；成文所用 issues 与纪要上下文显式纳入。
#:
#: 注意 `NODE_ARCHIVE` 用的是最终产物哈希 —— 归档的是"文件"，
#: 只要导出物内容相同，重复归档就应当复用同一条目。
IDEMPOTENT_INPUT_KEYS: dict[str, tuple[str, ...]] = {
    "ingest": ("audio_paths",),
    "transcribe": ("audio_paths", "chunk_plan"),
    # 记录稿的内容由转写稿决定（版本号是"再一次"的语义，不进键）
    #
    # ⚠️ `record_markdown` 必须进键（T1-11 Review 补的第 7 项）。
    #
    # `transcribe` 的返回值**不进**图状态（它返回的键只有
    # `transcript` / `transcript_meta` / `errors`）。而"文字稿导入"这条路上
    # `transcript` 恒为空表 —— 于是键里带的 `transcript=[]` 从来没变过：
    # 用户在界面上改了记录稿、再点心智中"重新生成"，`review` 会**幂等命中**
    # 上一轮的结果，改的内容永远不生效。
    #
    # 表现极具迷惑性：不报错、不提示，就是"改了没反应"。
    # 带上 `record_markdown` 以后，改过正文就会得到一个新键 ——
    # 该重跑的重跑，没改的还是命中缓存。
    "review": ("transcript", "record_markdown"),
    "extract": ("record_markdown",),
    "generate": ("topics", "resolutions", "action_items"),
    # 成文是确定性渲染：结构化数据一样，Markdown 就一样
    "assemble": ("minutes_structured", "record_markdown", "transcript", "minutes", "issues"),
    "export": ("minutes_markdown", "minutes_structured", "record_markdown", "transcript", "minutes"),
    "archive": ("export_paths",),
}

#: 被幂等保护的副作用节点（施工卡"幂等（SAD §5.4）"列的四个）。
#: 纯计算节点不落 `idempotency_record`：把每次转写都记一条，
#: 表会迅速膨胀，而它对"防重"毫无贡献（转写本来就不产生文件）。
SIDE_EFFECT_NODES: frozenset[str] = frozenset(
    {"ingest", "export", "archive"}
)


def node_input_from_state(node_id: str, state: dict) -> dict:
    """按 `IDEMPOTENT_INPUT_KEYS` 从状态里取该节点的幂等输入。

    缺字段时**用 None 占位而不是跳过**：跳过会让"键里少一段"，
    与"字段值为 None"算出同一个键 —— 那意味着"还没算出来的产物"
    和"算出来是空的产物"被判成同一个，会错误复用。
    """
    keys = IDEMPOTENT_INPUT_KEYS.get(node_id)
    if keys is None:
        raise KeyError(
            f"节点 {node_id!r} 没有登记幂等输入字段；"
            f"若它确实无副作用，不应调用本函数"
        )
    if node_id in {"assemble", "export"}:
        from mmwf.domain.minutes.state_compat import upgrade_legacy_state
        state = upgrade_legacy_state(state)
        context = (state.get("minutes") or {}).get("_context", {})
        out = {k: state.get(k) for k in keys}
        out["fingerprints"] = {
            "source": content_hash({"record": state.get("record_markdown"), "segments": state.get("transcript")}),
            "template": content_hash(context.get("template_snapshot") or context.get("template_fingerprint")),
            "candidate": content_hash(state.get("minutes_structured") or state.get("minutes")),
        }
        return out
    return {k: state.get(k) for k in keys}


# ---------------------------------------------------------------------------
# 幂等记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdempotencyHit:
    """一次幂等命中的结果。"""

    idem_key: str
    task_id: str
    node_id: str
    result_ref: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "idem_key": self.idem_key,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "result_ref": self.result_ref,
        }


class IdempotencyStore:
    """`idempotency_record` 表的读写（SAD §5.4）。

    连接按需打开、用完关闭 —— 不做长连接，因为编排是"一次调用一次落库"，
    长连接只会在多线程场景下制造"连接被两个线程共用"的问题
    （施工卡"常见坑"最后一条点名的正是这个）。

    Args:
        db_path: 主库路径。传 `:memory:` 会因每次新开连接而丢数据，
            所以**不允许**（测试请用临时文件，见 tests 的 fresh_env）。
        ensure_schema: 首次使用前自动跑一遍迁移（默认 True）。

            为什么默认开着：`IdempotencyStore` 的每一个方法都**按需开连接**，
            没有"打开时统一建表"的时机 —— 忘了跑迁移的话，第一处
            失败会以 `sqlite3.OperationalError: no such table` 的形式
            出现在 `put()` 里，而调用栈离真正的原因（没迁移）很远。
            迁移本身幂等（`runner.migrate` 会跳过已应用版本），
            在已迁移的库上多跑一次只是几次 `PRAGMA` 查询。

            想跳过（比如库是只读挂载）时显式传 `ensure_schema=False`。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        now: Callable[[], str] | None = None,
        ensure_schema: bool = True,
    ) -> None:
        p = Path(db_path)
        if str(db_path) == ":memory:":
            raise ValueError(
                "IdempotencyStore 不支持 :memory:（每次连接都是新库，"
                "幂等记录写进去就没了）。测试请用临时文件。"
            )
        self._db_path = p
        self._now = now or _default_now
        self._schema_ready = False
        if ensure_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """确保 `idempotency_record` 存在。幂等，可反复调用。"""
        if self._schema_ready:
            return
        from mmwf.infra.migrations import runner as _runner

        _runner.migrate(self._db_path)
        self._schema_ready = True

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, idem_key: str) -> IdempotencyHit | None:
        """按键取既有记录；没有则 None。"""
        with dbmod.connect_ctx(self._db_path) as conn:
            row = conn.execute(
                "SELECT idem_key, task_id, node_id, result_ref "
                "FROM idempotency_record WHERE idem_key = ?",
                (idem_key,),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyHit(
            idem_key=row["idem_key"],
            task_id=row["task_id"],
            node_id=row["node_id"],
            result_ref=row["result_ref"],
        )

    def find(self, task_id: str, node_id: str) -> list[IdempotencyHit]:
        """列出某任务某节点的全部记录（供排查与验收脚本用）。"""
        with dbmod.connect_ctx(self._db_path) as conn:
            rows = conn.execute(
                "SELECT idem_key, task_id, node_id, result_ref "
                "FROM idempotency_record WHERE task_id = ? AND node_id = ? "
                "ORDER BY created_at",
                (task_id, node_id),
            ).fetchall()
        return [
            IdempotencyHit(
                idem_key=r["idem_key"],
                task_id=r["task_id"],
                node_id=r["node_id"],
                result_ref=r["result_ref"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def put(self, idem_key: str, task_id: str, node_id: str,
            result_ref: str | None) -> bool:
        """登记一条幂等记录。返回 True 表示**本次是首个写入者**。

        用 `INSERT OR IGNORE`：并发下两个进程都会尝试插入，
        只有一个 rowcount == 1，另一个静默忽略（它拿到的是别人的成果）。
        "先 SELECT 再 INSERT" 在这里是错的 —— 两步之间那个窗口足以
        让两个进程都以为自己是第一个。
        """
        with dbmod.connect_ctx(self._db_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO idempotency_record "
                "(idem_key, task_id, node_id, result_ref, created_at) "
                "VALUES (?, ?, ?, mmwf_encrypt(?), ?)",
                (idem_key, task_id, node_id, result_ref, self._now()),
            )
            return cur.rowcount == 1

    def get_or_run(
        self,
        task_id: str,
        node_id: str,
        node_input: Any,
        fn: Callable[[], Any],
        *,
        result_ref_of: Callable[[Any], str | None] | None = None,
    ) -> tuple[Any, bool]:
        """幂等执行：命中则复用，未命中则执行并登记。

        Returns:
            `(结果, 是否命中了既有记录)`。
            命中时返回的是 **`result_ref`（先前登记的产物引用）**，
            而不是先前那次调用的返回值 —— 后者没有被持久化，
            跨进程恢复时根本拿不到。这是设计上的取舍：
            产物必须是"可重新获取的引用"（路径 / ID），而不是内存对象。

        Args:
            fn: 真正干活的函数，无参数（用闭包捕获所需输入）。
            result_ref_of: 把 `fn` 的返回值转成产物引用。默认取
                `Path` / `.rel_path` / `.filename` / `str()`。
        """
        key = make_idempotency_key(task_id, node_id, node_input)
        hit = self.get(key)
        if hit is not None:
            logger.info("幂等命中，复用既有产物：%s/%s -> %s",
                        task_id, node_id, hit.result_ref)
            return hit.result_ref, True

        result = fn()
        ref = (result_ref_of or _default_result_ref)(result)
        inserted = self.put(key, task_id, node_id, ref)
        if not inserted:
            # 极小概率：查的时候没有，插的时候别人已经插了（并发）。
            # 尊重先写入者的结果，而不是覆盖 —— 否则两个进程会互相
            # 覆盖对方的产物引用，最后指向一个可能已被删掉的路径。
            winner = self.get(key)
            if winner is not None:
                logger.info("并发写入，改用先写入者的产物：%s", winner.result_ref)
                return winner.result_ref, True
        return result, False

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def drop(self, idem_key: str) -> bool:
        """删除一条记录（供"撤销导出后再导出"这类场景）。

        正常编排路径**不该**调用它 —— 幂等记录是"已做过"的凭据，
        主动删掉等于放弃防重。它存在的意义是给运维一个明确的
        "我要重来一次"的动作，而不是让代码随手清理。
        """
        with dbmod.connect_ctx(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM idempotency_record WHERE idem_key = ?", (idem_key,)
            )
            return cur.rowcount == 1

    def count(self) -> int:
        with dbmod.connect_ctx(self._db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM idempotency_record").fetchone()
        return int(row["n"])


def _default_result_ref(result: Any) -> str | None:
    """默认的产物引用提取。

    按"最常见到最兜底"的顺序试：显式属性 -> 文件属性 -> 原样 str。
    刻意**不用** `isinstance(x, Path)` 做首判：服务层返回的
    dataclass（如 `ExportResult`）更常见，而它有 `rel_path` 属性。
    """
    if result is None:
        return None
    for attr in ("result_ref", "rel_path", "filename", "path"):
        val = getattr(result, attr, None)
        if val:
            return str(val)
    if isinstance(result, (str, Path)):
        return str(result)
    if isinstance(result, dict):
        for k in ("rel_path", "path", "filename"):
            if result.get(k):
                return str(result[k])
    return str(result)


def _default_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "IDEMPOTENT_INPUT_KEYS",
    "KEY_LENGTH",
    "SIDE_EFFECT_NODES",
    "IdempotencyHit",
    "IdempotencyStore",
    "canonical_json",
    "content_hash",
    "make_idempotency_key",
    "node_input_from_state",
]
