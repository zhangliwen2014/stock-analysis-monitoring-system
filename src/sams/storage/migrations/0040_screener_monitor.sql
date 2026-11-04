-- 0040_screener_monitor.sql — 方案自动监控与当前名单（S4-B / D254–D257）
--
-- 方案的「上次运行」与「更新的时刻」分开：`updated_at` 回答条件是什么时候写成这样的，
-- `last_run_at` 回答监控最后一次真的看过市场是什么时候。后者为 NULL 还兼作基线闸：
-- 新启用、改了条件或刚迁移之后的第一趟只记名单，不发通知——否则打开开关的当晚
-- 会收到几十条「新命中」，而那只是第一次看。
--
-- `screener_hits` 只存当前名单，不存历史：一只票离开再来，就是一次新的命中。
-- 主键不含 market：一只票的代码在同一方案里只可能出现一次，市场跟着行写。

ALTER TABLE screener_presets ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE screener_presets ADD COLUMN channel_ids VARCHAR NOT NULL DEFAULT '[]';
ALTER TABLE screener_presets ADD COLUMN last_run_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS screener_hits (
    user_id    VARCHAR NOT NULL,
    preset_id  VARCHAR NOT NULL,
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    first_seen DATE NOT NULL,
    last_seen  DATE NOT NULL,
    PRIMARY KEY (user_id, preset_id, code)
);

CREATE INDEX IF NOT EXISTS idx_screener_hits_preset
    ON screener_hits(user_id, preset_id);
