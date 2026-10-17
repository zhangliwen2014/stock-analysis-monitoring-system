-- 0003 行情口径：把「存什么价、什么单位」在 M1 定死。
--
-- 为什么现在改：DuckDB 不能 ALTER 主键。等 M2 用指标、M3 用除权换算跑出结果后再补
-- 复权维度，只能重建表重灌；而那时「哪条序列是用哪套口径算的」已经无从考证。
--
-- 口径决定（spec §5.1）：
--   入库只存不复权价，adjust 恒为 'raw'。前复权会回溯改写历史收盘价（等价于用 T 之后的
--   信息算 T 的指标，违反 D9 反未来函数），后复权序列由 corp_actions 推导、在读取层现算。
--   因此删掉 bars_daily.adj_factor：一个恒等于 1、又没有生产者的列，会被下游当成可信口径。
--   volume 统一为「股」（A 股源给手，适配层已换算），amount 统一为「本币元」（港美股不可得）。

CREATE TABLE bars_daily_new (
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    date       DATE NOT NULL,
    adjust     VARCHAR NOT NULL DEFAULT 'raw',
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE NOT NULL,
    volume     DOUBLE,
    amount     DOUBLE,
    turnover   DOUBLE,
    source     VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code, date, adjust)
);

INSERT OR REPLACE INTO bars_daily_new
    (market, code, date, adjust, open, high, low, close, volume, amount, turnover,
     source, updated_at)
SELECT market, code, date, 'raw', open, high, low, close, volume, amount, turnover,
       source, updated_at
FROM bars_daily;

DROP TABLE bars_daily;
ALTER TABLE bars_daily_new RENAME TO bars_daily;
CREATE INDEX IF NOT EXISTS idx_bars_daily_code_date ON bars_daily (code, date);

-- 公司行为：复权因子与持仓成本换算的唯一依据。
CREATE TABLE IF NOT EXISTS corp_actions (
    market         VARCHAR NOT NULL,
    code           VARCHAR NOT NULL,
    ex_date        DATE NOT NULL,
    kind           VARCHAR NOT NULL,
    cash_per_share DOUBLE,
    ratio          DOUBLE,
    src            VARCHAR NOT NULL,
    note           VARCHAR,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code, ex_date, kind)
);
