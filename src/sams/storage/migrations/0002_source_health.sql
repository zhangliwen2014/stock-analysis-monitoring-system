-- 0002_source_health.sql — 数据源健康落库
-- 动机：采集与 `sams health` 是两个进程，进程内的失败记录一退出就没了，
-- 健康检查因此永远报「一切正常」。这里把最近一次结果持久化，按源名幂等覆盖。

CREATE TABLE IF NOT EXISTS source_health (
    name                 VARCHAR PRIMARY KEY,
    markets              VARCHAR NOT NULL DEFAULT '',
    ok                   BOOLEAN NOT NULL,
    last_error           VARCHAR,
    last_ok_at           DATE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
