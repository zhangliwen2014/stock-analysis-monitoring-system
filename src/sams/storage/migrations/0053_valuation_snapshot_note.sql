-- 0053_valuation_snapshot_note.sql — 东财港美股多了一条「按日估值」能力（D331）
--
-- 这条能力的 note 要说清两件事，否则运维会把它当成「逐日序列的补源」：
--   * 它只答**当日一行**（push2 报价快照的 f164/f167/f116），长窗口由调用方拒绝（D332）；
--   * 它与 eastmoney_hk_f10 / eastmoney_us_f10 同源，不算独立故障域（D322）。
--
-- 带旧文案做条件：运维自己在后台改过的备注不许被迁移覆盖。
UPDATE data_sources
   SET note = '东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与按日估值快照；基本面与 eastmoney_hk_f10 / eastmoney_us_f10 同源；估值只有当日一行，长窗口交给能给序列的源；快照整表分页，实测 HK 约 35–60s / US 约 100–170s）'
 WHERE name = 'eastmoney_global'
   AND note = '东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与快照；基本面与 eastmoney_hk_f10 / eastmoney_us_f10 同源；快照整表分页，实测 HK 约 35–60s / US 约 100–170s）';
