-- 0020：批注、闭环动作与管理员授权的存储面（M4-B spec §3，D121–D139）。
--
-- 1) `alerts` 的六列都是**可空**。它们描述的是「人对这条告警做了什么」：
--    `acknowledged_at` 是看过，`action_kind/action_ref` 是处置到了哪，`dismiss_code`
--    是忽略原因的结构化部分，`deferred_until` 是「到点再来」，`thesis_id` 是规则经
--    观点展开时留下的快照。旧行没有这些信息，回填任何值都是伪造历史。
--
-- 2) `alert_events` 是写多读少的审计表：`alerts` 上的列是**当前结论**，这张表是
--    「谁在什么时候把它从什么改成了什么」。没有它，undo 找不到上一态，复盘也答不出
--    「先已读后处置」与「直接处置」的区别。动作字符串与仓库层 `ACTIONS` 同一组。
--
-- 3) `notify_log.context_json` 记这一次发送实际带入了哪几条批注（计划 3 写入）。
--
-- 4) `annotations.target_label` 是创建时的显示名快照：标的改名、规则删除、成交页
--    不可达之后，批注列表仍能说清「这是关于什么的」。DuckDB 不支持
--    `ADD COLUMN ... NOT NULL DEFAULT ...`（0019 的实测），所以照旧走
--    可空列 → SET DEFAULT → 回填三步；读侧仍用 coalesce 兜底。
--
-- 5) `annotation_attachments` 与 `annotations.attachments_json` 并存：旧列保持写
--    `'[]'`，不删（迁移只增不改）；附件的唯一出处是新表。
--
-- 6) `app_settings` 是运行时策略（D127）。写入键必须有白名单（服务层），这里只给
--    唯一一个首期策略一个默认值 `on`。
--
-- 7) `admin_access_log` 既是 break-glass 的审计，也是授权与限次的唯一依据：
--    授权时写一行，`granted_until` 决定该条是否仍可读；不存正文与附件内容。

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS action_kind VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS action_ref VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS dismiss_code VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS thesis_id VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS deferred_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS alert_events (
    id          VARCHAR PRIMARY KEY,
    alert_id    VARCHAR NOT NULL,
    user_id     VARCHAR NOT NULL,
    action      VARCHAR NOT NULL,
    from_status VARCHAR NOT NULL,
    to_status   VARCHAR NOT NULL,
    note        VARCHAR,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert
    ON alert_events (alert_id, ts);

ALTER TABLE notify_log ADD COLUMN IF NOT EXISTS context_json VARCHAR;

ALTER TABLE annotations ADD COLUMN IF NOT EXISTS target_label VARCHAR;
ALTER TABLE annotations ALTER COLUMN target_label SET DEFAULT '';
UPDATE annotations SET target_label = '' WHERE target_label IS NULL;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_by VARCHAR;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;

ALTER TABLE annotation_revisions ADD COLUMN IF NOT EXISTS meta_json VARCHAR;
ALTER TABLE annotation_revisions ALTER COLUMN meta_json SET DEFAULT '{}';
UPDATE annotation_revisions SET meta_json = '{}' WHERE meta_json IS NULL;

CREATE TABLE IF NOT EXISTS annotation_attachments (
    id            VARCHAR PRIMARY KEY,
    annotation_id VARCHAR NOT NULL,
    user_id       VARCHAR NOT NULL,
    filename      VARCHAR NOT NULL,
    media_type    VARCHAR NOT NULL,
    size_bytes    BIGINT NOT NULL,
    sha256        VARCHAR NOT NULL,
    storage_path  VARCHAR NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_annotations_target
    ON annotations (target_type, target_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_annotations_user
    ON annotations (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_annotation_attachments_annotation
    ON annotation_attachments (annotation_id, position);

CREATE TABLE IF NOT EXISTS app_settings (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR
);
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('annotations.team_sharing', 'on');

CREATE TABLE IF NOT EXISTS admin_access_log (
    id            VARCHAR PRIMARY KEY,
    admin_user_id VARCHAR NOT NULL,
    annotation_id VARCHAR NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    action        VARCHAR NOT NULL DEFAULT 'break_glass',
    reason        VARCHAR NOT NULL,
    granted_until TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_access_admin
    ON admin_access_log (admin_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_admin_access_annotation
    ON admin_access_log (annotation_id, created_at);
