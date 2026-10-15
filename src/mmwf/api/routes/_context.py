"""路由依赖容器。

★ **这个文件是整个拆包的枢纽**。

原 `build_router` 用**闭包**捕获 `cfg` / `db_path` / 八个服务工厂 /
会议锁，30 个端点都靠闭包访问它们。正是这一点把 `build_router`
撑到了 1865 行 —— 端点一旦搬出闭包，这些东西就**全都访问不到**，
所以它们只能全部挤在同一个函数里。

`Runtime` 把闭包捕获物**显式化**成实例属性与方法。搬出之后：

    _ingest_service(meeting_id)   ->  rt.ingest_service(meeting_id)
    db_path                       ->  rt.db_path
    cfg                           ->  rt.cfg

于是每个端点模块只要 `def register(router, rt)` 就能拿到全部依赖，
不必再挤在一个函数里。

⚠️ **服务一律现构造、不缓存**（沿用原实现的判断，理由见各方法注释）：
`GraphRunner` 的 `SqliteSaver` 连接不能跨线程共用，缓存单例会在
FastAPI 的线程池里偶发 `ProgrammingError`。
"""

from __future__ import annotations

import threading
from typing import Any

from mmwf.infra.audit import AuditLogger
from mmwf.settings import Settings

from ._support import DEFAULT_OPERATOR, _config_dir


class Runtime:
    """一次 `build_router` 调用对应的运行时依赖。

    生命周期跟着 app（`build_router` 每次调用新建），进程退出即释放。
    """

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        #: 业务库路径。检查点与业务表共用同一份 `mmwf.db`，
        #: 但用的是 langgraph 自己的 checkpoint/writes 表。
        self.db_path = cfg.resolve_path(cfg.app.data_dir) / "mmwf.db"

        #: 每个会议一把**进程内**互斥锁。
        #:
        #: ⚠️⚠️ 这是 T1-11 Review 补的**最严重的一个缺陷**，值得单独说明。
        #:
        #: 症状：整条闭环跑不通，但报错是 **E6001（会话令牌失效）** ——
        #: 一个指向完全错误方向的错误码。
        #:
        #: 根因链条：
        #:   ① FastAPI 把 `def`（非 `async def`）端点丢进**线程池**跑，
        #:      所以流水线的节点函数是在**工作线程**里执行的；
        #:   ② `AuditLogger` 按需（`kind=thread`）缓存 sqlite 连接，
        #:      缓存键是 `threading.get_ident()` —— 于是每个工作线程
        #:      各自开一条连接，这个设计本身是对的；
        #:   ③ 但 sqlite3 的**默认 `check_same_thread=True`** 只允许
        #:      "创建连接的线程"使用它。某条线程复用了**另一条已死线程**
        #:      留下的缓存条目（线程 id 被复用）→
        #:      `ProgrammingError: SQLite objects created in a thread can
        #:      only be used in that same thread`；
        #:   ④ 这个异常在节点里**吞掉**（节点不该因审计写失败而崩），
        #:      于是审计链缺了一条 `start` 记录；
        #:   ⑤ `verify_token` 校验的是**审计链的完整性**（见 `deps.verify_token`：
        #:      "本机尚未生成会话令牌" 就是链校验失败时的文案）→ 链断了 →
        #:      每个后续请求都被判 **E6001**。
        #:
        #: 为什么"每次请求新建 runner + close()"没能防住：`GraphRunner.app`
        #: 是**惰性编译**的，`_saver` 只在第一次访问 `.app` 时才创建。
        #: 所以刚 new 出来、还没跑过的 runner，其 `close()` 是**空操作**
        #: （`self._saver is None` 直接返回）—— 也就是 `GET /meetings/{id}`
        #: 和 `GET /meetings` 这两个"只想读一眼状态"的端点，**完全没释放
        #: 连接**，全靠 GC。连接泄漏到一定程度才开始串线程。
        #:
        #: 修法：序列化**同一会议**的流水线操作。跨会议依然并发，
        #: 不牺牲吞吐；而"一次会议同时被两个请求推进"本来也是不该允许的
        #: 语义（G1~G4 的闸口决策天然要串行）。
        self._meeting_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    #

    def meeting_lock(self, meeting_id: str) -> threading.RLock:
        """取（必要时新建）某个会议的互斥锁。

        `threading.Lock` 保护字典本身：`Runtime` 会被多个工作线程共享，
        字典的读改写不是一个原子操作，并发首次访问同一个 `meeting_id`
        时可能造出两把不同的锁。
        """
        with self._locks_guard:
            lock = self._meeting_locks.get(meeting_id)
            if lock is None:
                lock = threading.RLock()
                self._meeting_locks[meeting_id] = lock
            return lock

    # -- 服务构造 ------------------------------------------------------

    def ingest_service(self, meeting_id: str):
        """构造 `IngestService`。

        ⚠️⚠️ **`make_ingest_service` 只收一个位置参数（`settings`）**
        —— 第一版写成 `make_ingest_service(meeting_id, self.cfg)`，
        把 `meeting_id` 当第一个位置参数、`self.cfg` 当第二个。

        后果（T1-15 Review 补的**第 19 个缺陷**，也是本项目最严重的一个）：
        **`POST /meetings` 一给 `audio_paths` 就 E9001**。
        即"投递音频"——本产品的**主入口**——从 T1-11 起一直是坏的。

        为什么之前没人发现：
          · `make_ingest_service(settings, *, adapter, probe_only)` 的
            第一个参数就叫 `settings`，传 `meeting_id`（一个字符串）进去
            **不会在构造时炸**，只有在 `self.cfg` 这个多余的位置参数上才炸；
          · 更要命的是它炸在 `TypeError`，被全局兜底处理器收成 **E9001**，
            界面上只是一句"程序遇到一个未预期的问题"，
            指向完全错误的方向；
          · 前几张卡的验收脚本走的是**文字稿路径**（`record_markdown`），
            音频路径只有"格式不对"这类**在 `ingest_one` 之前**就被拦下的用例，
            所以从没走到这一行。

        ⚠️ **`meeting_id` 在这里其实没用**：`IngestService` 是**无状态**的，
        它不绑会议（落盘的 `chunk_dir` 由调用方给）。所以这里
        **只传 `self.cfg`**，`meeting_id` 保留在签名里是为了让调用点读起来
        仍然表达"给这个会议接音频"的意图，也免得将来真要绑会议时改所有调用点。
        """
        from mmwf.app.services.ingest import make_ingest_service

        return make_ingest_service(self.cfg)

    def export_service(self, meeting_id: str):
        from mmwf.app.services.export import make_export_service

        return make_export_service(meeting_id, self.cfg)

    def review_service(self, meeting_id: str):
        """构造 `ReviewService`（记录稿读取用）。

        只用于 `GET .../record`；记录稿的**生成**走 `review` 节点，
        不在 API 层做 —— 否则同一件事会有两个出处。
        """
        from mmwf.app.services.review import make_review_service

        return make_review_service(meeting_id, self.cfg)

    def glossary_service(self, operator: str = DEFAULT_OPERATOR):
        """构造 `GlossaryService`（T1-14 B 档）。

        ⚠️⚠️ **必须注入 `AuditLogger`**，否则写操作会打 warning 且
        **完全不留痕**。而 `glossary_change` 是
        `MINIMUM_CHECK_ITEM_ACTIONS` 里"术语库/模板变更"这一类的
        **唯一动作** —— 不留痕 = **AUD-08 缺项**，那是红线。

        ⚠️ `operator` 也必须传。审计的核心是"谁做的"，
        默认值 `"system"` 只在命令行/验收脚本不传时才落到。

        ⚠️ **术语库不绑会议**（与 glossary 的既有设计一致）：
        术语库是全局资产，不归属任何一次会议。
        """
        from mmwf.app.services.glossary import make_glossary_service

        return make_glossary_service(
            self.cfg,
            db_path=self.db_path,
            audit=AuditLogger(self.db_path),
            operator=operator,
        )

    def transcript_store(self, meeting_id: str, *, audit: AuditLogger | None = None):
        """构造 `TranscriptStore`（T1-15）。

        ⚠️⚠️ **为什么不直接在端点里 `connect_ctx` 写 SQL**：
        `tools/check_layers.py` 的 `FORBIDDEN_IN_API` **含 `sqlite3`**，
        API 层不许直接读写 `transcript_segment`。而且段落的校验规则
        （`reviewed`/`version`/`flags` 怎么动）必须只有**一个**出处 ——
        T1-14 单列 `transcript_store.py` 就是为了这个。

        ⚠️ **写操作必须传 `audit`**。`TranscriptStore` 在没注入
        `AuditLogger` 时**只打 warning、完全不留痕** ——
        而"逐段改动"是 `MINIMUM_CHECK_ITEM_ACTIONS` 的最低检查项之一，
        不留痕就是红线。只读调用（列转写稿）**不传**，
        免得为一次列表各建一个写连接。
        """
        from mmwf.infra.transcript_store import make_transcript_store

        return make_transcript_store(meeting_id, self.db_path, audit=audit)

    def template_store(self):
        """构造 `TemplateStore`（T1-16）。

        ⚠️ 理由与 `transcript_store` 完全相同：`sqlite3` 是
        `FORBIDDEN_IN_API` 成员，API 层不许直接读写 `template` 表。
        模板的解析规则（哪些字段必填、`version_no` 怎么自增）
        必须只有**一个**出处 —— 那就是 `domain/template.py` +
        `infra/template_store.py`。

        ⚠️ **不缓存实例**：模板是全局资源（不绑会议），缓存看着安全，
        但 `list_templates()` 每次都要**重新读 yaml 目录**（N5.2 热改），
        缓存 store 省下的那点开销远小于"改了 yaml 没反应"的排查成本。

        ⚠️ `config_dir` 取自 `self.cfg` —— 内置模板目录是 `config/templates/`。
        传进去的是 `config` 那一级（store 内部再拼 `templates/`），
        这样将来若把别的配置文件也搬进来，不用改接口。
        """
        from mmwf.infra.template_store import make_template_store

        return make_template_store(self.db_path, config_dir=_config_dir(self.cfg))

    def filestore(self, meeting_id: str):
        """构造某个会议的文件仓储（T1-16 下载端点用）。

        ⚠️ **必须按会议构造**：`make_filestore(meeting_id, data_dir)`
        会把根目录定位到 `{data_dir}/{meeting_id}/`，`export/` 区就在里面。
        构造时传错会议 = 读到别人的文件目录。

        ⚠️ **不在端点里拼磁盘路径**：`export/` 是明文区、其余是加密区，
        这个区别由 `FileStore` 在 `read()` 里透明处理。
        端点若绕过仓储直接 `open()`，加密区就读不出来（或者读到密文）。
        """
        from mmwf.infra.filestore import make_filestore

        return make_filestore(meeting_id, self.cfg.resolve_path(self.cfg.app.data_dir))

    def runner(self, meeting_id: str):
        """构造 GraphRunner。

        ⚠️ **每次请求新建实例**，不缓存成模块级单例。
        原因见 T1-10 坑 4：`SqliteSaver` 的底层 sqlite3 连接
        不能跨线程共用。FastAPI 的同步端点跑在线程池里，
        缓存单例意味着多个请求共用一个连接 → 偶发
        `ProgrammingError: SQLite objects created in a thread...`。
        每次新建 + `close()` 才是安全的。

        ⚠️ **必须传 `checkpoint_path`**（T1-11 Review 补的严重缺陷）。
        第一版漏了它 → `GraphRunner.app` 走 `checkpointer=None` 分支 →
        图**完全没有持久化**：`start()` 抛 `ValueError: No checkpointer set`，
        `status()` / `values()` 也读不到任何东西。
        后果是**闸口这条主线整个是废的**（`/start`、`/topics`、
        `/minutes`、`/gate/*/decide` 四个端点全部 E9001）。
        检查点与业务表共用 `mmwf.db`，但用的是 langgraph 自己的
        checkpoint/writes 表（见 `make_checkpointer`）。

        ⚠️⚠️ **`deps` 也必须传，而且这是本项目最隐蔽的一个坑**
        （T1-11 Review 补的严重缺陷）。

        `Deps()` 的字段默认全是 `None`，各节点的 `_resolve()` 在
        `deps.settings is None` 时会退回 **`settings.get_settings()`** ——
        那是**从磁盘 config/ 重新加载的一份配置**，与 `create_app(cfg)`
        拿到的那份**是两个对象**。

        后果（不是"参数不一致"这么轻）：**API 层和流水线读写不同的数据目录。**
        本机实测：`create_app` 注入的 `data_dir` 是临时目录，
        而节点拿到的 `get_settings()` 指向项目**真实**的 `data/`。
        于是 `/start` 里 API 刚把 `transcript/segments.json` 写进临时目录，
        节点却在真实目录里找它 → `ReviewServiceError: 转写分段不存在`
        → 被包成 **E9001，整条闭环一步都走不动**。

        更糟的是它在**生产上不报错**（两者恰好都是同一个 `data/`），
        只在测试/多实例/换了配置目录时炸 —— 属于"测试环境才暴露、
        且症状指向完全错误方向"的那一类。

        修法：把 `self.cfg` 显式塞进 `Deps`，让"用哪份配置"只有**一个**答案。
        """
        from mmwf.graph.build import Deps
        from mmwf.graph.runner import GraphRunner

        deps = Deps(settings=self.cfg)
        if self.cfg.extraction.engine == "llm":
            from mmwf.adapters.llm import make_llm_client
            from mmwf.graph.nodes.extraction import make_llm_extractor

            # 仅装配，不探测服务；模型调用延迟到 extract 节点执行。
            client = make_llm_client(self.cfg)
            deps.extractor = make_llm_extractor(client, self.cfg)

        return GraphRunner(
            meeting_id,
            deps=deps,
            checkpoint_path=self.db_path,
            audit=AuditLogger(self.db_path),
        )
