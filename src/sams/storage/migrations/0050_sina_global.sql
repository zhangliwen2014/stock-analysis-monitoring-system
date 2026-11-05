-- 0050_sina_global.sql — 港美股日线补第三个独立故障域（#387 / D324）
--
-- HK/US 的日线原来只有 yfinance → 腾讯两跳，两家都在境外机房：同一次网络故障能把
-- 整条链一起打掉（11-05 港股停更就是那一次）。新浪这一路是第三个域，且与 A 股的
-- `sina` 是不同接口族（`stock_hk_daily` / `stock_us_daily` / `index_us_stock_sina`）。
--
-- 实测（2026-11-05）：港股 00700 全史 5508 行、带成交额（五位代码，`700` 会抛
-- KeyError）；美股 AAPL 10068 行、**不带**成交额（裸代码，`AAPL.OQ` 会抛 IndexError，
-- 类股 `BRK.B` 正常）；美股指数 `.IXIC` 5747 行，但 amount 整列为 0——适配器置 NULL。
--
-- 排在链尾（HK 7 / US 6）：它只报 daily_bars 与 US 的 index_daily，按能力过滤后
-- HK/US 日线是第 3 位、美股指数日线是第 2 位（紧跟腾讯），正是 D324 要的位置。
INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('sina_global', 'sina_global', TRUE, '{}', '新浪港美股日线（HK/US 全历史，不复权；US 指数 .INX/.IXIC/.DJI）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority)
SELECT tail.market, 'sina_global', tail.next_priority
FROM (
    SELECT market, max(priority) + 1 AS next_priority
    FROM data_source_routes
    WHERE market IN ('HK', 'US')
    GROUP BY market
) AS tail
WHERE EXISTS (
    SELECT 1 FROM data_sources s
    WHERE s.name = 'sina_global' AND s.kind = 'sina_global'
);
