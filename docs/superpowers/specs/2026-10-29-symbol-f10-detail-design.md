# 个股页 F10：公司概况、主要财务指标与公告（D214–D216）

用户原话：

> 「筛选出来的股票，点击股票时，除了可以看到到该股票K线外，应该还可以看到其
> 详细资料，比如F10的信息、公告等。」
>
> 「包括自选股、持仓股，都应该是这样的。查看股票详情都应该可以看到，你查看一下
> 东方财富、同花顺、万德股票等的个股页面。」

选股结果（D211）、持仓（`portfolio.html`）、报价板（`partials/quotes.html`）已经链到
`/symbol/{market}/{code}`；**自选股那张表还没有链接**，本轮一并补上（D214）。
个股页此后不只是一张图，而是一只票的「现场 + 档案」。

## 参照：东财 / 同花顺 / 万德的个股页都回答哪几问

三家页面结构不同，但一只票的「资料」都落在同一组问题上：

| 分节 | 回答的问题 | 本系统现状 |
|---|---|---|
| 公司概况 | 它到底是谁、做什么、在哪上市、谁管事、怎么联系 | **没有** |
| 主要财务指标 | ROE、营收/利润增速、股息率、市值，最近几期 | A 股有（`financials`），**HK 没有** |
| 估值 | PE(TTM)、PB、市值，哪一天 | A/HK/US 都有（`valuation_daily`） |
| 分红送转 | 除权日、每股派多少、送转多少 | A/HK 都有（`corp_actions`） |
| 公告 | 最近发布了什么、原文在哪 | **没有** |
| 资金流 | 主力/北向净流入 | A 股有（`funding_flow`），港美股无源 |

本轮补的是三块空白（公司概况、主要财务指标里的港股那一半、公告），其余几块用
现成的库读出、摆进同一屏。**不是**把东财页面整页搬过来：数据只进本系统自己的表，
正文与 PDF 留外链。

## 实测过的端点（2026-10-29）

| 数据 | A 股 | 港股 |
|---|---|---|
| 公司概况 | `PC_HSF10/CompanySurvey/PageAjax?code=SH600519` → `jbzl` / `fxxg` | `PC_HKF10/CompanyProfile/PageAjax?code=00700` → `zqzl` / `gszl` |
| 公告 | `np-anotice-stock.../ann?...&ann_type=A&stock_list=600519`（支持 `begin_time` / `end_time`） | 同一接口 `ann_type=H&stock_list=00700` |
| 主要财务指标 | 已有（`akshare_cn` → 东财 F10） | `datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_HKF10_FN_MAININDICATOR&filter=(SECUCODE="00700.HK")` |
| 公告详情页 | `data.eastmoney.com/notices/detail/600519/{art_code}.html`（200） | `data.eastmoney.com/notices/detail/00700/{art_code}.html`（200） |

港股那条「主要财务指标」端点本轮才找到：`PC_HKF10/CompanySurvey` 与
`PC_HKF10/NewFinanceAnalysis` 都是 404，`PC_HSF10?...code=HK00700` 回「股票代码不合法」。
数据中心接口给 `ROE_AVG` / `OPERATE_INCOME_YOY` / `HOLDER_PROFIT_YOY` / `REPORT_DATE`，
但**不给披露日**——这条缺口决定了下一节的口径。

## 设计

### A. 页面：个股页多一块「资料」，异步加载（D214）

`/symbol/{market}/{code}` 保持现有顺序：抬头 → 图 → 空态排一拍 → 规则 → 论点 →
告警 → 批注。在**图之后、空态之前**插一格局部：

```html
<div hx-get="/api/partials/f10?market={{ market }}&code={{ code }}"
     hx-trigger="load" hx-swap="outerHTML">
  <p class="muted">资料加载中…</p>
</div>
```

为什么是异步局部而不是首屏服务端渲染：个股页已经有两条异步（图、批注），资料要读
四张表，任何一张锁库都不该让 K 线跟着晚到；而且这一段与其余部分没有共享状态，单独
一个 `GET /api/partials/f10` 就够。

端点与页面同一套归一/404（复用 `_require_symbol`）：`/symbol/HK/700` 与
`/symbol/HK/00700` 指向同一份资料，库里的键只有一种形态。

局部渲染四张卡（顺序按「一眼先看谁」）：

1. **公司概况**：全称/英文名、行业、交易所/板块、上市日、官网、董事长/总经理/公司秘书、
   电话/邮箱、办公地址、员工数、公司简介（`<details>` 折起）。
   港股多两格：ISIN、每手股数、是否港股通标的。A 股多一格证监会行业。
2. **主要财务指标**：最近 8 期的报告期、ROE、营收同比、净利润同比、股息率、披露日；
   最上面一行是最近一个交易日的估值（PE/PB/市值），来自 `valuation_daily`。
   比率从库里的小数还原成百分数只在这一处（`ratio_pct`），与 D58 的入库口径对称。
3. **分红送转**：最近 6 条 `corp_actions`（`ex_date <= 今天`），派息写每股金额、
   送转写比例，`unknown` 原样写它的备注——认不出的方案不许擦掉（D39）。
4. **公告**：最近 10 条，日期 + 标题（外链到东财详情页）+ 分类。

自选股那行补链接：`watchlist.html` 的代码列改成
`<a href="/symbol/{{ row.market }}/{{ row.code }}">{{ row.code }}</a>`。

### B. 两项新能力、两个新源（D214）

`datasource/capabilities.py` 加两行：

```python
"company_profile": Capability(label="公司概况", lacking="不提供公司概况", probe=has_profile),
"announcements": Capability(label="公告", lacking="不提供公告", probe=has_announcements),
```

适配器：

- **`eastmoney_f10`**（`markets = ("A", "HK")`）：`company_profile` + `announcements`。
  两家市场走同一族接口，只差代码形态（`SH600519` / `00700`）与 `ann_type`（`A` / `H`）。
  解析放 `datasource/f10.py`（纯函数，进 `_NOT_ADAPTERS`），网络与归类留在适配器。
- **`eastmoney_hk_f10`**（`markets = ("HK",)`）：只有 `financials`，走数据中心
  `RPT_HKF10_FN_MAININDICATOR`。不并进 `eastmoney_f10`：能力是方法级的，一旦把
  `financials` 写在同时服务 A 的适配器上，A 股的财务链就会多出一个只会答港股的成员。

为什么不直接扩现有 `eastmoney`（A 股日线备源）：它声明 `markets=("A",)`，加 HK 会让
HK 的日线链多一个成员；加 `financials` 又会让它进 A 的财务链。新源只在需要的地方出现。

注册表复用现有 `_fetch_frame` 那条链（降级、源健康、0 行继续问、全链都说没有才确认）：

```python
def fetch_company_profile(self, market, code, start, end) -> FrameResult: ...
def fetch_announcements(self, market, code, start, end) -> FrameResult: ...
```

公司概况没有时间窗口，方法签名里的 `start/end` 只为与 `_walk` 同形，适配器不拿它裁剪；
公告按 `begin_time/end_time` 让源自己筛。

### C. 两张新表（迁移 0027）

```sql
CREATE TABLE IF NOT EXISTS company_profiles (
    market      TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    full_name   TEXT,
    name_en     TEXT,
    industry    TEXT,
    industry_csrc TEXT,
    exchange    TEXT,
    board       TEXT,
    listed_on   DATE,
    founded_on  DATE,
    isin        TEXT,
    lot_size    INTEGER,
    chairman    TEXT,
    president   TEXT,
    secretary   TEXT,
    legal_person TEXT,
    phone       TEXT,
    email       TEXT,
    website     TEXT,
    address     TEXT,
    reg_address TEXT,
    employees   INTEGER,
    auditor     TEXT,
    fiscal_year_end TEXT,
    hk_connect  BOOLEAN,
    stock_type  TEXT,
    summary     TEXT,
    source      TEXT NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL,
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
```

两张表的键都在写入层归一（D50）：`HK`/`700` 与 `HK`/`00700` 不能各存一行。
`fetched_at` 由 SQL `now()` 写，仓库不接受调用方传时钟——它不是业务时间。

**字段映射**（只列有值的那些，源没给的格留 NULL，不拿 `—`、`0` 或空串冒充）：

| 列 | A 股（`jbzl` / `fxxg`） | 港股（`zqzl` / `gszl`） |
|---|---|---|
| name | `SECURITY_NAME_ABBR` | `zqzl.zqjc` |
| full_name / name_en | `ORG_NAME` / `ORG_NAME_EN` | `gszl.gsmc` / `gszl.ywmc` |
| industry / industry_csrc | `EM2016` / `INDUSTRYCSRC1` | `gszl.sshy` / NULL |
| exchange / board | `TRADE_MARKET` / `SECURITY_TYPE` | `zqzl.jys` / `zqzl.bk` |
| listed_on / founded_on | `fxxg[0].LISTING_DATE` / `fxxg[0].FOUND_DATE` | `zqzl.ssrq` / `gszl.gsclrq` |
| isin / lot_size | NULL / NULL（手数在市场配置） | `zqzl.isin` / `zqzl.zxjydw` |
| chairman / president / secretary / legal_person | `CHAIRMAN` / `PRESIDENT` / `SECRETARY` / `LEGAL_PERSON` | `gszl.dsz` / NULL / `gszl.gsms` / NULL |
| phone / email / website | `ORG_TEL` / `ORG_EMAIL` / `ORG_WEB` | `gszl.lxdh` / `gszl.email` / `gszl.gswz` |
| address / reg_address | `ADDRESS` / `REG_ADDRESS` | `gszl.bgdz` / `gszl.zcdz` |
| employees / auditor | `EMP_NUM` / `ACCOUNTFIRM_NAME` | `ygrs`（去逗号）/ `gszl.hss` |
| fiscal_year_end / hk_connect / stock_type | NULL | `gszl.njr` / `sfhgtbd == "是"` / `zqlx` |
| summary | `ORG_PROFILE` | `gszl.gsjs` |

`summary` 原样存（含源里的全角空格与换行），显示时由 CSS 折行；不做清洗——清洗过
的简介与源里的简介是两段话，改坏时无从对照。

**公告**：

- `title` 取 `title`（A 股带「贵州茅台:」前缀，原样保留，改名字会让同一条公告在
  两个市场看起来不是同一种东西）；
- `published_on` 取 `notice_date`，缺失时退回 `display_time` 的日期部分；
- `category` 是 `columns[*].column_name` 用「、」连接；
- `url` = `https://data.eastmoney.com/notices/detail/{stock_code}/{art_code}.html`；
- 一次最多取回最近 100 条（`page_size=100`、单页）。窗口只用来裁剪这 100 条：
  **公告不是任何计算或回测的输入**（D216），这里不追求全历史；缺的是更早的条目，
  不是某个数。这条限制写进适配器 docstring，不在页面上假装「它只有这些公告」。

### D. 港股财务指标：有数，但没有披露日（D215）

`eastmoney_hk_f10.financials` 把 `RPT_HKF10_FN_MAININDICATOR` 映射进现有
`financials` 表：

- `report_date` ← `REPORT_DATE`；
- `roe` ← `ROE_AVG`（百分数，÷100）；
- `revenue_yoy` ← `OPERATE_INCOME_YOY`（÷100）；
- `profit_yoy` ← `HOLDER_PROFIT_YOY`（÷100）；
- `published_date` ← **NULL**：源不给，也不许拿 `REPORT_DATE + 45 天` 去猜，
  那正是 D61 要挡的「往回开时间机器」；
- `dividend_yield` / `market_cap` ← NULL：源的 `DIVIDEND_RATE` 与
  `TOTAL_MARKET_CAP` 各是哪个时点、哪个口径都没有核实过，不往里塞。

后果与边界必须一起写进屏幕：

- `FinancialRepo.load_as_of` / `history` 照旧只认 `published_date <= as_of` 的行，
  所以港股这些期**不参与** PR、选股与「已披露」判断；
- 个股页要显示它们，只能走一条新读法 `FinancialRepo.recent(market, code, limit)`：
  含 NULL 披露日、按报告期倒序，**只给展示用**。方法 docstring 写死这句话，
  免得下一个读它的人把它当成第二个 `load_as_of`；
- 页面上「披露日」列写「未给」，并在表脚注：
  「本市场源不给披露日：这些指标只用于看数，不参与选股与市赚率（D215）。」

这是一条**有意接受的不完整**：拿得到数，拿不到它何时公开。把它写成「已披露」会
让回测凭空多出未来的信息；把它丢掉则个股页对港股又是空白。折中是：存、标、不给计算用。

### E. 同步入口：CLI 两条 + 夜间的两条 job

CLI（`cli/sync.py`）：

```
sams sync profile       --market A --code 600519
sams sync announcements --market A --code 600519 [--start 90 天前] [--end 今天]
```

- `profile` 不吃窗口：一票一行，重复执行是覆盖；
- `announcements` 默认最近 90 天；`--start/--end` 只裁剪「最近 100 条」，
  不改变「一次最多 100 条」这条源侧限制。

调度（`services/scheduler/jobs.py`）：

```python
_DATA_JOBS = (
    ...,
    ("company_profile", "profile", run_profile),
    ("announcements", "announcements", run_announcements),
)
```

- 仍然按市场登记：链上没有这个能力的市场（本轮的 US）不登记 job——每天一条
  「取不到公司概况」会把真故障淹掉（D106）；
- `run_profile` 单元是票，窗口忽略；
- `run_announcements` 窗口取 `as_of - 30 天`；
- 只按只隔离：一只票失败进 `errors`，其余照常写。
- 新服务 `F10SyncService` 与读侧 `F10Service` 进 `ServiceContext`；`JobDeps` 加一格
  `f10`（三处测试的 `JobDeps(...)` 构造点会跟着改）。

首次上线顺序：迁移 0027 → 停服 → 跑 `sams sync profile/announcements`（A/HK 的自选、
持仓、规则标的）→ 起服。夜间的两条 job 从此接管。

### F. 空态：三种「没有」不许长成同一个样子

个股页那一块在三种情况下都可能是空的，下一步各不相同：

1. **这个市场没有源**（US）：卡片直接说「美股的这条链上还没有公司概况/公告的源」，
   不列空表。判据来自 `registry.chain_for(market, 能力)`，与健康页同一份。
2. **有源但还没采**：说「还没采集」，并给出命令
   `sams sync profile --market A --code 600519`；不摆一个点了也不动的按钮。
3. **采过，源答「没有」**：说「源在这段时间没有公告」/「源没有这只票的概况」，
   不加「暂时」「可能」这类替源开脱的词——`confirmed_empty_by` 已经说了是谁答的。

实施注记（0027 落地时）：第 3 种目前**只在采集那一刻存在**——`confirmed_empty_by`
随 `F10SyncSummary` 回到 CLI 打印「确认查无=东财」，两张表里 0 行没有地方记「谁答的
没有」，页面因此把第 3 种并进第 2 种的文案（本地没有 + 命令）。要让页面自己说出
第 3 种，得再加一张「某票某能力已被某源确认查无」的标记表；在补上之前，宁可说
「本地还没有」，不凭猜测替源说「它没有」（与 §F 开头同一条底线）。

财务指标与估值分两张小表：港股「无披露日」的脚注只在本市场出现；A 股不显示那句，
免得把一条真实的口径说成缺口。

## 明确不做

- **不抓公告正文与 PDF 正文**（D216）：只存元数据 + 东财详情页外链。正文有版权、
  体量与解析三层成本，而用户要的是「点进去能看原文」——外链已经满足；抓下来再
  存一份，会让「源删了公告」与「我们没抓到」变成同一种空白。
- **不做美股 F10**：现有链上没有能给美股公司概况/公告的源。宁可明说缺，不拿
  腾讯的行情接口或第三方页面拼一份来路不明的资料。
- **不做港股三大报表**（利润表/资产负债表/现金流量表）：本轮只做主要指标；
  三张表的字段口径与单位（千港元/人民币、重述）要先逐列核实，不在这轮硬上。
- **不把港股财务指标接进 PR / 选股**：没有披露日就没有 PIT（D215）。
- **不在页面请求里同步抓源**：打开一只票不该等一次外网请求；采不到就显示空态与命令。
- **不改用户数据**：与 D205 一致，本轮的写面只有两张新表和 `financials` 的新市场行。

## 测试

**解析（录制 payload）**

- A 股概况：`jbzl` + `fxxg` 合成一行，`listed_on`/`founded_on` 落成 `date`，
  `industry` 与 `industry_csrc` 各自对，简介原样。
- 港股概况：`zqzl` + `gszl` 合成一行，`isin`/`lot_size`/`hk_connect`/`fiscal_year_end`
  对；`ygrs = "87,412"` → `87412`。
- 概况缺 `jbzl` / 缺 `zqzl` → `FieldDrift`（不是「这只票没概况」）。
- A 股公告：`notice_date` 落成 `date`、`category` 用「、」连接、`url` 用
  `notices/detail/600519/...`；请求参数 `ann_type=A`、`stock_list=600519`。
- 港股公告：`ann_type=H`、`stock_list=00700`，`url` 用 `notices/detail/00700/...`。
- 公告 `data.list = []` → 0 行帧（是「这段时间没有」的答复）；缺 `data` → `FieldDrift`。
- 港股财务：`ROE_AVG=9.966...` → `roe=0.09966`、`OPERATE_INCOME_YOY=10.07` →
  `0.1007`、`published_date is None`、`report_date=2026-06-30`。
- 港股财务空结果 → 0 行帧；缺必需列 → `FieldDrift`；窗口外的期被裁掉。
- 连接错误 → `TransientError`。

**仓库**

- 概况 upsert 幂等（同键两次仍是 1 行）、键归一（`hk`/`700` 写成 `HK`/`00700`）。
- 公告同 `art_code` 重写不增行、`latest` 按日期倒序、无标题的行被丢弃并 warn。
- `FinancialRepo.recent` 含 NULL 披露日的期；`load_as_of` / `history` 仍然不含（D215）。

**服务与调度**

- `F10SyncService.sync_profile`：降级时 `degraded=True`；空答复写 0 行。
- `sync_announcements`：0 行不写；同窗口重跑行数不变。
- `build_jobs`：A/HK 有 `profile` 与 `announcements`，US 没有；job 按只隔离失败。
- 个股资料读模型：概况 + 最近 8 期 + 最近 10 条公告 + 分红 + 最新估值。

**Web**

- `/api/partials/f10?market=A&code=600519` 渲染出概况、财务表、公告链接；
  `/symbol/HK/700` 与 `/symbol/HK/00700` 同一份资料。
- US：两张卡都写「这条链上还没有源」，不渲染空表。
- 未采集：写「还没采集」与命令 `sams sync profile`。
- 未登录 303、不存在的票 404（与其余页面同一套）。
- `/watchlist` 的代码链到 `/symbol/{market}/{code}`。

**live（联网，单独标记）**

- A 股 `600519`：概况有行业与官网、公告至少 1 条、财务至少 4 期。
- 港股 `00700`：概况有 ISIN 与港股通标记、公告至少 1 条、财务至少 4 期且
  披露日为 NULL。

## 验收

1. 迁移 0027 在现有库上一次应用成功，`sams source list` 能看到
   `eastmoney_f10`（A/HK）与 `eastmoney_hk_f10`（HK）。
2. `sams sync profile --market A --code 600519` 与
   `sams sync announcements --market HK --code 00700 --start … --end …` 各写一次、
   重跑行数不变。
3. `/symbol/A/600519`、`/symbol/HK/00700` 四张卡都有内容；`/symbol/US/AAPL`
   明确说「没有源」而不是空表。
4. 港股财务那几行在屏幕上标着「披露日：未给」，并且不进入 `sams screen pr` 的
   已披露样本（现有 PR 测试不需要改，也不该因此多出样本）。
5. 自选股、持仓、选股结果、报价板四个入口都能进同一张个股页。
6. 全量测试绿；`sams serve` 在 127.0.0.1:8765 上重启后页面可读。
