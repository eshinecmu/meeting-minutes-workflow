"""组装免安装交付目录（T1 打包）。

产出（对应部署手册 §10 附录 A 的布局）::

    dist/mmwf-portable/
    ├── desktop/                 # 桌面壳（双击 mmwf.exe 启动）
    │   ├── mmwf.exe
    │   └── resources/
    │       ├── app/             # mmwf 应用代码
    │       ├── config/          # 配置
    │       └── python/          # 内嵌 Python 运行时 + 依赖
    └── 使用说明.txt

★ 为什么 python/ 与 app/ 放在 `resources/` 里而不是与 exe 同级
-------------------------------------------------------------
`main.js` 打包分支读的是 `process.resourcesPath`（Electron 标准布局），
即 `<exe 同级>/resources/`。部署手册 §10 附录 A 画的是
`app/` 与 `runtime/` 与 `desktop/` **同级** —— 两者不一致。

本次取 **Electron 标准布局**，理由：**零代码改动**即可跑通，
而手工检查要的是"能双击起来"。手册布局需要改 `main.js` 的路径解析，
属另一件事，已在 README 记为待办。

★ 配置为什么放 `resources/config` 而不是 `resources/app/config`
---------------------------------------------------------------
`settings.py::_find_config_dir` 的默认值是
`Path(__file__).resolve().parents[2] / "config"`。
应用代码在 `resources/app/mmwf/settings.py` 时，
`parents[2]` 正好是 `resources/` —— 所以配置要落在 `resources/config`。
这是**零代码改动**能生效的唯一位置。改动它会静默读不到配置
（启动时报"缺少 config/app.yaml"，但那个报错不会告诉你是路径层级错了）。

用法：
    python tools/build_portable.py            # 组装 + 调 electron-builder
    python tools/build_portable.py --stage-only   # 只组装，不打包
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGE = PROJECT_ROOT / "build" / "stage"
DIST = PROJECT_ROOT / "dist"
DESKTOP = PROJECT_ROOT / "src" / "desktop"
#: Electron 运行时（随 devDependency 下载）与 asar CLI —— 直拼路径要用。
ELECTRON_DIST = DESKTOP / "node_modules" / "electron" / "dist"
ASAR_CLI = DESKTOP / "node_modules" / "@electron" / "asar" / "bin" / "asar.js"

#: 内嵌 Python 的基座 —— 从当前 venv 的 `pyvenv.cfg` 读出来，
#: **不写死路径**：换机器/换 Python 版本时不用改脚本。
def _base_python_dir() -> Path:
    cfg = PROJECT_ROOT / ".venv" / "pyvenv.cfg"
    if not cfg.exists():
        raise SystemExit(f"找不到 {cfg}，请先创建 venv")
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("home"):
            return Path(line.split("=", 1)[1].strip())
    raise SystemExit(f"{cfg} 里没有 home 行")


#: 复制时排除的目录名（缓存/编译产物，没有价值且体积大）。
_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _copy_tree(src: Path, dst: Path, *, label: str) -> None:
    """复制目录树，跳过缓存目录与 `.pyc`。

    ⚠️ 这里**不做"删除目标里多余文件"**的动作。
    踩过的坑：`stage_runtime()` 是**两个来源合并**到同一个 `python/`
    （先是解释器基座、再是 `.venv` 的 site-packages），
    按"基座里没有就删掉"去清残留，会把第二次要拷进去的 site-packages
    当成残留删掉 —— 打出来的交付目录直接缺依赖。
    清残留只能靠"整目录先清空"，见 `main()`。
    """
    if not src.exists():
        raise SystemExit(f"{label} 源目录不存在：{src}")
    dst.mkdir(parents=True, exist_ok=True)
    n_files = 0
    for item in src.rglob("*"):
        if any(part in _EXCLUDE_DIRS for part in item.parts):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            if item.suffix == ".pyc":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            n_files += 1
    print(f"  ✓ {label}: {n_files} 个文件 -> {dst.relative_to(PROJECT_ROOT)}")


def stage_runtime() -> None:
    """组装 `python/`（内嵌运行时 + 依赖）。"""
    base = _base_python_dir()
    print(f"内嵌运行时基座：{base}")
    _copy_tree(base, STAGE / "python", label="Python 运行时")
    _copy_tree(
        PROJECT_ROOT / ".venv" / "Lib" / "site-packages",
        STAGE / "python" / "Lib" / "site-packages",
        label="第三方依赖",
    )


def stage_app() -> None:
    """组装 `app/`（应用代码）与 `config/`。"""
    _copy_tree(PROJECT_ROOT / "src" / "mmwf", STAGE / "app" / "mmwf", label="应用代码")
    _copy_tree(PROJECT_ROOT / "config", STAGE / "config", label="配置")


def stage_tools() -> None:
    """把运维脚本一并带上（部署手册 §7 故障排查要用到）。

    放在 `app/tools/` 而不是 `resources/tools/` —— 因为排查命令是
    `python -c ...` / `python <脚本路径>`，脚本要能被 `app` 下的
    Python 直接按路径调用，和 `mmwf` 包放一起最省事。
    """
    _copy_tree(PROJECT_ROOT / "tools", STAGE / "app" / "tools", label="运维脚本")


def write_manifest(dest: Path | None = None, *, mode: str = "electron-builder") -> None:
    """写一份组装清单（版本 + 时间 + 关键路径）。"""
    base = _base_python_dir()
    info = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python_base": str(base),
        "python_version": (base / "python.exe").exists() and _py_version(base) or "?",
        "layout": "electron-standard (resources/{app,config,python})",
        "assembled_by": mode,
    }
    target = (dest if dest is not None else STAGE) / "build-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  ✓ 清单：{info['python_version']}（{mode}）-> {target.relative_to(PROJECT_ROOT)}")


def _py_version(base: Path) -> str:
    try:
        out = subprocess.run(
            [str(base / "python.exe"), "-c",
             "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        return out.stdout.strip() or "?"
    except Exception:  # noqa: BLE001
        return "?"


def run_builder(out_dir: Path | None = None) -> int:
    """调 electron-builder 打包桌面壳。

    `out_dir` 用于**换一个输出目录**：electron-builder 打包前会先删掉
    已存在的 `win-unpacked`，在 WorkBuddy 环境里这一步会被批量删除闸门拦下
    （4600+ 文件）。换到一个还不存在的目录就绕开了删除，用来做"干净重建"。
    """
    npm = "npm.cmd"
    cmd = [npm, "run", "dist"]
    if out_dir is not None:
        rel = os.path.relpath(out_dir, DESKTOP).replace("\\", "/")
        cmd += ["--", f"-c.directories.output={rel}"]
    print(f"\n调用 electron-builder …（{out_dir or '默认 dist/'}）")
    proc = subprocess.run(
        cmd,
        cwd=str(DESKTOP), shell=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (proc.stdout or "")[-3000:]
    print(tail)
    if proc.returncode != 0:
        print("--- stderr ---")
        print((proc.stderr or "")[-3000:])
    return proc.returncode


def _node_exe() -> str:
    return shutil.which("node") or "node"


def assemble_direct(out_root: Path) -> int:
    """不依赖 electron-builder，直接拼出免安装目录 —— **全程只复制，不删除**。

    为什么需要这条路径（T1 收尾实测）：
      electron-builder 打包前会 `rm` 掉输出目录，再解包 Electron 时又会 `rm`
      一次临时目录，动辄数千个文件。WorkBuddy 这类环境带一道**批量删除闸门**
      （单次删除超过阈值直接抛错），于是打包在 `extractArchive` 处硬失败，
      且**不允许绕过**。而打包本身只是"把已知的东西摆到已知的位置"，
      完全可以自己摆 —— 本函数就是干这个的，不需要任何删除权限。

    与 electron-builder 产物的差异（已知且可接受，写在这里免得后人踩）：
      · `mmwf.exe` 的版本资源/图标仍是 Electron 默认值（没做签名与元数据改写）；
        窗口标题由 main.js 控制，不受影响；内网分发本来也不签名。
      · 不生成 `resources/app.asar.unpacked`（本项目没有需要解包的原生模块）。
      · 不做 `LICENSE -> LICENSE.electron.txt` 改名（纯装饰）。
    """
    app_out = out_root / "win-unpacked"
    if app_out.exists():
        print(f"✗ 目标目录已存在，拒绝在半成品上覆盖：{app_out}")
        print("  换一个 --out-dir，或自行清空该目录后重试。")
        return 2
    if not ELECTRON_DIST.exists():
        print(f"✗ 找不到 Electron 运行时：{ELECTRON_DIST}")
        return 2
    if not ASAR_CLI.exists():
        print(f"✗ 找不到 asar CLI：{ASAR_CLI}")
        return 2

    print(f"① 复制 Electron 运行时 -> {app_out.relative_to(PROJECT_ROOT)}")
    # ★ 不复制 `default_app.asar`：那是 Electron 自带的"欢迎页"应用，
    #   只有当 `resources/` 下既没有 `app.asar` 也没有 `app/` 时才会被加载。
    #   正常情况轮不到它，但**交付目录里留着它就是个歧义** ——
    #   一旦哪天 app.asar 没打成功，界面会变成 Electron 的默认页而不是报错，
    #   排查方向会被带偏。electron-builder 也是把它删掉的，这里等价处理。
    shutil.copytree(
        ELECTRON_DIST, app_out,
        ignore=shutil.ignore_patterns("default_app.asar"),
    )

    print("② electron.exe -> mmwf.exe")
    (app_out / "electron.exe").rename(app_out / "mmwf.exe")

    print("③ 打包 resources/app.asar")
    resources = app_out / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    asar_src = STAGE / "asar-src"
    asar_src.mkdir(parents=True, exist_ok=True)
    for name in ("main.js", "preload.js", "package.json"):
        shutil.copy2(DESKTOP / name, asar_src / name)
    _copy_tree(DESKTOP / "renderer", asar_src / "renderer", label="渲染层")
    proc = subprocess.run(
        [_node_exe(), str(ASAR_CLI), "pack", str(asar_src), str(resources / "app.asar")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("--- asar pack 失败 ---")
        print((proc.stdout or "")[-1500:])
        print((proc.stderr or "")[-1500:])
        return proc.returncode
    print(f"  ✓ app.asar：{(resources / 'app.asar').stat().st_size} 字节")

    print("④ 复制 python / app / config 到 resources/")
    for sub in ("python", "app", "config"):
        _copy_tree(STAGE / sub, resources / sub, label=f"resources/{sub}")

    write_manifest(out_root)
    print(f"\n✓ 组装完成（直拼）：{app_out.relative_to(PROJECT_ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="组装免安装交付目录")
    ap.add_argument("--stage-only", action="store_true", help="只组装，不调 electron-builder")
    ap.add_argument("--keep-stage", action="store_true", help="不清理已有 stage 目录")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="打包输出目录（相对项目根；默认 dist）。指向一个还不存在的目录可绕开删除",
    )
    ap.add_argument(
        "--direct", action="store_true",
        help="不用 electron-builder，自己拼出免安装目录（只复制、不删除；见 assemble_direct）",
    )
    args = ap.parse_args()

    if STAGE.exists() and not args.keep_stage:
        # ⚠️ 整目录清空是**必须**的：`stage_runtime()` 会把两个来源合并进
        #    `python/`，残留的旧文件不会被覆盖掉（实测会把上次的 site-packages
        #    和新基座混在一起）。
        #
        # ⚠️ 但 WorkBuddy 环境里有一道**批量删除闸门**（单轮删除超过阈值即拦），
        #    对 4600 个文件的 stage 会抛异常，把打包流程**静默**打断
        #    （日志停在"清理 …"就没下文）。
        #    这里显式接住它并给出可执行的下一步，而不是让它悄悄死掉。
        print(f"清理 {STAGE.relative_to(PROJECT_ROOT)} …")
        try:
            shutil.rmtree(STAGE)
        except BaseException as exc:  # noqa: BLE001 - 闸门会抛 SystemExit
            print(
                f"  ✗ 整目录清理被拦下（{type(exc).__name__}）。\n"
                f"    在 WorkBuddy 环境里请先手动清空：{STAGE}\n"
                "    （PowerShell 的 Remove-Item 可能报错但实际已删，用 Test-Path 复查）\n"
                "    然后加 --keep-stage 重跑；**不要**跳过清理直接打包。"
            )
            return 2

    print("组装运行时与代码 …")
    stage_runtime()
    stage_app()
    stage_tools()
    write_manifest(mode="electron-builder" if not args.direct else "direct")

    if args.stage_only:
        print("\n✓ 组装完成（未打包）")
        return 0

    out_dir = (PROJECT_ROOT / args.out_dir).resolve() if args.out_dir else DIST

    if args.direct:
        rc = assemble_direct(out_dir)
        if rc != 0:
            print("\n✗ 直拼组装失败")
        return rc

    rc = run_builder(out_dir if args.out_dir else None)
    if rc != 0:
        print("\n✗ electron-builder 失败")
        return rc
    print(f"\n✓ 打包完成，产物见 {out_dir.relative_to(PROJECT_ROOT)}/win-unpacked/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
