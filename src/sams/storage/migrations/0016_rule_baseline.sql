-- 0016：周复检要的那条「基线」（spec §9.3 / D96）
--
-- §9.3 的原话是「分项分较建仓时下滑超过一档」，而「建仓时」是一个必须被记下来的事实。
-- 库里没有它，`thesis.drift` 只剩两种错法：每次都拿今天的分和今天的分比（恒不漂移），
-- 或者拿第一次跑到那条规则时的分当基线（跑得晚一天，基线就换了，而屏幕上看不出来）。
--
-- M2-D 当时特意不给 thesis 存估值快照（D76：估值可重算，存一份就是第二个真相）。
-- 这一条不是推翻它：可重算的前提是**输入还在**，而 financials / valuation_daily 会随新的
-- 披露改写，`composite.score` 因此不是一个可重算的历史事实。这里存的是**当时那一个数**
-- 和当时那几路分项，不是那一次的输入。
--
-- 三列而不是三张表：一条论只有一条现行基线，改基线等于重新建仓（那是另一条论的事）。
-- `baseline_items` 存 JSON 是为了让「掉的是哪一档」能被指出来——只存总分回答不了那句话。
-- DuckDB 的 `ADD COLUMN` 不能带 NOT NULL（实测，0015 的头注是同一条），所以三列都可空，
-- 「没有基线」由读取侧显式判断（`services/rules/recheck.py` 里 D96 的那句谎）。
ALTER TABLE theses ADD COLUMN IF NOT EXISTS baseline_score DOUBLE;
ALTER TABLE theses ADD COLUMN IF NOT EXISTS baseline_items VARCHAR;
ALTER TABLE theses ADD COLUMN IF NOT EXISTS baseline_on DATE;
