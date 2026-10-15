"""基础设施层：数据库、文件仓储、加密、审计日志。

分层约束（SAD C01）：本层**不得**被领域层 import；
本层可以 import 领域的纯类型定义。

对外主要入口：
    db.connect / connect_ctx / transaction      —— 数据库连接与事务
    migrations.runner.migrate                    —— 建库与版本推进
    filestore.FileStore / make_filestore         —— 文件仓储（透明加解密）
    crypto.FileCipher                            —— AES-256-GCM 原语
    keystore.load_or_create_key                  —— 主密钥来源
    audit.AuditLogger / verify_chain / build_report —— 审计日志（只 INSERT + 链式哈希）

注意：audit 模块**不在此处 import**。原因有两点：
  1. 它是"谁都要用"的横切模块，在包的 `__init__` 里拉进来会让
     `import mmwf.infra` 变成隐式重依赖；
  2. 审计写入有副作用（建连接、抢写锁），不适合在 import 期触发。
调用方请显式 `from mmwf.infra.audit import AuditLogger`。
"""

from __future__ import annotations

from mmwf.infra.db import app_db_path, connect, connect_ctx, transaction

__all__ = ["app_db_path", "connect", "connect_ctx", "transaction"]
