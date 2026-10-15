"""数据库迁移脚本目录。

命名规则：`<3位数字>_<小写描述>.sql`，版本号即 SQLite 的 `user_version`。

  001_init.sql  -> user_version = 1（13 张核心表 + 索引 + 审计防篡改触发器）

  002_template_revision.sql -> user_version = 2（T2-02 DB 自建模板历史）

执行入口：`python tools/migrate.py` 或 `mmwf.infra.migrations.runner.migrate(path)`。
"""
