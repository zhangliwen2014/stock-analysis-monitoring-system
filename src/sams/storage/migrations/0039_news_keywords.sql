-- 0039_news_keywords.sql — 关键词命中与静音分组（S3-C / S3-E）
--
-- 关键词是用户级、市场级、可选代码级（D245）：`code IS NULL` 表示该市场全部
-- 自选/持仓/观察，不给每只票复制一行。去重靠读取时的 `IS NOT DISTINCT FROM`：
-- DuckDB 的唯一索引把 NULL 当互不相同的值，写不进「同一作用域只有一个词」。
--
-- `news_hits` 的主键直接承担「同一资讯×关键词只告警一次」（D246）：重跑同一晚的
-- 同步时，已命中的那一对不再插入，也就不会再推一遍。
--
-- 静音分组（D249）是新表：`kind ∈ all / rule / news / digest`。旧 `alert_mutes`
-- 只读不迁——迁移不搬历史，避免一次全表重写把旧行的时间戳改掉；读取时旧行按
-- `all` 解释。

CREATE TABLE IF NOT EXISTS news_keywords (
    id          VARCHAR PRIMARY KEY,
    user_id     VARCHAR NOT NULL,
    market      VARCHAR NOT NULL,
    code        VARCHAR,
    keyword     VARCHAR NOT NULL,
    channel_ids VARCHAR NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_keywords_user
    ON news_keywords(user_id, market);

CREATE TABLE IF NOT EXISTS news_hits (
    user_id    VARCHAR NOT NULL,
    source     VARCHAR NOT NULL,
    item_id    VARCHAR NOT NULL,
    keyword_id VARCHAR NOT NULL,
    hit_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, source, item_id, keyword_id)
);

CREATE INDEX IF NOT EXISTS idx_news_hits_time ON news_hits(user_id, hit_at DESC);

CREATE TABLE IF NOT EXISTS alert_mute_scopes (
    user_id    VARCHAR NOT NULL,
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    kind       VARCHAR NOT NULL,
    note       VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, market, code, kind)
);
