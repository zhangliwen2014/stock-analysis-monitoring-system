-- 0028_financial_indicators.sql — 个股页 Wind 式主要财务指标（D217–D219）
--
-- 全部可空：迁移后、重采前的老行这些列都是 NULL，页面显示「—」，不假装源没给的那格。
-- 新列只服务「看数」：它们不进 screener_fields，也不参与市赚率；PIT 闸门仍然只有
-- published_date（D61）。比率列在库里一律小数口径（D58），金额列按本币原值。

ALTER TABLE financials ADD COLUMN IF NOT EXISTS report_type TEXT;

ALTER TABLE financials ADD COLUMN IF NOT EXISTS revenue DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS net_profit DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS eps DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS bps DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS ocf_per_share DOUBLE;

ALTER TABLE financials ADD COLUMN IF NOT EXISTS gross_margin DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS net_margin DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS roa DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS roic DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS debt_ratio DOUBLE;

ALTER TABLE financials ADD COLUMN IF NOT EXISTS current_ratio DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS quick_ratio DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS inventory_turnover DOUBLE;
ALTER TABLE financials ADD COLUMN IF NOT EXISTS total_asset_turnover DOUBLE;
