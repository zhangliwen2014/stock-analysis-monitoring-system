-- 0042：现金流水分类（S4-D）。
--
-- `kind` 让净值重放分得清「内部变动」与「外部注入」：买入/卖出/手续费/分红/换汇
-- 不改变组合的总价值，存入/取出才会。只靠 reason 的文本猜是不可靠的——那列是给人看的，
-- 谁都可以改一个字。历史行按已知前缀回填，判不出的留 manual（不猜）。
--
-- `ref` 只给可幂等的自动入账（分红 `market:code:ex_date`）；手工与交易行为 NULL。
-- 唯一索引承担「同一笔分红只入一次」：服务层先查是为了给出一句人话，库这一层
-- 才是最后一道闸。
--
-- DuckDB 的 `ADD COLUMN` 不能带 NOT NULL（0015/0016/0017 的头注同一条），
-- 所以 kind 先可空 + 默认值；写侧永远填值，读侧 COALESCE 兜老行。
ALTER TABLE sim_cash_moves ADD COLUMN IF NOT EXISTS kind VARCHAR DEFAULT 'manual';
ALTER TABLE sim_cash_moves ADD COLUMN IF NOT EXISTS ref VARCHAR;

-- NULL 在唯一索引里互不冲突（DuckDB 与 SQL 标准一致）：手工与交易那两类 ref 为空，
-- 同一账户的无数条流水不会撞在一起。
-- 索引要先建：DuckDB 在同一个事务里「先更新再建索引」会报 outstanding updates。
CREATE UNIQUE INDEX IF NOT EXISTS uq_sim_cash_moves_ref
    ON sim_cash_moves (account_id, kind, ref);

UPDATE sim_cash_moves SET kind = 'fx'
 WHERE COALESCE(kind, 'manual') = 'manual' AND reason LIKE '换汇 %';
UPDATE sim_cash_moves SET kind = 'buy'
 WHERE COALESCE(kind, 'manual') = 'manual' AND reason LIKE '买入 %';
UPDATE sim_cash_moves SET kind = 'sell'
 WHERE COALESCE(kind, 'manual') = 'manual' AND reason LIKE '卖出 %';
UPDATE sim_cash_moves SET kind = 'fee'
 WHERE COALESCE(kind, 'manual') = 'manual' AND reason LIKE '手续费%';
UPDATE sim_cash_moves SET kind = 'deposit'
 WHERE COALESCE(kind, 'manual') = 'manual' AND reason LIKE '存入%';
UPDATE sim_cash_moves SET kind = 'withdraw'
 WHERE COALESCE(kind, 'manual') = 'manual' AND reason LIKE '取出%';
