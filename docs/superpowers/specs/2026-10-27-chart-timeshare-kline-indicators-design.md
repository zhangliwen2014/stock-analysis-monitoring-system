# SP2 图表：分时 / 多周期 K 线 / 多指标 + 自选行内移除

**Goal:** 把个股页那一块图换成主流行情页的样子：分时图与蜡烛 K 线两种视图、
周期从分钟到年线可换、主图与副图各自能同时挂多个技术指标，并把自选项的「移除」
收回它该在的地方（那一行的最右侧）。

**Architecture:** 分析层已经有 12 组指标与 day/week/month 重采样（`sams/analysis/indicators.py`、
`resample.py`），缺的全在 web 那一层：`bars_api` 只把 `close/ma20/ma60` 三列交出去，
前端只画一条折线。所以本期分三条腿：①分析层的周期再扩（季/年/N 日）②web 层把已有的
东西接出来并加选择器 ③新增一条「当日分钟」取数能力（库里没有分钟数据，只能现取）。

**Tech Stack:** FastAPI + Jinja + htmx + 本地 vendored ECharts（全量包，`candlestick` 已在里面）、
Polars（重采样与指标）、腾讯/新浪行情 HTTP 源。

---

## 1. 用户的三条原话（需求现场）

1. 「股票曲线可以日线/周线/月线/...等？ 比如平安银行」
2. 「自选股的图表，应可以看股票的标准 K 线」
3. 「自选股的图表，应该有多种数据选择，分时图、k 线图（5 分钟、15 分钟、30 分钟、。。。、
   xx 小时、5 日、10 日等，而且可以自己设置），支持多种技术指标（均线、macd、kdj 等，
   还可以同时显示多个技术指标）。你查一下通常的股票是如何显示的」

外加同一天的一条页面意见：「自选的移除，可以放在自选股票的最右侧，没有必要单独一个列表」。

## 2. 通常的股票行情页是怎么显示的（调研结论）

主流软件（东方财富 / 同花顺 / 通达信 / TradingView）在个股页上是同一套约定，不是各家的发明：

**视图只有两个基本档：分时 与 多日 K。**
- **分时图**：横轴是当日交易时段（A 股 09:30–11:30、13:00–15:00，午休不留空档），
  左轴价格、右轴涨跌幅（以昨收为 0 的中线），**白线**是每分钟最新价，**黄线**是当日均价
  （累计成交额 ÷ 累计成交量），昨收画一条水平基准线，下方一组每分钟成交量柱（红涨绿跌）。
  收盘后显示的是「最近一个交易日」那一条，并写明日期。
- **五日**是分时旁边一个独立档（把 5 个交易日的时间轴接起来）；本期不做（见 D167 的取舍）。

**K 线的标准构成**：蜡烛（开高低收，A 股习惯**红涨绿跌**）+ 主图均线（MA5/10/20/60 默认，
可换成 BOLL/EXPMA，也可关）+ 一根根周期标签 + 可拖动缩放 + **副图**（量能窗）单独一格，
默认 VOL，可换 MACD / KDJ / RSI / ATR / WR / CCI / BIAS / OBV / DMI，桌面软件能同时开多个副图。
周期条上给的是：分时、1/5/15/30/60 分钟、日、周、月、（季、年）、以及**多日线**（5 日、10 日、…）
与**自定义周期**（东方财富的「分时 K 线设置 / 更多周期」就是把这一条列给用户自己排序与取舍，
见 [自定义 K 线周期](https://caifuhao.eastmoney.com/news/20240517164443864423490)、
[多周期同列](https://qhweb.eastmoney.com/help/2255544.html)）。
**复权**是 K 线页上的第三个开关：不复权 / 前复权 / 后复权，散户默认前复权，
量化与研究口径默认后复权。

**指标口径的两条硬约定**（我们照着走，否则数字与行情软件对不上）：
- 均线/副图指标算在**同一复权口径**的价格上；量的指标（VOL/OBV）吃**裸量**不复权
  ——这条我们的 `indicators.py` 已经按通达信口径写死了（模块文档第 3 条）。
- MACD 的柱是 `(DIF − DEA) × 2`，KDJ 的种子是 50、`J = 3K − 2D`，WR 是 **0 顶 100 底**
  的国内口径——这三条也已经在 `indicators.py` 里。

## 3. 代码与来源的事实（先量过再设计）

**已经有的**（这是本期最大的好消息：不是从零做指标库）：

| 能力 | 位置 | 状态 |
| --- | --- | --- |
| 12 组指标：`ma/ema/macd/rsi/boll/kdj/atr/dmi/cci/wr/obv/volume_ma` | `sams/analysis/indicators.py:331` `INDICATORS` | 已实现且有单测；`metrics(series, names)` 可一次算多组并横向并表 |
| MA 窗口可选：`ma(series, *windows)` | 同上 `:110` | 「均线自己设」在分析层已经成立，只缺一个存偏好的地方 |
| day/week/month 重采样，含 `open/high/low/close` 与 `*_hfq`、`first/last/bars/pending` | `sams/analysis/resample.py:83` | 周线月线**不需要新数据**，日线重采样即得；口径注释与 `pending_periods` 都在 |
| 过闸的后复权序列（OHLC 四档都乘因子） | `sams/analysis/adjust.py:137` `PRICE_COLUMNS=("open","high","low","close")` | 蜡烛图要的 `open_hfq/high_hfq/low_hfq` 已经算好了 |
| `AnalysisService.series(..., period=...)` | `sams/services/analysis.py:177` | CLI 已经在用三档周期（`sams/cli/analysis.py:304`） |

**缺的**（也就是本期要补的）：

1. `series()` 出口那张 `table` 用白名单 `keep` 只留了 `close/close_hfq/volume/pending/first/last/bars`
   （`analysis.py:261`），OHLC 与 `_hfq` 列被滤掉了——**但 `SeriesRead.series.frame` 里全都在**，
   web 层可以直接从那条序列取，不必动这个白名单。
2. `bars_api`（`sams/web/pages/symbol.py:162`）只吐 `dates/close_hfq/ma20/ma60`：周期、
   指标名、OHLC 三样都没接。
3. 前端 `app.js` 只画三条 `type:"line"`；没有蜡烛、没有副图、没有周期与指标选择器。
4. **分钟数据一条都没有**：库里只有日线（`bars_daily`），报价板只有「此刻」。分时图与分钟 K
   必须现取。实测覆盖（2026-10-27 本机跑通）：

| 要的东西 | 来源 | 实测结果 |
| --- | --- | --- |
| 分时：当日每分钟 `HHMM 价 累计量 累计额` + `qt` 里的昨收 | 腾讯 `web.ifzq.gtimg.cn/appstock/app/minute/query?code=sz000001` | A ✓（`date=20261027`）；HK ✓（`hk00700` 有全天分钟）；**US 只回 1 个点**（延迟行情，画不出一条线） |
| 分钟 K：`m1/m5/m15/m30/m60`，含 `prec`（昨收） | 腾讯 `ifzq.gtimg.cn/appstock/app/kline/mkline?param=sz000001,m5,,320` | A 个股 ✓、A 指数 ✓（`sh000001`/`sz399001`）；**HK/US 一律 `code:-1 param error`** |
| 分钟 K 备源：`scale=1/5/15/30/60`，返回 OHLCV+amount | 新浪 `quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData` | A ✓；HK/US 返回 `null` |
| 分钟 K（东财） | `push2his.eastmoney.com/.../kline/get?klt=5` | 本机空响应（与代码表那次同源问题），本期不指 |

结论：**分钟粒度只有 A 股**（两家互为备源），分时线 A/HK 可用、US 只有单点，日线及以上粒度
三个市场都有（已入库）。屏幕上不许把「这一档答不了」画成「这只票没数据」。

## 4. 决策

- **D157 口令下限放到 6 位**（用户口径：「6 位以上就可（含 6 位）」）。闸只有
  `users.MIN_PASSWORD_LEN` 一处，改数不改结构；bcrypt 的 72 字节上限那道闸原样留着。
  已实现（提交 `4732396`）。
- **D158 一块图、两种视图**：`分时 | K线`。切换走 htmx 拉一个 chart 局部
  （`GET /api/partials/chart`），由服务端把「当前选中哪一档」渲染回选择器——CSP 是 `'self'`，
  不写内联 JS，也不在前端留第二份状态。选中态换了要看得见（当前档高亮），否则用户不知道自己看的是哪档。
- **D159 K 线画标准蜡烛**：`type:"candlestick"`，**红涨绿跌**（A 股习惯，与站内涨跌配色一致），
  四档价格一律取后复权列（`open_hfq/high_hfq/low_hfq/close_hfq`）——与均线和副图指标同基，
  这是既有那条「图上那根收盘价必须与均线同基」规矩的延长线。图例写明「后复权」。
- **D160 周期分两条腿给**：
  - **日线及以上**（`day/week/month/quarter/year/5d/10d`）全部由库里的日线重采样得到，
    **不新增数据源**。其中 `quarter/year/5d/10d` 是 `resample.py` 的扩项（D161）。
  - **分钟粒度**（`m1/m5/m15/m30/m60`）现取（D163）。
  - 每档给一个默认窗口（单位都是**日历日**，与那条 `days ≤ 2000` 的上限同一种量纲）：
    日 180、周 730、月 1825、季/年 2000（约五年半——就顶在那条既有上限上，不为一档图
    把闸放宽）、5 日/10 日 2000、分钟档由来源能给的根数说了算（不套日历日）。
- **D161 重采样扩到 `quarter/year/nday`**：分组函数从 `(week, month)` 变成
  `(week, month, quarter, year, nday)` 五种，`nday` 是**按交易日数分组**（每 N 根日线一根 K，
  即行情软件的「5 日线 / 10 日线」）。季/年的收市日标签是**该季/该年的日历末日**
  （3-31 / 6-30 / 9-30 / 12-31），沿用周月那条口径 1：日历收市日在后、实际最后一根另存
  `last`，提前收市看得出来的那一写法。
  **`nday` 是这一条里的例外**：它没有「日历收市日」可言，标签就是**组内最后一个交易日**；
  分组从**送进重采样那条序列的第一根**起往后数，每 n 根一组，`pending` 的判据是
  「**这一组不足 n 根**」——不足就是没走完，这根 5 日线只盖了三天，与周线那条口径 2 同构。
  边界随读取窗口首根移动（换个窗口，组会整体挪一位）这件事写进 `resample` 的 docstring：
  这一档本来就没有日历锚可依附，说清楚比假装它稳定好。
  `PERIODS` 与 CLI 的 `choices` 同处改，不认识的名字照旧报错而不是安静按日线算。
- **D162 指标同时挂多个**：
  - **主图**（叠加在蜡烛上，共用价格轴）：`MA`（默认 5/10/20/60，窗口可自己设，见 D166）、
    `BOLL`、`EMA`，三者可同时开，也可全关（只剩蜡烛）。
  - **副图**（各自独立一格、各自一条纵轴，横轴与主图对齐）：`VOL`（默认）、`MACD`、`KDJ`、
    `RSI`、`ATR`、`WR`、`DMI`、`CCI`、`OBV`，最多同时 **3 个**（再多就不是看图而是刷屏）。
  - 接口只多收两组名字：`main=ma,boll` 与 `sub=macd,kdj,vol`；名字来自 `INDICATORS` 那份注册表，
    拼错回 **400 + 那句「可选的是 …」**（分析层本来就报这句，web 层要转成 400 而不是 500）。
  - 副图那一格的数值口径由分析层保证（量吃裸量、比值类算在 `close_hfq` 上），前端不再算第二遍。
- **D163 当日分钟两条能力，都不入库**：新增
  `minute_series`（分时线：每分钟价 + 累计量额 + 昨收，腾讯）与
  `minute_bars`（分钟 K：OHLCV，腾讯主、新浪备），都登记进 `capabilities.CAPABILITIES` 一处，
  于是降级链、健康页、源健康三处自动认得（D38 那条老规矩）。
  **不建 `bars_minute` 表**：来源只答最近那一段，回填不了历史，存下来就是一份「看起来是全的」
  假数据；进程内 60 秒缓存足够一个人连着点周期条。
- **D164 分钟序列不过复权闸，并把为什么写在注释里**：`adjusted_series` 那道闸（D36/D37）管的是
  「跨除权日的序列漏乘因子」。当日分钟只有一个交易日，除权当天的开盘价交易所已经调过了，
  因子在同一天内是常数——所以分钟帧直接按 `adj_factor=1` 组装成 `AdjustedSeries`，
  指标层照吃。这条不是绕过闸，是那条闸的前提在这一档根本不成立。
- **D165 来源答不了就直说，不画空的**：HK/US 没有分钟 K（腾讯 `param error`、新浪 `null`）时，
  周期条上那一档**置灰但留在原位**（不是消失：消失会让人以为软件坏了），点开给一句
  「腾讯与新浪都不答港股的 5 分钟线」；US 的分时只有单点，直接不给分时档。
  这句「谁能答、谁不能」由 `capability_labels` 那条反查回答，不写死在前端。
- **D166 自选行的移除上板**：`partials/quotes.html` 收 `manage` 标志，`/watchlist` 用
  `hx-get="/api/partials/quotes?manage=1"`（看板不带这个参数，看板不是来删票的地方），
  末列放一个移除表单；**备注并进「名称」列**（muted 后缀），`watchlist.html` 里那张重复的
  管理表整块删掉。加票表单与「备注」输入留着。移除仍走 `/watchlist/remove`（CSRF + 303 回本页）。
  这一条同时解决 D145 那屏留下的「同一张票在页面上列两遍」。
- **D167 本期不做的**（写清楚免得下次当成漏项）：五日分时（多日时间轴拼接）、
  不复权/前复权切换（现在整条图注都写「后复权」，等 D168 那条口径讨论完再开）、
  多周期同列（一块屏并排两个周期）、画线工具、盘口五档与逐笔。
- **D168 图的口径要自己说自己是哪一份**：图注一行交代
  「后复权 · 锚在 `SeriesRead.anchored_on` · 预热被覆盖确认截在 `warmup_floor`（若有）」——
  这两个字段分析层已经给了，屏幕上第一次要用它。跨那次除权之前的因子不在窗口里，
  这条曲线的绝对价位只在锚以下可比（`SeriesRead` 的 docstring 早就写了这句）。
- **D169 自选的「移除」是折起，不是删除**（用户口径：「移除是否为标记一下，因为如果有人在
  自选股上写了一下内容，移除是否就看不到数据了？」）。查过之后的事实先摆出来：
  `WatchlistRepo.remove()` 是硬 `DELETE`，丢掉的是那一格**备注**；更要紧的是
  `services/scheduler/universe.py:73` 每天要采哪些票读的是「自选 + 持仓 + 启用规则 + 论点」
  四个来源，一只只挂在自选上的票被删之后**不再进采集清单**，K 线从此停在删除那天。
  所以这一刀砍掉的东西比屏幕上看见的多。改成：
  - 迁移 `0022_watchlist_hidden.sql` 给 `watchlists` 加一列 `removed_at TIMESTAMPTZ`
    （**只写不读回 Python**：那条「别把 TIMESTAMPTZ 取回 Python」的旧账还在）。
  - `remove()` ⇒ `UPDATE ... SET removed_at = current_timestamp`；新增 `restore()`
    （置回 NULL）、`hidden(user_id)`（只给折起的几行）、`purge(user_id, market, code)`
    （真正的 DELETE，只有确认屏那一步会走到它）。返回值的含义不变：「这一行原来是可见的」。
  - **`list()` 与 `codes()` 过滤 `removed_at IS NULL`**：行情板、自选页的表、
    以及「按自选展开规则标的」都不再包含它——折起就是「这只票我暂时不盯」。
  - **`list_all()` 不过滤**：采集清单继续覆盖它，日线与报价照常更新，
    恢复时不需要回填，历史也不会停在某天。这是这一条与旧行为最实质的差别。
  - `add()` 走既有的 `INSERT OR REPLACE`，重新加一只折起的票不产生第二行，并写上新备注。
    **实现时订正**：DuckDB 的 `INSERT OR REPLACE` 保留语句里没提到的列的旧值（实测），
    所以那一列必须显式写 `removed_at = NULL`——不写的话「把折起的票重新加回自选」会静默失败：
    屏幕上说加好了，看板里没有那一行。
  - 屏幕上 `/watchlist` 页底一栏「已移除（N）」：代码 / 那格备注 / 「恢复」/「彻底删除」。
    「彻底删除」必须先落到一屏确认再执行 `purge`——那一刀才是真删，值得多点一下。
    **实现时订正**：这一屏只摆「删的是哪只、你给它写过什么、删掉之后没了什么」三件事，
    不列批注与规则的条数——`purge` 只动 `watchlists` 那一张表，批注与论点一条都不删，
    把它们的条数写在删除确认屏上会让人以为那些也一起没了，那是反向的误导。
    看板那一屏不放这一栏（看板不是来删票的地方）。
  - 写在别处的内容本来就不跟着自选走：批注按 `target_type/target_ref`（`A:600519`）存，
    论点、规则、告警、日线都按标的存，与 `watchlists` 没有外键关系，折起之后从
    `/symbol/A/600519` 进去全部照旧看得见。
  - 已知的一处行为：`services/review.py:100` 复盘时「响过的票自动加进自选」会把折起的票
    放回看板。不改它——那条路径的意图正是「这只票刚出过事，值得盯」，
    不想看它再折一次即可，不为此加参数。
  - D166 那一栏的移除表单端点名字不变（`/watchlist/remove`），语义按这一条换成折起。


## 5. 接口

### 5.1 数据层

- `resample(series, period, as_of=...)`：`period` 取值扩为 `week/month/quarter/year/nday`，
  `nday` 带 `n`（`5d`/`10d`）。
- `services.analysis.PERIODS` 跟着这份名单走；`series(..., period="quarter")` 与
  `period="5d"` 可用，其余语义不变（预热按日线根数计、`pending_periods` 照旧）。
- `capabilities.CAPABILITIES` 加 `minute_series`、`minute_bars` 两行（label：分时、分钟 K）。
- 新模块 `sams/datasource/minute.py`：`has_minute_series` / `has_minute_bars` 两个探针，
  腾讯实现 `minute_series` + `minute_bars`，新浪实现 `minute_bars`；
  返回 `MinuteRead(times_or_datetimes, open/high/low/close, volume, amount, prev_close, source, as_of_date)`。

### 5.2 HTTP

| 方法 | 路径 | 参数 | 出参 |
| --- | --- | --- | --- |
| GET | `/api/symbol/{m}/{c}/bars` | `days`、`period`、`main`（逗号分隔指标名）、`sub`（同上，≤3）、`ma`（窗口串 `5,10,20,60`） | `dates`、`open/high/low/close`（后复权）、`main:{name:[cols]}`、`sub:{name:[cols]}`、`pending`、`anchored_on`、`warmup_floor`、`note` |
| GET | `/api/symbol/{m}/{c}/minute` | 无 | `date`、`times`、`price`、`avg`、`volume`、`prev_close`、`note` |
| GET | `/api/partials/chart` | `market`、`code`、`view`(`time`/`kline`)、`period`、`main`、`sub`、`ma` | 选择器 + `<div data-chart-url>` 的整块局部 |

`bars_api` 从 `read.series.frame` 取 OHLC（D3 事实 1），不放宽 `analysis.py` 的 `keep` 白名单
——那张表是给命令行屏幕的，宽了会把 20 列推给终端；图的出口是序列本身。

### 5.3 前端

- `app.js` 的 `samsChart(el)` 按 `el.dataset.chartKind` 分两条渲染路径：
  `kline`（蜡烛 + N 个 grid）与 `time`（分时双线 + 量柱 + 昨收基准线）。
- 一个 ECharts 实例吃 `grid` 数组：`[主图, 副图1, 副图2, 副图3]`（少一个就少一格），
  `xAxis` 全部 `grid_index` 对齐、`axisLabel` 只在最下面那格显示；`dataZoom` 同时控制所有轴
  （拖动缩放是这条页的基本动作）。
- 周期条与指标选择器是 `<a hx-get>` 的普通链接（当前档加 `aria-current="true"` 与高亮样式），
  换档 = htmx 换整块 → `htmx:afterSwap` 触发 `boot()` 重画，沿用 D146 那套既有机制。

## 6. 测试钉（用例先红后绿，一条行为一枚）

1. `bars?period=week` 的根数 ≈ 日线根数 / 5，且最后一根若在 `pending` 列表里必须在
   `payload["pending"]` 出现——这根周 K 只走了三天，是它唯一能自证的地方。
2. `period=5d` 与 `period=10d`：从**首根起往后数**每 n 根一组（钉每一根的 `bars` 都是 n，
   只有最后一组可能短），标签是组内最后一个交易日；最后一组不足 n 根时必须在 `pending`
   里出现（D161：这根 5 日线只走了三天，得自己说得出走了哪三天）。
3. `period=quarter`/`year`：收市日标签是**该季/该年的日历末日**（3-31、12-31），
   库里那一组实际的最后一根落在 `last` 列——与周线那条「周五休市仍叫那个周五」同一口径。
4. `bars` 里 `open_hfq/high_hfq/low_hfq/close_hfq` 四列齐且 `high ≥ max(open,close)`、
   `low ≤ min(open,close)`（复权后仍成立，否则蜡烛画反）。
5. `main=ma,boll&sub=macd,kdj,vol` 一次回三个指标族的列，且 `sub` 超过 3 个回 400。
6. 指标名拼错回 **400**（不是 500）并带那句「可选的是 …」；`period` 不认识同样 400。
7. `minute` 接口：均价 = 累计额 ÷（累计量 × 100）（手→股），昨收取自 `qt`；
   来源不可用时给 `note` 与空数组，不 500（与 `bars` 那条 `NotAdjustable` 同一姿势）。
8. HK/US 的分钟 K 在来源拒绝时：接口 200 + `note` 说清「两个源都不答」，
   周期条那一档仍在（置灰）——不许画成「这只票没数据」。
9. `chart` 局部里当前视图/当前周期/当前指标带 `aria-current`，换档后旧档不再带——
   钉的是「看得出来现在看的是哪档」。
10. `/watchlist` 的票那一行末列有移除表单（`?manage=1` 的局部里也有，10 秒刷新后还得在）；
    看板那一屏（不带 `manage`）不含任何移除表单。
11. 自选页不再有第二张票列表：同一页上 `600519` 只出现在一行里（D166 的直接验收）。
12. 6 位口令能用、5 位仍挡（已随 D157 落：`test_six_chars_is_the_floor`）。

## 7. 分期与顺序

1. **计划 A（先做）**：K 线本体——蜡烛 + `day/week/month/quarter/year/5d/10d` 周期（含 D161
   的重采样扩项）+ 主图/副图多指标 + 选择器 + D166 自选行内移除 + D168 口径行。
   全部吃库里已有的日线，不引外部依赖。
2. **计划 B**：分钟粒度——`minute_series` / `minute_bars` 两条能力、腾讯/新浪适配器、
   `/minute` 接口、分时图画法、D164 的 `adj_factor=1` 组装、D165 的置灰话术。
3. **计划 C**：偏好持久化——每人的周期条排序与均线窗口（D166/D162 的「自己设置」落库），
   新表 `0022_chart_prefs.sql`，设置页一个卡片。
4. 已写好的**数据管理页**（D149–D156）与本期互不阻塞，排在计划 A 之后由用户点头再动。
