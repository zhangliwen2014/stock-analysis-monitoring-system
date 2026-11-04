-- 0041_symbol_aliases.sql — 港美股的检索别名（S4-C）
--
-- 主档名称是源给的那一串：美股是 "Apple Inc. - Common Stock"，港股虽是中文却常带
-- "－Ｗ" 这类后缀。用户敲的是「苹果」「腾讯」——别名只补检索这一格，不改主档名称，
-- 也不参与任何计算：一只票的身份仍然是 (market, code)。
--
-- 种子只为常见票兜个底，不是完整中文名库；用户自己的别名可以按同一张表追加，
-- `source` 区分是谁写的。主键含别名，同一票多个叫法各占一行。

CREATE TABLE IF NOT EXISTS symbol_aliases (
    market VARCHAR NOT NULL,
    code   VARCHAR NOT NULL,
    alias  VARCHAR NOT NULL,
    source VARCHAR NOT NULL DEFAULT 'seed',
    PRIMARY KEY (market, code, alias)
);

INSERT OR IGNORE INTO symbol_aliases (market, code, alias, source) VALUES
    ('US', 'AAPL',  '苹果',           'seed'),
    ('US', 'MSFT',  '微软',           'seed'),
    ('US', 'NVDA',  '英伟达',         'seed'),
    ('US', 'TSLA',  '特斯拉',         'seed'),
    ('US', 'AMZN',  '亚马逊',         'seed'),
    ('US', 'GOOGL', '谷歌',           'seed'),
    ('US', 'META',  '脸书',           'seed'),
    ('US', 'NFLX',  '奈飞',           'seed'),
    ('US', 'AMD',   '超威半导体',     'seed'),
    ('US', 'INTC',  '英特尔',         'seed'),
    ('US', 'BRK.B', '伯克希尔',       'seed'),
    ('US', 'BABA',  '阿里巴巴',       'seed'),
    ('US', 'JD',    '京东',           'seed'),
    ('US', 'PDD',   '拼多多',         'seed'),
    ('US', 'BIDU',  '百度',           'seed'),
    ('US', 'NIO',   '蔚来',           'seed'),
    ('US', 'XPEV',  '小鹏',           'seed'),
    ('US', 'LI',    '理想',           'seed'),
    ('US', 'TSM',   '台积电',         'seed'),
    ('HK', '00700', '腾讯',           'seed'),
    ('HK', '03690', '美团',           'seed'),
    ('HK', '01810', '小米',           'seed'),
    ('HK', '01211', '比亚迪',         'seed'),
    ('HK', '00941', '中国移动',       'seed'),
    ('HK', '09988', '阿里巴巴',       'seed'),
    ('HK', '09618', '京东',           'seed'),
    ('HK', '09888', '百度',           'seed'),
    ('HK', '01024', '快手',           'seed'),
    ('HK', '02020', '安踏',           'seed'),
    ('HK', '09999', '网易',           'seed'),
    ('HK', '00388', '香港交易所',     'seed');
