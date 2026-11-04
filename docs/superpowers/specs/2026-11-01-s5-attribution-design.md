# S5 业绩归因（设计）

**一句话**：回答「这段时间赚（亏）的钱是谁带来的」——按标的拆、按价格段与换汇段拆、按行业与市场聚合，
再和基准比出超额；算不出的部分明说算不出，不拿一个 0 顶上。

这一层承接用户的三条核心能力里的**买卖评估**：决策卡（S1-A）答「现在要不要动」，限额（S4-E）答
「现在超没超」，归因答「上次动得对不对」。复盘页今天只有告警处置统计，没有钱的事，所以这块是新地。

---

## 0. 现状事实（决定切片边界）

* `nav_daily` 只有账户级的 `cash / market_value / nav`，**没有单票**。要做归因，要么在外面重放一遍
  持仓与取价逻辑（复制第二份，迟早漂移），要么在重建那一趟里把每票的日价值一起留下。
* `NavService.rebuild` 已经逐日重放持仓（`src/sams/services/nav.py:118-147`），`_point` 已用
  `bisect_right` 取「≤ D 的最近收盘」并按 `STALE_TRADING_DAYS` 挡陈旧价，FX 走 `FxRepo.latest(day, …)`
  （`nav.py:230-260`）。归因需要的每票每日「本币价 × 汇率」在这一趟里**已经算过一遍**。
* 没有「按 D 日持仓」的公开 API：`SimOrderRepo.positions()` 读的是 `sim_positions` 活表；
  `PortfolioService.positions()` 无日期参数，取价硬钉在「库里最新的一根」（`portfolio.py:286`）；
  `realized_pnl()` 重放全部成交、无区间右界（`ledger.py:822`）。
* `IndexDailyRepo` 只有 `upsert / latest_with_change / latest_date`，**没有区间读**；指数适配器
  sina 只有 A、tencent 只有 A+HK，**US 指数取不到**。表 `index_daily` 已在（迁移 0021）。
* 线上 `config/settings.toml` 没有 `[indices]`，走 `DEFAULT_INDEX_CODES` 兜底（A 第一个是 `sh000001`）。
  实机核对（2026-11-01）：看板大盘条 A 有三只、HK 有三只，最新为 10-30 收盘，**US 一行都没有**；
  历史深度只在大盘条问「最新一日」时不需要，归因要用整段，验收时先看区间起点有没有行。
* TWR 只在「以最新完整日结尾的连续完整日」上链乘，窗口会**静默收窄**（D260）。归因必须自己把
  用的是哪几天写在卡上，不能直接引用 `NavSeries.twr` 当成请求的那个窗口。

## 1. 范围与切片

| 切片 | 内容 | 依赖 |
| --- | --- | --- |
| S5-A | 迁移 0045 `nav_holding_daily` + `rebuild` 同趟写入 + 幂等覆盖 + 回填 | S4-D |
| S5-B | `AttributionService`：窗口选择、每票贡献、价格段/换汇段、行业与市场聚合、残差 | S5-A |
| S5-C | 基准：`IndexDailyRepo.load()` 区间读、超额、缺日与无源话术 | S5-B |
| S5-D | 持仓页「业绩归因」卡（异步局部）+ `sams attribution` CLI | S5-B/C |
| S5-E | 全量回归 + 实机验收 + 进度记录 | 以上 |

## 2. 数据模型

### 2.1 迁移 0045：`nav_holding_daily`

```
account_id   VARCHAR  NOT NULL
day          DATE     NOT NULL
market       VARCHAR  NOT NULL
code         VARCHAR  NOT NULL
qty          DOUBLE   NOT NULL
price        DOUBLE   NOT NULL   -- 本币收盘（重建当天实际用的那一根，不是"最新"）
fx_rate      DOUBLE   NOT NULL   -- 本币 → 报告币种；本币即报告币种时为 1.0
value_report DOUBLE   NOT NULL   -- qty × price × fx_rate
PRIMARY KEY (account_id, day, market, code)
```

* **为什么落库而不是现算**：净值曲线和归因必须出自同一趟取价。归因自己再 `bisect` 一遍 bars，
  两边就会在「陈旧价怎么挡」「汇率取哪一天」上各自演化，最后出现「贡献加起来不等于净值涨幅」
  这种无法解释的表。
* **写谁**：这天还持有（`qty > 0`）的票，**或前一天持有、今天清仓**的那天也写一行
  （`qty = 0`，价与汇率照写）——否则卖出当天的价格效应没人认领，全部掉进残差。
  清仓行只延续一天。
* **不写谁**：`qty ≤ 0` 且前一天也没持有的；当天取不到有效价（陈旧/无收盘）的——那种日子
  净值行本身已经是 `complete=false`，归因只用完整日，不需要每行再带一个状态。
* **幂等**：`HoldingNavRepo.replace(account_id, start, end, rows)` 与 `nav_daily` 同段先删后写，
  重跑不累积。
* **回填**：`sams nav rebuild` 顺带写，`NAV_WINDOW_DAYS = 400` 内一次跑完即得历史。

### 2.2 读侧新增

* `IndexDailyRepo.load(market, index_code, start, end) -> list[tuple[date, float]]`（按日升序的收盘）。
* `NavRepo` 不动；新增 `HoldingNavRepo.series(account_id, start, end)`。

## 3. 口径（决定这一层会不会骗人）

### 3.1 窗口

区间 = 请求窗口 ∩ [账户开立日, 最新完整日]，并且**只取连续完整日**。首尾之间只要有一天
`complete=false`，窗口就缩到缺口之后的那一段，卡上写实际用的起止日与「已丢弃 N 天缺口」。
不足两个完整日 → 整卡「无法归因」并给原因，不显示一张全是 0 或全是「—」的表。

### 3.2 每票贡献（时间加权，能扛住区间内的买卖）

记 `u_{i,t} = price_{i,t} × fx_{i,t}`（该票每股的报告币种价值，**与持股数无关**），
`nav_{t-1}` 为前一日净值：

```
C_i = Σ_t  qty_{i,t-1} × (u_{i,t} − u_{i,t-1}) / nav_{i,t-1}
```

* 用「前一天持股数 × 当天每股价值变化」：当天买入或卖出只改权重，不改某只票的每股涨跌，
  所以不会因为买卖时点把归因算歪；
* 买入当天不计收益（`qty_{t-1} = 0`），卖出当天照计（清仓行的 `qty_{t-1} > 0`）；
* 各票贡献之和与「同一连续完整日区间的 TWR 链乘」相减即**残差**，残差必须印在卡上（§3.5）。

### 3.3 价格段与换汇段

同一区间里 `r_local = price_t/price_{t-1} − 1`、`r_fx = fx_t/fx_{t-1} − 1`，
两段各按 `qty_{i,t-1} × … / nav_{i,t-1}` 累加，两列相加与 `C_i` 相差一个二阶小量
（`(1+r_local)(1+r_fx) − 1 − r_local − r_fx`），差值并入残差并在口径说明里写清。
本币即报告币种时 `fx_rate = 1.0`，换汇段恒为 0——这跟持仓页现有的「价格段 / 换汇段」是同一句话，
但那里的换汇段锚在**买入日**汇率（`PositionPnl`），这里的锚在**区间起点**，两者数值不同，
卡上各说各的锚，不混用、不互相校正。

### 3.4 行业与市场聚合

按标的当天归属的行业求和（`IndustryService.industry_of`）。缺行业的票进「未分类」单独一行，
不摊进别的行业——与 S4-E 限额的未分类口径一致。市场聚合用 `market` 列本身。

### 3.5 残差与分红

`残差 = 区间 TWR − Σ C_i`，来源逐项点名：**当日新建仓的价格效应、手续费、外部存取款的入账时点、
以及没有归到票上的现金变动**。

分红**不进残差**：`sim_cash_moves` 的 `kind='dividend'` 行带 `ref = market:code:ex_date`
（S4-D 写入），据此把除权日到账的现金按当天归给那只票。这样「这只票这段时间给了我多少钱」
在含分红标的上不会低估（D267）。

### 3.6 基准与超额

* 基准 = `settings.indices.for_market(market)[0]`（`IndicesCfg`，D145 的大盘条同一份配置），
  不新增配置项；名字用 `settings.indices.label(code)`。A 用上证指数、HK 用恒指、
  US 用 `usINX`——**但 US 指数没有可用适配器**（sina 只 A，tencent 只 A+HK）。
* 基准区间收益 = 该指数在同一区间首尾收盘之比（首或尾落在非交易日时，取区间内最靠近的那两根，
  并把实际用的两个日期印出来）。
* 区间内指数缺日但首尾都在：给超额，同时写「基准缺 N 天，按现有收盘连乘」。
* 首或尾取不到、或该市场根本无源（US）、或区间内一根收盘都没落库：**超额 = `None`**，
  话术「{label} 在 {start} ~ {end} 没有收盘数据（该市场无可用数据源，或指数日线未同步），超额不给」。
  服务层不探测适配器能力（那要为了一句话去联网），把两种可能并列说出来比挑一个更诚实；
  不拿 0 冒充「跑平」。

## 4. 权限与失败

与 `LimitService` 同一条半径：`resolve_actor(users, username)` → `accounts.get(user.id, account_id)`，
别人的账户读不到。服务内部异常一律收敛成卡上的「归因未算：…」，页面不 500、不空表。

## 5. 页面（S5-D）

持仓页 NAV 卡之后新增「业绩归因」卡，异步局部加载（`hx-trigger="load"`，与 S2 的三张局部卡同法），
不拖慢首屏：

* 窗口切换：30 天 / 90 天 / 全部（默认 90 天，无完整日时退到全部并说明）；
* 表：标的（名称+代码）| 区间均权 | 贡献 | 价格段 | 换汇段 | 分红，按贡献绝对值降序；
  换汇段与分红为 0 时印「—」而不是「0.00%」——A 股账户的这两列永远是空的，
  把它们印成 0 会让「没有换汇影响」读起来像「换汇影响为零是结论」；
* 其下：行业聚合（最多 6 行 + 「未分类」）；
* 底部三行：区间收益（含实际起止日与丢弃的缺口天数）、基准与超额、残差说明；
* 无数据态各归其位：账户无持仓、净值未重建、基准无数、窗口不足——四种话术不同。
  其中「净值未重建」那句话里带真实账户 id 的 `sams nav rebuild --account <id>`，复制就能跑。

## 6. CLI

`sams attribution --account <id> [--window 90] [--json]`：打印与页面同一份报告（同一服务、同一话术），
用于实机核对；验收者只读页面，不用 CLI。

* `--window` 是往回看的**日历天数**（净值重放按日历天落点，周末靠沿用收盘价也能算出来），
  `0` = 从开户那天算起，与页面上那个「全部」按钮同一个口径；
* 归属从账户 id 反查主人（`sim_accounts.find` → `users.get`），命令行没有 `--user`：
  多一个参数就多一条「以别人身份读」的路；
* 退出码沿用 D77：0 = 这一段读完了（算不出来也算读完，原因写在 `skipped` 里），
  1 = 账户不存在，2 = 参数不对（`--window` 负数）。

## 7. 测试（红→绿，中文名）

1. `test_two_holdings_one_up_one_down_add_up_to_the_window_return` —— 贡献之和 ≈ TWR，残差 < 0.1pp
2. `test_a_position_bought_mid_window_does_not_claim_the_day_it_appeared`
3. `test_the_selling_day_still_earns_its_price_move` —— 清仓行在，贡献不为 0
4. `test_the_fx_move_is_shown_apart_from_the_price_move` —— HK 持仓两段相加≈贡献
5. `test_a_holding_without_industry_lands_in_unclassified_instead_of_leaking_into_others`
6. `test_a_gap_day_shrinks_the_window_and_says_so_instead_of_splicing_across_it`
7. `test_fewer_than_two_complete_days_refuses_the_whole_card` —— `rows=()` 且原因非空，不是一串 0
8. `test_the_dividend_paid_inside_the_window_is_attributed_to_its_stock`
9. `test_the_benchmark_excess_uses_the_first_and_last_close_in_the_window` /
    `test_a_market_without_an_index_source_says_no_benchmark_instead_of_zero_excess`
10. `test_another_users_account_is_not_readable_through_attribution`
11. 屏幕级：`tests/web/test_portfolio_attribution_web.py` 无净值/基准无数/正常三态的话术与「—」
12. CLI：`tests/cli/test_attribution_cli.py` —— `--json` 逐项等于 `evaluate()` 的返回（两种介质
    读的是同一份报告）、算不出来时给可复制的 `nav rebuild` 而不给表头、`--window 0` 的起点
    落在开户日、退出码 1（账户不存在）/2（`--window` 负数）

## 8. 实机验收（S5-E）

1. 停服 → `db migrate` 落 0045 → `sams nav rebuild --account <验收户>` 回填 400 天 → 起服；
2. 持仓页归因卡：验收户（HK 01021 + 03690）应给出**非零换汇段**（HKD 区间内跌过），
   与持仓卡「价格段 / 换汇段」方向一致、数值因锚点不同而不等；
3. 残差行存在且 |残差| 明显小于区间收益；
4. 基准：A 账户给出 `sh000001` 的首尾比与超额；US 账户给出「没有可取的数据源」；
   若 `index_daily` 为空 → 先跑 `sams run once --job indices --market A`，仍为空就照实记录
   「基准未落库」，并保证超额那行是「—」而不是 0；
5. 窗口切换 30/90/全部 的起止日随之前移，缺口话术随真实数据变化；
6. `sams attribution --json` 与页面数字逐项一致。

**实测（2026-11-02 起、2026-11-03 复核，验收账号 `sp1check`）**

全量回归：**5486 passed, 20 deselected, 0 failed**（`pytest -q -p no:randomly`），
`ruff check src tests` 通过。

1. `sams db migrate` 报「数据库已是最新，无待应用迁移」——0045 早已随 S5-A 落库；
   `sams nav rebuild --account <A 验收户> --days 400 --json` 回
   `days_written=2, complete_days=0, gap_days=2`：那一户 2026-11-01（周日）才开，
   11-02 的日线当时还没入库，所以两天都不完整。**这不是没跑命令**，见下面 D281。
2. 归因卡（文-中国银河，HK 01021 + 03690）给出**非零换汇段**：
   01021 价格段 -1.64% / 换汇段 -0.03%，03690 价格段 +0.52% / 换汇段 -0.02%。
   与持仓卡的「价格段 / 换汇段」方向一致（两户都两头为负），数值不等是因为锚点不同
   （持仓卡锚买入日、归因卡锚窗口起点），卡上就把这句话印着。
3. 残差行存在：`-0.00%`（JSON 里 `-8.7e-17`），区间收益 -1.17%，量级差十几个数量级。
4. 基准：该户给出 `上证指数 -1.44% · 超额 0.26%`，并附「基准实际用 2026-10-29 与
   2026-10-30 的收盘」。US 账户那一路在本验收账号下没有对应账户，由
   `tests/services/test_attribution.py::test_no_index_close_in_the_window_gives_no_excess_instead_of_a_zero`
   守着：那一趟取到的是「基准无数 → `benchmark.value is None`、`excess is None`、
   说明那行非空」，而 US 没有指数适配器落到的正是同一个形状（S5-C §3.6）。
5. 窗口切换按预期前移：30 天 → `2026-10-03 ~`，90 天 → `2026-08-04 ~`，全部 →
   `2026-10-29 ~`（= 开户日），三档的可用区间都落在同一句「2026-10-29 ~ 2026-11-01，
   共 4 天」上——卡上说的是实际能算的那几天，不是窗口名义的那几天。
6. CLI 与页面逐项一致：`sams attribution --account <HK 户> --window 90` printed
   30.35% / -1.67% / -1.64% / -0.03% 与 24.75% / 0.50% / 0.52% / -0.02%、行业两行、
   区间收益 -1.17%、基准 -1.44%、超额 0.26%、残差 -0.00%，与
   `/api/partials/portfolio/attribution` 一字不差；`--json` 同一批数（`weight`
   0.3035219438…、`window_return` -0.0117057800…、`excess` 0.0026475006…）。

实测这一趟暴露的那句话已经改掉，见 D281。还留下一处不好看的地方：`skipped` 那句话是服务层
拼的，命令行与网页共用同一串，所以网页上那对反引号会照原样印出来（净值卡用的是 `<code>`）。
要修就得让归因卡局部把消息里的反引号换成 `<code>`，纯渲染层的事，本单没做。

**事故（2026-11-03 12:32，净值 job 整库作废）**：`run_nav` 抛
`FATAL … database has been invalidated … Invalid Input Error: Failed to delete all rows from index.
Only deleted 0 out of 8 rows`，崩在 `nav_holding_daily` 的整段先删后写（HK 03690 / 01021，
2026-10-29 ~ 11-01 那 8 行）。根因在上游 DuckDB 2.0（duckdb#26106），不在我们的 SQL：
**11-02 验收序列里「停服 → migrate → `sams nav rebuild` → 起服」那一步留下的 WAL**——CLI 那一程
没 checkpoint 就结束，起服时重放 WAL 把 8 行放回表里，随后第一次**自动** checkpoint 把这些行的
索引项**从落盘的那份里丢掉**。行还在（扫描读得到），索引里没有：此后任何一趟删到它就 FATAL，
而 FATAL 会让整个库实例作废，进程里后面每一条 SQL 都失败——包括调度、盘中、HTTP。
诊断与修法见 D282。

## 9. 决策记录

* **D263**：归因只用连续完整日，窗口起止日写在卡上；不足两天整卡「无法归因」，不给 0。
* **D264**：每票贡献 = `Σ_t qty_{t-1} × (u_t − u_{t-1}) / nav_{t-1}`（前一日持股数 × 每股价值变化），
  买入当天不计、卖出当天照计；不用「首尾市值差」，后者把区间内的买卖当成价格涨跌。
* **D265**：残差显式印出并点名来源（新建仓、费用、外部流水时点、未归属现金变动）。
  「加起来等于总收益」是归因的诚实线，差多少就说多少，不静默配平。
* **D266**：每票日价值落在 `nav_holding_daily`，由 `NavService.rebuild` 同一趟写入。
  归因不自己取价，避免与净值各说各话。
* **D267**：区间内到账的分红按 `sim_cash_moves.ref`（`market:code:ex_date`）归给对应标的，
  不掉进残差。
* **D268**：基准取 `settings.indices.for_market(market)[0]`，US 无适配器就明说「该市场基准无数」；
  超额为 `None` 时不显示 0。价格段/换汇段的锚点与持仓卡不同（区间起点 vs 买入日），两套数值不互校。
* **D281**（实机验收暴露）：算不出净值的那两句话必须**先说盘后 job**，CLI 只是「等不了也可以跑」，
  并且带上**真的账户 id**。`run_nav` 每晚按账户重建最近 400 天，所以「先跑 `sams nav rebuild`」
  是一个假的操作指南——它把一个自动步骤说成手动步骤，还让网页用户去命令行找一个 id。
  改前：归因卡 `services/attribution.py` 只给命令、限额卡 `services/limits.py` 给的是
  `<账户>` 占位符，而同页的净值卡（`partials/portfolio_nav.html`）早就在说「盘后 job 每晚重建
  最近 400 天，也可以跑…」——三张卡对同一件事三种说法。改后三处同一口径，且三处的天数都取自
  `NAV_WINDOW_DAYS`（净值卡那句原先把 400 抄在模板里，现在由路由把常量交进去，
  `tests/web/test_portfolio_nav_web.py::test_the_nav_card_names_the_window_the_job_actually_uses`
  钉住：把常量打成 77，卡片就说 77）。
* **D282**（生产事故）：**重放 WAL 的那一程必须紧接着一次显式 `CHECKPOINT`**，这条路收在
  `Database.open`。上游 duckdb#26106 在 2.0.0 上实测的形状是：某程没 checkpoint 就结束 →
  下一程 open 时重放 WAL，行回到表里 → 那一程的第一次**自动** checkpoint（`wal_autocheckpoint`
  越过阈值那一下）把这些行的索引项丢掉，丢的是**落盘的那份** → 再下一程删到它们就
  `Failed to delete all rows from index`，FATAL，整个库实例作废。三个约束决定了修法长什么样：
  ①只认「重放」这个入口，所以 `open` 先查 `<file>.wal` 有没有非空内容再决定要不要 checkpoint，
  常态起服不为它多付一趟 IO；②DuckDB 在同进程里按路径共享实例，WAL 只由创建实例的**第一条**
  连接重放，所以 `Database` 要按路径计数（`_live`），计数从 0 变 1 的那一条负责 checkpoint、
  `close()` 负责减回去——全关干净再开是新实例，要重做；③FATAL 落在**提交**那一刻
  （`BEGIN … ROLLBACK` 探不出来，索引删是延迟的），所以探雷必须用 autocommit 的删。
  红灯测试是 `tests/test_adversarial_storage.py::test_should_still_delete_rows_that_came_back_from_the_wal`：
  四个进程（schema 落盘 → 写一行到 WAL 后 `os._exit` → 走 `Database.open` 重放并撑过自动
  checkpoint → 删那一行），因为**坏掉的索引只有下一程才看得见**；把 `open` 里的 checkpoint
  分支掐掉它就红。试过的便宜写法——重放那一程之后问文件「WAL 还在不在」——是**空的**：
  纯 DDL 的 WAL 本来就会被 DuckDB 在恢复时并进主文件，掐掉那一次 checkpoint 它照样消失，
  断言永远成立（这条负面结论也写在测试旁边）。
  另一半：**预防不治病**。修好代码后在生产的副本上照原样删那 8 行仍然 FATAL——索引已经写坏在
  文件里，之后的 `CHECKPOINT` 不会重建它。所以 11-03 的重建走 `EXPORT DATABASE`（FORMAT PARQUET，
  schema.sql 带 `CREATE TABLE` 的 PK 与 `duckdb_indexes()` 的二级索引）→ 新文件 `IMPORT DATABASE`
  → 逐表比行数（65 表全等）→ 在换入文件的字节副本上执行原那条区间删（过）→ 换入
  `data/sams.duckdb`，原文件留作 `data/sams.duckdb.corrupt-20261103.bak`。
  没做进代码：一条 `sams db repair` 命令。这个形状只在「用有 bug 的版本写过的旧文件」上出现一次，
  之后由 `open` 兜住，为它常驻一个命令是 YAGNI；真要第二次出现，上面的步骤就是操作手册。

## 10. 明确不做（S5 范围内）

* Brinson 的「配置效应 / 选股效应」分解——需要行业权重基准，且话术超出 v0 能核对的范围；
* 日内归因、按笔成交归因（「这笔买卖对不对」是买卖评估的另一切片，需要每笔的持仓期收益）；
* 胜率 / 盈亏比 / 期望值统计（同上，另片）；
* 给美股指数找新数据源；
* 把归因接进每日摘要（先让人自己在持仓页看，不制造每日一条长表）。
