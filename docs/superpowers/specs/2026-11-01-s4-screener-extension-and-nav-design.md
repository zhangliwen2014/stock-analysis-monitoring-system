# S4 选股扩展与组合净值（设计）

> 目标：把「选股」从快照+年报的截面，扩到**技术面**与**自动新命中**；把「监控」从价格规则
> 扩到方案名单的变化；把「持仓」从成本/浮盈扩到**净值、时间加权收益、回撤与限额**。
> 每个数都要能回答「哪天的、按什么口径、缺了哪一段」，缺数据时给缺口，不补零、不猜。
>
> 依据：`docs/superpowers/specs/2026-10-31-expert-review.md` 的 S4 行与
> `2026-10-31-product-platform-gap-analysis.md` 的选股/持仓差距。
> 前置：S0-E 报价读模型、S1 决策卡/告警、S2 行业/同业、S3 资讯与静音分组。

## 0. 现状事实（决定切片边界）

- 选股词表只有 `snapshot` / `financials` / `derived` 三种来源；技术指标层已存在
  （`analysis/indicators.py` 的 MA/RSI/量比），但**只走个股页**，选股引擎没有读日线。
- `bars_daily` 只有裸价；指标必须过 `adjusted_series()`（公司行为覆盖 + 未标记跳变 + 真实收盘
  三道闸）。线上覆盖：A 股 497 只票有确认区间（507 只有日线、496 只 ≥250 根）；
  港股 1 只、美股 0 只。技术面字段因此**v0 只开 A 股**。
- `scan_results` 主键 `(date, market, strategy_id)` 且 `INSERT OR REPLACE`；`strategy_id`
  现在是 `screen:{方案名}:{条件哈希}`，**跨用户同名同条件会互相覆盖**。方案没有
  `enabled/channel_ids/last_run_at`，盘后调度也不跑方案。
- `symbol_master` 已有美股 5919 只（AAPL 等代码可搜），但没有中文别名（搜「苹果」查无）；
  全局导航没有搜索框，候选面板也没有任何链到个股页的入口。
- `sim_cash_moves` 只有 `reason/actor`，没有 `kind/ref`；分红不入账；账户里看到的现金
  永远是「存进来的钱减掉买入」，不是「含股息的现金」。
- 没有 `nav_daily`、TWR、回撤；没有限额表，持仓页只有权重与集中度提示，没有可配置红线。

## 1. 范围与切片

| 切片 | 内容 | 验收 |
| --- | --- | --- |
| S4-A | 技术面字段：新增 `technical` 来源（MA20/60/250 偏离、RSI14、5 日量比、52 周位置），A 股 v0；快照日为止的复权序列，逐票过闸 | 同一快照日重跑结果一致；无覆盖/无日线的票记「无数」并给下一步；全量测试与 ruff 通过 |
| S4-B | 方案自动监控：迁移 0040（`screener_presets.enabled/channel_ids/last_run_at`、`screener_hits`），盘后 job 逐方案跑，**新进名单**发一条告警 | 首次运行只记基线不发；次日新进 1 只发 1 条；反复跑同一天 0 新增；静音 `kind=screen` 生效 |
| S4-C | 美股/港股中文别名 + 全局搜索：迁移 0041 `symbol_aliases` + 常见美股中文名种子；导航栏搜索框；候选面板与搜索结果链到个股页 | 搜「苹果」返回 AAPL，点击进 `/symbol/US/AAPL`；搜 AAPL 同样可达；A/HK 既有检索不回归 |
| S4-D | 分红入账 + 净值：迁移 0042（`sim_cash_moves.kind/ref`）、0043 `nav_daily`；除权日按税前毛额入账（幂等）；盘后重建 NAV/TWR/最大回撤 | 同一除权日重跑 0 重复入账；NAV 卡显示区间、缺口天数与口径；缺价/缺汇率的日整日 `complete=false` |
| S4-E | 限额：迁移 0044 `portfolio_limits`；单票/市场/行业权重与回撤四条；持仓页红标与摘要尾行 | 自定义线才判违规；默认线只作参考；数据不全显示「无法判定」而不是 0 |

切片顺序按依赖：S4-A → S4-B（自动跑技术面方案）→ S4-C（独立，可并行）→ S4-D → S4-E。

## 2. 数据模型

```sql
-- 0040：方案自动监控（S4-B）
ALTER TABLE screener_presets ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE screener_presets ADD COLUMN IF NOT EXISTS channel_ids VARCHAR NOT NULL DEFAULT '[]';
ALTER TABLE screener_presets ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMPTZ;

-- 名单成员表：一行 = 这只票现在在这个方案里。离开名单即删行，再进算新命中
CREATE TABLE IF NOT EXISTS screener_hits (
    preset_id  VARCHAR NOT NULL,
    market     VARCHAR NOT NULL,
    code       VARCHAR NOT NULL,
    first_seen DATE NOT NULL,
    last_seen  DATE NOT NULL,
    PRIMARY KEY (preset_id, market, code)
);
```

```sql
-- 0041：中文别名（S4-C）。代码仍是唯一键；别名只影响检索，不改主档
CREATE TABLE IF NOT EXISTS symbol_aliases (
    market VARCHAR NOT NULL,
    code   VARCHAR NOT NULL,
    alias  VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    PRIMARY KEY (market, code, alias)
);
CREATE INDEX IF NOT EXISTS idx_symbol_aliases_alias ON symbol_aliases (alias);
```

```sql
-- 0042：现金流水分类（S4-D）。历史行按 reason 前缀回填，判不出的留 manual
ALTER TABLE sim_cash_moves ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'manual';
ALTER TABLE sim_cash_moves ADD COLUMN IF NOT EXISTS ref VARCHAR;
CREATE UNIQUE INDEX IF NOT EXISTS uq_sim_cash_moves_ref
    ON sim_cash_moves (account_id, kind, ref);
```

```sql
-- 0043：每日净值（S4-D）。只写能算的日；算不出的日 complete=false，nav 为 NULL
CREATE TABLE IF NOT EXISTS nav_daily (
    account_id   VARCHAR NOT NULL,
    date         DATE NOT NULL,
    currency     VARCHAR NOT NULL,
    nav          DOUBLE,
    cash         DOUBLE,
    market_value DOUBLE,
    external_flow DOUBLE NOT NULL DEFAULT 0,
    complete     BOOLEAN NOT NULL,
    stale_prices INTEGER NOT NULL DEFAULT 0,
    note         VARCHAR,
    PRIMARY KEY (account_id, date)
);
```

```sql
-- 0044：限额（S4-E）。account_id 为 NULL = 该用户全部账户
CREATE TABLE IF NOT EXISTS portfolio_limits (
    id         VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL,
    account_id VARCHAR,
    kind       VARCHAR NOT NULL,   -- single / market / industry / drawdown
    scope      VARCHAR,            -- code / market / 行业名；drawdown 为空
    threshold  DOUBLE NOT NULL,    -- 小数比值（0.2 = 20%）
    note       VARCHAR,
    created_at TIMESTAMPTZ NOT NULL
);
```

## 3. S4-A 技术面字段

### 3.1 词表

新增 `Field.source == "technical"`，`markets={"A"}`，数值全部在**快照日收盘**上算：

| 字段 | 含义 | 单位 | 备注 |
| --- | --- | --- | --- |
| `ma20_gap` | 收盘 ÷ MA20 − 1 | 小数比值 | 站上/跌破 20 日线 |
| `ma60_gap` | 收盘 ÷ MA60 − 1 | 小数比值 | 中期趋势 |
| `ma250_gap` | 收盘 ÷ MA250 − 1 | 小数比值 | 年线位置 |
| `rsi14` | Wilder RSI(14) | 0–100 | 超买超卖 |
| `vol_ratio_5d` | 当日量 ÷ 前 5 日均量 | 倍 | 近 6 根内有送转/拆股则记「无数」 |
| `pos_52w` | (收盘 − 52 周最低) ÷ (最高 − 最低) | 0–1 | 区间位置；最高=最低时记「无数」 |

`source` 的语义在 `screener_fields.py` 的注释里补一行：`technical` = 引擎按快照日读日线现算，
不落库、不来自快照列。

### 3.2 读取与闸

- 一次选股只对**票池**读日线：`BarRepo.load(market, codes, end=snapshot_day)`，每票取最后
  260 根（MA250 + RSI 预热）。A 股票池约 500 只、24 万行，单次读可接受；不建指标表。
- 公司行为与覆盖按市场批量读（新增 `CorpActionRepo.list_market(market, until=day)`、
  `CorpActionsCoverageRepo.for_market(market)`），再按代码分组；不逐票查库。
- 逐票调 `adjusted_series(...)`，`today=snapshot_day`（PIT：快照日之后才发生的除权不进序列），
  `listed_on` 取主档。抛 `NotAdjustable` / `NotCovered` 的票：所有技术字段记 `None`，
  原因进 `MissingGroup`，**不退回裸价**。
- `ScreenerService.__init__` 增加 `bars/actions/coverage/symbols` 依赖；`bootstrap` 装配。

### 3.3 缺口分组

新增四个原因（同一原因只报一次，附代码）：

- `日线不足：MA250 要 250 根（港股/美股 v0 不提供技术面字段）`
- `公司行为覆盖未确认，无法复权：sams sync actions --market A --code <代码>`
- `公司行为无法折算或有未标记跳变，复权序列被拒绝：先补齐该票的公司行为`
- `近 6 个交易日有送转/拆股，量比口径不成立`（只影响 `vol_ratio_5d`）

结果新增 `technical_coverage=(可算票数, 票池)`，并进 `basis_note`：
「技术面字段按快照日复权收盘现算，只覆盖已确认公司行为区间的 N/M 只；其余记「无数」」。

## 4. S4-B 方案自动监控

- **身份**：`preset_strategy_id(preset_id, conditions)` 改为 `preset:{preset_id}:{条件哈希}`。
  一次性的代价是旧 `scan_results` 行不再被页面认作「上次跑」；它们仍留在库里，
  下次运行即恢复。换来的是跨用户同名方案不再互相覆盖。
- **调度**：`build_jobs` 在每个市场的 `spot` 与 `rules` 之间登记 `screener` job（DAILY）。
  job 读所有 `enabled` 方案、按方案市场跑 `ScreenerService.run`；无快照/空票池记「跳过」，
  不失败。仅用当日晚间快照，数据日 = 结果 `day`。
- **基线**：`last_run_at IS NULL`（新启用、改了条件、刚迁移）的那一次**只写 `screener_hits`
  与 `scan_results`，不发通知**，避免「一开监控收到 50 条」。用户改条件时把 `last_run_at`
  置空。
- **新命中**：本次命中集合与 `screener_hits` 比对；不在表里的 = 新命中，写成员行
  `first_seen=day`；在表里的更新 `last_seen`。本次缺席的行删除（再进算新命中）。
  反复跑同一天：成员不变，0 新增，0 推送。
- **通知**：一只新票一条，复用告警中心：`rule_id=None`、`stage='hit'`、`market/code` 为真实标的、
  `payload.kind='screen'`、`preset_id/preset_name/first_seen/conditions`；标题「方案《…》新命中」，
  正文含关键字段与个股页链接。渠道取方案 `channel_ids`；空 = 只进告警中心。
  静音新增 `kind='screen'`（`all` 覆盖它）。
- **页面**：方案卡增加「自动监控」开关、渠道多选、上次运行日、当前名单数；编辑条件后提示
  「下次运行只记基线，不推送」。`/alerts` 对 `kind=screen` 显示「选股」并链个股页。
- **摘要**：`DigestService` 在规则栏后追加「选股方案」行：每方案一行「新命中 N 只（代码…）」，
  被 `screen` 静音的条目不出现在摘要。

## 5. S4-C 中文别名与全局搜索

- `symbol_aliases` 只做检索：`SymbolListRepo.search` 先按代码/拼音/名称取，再按别名命中
  追加（去重、同一档）；别名大小写不敏感，中文原样。
- 迁移播种 30 个常见美股/港股中文名（苹果 AAPL、微软 MSFT、英伟达 NVDA、特斯拉 TSLA、
  亚马逊 AMZN、谷歌 GOOGL、Meta META、奈飞 NFLX、 AMD、英特尔 INTC、伯克希尔 BRK.B、
  阿里巴巴 BABA、京东 JD、拼多多 PDD、百度 BIDU、蔚来 NIO、小鹏 XPEV、理想 LI、
  台积电 TSM、腾讯 00700、美团 03690、小米 01810、比亚迪 01211、中国移动 00941 等），
  `source='seed'`。别名不覆盖主档名称，只补充。
- 导航栏加搜索框（每个页面可见）：`hx-get /api/symbol-search?scope=nav`，结果显示
  市场/代码/名称/最近收盘，整行链 `/symbol/{market}/{code}`；键盘 Esc 收起。
- 现有候选面板（自选/成交）同样给代码与名称加个股页链接；「加入自选」按钮保留。

## 6. S4-D 分红入账与净值

### 6.1 现金流水分类

- `kind ∈ deposit/withdraw/fx/buy/sell/fee/dividend/manual`。新写入由 `LedgerService` 与
  `DividendService` 填；历史行按 `reason` 已知前缀回填（存入/取出/换汇/买入/卖出/手续费），
  其余留 `manual`（不猜）。
- `ref` 只给可幂等的自动入账（分红 `market:code:ex_date`）；手工与交易行为 NULL。
  唯一索引 `(account_id, kind, ref)` 承担「同一笔分红只入一次」。

### 6.2 分红入账

- `DividendService.post(account, as_of)`：对账户持有过的每只票，读 `corp_actions` 中
  `kind='dividend'` 且 `ex_date` 在账户开立后、`as_of` 前的记录；数量取**除权日前**
  （`trade_date < ex_date`）已成交的累计持股；`delta = qty × cash_per_share`。
- 入账：`kind='dividend'`、`ref` 如上、`actor_id='system'`、reason 写明代码/除权日/每股/数量；
  同一 `ref` 已存在则跳过。**税前的毛额**：A 股红利税在卖出时按持有期补扣，v0 不建模，
  页面文案明说「税前，未扣红利税」。
- 源没有派息日，v0 按**除权日**入账；不编造派息日。
- 调度：`dividends` job 在 `actions` 之后、`rules` 之前，逐账户入账；CLI
  `sams portfolio dividends --account <id> --as-of …` 供补账。
- 拆股/送转 v0 **不动持仓股数**：查到区间内持仓票有 `split/bonus/transfer` 时，净值页
  给出黄色提示「本区间有送转/拆股，持仓股数未调整，净值可能失真」，不自动改用户台账。

### 6.3 NAV / TWR / 回撤

- `NavService.rebuild(account, start, end)`：
  - 逐日重放成交得到收盘持仓；收盘价取当日 bar（停牌用 ≤ 当日的最后收盘，超过 5 个交易日
    无更新记为缺价）；现金 = 逐日重放 `sim_cash_moves`；全部按当日汇率折成账户报告币。
  - 任一持仓/币种缺价或缺汇率：`complete=false`、`nav=NULL`、`note` 写缺哪一项；
    绝不用 0 或前值补出一个看起来平滑的净值。
  - `external_flow` = 当日存入 − 取出（报告币）；买入/卖出/手续费/分红是内部变动，不计入。
  - 写 `nav_daily`，重跑同一日覆盖同一行。
- TWR：只在**连续完整日**上算 `r_t = (NAV_t − F_t) / NAV_{t−1} − 1`，累计 `∏(1+r_t)−1`；
  中间有缺口就断开，页面显示「自 YYYY-MM-DD 起（缺口 N 天）」，不把缺口当 0 收益。
- 最大回撤：同一段连续完整日的 NAV 峰谷跌幅；与 TWR 并列显示，注明「按净值、不是按 TWR」。
- 调度：`nav` job 在 `dividends` 之后、`digest` 之前，逐账户重建最近 400 天；CLI
  `sams nav rebuild --account <id> --days 400`。
- 页面：持仓页顶部「组合净值」卡（异步局部）：净值、当日变动、区间 TWR、最大回撤、
  区间与缺口天数；下方小折线（ECharts，复用个股页的加载方式）。

## 7. S4-E 限额

- `LimitService.evaluate(account)` 读 `portfolio_limits` 与当日权重（`Exposure` + NAV）：
  - `single`：单票市值 ÷ NAV > 阈值；
  - `market`：市场市值 ÷ NAV > 阈值；
  - `industry`：行业市值 ÷ NAV > 阈值（行业来自主档，缺行业的持仓归「未分类」并计入缺口）；
  - `drawdown`：当前 NAV 相对区间峰值回撤 > 阈值（复用 S4-D 的完整日序列）。
- 只判**用户自定义**的线；没有配置时展示三条参考线（单票 20%、行业 40%、回撤 20%）为灰色
  「参考」，不算违规。
- 数据不全（缺价/缺行业/NAV 缺口）时该条显示「无法判定」，不显示 0%。
- 页面：持仓页「限额」卡（红/灰/无法判定），摘要尾行列违规条目。v0 不单独推送限额告警
  （避免权重日常波动刷屏；方案新命中与价格规则已覆盖更硬的信号）。
- 摘要尾行按**账户所属市场**归属，不按持仓标的的市场：A 股账户里的港股仓位是这个账户的风险，
  把它分到 HK 摘要等于对只收 A 摘要的人隐瞒这条红线（实机跑出来的，`LimitRow.market` 已删）。

## 8. 缺失与失败

- 技术面：无日线/无覆盖/被闸拒绝 → 该票该字段「无数」，原因分组；不降级裸价。
- 方案 job：无快照 → `skipped`；单方案异常 → 记录并继续其余方案（一个坏方案不拖垮当晚）。
- 分红：股数为 0 → 不入账；同一 `ref` → 跳过；无 `cash_per_share` 的 dividend 行在写入层
  已被拒，读到即跳过并 warn。
- NAV：缺价、缺汇率、送转未调整 → `complete=false` 或黄色提示；TWR/回撤不跨缺口拼接。
- 别名：查不到就是查不到，不返回「相似公司」猜测。

## 9. 测试（红→绿，中文名）

| 文件 | 关键行为 |
| --- | --- |
| `tests/services/test_screener_technical.py` | MA/RSI/量比/52 周位置数值；快照日 PIT；无覆盖整票无数；量比遇送转记无数；同一日重跑一致 |
| `tests/web/test_screen_technical.py` | A 股可选技术字段、港美不可选；无数原因分组显示；结果行链接保留 `back` |
| `tests/services/test_screener_monitor.py` | 首次只记基线；新进/退出/再进；同日重跑 0 推送；条件改动重置基线；方案异常不拖垮其余 |
| `tests/services/test_screener_alerts.py` | 一票一条 payload kind=screen；渠道为空只入库；`screen` 静音不发 |
| `tests/storage/test_screener_hits.py` | 成员表幂等；last_seen 更新；缺席删除；同日重跑稳定 |
| `tests/storage/test_symbol_aliases.py` | 别名大小写不敏感；与代码/名称结果去重；种子可重复应用 |
| `tests/web/test_search_links.py` | 搜「苹果」出 AAPL；面板/导航结果链到个股页 |
| `tests/services/test_dividends.py` | 除权日前持股 × 每股；重跑 0 重复；卖出后再除权不重复；税前文案 |
| `tests/services/test_nav.py` | 逐日重放；外部流入不计收益；缺价整日 complete=false；TWR 不跨缺口；最大回撤 |
| `tests/services/test_limits.py` | 单票/市场/行业超线；默认线不违规；无法判定 |
| `tests/test_scheduler_acceptance.py` | job 顺序：spot → screener → rules；actions → dividends → nav → digest |

## 10. 实机验收

1. `sams db migrate` → 0040–0044；全量 `pytest` + `ruff check src tests`。
2. A 股建一个含 `ma20_gap>=0` 与 `pr_now<=0.5` 的方案：结果与手动跑一致；无覆盖票出现在
   「无数」分组并给出同步命令。
3. 打开自动监控：第一次运行只有基线、无告警；把某只票临时移出条件再移回（或用测试阈值），
   次日出现一条「方案新命中」；同日重跑 0 新增。
4. 搜「苹果」→ AAPL → 个股页；搜「腾讯」→ 00700；A 股 600519 结果不变。
5. 找一个有分红的历史持仓账户：`sams portfolio dividends` 后现金增加一次，重跑 0 增加；
   持仓页净值卡出现 TWR/回撤与缺口天数。
6. 配一条单票 5% 限额：超线显示红标；删除后回灰；缺价票显示「无法判定」。
7. 更新评审文档 §7 的 S4 行与实测证据。

## 11. 决策记录

- **D251**：技术面字段 v0 只对 A 股开放；读快照日为止的日线，必须走 `adjusted_series()`；
  算不出的票记「无数」并给原因，不按裸价兜底。
- **D252**：首批六个技术字段：MA20/60/250 偏离、RSI14、5 日量比、52 周位置；量比窗口内
  有送转/拆股时记「无数」。
- **D253**：技术指标不落表，选股时按票池批量读日线现算；每票最多 260 根。
- **D254**：方案新增 `enabled/channel_ids/last_run_at`；`last_run_at IS NULL` 的那一次只记基线
  不发通知；改条件重置基线。
- **D255**：新命中去重靠 `screener_hits` 成员表；离开名单即删行，再进算新命中。
- **D256**：新命中一票一条告警，复用告警中心（`rule_id NULL` + `payload.kind=screen`）；
  渠道取方案配置；静音新增 `kind=screen`。
- **D257**：自动运行的 `scan_results` 键改为 `preset:{preset_id}:{条件哈希}`，修复跨用户
  同名方案互相覆盖；旧键行保留但不再视为「上次跑」。
- **D258**：美股/港股中文名走 `symbol_aliases` 检索表，不改主档；导航栏与候选面板都链个股页。
- **D259**：分红按除权日、税前毛额入账，`ref` 幂等；源无派息日；拆股/送转 v0 不改持仓，
  只在净值页提示失真。
- **D260**：NAV 只写能算的日；缺价/缺汇率整日 `complete=false`，TWR/回撤不跨缺口拼接。
- **D261**：限额 v0 只判用户自定义的单票/市场/行业/回撤；默认线只作参考；只上页面与摘要，
  不单独推送。
- **D262**：`sim_cash_moves` 补 `kind/ref`；历史行按 reason 已知前缀回填，判不出的留 `manual`。

## 12. 明确不做（S4 范围内）

- 不做技术面回测、盘中技术选股、分钟级指标选股。
- 不做港美股技术面字段（覆盖与日线都不够；先补数据，再开字段）。
- 不做自动拆股调整持仓、不做红利税模拟、不做派息日预测。
- 不做行业指数的成分与权重选股；不做做空/期权/期货。
- 不做限额的单独推送通道、不做组合优化建议。
- 不做别名的模糊匹配或自动翻译（种子之外由管理员后续补）。
