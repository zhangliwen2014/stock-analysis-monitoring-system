-- 0023：全市场快照（D175/D186）。主键 (market, day, code)：同一天重采是整日覆盖
CREATE TABLE IF NOT EXISTS snapshot_daily (
    market        TEXT NOT NULL,
    day           DATE NOT NULL,
    taken_at      TIMESTAMPTZ NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    price         DOUBLE,
    pct_chg       DOUBLE,
    amount        DOUBLE,
    turnover_rate DOUBLE,
    volume_ratio  DOUBLE,
    amplitude     DOUBLE,
    pe_ttm        DOUBLE,
    pe_dyn        DOUBLE,
    pb            DOUBLE,
    market_cap    DOUBLE,
    float_cap     DOUBLE,
    is_st         BOOLEAN,
    source        TEXT NOT NULL,
    PRIMARY KEY (market, day, code)
);
