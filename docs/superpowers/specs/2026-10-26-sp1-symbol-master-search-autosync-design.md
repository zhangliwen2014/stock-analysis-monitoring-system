# SP1 设计：代码表 + 搜索加票 + 自动补数 + 个股页可点 + 大盘条

日期：2026-10-26 ｜ 状态：已确认（用户逐节拍板；全拼列一项由控制器按顶流惯例代拍，见 D141）
上游：总 spec `2026-10-17-...-design.md` §14 M4 之后的增强线。
后续：**SP2** 个股档案页（分时、分钟线、日/周/月 K 切换、当日未定 K、估值面板 PE/PB/PE_TTM、F10 资料）；**SP3** 全市场每日快照 + `/screener` 筛选与横向分析、市场宽度。本期只做 SP1。

## 0. 问题（都是真人试用打出来的）

1. 主档按需建行：一只票只有日线同步成功才有名字/页面。加 600519 后当天盘后任务已成功记账，**它要等下一个交易日**才有数——试用者看到「休市什么都是空的、K 线全 404」。
2. 加票只认精确代码：想输 `payh`→平安银行，做不到；名字全靠记忆。
3. 持仓/自选的行内代码链接行为不一致（未同步票盲链 404，上轮缺陷 N4）。
4. 系统没有「大盘」概念：`bars_daily` 全是个股，指数一根都没有，休市连最近收盘都看不到。
5. 代码 000001 双重身份（上证指数 vs 平安银行）：指数必须与个股分开建模（D145）。

## 1. 决策

| # | 决策 | 理由 |
|---|---|---|
| D140 | 全市场代码表作为**基础数据**入库：新表 `symbol_master(market, code, name, …)`，每周盘后全量同步 **A+HK**；美股列表源弱（yfinance 限流史），留接口不做 | 代码表同时是搜索的库、名称的显示源、主档兜底的行来源——一块地基三处用 |
| D141 | 入库时用 **pypinyin** 预计算两列：`py_initials`（首字母，`平安银行→payh`）与 `py_full`（全拼 `pinganyinhang`，无声调、小写）。输入侧归一：剥市场前缀（`hk00700`/`sh600519`/`usAAPL`→定市场+大写代码）、大小写不敏感、剥空白 | 顶流（雪球/同花顺）都同时接受首字母与全拼；预计算让查询成为一条带索引的 LIKE，逐键现算 7 千行是死路。多音字取 pypinyin 默认读音，接受个别票要换写法才命中——搜索是发现手段，登记代码永远兜底（D143） |
| D142 | 候选排序钉死：**代码精确 > 首字母精确 > 全拼精确 > 前缀（首字母/全拼/代码）> 片段（代码/中文名）**，同级按代码升序；上限 10 条。每条候选显示 市场徽标/代码/名称/**昨收**（昨收只读本地 `bars_daily`，没同步过显示「—」，一次批量查询、零网络） | 可预期 > 聪明。热度/市值排序等 SP3 快照有数后再升级，接口留权重钩子 |
| D143 | 交互：自选页加票框改为搜了再加——htmx 防抖（300ms）拉 `/api/symbol-search?q=`；**点候选行=直接加入自选**（加票页里的点击意图明确，免二次确认）；查无提示「没找到与 X 匹配的 A股/港股：换个写法，或直接按代码登记」；「直接登记」对**不在代码表**的代码照收（新上市/表未更新），随后走自动补数 | 顶流惯例是搜索→个股页→关注两步；我们是列表加票场景，一步到位。直登入口保住「代码表不是存在的裁判」这一条：表是加速器不是门槛 |
| D144 | 自动补数：加自选、录成交、建规则、立论点**四个入口**统一调 `SymbolInterest.ensure(market, code, actor)`——它建主档行（代码表有名字就带名字）、查 `bars_daily` 无该股日线则写一行 `symbol_backfills(market, code, tries, last_error, created_at, done_at)`。调度器每拍（10s）消费**至多 3 条** pending：走现有 `sync.sync_daily`（顺带补主档、公司行为按既有链）；同票累计失败 3 次 ⇒ 置 `deferred`，个股页提示「今晚会随盘后任务再试」，次日盘后链成功即清账 | 「今天加票明天才有数」是试用者的头号困惑；每拍限 3 条防止一次导入打满源；失败停手与源冷却语义一致。写表而非直调调度器：layering 不破（服务层不 import scheduler，bootstrap 只给表两侧开门） |
| D145 | 指数独立表 `index_daily(market, index_code, date, open, high, low, close, prev_close, change_pct, source)`：键 `sh000001` 这类带市场前缀的指数代码，**不进 `bars_daily`**（000001 撞车）。清单配置驱动：`[indices]` 默认 9 个——A：上证指数 sh000001、深证成指 sz399001、创业板指 sz399006；HK：恒生 hkHSI、恒生科技 hkHSTECH、H股指数 hkHSCEI；US：标普500 usSPX、纳斯达克 usIXIC、道指 usDJI。数据源给 **腾讯/新浪** 加 `index_daily` 能力（method 走 `chain_for`/`_walk`，窗口型），盘后随市场每日链更新 | 顶流行情页第一屏就是指数；休市显示最近交易日收盘+涨跌幅，正是用户要的「大盘基本情况」 |
| D146 | 个股页可达性：`/symbol/{market}/{code}` 对**代码表内**任何票可进（进页时主档缺失即由代码表兜底建行）；无日线时不再 404，渲染空态「该股数据待同步」+「立即排一拍」按钮（= `SymbolInterest.ensure`）。自选与持仓的行内代码统一链到个股页（上轮 N4 就此了结）；代码表也查无的票保留 404 | 空态和 404 说的是两件不同的事：「有这只票、数还没来」vs「不认识这串代码」 |
| D147 | 代码表周同步语义：整批 upsert；本批缺失的旧行不删，置 `is_active=false`（退市/摘牌：搜索与候选不再出，但已挂自选/告警的行仍可渲染名称）。同步失败沿用旧表并在源健康记账——「代码表拿不到」绝不能把搜索变成错误页 | 代码表是显示层地基，宁可旧不可空 |
| D148 | 源层新增两个能力，全走 `capabilities.py` 单一清单：**`symbol_list`**（市场级、无代码无窗口，仿 `fx_rate` 不走 `_walk`，akshare 实现 A/港两张清单）与 **`index_daily`**（`_walk` 窗口型，tencent+sina 实现）。新迁移 `0021_symbols_and_indices.sql`：`symbol_master`、`index_daily`、`symbol_backfills` 三表 | 加一种数据只改一处是 capability 表存在的原理由（D38 一脉） |

## 2. 形状

### 2.1 数据模型（迁移 0021）

```sql
CREATE TABLE symbol_master (
  market TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
  py_initials TEXT, py_full TEXT, is_active BOOLEAN DEFAULT true,
  synced_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (market, code)
);
CREATE INDEX symbol_master_py ON symbol_master(market, py_initials, code);

CREATE TABLE index_daily (
  market TEXT NOT NULL, index_code TEXT NOT NULL, date DATE NOT NULL,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  prev_close DOUBLE, change_pct DOUBLE, source TEXT NOT NULL,
  PRIMARY KEY (market, index_code, date)
);

CREATE TABLE symbol_backfills (
  id TEXT PRIMARY KEY,             -- uuid
  market TEXT NOT NULL, code TEXT NOT NULL,
  open_key TEXT,                   -- '{market}|{code}'，结案置 NULL
  tries INT NOT NULL DEFAULT 0, last_error TEXT,
  status TEXT NOT NULL,            -- pending | deferred | done（done 行留给 prune 回收）
  created_at TIMESTAMPTZ NOT NULL, done_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX symbol_backfills_open ON symbol_backfills(open_key);
```

去重键走 `open_key` 而不是原计划的 partial unique index：DuckDB 2.0 实测不支持
partial unique index（`... WHERE status IN ('pending','deferred')` 那句直接报错），
所以开放期写非空占位、结案置 NULL——唯一索引里 NULL 互不冲突，同一只票结完之后还能再排。

`py_initials/py_full` 允许 NULL：手工直登、不在清单的票进 `symbols` 主档即可，代码表不强造行。

### 2.2 组件与数据流

- **`SymbolListRepo` / `IndexDailyRepo` / `SymbolBackfillRepo`**：新增于 `storage/repos/`，各自薄测（upsert 幂等、`open_key` 唯一索引防重）。
- **`SymbolMasterSync`（services）**：调 `registry.fetch_symbol_list(market)` → pypinyin 两列 → upsert + 缺席置 `is_active=false`。每周调度项：新 `JobSpec("symbol_master", WEEKLY, market, …)`，A/HK 各一。
- **`SymbolInterest`（services）**：`ensure(market, code, actor) -> EnsureReport`。四入口（dashboard 加自选、ledger 成交、rules 创建、theses 创建）在各自写成功后调用；它只做三件事：建主档（查代码表取名字）、判 `bars_daily` 空、上表插 pending（`open_key` 唯一索引保证幂等，见 §2.1）。
- **backfill 消费**：调度器 tick 里 `requests.drain()` 之后新增一步——取 `status=pending ORDER BY created_at LIMIT 3`，逐条 `sync.sync_daily(market, code, as_of-INITIAL_SYNC_DAYS, as_of)`；成功 → `done`；`SamsError` → `tries+1,last_error`，`tries≥3` 转 `deferred`。deferred 不再每拍碰；盘后链按 universe 自然重试，成功后把该股 backfill 清账（`sync_daily` 出口挂一步清账，避免同一票空转）。
- **搜索**：`GET /api/symbol-search?q=`（登录用户，htmx partial）。归一（D141）→ 单条 SQL 按 D142 分档取候选（`LIMIT 10`，`is_active=true`）→ 批量昨收 → 渲染候选行；候选行的加票按钮 POST 现有 `/watchlist/add`（服务端解析 market+code，前端传来的只有候选 id，不接受自由文本注入）。
- **大盘条**：看板模板顶部 partial `/api/partials/indices`：每市场读 `index_daily` 最近两日（收盘+涨跌幅+日期）；盘中若有指数即时报价（见下）以快照优先。休市文案示例：「上证 3,890.11 +0.42%（10-24 收盘）」。
- **指数盘中**：`quote` 轮询 universe 追加配置的指数代码（`UniverseBuilder` 增 `indices(market)` 一组），tencent/sina 的 `fetch_quotes` 按既有代码映射扩展支持指数串。**失败不影响日线路径**：报价拿不到指数时条子退回昨收盘语义。

### 2.3 页面改动

1. `/watchlist` 加票框：文本输入（placeholder「代码 / 首字母 / 名称，如 000001、payh、平安」）+ `hx-get` 下拉容器；市场下拉保留但被候选行携带的市场覆盖（选 `hk` 输入 `00700` 或选 `A` 输入 `sh600519` 都能归对）。备注列照旧。
2. 个股页空态（D146）：主档在、`bars_daily` 无行 ⇒ 「该股数据待同步：下一拍自动补，也可现在点」+ 按钮（POST `/symbol/{m}/{c}/sync` → `SymbolInterest.ensure` → 303 回本页，按钮变「已排队」）。backfill `deferred` 时按钮旁显示「今天已试过 3 次都失败：{last_error 首行}，今晚会随盘后任务再试」。
3. 自选表与持仓表的代码列统一渲染成 `/symbol/...` 链接（N4）。
4. 看板顶部大盘条（三市场分组横排），失败/无数据时整条不渲染（不给休市空白雪上加霜）。

### 2.4 配置

```toml
[indices.A]
codes = ["sh000001", "sz399001", "sz399006"]
[indices.HK]
codes = ["hkHSI", "hkHSTECH", "hkHSCEI"]
[indices.US]
codes = ["usSPX", "usIXIC", "usDJI"]
[indices.labels]
sh000001 = "上证指数"
sz399001 = "深证成指"
sz399006 = "创业板指"
hkHSI = "恒生指数"
hkHSTECH = "恒生科技指数"
hkHSCEI = "H股指数"
usSPX = "标普500"
usIXIC = "纳斯达克"
usDJI = "道琼斯"
```

展示名进配置：各源的指数名参差，界面念什么必须稳定。

代码表不设 `symbol_master_sync_weekday` 那一格：WEEKLY 周期本身在整周收满时到期
（`due.py` 的 `weekly_due`，判据是「下一个交易日已进下一 ISO 周」的那个收盘），
再加一个星期几旋钮就是让同一个 job 有两个互相矛盾的到期日。

## 3. 错误处理

- 代码表同步失败：旧表继续服务（D147）；源健康一行记账，管理后台健康页可见。
- backfill 消费中的单票失败：只记该票；绝不让 backfill 一步炸掉整拍（同 `_collect` 语义）。
- 搜索 SQL：`q` 为空/纯符号 → 直接空候选列表（不打库）；`q` 超长（>64 字符）截断后再查。LIKE 通配 `%_\` 一律转义（沿用批注搜索已钉的口径，防「输入 `%` 拖全表」）。
- 指数：某指数所有源失败 → 该条显示最近一行的旧数+「截至 {date}」；整市场无一行 → 该市场段不渲染。
- 直登非法代码形态：沿用 `watchlist_add` 现有 ValidationError 话术（市场没配/代码形态不对），不新造句子。

## 4. 测试口径（全离线，假源）

1. `symbol_master`：周同步 upsert 幂等、缺席置 `is_active=false`、pypinyin 抽样钉死（`平安银行→payh/pinganyinhang`、`腾讯控股→txkg…` 实际值以断言为准，源给什么名字测什么）。
2. 搜索：五档排序各造一票验先后；`hk00700` 归一；`q=pay` 前缀命中 `payh` 开头全部；`q=%` 转义不放大结果；昨收「—」与有数两态；候选上限 10。
3. `SymbolInterest`：四入口各测一次「新票 → backfills 恰一行」；同票重复不重复插（`open_key` 唯一索引，见 §2.1）；已有日线不插。
4. 消费循环：pending 3 条全消费、第 4 条留到下一拍；失败 3 次转 deferred；deferred 票盘后链成功后清账。
5. 个股页：代码表内票兜底建行；空态渲染+排队按钮；404 仅剩「代码表也查无」；自选/持仓链接形状一致（模板级断言 href）。
6. 指数：`index_daily` 假源窗口拉取与 upsert；大盘条休市文案（收盘+涨跌幅+日期）；某指数失败退回旧数；universe 追加指数不污染自选轮询断言（现有轮询测试保持绿）。
7. 层界：`test_layering` 保持绿（services 不 import scheduler/web；web 经表调 services）。

## 5. 与后续期的边界

- **SP2**：分时图与分钟线源、日/周/月切换、当日「未定版日线」（先由 `quotes_snapshot` 的 OHLCV 合成、标注未定，分钟线接入后改由分钟聚合）、估值面板（`valuation_daily` 现成表）、F10 资料（新源方法）。
- **SP3**：全市场每日快照表（akshare spot 一请求 5 千行）→ `/screener` 筛选、自选池对照排行、市场宽度（涨跌家数进大盘条槽位）。D142 的热度排序、D141 的多音字宽容在此期回收。
- 本期明确**不做**：美股代码表、指数分钟线、搜索结果分页。

**计划落点**：`docs/superpowers/plans/`，按 writing-plans 拆为 6-8 个任务（迁移+repos → pypinyin 依赖与同步链 → 源能力 → SymbolInterest 四入口 → 消费循环 → 搜索页 → 个股页/链接 → 大盘条+指数源），逐任务 TDD、双评审。
