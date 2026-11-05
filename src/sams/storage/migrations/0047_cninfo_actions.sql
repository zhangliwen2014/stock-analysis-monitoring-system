-- 0047_cninfo_actions.sql — A 股公司行为补第三个源（巨潮历史分红，D318 / #383）
--
-- 引进同花顺（0011）解决了「东财说没有、同花顺有 8~28 条」，但它自己有一条看不见的路：
-- 同花顺的 F10 页对**北交所**标的把日期列挂在「B股」那一组名下，而且那些列全是 `NaT`
-- （akshare 的解析器自己就是 `if "A股股权登记日" in columns ... else ...`）。
-- 实测 11-05：920289 的表里躺着「10转增12.5397股」这句已实施的方案，却没有 A 股除权日
-- （`akshare_ths` 因此按 D316 答「读不出」）；东财对这只票答「查无记录」。
-- 于是这只票在链上一个源都问不到，而交易所自己的披露平台写了那一天——巨潮 `p_sysapi1139`
-- 给回除权日 2024-07-17、`转增比例 12.5397`，口径与东财一致（每 10 股）。
--
-- 排在链尾（priority 6）：它给的是数值列 + 说明文本，两者可以互查单位（D34），但记录面比
-- 前两个窄——实测 002450、600636（同花顺当初被引进来看的那两只）在巨潮同样是 `records: []`。
-- 三个源是三家上游，谁都不是全集，这正是 D36 要的「多源独立答复」。
--
-- 只加公司行为能力：它没有日线，挂在日线链上只会让 `sams source` 误判 A 股多一个备源（D38）。
INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('akshare_cninfo', 'akshare_cninfo', TRUE, '{}', '巨潮 A 股历史分红（只有公司行为，没有日线）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('A', 'akshare_cninfo', 6);
