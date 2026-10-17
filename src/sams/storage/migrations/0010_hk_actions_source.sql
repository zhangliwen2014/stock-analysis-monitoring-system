-- 0010_hk_actions_source.sql — 港股公司行为补一个独立故障域的备源
--
-- 0007 之后 HK 的链是 yfinance → 腾讯，两条都只有日线能力。于是 `sync actions --market HK`
-- 的结果是「链上没有任何源能回答」——不是没分红，是没人能查（D38 要显式说出的正是这一对）。
-- 而港股漏一次除权的后果比 A 股更隐蔽：D37 的证据守卫依赖涨跌停上限，港股没有上限，
-- 漏记的实物分派在图上什么都不留下。
--
-- 这里加的是东财港股分红派息（`akshare_hk`，只有公司行为能力）。它排在 yfinance 之后：
-- Yahoo 给的是全历史且含拆股，覆盖面更广；东财这条是同一件事的独立故障域（不同上游、
-- 不同限流域），主源被限流时它顶上。
--
-- 只动 HK：A 股的公司行为已由 akshare（分红送配详情）承担，美股这一期只有 yfinance 一个源，
-- 暂无可加的备源。多排一个用不上的源等于在链上放一个每次都要问一遍的陌生人。
INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('akshare_hk', 'akshare_hk', TRUE, '{}', '东财港股分红派息（只有公司行为，没有日线）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('HK', 'akshare_hk', 3);

-- yfinance 现在也给公司行为（分红、拆股、资本利得分配）。备注会显示在管理后台的源列表里，
-- 说少了，下一个改链的人就会把它当成「只是个拉价格的源」从公司行为链上摘掉。
-- 带旧文案做条件：运维自己改过的备注不许被迁移覆盖。
UPDATE data_sources SET note = 'yfinance 港美股日线 + 公司行为（Yahoo）'
    WHERE name = 'yfinance' AND note = 'Yahoo 港美股日线';
