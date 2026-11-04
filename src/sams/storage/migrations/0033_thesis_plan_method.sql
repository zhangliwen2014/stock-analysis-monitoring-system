-- 0033_thesis_plan_method.sql — 论点价位的来路（S1-A）
--
-- `plan_version` 说这条论当时按哪一套方法落的数（decision-v0）；`plan_method` 存
-- 止损/入场/目标的规则名、PR 口径与读数日。手填的价位两列都留空：没有式子可 backing，
-- 给它一个版本号等于让屏幕替一片空白编一个出处。
--
-- 两列都可空：0013 之前的行没有这个信息，补一个默认值会造出「当时也按 decision-v0 落过」的假记录。

ALTER TABLE theses ADD COLUMN IF NOT EXISTS plan_version VARCHAR;
ALTER TABLE theses ADD COLUMN IF NOT EXISTS plan_method VARCHAR;
