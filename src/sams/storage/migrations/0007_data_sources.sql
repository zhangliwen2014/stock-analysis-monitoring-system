-- 0007_data_sources.sql — 数据源从「写在配置文件里」变成「库里的可维护记录」
--
-- 动机有两层：
--   1. 港美股此前只有 yfinance 一个源，主源被限流就全线抓瞎（spec §18 U5）。本轮补进
--      腾讯（A/HK/US 三市场）与新浪（A 股）两个独立故障域的备源，并把降级链存下来。
--   2. 「用哪些源、谁先谁后、谁停用了」是运维决定，不该要求改文件重启。存进库之后，
--      管理后台（M4 的 web，M1 的 `sams source`）改的是数据，实现代码一行不动。
--
-- 边界：本表存的是 `kind`（已注册实现的**名字**），不是 import 路径。
-- 允许配置任意模块名等于给运维界面开一个远程代码执行入口，所以 kind 必须在
-- datasource.sources 的注册表里存在才允许写入——校验放在 services 层（datasource 不能
-- 被 storage import，分层守卫会红）。
--
-- 种子里带降级顺序：A 股 akshare → eastmoney → 腾讯 → 新浪；港美股 yfinance → 腾讯。
-- 腾讯排第三而不是第二，是因为东财与 akshare 同源（akshare 的 A 股日线本就封装东财），
-- 真正的独立故障域是新浪与腾讯；顺序只是优先级，不影响「谁都能单独顶上」。

CREATE TABLE IF NOT EXISTS data_sources (
    name         VARCHAR PRIMARY KEY,
    kind         VARCHAR NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    options_json VARCHAR NOT NULL DEFAULT '{}',
    note         VARCHAR,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 一个市场一条链：priority 从 1 开始，越小越先试。
-- 拆成独立表而不是在 data_sources 里存 CSV：改顺序要动多个市场时，
-- 一行的 CSV 会让「A 股把 tencent 提到第 1 位」顺手改掉 HK 的顺序
CREATE TABLE IF NOT EXISTS data_source_routes (
    market   VARCHAR NOT NULL,
    name     VARCHAR NOT NULL,
    priority INTEGER NOT NULL,
    PRIMARY KEY (market, name)
);

-- ============ 种子：本轮新增的备源 + 完整降级链 ============

INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('akshare',   'akshare',   TRUE,  '{}', 'A 股主源（底层封装东财接口）'),
    ('eastmoney', 'eastmoney', TRUE,  '{}', 'A 股备源，与 akshare 同源'),
    ('tencent',   'tencent',   TRUE,  '{}', '腾讯 gtimg，A/HK/US 三市场不复权日线'),
    ('sina',      'sina',      TRUE,  '{}', '新浪 A 股日线，只覆盖最近约 1023 根'),
    ('yfinance',  'yfinance',  TRUE,  '{}', 'Yahoo 港美股日线');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('A',  'akshare',   1),
    ('A',  'eastmoney', 2),
    ('A',  'tencent',   3),
    ('A',  'sina',      4),
    ('HK', 'yfinance',  1),
    ('HK', 'tencent',   2),
    ('US', 'yfinance',  1),
    ('US', 'tencent',   2);
