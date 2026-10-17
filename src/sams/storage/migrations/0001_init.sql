-- 0001_init.sql — spec §5 全部表结构
-- 约定：
--   * 自然键做 PRIMARY KEY / UNIQUE，配合 INSERT OR REPLACE 实现幂等写入（spec §5.3）
--   * json 类字段以 VARCHAR 存 JSON 文本，由 Python 侧序列化（避免库版本差异）
--   * 价格/金额一律 DOUBLE：本系统用于分析与监控，不做账务清算
--   * 市场域表无 user_id（全局共享，spec D4）；用户域表首列之后均带 user_id 或经账户归属

-- ============ 市场域 ============

CREATE TABLE IF NOT EXISTS symbols (
    market      VARCHAR NOT NULL,
    code        VARCHAR NOT NULL,
    name        VARCHAR,
    currency    VARCHAR NOT NULL,
    exchange    VARCHAR,
    sector      VARCHAR,
    lot_size    INTEGER DEFAULT 1,
    list_status VARCHAR DEFAULT 'L',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code)
);

CREATE TABLE IF NOT EXISTS bars_daily (
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    date       DATE NOT NULL,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE NOT NULL,
    volume     DOUBLE,
    amount     DOUBLE,
    turnover   DOUBLE,
    adj_factor DOUBLE DEFAULT 1.0,
    source     VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code, date)
);
CREATE INDEX IF NOT EXISTS idx_bars_daily_code_date ON bars_daily (code, date);

CREATE TABLE IF NOT EXISTS quotes_snapshot (
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    price      DOUBLE,
    pct_chg    DOUBLE,
    volume     DOUBLE,
    amount     DOUBLE,
    bid1       DOUBLE,
    ask1       DOUBLE,
    status     VARCHAR,
    is_stale   BOOLEAN NOT NULL DEFAULT FALSE,
    source     VARCHAR NOT NULL,
    PRIMARY KEY (market, code, ts)
);

CREATE TABLE IF NOT EXISTS funding_flow (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    date         DATE NOT NULL,
    main_net_in  DOUBLE,
    north_net    DOUBLE,
    north_hold   DOUBLE,
    margin_bal   DOUBLE,
    source       VARCHAR NOT NULL,
    PRIMARY KEY (market, code, date)
);

CREATE TABLE IF NOT EXISTS financials (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    report_date  DATE NOT NULL,
    pe_ttm       DOUBLE,
    pb           DOUBLE,
    roe          DOUBLE,
    revenue_yoy  DOUBLE,
    profit_yoy   DOUBLE,
    dividend_yield DOUBLE,
    market_cap   DOUBLE,
    source       VARCHAR NOT NULL,
    PRIMARY KEY (market, code, report_date)
);

CREATE TABLE IF NOT EXISTS indices (
    index_code VARCHAR NOT NULL,
    market     VARCHAR NOT NULL,
    name       VARCHAR,
    PRIMARY KEY (market, index_code)
);

CREATE TABLE IF NOT EXISTS index_constituents (
    market     VARCHAR NOT NULL,
    index_code VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    date       DATE NOT NULL,
    weight     DOUBLE,
    PRIMARY KEY (market, index_code, code, date)
);

CREATE TABLE IF NOT EXISTS scan_results (
    date        DATE NOT NULL,
    market      VARCHAR NOT NULL,
    strategy_id VARCHAR NOT NULL,
    universe_size INTEGER,
    hits_json   VARCHAR NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, market, strategy_id)
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    market        VARCHAR NOT NULL,
    date          DATE NOT NULL,
    is_trading    BOOLEAN NOT NULL,
    is_half_day   BOOLEAN NOT NULL DEFAULT FALSE,
    tz            VARCHAR NOT NULL,
    sessions_json VARCHAR NOT NULL,
    source        VARCHAR NOT NULL,
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date      DATE NOT NULL,
    base_ccy  VARCHAR NOT NULL,
    quote_ccy VARCHAR NOT NULL,
    rate      DOUBLE NOT NULL,
    source    VARCHAR,
    is_stale  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (date, base_ccy, quote_ccy)
);

-- ============ 用户域 ============

CREATE TABLE IF NOT EXISTS users (
    id            VARCHAR PRIMARY KEY,
    username      VARCHAR NOT NULL UNIQUE,
    pwd_hash      VARCHAR NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    status        VARCHAR NOT NULL DEFAULT 'active',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions (user_id);

CREATE TABLE IF NOT EXISTS watchlists (
    user_id   VARCHAR NOT NULL,
    market    VARCHAR NOT NULL,
    code      VARCHAR NOT NULL,
    note      VARCHAR,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, market, code)
);

CREATE TABLE IF NOT EXISTS theses (
    id                 VARCHAR PRIMARY KEY,
    user_id            VARCHAR NOT NULL,
    market             VARCHAR NOT NULL,
    code               VARCHAR NOT NULL,
    style              VARCHAR NOT NULL,
    status             VARCHAR NOT NULL,
    entry_low          DOUBLE,
    entry_high         DOUBLE,
    stop_price         DOUBLE,
    target_price       DOUBLE,
    reward_risk        DOUBLE,
    win_rate           DOUBLE,
    win_rate_samples   INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS thesis_events (
    id         VARCHAR PRIMARY KEY,
    thesis_id  VARCHAR NOT NULL,
    from_status VARCHAR,
    to_status  VARCHAR NOT NULL,
    actor_id   VARCHAR NOT NULL,
    note       VARCHAR,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS valuation_snapshots (
    id            VARCHAR PRIMARY KEY,
    thesis_id     VARCHAR NOT NULL,
    date          DATE NOT NULL,
    scores_json   VARCHAR NOT NULL,
    breakdown_json VARCHAR NOT NULL,
    fair_low      DOUBLE,
    fair_high     DOUBLE,
    entry_low     DOUBLE,
    entry_high    DOUBLE,
    stop          DOUBLE,
    target        DOUBLE,
    reward_risk   DOUBLE,
    intraday      BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thesis_id, date, intraday)
);

CREATE TABLE IF NOT EXISTS rules (
    id             VARCHAR PRIMARY KEY,
    user_id        VARCHAR NOT NULL,
    account_id     VARCHAR,
    name           VARCHAR NOT NULL,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    target_type    VARCHAR NOT NULL,
    target_ref     VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,
    condition_kind VARCHAR NOT NULL DEFAULT 'template',
    params_json    VARCHAR NOT NULL,
    filters_json   VARCHAR NOT NULL DEFAULT '{}',
    cooldown_min   INTEGER NOT NULL DEFAULT 30,
    severity       VARCHAR NOT NULL DEFAULT 'mid',
    channel_ids    VARCHAR NOT NULL DEFAULT '[]',
    thesis_id      VARCHAR,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rule_revisions (
    id         VARCHAR PRIMARY KEY,
    rule_id    VARCHAR NOT NULL,
    changed_by VARCHAR NOT NULL,
    before_json VARCHAR NOT NULL,
    after_json VARCHAR NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS channels (
    id              VARCHAR PRIMARY KEY,
    user_id         VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    config_encrypted VARCHAR,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    last_ok_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS alerts (
    id           VARCHAR PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    rule_id      VARCHAR NOT NULL,
    account_id   VARCHAR,
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    stage        VARCHAR NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_json VARCHAR NOT NULL,
    status       VARCHAR NOT NULL DEFAULT 'new',
    handled_at   TIMESTAMPTZ,
    handle_note  VARCHAR,
    dismiss_reason VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_alerts_throttle
    ON alerts (rule_id, market, code, triggered_at);

CREATE TABLE IF NOT EXISTS notify_log (
    id         VARCHAR PRIMARY KEY,
    alert_id   VARCHAR NOT NULL,
    channel_id VARCHAR,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    success    BOOLEAN NOT NULL,
    error      VARCHAR,
    attempt    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS annotations (
    id               VARCHAR PRIMARY KEY,
    user_id          VARCHAR NOT NULL,
    target_type      VARCHAR NOT NULL,
    target_ref       VARCHAR NOT NULL,
    body_md          VARCHAR NOT NULL,
    tags_json        VARCHAR NOT NULL DEFAULT '[]',
    attachments_json VARCHAR NOT NULL DEFAULT '[]',
    pinned           BOOLEAN NOT NULL DEFAULT FALSE,
    visibility       VARCHAR NOT NULL DEFAULT 'private',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS annotation_revisions (
    id            VARCHAR PRIMARY KEY,
    annotation_id VARCHAR NOT NULL,
    user_id       VARCHAR NOT NULL,
    before_md     VARCHAR,
    after_md      VARCHAR,
    edited_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sim_accounts (
    id              VARCHAR PRIMARY KEY,
    user_id         VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    broker          VARCHAR,
    market_scope    VARCHAR NOT NULL DEFAULT '[]',
    fee_model       VARCHAR NOT NULL DEFAULT 'default',
    margin_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    report_currency VARCHAR NOT NULL DEFAULT 'CNY',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS sim_cash_balances (
    account_id VARCHAR NOT NULL,
    currency   VARCHAR NOT NULL,
    amount     DOUBLE NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, currency)
);

CREATE TABLE IF NOT EXISTS sim_orders (
    id              VARCHAR PRIMARY KEY,
    account_id      VARCHAR NOT NULL,
    market          VARCHAR NOT NULL,
    code            VARCHAR NOT NULL,
    currency        VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,
    qty             DOUBLE NOT NULL,
    price           DOUBLE NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    fee             DOUBLE DEFAULT 0,
    fee_currency    VARCHAR,
    fx_rate_applied DOUBLE,
    thesis_id       VARCHAR
);

CREATE TABLE IF NOT EXISTS sim_positions (
    account_id   VARCHAR NOT NULL,
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    currency     VARCHAR NOT NULL,
    qty          DOUBLE NOT NULL,
    avg_cost     DOUBLE NOT NULL,
    open_ts      TIMESTAMPTZ,
    last_price   DOUBLE,
    last_price_ts TIMESTAMPTZ,
    PRIMARY KEY (account_id, market, code)
);

CREATE TABLE IF NOT EXISTS fx_trades (
    id         VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL,
    from_ccy   VARCHAR NOT NULL,
    to_ccy     VARCHAR NOT NULL,
    from_amt   DOUBLE NOT NULL,
    to_amt     DOUBLE NOT NULL,
    rate       DOUBLE NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_runs (
    id            VARCHAR PRIMARY KEY,
    job_name      VARCHAR NOT NULL,
    market        VARCHAR,
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    status        VARCHAR NOT NULL,
    rows_affected INTEGER,
    error         VARCHAR
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name       VARCHAR PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
