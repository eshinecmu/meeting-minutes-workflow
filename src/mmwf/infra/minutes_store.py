"""既有 minutes_version 的加密配对快照；单进程会议锁及 SQLite 事务。"""
import base64
import json
import threading
from datetime import datetime, timezone
from uuid import uuid4

from mmwf.domain.errors import MmwfError
from mmwf.infra.crypto import FileCipher
from mmwf.infra.db import connect_ctx, transaction
from mmwf.infra.keystore import load_or_create_key

_locks = {}
_guard = threading.Lock()


def minutes_lock(db_path, meeting_id):
    with _guard:
        return _locks.setdefault((str(db_path.resolve()), meeting_id), threading.RLock())


class MinutesStore:
    def __init__(self, db_path):
        from pathlib import Path
        self.db_path = Path(db_path)
        self.cipher = FileCipher(load_or_create_key(self.db_path.parent).key)

    def _encode(self, value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
        return json.dumps({'encrypted': base64.b64encode(self.cipher.encrypt_bytes(raw)).decode('ascii')})

    def _decode(self, value):
        return json.loads(self.cipher.decrypt_bytes(base64.b64decode(json.loads(value)['encrypted'])))

    def _row(self, row):
        if row is None:
            return None
        result = dict(row)
        for key in ('structured_json', 'markdown', 'note'):
            result[key] = self._decode(result[key])
        return result

    def get(self, meeting_id, version=None):
        with connect_ctx(self.db_path) as conn:
            row = conn.execute('SELECT * FROM minutes_version WHERE meeting_id=? ' +
                ('ORDER BY version_no DESC LIMIT 1' if version is None else 'AND version_no=?'),
                (meeting_id,) if version is None else (meeting_id, version)).fetchone()
        return self._row(row)

    def save(self, meeting_id, structured, markdown, note, *, created_by='system'):
        """相同配对指纹复用；敏感正文、问题清单不以明文入库。"""
        with minutes_lock(self.db_path, meeting_id), connect_ctx(self.db_path) as conn, transaction(conn, immediate=True):
            rows = conn.execute('SELECT * FROM minutes_version WHERE meeting_id=? ORDER BY version_no', (meeting_id,)).fetchall()
            for row in rows:
                decoded = self._row(row)
                if decoded['note']['pair_fingerprint'] == note['pair_fingerprint']:
                    return decoded
            version = max((r['version_no'] for r in rows), default=0) + 1
            conn.execute('INSERT INTO minutes_version (version_id,meeting_id,version_no,structured_json,markdown,is_final,created_by,created_at,note) VALUES (?,?,?,?,?,0,?,?,?)',
                (uuid4().hex, meeting_id, version, self._encode(structured), self._encode(markdown),
                 created_by, datetime.now(timezone.utc).isoformat(), self._encode(note)))
        return self.get(meeting_id, version)

    def mark_final(self, meeting_id, version, pair_fingerprint):
        """仅由交付服务在 G3 后复检成功时调用。"""
        with minutes_lock(self.db_path, meeting_id), connect_ctx(self.db_path) as conn, transaction(conn, immediate=True):
            row = self._row(conn.execute('SELECT * FROM minutes_version WHERE meeting_id=? AND version_no=?', (meeting_id, version)).fetchone())
            if row is None or row['note']['pair_fingerprint'] != pair_fingerprint:
                raise MmwfError(code='E4001', detail='纪要配对指纹不一致')
            conn.execute('UPDATE minutes_version SET is_final=1 WHERE meeting_id=? AND version_no=?', (meeting_id, version))
        return self.get(meeting_id, version)
