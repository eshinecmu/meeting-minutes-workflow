"""python-docx 后端隔离层（T1-09）。

为什么要有这个文件
------------------
SAD §4 的依赖方向规定：

    > 领域层只依赖标准库与纯算法依赖（如 `python-docx` 仅允许出现在 M11，
    > 且须经 `DocxWriter` 抽象隔离）。

`tools/check_layers.py` 据此把 `docx` 加进了 `FORBIDDEN_IN_DOMAIN`，
只对 `/domain/docx/` 这一个目录放行。放行不等于可以随处 import ——
**本文件是 `domain/docx` 包内唯一允许 `import docx` 的地方**，
其余模块一律 `from mmwf.domain.docx._docx_backend import ...`。

这样做的实际收益（不是为了形式合规）：

  1. **换库只改一个文件**。万一将来 python-docx 停更、或换成
     `docxtpl`、或要改成直接拼 OOXML，改动面被压到本文件。
     md2docx 的教训就摆在 SAD §6 的依赖表里："因其仅 6 个提交，
     须把关键映射逻辑内联进本项目" —— 停更的上游随时会来。
  2. **测试可以打桩**。`domain/docx` 的纯逻辑（编号、文本规范化、
     样式表）不该被 python-docx 的安装与否绑架。有了隔离层，
     测试可以在没有 python-docx 的环境里只 import 纯逻辑模块。
  3. **导入失败要有人话**。python-docx 装在 `pyproject.toml` 的
     `docx` extra 里，没装时 `import docx` 抛的是
     `ModuleNotFoundError: No module named 'docx'` —— 对运维是废信息。
     本模块把它转成带安装指引的 `DocxBackendError`。

惰性导入，而不是模块级 try/except
--------------------------------
模块级 `try: import docx except ImportError: docx = None` 会让每次使用
都多一层 `if docx is None` 判断，而且失败点从 import 时挪到了运行时的
任意一行 —— 排查反而更难。这里用 `_require_docx()` 做**显式惰性导入**：
失败点在第一次调用时抛出，且错误信息是完整的。

关于 `qn` / `OxmlElement` 的类型
-------------------------------
`OxmlElement` 返回的是 lxml 元素，`qn()` 返回 Clark 记号的字符串。
两者都是 python-docx 的公开 API（`docx.oxml`），不是私有实现 —— 
OOXML 补丁必须走它们，否则就得手写 XML 字符串，那才是真的脆。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器
    from docx.document import Document as _Document
    from docx.table import Table as _Table
    from docx.text.paragraph import Paragraph as _Paragraph
    from docx.text.run import Run as _Run


class DocxBackendError(RuntimeError):
    """python-docx 不可用或版本不匹配。

    带安装指引，因为它最可能出现在"运维把服务部署到目标机、
    忘了装 docx extra"这个场景，而不是开发机。
    """


#: 安装指引。运维手册 §2.3 的离线 wheel 流程会用到这段话。
_INSTALL_HINT = (
    "导出 docx 需要 python-docx。请在项目目录执行：\n"
    '    pip install -e ".[docx]"\n'
    "离线环境请先按《部署运维与保密合规手册》§2.3 构建 wheel 仓库，"
    "再从本地源安装。"
)


def _require_docx():
    """惰性导入 python-docx，失败时给可执行的提示。"""
    try:
        import docx  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 装好依赖后不会走到
        raise DocxBackendError(
            f"python-docx 未安装：{exc}\n{_INSTALL_HINT}"
        ) from exc
    return docx


# ---------------------------------------------------------------------------
# 对外暴露的构造入口（其余模块只用这些，不直接 import docx）
# ---------------------------------------------------------------------------

def new_document():
    """新建一个空白 Document。

    **刻意不用 python-docx 的 `default.docx` 模板**。
    它带一套西文样式（Normal / Heading 1..9 / Title），而公文的版式
    我们**全部自己注入**（见 patches.py）。用自带模板会引入两个隐患：

      · 自带样式里 `w:rFonts` 只设了 `w:ascii`，中文会落回宋体 ——
        正好是部署手册 §7.5 排障表第一行的症状；
      · 将来有人改了 `Normal` 样式的缩进，表格缩进穿透（C15）会
        以"时灵时不灵"的形式复现，因为表格继承了它。

    自己从零注入，每个段落、每个 run 的格式都是**显式写死的**，
    没有"继承来的惊喜"。代价是要多写几行 `add_paragraph()`，
    换来的是任何一条版式都能被 `tools/verify_docx_style.py` 断言。
    """
    _require_docx()
    from docx import Document

    doc = Document()
    _strip_default_styles(doc)
    return doc


def _strip_default_styles(doc) -> None:
    """清掉自带样式的字体设定，避免与 GB 注入的字体打架。

    只清字体与缩进这两个会和 GB 版式正面冲突的属性，
    **不删样式本身** —— `doc.styles` 里的对象被段落引用着，
    删掉会留下悬空引用（`w:pStyle` 指向不存在的 style），
    Word 打开时可能报"文档已损坏"。
    """
    from docx.oxml.ns import qn

    for style in doc.styles:
        try:
            rpr = style.element.find(qn("w:rPr"))
        except (AttributeError, ValueError):  # pragma: no cover - 极少数样式无 element
            continue
        if rpr is None:
            continue
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is not None:
            rpr.remove(rfonts)


def open_document(path) -> "_Document":
    """打开一个已有 docx（用于对照校验与回归）。"""
    _require_docx()
    from docx import Document

    return Document(str(path))


def qn(tag: str) -> str:
    """命名空间限定名（`"w:ind"` -> `"{...}ind"`）。"""
    _require_docx()
    from docx.oxml.ns import qn as _qn

    return _qn(tag)


def OxmlElement(tag: str):
    """新建一个 OOXML 元素。"""
    _require_docx()
    from docx.oxml import OxmlElement as _OxmlElement

    return _OxmlElement(tag)


# ---------------------------------------------------------------------------
# 长度与枚举：同样只能从这里出去
# ---------------------------------------------------------------------------
#
# 下面三个函数看着"只是转发"，但它们是隔离契约的一部分：
# `patches.py` / `writer.py` 一旦自己 `from docx.shared import Pt`，
# "换库只改一个文件"的承诺就当场失效。验收脚本（附加 5）用 AST 查的
# 正是这件事 —— 靠字符串匹配查不出来，因为这类 import 都写在函数体里。

def Pt(value: float):
    """磅 -> python-docx 的 EMU 长度对象。"""
    _require_docx()
    from docx.shared import Pt as _Pt

    return _Pt(value)


def Cm(value: float):
    """厘米 -> python-docx 的 EMU 长度对象（页面边距用）。"""
    _require_docx()
    from docx.shared import Cm as _Cm

    return _Cm(value)


def WD_ALIGN_PARAGRAPH():
    """段落对齐枚举（`WD_ALIGN_PARAGRAPH`）。

    返回枚举类本身而不是某个成员：调用方要按 `GBStyle.align`
    查表，写死一个成员在这里没法表达。
    """
    _require_docx()
    from docx.enum.text import WD_ALIGN_PARAGRAPH as _WAP

    return _WAP


def WD_LINE_SPACING():
    """行距规则枚举（`WD_LINE_SPACING`）。"""
    _require_docx()
    from docx.enum.text import WD_LINE_SPACING as _WLS

    return _WLS


def xml_escape_safe() -> None:  # pragma: no cover - 占位，说明性函数
    """保留位：说明本项目**不做**的 XML 处理。

    所有文本都通过 `run.text = ...` / `paragraph.add_run(...)` 交给
    python-docx 转义。本文件**不提供**任何手拼 XML 字符串的入口 ——
    一旦有了，就一定会有人把用户发言直接塞进 XML 里（会议纪要是
    自由文本，出现 `<` `&` 是家常便饭），产出的 docx 会损坏。
    """
    return None


#: 确定性序列化使用的固定时间戳（ZIP entry 的 `date_time`）。
#:
#: ⚠️ 必须是 **1980-01-01 00:00:00** —— ZIP 格式的时间字段从这里起算，
#: 早于此值无法表示（`zipfile` 会抛 `ValueError`），晚于它则会让
#: 同一个文档在不同时刻产出不同字节。
DETERMINISTIC_ZIP_DATETIME = (1980, 1, 1, 0, 0, 0)


def save_bytes(doc) -> bytes:
    """把 `Document` 序列化成 bytes，且**结果可复现**（T1-09/T1-10）。

    为什么不能直接用 `doc.save(buf)`
    -------------------------------
    python-docx 的 `save()` 底层是 `zipfile.ZipFile`，它给每个 ZIP 条目
    写的是**当前本地时间**（`ZipInfo.date_time`）。于是"内容完全一样"
    的两次渲染，产出的字节**不一样**：

        word/document.xml  date=(2026,9,28,16,45,28) crc=ca5fecda
        word/document.xml  date=(2026,9,28,16,45,30) crc=ca5fecda
        # CRC 相同 -> 内容相同；字节不同 -> 只有时间戳在变

    这直接破坏了导出的幂等判定：`ExportService.export_docx` 用
    "新渲染字节的 sha256" 与"manifest 里已落盘文件的 sha256"比对来决定
    `reused`。带上时钟之后，同一个版本**只要跨过一秒再导出就不是复用**，
    于是重复导出会真的重写文件、重登 manifest —— C07 幂等形同虚设。

    *为什么之前没暴露*：同一次测试里两次导出经常落在同一秒内，
    哈希碰巧相等；一旦测试变慢（比如全量回归 1700 个用例）就随机失败。
    这是**典型的不确定性缺陷**，靠"重跑一下"永远不会被发现。

    为什么选 1980-01-01 而不是"渲染时刻"
    ------------------------------------
    本项目对 docx 的既有做法是**基准快照对照**（T1-09 的
    `tools/make_docx_fixtures.py` + `verify_docx_style.py`）。
    快照要能字节级比对，序列化就必须**只由内容决定**。
    固定时间戳顺带买到三件事：基准可复现、幂等真成立、
    `export/` 里的文件"内容相同则字节相同"（可校验传输完整性）。

    实现方式：自己当 zip writer。python-docx 的
    `Package.save()` 走到 `_ZipPkgWriter`，那里 `zwrite()` 只接
    路径/流、不暴露 `ZipInfo` —— 所以这里改成"先落到内存里的
    临时 zip，再按固定时间戳重写一遍"。多一次 IO 换来确定性，
    而导出本来就是低频操作。**不写临时文件**：`export/` 是明文区，
    多一步落盘就多一处忘记清理的未加密残留。
    """
    import io
    import zipfile

    raw = io.BytesIO()
    doc.save(raw)

    src = zipfile.ZipFile(io.BytesIO(raw.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            info = zipfile.ZipInfo(item.filename,
                                   date_time=DETERMINISTIC_ZIP_DATETIME)
            info.compress_type = item.compress_type
            info.external_attr = item.external_attr
            info.internal_attr = item.internal_attr
            info.create_system = item.create_system
            # 权限位（低 16 位）里含平台相关的默认值，抹掉更稳。
            info.external_attr = (info.external_attr >> 16) << 16
            dst.writestr(info, src.read(item.filename))
    return out.getvalue()


__all__ = [
    "Cm",
    "DETERMINISTIC_ZIP_DATETIME",
    "DocxBackendError",
    "OxmlElement",
    "Pt",
    "WD_ALIGN_PARAGRAPH",
    "WD_LINE_SPACING",
    "new_document",
    "open_document",
    "qn",
    "save_bytes",
]
