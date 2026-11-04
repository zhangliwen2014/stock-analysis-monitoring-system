-- 0030_us_financials.sql — 美股主要财务指标源（D228）
--
-- 路由排在美股现有链尾：这条链上的前四个源都不提供 `financials`，所以顺序只决定
-- 「哪天有第二个美股财务源时谁先答」，不改变日报价/日线的取数。种子不覆盖用户
-- 在管理后台改过的启停与优先级（INSERT OR IGNORE）。

INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('eastmoney_us_f10', 'eastmoney_us_f10', TRUE, '{}',
     '东方财富美股主要财务指标（只有 US；仅年报，无披露日）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('US', 'eastmoney_us_f10', 5);
