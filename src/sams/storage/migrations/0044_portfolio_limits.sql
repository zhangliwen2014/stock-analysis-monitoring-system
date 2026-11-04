-- 0044：组合限额（S4-E §2）。
--
-- 一行 = 用户自己定的一条红线。`kind` 只有四种：单票/市场/行业权重与净值回撤；
-- 权重线的 `scope` 是代码、市场名或行业名，回撤线没有对象（空串）。
--
-- `account_id` 为空表示这个用户的**全部账户**：同一屏上每个账户各算各的权重，
-- 同一个阈值对每个账户都生效，不是把所有账户的持仓加在一起判。
--
-- 阈值存小数比值（0.2 = 20%）：与 `exposure` / `nav` 里的权重同一口径，
-- 屏幕上的百分号只在显示时加，不在数据里存两套标度。
CREATE TABLE IF NOT EXISTS portfolio_limits (
    id         VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL,
    account_id VARCHAR,
    kind       VARCHAR NOT NULL,
    scope      VARCHAR,
    threshold  DOUBLE NOT NULL,
    note       VARCHAR,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_limits_user
    ON portfolio_limits (user_id, account_id);
