-- 0021：全市场代码表、指数日线、个股补数队列（SP1 D140/D144/D145）

CREATE TABLE symbol_master (
    market VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    name VARCHAR,
    py_initials VARCHAR NOT NULL,
    py_full VARCHAR NOT NULL,
    currency VARCHAR,
    is_active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code)
);
CREATE INDEX symbol_master_search ON symbol_master (market, py_initials, code);

CREATE TABLE index_daily (
    market VARCHAR NOT NULL,
    index_code VARCHAR NOT NULL,
    date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE NOT NULL,
    volume DOUBLE,
    amount DOUBLE,
    prev_close DOUBLE,
    change_pct DOUBLE,
    source VARCHAR,
    PRIMARY KEY (market, index_code, date)
);

CREATE TABLE symbol_backfills (
    id VARCHAR PRIMARY KEY,
    market VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    last_error VARCHAR,
    status VARCHAR NOT NULL,
    open_key VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at TIMESTAMPTZ
);
-- DuckDB 没有 partial unique index：开放期非空、结案置 NULL，NULL 不互相冲突
CREATE UNIQUE INDEX symbol_backfills_open ON symbol_backfills (open_key);
