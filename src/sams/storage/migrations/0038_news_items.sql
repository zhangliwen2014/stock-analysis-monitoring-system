-- 0038_news_items.sql — 自选/持仓资讯流（S3-A）
--
-- 去重键 `(source, item_id)`（D244）：同一篇文章在不同源出现算两条——来源不同、
-- 原文链接不同，跨源合并等于替读者猜「谁先发」。
-- `item_id` 取源自身 id；源没给时仓库层按 (title, published_at) 取指纹，
-- 同一源内稳定，否则每次同步都会多出一条「看起来是新的旧闻」。
--
-- 公告不搬进这张表：`announcements` 已被日历、个股页与 F10 使用，
-- 统一视图在服务层拼，底层各读各的表。

CREATE TABLE IF NOT EXISTS news_items (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    source       VARCHAR NOT NULL,
    item_id      VARCHAR NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    title        VARCHAR NOT NULL,
    summary      VARCHAR,
    url          VARCHAR NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source, item_id)
);

CREATE INDEX IF NOT EXISTS idx_news_items_code_time
    ON news_items(market, code, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_news_items_fetched ON news_items(fetched_at);
