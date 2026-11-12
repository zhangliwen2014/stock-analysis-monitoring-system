# 个股页估值空态的下一步：按市场能力分支（D338）

## 需求

R3-02 的原话里，个股页的估值空态仍在印一条命令：

```
本地还没有这只票的估值。采一次： sams sync valuation --market A --code 600519
```

D336 把个股页的财务卡与分红卡改成了「已排进后台补采队列」，并在取舍一节里明确留下
这一片：

> 估值空态不在本片：HK/US 的估值源本身没有，按 §5-D2 的规矩要把话术改成「这个市场
> 暂时没有这个源」，那是另一句需要按市场能力分支的话，不在这一片顺手做半截。

D336 写这句话时把前提当成「HK/US 没有估值源」。现在这条前提不成立：`yfinance_us`
与 `eastmoney_global` 都实现了 `valuation`，`baidu_valuation` 覆盖 A/HK——港美股
链上有没有估值源取决于配置与源能力，不能按市场名硬编码。所以这一片的判据只有一个：
`registry.chain_for(market, "valuation")` 是否非空（`F10Service._has` 已有同一形状）。

仍然是同一条冲突：网页用户没有终端，`sams serve` 占着库时命令行只会拿到
`DatabaseLocked`；数据缺口该由后台自动补，而不是让人去敲命令。

## 取舍

1. **空态按能力分三支，不按市场名分。** 链上没有估值源 → 说「{市场}的这条链上还没有
   按日估值的源」；有源但库里没有这只票的估值 → 排进 `symbol_backfills`（
   `kind=valuation`），说「已排进后台补采队列」；有源但代码表不认识的票不排
   （D314），说明「代码表里没有这只票」。判断能力走
   `F10Read.has_valuation_source`，与概况/财务/公告三个 `has_*_source` 同一处来源。
2. **打开卡片就排（D283）。** 整张资料块与 htmx 只换财务卡的两条出口都要排，
   也都要说同一句话：那句话是承诺，说出「已排进」时队列里必须真有这一行。
   已有估值行时不排——空态才排，补上的数据不会被下一次进入覆盖。
3. **网页不再出现 `sams sync valuation`。** 两个 handler 上下文里的
   `valuation_command` 一并删除，模板只认 `read.valuation` / `has_valuation_source` /
   `valuation_queued`。命令行不受影响：`sams sync valuation` 仍是 CLI 用户的下一步
   （`sams screen`、`sams pr` 的出口不动）。
4. **不碰「估值位置」卡。** PR/PE/PB 分位卡的「没有」来自样本不足或复权拒绝
   （`ValuationService` 的 `reason` 是事实），不是「本地缺这一票的估值」；那几句
   已经不带命令，不需要再分岔。

## 契约

### 服务层

| 项 | 值 |
| --- | --- |
| `_VALUATION_METHOD` | `"valuation"`（与 `registry.fetch_valuation` 的 method 同名） |
| `F10Read.has_valuation_source` | `F10Service._has(market, "valuation")`；整个市场没配源 → `False` |

### 个股页

`_queue_valuation(ctx, market, code, read) -> bool`（与 `_queue_financials` 同形）：

* `read.valuation is not None` → `False`（已有数，不排）
* `not read.has_valuation_source` → `False`（排进去每一拍都只会得到「没有这个源」）
* `not ctx.symbol_list_store.admits(market, code)` → `False`（D314）
* 否则 `ctx.backfills_store.enqueue(market, code, VALUATION)`，返回 `True`

`f10_partial` 与 `f10_financials_partial` 都传 `valuation_queued=_queue_valuation(...)`，
并删除 `valuation_command`。

### 模板（`partials/f10_financials.html`）

估值行四支，顺序固定：

1. `read.valuation` → 现在的最近交易日一行（PE / PB / 股息率 / 市值）。
2. `not read.has_valuation_source` → `{{ market_name }}的这条链上还没有按日估值的源。`
3. `valuation_queued` → `本地还没有这只票的估值，已排进后台补采队列：补完刷新这一页。`
4. 其余（有源、未排：代码表不认识的票）→
   `本地还没有这只票的估值；代码表里没有这只票，补采排不上——先等代码表同步。`

三句「没有」都不带 `sams `。

## 测试

* 测试替身 `StubF10Adapter` 补一条 `valuation` 能力（返回 `empty_valuation()`）：
  A 股链上因此「有源」，US/HK 的替身仍没有 → 两条分支都能在 web 用例里走到。
* A 股无本地估值：整张资料块与财务卡局部都说「已排进后台补采队列」，页面没有
  `sams sync valuation`，队列有 `kind=valuation` 的 pending 行。
* US（无源）：说「美股的这条链上还没有按日估值的源」，不排、不说「已排进」。
* A 股代码表不认识的票：不说「已排进」、说「代码表里没有这只票」，队列无行。
* 已有估值行：不排。

## 验证

* 串行 pytest（web 资料卡相关套件）、`ruff check src tests`。
* 生产库副本 + 临时服务（`SAMS_DB_PATH` + `--port 8799`）：找一只 A 股无估值的票与
  一只港股，确认空态话术、队列行与「没有源」的市场；不操作生产通道。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。
