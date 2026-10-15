"""统一错误码与错误类型（SAD §9.3）。

为什么错误码放在**领域层**
--------------------------
错误码是**业务契约**的一部分，不是基础设施细节：
  - 前端要根据码决定显示什么文案；
  - API 层要按码映射 HTTP 状态；
  - 测试要按码断言。
它必须被所有层引用而不引入任何依赖 —— 所以放在领域层最底层，
只有标准库依赖（满足 check_layers 的 C01 约束）。

为什么错误对象同时带「技术描述」和「使用者文案」
------------------------------------------------
SAD §9.3 明确规定了一条很容易被违反的纪律：

    > **严禁**把堆栈、SQL、模型原始输出直接展示给使用者。

现实里违反它几乎总是**无意的**：工程师写 `raise AsrError(str(e))`，
上游异常的信息就顺着 `str(e)` 一路流到了界面上。
本模块从类型上杜绝这件事——`Detail` 与 `Message` 是两个字段，
调用方要显示给用户就必须**显式写一句中文**。想偷懒就得先写下
`user_message=...`，这个动作本身就是提醒。

严重度（`Severity`）的用途
--------------------------
不是装饰。它决定上层行为：
  - `WARNING`：记审计、继续跑（如"必需字体缺失，导出会与规范有差异"）；
  - `ERROR`：当前任务失败，可重试；
  - `FATAL`：**停止处理并要求人工介入**（如 E2003 时间戳错位，
    SAD §9.3 的文案是"已暂停处理"，因为静默失效的转写稿不可信，
    让它流下去比直接停下更危险）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """错误严重度，决定上层是"记一笔继续"还是"停下等人"。"""

    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


@dataclass(frozen=True)
class ErrorSpec:
    """一个错误码的静态定义（码 -> 严重度 + 使用者文案）。

    `user_message` 写成**完整的一句话**，而不是名词短语。原因：
    它会被直接渲染到界面提示条里，用户看到的就是这一行字，
    没有上下文可补。写"格式不支持"不如写"这个文件的格式不支持…"。
    """

    code: str
    severity: Severity
    user_message: str
    title: str = ""


# ---------------------------------------------------------------------------
# 错误码表（SAD §9.3 逐条落地）
#
# 只登记一期真正会抛出的码。**未使用的码不预登记** —— 一份
# "声明了但没人抛"的码表会让人以为系统有那些能力，反而误导排查。
# 新增能力时在这里加，并在测试里断言"码表与文档一致"。
# ---------------------------------------------------------------------------

INPUT_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E1001", Severity.ERROR,
        "这个音频文件的格式不支持。请转成 wav、mp3、m4a、wma、amr、flac、aac 或 ogg 后再试。",
        "音频格式不支持",
    ),
    ErrorSpec(
        "E1002", Severity.ERROR,
        "找不到要处理的文件。请确认文件没有被移动或删除。",
        "文件不存在",
    ),
    ErrorSpec(
        "E1003", Severity.ERROR,
        "这个音频文件读不出来，可能已经损坏。请换一份完整的录音后重试。",
        "音频损坏",
    ),
    ErrorSpec(
        "E1004", Severity.ERROR,
        "导入的文字稿是空的。请检查文件内容后重试。",
        "文字稿为空",
    ),
)

ASR_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E2001", Severity.ERROR,
        "本地识别引擎没有就绪，当前无法转写。请联系技术人员检查模型是否安装完整。",
        "识别引擎未就绪",
    ),
    ErrorSpec(
        "E2002", Severity.ERROR,
        "转写用时过长，已中止本次处理。可以稍后重试，或把音频分成几段分别投递。",
        "转写超时",
    ),
    ErrorSpec(
        "E2003", Severity.FATAL,
        "转写结果出现异常，已暂停处理。请重新投递音频，或联系技术人员。",
        "转写结果异常",
    ),
    ErrorSpec(
        "E2004", Severity.WARNING,
        "术语库里的词语没有在识别中生效，个别人名或单位名可能写法有误。"
        "建议在复核页重点核对这些人名。",
        "术语未生效",
    ),
)

#: 抽取与生成错误（SAD §9.3 码表 E3xxx）。
#:
#: ⚠️ T1-11 补登记：这一族**整族漏了**。E3002 从 T1-07 起就在
#: `app/services/extraction.py` 与 `graph/nodes/` 里被抛（施工文档
#: line 379 明确写了 `SchemaViolationError → E3002`），但码表里连
#: `EXTRACTION_SPECS` 这个名字都没有 —— 只有一个没人用的 `_EXTRACTION = "E3xxx"`
#: 占位串。后果与 E5xxx 那次一样：`user_message` 退回空串。
#: 是 T1-11 的验收脚本"附加 B"逐族扫前缀时把它抓出来的 ——
#: 这类"整族缺失"靠人工翻代码很难发现，**验收脚本按前缀扫**才是有效手段。
EXTRACTION_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E3001", Severity.ERROR,
        "文稿切分时出错，无法继续处理。请确认文稿内容完整后重试。",
        "分块失败",
    ),
    ErrorSpec(
        "E3002", Severity.WARNING,
        "纪要结构自检未通过，已自动重试。若反复出现，请核对文稿是否完整。",
        "结构自检未通过",
    ),
    ErrorSpec(
        "E3003", Severity.ERROR,
        "内容整理用时过长，已中止本次处理。可以稍后重试。",
        "生成超时",
    ),
    ErrorSpec(
        "E3004", Severity.FATAL,
        "本机的文字处理服务没有就绪，无法整理内容。请联系技术人员检查安装。",
        "模型不可用",
    ),
)

_GATE = "E4xxx"
_EXPORT = "E5xxx"
_PERMISSION = "E6xxx"
_SYSTEM = "E9xxx"

#: 闸口与状态错误（T1-10 落地，SAD §9 码表 E4xxx）
#:
#: 文案都由这里给（`MmwfError.__post_init__` 会按码表覆盖调用处的空白），
#: 免得每个抛错点各写一句，最后界面上三种说法。
GATE_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E4001", Severity.ERROR,
        "当前状态不接受这个操作，页面可能已过期。请刷新后重试。",
        "操作不被允许",
    ),
    ErrorSpec(
        "E4002", Severity.FATAL,
        "找不到任务的进度记录，无法从中断处继续。请联系技术人员。",
        "进度记录丢失",
    ),
    ErrorSpec(
        "E4003", Severity.WARNING,
        "没有需要确认的内容，已跳过这一步。",
        "待确认项为空",
    ),
    ErrorSpec(
        "E4004", Severity.ERROR,
        "重试多次仍未通过自检，已带着待核对清单继续成文。"
        "请重点核对清单中列出的问题。",
        "自检未通过",
    ),
    ErrorSpec(
        "E4005", Severity.WARNING,
        "任务已被取消，已完成的中间结果仍然保留。",
        "任务已取消",
    ),
)

#: 导出错误（T1-09 落地，SAD §9 码表 E5xxx）
#:
#: ⚠️ T1-11 补登记：E5001 / E5002 自 T1-09 起就在 `app/services/export.py`
#: 与 `domain/docx/font_check.py` 里被抛出，但**一直没进这张表** ——
#: 后果是 `MmwfError.__post_init__` 找不到 spec，`user_message` 与
#: `title` 都退回空串，界面提示条会显示成一片空白。
#: 码表是权威，**在代码里用过就必须登记**，否则等于没有用户文案。
EXPORT_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E5001", Severity.ERROR,
        "导出用的版式模板缺失，无法生成公文格式的文件。请联系技术人员检查安装。",
        "版式模板缺失",
    ),
    ErrorSpec(
        "E5002", Severity.WARNING,
        "本机未安装『仿宋_GB2312』等规范字体，导出的文号格式会与规范有差异。"
        "建议联系技术人员安装字体后再导出。",
        "字体缺失",
    ),
    ErrorSpec(
        "E5003", Severity.ERROR,
        "导出文件时出错，没有生成文件。请重试，若反复出现请联系技术人员。",
        "导出失败",
    ),
    ErrorSpec(
        "E5004", Severity.ERROR,
        "目标文件正被其他程序打开，无法写入。请关闭该文件后重试。",
        "文件被占用",
    ),
)

#: 权限与合规错误（T1-11 落地，SAD §9 码表 E6xxx）
#:
#: 这三个是 API 层的立身之本：本服务只监听回环、且每次启动换令牌，
#: 所以"谁能调"全靠 E6001 / E6002 把住；E6003 是密级确认闸。
PERMISSION_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E6001", Severity.ERROR,
        "会话已失效，请重新启动程序。",
        "会话已失效",
    ),
    ErrorSpec(
        "E6002", Severity.ERROR,
        "本程序只允许在这台电脑上使用，已拒绝本次访问。",
        "仅限本机访问",
    ),
    ErrorSpec(
        "E6003", Severity.WARNING,
        "这份材料的密级还没有确认，请先确认密级再继续。",
        "密级未确认",
    ),
)

#: 系统错误（T1-10/T1-11 落地，SAD §9 码表 E9xxx）
#:
#: ⚠️ T1-11 补登记：**E9001 自 T1-10 起就被大量使用** ——
#: `graph/nodes/_base.py` 把所有未预期异常都包成 E9001 再抛
#: （见该文件"否则会一路冒到 API 层变成英文 500"的注释），
#: `graph/interrupts.py` 与 `graph/runner.py` 也在用。
#: 它没进码表时，那条设计意图**根本没生效**：用户看到的是空文案。
SYSTEM_SPECS: tuple[ErrorSpec, ...] = (
    ErrorSpec(
        "E9001", Severity.ERROR,
        "程序遇到一个未预期的问题，本次操作没有完成。请重试；"
        "若反复出现，请把界面上的编号告诉技术人员。",
        "程序出错",
    ),
    ErrorSpec(
        "E9002", Severity.FATAL,
        "磁盘空间不足，无法继续处理。请清理磁盘后重试。",
        "磁盘空间不足",
    ),
    ErrorSpec(
        "E9003", Severity.WARNING,
        "同一个任务正在处理中，请等待它完成后再操作。",
        "任务正在处理",
    ),
)

#: 全部已登记的错误码规格
ALL_SPECS: dict[str, ErrorSpec] = {
    s.code: s
    for s in (
        *INPUT_SPECS,
        *ASR_SPECS,
        *EXTRACTION_SPECS,
        *GATE_SPECS,
        *EXPORT_SPECS,
        *PERMISSION_SPECS,
        *SYSTEM_SPECS,
    )
}

#: 各错误码族的名称（族前缀 -> 中文名）。
#:
#: 用途：① 给码表自检/验收脚本按族核对"有没有整族漏登记"；
#: ② 生成运维手册的码表附录。
#: **必须是"已落地族"的完整清单** —— 漏掉一族，那族的整族缺失就查不出来
#: （E3xxx 就是这么漏掉的）。
ERROR_FAMILIES: dict[str, str] = {
    "E1": "输入与文件",
    "E2": "转写",
    "E3": "抽取与生成",
    "E4": "闸口与状态",
    "E5": "导出",
    "E6": "权限与合规",
    "E9": "系统",
}

#: 施工文档已定义、但一期尚未实现的错误码前缀（仅用于生成说明，不参与运行时）
RESERVED_PREFIXES: dict[str, str] = {
    _GATE: "闸口与状态",
    _EXPORT: "导出",
    _PERMISSION: "权限与合规",
    _SYSTEM: "系统",
}


# ---------------------------------------------------------------------------
# 异常基类
# ---------------------------------------------------------------------------

@dataclass
class MmwfError(Exception):
    """本项目所有业务异常的基类。

    刻意**继承自普通 Exception 而不是 RuntimeError 的某个语义子类**：
    业务异常要能被 API 层统一捕获并转成结构化响应，
    而 RuntimeError/ValueError 那些是"程序写错了"的信号，不该被这种捕获吞掉。

    字段的两分法（本类的核心设计）：
      · `detail`       —— 给日志和排查用，可以含路径、参数、上游原文；
      · `user_message` —— 给用户看，必须是完整、无术语的中文句子。

    `to_dict()` 供 API 层直接序列化，**只输出 user_message，不输出 detail**。
    要从 detail 里取信息给用户，必须先在调用处显式拼一句 user_message。
    """

    code: str = ""
    detail: str = ""
    user_message: str = ""
    severity: Severity = Severity.ERROR
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = ALL_SPECS.get(self.code)
        if spec is not None:
            # 码表是权威：别让各个抛错点自己编用户文案（会不一致）
            if not self.user_message:
                object.__setattr__(self, "user_message", spec.user_message)
            if not self.title:
                object.__setattr__(self, "title", spec.title)
            object.__setattr__(self, "severity", spec.severity)
        Exception.__init__(self, f"[{self.code}] {self.detail or self.user_message}")

    #: 人类可读短标题（界面上作为提示条标题）
    title: str = ""

    @property
    def is_fatal(self) -> bool:
        return self.severity is Severity.FATAL

    def to_dict(self) -> dict[str, Any]:
        """API 响应体形状。**不含 detail**——见类文档。"""
        return {
            "code": self.code,
            "title": self.title,
            "message": self.user_message,
            "severity": self.severity.value,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail or self.user_message}"


class AsrError(MmwfError):
    """转写环节错误（E2xxx）。"""


class AsrNotReadyError(AsrError):
    """E2001：引擎/模型不可用。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E2001", detail=detail, context=ctx)


class AsrTimeoutError(AsrError):
    """E2002：超出分配的转写时间。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E2002", detail=detail, context=ctx)


class AsrSilentFailureError(AsrError):
    """E2003：时间戳与文本长度错位——**上游静默失效**。

    这是本项目最需要警惕的一类失败：引擎**不报错**，返回一份看起来正常
    但时间戳与文本对不上的结果。如果放它过去，后续的"逐字溯源"全部错位，
    而用户完全不知道。所以严重度是 FATAL（SAD §9.3 的文案是"已暂停处理"）。
    """

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E2003", detail=detail, context=ctx)


class AsrHotwordIneffectiveError(MmwfError):
    """E2004：热词未生效（WARNING 级——结果可用但需人工核对人名）。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E2004", detail=detail, context=ctx)


class InputError(MmwfError):
    """输入与文件环节错误（E1xxx）。"""


class UnsupportedFormatError(InputError):
    """E1001。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E1001", detail=detail, context=ctx)


class FileMissingError(InputError):
    """E1002。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E1002", detail=detail, context=ctx)


class CorruptAudioError(InputError):
    """E1003。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E1003", detail=detail, context=ctx)


class EmptyTranscriptError(InputError):
    """E1004。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E1004", detail=detail, context=ctx)


# ---------------------------------------------------------------------------
# 抽取与生成（E3xxx，T1-11 补登记）
# ---------------------------------------------------------------------------

class ExtractionError(MmwfError):
    """抽取与生成环节错误（E3xxx）。

    ⚠️ 这一族从 T1-07 起就被抛用（施工文档 line 379 点名
    `SchemaViolationError → E3002`），但**整族漏了登记**。
    T1-11 的验收脚本按前缀扫族时才发现 —— 见 `EXTRACTION_SPECS` 的注释。
    """


class ChunkingError(ExtractionError):
    """E3001：文稿分块失败。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E3001", detail=detail, context=ctx)


class SchemaViolationError(ExtractionError):
    """E3002：模型输出不符合既定结构。

    ⚠️ 严重度是 **WARNING**，不是 ERROR —— 它是"自检没通过，已自动重试"
    的**过程**信号，不是终态失败。编排层靠它决定走 retry 边
    （`graph/build.py` 的 `ROUTE_RETRY`）。把它当 ERROR 会让前端
    一次瞬时就弹红框，而其实后面会重试成功。
    """

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E3002", detail=detail, context=ctx)


class GenerationTimeoutError(ExtractionError):
    """E3003：生成超时。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E3003", detail=detail, context=ctx)


class ModelUnavailableError(ExtractionError):
    """E3004：本机模型服务不可用（FATAL —— 无降级路径）。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E3004", detail=detail, context=ctx)


# ---------------------------------------------------------------------------
# 权限与合规（E6xxx，T1-11 落地）
# ---------------------------------------------------------------------------

class PermissionError_(MmwfError):
    """权限与合规错误（E6xxx）。

    ⚠️ 类名带下划线后缀，因为它要避开内置的 `PermissionError`。
    用 `as` 重命名内置异常很容易在别处踩坑，所以宁可类名难看一点。
    """


class TokenInvalidError(PermissionError_):
    """E6001：令牌无效或已过期。

    API 层每个受保护端点都会抛它。**注意走的是统一信封 + 200 状态码**，
    不是 HTTP 401 —— 见 `api/envelope.py` 的说明。
    """

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E6001", detail=detail, context=ctx)


class NonLoopbackError(PermissionError_):
    """E6002：请求来自非回环地址。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E6002", detail=detail, context=ctx)


class ClassificationUnconfirmedError(PermissionError_):
    """E6003：密级未确认。"""

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E6003", detail=detail, context=ctx)


# ---------------------------------------------------------------------------
# 系统（E9xxx，T1-10/T1-11 落地）
# ---------------------------------------------------------------------------

class SystemError_(MmwfError):
    """系统级错误（E9xxx）。类名后缀同 `PermissionError_`。"""


class UnexpectedSystemError(SystemError_):
    """E9001：未预期错误。

    `graph/nodes/_base.py` 用它把任意异常包成统一形状 —— 这样用户
    看到的是中文提示，而不是一路冒到 API 层的英文 500。
    """

    def __init__(self, detail: str = "", **ctx: Any) -> None:
        super().__init__(code="E9001", detail=detail, context=ctx)
