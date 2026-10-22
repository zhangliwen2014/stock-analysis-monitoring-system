-- 0018：调度器的两本账（spec §12）
--
-- 1) `task_runs` 从 0001 就在库里，却一直没有写入方。调度器要回答的第一个问题不是
--    「上次跑得怎么样」而是「**这个周期**跑过没有」——`started_at` 的日期答不了：周任务的
--    两个成功行可能落在周三与周四（补跑），月任务更是跨月。所以周期键单独一列，
--    按 job 的周期写成 `YYYY-MM-DD` / `YYYY-Www` / `YYYY-MM`（D98）。
--    `note` 与 `error` 分开：`error` 是「没成」，`note` 是「成了，但只更新了 3 只」——
--    把后者写进 error，健康页会把一次正常补跑报成失败。
--
-- 2) `scheduler_heartbeats` 是「这台机器上调度器还活着吗」的唯一依据。一行代表一次
--    启动，`beat_at` 每 tick 原地更新（不增行）。单实例守卫读它（D100），健康页读它
--    （D101）。`beat_id` 不是外键：两个进程同时启动时，先写的那一行必须留下痕迹。
CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
    id         VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    beat_at    TIMESTAMPTZ NOT NULL,
    note       VARCHAR
);

-- DuckDB 的 ADD COLUMN 不能带 NOT NULL（0015/0016/0017 的头注同一条）；
-- IF NOT EXISTS 让「迁移重放」与「新库一次建全」走同一条语句。
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS period_key VARCHAR;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS note VARCHAR;
