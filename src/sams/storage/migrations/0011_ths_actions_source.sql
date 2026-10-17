-- 0011_ths_actions_source.sql — A 股公司行为补第二个源（同花顺）
--
-- 0007 之后 A 股的公司行为只有 akshare（东财分红送配详情）一条。交易员试用报告里两条
-- 后果叠在一起：
--
-- 1. 东财那张 `RPT_SHAREBONUS_DET` 对 002450、600636 这类票直接回「返回数据为空」，
--    而同花顺同一只票有 8~28 条除权记录。单源说「没有」，覆盖标记就给不出来，
--    跨除权日的区间一路拒绝出数（D36）——症状与「这只票从没分过红」在界面上一模一样。
-- 2. A 股是唯一有 D37 证据守卫的市场，而那条守卫的阈值是涨跌停 ×1.5=15%：现金分红的
--    价格效应通常只有 1~6%，漏记一次派息它抓不住。这时候唯一的防线是「有第二个源能
--    把这条记录给出来」，单源等于没有防线。
--
-- 同花顺排在链尾（priority 5）：东财给数值列、可以拿描述列反查口径（D34 的单位换算靠它），
-- 证明过的样式更多，所以仍是主源；同花顺只给一句方案说明，认不出的样式按 D39 记 unknown。
-- 两个源是不同上游、不同限流域，这才是「备份」的意思（akshare 与 eastmoney 那种同源不算）。
--
-- 只加公司行为能力：它没有日线，挂在日线链上只会让 `sams source` 误判 A 股多一个备源（D38）。
INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('akshare_ths', 'akshare_ths', TRUE, '{}', '同花顺 A 股分红送配（只有公司行为，没有日线）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('A', 'akshare_ths', 5);
