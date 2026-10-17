-- 0006_drop_ownerless_turnover.sql — 删掉 bars_daily.turnover
-- 与 0003 删 adj_factor 同一条理由：没有生产者的列会被下游当成可信口径。
-- 采集层的标准列集根本不产出换手率（要流通股本，M1 不采），DDL 里留着它，
-- M2 的「换手率 > x%」模板就会把它当现成指标读，读到的是恒 NULL 或恒 0。
-- 换手率是派生量：volume / 流通股本，需要时由读取层算，不入库。

-- 索引依赖这张表，DuckDB 不许在有人依赖时改列：先拆索引，改完再建回来
DROP INDEX IF EXISTS idx_bars_daily_code_date;

ALTER TABLE bars_daily DROP COLUMN turnover;

CREATE INDEX IF NOT EXISTS idx_bars_daily_code_date ON bars_daily (code, date);
