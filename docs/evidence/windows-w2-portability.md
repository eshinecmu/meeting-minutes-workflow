# Windows W2：测试可移植性

本轮只修改测试及验收工具；不增加依赖，不运行全量套件，不提交、推送或创建分支。
宿主为 macOS，项目 `.venv/bin/python` 为 Python 3.12；Windows 3.13.14 的实际验收仍由目标机重跑。

## 修复与模拟证据

- 三处 CLI 测试改用 `sys.executable`，将带空格的 `C:\Program Files\…\Scripts\python.exe` 作为单个 argv 元素原样传递：proven by simulated test `tests/unit/test_test_portability.py::test_cli_uses_current_windows_interpreter`（三个参数子例）。
- 联网守卫集中在 `tools/_netguard.py`，供 pytest fixture、验收插件和 shadow 子进程复用；T1-05 的计数守卫复用同一地址判定。仅数字 `127.0.0.0/8`、`::1` 放行，不做 DNS，非回环的 connect/connect_ex/create_connection 硬失败，数据报仍阻断：proven by simulated test `tests/unit/test_test_portability.py::test_external_connect_fails_before_dns_or_network`、`tests/unit/test_test_portability.py::test_guard_accepts_only_numeric_loopback`、`tests/unit/test_test_portability.py::test_loopback_connect_ex_and_ipv6_delegate`。
- 模拟 Windows TCP socketpair 的 connect 接缝，真实事件循环通过本地 socketpair 收到线程安全唤醒并关闭句柄：proven by simulated test `tests/unit/test_test_portability.py::test_windows_tcp_socketpair_event_loop_works`。沙箱禁止 TCP bind，因此此测试不声称执行了真实 ProactorEventLoop。
- kill-recovery 保留真实 `Popen.kill()` 与恢复行为断言，退出码区分 Windows 的 1 与 POSIX 的 `-SIGKILL`；模拟 Windows 不提供 SIGKILL：proven by simulated test `tests/unit/test_test_portability.py::test_killed_worker_windows_positive_exit`。
- shadow 两个文件的符号链接参数子例实际尝试创建；仅 Windows WinError 1314（权限不足）跳过，其他错误继续失败；创建成功仍执行原有拒绝重叠及不修改资料断言：proven by simulated test `tests/unit/test_test_portability.py::test_symlink_skip_only_windows_privilege_error`、`tests/unit/test_test_portability.py::test_windows_symlink_success_keeps_real_assertion`。
- PDF 替身脚本由解释器启动，移除 shebang/chmod 依赖；保留独立进程、HTTP、G3、FileStore 和版本/转换失败反例：proven by simulated test `tests/unit/test_test_portability.py::test_pdf_stub_launches_python_without_posix_permissions`。
- Windows + Git Bash + Python 3.13.14 下真实 Proactor、TerminateProcess、符号链接权限和完整验收结果：only verifiable by rerunning on Windows。

`tests/`、`tools/` 已搜索 bin/python、/tmp、POSIX shell、os.kill/signal、symlink、chmod。剩余 /tmp 为文档、合成输入或禁止落入系统临时目录的断言；POSIX 信号仅在 POSIX 退出码分支引用。Windows 专用 CDP 工具的 taskkill/PowerShell 是其原定运行平台，不改为跨平台工具。验收工具显示的解释器路径也改为实际命令。联网失败反例改为保留外部地址 203.0.113.1，由守卫在连接前拒绝。

## 精确命令与输出

```sh
.venv/bin/python tools/check_layers.py
# ✓ 层次依赖检查通过（检查 78 个文件）

.venv/bin/python tools/check_deps.py
# ✓ 依赖白名单检查通过（扫描 requirements.in, pyproject.toml）

.venv/bin/python -m mypy src/mmwf/domain --strict
# Success: no issues found in 48 source files

.venv/bin/python -m pytest -o addopts= -q tests/unit/test_test_portability.py tests/unit/test_llm_adapter.py tests/unit/test_grounding.py tests/unit/test_accept_t2_02.py tests/unit/test_accept_t2_03.py tests/unit/test_accept_t2_04.py tests/unit/test_accept_t2_05.py tests/unit/test_accept_f5_9.py tests/integration/test_accept_f5_9_evidence.py tests/integration/test_accept_t2_05_evidence.py tests/integration/test_shadow_cli.py tests/integration/test_shadow_pipeline.py tests/integration/test_it_acceptance_gaps.py tests/integration/test_security_acceptance_gaps.py tests/integration/test_stability_faults.py tests/integration/test_pdf_soffice_stub.py tests/integration/test_extraction_engine_config.py tests/integration/test_dr_fault_injection.py tests/integration/test_post_validation_pipeline.py
# 243 passed, 2 skipped, 1 warning in 60.56s

.venv/bin/python -m pytest -o addopts= -q tests/unit/test_test_portability.py
# 补充 PDF 替身启动模拟，并修正 socketpair 模拟以兼容宿主 fallback 后：
# 32 passed in 0.16s

git diff --check
# 无输出，退出码 0
```

上述两次 pytest 有重叠，不相加。定向运行中的两项跳过均为已有门控：SEC-05 环境禁止回环 bind、renderer 交互缺少 Node。唯一 warning 来自 LangChainPendingDeprecationWarning。本轮无新增 blanket skip；符号链接权限跳过未在本次 macOS 运行触发。

首次 socketpair 探针尝试真实回环 bind，被沙箱以 PermissionError 拒绝；已改为模拟 connect 接缝加真实本地 socketpair，并通过上述最终验证。
