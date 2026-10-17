-- 0004 迁移校验和：让「改了已应用的迁移」变成启动即失败，而不是两台机器两套 schema。
--
-- 0001 全是 CREATE TABLE IF NOT EXISTS，applied() 又只记文件名：改一次 0001
-- 对新库生效、对老库永久不生效，而 `sams health` 会显示「已是最新」。
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum VARCHAR;
