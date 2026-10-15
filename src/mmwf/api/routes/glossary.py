"""术语库端点（F7.1，T1-14 B 档）：列表 / 新增 / 更新 / 删除 / 导入 / 导出。

术语库是**全局资产，不绑会议** —— 与 glossary 的既有设计一致。

⚠️ 写操作**必须**把 `operator` 传进 `GlossaryService` 并注入
`AuditLogger`：`glossary_change` 是"术语库/模板变更"这类最小核查项的
**唯一动作**，不留痕 = AUD-08 缺项，那是红线。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Path as PathParam

from mmwf.api import envelope as env
from mmwf.domain.errors import FileMissingError, MmwfError
# ⚠️ `GlossaryFormatError` 必须**显式**在路由层接住并翻译成 E1001。
# 它不是 `MmwfError` 的子类，掉进兜底处理器会变成 E9001（内部错误），
# 把"用户填错类别"误报成"系统故障"。详见 `_build_entry`。
from mmwf.domain.glossary import GlossaryFormatError

from ._context import Runtime
from ._support import DEFAULT_OPERATOR, _make_tmp_dir


def register(router: APIRouter, rt: Runtime) -> None:
    """注册术语库端点。"""

    # -- ⑪ 术语库（T1-14 B 档）-----------------------------------------

    @router.get("/glossary")
    def list_glossary(enabled_only: bool = False):
        """列出术语（F7.1）。

        `conflicts` 一并带出：同一别名指向多个规范写法是**实际会遇到**的
        录入错误，而它只有在"回改时按先出现的那条替换"时才会暴露 ——
        与其等用户发现替换结果不对，不如在列表里就提示。
        """
        svc = rt.glossary_service()
        entries = svc.list_entries(enabled_only=enabled_only)
        return env.ok(
            {
                "entries": [e.to_dict() for e in entries],
                "count": len(entries),
                "conflicts": svc.conflicts(),
            }
        )

    @router.post("/glossary")
    def create_glossary(payload: dict[str, Any] = Body(...)):
        """新增（或按 `(category, canonical)` 覆盖）一条术语（F7.1）。

        ⚠️ **`entry_id` 由服务端确定性生成**（T1-14 裁决 D5）：
        `f"{category}:{canonical}"`。

        为什么不能用随机 id：表上有 `UNIQUE (category, canonical)`，
        而 `entry_id` 是主键。随机 id 意味着重复 POST 同一条术语会
        **撞 UNIQUE 约束抛 IntegrityError**（而不是优雅 upsert），
        并且会制造出"同一术语两条记录"的脏数据 —— 回改时到底用哪条？
        """
        svc = rt.glossary_service(operator=payload.get("operator") or DEFAULT_OPERATOR)
        entry = _build_entry(payload)
        action = svc.upsert(entry)
        return env.ok(
            {"entry": entry.to_dict(), "action": action},
            "术语已新增" if action == "created" else "术语已更新",
        )

    @router.put("/glossary/{entry_id}")
    def update_glossary(entry_id: str = PathParam(...), payload: dict[str, Any] = Body(...)):
        """更新一条术语（F7.1）。

        ⚠️ **`entry_id` 以路径为准**，body 里的 `entry_id` 忽略 ——
        避免"路径说 A、body 说 B"的歧义（T1-14 裁决 D5）。

        `enabled` 单独走 `set_enabled()`（而不是 upsert）：
        停用是**状态切换**，不该要求调用方把整条术语重传一遍 ——
        重传就有"传漏了某个字段导致内容被清空"的风险。
        """
        svc = rt.glossary_service(operator=DEFAULT_OPERATOR)
        existing = svc.repo.get(entry_id)
        if existing is None:
            raise _glossary_missing(entry_id)

        # 只切换启用状态：body 里除 operator 外没有其它字段
        if set(payload) <= {"operator", "enabled"} and "enabled" in payload:
            ok_ = svc.set_enabled(entry_id, bool(payload["enabled"]))
            fresh = svc.repo.get(entry_id)
            return env.ok(
                {"entry": fresh.to_dict() if fresh else None,
                 "action": "enable" if payload["enabled"] else "disable"},
                "术语已启用" if payload["enabled"] else "术语已停用",
            )

        merged = dict(existing.to_dict())
        merged.update({k: v for k, v in payload.items() if k != "operator"})
        merged["entry_id"] = entry_id
        entry = _build_entry(merged)
        action = svc.upsert(entry)
        return env.ok(
            {"entry": entry.to_dict(), "action": action},
            "术语已更新",
        )

    @router.delete("/glossary/{entry_id}")
    def delete_glossary(entry_id: str = PathParam(...), operator: str = DEFAULT_OPERATOR):
        """删除一条术语（F7.1）。

        删除前 `GlossaryService.delete()` 会先把内容写进审计 ——
        删了之后就再也查不到"删的是什么"了。
        """
        svc = rt.glossary_service(operator=operator)
        found = svc.delete(entry_id)
        if not found:
            raise _glossary_missing(entry_id)
        return env.ok({"removed": True, "entry_id": entry_id}, "术语已删除")

    @router.post("/glossary/import")
    def import_glossary(payload: dict[str, Any] = Body(...)):
        """批量导入术语（F7.1）。

        **支持两条路**（T1-14 裁决 D4）：
          · `{"path": "D:/术语.csv"}`  —— 前端已用系统对话框选好本机文件
          · `{"csv_text": "category,canonical\\n..."}` —— 内容已在内存

        为什么**不用 multipart 上传**：Electron 是**本机程序**，
        文件已经在同一台机器的磁盘上。让前端把整份 CSV 塞进 multipart
        再 POST 到本机另一个端口，是纯粹的无谓拷贝；而且会引入
        `python-multipart` 依赖 —— 每多一个依赖就多一份离线打包
        与保密审查的负担。

        ⚠️ **`csv_text` 要落受控临时文件**：`import_csv()` 收的是路径。
        临时文件落 `data_dir/.tmp`（不是系统 `/tmp`）——
        术语库可能含单位专有名词，与 `filestore.plaintext()` 的既有约定一致。
        `finally` 里必删。

        ⚠️ **`problems` 必须原样带出**：`ImportReport.problems` 是
        格式问题清单。"导入成功但有 3 行被跳过"是最容易被忽略的情况 ——
        只报 `created: 7` 而不报 `problems: 3`，用户会以为全导进去了。

        ⚠️⚠️ **文件级失败要翻成 E1xxx，不能让它冒成 E9001**（T1-14 补）：
        `read_csv()` 在两种情况下会**抛异常而不是记 problems** ——
        CSV 为空/没有表头（`GlossaryFormatError`）、路径不存在
        （`FileNotFoundError`）。两者都不是 `MmwfError`，
        掉进兜底处理器就变成 E9001"系统内部错误"。
        但"你选的文件是空的"对用户来说是个**可自行修正的输入问题**，
        必须给 E1001；文件不见了给 E1002。

        ⚠️ 包装时**不要**用同类名去接 —— `FileMissingError(detail, **ctx)`
        会把多余 kwarg 静默吞进 context（详见 `_glossary_missing` 的说明），
        所以这里统一构造基类 `MmwfError`。
        """
        svc = rt.glossary_service(operator=payload.get("operator") or DEFAULT_OPERATOR)

        path = payload.get("path")
        csv_text = payload.get("csv_text")
        if not path and csv_text is None:
            raise MmwfError(code="E1001", detail="必须提供 path 或 csv_text 之一")

        tmp_dir: Path | None = None
        try:
            if path:
                target = Path(str(path))
                if not target.is_file():
                    raise FileMissingError(f"术语库文件不存在：{target}")
                report = svc.import_file(target)
            else:
                tmp_dir = _make_tmp_dir(rt.cfg)
                fname = str(payload.get("filename") or "import.csv")
                # 只取文件名，防目录穿越（前端可能传 "../../x.csv"）
                safe_name = Path(fname).name or "import.csv"
                target = tmp_dir / safe_name
                target.write_text(str(csv_text), encoding="utf-8")
                report = svc.import_file(target)
        except GlossaryFormatError as exc:
            raise MmwfError(
                code="E1001",
                detail=f"术语库 CSV 无法解析：{exc}",
                user_message="这个 CSV 读不出内容。请确认第一行是表头（category,canonical,aliases,note），并且文件不是空的。",
                context={"area": "glossary"},
            ) from exc
        except FileNotFoundError as exc:
            raise MmwfError(
                code="E1002",
                detail=f"术语库文件不存在：{exc}",
                user_message="找不到要导入的术语库文件。请重新选择文件后重试。",
                context={"area": "glossary"},
            ) from exc
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        data = report.to_dict()
        if data.get("problems"):
            return env.ok(
                data,
                f"导入完成，但有 {len(data['problems'])} 处格式问题被跳过，请核对",
            )
        return env.ok(data, f"已导入 {data.get('created', 0) + data.get('updated', 0)} 条术语")

    @router.get("/glossary/export")
    def export_glossary():
        """导出术语库为 CSV 文本（F7.1，备份/交付用）。

        同样**不落文件到未知位置**：`export_file()` 收路径，
        这里落受控临时文件 → 读回文本 → 删。
        响应体给 `csv` 文本，前端自己决定存哪。
        """
        svc = rt.glossary_service()
        tmp_dir: Path | None = None
        try:
            tmp_dir = _make_tmp_dir(rt.cfg)
            target = tmp_dir / "glossary-export.csv"
            svc.export_file(target)
            text = target.read_text(encoding="utf-8-sig")
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return env.ok({"csv": text, "count": svc.repo.count()}, "术语库已导出")

def _glossary_missing(entry_id: str) -> MmwfError:
    """构造"术语条目不存在"的业务异常（T1-14）。

    为什么复用 `FileMissingError`（E1002）而**不合新错误码**：
    SAD §9.3 的码表里 E1xxx 这一族是"输入与文件"，
    "要操作的条目不存在"确实属于这一类；新增错误码要走附录 D
    的变更流程（登记 + 评审），为了一个 404 类场景不值当。

    ⚠️ 但 E1002 的**登记文案**是"找不到要处理的文件。请确认文件没有
    被移动或删除。" —— 对术语库来说是错的（用户会去找文件）。
    所以这里**显式传 `user_message` 覆盖它**，
    这是 `MmwfError` 设计好的用法（见 errors.py 的类文档：
    要给出更贴切的说法就必须在调用处显式拼一句）。

    ⚠️⚠️ **不能用 `FileMissingError(detail, user_message=...)`**：
    那个子类的签名是 `__init__(self, detail="", **ctx)` ——
    多余的 kwarg 会被 `**ctx` **静默收进 `context`**，
    根本到不了 `user_message` 字段，于是覆盖失效、用户仍看到文件文案。
    （第一版就是这么写的，smoke test 抓到了。）
    正确做法是直接构造基类 `MmwfError(code="E1002", ...)`。
    """
    return MmwfError(
        code="E1002",
        detail=f"术语条目 {entry_id!r} 不存在",
        user_message="这条术语已经不存在了，可能是别的窗口删掉了它。请刷新术语库后重试。",
        context={"entry_id": entry_id, "area": "glossary"},
    )

def _build_entry(payload: dict[str, Any]) -> Any:
    """把请求 body 转成 `GlossaryEntry`（T1-14 B 档）。

    `entry_id` 的生成规则（T1-14 裁决 D5）：
      · 已给 → 原样用（`PUT` 走这条，路径参数优先）
      · 未给 → `f"{category}:{canonical}"` **确定性生成**

    为什么确定性而不是随机：表上有 `UNIQUE (category, canonical)`，
    随机 id 会让"重复 POST 同一条术语"撞 UNIQUE 抛 IntegrityError，
    而不是优雅 upsert —— 并且会造出同一术语两条记录的脏数据。

    ⚠️ `canonical` 可能含 `:`（如"关于：XX 事项"），所以拼出的 id
    **只当不透明主键用，绝不反向解析**。所有查询都从库里按 id 取，
    没有一处靠拆 id 得到 category/canonical。

    `category` 与 `canonical` 的取值合法性由 `GlossaryEntry.__post_init__`
    把关（它会抛 `GlossaryFormatError`），这里不重复校验 ——
    一处规则一个出处。

    ⚠️⚠️ **但"不重复校验" ≠ "不翻译异常"**（T1-14 补，第一版就漏了）。
    `GlossaryFormatError` **不是 `MmwfError` 的子类**，所以
    `@app.exception_handler(MmwfError)` 兜不住它，它会掉进
    `@app.exception_handler(Exception)` 那个"兜底"分支 ——
    结果是**响应虽然还是 200 + 信封，但错误码变成 E9001（内部错误）**。
    用户输错一个类别，看到的却是"系统内部错误，请联系管理员"，
    而且真的会产生一条 E9001 日志、污染"内部错误"这个信号（SAD §9.3
    要求 E9xxx 只用于真正的未预期故障）。
    所以在这里显式接住它、翻成 E1001。
    """
    from mmwf.domain.glossary import GlossaryEntry, parse_aliases

    category = str(payload.get("category") or "").strip()
    canonical = str(payload.get("canonical") or "").strip()
    if not category or not canonical:
        raise MmwfError(code="E1001", detail="category 与 canonical 均为必填")

    entry_id = str(payload.get("entry_id") or "").strip()
    if not entry_id:
        entry_id = f"{category}:{canonical}"

    # ⚠️⚠️ `parse_aliases()` **必须在 `try` 里面**（T1-14 验收抓到）。
    # 第一版把它写在 `try` 外，只把 `GlossaryEntry(...)` 构造包进去 ——
    # 于是"类别非法"被正确翻成 E1001，而"aliases 传了个数字"
    # （`parse_aliases(12345)` → `GlossaryFormatError`）**从 try 外冒出去**，
    # 又掉进兜底处理器变成 E9001。
    # 验收脚本一报就是这个现象：同一类错误、同一份 `_build_entry`，
    # 一个报 E1001、一个报 E9001 —— **两个都抛 GlossaryFormatError**，
    # 差别只在"抛在 try 内还是 try 外"。这类 bug 极难靠阅读发现，
    # 因为两个分支看起来一模一样。
    try:
        aliases = parse_aliases(payload.get("aliases"))
        return GlossaryEntry(
            entry_id=entry_id,
            category=category,
            canonical=canonical,
            aliases=aliases,
            note=str(payload.get("note") or ""),
            enabled=bool(payload.get("enabled", True)),
        )
    except GlossaryFormatError as exc:
        # `detail` 保留原始信息（含合法类别清单，便于排查与验收脚本断言）；
        # `user_message` 换成面向行政人员的中文说明 —— 用户不需要知道
        # 领域层的类名，只需要知道"哪一项填错了、能填什么"。
        raise MmwfError(
            code="E1001",
            detail=f"术语格式不合法：{exc}",
            user_message="这条术语的类别或别名写法不正确。类别只能选「单位 / 人名 / 术语 / 代号」，别名用竖线分隔。",
            context={"category": category, "area": "glossary"},
        ) from exc
