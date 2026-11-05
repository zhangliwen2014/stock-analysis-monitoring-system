# 数据源盘点：16 种能力里 12 个格只有一个源，而其中 6 个此刻正坏着（#387，D322–D328）

一句话：降级链今天大多是**一条链上只有一个能答的源**，而那个源正在坏——`task_runs` 里过去
两天有 380 条 failed，几乎全部落在这 12 个孤源格上。「有备源」这件事目前是配置上的形状，
不是事实。本轮按实测补上真正会响的第二个源，并把崩在库里的 `TypeError` 归到它该在的那一类。

---

## 1. 盘点（2026-11-05 14:47 拉库副本，只读）

链快照（`data_sources.enabled` + `data_source_routes`，库里 13 个源全部启用、全部排进了链）：

```
A : akshare → eastmoney → tencent → sina → akshare_ths → akshare_cninfo → eastmoney_f10 → sina_quote
HK: yfinance → tencent → akshare_hk → eastmoney_f10 → eastmoney_hk_f10 → sina_quote
US: yfinance → tencent → sina_quote → nasdaq_trader → eastmoney_us_f10
```

能力 × 市场（数字 = 链上真有这个能力的源数；★ = 孤源，一挂就整条断；— = 没有源）：

| 能力 | A | HK | US |
|---|---|---|---|
| 日线 | 4 | 2 | 2 |
| 公司行为 | 3 | 2 | ★1 yfinance |
| 基本面 | ★1 akshare | ★1 eastmoney_hk_f10 | — |
| 按日估值 | ★1 akshare | ★1 yfinance | ★1 yfinance |
| 资金流 | ★1 akshare | — | — |
| 公司概况 | ★1 eastmoney_f10 | ★1 eastmoney_f10 | — |
| 公告 | ★1 eastmoney_f10 | ★1 eastmoney_f10 | — |
| 个股新闻 | ★1 eastmoney_f10 | ★1 eastmoney_f10 | — |
| 上市日期 | ★1 akshare | — | — |
| 汇率 | sina 与 yfinance 两个（不按市场路由） | | |
| 报价 | 2 | 2 | 2 |
| 代码表 | ★1 akshare | ★1 akshare_hk | ★1 nasdaq_trader |
| 指数日线 | 2 | ★1 tencent | ★1 tencent |
| 分时 | ★1 tencent | ★1 tencent | ★1 tencent |
| 分钟 K | 2 | ★1 tencent | ★1 tencent |
| 全市场快照 | ★1 tencent | ★1 tencent | ★1 tencent |

**孤源不是理论风险**。过去两天（含 11-05 当天）`task_runs` 里 `status='failed'` 的分布：

| job | 市场 | 条数 | 报错里那一句 |
|---|---|---|---|
| actions | US | 53 | 全链失败（yfinance `Too Many Requests` → 冷却中） |
| valuation | US | 53 | 同上 |
| valuation | HK | 53 | 同上 |
| actions | HK | 53 | 同上 |
| actions | A | 68 | 同花顺 `No tables found`（40 条）+ `冷却中`（28 条）＝ #383 |
| financials | A | 38 | 东财接口在库里崩成 `TypeError: 'NoneType' object is not subscriptable` |
| valuation | A | 38 | 同上 |
| profile | A | 36 | 东财 F10 `概况缺少 jbzl（接口可能改版）` |
| flow | A | 37 | 同类（其中 7 条也是那个 `TypeError`） |

`TypeError` 这一类是本轮顺手挖出来的**假故障**：崩点在 akshare 库里
（`stock_value_em.py:37` 拿 `data_json["result"]["data"]`，而东财对某些代码回的 `result` 是
`null`），不是我们的解析器写错。实测同一接口 `600519 / 000001 / 300750` 各回 2144/2144/2039 行
都正常，`000996`、`999999` 回 `null`。也就是说**源清楚地说了一句「这张表里没有它」**，
而链把这句话读成「源挂了」：既不给下一个源换过去的正确理由，也发不出 D36 的覆盖确认。

---

## 2. 候选备源实测（11-05 14:50–15:20，本机，每个接口打一次）

只列**实测过**的。行数与耗时是那一拍的数，用来决定它排在链上第几位。

| 接口 | 上游 | 市场 | 能填的能力 | 实测 |
|---|---|---|---|---|
| `stock_zh_valuation_baidu` | 百度股市通 | A | 按日估值（按指标一问） | 365 行 / 4.5s |
| `stock_hk_valuation_baidu` | 百度股市通 | HK | 按日估值 | 365 行 / 2.0s |
| `stock_financial_analysis_indicator` | 新浪 | A | 基本面（按报告期） | 11 行 / 1.0s |
| `stock_financial_abstract_new_ths` | 同花顺 | A | 基本面（长表 metric/value） | 1200 行 / 0.6s |
| `stock_profile_cninfo` | 巨潮 | A | 公司概况 + **上市日期** | 1 行 / 0.7s（列里有 `上市日期`） |
| `stock_individual_notice_report` | 巨潮 | A | 公告 | 1079 行 / 34.4s |
| `stock_hk_daily` | 新浪 | HK | 日线 | 1997 行 / 1.0s |
| `stock_us_daily` | 新浪 | US | 日线 | 10068 行 / 2.3s |
| `index_us_stock_sina` | 新浪 | US | 指数日线 | 5747 行 / 2.0s |
| `stock_hk_hist_min_em` | 东财 | HK | 分钟 K | 1507 行 / 0.3s |
| `stock_us_hist_min_em` | 东财 | US | 分钟 K | 1895 行 / 0.4s |
| `stock_hk_index_daily_em` | 东财 | HK | 指数日线 | 9001 行 / 10.7s |
| `stock_hk_security_profile_em` | 东财 | HK | **上市日期**（列里有 `上市日期`/`发行价`） | 1 行 / 1.2s |
| `stock_hk_financial_indicator_em` | 东财 | HK | 基本面 | 1 行 / 1.2s |
| `stock_financial_us_report_em` | 东财 | US | 基本面 | 683 行 / 4.4s |
| `stock_hk_spot_em` | 东财 | HK | 全市场快照 | 4743 行 / 60s |
| `stock_us_spot_em` | 东财 | US | 全市场快照 | 13895 行 / 167s |

试过但不接（写下来是为了别让下一个人再试一遍）：

* `stock_us_valuation_baidu`：`AAPL / 105.AAPL / 106.AAPL` 三种形态全回非 JSON
  （`JSONDecodeError: line 1 column 1`）。**美股估值没有第二个域可接**，见 §4。
* `stock_individual_basic_info_us_xq`（雪球）：要 `xq_a_token`，匿名被拒。
* `stock_hk_fhpx_detail_ths`（同花顺 HK 分红）：`ValueError: No tables found`——
  和 #383 是同一类列名漂移，先不进链。
* `stock_fund_flow_individual`（同花顺即时资金流）：5217 行 / 14.6s 能通，但它是**即时**一张表、
  没有按日历史，而我们的 `funding_flow` 是按日按票（D60）。语义对不上，不拿即时数冒充按日。

顺带订正一处旧结论：`spot.py` 的模块注释写着「东财三个 `*_spot_em` 在本机全不通
（实测 RemoteDisconnected）」。本轮实测 HK/US 两个都通了（4743 行 / 60s、13895 行 / 167s），
只有 A 股那次在并发下回了 `RemoteDisconnected`。旧话不再成立，注释按实测改。

---

## 3. 设计

### D322 备源的判据先分域名，再分接口族，同域要在 `note` 里如实写

`akshare` 这个源名的实际上游是东财，`eastmoney_f10` 也是东财。同一个网站的两条路径会在
同一场故障里一起倒下（11-05 的东财 `result: null` 一次打掉了 A 的 valuation/financials/flow
三条链，就是同一域名的三个格同时坏）。所以补源按两档记账：

* **换域名的**（百度、新浪、巨潮、同花顺）算真备源；
* **只换接口族的**（东财 HK/US 那一批）算备源，但在 `data_sources.note` 里写「与 X 同源」，
  健康页与目录都念得到这句话。本期没有任何一格靠同源凑数假装成两个域。

### D323 只把实测过的接口排进链（延续 D319）

上表每一行都是这一台机器上跑过的数。没测过的、测失败的、语义对不上的，一律不进链、
不写 `QUOTE_DELAY_S` 那类「大概是这个数」的配置。

### D324 本期接的五个改动：2 个新 kind + 3 处扩能

| kind | 新建/扩展 | 市场 | 提供的能力 | 排进链的位置 |
|---|---|---|---|---|
| `baidu_valuation` | 新建 `datasource/baidu_valuation.py` | A、HK | `valuation` | 估值链第 2 位（A 在 akshare 后、HK 在 yfinance 后） |
| `sina_global` | 新建 `datasource/sina_global.py` | HK、US | `daily_bars`、`index_daily`(US) | HK/US 日线第 3 位；US 指数日线第 2 位 |
| `eastmoney_global` | 新建 `datasource/eastmoney_global.py` | HK、US | `minute_bars`、`index_daily`(HK)、`listed_date`(HK)、`financials`、`market_snapshot` | 各自那条链的第 2 位（快照因 60/167s 排最后） |
| `akshare_cninfo` | 扩能（已存在，只加方法） | A | `company_profile`、`announcements`、`listed_date` | 概况/公告链第 2 位；上市日期链第 2 位 |
| `sina` | 扩能（已存在，A 股那一路） | A | `financials` | 基本面链第 2 位 |

`eastmoney_us_f10` 已在 US 基本面链第 1 位（它是东财 F10 族），本期给它补的是同域不同族的
`eastmoney_global`——US 基本面从「—」变成「2」，但这两格同源，按 D322 第 2 档记账。

### D325 慢源排最后，并把实测耗时写进 `note`

快照备源一次要 60 秒（HK）到 167 秒（US）。降级链只会在家主源失败时才走到它，
所以这个代价平时不付；一旦付起来，屏幕与 `task_runs` 里要能看出「这次是那条慢的路」。
`note` 里写实测数，不写「较慢」这种没法核对的话。

### D326 新能力不改服务层判定口径

US 基本面、HK 上市日期接上之后走的是已有的 job、已有的表、已有的「未接入」话术（D247）。
本轮不在 services 里加分支：链上有源就用，没有还照旧说「这个市场未接入」。

### D327 崩在库里的 `TypeError` 归到它该在的那一类

三个 akshare 系适配器（valuation / financials / flow）在库里包一层：库内部抛出的
`TypeError`（`data_json[...] ` 那一类）翻成 `EmptyResponse`——意思是「源答了一句说不清的『没有』」，
可以换下一个源，但**不能**替任何人发 D36 的覆盖确认。判据只认这一种形状：
异常类型是 `TypeError` 且消息含 `NoneType`，别的一律照原样抛出（改版就该是 `FieldDrift`，
不该被这条兜底吞掉）。

### D328 本期算完 #384 那一刀

补源全部走迁移 `0048_data_source_backups.sql` 种子。而 #384 记的是「新注册的源要重启两次才
上岗：链快照排在迁移之前」。本轮既然靠迁移种源，就必须让**一次启动**后新源出现在链上，
否则每次补源都要人工重启两遍，屏幕上还会先亮一轮「这个市场只剩一个源」。验收第 6 条盯这件事。

---

## 4. 本期不做（逐条给理由）

* **美股估值**。§2 实测：百度的三种代码形态全失败、雪球要 token。US 估值仍是 yfinance 孤源，
  而它正在 429。真正的解法要么找到新域名（先立 #387b 去测 `stock_us_*` 之外的美股估值通道），
  要么改成用日线 + 基本面 + 报价**派生** PE/PB（那是口径决定，要单独一份 spec）。
  本轮不拿没测过的东西填这一格。
* **HK/US 分时（`minute_series`）**。东财那两个接口实测给的是分钟 K（列名里没有均价），
  拿它拼成分时就是造一个源没给过的数。分时继续靠腾讯，HK/US 缺分时按 D165 的置灰话说。
* **HK/US 资金流**。同花顺只有即时表（D60 要按日），东财 HK/US 个股资金流本轮没测出可用接口。
* **根治 yfinance 429**（请求预算、按票轮换、把估值刷新降频）。本轮做的是让估值有第二家，
  429 的账单本身留给 #365/#387b。
* **代码表（`symbol_list`）补第二家**。它是每天一次的低频表，孤源但不在坏的那批里；
  东财 HK/US 快照顺带能提供代码表，要不要接由它自己的失败记录决定，不本期硬塞。
* **个股新闻补源**。HK/US 新闻本来就是「未接入」话术（D247），A 股那条链上唯一源
  `eastmoney_f10` 的 news 接口 `task_runs` 里全 ok（11-04/11-05 三条 job 无一失败），
  没坏的不修。

---

## 5. 验收

1. **矩阵复测**：跑 `tmp/_chain_matrix.py`（本轮改成断言脚本），D324 表里那五处改动之后，
   `valuation A`、`valuation HK`、`financials A`、`company_profile A`、`announcements A`、
   `listed_date A`、`index_daily HK/US`、`minute_bars HK/US`、`market_snapshot HK/US`、
   `daily_bars HK/US` 每格 ≥ 2 个源；US `financials` 从 0 变成 2 且 `note` 里带「同源」。
2. **单测各条**（红→绿，一次一个行为）：
   * 百度估值：夹具解析出 `valuation_daily` 标准列（`pe_ttm`/`pb` 来自两个 indicator 请求），
     源回的 `value` 为空 ⇒ `EmptyResponse`，不是 0 行确认。
   * 新浪 HK/US 日线：日期升序、`amount` 缺列时留 `null` 而不是 0（新浪那一路确实没有成交额，
     实测列名 `date,open,high,low,close,volume[,amount]`）。
   * 东财 HK/US 分钟 K：`scale=5`、时间戳按市场时区，列名漂移 ⇒ `FieldDrift`。
   * 巨潮概况：`上市日期` 进 `listed_date`，缺这个键 ⇒ 返回 `None`（一句答复）而不是抛。
   * 巨潮公告：1079 行形状的标准列 + `公告日期` 过滤窗口。
   * `TypeError` 归类：假的「库内崩」异常 ⇒ `EmptyResponse`；同一段代码里换 `KeyError`
     ⇒ 照原样抛（钉住 D327 只认那一种形状）。
3. **链上行为**：把 A 估值链的第 1 位弄坏（测试替身），`registry.fetch_valuation` 要走到百度并
   在 `attempts` 里留下两条；两家的 `source` 列各写自己，不能都写 `akshare`。
4. **实机**：跑一次 `sams sync valuation --market A` 的当日增量，`valuation_daily` 里
   11-05 那批 `source` 出现 `baidu_valuation`（谁顶上来的记录）；`/health` 页念得到新增的 5 个源。
5. 全量回归 + `ruff check src tests` 干净。
6. **#384**：只重启一次 serve，`/admin` 数据源目录里能看到并启用新源，且链快照里已有它们。

---

## 6. 实施顺序（每步一个提交）

1. D327 的 `TypeError` 归类（最小、先做：它把三条 A 链的失败从「挂了」改成「没有」，
   后面所有补源的实测都靠这个区分才看得清）。
2. `akshare_cninfo` 扩能（概况 + 公告 + 上市日期，三刀各自红→绿）。
3. `sina` 扩 `financials`。
4. `baidu_valuation` 新 kind + 迁移 0048 的路由种子。
5. `sina_global` 新 kind（HK/US 日线、US 指数日线）。
6. `eastmoney_global` 新 kind（分钟 K、HK 指数日线、HK 上市日期、HK/US 基本面、快照）。
7. D328/#384：一次启动上岗。
8. 矩阵断言脚本 + 全量回归 + 实机同步。
