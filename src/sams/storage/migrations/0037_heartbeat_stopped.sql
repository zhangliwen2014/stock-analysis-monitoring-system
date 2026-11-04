-- 0037_heartbeat_stopped.sql — 正常退出的心跳打标记（实机重启暴露）
--
-- 单实例守卫读的是「最近一拍是不是还新鲜」。正常停服不会删掉那一行，
-- 于是计划内的重启若落在 5 分钟阈值内，调度线程会被自己的上一世挡死，
-- 且线程异常退出后页面看起来照常（只是没有 job 在跑）。
--
-- 停止标记让守卫区分「还在跳」与「跳完收工了」；历史行仍保留可查。

ALTER TABLE scheduler_heartbeats ADD COLUMN stopped_at TIMESTAMPTZ;
