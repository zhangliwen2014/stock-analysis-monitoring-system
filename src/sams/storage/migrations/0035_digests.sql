-- 0035_digests.sql — 每日收盘摘要（S1-C）
--
-- 一条 `(user_id, market, as_of)` 只写一次：摘要是当晚的快照，不是视图。
-- 重跑返回已有那一行，不覆盖正文、不重推——唯一键就是幂等本身。
--
-- 投递结局写回同一行（`pushed` / `delivery_outcome` / `delivery_error`），
-- 不另开 `notify_log`：那一张表的每一行都要挂一条 alert，而摘要不是告警。

CREATE TABLE IF NOT EXISTS digests (
    id               VARCHAR NOT NULL PRIMARY KEY,
    user_id          VARCHAR NOT NULL,
    market           VARCHAR NOT NULL,
    as_of            DATE NOT NULL,
    title            VARCHAR NOT NULL,
    body             VARCHAR NOT NULL,
    has_changes      BOOLEAN NOT NULL DEFAULT FALSE,
    heartbeat        BOOLEAN NOT NULL DEFAULT FALSE,
    source_note      VARCHAR NOT NULL DEFAULT '',
    pushed           BOOLEAN NOT NULL DEFAULT FALSE,
    delivery_outcome VARCHAR NOT NULL DEFAULT '',
    delivery_error   VARCHAR NOT NULL DEFAULT '',
    delivered_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, market, as_of)
);

CREATE INDEX IF NOT EXISTS idx_digests_user_date ON digests(user_id, as_of DESC);
