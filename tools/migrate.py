"""建库 / 迁移命令行入口（T1-02）。

用法：
    python tools/migrate.py                     # 建/升级 ./data/mmwf.db 到最新版本
    python tools/migrate.py --db path/to.db     # 指定库文件
    python tools/migrate.py --check             # 只检查是否已是最新，不做变更
    python tools/migrate.py --list              # 列出所有迁移与已应用状态

设计：库路径默认取 config/app.yaml 的 app.data_dir，避免把路径硬编码在两处
（N5.2：配置可外部修改，代码不硬编码路径）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.infra.db import app_db_path, connect, connect_ctx, pragma_value  # noqa: E402
from mmwf.infra.migrations.runner import (  # noqa: E402
    discover_migrations,
    expected_tables,
    get_user_version,
    list_tables,
    migrate,
)
from mmwf.settings import load_settings  # noqa: E402


def _default_db_path() -> Path:
    """按 config/app.yaml 的 data_dir 推出业务库路径。"""
    settings = load_settings(PROJECT_ROOT / "config")
    return settings.resolve_path(app_db_path(settings.app.data_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description="mmwf 建库与迁移")
    parser.add_argument("--db", type=Path, default=None,
                        help="库文件路径（默认取 config/app.yaml 的 app.data_dir）")
    parser.add_argument("--check", action="store_true",
                        help="只检查是否最新，不做任何变更（退出码 1 表示待迁移）")
    parser.add_argument("--list", action="store_true", dest="do_list",
                        help="列出全部迁移与已应用状态")
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    migrations = discover_migrations()
    latest = max((m.version for m in migrations), default=0)

    if args.do_list:
        print(f"迁移目录：{migrations[0].path.parent if migrations else '(空)'}")
        applied: set[int] = set()
        if db_path.exists():
            with connect_ctx(db_path, readonly=True) as conn:
                applied = set(range(1, get_user_version(conn) + 1))
        for m in migrations:
            mark = "已应用" if m.version in applied else "待应用"
            print(f"  [{mark}] {m.version:03d}_{m.name}.sql")
        print(f"\n库：{db_path}")
        print(f"库版本：{get_user_version(connect(db_path)) if db_path.exists() else 0}，"
              f"最新版本：{latest}")
        return 0

    if args.check:
        if not db_path.exists():
            print(f"✗ 库不存在，需要首次建库：{db_path}")
            return 1
        with connect_ctx(db_path, readonly=True) as conn:
            cur = get_user_version(conn)
        if cur == latest:
            print(f"✓ 库已是最新版本（user_version={cur}）：{db_path}")
            return 0
        print(f"✗ 库版本 {cur} 落后于最新 {latest}，需要迁移：{db_path}")
        return 1

    existed = db_path.exists()
    version = migrate(db_path, verbose=True)

    with connect(db_path) as conn:
        tables = list_tables(conn)
        fk = pragma_value(conn, "foreign_keys")

    print()
    print(f"{'✓' if fk == 1 else '✗'} PRAGMA foreign_keys = {fk}")
    print(f"{'✓' if len(tables) == len(expected_tables()) else '✗'} 业务表 {len(tables)} 张：{tables}")
    print(f"{'✓' if version == latest else '✗'} user_version = {version}（最新 {latest}）")
    print(f"\n{'首次建库完成' if not existed else '迁移完成'}：{db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
