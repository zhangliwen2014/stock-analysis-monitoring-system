-- 0036_company_business.sql — 核心业务六字段（S2 / D240）
--
-- 键是 (market, code, field)：同一只票的六项各自带来源与日期，
-- 不用「一票一行六列」——那样一个字段的来源日期会污染另外五个。
-- 这是公司事实，不是个人笔记：不带 user_id，confirmed_by 只记录谁确认的。
--
-- 空值不落行：「没有」由缺行表达，读取侧据此回落到系统草稿/缺失，
-- 而不是把空字符串当成一个已确认的答案。

CREATE TABLE IF NOT EXISTS company_business (
    market       VARCHAR NOT NULL,
    code         VARCHAR NOT NULL,
    field        VARCHAR NOT NULL,
    value        VARCHAR NOT NULL,
    source       VARCHAR NOT NULL,
    as_of        DATE NOT NULL,
    confirmed_by VARCHAR,
    confirmed_at TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, code, field)
);
