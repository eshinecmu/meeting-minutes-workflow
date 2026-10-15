"""闸口快照与决策登记；审计链和登记共用 BEGIN IMMEDIATE。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from urllib.parse import quote

from mmwf.domain.review.gate_edit import canonical_json
from mmwf.graph.idempotency import make_idempotency_key
from mmwf.infra.audit import AuditLogger
from mmwf.infra.db import connect_ctx, transaction


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class GateStore:
    def __init__(self, db_path, data_root):
        self.db_path = Path(db_path)
        self.data_root = Path(data_root)

    @staticmethod
    def key(meeting_id, gate, checkpoint_id):
        return make_idempotency_key(meeting_id, "gate_decision", {"gate": gate, "checkpoint_id": checkpoint_id})

    def get(self, meeting_id, gate, checkpoint_id):
        with connect_ctx(self.db_path) as conn:
            row = conn.execute("SELECT result_ref FROM idempotency_record WHERE idem_key=?",
                               (self.key(meeting_id, gate, checkpoint_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot(self, meeting_id, checkpoint_id, content):
        # 编码整个 ID，避免检查点带路径分隔符；前缀也避免 '.' / '..'。
        ref = f"gate/ckpt-{quote(checkpoint_id, safe='')}/snapshot.json"
        dst = self.data_root / meeting_id / ref
        dst.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dst.parent, delete=False) as f:
                temp = Path(f.name)
                f.write(canonical_json(content))
            temp.replace(dst)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return ref

    def register(self, record, detail):
        audit = AuditLogger(self.db_path)
        with connect_ctx(self.db_path) as conn, transaction(conn, immediate=True):
            key = self.key(record["meeting_id"], record["gate"], record["checkpoint_id"])
            old = conn.execute("SELECT result_ref FROM idempotency_record WHERE idem_key=?", (key,)).fetchone()
            if old:
                return json.loads(old[0])
            conn.execute("INSERT INTO idempotency_record (idem_key, task_id, node_id, result_ref, created_at) VALUES (?,?,?,?,?)",
                         (key, record["meeting_id"], "gate_decision", canonical_json(record), now_iso()))
            # 所有审计写入均由 SQLite 写锁串行保护链头，不仅依赖会议锁。
            audit.log(record["operator"], "gate_decision", "meeting", record["meeting_id"], detail, _conn=conn)
        return record

    def registered(self):
        """只枚举待恢复登记；消费标记复用 result_ref JSON，不改表结构。"""
        with connect_ctx(self.db_path) as conn:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT result_ref FROM idempotency_record WHERE node_id='gate_decision' "
                "AND COALESCE(json_extract(result_ref, '$.consumed'), 0)=0 ORDER BY created_at")]

    def mark_consumed(self, record):
        """保留防重决定，仅标记不再需要恢复；不更新不可变审计链。"""
        with connect_ctx(self.db_path) as conn, transaction(conn, immediate=True):
            conn.execute("UPDATE idempotency_record SET result_ref=json_set(result_ref, '$.consumed', json('true')) "
                         "WHERE idem_key=?", (self.key(record["meeting_id"], record["gate"], record["checkpoint_id"]),))

    def timeout_count(self, meeting_id):
        with connect_ctx(self.db_path) as conn:
            rows = conn.execute("SELECT detail FROM audit_log WHERE action='gate_decision' AND target_id=? AND result='success'", (meeting_id,))
            return len({d["checkpoint_id"] for r in rows if (d := json.loads(r[0])).get("auto") is True and d.get("reason") == "timeout"})

    def observe(self, meeting_id, status, created_at):
        pending = status.pending_gate
        stamp = now_iso()
        with connect_ctx(self.db_path) as conn, transaction(conn, immediate=True):
            # 同轮始终取不可变 checkpoint.created_at，读取不会续期。
            conn.execute("""INSERT INTO task_state (task_id,current_node,status,awaiting_actor,awaiting_since,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                current_node=excluded.current_node,status=excluded.status,awaiting_actor=excluded.awaiting_actor,
                awaiting_since=excluded.awaiting_since,updated_at=excluded.updated_at""",
                (meeting_id, status.current_node or status.status, status.status, status.awaiting_actor,
                 created_at if pending else None, stamp))
