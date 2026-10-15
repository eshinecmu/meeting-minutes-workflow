# mmwf — 会议纪要自动化工作台

> **离线运行的会议纪要自动化工具**：把会议录音放进去，产出**结构合规、可溯源、可逐条校订**的公文式会议纪要，并直接导出符合 **GB/T 9704—2012** 的 `.docx`。

全流程在本机完成——转写与摘要均为本地推理，程序自身零外部网络请求。

**一期状态**：W1—W8 收口，SAD 端点清单 30/30 全清，回归 2266 passed / 0 failed。

---

## 目录

- [核心特性](#核心特性)
- [技术架构](#技术架构)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [转写引擎](#转写引擎)
- [主密钥与加密](#主密钥与加密)
- [硬约束（改代码前必读）](#硬约束改代码前必读)
- [文档索引](#文档索引)
- [许可](#许可)

---

## 核心特性

| 特性 | 说明 |
|---|---|
| **全离线** | 转写（FunASR/Paraformer）与摘要（Ollama 本地模型）均在本机推理；内置合规自检，可检出任何真实外发连接 |
| **公文合规** | 导出严格对照 GB/T 9704—2012：字体、字号、行距、首行缩进、页码域、中英混排与引号规范 |
| **可溯源** | 纪要结论可回指记录稿原文；审计日志采用链式哈希，只增不改，支持检索与导出 |
| **逐条校订** | 转写稿按"需复核比例"自动分流，人工逐条接受 / 修改，改动全程留痕 |
| **加密存储** | 音频与文稿以 AES-256-GCM 静态加密，主密钥优先托管到操作系统凭据管理器 |
| **三道门鉴权** | 无登录体系，靠"只听回环 + 启动令牌 + 关闭接口文档"三道门限制调用来源 |
| **断点续跑** | 基于状态机编排，闸口决策、幂等副作用，进程被杀后可恢复 |

---

## 技术架构

### 技术栈

| 层 | 选型 |
|---|---|
| 桌面壳 | Electron 30 |
| HTTP API | FastAPI + uvicorn |
| 流程编排 | LangGraph |
| 配置 | Pydantic v2 + PyYAML |
| 存储 | SQLite |
| 加密 | cryptography（AES-256-GCM）+ keyring |
| 文档导出 | python-docx |
| 语音转写 | FunASR / Paraformer（本地） |
| 文本摘要 | Ollama（本地 LLM） |
| 音频处理 | ffmpeg / ffprobe |

### 分层

```
┌─────────────────────────────────────────────┐
│  表现层   src/desktop/        Electron 桌面壳  │
├─────────────────────────────────────────────┤
│  应用层   src/mmwf/api/       路由与鉴权        │
│          src/mmwf/graph/     状态机编排        │
│          src/mmwf/app/       服务编排          │
├─────────────────────────────────────────────┤
│  领域层   src/mmwf/domain/    纯 Python，禁框架 │
├─────────────────────────────────────────────┤
│  适配层   src/mmwf/adapters/  ASR / LLM / 音频 │
├─────────────────────────────────────────────┤
│  基础设施 src/mmwf/infra/     DB/存储/加密/审计 │
└─────────────────────────────────────────────┘
```

**分层红线**：依赖只能由外向内单向流动，领域层不得 import 任何框架
（见[硬约束](#硬约束改代码前必读) C01，由 `tools/check_layers.py` 强制检查）。

---

## 目录结构

```
mmwf/
├── config/                  # 全局配置（可热改，无需改代码）
│   ├── app.yaml            # 应用级（host 锁定回环）
│   ├── models.yaml         # 模型与导出配置
│   ├── extraction.yaml     # 分块与归并参数
│   ├── thresholds.yaml     # 阈值
│   ├── glossary.csv        # 术语库
│   └── templates/          # 四类会议模板
├── src/mmwf/
│   ├── settings.py         # 配置中心
│   ├── main.py             # FastAPI 装配 + 三道门 + 异常处理器 + 启动入口
│   ├── api/                # 应用层：路由与鉴权（31 个端点）
│   ├── graph/              # 应用层：状态机编排（节点 / 闸口 / 幂等 / 运行器）
│   ├── app/services/       # 应用层：投递 / 术语 / 复核 / 导出服务
│   ├── domain/             # 领域层：纯 Python（错误码 / 抽取 / 术语 / 复核 / docx）
│   ├── adapters/           # 适配层：ASR / LLM / ffmpeg
│   └── infra/              # 基础设施：DB / 加密 / 文件仓储 / 审计 / 迁移
├── src/desktop/            # 表现层：Electron（主进程 + 渲染层）
├── tests/                  # 单元 / 集成 / 契约 + 离线夹具
├── tools/                  # 检查与验收脚本
└── docs/                   # 施工卡与工程记录
```

---

## 快速开始

**环境要求**：Python 3.11 – 3.13；Node.js 18+（仅桌面壳需要）。

```bash
# ① 建虚拟环境
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# ② 装依赖（按需选择 extra）
pip install -e ".[dev,infra]"   # 基础 + 开发测试
pip install -e ".[docx]"        # 导出 GB/T 9704 公文 docx
pip install -e ".[graph]"       # 状态机编排
pip install -e ".[api]"         # FastAPI + uvicorn
pip install -e ".[keystore]"    # 强烈建议：主密钥托管到 OS 凭据管理器

# ③ 建库（首次，幂等）
python tools/migrate.py

# ④ 跑测试
pytest tests/unit -v

# ⑤ 跑施工卡验收（逐张，可独立运行）
python tools/accept_t1_11.py    # API 三道门 / 统一信封 / 正常流程闭环
python tools/accept_t1_13.py    # 合规自检：真外发检出 / 代理告警
```

**桌面壳**：

```bash
cd src/desktop
npm install                     # 装 Electron（首次）
npm start                       # 起桌面壳（自动拉起 Python 后端）
```

**辅助工具**：

```bash
python tools/verify_docx_style.py 输出.docx   # docx 版式逐项校验
python tools/verify_audit_chain.py --db data/mmwf.db   # 审计链完整性
python tools/check_layers.py                  # 分层依赖红线
python tools/check_deps.py                    # 依赖白名单
```

---

## 转写引擎

`funasr` / `torch` 体积达数 GB，**默认不安装**。适配器、契约测试与验收脚本
均可在**没有 funasr** 的机器上跑通（走离线夹具 `tests/fixtures/asr/fake_engine.py`）：

```bash
pytest tests/unit/test_asr_funasr.py tests/contract/test_asr_contract.py -q
```

装好真实引擎后：

```bash
pip install -e ".[asr]"         # funasr + torch
# 按部署手册预先离线下载模型到 models/funasr/paraformer-zh/
pytest tests/contract -q -m real_model
```

> **换引擎只改 `config/models.yaml` 的 `asr.provider`，不动任何 Python 代码**：
> 新引擎 = 新文件 + 在 `adapters/asr/__init__.py` 的 `_REGISTRY` 里注册一行。

---

## 主密钥与加密

| | 装了 `keyring` | 未装 |
|---|---|---|
| 密钥后端 | 操作系统凭据管理器 | `data/.master.key`（明文） |
| `secure` 标志 | `True` | `False` |
| 适用场景 | 正式部署 | 仅开发 / 演示 |
| 额外产物 | 无 | `KEY_STORAGE_UNHARDENED.txt` 警示文件 |

程序会**如实标注**当前档位（`keystore.describe_key_source()`），
不会把明文存储伪装成安全方案。

---

## 硬约束（改代码前必读）

来自《架构设计说明书》§14，**违反任一条不予合并**：

| # | 约束 | 检查方式 |
|---|---|---|
| C01 | 领域层不得 import fastapi / sqlite3 / httpx / torch / funasr / ollama / docx / psutil | `python tools/check_layers.py` |
| C09/C10 | 不得引入 SenseVoice、Whisper-large-v3、任何云 SDK、遥测组件 | `python tools/check_deps.py` |
| C11 | 服务只允许监听 `127.0.0.1`，禁止 `0.0.0.0` | `AppConfig` 校验 + 源码扫描 + 启动断言 |

> 全部约束条目与逐条踩坑记录，见 [`docs/施工日志与踩坑实录.md`](docs/施工日志与踩坑实录.md)。

---

## 文档索引

### 设计文档（`docs/设计文档/`）

| # | 文档 | 解决的问题 |
|:--|:--|:--|
| 00 | [文档总索引](docs/设计文档/00-README-文档总索引.md) | 全套文档的阅读顺序 |
| — | [调研报告](docs/设计文档/会议录音转纪要工作流调研报告.md) | 上游依据：为什么现成工具不够用 |
| — | [产品需求文档 PRD](docs/设计文档/会议纪要自动化工作台PRD.md) | 做什么（功能需求与验收标准） |
| 01 | [架构设计说明书](docs/设计文档/01-架构设计说明书.md) | 为什么这么设计（SAD，约 2.1 万字） |
| 02 | [开发施工文档](docs/设计文档/02-开发施工文档.md) | 每一行代码写在哪、怎么写（约 3.4 万字） |
| 03 | [测试与验收方案](docs/设计文档/03-测试与验收方案.md) | 怎么测、测到什么程度算过（约 2.3 万字） |
| 04 | [部署运维与保密合规手册](docs/设计文档/04-部署运维与保密合规手册.md) | 怎么装、保密检查怎么过（约 2.2 万字） |

### 工程记录（`docs/`）

| 文档 | 位置 |
|---|---|
| 施工日志与踩坑实录（111 KB，含全部铁律与缺陷复查） | `docs/施工日志与踩坑实录.md` |
| 各施工卡（开工前的规格沉淀） | `docs/T1-*-施工卡.md`、`docs/T2-*-施工卡.md` |
| 一期验收记录 | `docs/T1-17-一期验收记录.md` |
| 部署目录手工检查指引 | `docs/T1-18-交付目录手工检查指引.md` |

> 本仓库 README 与 `docs/` 中的 `SAD §x`、`PRD §x` 等引用，均指向上述设计文档。

---

## 许可

本项目采用 [MIT 许可证](LICENSE)。
