# Windows W1：相对路径 POSIX 化验证记录

本次在 macOS 使用项目 `.venv/bin/python` 验证。首轮只运行定向测试，未覆盖新增隐藏路由引起的清单回归；复审后已删除额外路由，并按用户要求通过完整门禁。未添加依赖，未提交、推送或创建分支。

统一入口为 `src/mmwf/infra/paths.py::relative_posix`，使用纯路径对象的 `as_posix()`，兼容 Windows 字符串、PureWindowsPath、宿主路径及空引用。它只负责表示转换；文件访问仍由 FileStore 同时检查 POSIX/Windows 根、盘符、UNC、`..` 及解析后的真实路径边界。

## 修复与模拟证据

- 仓储响应、manifest 写入/覆盖/删除及各产物区统一路径：proven by simulated test `tests/unit/test_windows_paths.py::test_store_windows_registration_read_delete`。
- 旧 manifest 读取与 Windows 扫描结果双向归一化、重建与真实漏登记报告：proven by simulated test `tests/unit/test_windows_paths.py::test_verify_normalises_legacy_entries_and_windows_scan`。
- DOC/PDF 转换及返回路径：proven by simulated test `tests/unit/test_windows_paths.py::test_converter_windows_source_returns_posix`。
- PDF 响应、来源登记、缓存复用、幂等结果引用、归档及审计一致性：proven by simulated test `tests/unit/test_windows_paths.py::test_pdf_windows_source_cache_archive_audit`。
- 图节点结果、归档幂等键、旧 PDF/归档登记读取：proven by simulated test `tests/unit/test_windows_paths.py::test_graph_archive_keys_and_legacy_bindings`。
- 默认相对幂等引用：proven by simulated test `tests/unit/test_windows_paths.py::test_windows_relative_idempotency_refs`。
- 记录稿和导出审计路径：proven by simulated test `tests/unit/test_windows_paths.py::test_windows_audit_record_and_export_details`。
- 纪要交付版本匹配：proven by simulated test `tests/unit/test_windows_paths.py::test_minutes_windows_delivery_reference`。
- 下载接受 `export/文件名`，映射到宿主路径：proven by simulated test `tests/unit/test_windows_paths.py::test_download_accepts_posix_reference_with_windows_written_file`。
- 下载及仓储继续拒绝盘符、UNC、穿越：proven by simulated test `tests/unit/test_windows_paths.py::test_download_rejects_windows_drives_and_traversal`、`tests/unit/test_windows_paths.py::test_windows_resolve_keeps_traversal_drive_unc_rejection`。
- Windows 分隔符不改变加密区/明文区判定：proven by simulated test `tests/unit/test_windows_paths.py::test_windows_encrypted_area_and_plaintext_export`。
- 闸口快照引用：proven by simulated test `tests/unit/test_windows_paths.py::test_gate_snapshot_ref_is_posix_with_windows_checkpoint`。
- shadow 工具的相对代码指纹键改用同一入口；该入口的 Windows 输入及 os.sep 模拟：proven by simulated test `tests/unit/test_windows_paths.py::test_relative_posix_with_windows_inputs`。工具整体回归纳入本轮完整门禁。

审计发现 minutes_store 和 transcript_store 不生成相对文件引用；记录稿路径、闸口引用与 shadow 的输出模板原本使用 `/`。保留绝对路径（数据库、外部进程、锁和本地来源信息）的宿主表示。

真实 Windows + Git Bash + Python 3.13.14 的完整验收：only verifiable by rerunning on Windows。本次没有新增跳过或降低断言。

## 复审修复

- 下载仅保留原有的一条路由，将匹配参数改为 `{file:path}`；同步 SAD、工具清单及独立令牌清单的参数声明。没有新增端点，SAD 34 条、路由 35 条、去重路径 30 条的断言保持不变。POSIX 引用经真实路由下载：proven by simulated test `tests/unit/test_windows_paths.py::test_download_accepts_posix_reference_with_windows_written_file`。
- 新增真实路由测试覆盖 `export/..%2f`、编码前缀分隔符、编码反斜杠及双重编码；断言 HTTP 400、E1001、trace_id，且不读取交付物：proven by simulated test `tests/unit/test_windows_paths.py::test_download_routed_encoded_traversal_is_400`。400 是本轮评审要求的局部例外，已同步 SAD 和使用说明。
- `ManifestEntry`、`StoredFile` 的 `__post_init__` 已移到字段之后。
- 明确仓储引用不支持 POSIX 字面反斜杠文件名，反斜杠恒作为目录分隔符；确认 write/resolve 指向同一目标：proven by simulated test `tests/unit/test_windows_paths.py::test_posix_backslash_name_is_store_directory_separator`。

## 精确命令与输出（本轮）

用户指定的完整检查：

```sh
.pipeline/bin/check_changed_lint.sh && .venv/bin/python -m pytest -o addopts='' -q -rfE -p no:cacheprovider -m 'not real_model'
```

退出码 0。层次依赖检查通过（78 文件），依赖白名单检查通过，严格 mypy：Success: no issues found in 48 source files。pytest：**3398 passed, 19 skipped, 1 deselected, 1 warning in 105.90s (0:01:45)**。未新增 skip 或放宽断言；warning 为既有 LangChainPendingDeprecationWarning。

评审指定的两个文件独立复跑：

```sh
.venv/bin/python -m pytest -o addopts='' -q tests/unit/test_api.py tests/contract/test_post_validation_contract.py
```

**106 passed, 1 warning in 3.06s**，包含 `TestToken::test_protected_endpoint_list_matches_router` 和 `test_endpoint_inventory_d10`。

路由、清单和 Windows 模拟的定向检查：

```sh
.venv/bin/python -m pytest -o addopts='' -q tests/unit/test_api.py tests/contract/test_post_validation_contract.py tests/unit/test_windows_paths.py tests/unit/test_api_diff_export.py
```

**199 passed, 1 warning in 4.23s**。

### F5.9 证据失败归因

修改路由前，在带有上一轮 W1 改动的工作树执行：

```sh
.venv/bin/python -m pytest -o addopts='' -q tests/integration/test_accept_f5_9_evidence.py
```

**2 passed in 27.92s**，未复现评审记录的失败。

另使用 `git archive HEAD` 在仓库内 `.pipeline/w1-head-*` 临时目录还原 HEAD，链接同一项目 `.venv`，将 PYTHONPATH 指向副本的 src/tools，在副本执行同一测试。这样不依赖 git stash，也不改动现有工作树或索引；临时目录已清理。

HEAD 基线：**2 passed in 28.46s，退出码 0**。因此不能将评审记录中的证据失败归类为已确认的既有失败或 W1 引入失败；本轮实测 HEAD 与修改树均通过，完整门禁也包含该测试并通过。

最终修改树独立复跑同一 F5.9 命令：**2 passed in 28.88s**。

```sh
git diff --check
```

通过，无输出。
