-- 0048_data_source_backups.sql — 给孤源格补第二个源（#387 / D324–D328）
--
-- 第一条把 0047 的巨潮从 priority 6 挪到 7：它与 eastmoney_f10 同号，同号时按名字排，
-- 巨潮会排到东财前面——公司概况/公告的「第 2 位」就变成了第 1 位，而东财才是这两格的
-- 主源（D324）。公司行为链不受影响：eastmoney_f10 没有这个能力，链上仍只有前三家。
UPDATE data_source_routes SET priority = 7
 WHERE market = 'A' AND name = 'akshare_cninfo';

-- 0047 写这句时它只有公司行为；D324 起同一份巨潮端点也供概况、公告与上市日期。
UPDATE data_sources
   SET note = '巨潮 A 股：历史分红、公司概况、公告、上市日期（无日线）'
 WHERE name = 'akshare_cninfo';
