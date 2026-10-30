-- 0029：筛选方案带说明（D220）。
--
-- 说明是选方案时读的那句话，可空；老方案迁移后是空串，列表显示「—」，不替人编一句。
-- 名字仍按用户唯一（0024 的约束），说明不进唯一键。

-- DuckDB 不支持 `ADD COLUMN IF NOT EXISTS ... NOT NULL` 的组合；迁移只跑一次，
-- 用普通 ADD COLUMN 保证老行拿到空串而不是 NULL。
ALTER TABLE screener_presets ADD COLUMN description TEXT NOT NULL DEFAULT '';
