-- =============================================================================
-- mmwf 初始建库脚本  001_init.sql
-- =============================================================================
-- 来源：《架构设计说明书》§8.3（原样落地，勿擅改字段名与 CHECK 取值）。
--
-- 执行约定
--   1. 本文件由 migrations/runner.py 在事务内执行；文件内**不写** BEGIN/COMMIT。
--   2. runner 执行前已设置 PRAGMA foreign_keys=ON，否则下列 FOREIGN KEY 全是摆设。
--   3. 本文件必须**幂等**：全部使用 CREATE TABLE IF NOT EXISTS，
--      使 runner 重复调用不会因"表已存在"而崩。
--   4. 每个迁移文件对应一个整数版本号（001 -> user_version = 1）。
--      runner 只执行 user_version 之后的迁移，已应用的不会重跑。
--
-- 约束对应关系
--   C12  审计日志只可追加，不可改不可删  -> audit_log_no_update / audit_log_no_delete 触发器
--   F3.7 议定事项责任人缺失时强制填"暂无" -> resolution.owner / action_item.owner 默认值
-- =============================================================================


-- ① 会议主表 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meeting (
    meeting_id      TEXT PRIMARY KEY,              -- uuid4
    title           TEXT NOT NULL,
    meeting_time    TEXT,                          -- ISO8601
    location        TEXT,
    host            TEXT,                          -- 主持人
    attendees       TEXT,                          -- JSON 数组
    absentees       TEXT,                          -- 列席人，JSON 数组
    recorder        TEXT,                          -- 记录人
    classification  TEXT NOT NULL DEFAULT 'internal'
                    CHECK (classification IN ('public','internal','work_secret','state_secret')),
    template_id     TEXT,                          -- 关联纪要模板
    status          TEXT NOT NULL DEFAULT 'created',
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (template_id) REFERENCES template(template_id)
);
CREATE INDEX IF NOT EXISTS idx_meeting_status ON meeting(status);
CREATE INDEX IF NOT EXISTS idx_meeting_time   ON meeting(meeting_time);


-- ② 音频文件 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audio_file (
    audio_id        TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    original_path   TEXT NOT NULL,
    format          TEXT NOT NULL,
    duration_ms     INTEGER NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id)
);
CREATE INDEX IF NOT EXISTS idx_audio_meeting ON audio_file(meeting_id);


-- ③ 转写分段（核心：置信度 + 时间戳） ----------------------------------------
CREATE TABLE IF NOT EXISTS transcript_segment (
    segment_id      TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    audio_id        TEXT,
    seq             INTEGER NOT NULL,              -- 段序
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    text            TEXT NOT NULL,
    confidence      REAL,                          -- 0..1，NULL 表示引擎未提供
    speaker         TEXT,                          -- 一期为 NULL
    speaker_conf    REAL,
    flags           TEXT,                          -- JSON: ["low_conf","silence","overlap"]
    reviewed        INTEGER NOT NULL DEFAULT 0,    -- 0/1
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id)
);
CREATE INDEX IF NOT EXISTS idx_seg_meeting_seq ON transcript_segment(meeting_id, seq);
-- 低置信度段是校订阶段的主要工作对象，单独建部分索引（只索引 <0.7 的行，体积小）
CREATE INDEX IF NOT EXISTS idx_seg_lowconf     ON transcript_segment(meeting_id, confidence)
    WHERE confidence < 0.7;


-- ④ 议题 --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topic (
    topic_id        TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    name            TEXT NOT NULL,
    discussion      TEXT,                          -- 讨论要点
    source_refs     TEXT NOT NULL,                 -- JSON: [{segment_id,start_ms,end_ms,quote}]
    confidence      REAL,
    decision_state  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (decision_state IN ('pending','approved','rejected','edited')),
    edited_by       TEXT,
    edited_at       TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_meeting_seq ON topic(meeting_id, seq);


-- ⑤ 议定事项（粒度控制的主战场） --------------------------------------------
CREATE TABLE IF NOT EXISTS resolution (
    resolution_id   TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    topic_id        TEXT,
    seq             INTEGER NOT NULL,
    content         TEXT NOT NULL,                 -- 事项内容
    owner           TEXT NOT NULL DEFAULT '暂无',   -- 责任人，无则填"暂无"（F3.7 强制）
    deadline        TEXT,                          -- 时限
    source_refs     TEXT NOT NULL,                 -- JSON
    confidence      REAL,
    decision_state  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (decision_state IN ('pending','approved','rejected','edited')),
    edited_by       TEXT,
    edited_at       TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id),
    FOREIGN KEY (topic_id)   REFERENCES topic(topic_id)
);
CREATE INDEX IF NOT EXISTS idx_resolution_meeting_seq ON resolution(meeting_id, seq);


-- ⑥ 待办事项 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS action_item (
    action_id       TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    topic_id        TEXT,
    task            TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '暂无',
    due_at          TEXT,
    source_refs     TEXT NOT NULL,
    decision_state  TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id),
    FOREIGN KEY (topic_id)   REFERENCES topic(topic_id)
);
CREATE INDEX IF NOT EXISTS idx_action_meeting ON action_item(meeting_id);


-- ⑦ 待决策事项 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_decision (
    decision_id     TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    topic_id        TEXT,
    content         TEXT NOT NULL,
    required_from   TEXT,                          -- 需谁决策
    source_refs     TEXT NOT NULL,
    decision_state  TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id),
    FOREIGN KEY (topic_id)   REFERENCES topic(topic_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_meeting ON pending_decision(meeting_id);


-- ⑧ 纪要版本 ----------------------------------------------------------------
-- ⚠️⚠️ **一期弃用（T1-17 缺陷 25 标注）—— 建表保留，但不写不读。**
--
-- 现状（一期收口时实测）：`grep -rn "minutes_version" src/` 只命中
-- 本文件的建表语句与 `api/routes.py` 的一条注释，**全工程的写入数为 0、读取数为 0**。
-- 也就是说：这是一张**死表**。
--
-- 为什么保留而不是删掉：
--   1. 删表是**不可逆的 schema 变更**。已交付的 `mmwf.db` 可能已经建过它，
--      删除语句在别人的库上跑会失败或造成数据损坏，风险远大于收益。
--   2. 本表结构本身**没问题**，只是**一期没有需求牵引**。
--
-- 为什么一期不用它：
--   一期的"纪要版本"由**文件**承载 —— `record/vN.md`（记录稿版本）
--   与 `export/vN.docx`（导出件）都在 filestore 的 manifest 里有登记，
--   `GET /meetings/{id}/record`、`GET /meetings/{id}/minutes`、`GET /meetings/{id}/diff`
--   全部从文件读，行为已被 T1-08/T1-11/T1-16 验收锁定。
--   此刻把读取来源切到这张表，等于**重开已闭的卡**，回归风险远大于收益。
--
-- 什么时候启用：**二期 T2-04（后置校验与问题清单）**。
--   那里有明确需求牵引（F7.2「模板版本管理：修改后 version 递增，历史版本可查」），
--   届时"结构化快照 + Markdown 快照"才有真正的写入方（生成节点）与消费方（校验/比对）。
--   ⚠️ 启用时**不要再新建表**，直接用这一张。
CREATE TABLE IF NOT EXISTS minutes_version (
    version_id      TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    structured_json TEXT NOT NULL,                 -- 结构化数据快照
    markdown        TEXT NOT NULL,                 -- 渲染后 Markdown
    is_final        INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    note            TEXT,
    UNIQUE (meeting_id, version_no),
    FOREIGN KEY (meeting_id) REFERENCES meeting(meeting_id)
);


-- ⑨ 术语库 ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS glossary_entry (
    entry_id        TEXT PRIMARY KEY,
    category        TEXT NOT NULL
                    CHECK (category IN ('org','person','term','codename')),
    canonical       TEXT NOT NULL,                 -- 规范写法
    aliases         TEXT,                          -- JSON 数组，易错写法
    note            TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL,
    UNIQUE (category, canonical)
);


-- ⑩ 纪要模板 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS template (
    template_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    meeting_kind    TEXT NOT NULL,                 -- 党组会/专题会/协调会/例会
    schema_json     TEXT NOT NULL,                 -- 必填要素与层级定义
    style_json      TEXT NOT NULL,                 -- 版式参数（行距/边距/字体档）
    version_no      INTEGER NOT NULL DEFAULT 1,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE (name, version_no)
);


-- ⑪ 任务状态（与 LangGraph 检查点联动） -------------------------------------
CREATE TABLE IF NOT EXISTS task_state (
    task_id         TEXT PRIMARY KEY,              -- == meeting_id（一期一对一）
    current_node    TEXT NOT NULL,
    status          TEXT NOT NULL,
    awaiting_actor  TEXT,                          -- 卡在哪个闸口等谁
    awaiting_since  TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_state_status ON task_state(status, updated_at);


-- ⑫ 幂等记录 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS idempotency_record (
    idem_key        TEXT PRIMARY KEY,              -- sha256(task_id:node_id:input_hash)[:32]
    task_id         TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    result_ref      TEXT,                          -- 产物路径/ID
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_task ON idempotency_record(task_id, node_id);


-- ⑬ 审计日志（追加写，禁止 UPDATE/DELETE） ----------------------------------
-- 注意：AUTOINCREMENT 只在 INTEGER PRIMARY KEY 上生效，写成 BIGINT 会静默失效。
-- 保留 AUTOINCREMENT 是为了让 log_id 严格单调、不复用（普通 rowid 在删行后会复用）。
CREATE TABLE IF NOT EXISTS audit_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    operator        TEXT NOT NULL,
    action          TEXT NOT NULL,                 -- login/import/export/edit/approve...
    target_type     TEXT,                          -- meeting/segment/minute/...
    target_id       TEXT,
    detail          TEXT,                          -- JSON
    result          TEXT NOT NULL,                 -- success/failure
    prev_hash       TEXT,                          -- 链式哈希，防篡改
    row_hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_oper ON audit_log(operator, ts);


-- =============================================================================
-- 审计日志防篡改触发器（SAD C12 / 铁律：审计日志只可追加）
-- =============================================================================
-- 机制：row_hash = sha256(prev_hash + ts + operator + action + target_id + detail + result)
--       任何一行被改写，从该行起的链式哈希全部失效，校验脚本即可定位。
--       触发器是第一道闸（数据库层直接拒绝），链式哈希是第二道闸（离线可验证）。
--
-- 为什么要两道：触发器能被 DROP TRIGGER 绕过（管理员权限），
--       而链式哈希把证据留在数据本身里，即便表被外部工具改动也能事后举证。
-- =============================================================================

DROP TRIGGER IF EXISTS audit_log_no_update;
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

DROP TRIGGER IF EXISTS audit_log_no_delete;
CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
