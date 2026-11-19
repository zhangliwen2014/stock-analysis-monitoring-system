-- 0054_mute_expiry.sql — 单票静音必须有到期（D361 / R3-10）
--
-- 可空只留给迁移前的行与只读旧表：它们没有到期，继续生效，台账标「历史（无到期）」。
-- 新写入一律填（MuteRepo.add 的 days 参数），NULL 不是「永久」的新写法。
ALTER TABLE alert_mute_scopes ADD COLUMN expires_at TIMESTAMPTZ;
