-- 0049_baidu_valuation.sql — A/HK 按日估值补第二个源（#387 / D324）
--
-- A/HK 的 `valuation` 原来各只有一家：A 是 `akshare`（上游东财），HK 是 `yfinance`。
-- 11-05 东财在 `result: null` 上崩了 38 次、yfinance 在 429 里冷却——两格同时是孤源，
-- 一挂就整条断。百度股市通是另一个域名，按指标一问（PE(TTM)/PB/总市值），实测
-- 近一年 365 行 / 4.5s（A）、365 行 / 2.0s（HK）；一只票要问三次。
--
-- 只有 `近一年`/`近三年` 是逐日序列：`近五年` 两天一格、`近十年` 五天一格、`全部`
-- 约十五天一格（600519 实测 913/731/614 行）。适配器只问前两档，更早的窗口按 0 行交回，
-- 不拿降采样序列冒充按日（D323）。
--
-- 排在链尾（A 8 / HK 6）：它只报 valuation，按能力过滤后估值链上紧跟主源；总市值是
-- 亿元，适配器乘 1e8 入库（D239）。
INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('baidu_valuation', 'baidu_valuation', TRUE, '{}', '百度股市通 A/港股按日估值（PE(TTM)/PB/总市值；逐日只到近三年）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority)
SELECT tail.market, 'baidu_valuation', tail.next_priority
FROM (
    SELECT market, max(priority) + 1 AS next_priority
    FROM data_source_routes
    WHERE market IN ('A', 'HK')
    GROUP BY market
) AS tail
WHERE EXISTS (
    SELECT 1 FROM data_sources s
    WHERE s.name = 'baidu_valuation' AND s.kind = 'baidu_valuation'
);
