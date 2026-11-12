# 个股页概况/公告/新闻空态接补采队列（D339）

## 需求

R3-02 把个股页的命令行话术逐条点了名。D336/D337/D338 已经清掉财务卡、分红卡、
估值卡与选股页技术面缺口，个股页还剩两格在印命令：

* 概况卡（`partials/f10.html`）：`本地还没有这只票的概况。采一次：sams sync profile …`
* 资讯卡（同文件，公告与新闻合并成一格）：
  `本地还没有这只票的公告或新闻。采一次：sams sync announcements … 与 sams sync news …`

仍是同一条冲突：网页用户没有终端，`sams serve` 占着库时命令行只会拿到
`DatabaseLocked`。这一片把这三格也接到 `symbol_backfills` 上，沿用 D283「打开页面
就排」、D336/D338「那句话是承诺，队列里必须真有那一行」的形状。

组合 NAV 卡的 `sams nav rebuild`、规则页的 `sams sync daily`、组合成交的
`sams sync fx`、新闻列表页的 `sams sync news` 是另几处不同子系统的话术，不在这一片。

## 取舍

1. **三种新活，名字跟 job 走。** `symbol_backfills` 增 `profile` / `announcements` /
   `news` 三个 kind（与 `_DATA_JOBS` 里的 job 名一致）。消费者按名字分派到
   `F10SyncService.sync_profile` / `sync_announcements` 与 `NewsService.sync`，
   窗口与同名 job 同口径：概况无窗口（一票一行覆盖）、公告最近 30 天
   （`ANNOUNCEMENTS_WINDOW_DAYS`）、新闻最近 30 天（`NEWS_WINDOW_DAYS`）。
   三者的 0 行都是合法答复（档案没有记录不是补数失败，D36），消费者记 done。
2. **有没有源问链，不按市场名猜。** 概况/公告用已有的
   `F10Read.has_profile_source` / `has_announcement_source`；新闻新增
   `has_news_source`（`chain_for(market, "stock_news")`）。资讯卡那句「v0 只接 A 股」
   仍由 `NEWS_MARKETS`（`news_covered`）说，但「排不排」要看链上真有没有新闻源——
   A 股没接新闻源时不排 news，免得每一拍白试一次（D336 同一条规矩）。
3. **打开资料块就排（D283）。** 三格都只在整张资料块（`f10_partial`）里渲染，
   htmx 财务卡局部不含它们，所以排队只发生在 `f10_partial`。代码表不认识的票不排
   （D314），已有数据不排。
4. **资讯卡按「能不能排」重排空态分支。** 公告与新闻合并成一格，空态先说「有没有源」，
   再说「排没排」，最后才是「代码表不认识」：只要公告或新闻任一可排就说「已排进」，
   两个源都没有才说「这条链上还没有公告或新闻的源」。原来 HK/US 那句「这里只有公告」
   被更可操作的排队/缺源话术取代。

## 契约

### 仓库与消费者

| 项 | 值 |
| --- | --- |
| 新 kind 常量 | `PROFILE="profile"` / `ANNOUNCEMENTS="announcements"` / `NEWS="news"` |
| `BackfillConsumer` 新依赖 | `f10=None` / `news=None`；缺服务时该行记失败留在队列（与 `_fundamentals()` 同形） |
| `profile` 窗口 | 无（`sync_profile(market, code)`） |
| `announcements` 窗口 | `end - ANNOUNCEMENTS_WINDOW_DAYS .. end` |
| `news` 窗口 | `end - NEWS_WINDOW_DAYS .. end` |
| 三者 0 行 | 合法答复，记 done（`_fetch` 返回 False） |

`bootstrap` 把 `f10_sync` / `news` 注入消费者。这三种 kind 只由页面排成 pending，
没有孤源止损的 deferred 账，消费者吃完即 done，不需要 job 侧 `_settle`/`_rearm`。

### 服务层

`F10Read.has_news_source: bool`，由 `F10Service._has(market, "stock_news")` 填。

### 个股页

三个 helper（与 `_queue_valuation` 同形），只在 `f10_partial` 调用：

* `_queue_profile`：`read.profile is not None` 或无概况源 → 不排；否则在册才
  `enqueue(..., PROFILE)`。
* `_queue_announcements`：`read.announcements` 非空或无公告源 → 不排；否则在册才
  `enqueue(..., ANNOUNCEMENTS)`。
* `_queue_news`：`news_feed` 非空、市场不在 `NEWS_MARKETS`、或无新闻源 → 不排；
  否则在册才 `enqueue(..., NEWS)`。

`f10_partial` 传 `profile_queued` / `announcements_queued` / `news_queued`，
删除 `profile_command` / `announcement_command` / `news_command`。

### 模板（`partials/f10.html`）

概况卡（接在「没有概况源」之后）：

* `profile_queued` → `本地还没有这只票的概况，已排进后台补采队列：补完刷新这一页。`
* 其余 → `本地还没有这只票的概况；代码表里没有这只票，补采排不上——先等代码表同步。`

资讯卡空态分支顺序：

1. `news_feed` → 列表。
2. `not has_announcement_source and not news_covered` →
   `{{ market_name }}的新闻源未接入；这条链上也没有公告的源。`
3. `announcements_queued or news_queued` →
   `本地还没有这只票的公告或新闻，已排进后台补采队列：补完刷新这一页。`
4. `not admitted` →
   `本地还没有这只票的公告或新闻；代码表里没有这只票，补采排不上——先等代码表同步。`
5. 其余 → `{{ market_name }}的这条链上还没有公告或新闻的源。`

三句「没有」都不带 `sams `。

## 测试

* 消费者：`profile`/`announcements`/`news` 各分派到对应服务、窗口与 job 同口径、
  0 行记 done、缺服务记失败留队列。
* 测试替身 `StubF10Adapter` 补 `stock_news` 能力（返回 `empty_news()`）：A 股链上
  因此有新闻源；US/HK 替身仍没有。
* 个股页：A 股空概况/空资讯 → 队列有对应 kind 的 pending 行、文案是「已排进」、
  页面没有 `sams sync profile/announcements/news`；US 无源 → 说缺源不排；
  代码表不认识的票不排不说已排；已有概况/公告不排。
* 既有 `test_a_source_without_local_data_prints_the_command` 改成排队断言。

## 验证

* 串行 pytest（消费者 + 个股页 F10 套件）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：找无概况/无公告的 A 股与港股，确认空态话术与队列行；
  不操作生产通道。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。
