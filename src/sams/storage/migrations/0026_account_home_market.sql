-- 0026：账户的主场市场（D208）。录入成交时默认选它，不是权限也不是约束。
-- 已有账户一律当 A 股账户：它们都开在只有 A 股记账的场景里，默认值就是它们的事实。
-- 不带 IF NOT EXISTS：DuckDB 不支持「IF NOT EXISTS + NOT NULL」，而迁移本身只应用一次。
ALTER TABLE sim_accounts ADD COLUMN home_market VARCHAR DEFAULT 'A' NOT NULL;
