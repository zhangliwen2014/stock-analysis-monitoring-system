-- 0034_alert_evidence_states.sql — 告警证据链与跃迁去重（S1-B）
--
-- `trigger_bar_ts` 说这条告警判自哪根 bar，`dedupe_key` 记下它按哪个键去重。
-- 老行两列都留空：没有可靠来源时补一个值，等于让复盘看见一根没发生过的 bar。
--
-- `alert_states` 是跃迁去重的持久事实：一条去重键只有一个当前状态。
-- `account_key` 用空串代表「不挂账户」——DuckDB 的主键不接受 NULL，
-- 而自选/价格类规则的 `account_id` 是 NULL 才是常态。
--
-- `alert_mutes` 的键不带规则：静音讲的是「这只票别再烦我」，不是某一条规则。

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS trigger_bar_ts DATE;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR;

CREATE TABLE IF NOT EXISTS alert_states (
    user_id     VARCHAR NOT NULL,
    rule_id     VARCHAR NOT NULL,
    market      VARCHAR NOT NULL,
    code        VARCHAR NOT NULL,
    account_key VARCHAR NOT NULL,
    state       VARCHAR NOT NULL,
    bar_ts      DATE,
    value_text  VARCHAR,
    updated_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, rule_id, market, code, account_key)
);

CREATE TABLE IF NOT EXISTS alert_mutes (
    user_id    VARCHAR NOT NULL,
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    note       VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, market, code)
);
