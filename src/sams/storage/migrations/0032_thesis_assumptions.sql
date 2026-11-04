-- 0032_thesis_assumptions.sql — 证伪台账（D232 P0-3）
--
-- 一条假设 = 指标 + 方向 + 阈值 + 复核日。状态与「判出来的那个数」一起存：
-- 回看时要知道它当时被什么数证伪，而不是只有一个红点。
-- 归属不在本表：所有读写都先经 theses 验 user_id。

CREATE TABLE IF NOT EXISTS thesis_assumptions (
    id              VARCHAR PRIMARY KEY,
    thesis_id       VARCHAR NOT NULL,
    metric          VARCHAR NOT NULL,
    op              VARCHAR NOT NULL,
    threshold       DOUBLE NOT NULL,
    due_on          DATE,
    note            VARCHAR,
    status          VARCHAR NOT NULL DEFAULT 'open',
    evaluated_on    DATE,
    evaluated_value DOUBLE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_thesis_assumptions_thesis
    ON thesis_assumptions (thesis_id);
