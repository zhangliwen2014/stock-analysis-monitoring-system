-- 0012：基本面的两个缺口——披露日、按日估值倍数（spec D60 / D61 / §18 U13）
--
-- 1) `financials` 只有 `report_date`（报告期），而报告期不等于「谁知道的那一天」：
--    2024Q1 的报表在 4 月末才公开。按报告期取数等于把未来信息放进历史——一个在 3 月
--    就用了 4 月才披露的 ROE 的选股回测，结论好看得没有意义。`published_date` 是
--    读取层唯一的时间闸门（D61），它为空时一律不采信并单独计数，因为「源没给披露日」
--    与「那天真没披露」在这一列上是两种含义（D43 的那条线）。
--
-- 2) 市赚率要的是「今天的 PE」配「最近已披露那期的 ROE」（D59），而 `financials` 的
--    粒度是报告期：把某一天的 PE 写进某一期的行里，就是拿一个日期冒充一个期间。
--    倍数（PE/PB）、股息率、市值按日进这张新表，粒度与日线对齐；M2-D 要的
--    「PE-PB 历史分位」从此有真实序列可问，不必再补一次表。
--
-- 3) `symbols.listed_on`：D37 的上市豁免至今只能靠命令行手传（U18），而上市日期
--    本来就写在基本面源给的那张公司概况里。有列才谈得上「机器自己拿到」。
--
-- DuckDB 的 ADD COLUMN IF NOT EXISTS 让重放安全：迁移器虽已记账不重跑，
-- 但同一份 SQL 在「手工建过渡库」时也必须是幂等的。
ALTER TABLE financials ADD COLUMN IF NOT EXISTS published_date DATE;
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS listed_on DATE;

CREATE TABLE IF NOT EXISTS valuation_daily (
    market         VARCHAR NOT NULL,
    code           VARCHAR NOT NULL,
    -- 源给的估值日期（不是采集时间）：一个周末采集的数属于它对应的那个交易日，
    -- 否则「按日 PE」会变成「按采集日 PE」，历史分位会整段平移
    date           DATE NOT NULL,
    -- 倍数列：20 倍市盈率就存 20.0，不参与 ÷100（D58）
    pe_ttm         DOUBLE,
    pb             DOUBLE,
    -- 比率列：一律小数口径，4.2% 存 0.042（D58）；NULL 是「不知道」，不是 0
    dividend_yield DOUBLE,
    market_cap     DOUBLE,
    source         VARCHAR NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code, date)
);
