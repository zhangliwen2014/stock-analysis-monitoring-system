-- 0046：补数队列分清「补什么」（spec 2026-11-03 §3.3）。
--
-- 「有人想看这只票」的登记处原来只有一种活：`BackfillConsumer` 拿到行就调 `sync_daily`。
-- 公司行为覆盖没确认时，界面上能说的只剩一句命令行——而 DuckDB 单写者，serve 跑着的时候
-- 那条命令必然撞 DatabaseLocked。所以缺覆盖也要能排队，队列就得能区分两种活。
--
-- 去重键必须带上 kind：一条待补日线会占住 `'{market}|{code}'`，后一种活永远进不来，
-- 而先完成的那件又用 `mark_done_by_code` 把两件一起标成 done。同一票于是可以各挂一条
-- 开放行——它们是两份不同的账。
--
-- 默认值 'bars' 让既有行为一字不改（旧调用方不传 kind），但**开放行的键要一起归一**：
-- 不归一时旧行留着 `A|600519`、新行插成 `A|600519|bars`，去重失效，同一只票能挂出两条
-- 重复的日线活。
--
-- 索引要先拆后建：`symbol_backfills_open` 依赖这张表，DuckDB 因此拒绝 `ALTER TABLE`
-- （「Cannot alter entry ... because there are entries that depend on it」，先例
-- 0031/0006）。中间没有查询方，唯一性只在重建那一刻重新校验一次——归一之后的键彼此不同，
-- 而旧数据里每票本来至多一条开放行（就是被那个唯一索引保证的）。
DROP INDEX symbol_backfills_open;

ALTER TABLE symbol_backfills ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'bars';

UPDATE symbol_backfills
SET open_key = open_key || '|bars'
WHERE open_key IS NOT NULL AND kind = 'bars';

CREATE UNIQUE INDEX symbol_backfills_open ON symbol_backfills (open_key);
