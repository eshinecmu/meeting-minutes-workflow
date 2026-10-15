r"""字体预检与回退（T1-09，F5.7 + R7 + SAD §9.3 E5002）。

（本模块的 docstring 用 raw 字符串：正文里要写 Windows 注册表路径
`HKLM\SOFTWARE\...`，普通字符串里的 `\S` 会触发 SyntaxWarning。）

为什么这件事需要一个独立模块
----------------------------
因为**公文字体是商业字体，不能随包分发**（部署手册 §2.1 第三段）：

    > 方正小标宋简体、仿宋_GB2312、方正楷体_GBK 均为商业字体，
    > 受版权保护，本项目不得随包分发，模板只引用字体名。

于是必然出现"目标机没装"的情况。此时系统的行为必须是
**降级而不是失败** —— 一份字体不合规的公文，远好过交付不出来。
但降级必须**留痕**：使用者有权知道"你这个标题显示成宋体了，
跟规范有差异"，否则他打印盖章发出去，事后被指出版式不合规，
系统却什么都没说过。这就是 E5002（警告级）存在的意义。

警告级，不是错误级
------------------
`Severity.WARNING` 在 SAD §9.3 的定义是"记审计、继续跑"。
字体缺失正好符合：结果可用（内容一个字没少），只是与规范有差异。
把它当 ERROR 会让导出直接失败，那是**过度严格**——
党政机关的办公机通常已装齐字体（部署手册 §2.2 明说了这是
本方案在体制内环境的天然优势），偶发缺失不该阻断业务。

三个策略档位（config/models.yaml 的 `font_fallback_policy`）
-----------------------------------------------------------
    warn      默认。回退 + 警告，继续导出。
    fallback  回退但不报文件级警告（只记日志）。用于已确认
              "本机就该用宋体替代"的特殊场合。
    fail      不回退，直接抛 E5001/E5002。用于**对外正式行文**——
              版式不合规就不该出文件，宁可让人去装字体。

`fail` 档为什么要报 E5001 而不是 E5002
--------------------------------------
E5001 是"版式模板缺失"（错误级），E5002 是"必需字体缺失"（警告级）。
`fail` 档下字体缺失导致**导出无法进行**，性质上是"模板不可用"，
所以走 E5001；同时把"具体缺哪个字体"放进 detail 供排查。
这样使用者看到的文案是"版式模板缺失"（他知道找谁），
日志里能看到 `font missing: 仿宋_GB2312`（运维知道装什么）。

检测手段：真的去问操作系统，不猜
--------------------------------
不做"路径下有没有那个 .ttf"式的猜测（字体可能装在用户目录、
可能装在字体集合 TTC 里、可能是设备字体）。Windows 上直接读注册表
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts`，
那是 Windows 字体的权威清单（部署手册 §2.2 也是这么让运维查的）。

非 Windows / 读不到注册表时**不报错**，而是报"无法预检"并**放行**——
预检失败不等于字体缺失，把它当缺失会让整个导出在容器里 100% 失败。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from mmwf.domain.docx.style_mapper import FONT_FALLBACK, GB_STYLES

#: 三个策略档位（config/models.yaml `font_fallback_policy`）。
POLICY_WARN = "warn"
POLICY_FALLBACK = "fallback"
POLICY_FAIL = "fail"
VALID_POLICIES = frozenset({POLICY_WARN, POLICY_FALLBACK, POLICY_FAIL})

#: 默认字体是否可用时的三种结论。
STATUS_INSTALLED = "installed"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"      # 非 Windows 或注册表不可读


@dataclass(frozen=True)
class FontCheckResult:
    """单个字体的预检结果。"""

    font_name: str
    status: str
    #: 实际会被使用的字体（回退链里第一个可用的）。未知状态下等于原名。
    resolved: str
    #: 回退链（不含自己）。`installed` 时为空。
    fallback_tried: tuple[str, ...] = ()

    @property
    def installed(self) -> bool:
        return self.status == STATUS_INSTALLED

    @property
    def is_fallback(self) -> bool:
        """是否发生了回退（字体不在，但找到了替代）。"""
        return self.status == STATUS_MISSING and self.resolved != self.font_name


@dataclass(frozen=True)
class FontReport:
    """一次导出前对全部必需字体的预检报告。"""

    checks: tuple[FontCheckResult, ...] = ()
    policy: str = POLICY_WARN
    precheck_available: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """缺失的原始字体名（去重、保持首次出现顺序）。"""
        seen: list[str] = []
        for c in self.checks:
            if c.status == STATUS_MISSING and c.font_name not in seen:
                seen.append(c.font_name)
        return tuple(seen)

    @property
    def all_installed(self) -> bool:
        return not self.missing

    def resolve(self, font_name: str) -> str:
        """查该字体在本机的实际落点（未预检过则原样返回）。"""
        for c in self.checks:
            if c.font_name == font_name:
                return c.resolved
        return font_name

    def summary(self) -> str:
        if not self.precheck_available:
            return "字体预检不可用（非 Windows 或注册表不可读），按放行处理"
        if self.all_installed:
            return f"字体预检通过（{len(self.checks)} 项全部就绪）"
        return f"字体预检：缺失 {len(self.missing)} 项 —— {'、'.join(self.missing)}"

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "precheck_available": self.precheck_available,
            "all_installed": self.all_installed,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
            "fonts": [
                {
                    "font": c.font_name,
                    "status": c.status,
                    "resolved": c.resolved,
                    "fallback_tried": list(c.fallback_tried),
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# 系统字体清单
# ---------------------------------------------------------------------------

#: 注册表里 Windows 字体的权威位置（部署手册 §2.2 给运维的查询路径）。
_FONT_REG_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

#: 进程内缓存。字体清单在一次运行内不会变，而读注册表有开销；
#: 反复预检（每次导出都做）时缓存能把开销降到零。
_FONT_CACHE: set[str] | None = None


def list_installed_fonts(*, use_cache: bool = True) -> set[str]:
    """列出本机已安装的字体名（小写、去掉 "(TrueType)" 之类的后缀）。

    非 Windows 或读取失败时返回**空集合**，调用方据此走
    `STATUS_UNKNOWN`（放行），而不是判定"全都缺失"。
    """
    global _FONT_CACHE
    if use_cache and _FONT_CACHE is not None:
        return _FONT_CACHE

    names: set[str] = set()
    if sys.platform == "win32":  # pragma: no cover - 仅 Windows 真机走这里
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _FONT_REG_KEY) as key:
                idx = 0
                while True:
                    try:
                        value_name, _, _ = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    names.add(_clean_font_name(value_name))
                    idx += 1
        except OSError:
            names = set()

    if use_cache:
        _FONT_CACHE = names
    return names


def _clean_font_name(raw: str) -> str:
    """注册表里的值名形如 `仿宋_GB2312 (TrueType)`，剥掉括号部分。

    **不要**用 `(TrueType)` 当后缀一刀切：字体名本身可能含括号
    （如 `方正小标宋_GBK(Bold)`）。只在**末尾**且形如 `(TrueType)`
    / `(OpenType)` / `(All res)` 时才剥。实际做法是从最后一个
    `(` 开始判断，且括号内只有单词字符与空格。
    """
    text = raw.strip()
    if text.endswith(")") and "(" in text:
        head, _, tail = text.rpartition("(")
        inner = tail[:-1].strip().lower()
        if inner in {"truetype", "opentype", "all res", "truetype italic",
                     "all res font", "type 1"}:
            return head.strip()
    return text


def _normalize(name: str) -> str:
    """比较用归一化：小写、去空格、去连字符差异。

    "Times New Roman" 与 "times new roman" 是同一个字体；
    "仿宋_GB2312" 与 "仿宋 GB2312" 在有些机器上写法不同。
    """
    return name.replace(" ", "").replace("-", "").lower()


def is_installed(font_name: str, installed: set[str] | None = None) -> bool:
    """该字体（或其别名写法）是否已安装。"""
    fonts = installed if installed is not None else list_installed_fonts()
    if not fonts:
        return False
    target = _normalize(font_name)
    return any(_normalize(f) == target for f in fonts)


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

def required_fonts(styles: Mapping | None = None) -> tuple[str, ...]:
    """导出一次公文需要哪些字体（按 `GB_STYLES` 去重、保持出现顺序）。

    顺序有意义：标题字体排最前 —— 报告里先看到的是"最要紧的那个缺了"。
    """
    table = styles if styles is not None else GB_STYLES
    out: list[str] = []
    for style in table.values():
        if style.font not in out:
            out.append(style.font)
    return tuple(out)


def resolve_font(font_name: str, installed: set[str] | None = None) -> tuple[str, str, tuple[str, ...]]:
    """按回退链决定实际用哪个字体。

    返回 `(status, resolved, fallback_tried)`。
    连回退链的最后一个都找不到时，**返回链的末位**并把状态标 `missing` ——
    不能返回空串（空字体名会让 Word 用默认字体，而我们连"用了什么"
    都报不出来）。
    """
    if not installed:
        # 预检不可用：不猜，放行（见模块文档）
        return STATUS_UNKNOWN, font_name, ()

    if is_installed(font_name, installed):
        return STATUS_INSTALLED, font_name, ()

    tried: list[str] = []
    for candidate in FONT_FALLBACK.get(font_name, ()):
        tried.append(candidate)
        if is_installed(candidate, installed):
            return STATUS_MISSING, candidate, tuple(tried)

    # 回退链走完还没有 —— 但仍要返回一个名字，让导出继续
    chain = FONT_FALLBACK.get(font_name, ())
    resolved = chain[-1] if chain else font_name
    return STATUS_MISSING, resolved, tuple(tried)


def check_fonts(
    fonts: Iterable[str] | None = None,
    *,
    policy: str = POLICY_WARN,
    installed: set[str] | None = None,
) -> FontReport:
    """对导出所需字体做一次预检。

    `installed` 可显式传入（测试用）—— 传 `set()` 表示"预检不可用"，
    传具体集合则按集合判。
    """
    if policy not in VALID_POLICIES:
        raise ValueError(
            f"未知的字体策略 {policy!r}，可选：{'、'.join(sorted(VALID_POLICIES))}"
        )

    names = tuple(fonts) if fonts is not None else required_fonts()
    fonts_set = installed if installed is not None else list_installed_fonts()

    # `installed=None` 走真实探测；探测返回空集合 -> 不可用
    precheck_available = bool(fonts_set)
    if not precheck_available:
        checks = tuple(
            FontCheckResult(n, STATUS_UNKNOWN, n, ()) for n in names
        )
        return FontReport(checks=checks, policy=policy, precheck_available=False)

    checks = tuple(
        FontCheckResult(n, *resolve_font(n, fonts_set)) for n in names
    )

    warnings: list[str] = []
    missing = tuple(dict.fromkeys(c.font_name for c in checks if c.status == STATUS_MISSING))

    if missing and policy in (POLICY_WARN, POLICY_FALLBACK):
        fallen = [c for c in checks if c.is_fallback]
        if policy == POLICY_WARN:
            for c in fallen:
                warnings.append(
                    f"font missing: {c.font_name} -> 已回退为 {c.resolved}"
                )
            unresolvable = [c.font_name for c in checks
                            if c.status == STATUS_MISSING and not c.is_fallback]
            for name in unresolvable:
                warnings.append(
                    f"font missing: {name} -> 未能回退到任何替代字体，"
                    "将使用 Word 默认字体"
                )

    return FontReport(
        checks=checks, policy=policy,
        precheck_available=True, warnings=tuple(warnings),
    )


def policy_blocks_export(report: FontReport) -> bool:
    """`fail` 档下字体缺失是否应当阻断导出。

    `fail` 档**只在真的有缺失时**阻断；`unknown`（预检不可用）不阻断 ——
    容器里读不到注册表很常见，就因为这个不让导出，是把环境问题
    当成了业务问题。
    """
    return report.policy == POLICY_FAIL and bool(report.missing)


def precheck_policy_error(report: FontReport) -> str:
    """`fail` 档阻断导出时，给调用方的技术描述（进日志与审计 detail）。

    返回的是 **detail 级别**的文字（含字体名），不是给使用者的文案 ——
    使用者文案由 `errors.py` 的 E5001 提供（见 SAD §9.3 的两分法）。
    本函数只负责"把足够排查的信息拼出来"。
    """
    if not report.missing:
        return ""
    return (
        "font fallback policy = fail，以下必需字体缺失，导出已中止："
        + "、".join(report.missing)
        + "。请安装字体后将 font_fallback_policy 改回 warn，或确认接受回退。"
    )


def build_check_result_json(report: FontReport) -> dict:
    """给 `GET /system/fonts` 用的响应体（SAD §8.1）。"""
    return {
        "available": report.precheck_available,
        "all_installed": report.all_installed,
        "missing": list(report.missing),
        "policy": report.policy,
        "detail": [
            {"font": c.font_name, "status": c.status, "resolved": c.resolved}
            for c in report.checks
        ],
    }


def default_font_paths() -> Sequence[Path]:  # pragma: no cover - 说明性占位
    """保留位：为什么本项目**不**按路径找字体。

    字体可能装在 `C:\\Windows\\Fonts`、用户目录 `%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`、
    或作为 TTC 字体集合的一部分；路径查法漏报率高。
    **唯一权威是注册表**（部署手册 §2.2 也让运维查注册表）。
    本函数返回空序列，仅用于文档说明。
    """
    return ()


__all__ = [
    "POLICY_FAIL",
    "POLICY_FALLBACK",
    "POLICY_WARN",
    "STATUS_INSTALLED",
    "STATUS_MISSING",
    "STATUS_UNKNOWN",
    "VALID_POLICIES",
    "FontCheckResult",
    "FontReport",
    "build_check_result_json",
    "check_fonts",
    "is_installed",
    "list_installed_fonts",
    "policy_blocks_export",
    "precheck_policy_error",
    "required_fonts",
    "resolve_font",
]
