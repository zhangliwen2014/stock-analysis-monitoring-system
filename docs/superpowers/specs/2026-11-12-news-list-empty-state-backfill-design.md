# 资讯列表页空态接补采队列（D341）

## 需求

R3-02 点名网页里的命令行话术。D336–D340 清掉了个股页六格与建规则屏两处，资讯列表页
（`pages/news.py::_empty_message`）还剩一句：

> 最近 30 天没有公告或新闻。盘后任务会自动采集；也可以按代码补一次：
> `sams sync news --market {market} --code {codes[0]}`

仍是同一条冲突：网页用户没有终端，`sams serve` 占着库时命令行只会拿到
`DatabaseLocked`。D339 已经给 `symbol_backfills` 加了 `news` 活、消费者也会分派到
`NewsService.sync`——这一片把列表页这句也接到同一条队列上，沿用 D283「打开就排」、
D336/D338/D339「那句话是承诺，队列里必须真有那一行」的形状。

组合 NAV 卡的 `sams nav rebuild`、组合成交的 `sams sync fx` 是另两处不同子系统的话术，
不在这一片。

## 取舍

1. **复用 D339 的 `news` 活，不新增 kind。** 列表页空态命中的那一支（`covered` 且
   `codes` 非空、`total==0`、非 `only_hits`）就是「这些票最近 30 天没有资讯」。把
   `codes` 里准入的票逐只排进 `news`：

   ```python
   def _queue_news_scope(ctx, market: str, codes: list[str]) -> bool:
       queued = False
       for code in codes:
           if not ctx.symbol_list_store.admits(market, code):
               continue
           ctx.backfills_store.enqueue(market, code, NEWS)
           queued = True
       return queued
   ```

   `enqueue` 幂等：列表反复刷新不刷屏。`codes` 来自 `_scope`——带 `code` 参数时是
   单只（个股页「全部」链进来），否则是自选 + 持仓 + 观察。逐只排正是「这些票的资讯
   后台补」，与个股页 D339 同一意图，只是范围从一只扩到这一屏的标的。
2. **文案按「排没排上」分支。** `_empty_message` 增 `ctx` 参数，在最后那一支先
   `_queue_news_scope`：
   * 排上了 → `最近 {N} 天没有公告或新闻。盘后任务会自动采集；已排进后台补采队列：补完刷新这一页。`
   * 一只都没排上（代码表都不认识）→ `最近 {N} 天没有公告或新闻。代码表里没有这些票，补采排不上——先等代码表同步。`

   其余几支（`total>0`、`only_hits`、搜索无匹配、`not covered`、`codes` 为空）不动。
3. **只排 `news`，不顺手排 `announcements`。** 被替换的命令就是 `sams sync news`；
   公告由同名盘后 job 与个股页 D339 的 `announcements` 活覆盖，不在这一片重复排，
   免得给同一屏标的翻倍落队。
4. **准入问链下那道闸（D314），市场已在路由层校验。** `news_page` 入口已挡未配置市场
   （`market not in ctx.settings.markets` → ValidationError），故 helper 只需逐只
   `admits`；整表未同步的市场 `admits` 返回 True，新装库第一晚照样排得上。

## 契约

* `pages/news.py` 增 `from sams.storage.repos.symbol_backfills import NEWS` 与
  `_queue_news_scope(ctx, market, codes) -> bool`。
* `_empty_message` 签名增 `ctx`（放在关键字参数里），最后那一支按 `_queue_news_scope`
  的返回值选两句文案之一；不再出现 `sams sync news`。调用方 `news_page` 传 `ctx=ctx`。
* 模板无需改：文案在 Python 字符串里。

## 测试

* 新增（`test_news_web.py`）：A 股自选一只无资讯的票（`_symbol`+`_watch`，不喂
  `_news`/`_announcement`）→ `/news?market=A` 空态说「已排进后台补采队列」、无
  `sams sync news`、`backfills_store.open_for("A","600519",kind=NEWS)` 非 None。
* 新增：代码表认识的市场里自选一只表外票（先 `symbol_list_store.upsert_batch` 播种
  一条 active 使 `count>0`，再 watch 表外代码）→ 空态说「代码表里没有这些票」、不排、
  不说「已排进」。
* 新增：已有资讯的票（喂 `_news`）→ 列表照常渲染，不排 news（`open_for` 为 None）。
* 既有 `test_a_market_without_a_news_source_says_so`（HK 未接入）不动：那一支不排队。

## 验证

* 串行 pytest（`test_news_web.py`）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：找一个自选里有、但最近 30 天无资讯的 A 股市场，打开
  `/news` 确认空态话术与队列行；不操作生产通道。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

* `ruff check src tests`：All checks passed。
* 串行 pytest：`test_news_web.py` 18 passed（含 3 个新增）、`test_alerts_news_web.py`
  2 passed。`_empty_message` 仅 `news_page` 一处调用，已同步传 `ctx`。
* 生产库副本（`tmp/d341_verify.duckdb`）+ 临时服务 8799，登录 sp1check，走单代码路径：
  * 无资讯的 A/605177（`/news?market=A&code=605177`）：空态说「最近 30 天没有公告或
    新闻。盘后任务会自动采集；已排进后台补采队列：补完刷新这一页。」，无 `sams sync news`。
  * 有资讯的 A/600519：列表照常渲染，无排队话术。
  * 停服查库：`symbol_backfills` 有 A/605177 的 `news` pending 行；A/600519 无 news 行。
    全程未操作生产通道。
* 生产 `sams serve` 重启后 `/healthz` 200、`/login` 200。