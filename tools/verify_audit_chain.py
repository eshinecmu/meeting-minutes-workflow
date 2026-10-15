"""审计链校验工具（T1-04）。

用法：
    python tools/verify_audit_chain.py                          # 校验 ./data/mmwf.db
    python tools/verify_audit_chain.py --db path/to.db          # 指定库
    python tools/verify_audit_chain.py --db D:/backup/x.db      # 校验备份（部署手册 §5.2 要求）
    python tools/verify_audit_chain.py --verbose                # 逐行打印
    python tools/verify_audit_chain.py --limit 50               # 只看最近 50 行

退出码
------
    0  链式哈希完好
    1  发现篡改（**这是安全事故信号，不是程序 bug**）
    2  用法/环境错误（库不存在、表不存在等）

关于退出码为什么要把"篡改"和"环境错误"分开
------------------------------------------
因为处置方式完全不同：
  - 退出码 2：去把库文件/迁移修好就行，属于运维问题；
  - 退出码 1：部署手册 §7.7 规定"立即停止使用、封存设备、上报保密机构、
    **不得自行修复**"——自行修复会破坏证据。
把两者混成一个非零码，自动化巡检就没法区分"跑完继续"和"报警拉闸"。

这个脚本会打印它**是只读打开的**，因为这一点很关键：
校验程序本身若能写审计库，就多了一个可以被利用的篡改入口。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmwf.infra.audit import (  # noqa: E402
    MINIMUM_CHECK_ITEM_ACTIONS,
    audit_minimum_coverage,
    verify_chain,
)

OK = "✓"
BAD = "✗"

EXIT_OK = 0
EXIT_TAMPERED = 1
EXIT_USAGE = 2


def _default_db_path() -> Path:
    """按 config/app.yaml 的 data_dir 推出业务库路径（与 migrate.py 同一套规则）。"""
    from mmwf.infra.db import app_db_path
    from mmwf.settings import load_settings

    settings = load_settings(PROJECT_ROOT / "config")
    return settings.resolve_path(app_db_path(settings.app.data_dir))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mmwf 审计链校验（红线项 2：审计链完整性）",
        epilog="退出码：0=完好，1=发现篡改，2=用法或环境错误",
    )
    parser.add_argument("--db", type=Path, default=None,
                        help="库文件路径（默认取 config/app.yaml 的 app.data_dir）")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="逐行打印每条记录的哈希，便于人工核对")
    parser.add_argument("--limit", type=int, default=None,
                        help="只校验最近的 N 行（大库排查时用；默认全量）")
    parser.add_argument("--skip-coverage", action="store_true",
                        help="跳过 8 类最小核查项覆盖检查")
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    print(f"审计链校验（只读打开，不修改任何数据）")
    print(f"  库文件：{db_path}")

    if not db_path.exists():
        print(f"{BAD} 库文件不存在：{db_path}")
        print("  提示：首次部署请先执行 python tools/migrate.py")
        return EXIT_USAGE

    try:
        result = verify_chain(db_path)
    except Exception as e:  # noqa: BLE001 - 顶层兜底，给可读错误而非堆栈
        print(f"{BAD} 校验失败：{type(e).__name__}: {e}")
        if "no such table" in str(e).lower():
            print("  提示：库中还没有 audit_log 表，请先执行 python tools/migrate.py")
        return EXIT_USAGE

    print(f"  记录总数：{result.total_rows}")
    print()

    if args.verbose:
        from mmwf.infra.audit import query

        records = query(db_path)
        if args.limit:
            records = records[-args.limit:]
        print(f"{'log_id':>7}  {'ts':<21} {'operator':<12} {'action':<18} "
              f"{'result':<8} row_hash")
        print("-" * 118)
        for r in records:
            print(f"{r.log_id:>7}  {r.ts:<21} {r.operator:<12} {r.action:<18} "
                  f"{r.result:<8} {r.row_hash[:20]}…")
        print()

    if result.ok:
        print(f"{OK} {result.summary()}")
    else:
        print(f"{BAD} {result.summary()}")
        print()
        print("发现以下问题行：")
        for p in result.problems:
            print(f"  {BAD} {p}")
        print()
        print("=" * 72)
        print("⚠  这是篡改信号，不是程序故障。请按部署手册 §7.7 处置：")
        print("   1. 立即停止使用本系统")
        print("   2. 封存设备与存储介质，保留现场")
        print("   3. 上报保密机构与信息化部门")
        print("   4. **不得自行修复**（自行修复会破坏证据）")
        print("=" * 72)

    if not args.skip_coverage:
        print()
        print("8 类最小核查项覆盖（部署手册 §6.2）：")
        coverage = audit_minimum_coverage(db_path)
        for item, state in coverage.items():
            actions = "/".join(sorted(MINIMUM_CHECK_ITEM_ACTIONS[item]))
            mark = {True: OK, False: BAD}.get(state == "ok")
            note = {"ok": "已有记录", "empty": "动作已登记，尚无记录",
                    "missing": "**动作未登记**"}[state]
            print(f"  {mark if state == 'ok' else ('·' if state == 'empty' else BAD)} "
                  f"{item:<16} [{actions}] {note}")
        missing = [k for k, v in coverage.items() if v == "missing"]
        if missing:
            print(f"\n{BAD} 有 {len(missing)} 项连动作都没登记：{missing}")
            print("   这属于配置缺失，需在 infra/audit.py 的 ACTIONS 中补登记并重新验收。")
        empty = [k for k, v in coverage.items() if v == "empty"]
        if empty:
            print(f"\n· 有 {len(empty)} 项动作齐备但库中尚无记录（属正常：功能可用、只是还没用到）")

    print()
    if result.ok:
        print(f"{OK} 审计链校验通过")
        return EXIT_OK
    print(f"{BAD} 审计链校验未通过（退出码 {EXIT_TAMPERED}）")
    return EXIT_TAMPERED


if __name__ == "__main__":
    sys.exit(main())
