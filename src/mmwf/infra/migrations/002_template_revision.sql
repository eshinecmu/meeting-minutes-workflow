-- T2-02 方案 A：只保存 DB 自建模板的完整历史，不灌入 builtin。
CREATE TABLE IF NOT EXISTS template_revision (
    template_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    snapshot_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (template_id, version_no)
);

-- 仅保存迁移时仍存在的当前版；旧正文已丢失，不伪造 v1。
-- 字段对应 template_from_row().to_dict()，JSON 子对象保持原类型。
INSERT INTO template_revision (template_id, version_no, snapshot_json, captured_at)
SELECT t.template_id, t.version_no,
    json_object(
        'template_id', t.template_id,
        'name', t.name,
        'meeting_kind', coalesce(nullif(json_extract(t.schema_json, '$.meeting_kind'), ''), t.meeting_kind, ''),
        'version_no', t.version_no,
        'enabled', json(CASE WHEN t.enabled THEN 'true' ELSE 'false' END),
        'source', 'db',
        'required_elements', json_extract(t.schema_json, '$.required_elements'),
        'section_order', json_extract(t.schema_json, '$.section_order'),
        'style', json(CASE WHEN json_type(coalesce(nullif(t.style_json, ''), '{}')) = 'object'
            THEN coalesce(nullif(t.style_json, ''), '{}') ELSE '{}' END)
    ), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM template AS t
WHERE NOT EXISTS (
    SELECT 1 FROM template_revision AS r
    WHERE r.template_id = t.template_id AND r.version_no = t.version_no
);
