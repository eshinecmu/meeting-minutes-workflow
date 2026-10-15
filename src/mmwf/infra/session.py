"""Process-owned session lock; the OS releases it even after an abnormal exit."""
from contextlib import contextmanager
import getpass
import os

from mmwf.domain.errors import MmwfError
from mmwf.infra.db import connect_ctx


def _secret_active(db_path):
    if not db_path.exists():
        return False
    with connect_ctx(db_path, readonly=True) as conn:
        return conn.execute(
            "SELECT 1 FROM meeting WHERE classification='state_secret' "
            "AND status NOT IN ('created','archived','cancelled','failed') LIMIT 1"
        ).fetchone() is not None


@contextmanager
def active_session(directory, db_path):
    directory.mkdir(parents=True, exist_ok=True)
    # Do not unlink: replacing the inode would allow a third instance to bypass it.
    with (directory / 'session.lock').open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            operator = handle.read().decode('utf-8', errors='replace') or '其他操作人'
            secret = _secret_active(db_path)
            raise MmwfError(code='E6002' if secret else 'E9003',
                            user_message=('涉密会议处理中，登录通道已锁定。' if secret else
                                          f'系统正由 {operator} 使用中，请稍后再试。')) from None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(getpass.getuser().encode('utf-8'))
            handle.flush()
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
