"""导出端点（F5.1 / F5.8 / F4.3，T1-16）：生成 / 撤销 / 差异对比 / 文件下载。

⚠️⚠️ **注册顺序是本模块的硬性约束**，改动前务必先读 `register` 里的说明：

`GET /meetings/{id}/export/undo`（占位）必须声明在
`GET /meetings/{id}/export/{file}` 之前。两者**不在同一个方法空间**，
顺序错了不会报"路由不匹配"，而是让 `GET .../export/undo` 落到
`{file}` 上、拿着 `file="undo"` 去找一个不存在的文件，返回一条
**伪装成正常业务的 E1002** —— 用户会去导出记录里翻半天找一个叫
undo 的文件，而真因只是"这个地址只能 POST"。

`undo_export`（真正的 POST 撤销）与 `export_meeting` 也同在本模块，
为的是让"导出—撤销—下载"这一组动作看得见全貌。
"""

from __future__ import annotations

import logging
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Path as PathParam, Request
from fastapi.responses import JSONResponse, Response

from mmwf.api import envelope as env
from mmwf.domain.errors import FileMissingError, MmwfError

from ._context import Runtime
from ._support import DEFAULT_OPERATOR, _media_type_for, _require_meeting

logger = logging.getLogger(__name__)


def register(router: APIRouter, rt: Runtime) -> None:
    """注册导出端点。**顺序敏感**，见模块 docstring。"""

    # -- ⑦ 导出 --------------------------------------------------------

    @router.post("/meetings/{meeting_id}/export")
    def export_meeting(meeting_id: str = PathParam(...), payload: dict[str, Any] = Body(default={})):
        """导出 docx（F5.1 / F5.8，幂等）。

        幂等由 `ExportService.export_docx` 内部保证（C07）；
        重复调用返回同一路径 + `reused=True`，前端不用去重。
        """
        _require_meeting(meeting_id, rt.db_path)
        # ⚠️ **不要**在这里 try/except 转信封（T1-11 Review 改）。
        # 第一版写的是：
        #     except Exception as exc:
        #         if isinstance(exc, MmwfError):
        #             return env.from_error(exc, trace_id=None)
        # 三个问题：
        #   ① `trace_id=None` —— 把已生成的追踪号丢掉了，
        #      而这恰恰是用户唯一能报给运维的信息；
        #   ② 重复了 `main.py` 的全局处理器（同一件事两处实现，必漂移）；
        #   ③ `except Exception` + 单分支 if，看着像"都处理了"，
        #      实际非 MmwfError 只是 re-raise —— 白写一层。
        # 全局处理器（`@app.exception_handler(MmwfError)`）已经覆盖
        # 所有业务异常并附带 trace_id，端点里**只管业务**就好。
        from mmwf.app.services.minutes import http_export
        result = http_export(rt, meeting_id, payload)
        data = result.to_dict()
        message = "文件已生成" if not result.reused else "文件已存在，未重复生成"
        return env.ok(data, message)

    @router.post("/meetings/{meeting_id}/export/undo")
    def undo_export(meeting_id: str = PathParam(...), payload: dict[str, Any] = Body(default={})):
        """撤销导出（F5.8）。

        删除选中产物与 manifest 条目；PDF 同时移除来源和归档登记。
        """
        _require_meeting(meeting_id, rt.db_path)
        rel_path = (payload.get("rel_path") or "").strip()
        if not rel_path:
            raise MmwfError(code="E1001", detail="缺少 rel_path")
        svc = rt.export_service(meeting_id)
        removed = svc.undo_export(rel_path)
        if not removed:
            raise FileMissingError(f"manifest 中没有 {rel_path}")
        return env.ok({"removed": True, "rel_path": rel_path}, "已撤销导出")

    # -----------------------------------------------------------------------
    # T1-16：模板 3 端点 + 差异对比 + 导出文件下载（25 → 30）
    # -----------------------------------------------------------------------
    #
    # ⚠️⚠️ **声明顺序**：`GET /meetings/{id}/export/{file}` 必须放在
    # `POST /meetings/{id}/export/undo` **之后**。FastAPI 按声明顺序匹配，
    # `{file}` 是路径参数、`undo` 是字面量 —— 顺序反了会让
    # `GET .../export/undo` 命中 `{file}`，报出一个**看起来正常的
    # "文件不存在"**（E1002），而不是路由不匹配。详见施工卡坑 14。
    #
    # ★ 但"放在后面"**并不够**：`undo` 那条路由只声明了 `POST`，
    # 而 `{file}` 是 `GET` —— 两者**不在同一个方法空间里**，
    # 所以 `GET .../export/undo` 根本不会撞上 undo 路由，只会一路
    # 落到 `{file}` 上，然后拿着 `file="undo"` 去找一个不存在的文件，
    # 返回一条**伪装成正常业务的 E1002**。
    #
    # 这是本卡验收时被 `test_export_undo_route_not_shadowed` 抓出来的真问题
    # （不是我预设的"顺序"，而是"方法空间不重叠"）。修法：**显式补一条
    # `GET .../export/undo`，声明在 `{file}` 之前**，把方法用错这件事
    # 如实报出来，而不是编一个"文件不存在"糊过去。
    #
    # ★★ 还有一个**更隐蔽**的地方：这条占位路由**不能**用
    # `raise MmwfError(http_status=405)` 去改状态码 ——
    # `MmwfError` 的处理器（`main.py` 里 `@app.exception_handler(MmwfError)`）
    # 是**单一实例**注册的，它把 `status_code` **写死成 200**。
    # 就算我给异常加个 `http_status` 字段，也没有任何地方会去读它。
    # 于是"改状态码"这件事会**静默失败**：代码看着对，响应还是 200，
    # 断言一改成 `in {404,405}` 就红 —— 而红的地方离真因很远。
    #
    # 更要紧的是：**信封约定就是"一切响应都 200 + `ok/code`"**，
    # 这是 SAD 定的、前端按它解析的。为了一条占位路由去破例开一个
    # "非 200 的业务响应"口子，是拿系统级不变量换一个端点级的小便利。
    # 所以这里**老老实实回 200 + E1001**，理由写在 `user_message` 里；
    # 验收断言也**不查状态码**，查的是"**别拿到文件字节流**、别报错成
    # E1002"。判据要落在**这件事的性质**上，而不是一个我顺手挑的数字上。
    #
    # 为什么要这么较真：`undo` 是个**字面量**路径段，用户完全可能
    # 手敲 URL 或在地址栏里改路径。如果这里回 E1002，用户会去"导出记录"
    # 里翻半天找那个叫 undo 的文件 —— 而真正的原因是"这个地址只能 POST"。
    # 报错必须能指向真因。

    @router.get("/meetings/{meeting_id}/export/undo")
    def undo_export_method_guard(meeting_id: str = PathParam(...)):
        """★★ 只为**占位**，防止 `GET .../export/undo` 被 `{file}` 吃掉。

        不是功能端点。真正的撤销在 `POST`（见上面 `undo_export`）。
        这里**必须**声明在 `GET .../export/{file}` 之前。

        ⚠️ 注意 `PathParam(...)` 这里是必须的：FastAPI 靠签名里的
        `PathParam` 标记把 `{meeting_id}` 认成路径参数。少了它，
        `meeting_id` 会被当成查询参数，路由路径本身就不匹配了 ——
        那就等于这条占位路由**不存在**，坑照旧。
        """
        _ = meeting_id  # 只为让 FastAPI 把 `{meeting_id}` 认成路径参数
        raise MmwfError(
            code="E1001",
            detail="GET 不支持撤销导出（应为 POST）",
            user_message=(
                "撤销导出要在导出记录里点「撤销」按钮，不能直接在地址栏打开这个地址。"
            ),
            context={"method": "GET", "allow": "POST", "path": "export/undo"},
        )

    @router.get("/meetings/{meeting_id}/diff")
    def meeting_diff(
        meeting_id: str = PathParam(...),
        base: int | None = None,
        compare: int | None = None,
    ):
        """两版记录稿的**段落级**差异（F4.3）。

        **输入取自哪里**（施工卡裁决 D2）：磁盘 `record/v{N}.md`，**不是**
        `minutes_version` 表。

        ⚠️ T1-17 缺陷 25：那张表**已明确弃用**（一期不写不读）。弃用理由与
        启用时点（**二期 T2-04**）记在 `infra/migrations/001_init.sql`
        该表上方的长注释里，此处不重复。本端点用文件、不用表，
        是**一期既定事实**，T1-17 已确认不改。

        **只有 1 版时的语义**（裁决 D4）：`compare` 存在、`base` 不存在
        → **200 + 空 segments + 说明**，**不是 E1002**。
        "还没有第二版"不是错误。
        """
        _require_meeting(meeting_id, rt.db_path)
        svc = rt.export_service(meeting_id)
        try:
            latest = svc.latest_record_version()
        except Exception as exc:  # noqa: BLE001 — 记录稿尚无/读不到
            raise MmwfError(
                code="E1002",
                detail=f"meeting_id={meeting_id!r} 没有记录稿：{exc}",
                user_message=(
                    "这次会议还没有生成记录稿，暂时没有可对比的内容。"
                    "请先运行处理流程生成记录稿。"
                ),
                context={"area": "diff", "meeting_id": meeting_id},
            ) from exc

        cmp_v = int(compare) if compare is not None else int(latest)
        if cmp_v < 1:
            raise MmwfError(
                code="E1001",
                detail=f"compare 必须 >= 1，实际 {cmp_v}",
                user_message="要对比的版本号必须是 1 或更大。",
                context={"area": "diff", "meeting_id": meeting_id},
            )
        base_v = int(base) if base is not None else cmp_v - 1

        def _read(version: int) -> str | None:
            try:
                return svc.read_record(version)
            except Exception:  # noqa: BLE001 — 版本不存在时 read_record 抛
                return None

        compare_text = _read(cmp_v)
        if compare_text is None:
            raise MmwfError(
                code="E1002",
                detail=f"记录稿版本 v{cmp_v} 不存在（最新是 v{latest}）",
                user_message=(
                    f"没有第 {cmp_v} 版记录稿。这次会议最新的记录稿是第 {latest} 版，"
                    "请确认版本号。"
                ),
                context={"area": "diff", "meeting_id": meeting_id, "version": cmp_v},
            )

        base_text = _read(base_v) if base_v >= 1 else None

        from mmwf.domain.minutes.validator import diff_versions, summarize_blocks

        if base_text is None:
            blocks: list[dict[str, Any]] = []
            summary = summarize_blocks(blocks)
        else:
            blocks = diff_versions(base_text, compare_text)
            summary = summarize_blocks(blocks)

        data: dict[str, Any] = {
            "meeting_id": meeting_id,
            "base_version": base_v,
            "compare_version": cmp_v,
            "segments": blocks,
            "summary": summary,
        }
        if base_text is None:
            data["comparable"] = False
        else:
            data["comparable"] = True

        if base_text is None:
            msg = f"这是第 {cmp_v} 版，没有可对比的上一版。"
        elif not blocks:
            msg = f"第 {base_v} 版与第 {cmp_v} 版**完全相同**（没有段落差异）。"
        else:
            changed = summary["changed"] + summary["removed"] + summary["added"]
            msg = (
                f"第 {base_v} 版 → 第 {cmp_v} 版："
                f"{changed} 处变化（改 {summary['changed']}、删 {summary['removed']}、"
                f"增 {summary['added']}），未变 {summary['unchanged']} 段。"
            )
        return env.ok(data, msg)

    @router.get("/meetings/{meeting_id}/export/{file:path}")
    def download_export(meeting_id: str = PathParam(...), file: str = PathParam(...),
                        request: Request = None):
        # 校验和读取覆盖同一会议锁，避免撤销/重导出插入二者之间。
        try:
            with rt.meeting_lock(meeting_id):
                return _download_export(meeting_id, file)
        except MmwfError as exc:
            # 下载路径语法错误在 HTTP 层明确拒绝；仍保留既有错误信封与编号。
            # 直接调用端点的判据测试继续接收原异常。
            if exc.code != "E1001" or request is None:
                raise
            return JSONResponse(env.from_error(exc, trace_id=getattr(request.state, "trace_id", None)),
                                status_code=400)

    def _download_export(meeting_id: str, file: str):
        """下载导出文件（F5.1）。

        ★★★ **本端点是本卡唯一的安全红线** —— `file` 必须防路径穿越。

        只做黑名单（`".." in file`）**挡不住**这些：

            `..%2f..%2fkeystore/master.key`   FastAPI 解码后才含 `..`
            `....//x`                          不含连续两点
            `..\\\\x`                            Windows 反斜杠

        接受文件名或 API 返回的 `export/文件名` POSIX 相对引用。
        区名固定为 export，剥去这个前缀后仍必须是单个文件名；
        禁止嵌套目录、盘符、UNC、编码分隔符和目录穿越。
        """
        _require_meeting(meeting_id, rt.db_path)

        raw = str(file or "")
        if raw.startswith("export/"):
            raw = raw[len("export/"):]
        # ★ 白名单：文件名必须等于原样输入。这就同时挡掉了
        #   绝对路径、相对路径、URL 解码后的 `..`、反斜杠、盘符。
        if not raw or raw in {".", ".."} or Path(raw).name != raw or PureWindowsPath(raw).anchor:
            raise MmwfError(
                code="E1001",
                detail=f"非法的文件名：{raw!r}（含目录成分或为空）",
                user_message="文件名不正确。请从导出记录里选取要下载的文件。",
                context={"area": "export", "meeting_id": meeting_id},
            )
        # 额外挡：Windows 上 `Path("a\\b").name` 在 POSIX 语义下是 `"a\\b"`，
        # 所以反斜杠要单独看。`os.sep` 在不同平台不同，这里两种都查。
        if "\\" in raw or "/" in raw:
            raise MmwfError(
                code="E1001",
                detail=f"文件名含路径分隔符：{raw!r}",
                user_message="文件名不正确。请从导出记录里选取要下载的文件。",
                context={"area": "export", "meeting_id": meeting_id},
            )
        # ★★ 百分号编码的分隔符也要挡：`..%2f..%2fx` 里的 `%2f`
        #    在**我们这一层**已不是 `/`，所以 `Path(raw).name == raw`
        #    成立、上面两条都放行 —— 它会变成一个"名字里带 %2f 的普通文件"。
        #    单纯从**本系统**看这无害（它在 export/ 里就是一个文件，
        #    filestore 不会把 `%2f` 当分隔符）。
        #    但**必须拒**，理由有两条：
        #      ① 契约要明确。同一个"想穿越"的意图不该有时 200 有时 400；
        #         今天无害是因为 filestore 恰好不做二次解码，
        #         哪天有人加一层解码，它就变成真穿越 —— 不给自己埋雷。
        #      ② 真实文件名里不该有 `%2f`/`%5c`/`%00`。挡掉不会误伤。
        lowered = raw.lower()
        if any(tok in lowered for tok in ("%2f", "%5c", "%00", "%2e")):
            raise MmwfError(
                code="E1001",
                detail=f"文件名含百分号编码的路径字符：{raw!r}",
                user_message="文件名不正确。请从导出记录里选取要下载的文件。",
                context={"area": "export", "meeting_id": meeting_id},
            )
        if "\x00" in raw:
            raise MmwfError(
                code="E1001",
                detail="文件名含空字节",
                user_message="文件名不正确。",
                context={"area": "export", "meeting_id": meeting_id},
            )

        from mmwf.app.services.minutes import check_download
        check_download(rt, meeting_id, raw)
        fs = rt.filestore(meeting_id)
        # ★ 区名**写死** `export`，`raw` 只当文件名 —— 见 docstring。
        rel = f"export/{raw}"
        if not fs.exists(rel):
            raise MmwfError(
                code="E1002",
                detail=f"导出文件不存在：{raw!r}（meeting_id={meeting_id!r}）",
                user_message=(
                    "找不到这个导出文件。可能是它已经被撤销或删除，"
                    "请回到导出记录重新生成。"
                ),
                context={"area": "export", "meeting_id": meeting_id, "file": raw},
            )
        try:
            payload = fs.read(rel)
        except Exception as exc:  # noqa: BLE001 — 读失败统一翻译，别漏成 E9001
            raise MmwfError(
                code="E1002",
                detail=f"读导出文件失败：{exc!r}",
                user_message="这个文件读不出来，可能已经损坏。请重新导出一份。",
                context={"area": "export", "meeting_id": meeting_id, "file": raw},
            ) from exc

        _audit_export_download(
            db_path=rt.db_path, meeting_id=meeting_id, filename=raw, size=len(payload)
        )

        media = _media_type_for(raw)
        from fastapi.responses import Response

        # ⚠️ 用 `Response` 而不是 `FileResponse`：
        #   ① 我们已经有字节（经 filestore 读的，可能解过密）；
        #   ② `FileResponse` 需要磁盘路径，而 filestore 的路径语义
        #      应当由仓储层保管，不该在端点里拼。
        # `filename*=UTF-8''` 是 RFC 5987 的写法，中文文件名靠它才不乱码。
        from urllib.parse import quote

        ascii_fallback = raw.encode("ascii", "ignore").decode("ascii") or "download"
        disposition = (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(raw)}"
        )
        return Response(
            content=payload,
            media_type=media,
            headers={"Content-Disposition": disposition},
        )

def _audit_export_download(
    *, db_path: Any, meeting_id: str, filename: str, size: int
) -> None:
    """下载导出文件的留痕。

    ⚠️ **为什么"下载"也要记**：导出文件里是**纪要正文**（敏感内容）。
    部署手册 §6.2 关心"谁在什么时候把纪要取走了"。
    只记"生成"不记"取走"，等于只知道文件存在过。

    ⚠️ 动作名 `export_download` **必须已在 `ACTIONS` 里登记**
    （没登记会抛 `AuditError`，被下面吞掉 → 静默）。
    首次集成时**实测确认它真写进去了**，别以"没报错"为准。
    """
    try:
        from mmwf.infra.audit import AuditLogger

        audit = AuditLogger(db_path)
        audit.log(
            operator=DEFAULT_OPERATOR,
            action="export_download",
            target_type="meeting",
            target_id=meeting_id,
            detail={"file": filename, "size": size},
        )
    except Exception as exc:  # noqa: BLE001 — 审计失败不阻断下载
        logging.getLogger(__name__).warning(
            "导出下载审计写入失败（业务已成功）：%s", exc
        )
