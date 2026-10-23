-- 0019：盘中报价的形状、测试告警与两条查询索引（spec §4.2/§4.3，D114/D116/D131）。
--
-- 1) `quotes_snapshot` 在 0001 里只有价/涨跌幅/量/额/买卖一。盘中判 urgent 要开盘价与
--    前收盘（跳空与当日振幅），页面也要一个不依赖 `bars_daily` 的当日高低。四列都可空：
--    老快照不知道就说不知道，回填 0 会让「开盘价为零」与「没记过」长得一样。
--
-- 2) `quote_source_health` 与 `source_health` 分表。报价与日线是两条独立的故障域：
--    日线源在盘后同步时坏掉，不该让盘中报价显示为降级；反过来也一样。`ok` 沿用三态
--    （NULL = 从没被路由到），`last_ok_at` 是时刻而不是日期——报价的新鲜度按秒算，
--    日期粒度会把「三分钟没报价」显示成「今天成功过」。
--
-- 3) `alerts.is_test`（D131）：测试告警要能在收件箱里被认出来，且不参与 no_open_alert、
--    健康积压与复盘。DuckDB **不支持** `ADD COLUMN ... NOT NULL DEFAULT ...`（实测
--    ParserException），所以分三步：加可空列 → 设默认值 → 回填旧行。只加列不设默认的话，
--    服务层漏传一次就会写进 NULL；只回填不设默认的话，下一行仍是 NULL。
--
-- 4) 两个索引各服务一条查询：`quotes_snapshot` 按 `(market, code)` 取最近一笔填
--    `QuoteBoard`；`alerts` 按 `(user_id, triggered_at)` 取某个用户的收件箱。
--
-- 5) `sina_quote` 追加到每个市场链末位（D114）。它只报 `quote` 能力，日线链按能力过滤后
--    看不见它（D38）；放在同一张路由表里是因为「一个市场有哪些源」只能有一处答案。
--    已经手工加过同名源的库不动它的配置；只在该市场还没有这条路由时追加。

ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS open DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS prev_close DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS high DOUBLE;
ALTER TABLE quotes_snapshot ADD COLUMN IF NOT EXISTS low DOUBLE;

CREATE INDEX IF NOT EXISTS idx_quotes_snapshot_lookup
    ON quotes_snapshot (market, code, ts);

ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR;

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS is_test BOOLEAN;
ALTER TABLE alerts ALTER COLUMN is_test SET DEFAULT FALSE;
UPDATE alerts SET is_test = FALSE WHERE is_test IS NULL;

CREATE INDEX IF NOT EXISTS idx_alerts_user_triggered
    ON alerts (user_id, triggered_at);

CREATE TABLE IF NOT EXISTS quote_source_health (
    name                 VARCHAR PRIMARY KEY,
    markets              VARCHAR NOT NULL DEFAULT '',
    ok                   BOOLEAN,
    last_error           VARCHAR,
    last_ok_at           TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('sina_quote', 'sina_quote', TRUE, '{}', '新浪 hq.sinajs.cn 批量报价（只提供报价，没有日线）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority)
SELECT tail.market, 'sina_quote', tail.next_priority
FROM (
    SELECT market, max(priority) + 1 AS next_priority
    FROM data_source_routes
    WHERE market IN ('A', 'HK', 'US')
    GROUP BY market
) AS tail
WHERE EXISTS (
    SELECT 1 FROM data_sources s
    WHERE s.name = 'sina_quote' AND s.kind = 'sina_quote'
);
