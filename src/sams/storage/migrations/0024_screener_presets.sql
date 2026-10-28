-- 0024：筛选方案（D182）。条件以 JSON 存：形状跟着词表走，加字段不必改 schema
CREATE TABLE IF NOT EXISTS screener_presets (
    id              VARCHAR PRIMARY KEY,
    user_id         VARCHAR NOT NULL REFERENCES users(id),
    name            VARCHAR NOT NULL,
    market          VARCHAR NOT NULL,
    conditions_json VARCHAR NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, name)
);
