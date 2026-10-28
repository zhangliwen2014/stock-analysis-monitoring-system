-- 0025：每人的图表偏好（D193）。周期条顺序与均线窗口都是短列表，JSON 存：
-- 加一档周期不必改 schema；没有行=用默认，清空就是删掉这一行
CREATE TABLE IF NOT EXISTS chart_prefs (
    user_id      VARCHAR PRIMARY KEY REFERENCES users(id),
    periods_json VARCHAR NOT NULL,
    ma_json      VARCHAR NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
