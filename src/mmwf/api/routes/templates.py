"""模板端点（F7.2，T1-16）：列表 / 新建 / 修改。

模板分两类来源（`config/templates/*.yaml` 出厂 + `template` 表自建），
**内置模板每次读盘、不缓存** —— 这是"改一行 yaml 即生效"的技术保障。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Path as PathParam

from mmwf.api import envelope as env
from mmwf.domain.errors import MmwfError
from mmwf.domain.template import SOURCE_DB

from ._context import Runtime
from ._support import DEFAULT_OPERATOR


def register(router: APIRouter, rt: Runtime) -> None:
    """注册模板端点。"""

    @router.get("/templates")
    def list_templates(
        meeting_kind: str | None = None,
        include_disabled: bool = False,
    ):
        """模板列表（F7.2）。

        **两类来源合并**（施工卡裁决 D1）：

          · `source="builtin"` —— `config/templates/*.yaml`，出厂模板；
          · `source="db"` —— `template` 表，用户自建。

        ⚠️ **内置模板每次读盘，不缓存**。这是 N5.2
        （"改一行 yaml 即生效，不改代码"）的技术保障。
        曾经考虑过缓存（模板是低频资源），但缓存会让**热改失效**，
        而"我改了配置没反应"是最难排查的一类问题。
        4 个 yaml 几百字节，每次读的开销远小于一个排查工单。
        """
        store = rt.template_store()
        try:
            result, problems = store.list_templates(
                meeting_kind=meeting_kind,
                include_disabled=bool(include_disabled),
            )
        except MmwfError:
            raise
        except Exception as exc:  # noqa: BLE001 — 读盘/解析异常统一翻译
            raise MmwfError(
                code="E1001",
                detail=f"读取模板失败：{exc!r}",
                user_message="模板读取失败。请检查 config/templates 目录下的文件是否正常。",
                context={"area": "template"},
            ) from exc

        data = result.to_dict()
        # 单个 yaml 坏了**不该让整个列表 500** —— 其余的还是好的，
        # 用户该看到它们。坏的那个进 warnings（施工卡 §2.2）。
        if problems:
            data["warning"] = "；".join(problems)

        if result.total == 0:
            msg = "还没有任何模板。内置模板应来自 config/templates 目录，请检查该目录。"
        else:
            msg = (
                f"共 {result.total} 个模板"
                f"（内置 {result.builtin} 个，自建 {result.db} 个）。"
            )
        if problems:
            msg += f" 有 {len(problems)} 个模板文件没能读取，详见 warning。"
        return env.ok(data, msg)

    @router.post("/templates")
    def create_template(payload: dict[str, Any] = Body(default={})):
        """新建模板（F7.2）。

        `template_id` 可省略 —— 省略时由服务端生成
        （`tpl-custom-<8位hex>`）。

        ⚠️ **与内置模板同名不报错**，但该条会被内置屏蔽（列表里看不到）。
        为什么允许：yaml 是**可热改的外部文件**。用"表里有没有"来决定
        允不允许建，会出现"现在能建、明天 yaml 加了个同名的就建不了"
        这种随外部文件波动而变的行为。但**必须告警**，不能静默。
        """
        store = rt.template_store()
        try:
            tpl = _parse_template_body(payload, source=SOURCE_DB)
            tid = str(payload.get("template_id") or "").strip()
            if not tid:
                tid = store.new_template_id()
            created = store.create(tpl, template_id=tid)
        except MmwfError:
            raise

        _audit_template_change(
            db_path=rt.db_path,
            action="create",
            template_id=created.template_id,
            name=created.name,
            operator=_operator_of(payload),
        )
        # 与内置同名时不报错、但**必须告知**（否则用户会看到"创建成功"
        # 却在列表里找不到，以为系统丢数据）。
        warning = None
        if store.get(created.template_id) is not None and store.get(
            created.template_id
        ).is_builtin:  # type: ignore[union-attr]
            warning = (
                f"新模板 {created.template_id} 与内置模板同名，"
                "列表中以内置为准，该条不会显示。请换一个 template_id。"
            )
        data = {"template": created.to_dict()}
        if warning:
            data["warning"] = warning
        return env.ok(data, f"模板「{created.name}」已创建（{created.template_id}）。")

    @router.put("/templates/{template_id}")
    def update_template(
        template_id: str = PathParam(...), payload: dict[str, Any] = Body(default={})
    ):
        """修改模板（F7.2）。

        **两条硬规则**（施工卡 §2.3）：

          ① ★ **内置模板不可改** → **E1003**（不是 E1001）。
             E1001 = "你填的东西不合格式"；E1003 = "这个对象不允许这样操作"。
             用户填对了但被规则拒绝 —— 属于后者，前端的提示也不同。
          ② ★ **`version_no` 由服务端自增**，请求体里传的一律忽略。
             F7.2 要求「修改后 version 递增，历史版本可查」；
             让客户端传 = 允许它把版本号改回去或跳号。
        """
        store = rt.template_store()
        try:
            tpl = _parse_template_body(payload, source=SOURCE_DB)
            updated = store.update(tpl, template_id=template_id)
        except MmwfError:
            raise

        _audit_template_change(
            db_path=rt.db_path,
            action="update",
            template_id=updated.template_id,
            name=updated.name,
            operator=_operator_of(payload),
            version=updated.version_no,
        )
        return env.ok(
            {"template": updated.to_dict()},
            f"模板「{updated.name}」已更新到第 {updated.version_no} 版。",
        )

def _operator_of(payload: dict[str, Any]) -> str:
    """从请求体里取操作员。取不到就用 `DEFAULT_OPERATOR`。

    ⚠️ 为什么操作员从**请求体**取而不是从令牌取：本系统的令牌是
    "本机应用启动令牌"（`deps.verify_token`），它证明"是本机这个软件
    在调"，**不区分操作人**。操作人是谁由界面传
    （F8.2「按人检索」的前提）。
    """
    raw = payload.get("operator")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_OPERATOR

def _parse_template_body(payload: dict[str, Any], *, source: str) -> Any:
    """请求体 → `MinutesTemplate`。**把解析层的错翻译成 E1001**。

    ⚠️ 这一步看着多余（`parse_template` 已抛 `TemplateFormatError`，
    它本来就是 E1001、是 `MmwfError` 子类），但它挡的是**别的异常**：
    结构怪异的请求体可能触发 `TypeError`（比如
    `required_elements=[1,2,3]`）—— 那些会变成 **E9001**。

    与 T1-14 的坑 9 同源：**用户填错必须是 E1001，不能是 E9001**。
    """
    from mmwf.domain.template import parse_template

    try:
        return parse_template(payload, source=source)
    except MmwfError:
        raise
    except Exception as exc:  # noqa: BLE001 — 兜住一切非 MmwfError
        raise MmwfError(
            code="E1001",
            detail=f"模板请求体解析失败：{exc!r}",
            user_message="模板内容有问题，请检查后重试。",
            context={"area": "template"},
        ) from exc

def _audit_template_change(
    *,
    db_path: Any,
    action: str,
    template_id: str,
    name: str,
    operator: str,
    version: int | None = None,
) -> None:
    """写 `template_change` 审计（F8.2 / AUD-06「术语库/模板变更」）。

    ⚠️ **动作名必须是 `ACTIONS` 里登记过的**。`AuditLogger.log()` 对
    未登记的动作**抛 `AuditError`**（T1-14 实测踩过）。
    `template_change` 已在 `infra/audit.ACTIONS` 的"配置变更"组里。

    ⚠️ **审计失败不该让业务失败**（模板已经建好了，因为写审计失败而
    回滚是更坏的选择）—— 吞噬异常并打 warning。
    """
    try:
        from mmwf.infra.audit import AuditLogger

        audit = AuditLogger(db_path)
        detail: dict[str, Any] = {"name": name, "op": action}
        if version is not None:
            detail["version_no"] = version
        audit.log(
            operator=operator,
            action="template_change",
            target_type="template",
            target_id=template_id,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — 审计失败不阻断业务
        logging.getLogger(__name__).warning(
            "模板变更审计写入失败（业务已成功）：%s", exc
        )
