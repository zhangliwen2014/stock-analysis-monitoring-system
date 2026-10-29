-- 0027_f10_detail.sql — 个股页 F10：公司概况、公告，以及港股主要指标源（D214–D216）
--
-- 两张新表都只服务「看」，不参与任何计算或回测：
--   * company_profiles 一票一行，重复执行是覆盖；
--   * announcements 一次只取最近一页，缺的是更早的条目，不是某个数（D216）。
-- 键的形态由写入仓库归一（D50）：HK/700 与 HK/00700 不能各存一行。
--
-- 种子把两个新源排进链尾：它们只提供概况/公告/港股指标，不会改变现有日线链的顺序；
-- US 不登记——现有链上没有能给美股公司概况或公告的源，宁可明说缺（D214）。

CREATE TABLE IF NOT EXISTS company_profiles (
    market         TEXT NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT,
    full_name      TEXT,
    name_en        TEXT,
    industry       TEXT,
    industry_csrc  TEXT,
    exchange       TEXT,
    board          TEXT,
    listed_on      DATE,
    founded_on     DATE,
    isin           TEXT,
    lot_size       INTEGER,
    chairman       TEXT,
    president      TEXT,
    secretary      TEXT,
    legal_person   TEXT,
    phone          TEXT,
    email          TEXT,
    website        TEXT,
    address        TEXT,
    reg_address    TEXT,
    employees      INTEGER,
    auditor        TEXT,
    fiscal_year_end TEXT,
    hk_connect     BOOLEAN,
    stock_type     TEXT,
    summary        TEXT,
    source         TEXT NOT NULL,
    fetched_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market, code)
);

CREATE TABLE IF NOT EXISTS announcements (
    market       TEXT NOT NULL,
    code         TEXT NOT NULL,
    art_code     TEXT NOT NULL,
    published_on DATE NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT,
    url          TEXT NOT NULL,
    source       TEXT NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market, code, art_code)
);

INSERT OR IGNORE INTO data_sources (name, kind, enabled, options_json, note) VALUES
    ('eastmoney_f10', 'eastmoney_f10', TRUE, '{}',
     '东方财富 F10 公司概况与公告（A/HK）'),
    ('eastmoney_hk_f10', 'eastmoney_hk_f10', TRUE, '{}',
     '东方财富港股主要财务指标（只有 HK；无披露日，不进 PR）');

INSERT OR IGNORE INTO data_source_routes (market, name, priority) VALUES
    ('A',  'eastmoney_f10',    6),
    ('HK', 'eastmoney_f10',    4),
    ('HK', 'eastmoney_hk_f10', 4);
