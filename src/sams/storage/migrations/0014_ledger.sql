-- 0014：账本缺的两样东西——现金的「为什么」，和成交那一天的汇率。
--
-- `sim_cash_moves`（§5.4 规则 4）。它不是「余额的历史快照」：那种表能被事后编出来。
-- 记的是 delta + reason + actor，于是「谁在什么时候凭什么动了 5 万」是可证的。
-- M3 允许手工调现金（`fx_trades` 要到 M5 才有），能改数的地方就必须有这一行，
-- 否则余额对不上时只剩一个数字，没有任何一句解释。
CREATE TABLE IF NOT EXISTS sim_cash_moves (
    id         VARCHAR PRIMARY KEY,
    account_id VARCHAR NOT NULL,
    currency   VARCHAR NOT NULL,
    delta      DOUBLE NOT NULL,
    reason     VARCHAR NOT NULL,
    actor_id   VARCHAR NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sim_cash_moves_account ON sim_cash_moves (account_id, currency);

-- `fx_rate_ref`：这一笔成交**当天**「标的币种 → 报告币种」的汇率。
--
-- 0001 里那个 `fx_rate_applied` 不是同一个数：它「仅当该笔订单伴随换汇时记录」，
-- 服务于现金池对账（spec §5.2），大部分订单那一列是 NULL。而持仓盈亏要拆成
-- 价格段与换汇段（§5.4 line 434），拆的基准是「下单那天的汇率」——每一笔都得有，
-- 包括没换汇的那些。两个问题问的是两个数，所以是两列。
-- 缺它时两段拆分只能整段报成价格盈亏：人民币贬值会被读成「这只票涨了」。
ALTER TABLE sim_orders ADD COLUMN IF NOT EXISTS fx_rate_ref DOUBLE;
