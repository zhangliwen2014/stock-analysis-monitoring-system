-- 0045：每票日价值（S5-A §2.1）。
--
-- 一天一只票一行，由 `NavService.rebuild` 在同一趟里写：净值曲线和归因必须出自同一次取价。
-- 让归因自己去 bars 表重放一遍「≤ D 的最近收盘 + 当天汇率」，两边就会在陈旧价怎么挡、
-- 汇率取哪一天上各自演化，最后出现「贡献加起来不等于净值涨幅」这种没法解释的表（D266）。
--
-- price 是那当天实际用上的本币收盘价（不是「最新收盘」），fx_rate 是本币 → 报告币种，
-- 两者与 qty 一起把 value_report 摊开：归因要按「每股价值变化」算持有收益，
-- 还要把价格段与换汇段分开报（D264）。
--
-- qty=0 的行是**清仓当天**：卖出那天的价格效应如果没有归属，就会整段掉进残差，
-- 所以这只票不再持有、也要留下当天的价与汇率（只延续一天）。
-- 算不出来的日子（缺价/缺汇率）根本没有行——那天 nav_daily 本身已是 complete=false，
-- 归因只用完整日，不需要每行再背一个状态列。
CREATE TABLE IF NOT EXISTS nav_holding_daily (
    account_id    VARCHAR NOT NULL,
    date          DATE NOT NULL,
    market        VARCHAR NOT NULL,
    code          VARCHAR NOT NULL,
    qty           DOUBLE NOT NULL,
    price         DOUBLE NOT NULL,
    fx_rate       DOUBLE NOT NULL,
    value_report  DOUBLE NOT NULL,
    PRIMARY KEY (account_id, date, market, code)
);

-- 归因按账户 + 区间整段读；主键已经能覆盖 (account_id, date)，但按市场聚合时
-- 还要走 market，留给读侧一个便宜的过滤。
CREATE INDEX IF NOT EXISTS nav_holding_daily_account_date
    ON nav_holding_daily (account_id, date, market);
