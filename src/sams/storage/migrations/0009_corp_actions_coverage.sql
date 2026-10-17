-- 0009：公司行为的覆盖区间（spec D36 / §18 U12）
--
-- `corp_actions` 里 0 行有两种完全不同的含义：这只票从来没分过红，或者没人查过它。
-- 库里长得一样，读取层就无从判断「区间收益可信」还是「算不出」——而 U12 要防的正是
-- 把后者当前者：601088 用裸价算出 −3.81%，交易所公布 +1.26%。
--
-- 这张表记的是「谁在什么时候替这只票确认过一遍」：采集成功不只是写几行事实，
-- 还要写下这次确认覆盖到哪里。没有它，守卫只能永远拒绝或者永远放行。
CREATE TABLE IF NOT EXISTS corp_actions_coverage (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    -- 本次确认所覆盖的除权日区间；full_history 为真时 covered_from 只是最早一条的日期
    covered_from DATE NOT NULL,
    covered_to   DATE NOT NULL,
    -- 采集者是否拿到了「这只票的全部历史公司行为」。全历史源（东财明细、yfinance actions）
    -- 给真；按区间拉的源给假——两者对「区间之外有没有除权」的回答能力不同
    full_history BOOLEAN NOT NULL DEFAULT FALSE,
    src          VARCHAR NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 每个源各记一段：A 股主源确认过的区间与备源确认过的区间不是一回事，
    -- 合成一段等于凭空造出一个谁都没看过的覆盖面
    PRIMARY KEY (market, code, src)
);
