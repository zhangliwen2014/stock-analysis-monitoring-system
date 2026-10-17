-- 0005_source_health_tristate.sql — source_health.ok 去掉 NOT NULL
-- 动机：0002 建表时 ok 还是二态；本轮把 SourceRecord.ok 改成 bool | None
-- （None = 这个源从没被路由过），库却存不下这个值域，
-- repos/source_health.py 里「NULL → None」的解码分支因此永远走不到，
-- 而下一个人真去落「未知」状态时会当场撞 ConstraintException。
-- DuckDB 不能 ALTER 掉约束，照 0003 的办法重建。

CREATE TABLE IF NOT EXISTS source_health_new (
    name                 VARCHAR PRIMARY KEY,
    markets              VARCHAR NOT NULL DEFAULT '',
    ok                   BOOLEAN,
    last_error           VARCHAR,
    last_ok_at           DATE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT OR REPLACE INTO source_health_new SELECT * FROM source_health;

DROP TABLE source_health;

ALTER TABLE source_health_new RENAME TO source_health;
