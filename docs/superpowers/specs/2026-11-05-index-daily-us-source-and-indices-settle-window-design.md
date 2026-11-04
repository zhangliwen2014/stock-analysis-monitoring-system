# 美股指数的来源，与指数日线那一道不回头的窗口（#375）

**日期**：2026-11-05　**来源**：第三轮专家实机报告 #375（原话「大盘条与港股/美股日线落后一天」），
承 T2 §5 与上一轮 §9（`2026-11-05-daily-bar-settle-window-and-close-basis-design.md`）
**目标**：让休市看板的美股那一段真的有三条指数，并把「库里一根指数日线都没有」这种空序列
从 `ok · 3/3` 里揪出来——和上一轮的日线同一条规矩：当晚自己回补，不在界面上念给人听。
**状态**：已实现并实机验收（F1/F2 见 §7，全量回归与实机读数见 §8）。

## 1. 实机读数（2026-11-05，实例 8765）

库里 `index_daily` 按市场分组只有两段（`SELECT market, count(*), max(date) FROM index_daily GROUP BY market`）：

| 市场 | 行数 | 库内最新日 |
| --- | --- | --- |
| A | 有 | 追平到最近交易日 |
| HK | 有 | 追平到最近交易日 |
| **US** | **0**（表里一段都没有，`GROUP BY` 干脆不返回这一行） | — |

而 `jobs` 表里 `indices@US` 每晚报的是 `ok · 3/3 只指数日线`、`rows_affected = 0`。
两头合起来的后果在 `dashboard.py:238`：休市时那一段回落 `latest_with_change(market, index_code)`，
库是空的 → `latest is None` → `continue`，看板的美股大盘条**永久少三只**，
并且没有任何一处说它少了——健康页看见的是「3/3 成功」。

腾讯到底有没有美股指数的日线？有。2026-11-05 实测（`web.ifzq.gtimg.cn/appstock/app/kline/kline`，
与 `KLINE_PATHS["US"]` 同一个端点）：

| 请求的 symbol | 响应节点键 | 结果 |
| --- | --- | --- |
| `us.INX` | `us.INX` | 有 `day` 序列（2026-10-20 收 7712.570） |
| `usINX` | `us.INX` | **同样有序列**，但节点键多了个点 |
| `us.IXIC` / `usIXIC` | `us.IXIC` | 有序列（26424.820） |
| `us.DJI` / `usDJI` | `us.DJI` | 有序列（52495.880） |
| `usSPX` | `usSPX` | `day` 空数组——它认 `INX` 不认 `SPX`，配置默认给的正是 `usINX` |
| `us.DOW` | `us.DOW` | `day` 空数组，`qt` 里回的是陶氏化学 `DOW.N` |

也就是说：`[indices]` 里那三只（`config.py:36-72`：`usINX` / `usIXIC` / `usDJI`）本晚就能取到，
缺的只是适配器的两句代码。

## 2. 根因（三处，都指到行）

### 2.1 市场闸挡在一条不真的前提上

`tencent.py:502-503`：

```python
if market not in ("A", "HK"):
    raise EmptyResponse("tencent", f"腾讯指数日线没有 {market} 市场")
```

docstring 那句「腾讯没有指数的美股那一跳按 `EmptyResponse` 答，让降级链继续问下一个源」（D148 写的）
被上面的实测推翻。而它答的是 `EmptyResponse` 不是故障，所以链把这当成一次正常答复
（`registry.py:466-468` → `empty_everywhere` → `sync.py:113-120` 返回 `written=0` 的空摘要），
一路无异常——这正是 §1 里那句 `3/3` 的来历。

顺带一处自相矛盾：`config.py:40` 的注释早就写着「美股标普用腾讯认识的符号（`usINX`）：
`usSPX` 在腾讯那里查无此行，整条 bar 永远空着」。报价那一条路确实通
（`tencent_quote_symbol` 对指数码原样交回，实测 `q=usINX` 正常回值），
只有日线这一头的市场闸说没有——所以这不是「新能力」，是把一句写错的既有前提改回来。

放开这一道闸之后还有第二道，而且是**该留的**那道：`parse_tencent_daily` 死认请求的那个键
（`tencent.py:147-149`，「不许拿别人的序列」，D272 换来的）。请求 `usINX` 时响应节点写作 `us.INX`，
直接放开市场闸会得到一条 `FieldDrift`。上表给了出路：**用点号形态请求**，请求键与节点键同名，
那道闸一次都不用放宽。

### 2.2 指数用的还是 `_window`（不回头）

`jobs.py:369`：

```python
start = _window(deps.sync.indices.latest_date(market, index_code), ctx.as_of)
```

上一轮的定稿窗口（D300）只给了 `run_bars`。同一条病在指数上照旧：白天手动跑过一次、
或盘后第一趟落到收盘前，那根盘中指数就永久是这一天的「收盘价」，`latest + 1 > as_of` 让今晚一只不问。
指数没有 `updated_at`（`index_daily` 建表就没有这一列），所以库内连证据都不留，但**病根与日线同一处**，
修法也就同一处。

### 2.3 `_collect` 只问「有没有抛错」

`jobs.py:81-99` 的分子 `done` 加一是「这一只没抛 `SamsError`」，与它写回几行无关。
日线那一头为这个单独补了 `barless` 计数并写进 note（D272：「24 只北交所代码就是这么藏了两周」），
`run_indices` 没跟上——于是 0 行的 US 报成 `3/3`。这不是 note 的措辞问题：健康读数、
`run once` 的回执、以后的排障全靠这一句认市场缺了什么。

## 3. 修法

### F1 腾讯的美股指数日线：请求键用点号形态，入库键不动

`tencent.index_daily`：

- 市场闸收到 `("A", "HK", "US")`；别的市场（没有第四个）照旧按 `EmptyResponse` 答。
- 新增一个模块级小函数把「要问腾讯哪一种写法」说清：

```python
def tencent_index_symbol(market: str, code: str) -> str:
    """美股指数的请求键要写成点号形态：`usINX` → `us.INX`（实测 2026-11-05）。
    A/HK 两边请求键与响应节点同形，原样交回。"""
```

- `index_daily` 内 `symbol = tencent_index_symbol(market, code)`，`kline_param` 与
  `parse_tencent_daily` 都用这个 `symbol`——**请求键与节点键同名**，`parse` 那道防串号的闸
  一个字都不放宽；美股段拿到的仍是 `usINX` 这个配置形态（末尾那行
  `bars.with_columns(pl.lit(code).alias("code"))` 原样保留）。
- 入库键必须是配置形态：`[indices]` 配置、`index_daily` 表键、`dashboard.py` 的查询三处同形，
  哪一处换成 `us.INX` 都会让大盘条问不到自己刚写的行。
- docstring 与 `sources.py:679` 那句注册 note（「A/HK 指数日线」）一起改口。

`sync_index`、`registry.fetch_index_daily`、`IndexDailyRepo`、迁移与配置**都不动**：
链上早就有这个能力（`has_index_daily` 探的是方法存在，`markets` 探的是适配器声明的市场，
腾讯两个都满足），缺的只是那一句 `if`。

### F2 指数吃定稿窗口，并把空序列单独说出来

`run_indices`（`jobs.py:359-375`）：

- `_window` → `_settle_window`（D300 那个函数，不新写第二套窗口逻辑）。
- 与 `run_bars` 同一条判据：`written == 0` **且** `summary.latest_date is None` 才计入 `empty_series`
  ——只看今晚行数会把长期停市的指数念成窟窿，而噪音的下场是没人再读这一句。
  `sync_index` 两个分支都回填了 `latest_date`，所以这一头不用自己再查库。
- note 追加一句 `，{N} 只一根 K 线都没有`（与 `run_bars` 那枚 D272 的话术逐字同形，
  同一句病在一个产品里只该有一种说法）。仍不进 `errors`、仍 `ok`：
  空序列是一句要说的**事实**，不是一次失败。
- 不排进 `symbol_backfills`：那张队列的键是 `(market, code)` 的票，指数不是票（D145 分表分键的
  同一个理由）；而 F2 的回退本身就是每晚一次的重试，不需要第二个重试机制。

缺口到此为止都在后台：job note + 健康读数。**不加任何界面文案**，
按「这个不应该跑到用户界面，需要自动处理，如果数据不全，应该通知后台系统自动做」。

## 4. 测试（先红后绿，一条行为一枚）

`tests/datasource/test_tencent.py`：

1. **翻转** `test_tencent_index_daily_us_empty`（:338）。它现在钉的是 §2.1 那句不真的前提，
   留着它就等于把「美股指数取不到」当规格。改名
   `test_tencent_index_daily_us_asks_the_dotted_symbol`：payload 节点键 `us.INX`，
   断言 `get.calls[0][1].startswith("us.INX,day,")` 且 `out["code"].unique() == ["usINX"]`
   （请求点号、入库源形态——这一枚同时钉住两头）。
2. **新增** `test_tencent_index_daily_us_rejects_a_foreign_node`：请求 `usINX` 而响应只给
   `data["us.DJI"]` 时报 `FieldDrift`。第一枚放开市场闸之后，这一枚证明
   `parse_tencent_daily` 的防串号闸在美股段仍然生效（否则「放开」听起来像拆了闸）。

`tests/services/test_scheduler_jobs_data.py`：

3. **改** `test_indices_job_is_incremental`（:654）：库里最新一根 2024-10-24，问出去的起点是
   `date(2024, 10, 24) - timedelta(days=14)`；并加一句 `note == "1/1 只指数日线"`——
   有数据的那一头不许被点名。
4. **改** `test_indices_job_skips_an_index_already_at_as_of`（:663）——这条规格的正是 §2.2 的病，
   换成 `test_an_index_bar_written_today_is_repulled`：seed `AS_OF` 那一根，
   断言 `adapter.calls == [("A", "sh000001", AS_OF - timedelta(days=14), AS_OF)]`。
   与 `run_bars` 那一对（:282 / :301）逐字同形，不另立第三种说法。
5. **新增** `test_indices_note_names_the_indexes_with_no_bars_at_all`：空库、
   一只抛 `EmptyResponse` 的替身 → `outcome.ok`、`errors == ()`，
   且 `note == "1/1 只指数日线，1 只一根 K 线都没有"`。
   这一枚是 §2.3 的正身：`3/3` 不许再把空序列盖过去。

`run_indices` 的 5 枚（含改动）跑完，顺手跑 `tests/services/test_scheduler_jobs_data.py`
整文件与 `tests/storage/test_index_daily.py`、`tests/web/test_indices.py`（读取侧没动，
让它们证明没被碰坏）。最后全量。

## 5. 这一轮不做

- **不给 `index_daily` 加 `updated_at`**：加这一列唯一的用处是让指数也进收盘对账，
  而对账的见证 `snapshot_daily` 只收票码、不收指数码（`spot` 写的是 `universe.codes(market)`），
  指数那一头根本没有「当天收盘快照」可比。没有证据的列不先建。
- **不做指数 ↔ 报价对账、不定指数的权威收盘源**：同上，先有据再谈口径。
- **不动 `[indices]` 默认值**：实测 `usSPX` 是空答复而 `usINX` 有数据，配置给的本来就是对的。
- **不给 US 指数补第二个源**：链上 `yfinance` 没实现 `index_daily`（全仓只有
  `tencent.py:495` 与 `sina.py:252` 两处定义，新浪那一头 `market != "A"` 直接答没有），
  所以放开之后 US 指数是**孤源**。这正是 #365 悬着的「孤源链整夜撞墙」，不在本轮顺手扩——
  但它在 §7 验收里会被看见，记录在这儿是为了别把它当新发现。
- **不改任何 UI 文案**。

## 6. 决策

- **D304 美股指数的请求键写成点号形态（`usINX` → `us.INX`），入库键仍是配置形态。**
  实测的响应节点键带点，而 `parse_tencent_daily` 按请求键取节点是 D272 换来的防串号闸：
  放宽那道闸（传第二个键给 parser）会让「请求 A 拿回 B」在美股段重新可能发生，
  换请求形态则两处都不动。代价是多一个只有美股用得上的小函数。
- **D305 指数日线也吃定稿窗口（`_settle_window`，D300 同一条）。**
  「一根 K 线写进库时值多少，取决于写它的那一刻收盘了没有」这句话对市场指数同样成立。
  每晚每市场 3 次请求，可忽略。
- **D306 `indices` job 把「N 只一根 K 线都没有」写进 note（与 D272 的 barless 句同一条话术）。**
  「没抛错」不等于「有数据」；指数不在 `symbol_backfills` 里，note 就是它唯一的点名处。
  仍算 `ok`——空序列是事实不是失败，进 `errors` 会让健康页把一次取不到数念成一次故障。

## 7. 实现后怎么验（实机）

1. `sams run once --job indices --market US`：`note` 应报 `3/3 只指数日线`、
   `rows_affected > 0`（首次是 730 天窗口那批，每只约 500 根），不再是 0。
   同一周期键已经 `ok` 时这一句会被跳过（见 §8 末尾那条坑），换个 `--as-of` 就是新周期。
2. 库里 `SELECT index_code, count(*), max(date) FROM index_daily WHERE market='US' GROUP BY index_code`
   返回三行，`max(date)` 落在最近一个美股交易日。
3. 空序列那一句要能看见：在 `config/settings.toml` 临时加一段，把 US 换成源不认的码
   （`usSPX`，实测空答复），跑一次再删掉——

   ```toml
   [indices.US]
   codes = ["usSPX"]
   ```

   `sams run once --job indices --market US` 的 note 应含 `1 只一根 K 线都没有`且仍算成功。
4. 休市时段打开看板（`/api/partials/indices`）：美股那一段出现三条指数，
   每条带最近一个交易日的收点与涨跌幅，而不是整段消失。
5. `indices@A` 与 `indices@HK` 的 `rows_affected` 从今晚起非零（定稿窗口在回拉最近 14 天）——
   这是 D305 的预期，不是回归。

## 8. 实现记录与实机验收（2026-11-05 已跑，实例 8765）

| 提交 | 内容 |
| --- | --- |
| `f925a16` | F1：`tencent.index_daily` 放开市场闸 + `tencent_index_symbol` 的点号请求键 |
| `c2a41cb` | F2：`run_indices` 换 `_settle_window`，note 点名空序列；模块 docstring 的窗口规则同步改口 |
| `8819c91` | 补 live 用例 `test_live_tencent_index_daily_covers_all_three_markets`（真接口问三市场的指数） |

测试按 §4 逐枚见过红→绿：F1 那两枚先红在 `EmptyResponse: 腾讯指数日线没有 US 市场`，
F2 的窗口两枚先红在「问出去的区间是 `AS_OF` 而不是 `AS_OF-14`」与「calls 为空」，
点名那枚先红在 `note == "1/1 只指数日线"`。全量 **5583 passed, 20 deselected**（40 分 40 秒，exit 0）。

实机读数（停服 → `run once` → 查库 → 重启）：

```
task_runs（job_name='indices'，按结束时间倒序）
  indices A  2026-11-05  ok  rows=33    3/3 只指数日线
  indices US 2026-11-05  ok  rows=0     1/1 只指数日线，1 只一根 K 线都没有   ← §7.3 的临时配置探针
  indices US 2026-11-04  ok  rows=1503  3/3 只指数日线                      ← F1 之后第一次真取数
  indices HK 2026-11-04  ok  rows=3     3/3 只指数日线
  indices A  2026-11-04  ok  rows=3     3/3 只指数日线
  indices US 2026-11-03  ok  rows=0     3/3 只指数日线                      ← 修好之前那一条静默
```

最后一行与倒数第四行是这一轮的全部内容：同一只 job、同一句 `3/3`、一个写 1503 行一个写 0 行。

- §7.2：`index_daily` 的 US 段现在有 `usINX` / `usIXIC` / `usDJI` 各 **501 行**，
  区间 2024-11-04 → 2026-11-03，`source='tencent'`。
- 数是对的：库内 `usINX` 2026-11-03 收 **7568.92**，而腾讯报价 `q=usINX` 那一条的
  「昨收」字段也是 7568.92（`v_usINX="200~标普500~.INX~7707.44~7568.92~…"`，取数于 11-04 盘中）。
  两个不同的端点、两条不同的解析路径落在同一个数上，这比「看起来有数」强。
- §7.4 只验到一半：验收时美股**正在盘中**，看板走的是实时报价那一条分支，
  三条 bar 都出得来（标普500 7,699.20 +1.72%）。休市回落库内那一条分支
  （`dashboard.py:238` 的 `latest_with_change`）没法当场看见，
  但它读的就是上面那 501 行，而 A 与 HK 两段同一时刻报的正是这条分支的结果（`休市 · 11-04 收盘`）。
- §7.5：`indices@A` 从 3 行变成 **33 行**（3 只 × 最近 14 个日历天里的 11 个交易日）——
  定稿窗口在指数上开始回头，与 D300 在日线上做的是同一件事。

一处坑，记下来免得下次再踩：`run once` 按**周期键**判重，同一只 job 在同一个
`period_key` 上已经 `ok` 就直接跳过（`runner.py:149-151`）。验收时那一晚 US 的周期键已经是
2026-11-03，所以第一次 `run once --job indices --market US` 报的是「周期 2026-11-03 已经成功」，
得用 `--as-of 2026-11-04` 换一个新键才会真跑。这不是 bug——它挡的正是「同一晚把同一段重跑一遍」。

顺带看见（不在本轮，交给 #368/#365）：`run once` 会先把补数队列排一遍，
本次那里撞了 yfinance 的限流（`源 yfinance 连续失败 1999 次…冷却 1800 秒`），
与本轮无关，但那条链上的失败计数怎么涨到 1999 仍未有人查过。
