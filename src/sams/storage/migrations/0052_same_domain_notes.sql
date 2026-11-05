-- 0052_same_domain_notes.sql — 让 US/HK 基本面的另一家在目录里说清「同源」（D322）
--
-- 0051 的 eastmoney_global 在自己的 note 里写了「与 eastmoney_hk_f10 / eastmoney_us_f10
-- 同源」，但运维点开另一家时看不到这句话，会把它当成一个独立的故障域——而 D322 的
-- 第 2 档记账恰恰是「同源不算第二个域」。补源的意义是「另一条上游还能答」，不是
-- 「同一个上游报了两遍」。
--
-- 带旧文案做条件：运维自己在后台改过的备注不许被迁移覆盖。
--
-- 第三条与同源无关：0051 写的 US 快照耗时（167s）在 11-06 的实机备源跑里是 99s，
-- 改成区间（D325 要的是能核对的实测数，一个点会被下一次跑出来的数打脸）。
UPDATE data_sources
   SET note = '东方财富港股主要财务指标（只有 HK；无披露日，不进 PR；与 eastmoney_global 同源，不算独立故障域）'
 WHERE name = 'eastmoney_hk_f10'
   AND note = '东方财富港股主要财务指标（只有 HK；无披露日，不进 PR）';

UPDATE data_sources
   SET note = '东方财富美股主要财务指标（只有 US；仅年报，无披露日；与 eastmoney_global 同源，不算独立故障域）'
 WHERE name = 'eastmoney_us_f10'
   AND note = '东方财富美股主要财务指标（只有 US；仅年报，无披露日）';

-- 0051 只测到 US 的 167s；11-06 的实机备源跑（腾讯停用、5915 只）用了 99s。
-- 快照耗时会随网络与限流浮动，note 里给区间而不是一个点。
UPDATE data_sources
   SET note = '东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与快照；基本面与 eastmoney_hk_f10 / eastmoney_us_f10 同源；快照整表分页，实测 HK 约 35–60s / US 约 100–170s）'
 WHERE name = 'eastmoney_global'
   AND note = '东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与快照；基本面与 eastmoney_hk_f10 / eastmoney_us_f10 同源；快照整表分页，实测 HK 约 35–60s / US 约 167s）';
