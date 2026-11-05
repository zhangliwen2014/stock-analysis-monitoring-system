-- 0051_eastmoney_global.sql — 港美股补第二个源：分钟K、HK指数日线、HK上市日期、HK/US基本面、快照（#387 / D324）
--
-- HK/US 的五条能力原先各只有一家：分钟K只有腾讯、HK 指数日线只有腾讯、HK 上市日期
-- 只有东财 F10、HK/US 基本面只有东财 F10、HK/US 快照只有腾讯。这一路是东财的另一个
-- 域名与另一套接口族（push2his/push2/datacenter），与 A 股 `eastmoney` 的日线不共用路径。
--
-- 实测（2026-11-05/06）：分钟K 时间列是北京时间、US 要折回纽约；HK 指数 secid 恒指/国企
-- 在 100、恒生科技在 124；HK 上市日期取 `LISTING_DATE`（不是 `FOUND_DATE`）；HK 基本面
-- 是 F10 主要指标全史（无披露日），US 基本面与 `eastmoney_us_f10` 同一个数据中心报表
-- （D322 第 2 档：备注写明「同源」）；快照 `clist` 一页硬上限 100，整表 HK 4745 行 35–60s、
-- US 13899 行约 167s，所以它在快照链上排腾讯之后（D325）。
--
-- 优先级定为 3：能力过滤后，HK/US 的分钟K、快照与 HK 指数日线都紧跟腾讯（2），
-- 基本面排在 yfinance 之后、两家 F10 专用源之前（HK 4 / US 5）。HK 的 3 与
-- `akshare_hk` 同号，按名字排序互不遮挡——两者没有一条能力重叠。
INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('eastmoney_global', 'eastmoney_global', TRUE, '{}',
     '东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与快照；基本面与 eastmoney_hk_f10 / eastmoney_us_f10 同源；快照整表分页，实测 HK 约 35–60s / US 约 167s）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('HK', 'eastmoney_global', 3),
    ('US', 'eastmoney_global', 3);
