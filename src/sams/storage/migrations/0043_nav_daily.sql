-- 0043：组合净值（S4-D §6.3）。
--
-- 一天一行、每个账户一行。重跑同一天覆盖同一行（主键 account_id + date），
-- 而不是追加：那支折线读的是「这一天收盘时这个账户值多少」，同一晚跑两遍
-- 该得到同一张图，不是两条重叠的历史。
--
-- `nav` 可空是这条表最重要的设计：缺价或缺汇率的那一天写 NULL、complete=false，
-- 并在 note 里点名缺哪一项。用 0 或前值补出来的曲线看起来平滑，而「算不出来」
-- 与「没变化」是两件事（与 `core/funds.weighted_rate` 同一条规矩）。
-- cash / market_value 是报告币种里的两段，两者相加才是 nav；不完整时三段一起空。
--
-- external_flow 是当日「存入 − 取出」（报告币种）：TWR 要从当日净值里先扣掉它，
-- 否则一次注资会被读成一段收益。买入/卖出/手续费/分红是内部变动，不计入。
CREATE TABLE IF NOT EXISTS nav_daily (
    account_id    VARCHAR NOT NULL,
    date          DATE NOT NULL,
    currency      VARCHAR NOT NULL,
    nav           DOUBLE,
    cash          DOUBLE,
    market_value  DOUBLE,
    external_flow DOUBLE NOT NULL DEFAULT 0,
    complete      BOOLEAN NOT NULL,
    --: 这一天有几只持仓的收盘价超过 5 个交易日没更新；数字进 note 也进这一列
    stale_prices  INTEGER NOT NULL DEFAULT 0,
    note          VARCHAR,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, date)
);
