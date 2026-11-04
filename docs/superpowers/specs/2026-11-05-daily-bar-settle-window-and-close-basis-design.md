# 日线定稿：盘中写下的那一根永远是盘中价 — 设计

**日期**：2026-11-05
**来源**：第三轮专家实机报告 R3-06 / R3-20（`2026-11-03-expert-testing-round3.md`），开放决策 D8「权威收盘价」
**任务**：#373（T4）
**状态**：已实现（F1/F2/F3 见 §7），实机验收未跑（§8）

---

## 1. 实机读数：那一天两个收盘价，差的不是两个源

R3-06 报的是「同一只票同一天有两个收盘」：`/symbol/HK/00700` 头部现价 **383.60**，
`/api/symbol/HK/00700/bars` 的 `close` 是 **388.069289953836**，差 1.16%。

先把库里实际存的数取出来（`data/sams.duckdb`，只读，2026-11-05）：

```
bars_daily  HK 00700  2026-10-30  close=383.6  source=tencent
quotes_snapshot HK 00700 当日最后一笔  price=383.60
```

`388.069289953836` 在全库 `bars_daily` 里**不存在**。它是读取层算出来的：
`web/pages/symbol.py:399-400` 把 `close` 这个键填的是 `_column(frame, "close_hfq")`，
而 `close_hfq = close × adj_factor`（`analysis/adjust.py:163-168`，累计因子只乘区间内的除权日）。
所以那两个数不是「报价源 vs 日线源」，是**同一根日线的交易所收盘 vs 后复权收盘**，
而接口把后者叫作 `close`（payload 里没有任何字段说这根线是什么基）。

这条先记下，因为它决定了 T4 该修什么：源之间其实很一致——

| 对照 | 配对数 | 差 >0.1% | 差 >0.5% | 最大差 |
| --- | --- | --- | --- | --- |
| A：当日**收盘后**写入的 bar ↔ 当日 `snapshot_daily.price` | 1689 | **0** | **0** | 0.00% |
| A：当日**盘中**写入的 bar ↔ 当日 `snapshot_daily.price` | 205 | **179** | **95** | **3.75%** |

（`snapshot_daily` 是每晚 `spot` job 拉的全市场收盘快照，A 股那一趟在本地 17:00–20:00 之间取，
`day` 与 `taken_at` 的日期逐日核对过，不存在「拿今天的价记成昨天」。它和日线同源同基，都是不复权价。）

盘中写的那 205 行全落在 **2026-10-29** 这一天，例子：

```
600018  2026-10-29  bars.close=5.38 (written 10-29 12:55)   snapshot=5.48 (+0.92%)
600487  2026-10-29  bars.close=46.13 (written 10-29 13:07)  snapshot=44.60 (−7.98%)
300199  2026-10-29  bars.close=23.18 (written 10-29 13:03)  snapshot=22.31 (−3.67%)
```

## 2. 根因（三处，都指到行）

1. **`_window` 只往前走，从不回头**（`services/scheduler/jobs.py:52-56`）：
   `start = latest + 1`，`latest >= as_of` 时返回 None。日线 job 每晚按它取增量窗口
   （`jobs.py:99`）。于是白天任何一次采集（人手动 `sams run once --job bars`、或白天的补采队列）
   把「当日那根还没走完的 K 线」写进 `bars_daily` 之后，**当晚那一趟看见 `latest == as_of` 就直接跳过**，
   这根盘中价从此永远是该日的「收盘价」。
2. **库里没有「这根定稿了没有」这一格**：`bars_daily` 有 `source` 与 `updated_at`
   （`0001_init.sql`，`updated_at` 默认 `now()`，`INSERT OR REPLACE` 每次刷新），
   写入时刻在库，只是没人读过。判据现成：`updated_at` 早于该市场当天的盘后时点
   （`CalendarView.post_close_run_at`，`scheduler/due.py:59-61` 已经在算它）就是没定稿。
3. **接口把后复权叫 `close`**（`web/pages/symbol.py:399-400`）：图上画后复权是既有的产品决定
   （`analysis/adjust.py` 的头、`symbol.py:300-305` 的图注「后复权 · 均线算在同一基上」），
   问题在**键名不说自己是谁**。R3-20 报的「告警正文 11.59 不复权、点进去的图同日 13.08 后复权」
   是同一条：两端都写「收盘价」，核对要人心里自己换基。

顺带查出、**这一轮不修**的一条：`bars_daily` 里 HK 有 101/4099、US 有 23/1006 行的收盘角落在
交易所最小变动价位之外（HK 0.50–9.99 档的档位是 0.010，库里却有 9.765、6.095 这样三位小数）。
A 股 25.5 万行里 0 行有这种尾巴。这是「源给的序列本身带不带复权」的问题，要拿第二个源逐只核，
另开任务，不与「盘中写入」混在同一个改动里。

## 3. 修法

### F1 每晚把最近的日子重拉一遍（定稿窗口）

`run_bars` 的窗口起点从 `latest + 1` 改成 **`latest − SETTLE_DAYS`（日历天）再钳到不超过 `as_of`**，
`SETTLE_DAYS = 14`（约十个交易日）。

- 代价几乎为零：`fetch_daily(market, code, start, end)` 是**每票一次请求**，不是每天一次；
  区间多带 14 行，请求数不变。
- 为什么按日历天而不是交易日：长假（春节七天）之后要覆盖的还是那几天，交易日数会把它们漏在外面。
- 为什么写「钳到 `as_of`」而不是让 `start` 越过 `as_of`：追平的那几天本来就没东西可拉。
- 只有 `bars` 用这个窗口。`valuation/flow/actions/indices/fx` 各自的 `_window` 不动——
  它们不是「同一天的数会随时间变成另一个数」的那一类。

这条自己就把 10-29 那 205 行修掉了：下一晚跑到时，`latest=11-04`，起点回退到 10-21，
10-29 被重新写一次，写的就是真正的收盘。

### F2 拿收盘快照当见证，对不上就自动重采并计数

新 job `close-check`（每市场、`DAILY`，排在 `spot` 之后、`screener` 之前）：

- 比当天有快照的票：`abs(bars.close − snapshot.price) / bars.close > 0.001` 就是不平；
- 不平的票**就地登记一次日线重采**（复用 `symbol_backfills` 那条队列，下一晚由 `bars` 消费），
  job 的 `note` 点名前几只，`rows_affected` 是不平的只数；
- 屏幕上不出现「请你重采」这类话：这一趟只做「查出来 + 自己修 + 留一行读数」
  （用户定的原则：数据不全由后台自动处理，不跑到界面上）。

F1 已经把最近 14 天的错盖掉，F2 要抓的是 F1 盖不到的那一类：源本身给的数不是交易所收盘
（§2 那条尾巴），以及 `snapshot_daily` 有而 `bars` 缺的那天。

### F3 接口说清自己画的是哪根线

`/api/symbol/{market}/{code}/bars` 的载荷里：

- `close/open/high/low` 放**交易所原值**（库里那一根），
- `close_hfq/open_hfq/...` 放后复权那一根，图上仍画它（既有产品决定不变），
- 新增一格 `basis`（`"hfq"` / `"raw"`）写清这一屏画的是哪根，客户端与测试都读它。

前端 `static/app.js:51` 现在把 `payload.close` 直接喂给蜡烛，改读 `*_hfq`；
个股页头部那句「现价 383.60」与图上的 388.07 从此各自有名有姓，R3-20 那条「核对要心算换基」跟着消。

## 4. 测试（先红后绿，一条行为一个）

1. `tests/services/test_scheduler_jobs_data.py`：一只票库里已有 `as_of` 当天那根（盘中写的），
   今晚 `run_bars` 仍要请求区间 `[as_of-14, as_of]` 而不是跳过——断言重拉的票数为 1（今天会红：`_window` 返回 None）。
2. 同文件：`valuation`/`flow` 的窗口不变（钉住「只改 bars 这一条」）。
3. `tests/storage/test_bars_symbols.py`：`upsert` 覆盖同一键时 `updated_at` 被刷新——重拉能盖掉旧值的前提。
4. `tests/services/test_scheduler_close_check.py`（新）：给一天的 `bars` 与 `snapshot_daily` 造出
   0.5% 的不平，`run_close_check` 报出不平数、并往 `symbol_backfills` 登记那只票；
   两边一致时 `note` 报「0 只不平」且不登记任何东西。
5. 同文件：容差判的是相对差，绝对差不平 0.01 元的低价票（5.38 vs 5.48）与高价票同权。
6. `tests/web/test_symbol.py`：造一次除息（裸价 39.90、后复权 42.00），`/api/symbol/A/600519/bars`
   的 `close` 是库里那一根，`close_hfq` 是乘过因子的另一根，`basis` 说 `"hfq"`
   （今天会红：`close` 现在装的是 hfq）。
7. `tests/web/test_symbol_minute.py`：分钟那一支的 `basis` 是 `"raw"`（`services/minute.py:301` 恒为 `adj_factor=1`）。

## 5. 这一轮不做

- §2 末尾那条「源给的收盘不在最小变动价位上」——另开任务，要第二个源逐只核。
- 全历史重拉：`SETTLE_DAYS` 只回退 14 天。10-29 那批能被 F1 自己盖掉，更早的同类错由 F2 逐晚点名。
- 盘中禁止写当日 bar（在写入处判「今天还没收盘就别存」）：那会让白天的手动采集与补采队列整体失效，
  而「当日这根本来就是暂定的」这件事，靠每晚重拉一次就够了。
- 估值/资金流/公司行为的同类定稿问题：它们的数不随「当天还剩几小时」变化。

## 6. 决策

- **D300** 日线每晚把最近 14 个日历天重拉一遍（定稿窗口），判据是「写没写过」而不是「库里有没有」。
  一根 bar 的真值取决于它写入的时刻：盘中写的是盘中价，收盘后写的才是收盘价（§1 那张表：
  收盘后写入的 1689 行与收盘快照 0 行不符，盘中写入的 205 行里 95 行差过 0.5%）。
- **D301** 当天开市市场的全市场收盘快照（`snapshot_daily`）是当日收盘的**见证**；
  bar 与它对不上（相对差 >0.1%）即自动登记一次重采，健康读数里留一个不平只数，界面不出现「请你重采」。
- **D302** `/api/symbol/.../bars` 里 `close` 这个键只放交易所原值；后复权那一根叫 `close_hfq`，
  另给一格 `basis` 说清图上画的是哪根。图仍画后复权（既有决定），但**不再用同一个名字装两根线**。
- **D303** `bars_daily` 里角落在最小变动价位之外的收盘（HK 101 行、US 23 行）本轮不判定、不修改，
  只作为下一任务的输入：那要么是最小变动价位表用错了档，要么是源给的就是复权价。

## 7. 实现记录

四条改动按 F1 → F2 → F3 顺序落地，各自先红后绿（唯一例外见 §7.4 末段）。

| 提交 | 内容 |
| --- | --- |
| `992c6df` | F1 窄版：`run_bars` 的起点从 `latest + 1` 改成「`latest` 当天也重拉」 |
| `7cf0064` | F1 宽版：按本设计回退 `SETTLE_DAYS = 14` 个日历天，把自己写坏的那批历史一并盖回去 |
| `bf62b5a` | F2：`close-check` job + `CloseAudit`/`close_mismatches` 两层 + `--job` 帮助补上这一档 |
| `a9f9d7a` | F3：`bars` 接口分开两基，前端按 `basis` 画那一族 |

### 7.1 F1 落在 `_settle_window`

`services/scheduler/jobs.py` 新增 `_settle_window(latest, as_of)`：`latest is None` 时给
`as_of − INITIAL_SYNC_DAYS`，否则 `latest − SETTLE_DAYS`，仍用「起点越过 `as_of` 就返回 None」
那条钳制（手动 `--as-of` 回溯到历史某天时不会一路拉到今天）。`run_bars` 是唯一换用它的调用方，
`_window` 原封不动留给 `valuation/flow/indices/fx`，那四条各自的增量窗口钉在原有测试里。

为什么不选窄版：重拉不按天花请求数（`fetch_daily` 是每票一次请求），`upsert` 幂等，
而宽版顺带把 §1 那 205 行 poison 自我修复掉——不需要一条手工修数命令。

### 7.2 F2 的实际形状

- `storage/repos/snapshots.py`：`close_mismatches(market, day, *, tolerance)` 与 `CloseMismatch(code, stored, witness)`，
  按相对差倒序返回，SQL 里带上 `adjust = "raw"` 与 `close > 0`。
- `services/market_snapshot.py`：`audit_closes(market, day, *, tolerance) -> CloseAudit`；
  `latest_until(market, day) != day` 时返回 `witness_day=None`，也就是「这一晚没有见证」。
- `services/scheduler/jobs.py`：`run_close_check` + `CLOSE_TOLERANCE = 0.001`，注册在 `spot` 之后、
  `screener` 之前。登记的是 `BARS` 那一档，消费者按 730 天回拉（`services/scheduler/backfill.py`），
  所以 F2 修的范围比 F1 的 14 天宽——14 天之外的同类错也走这条出路。

两处比设计更窄，都写进测试：

1. **快照有、`bars` 没有的那只票不算不平**。停牌是最常见的解释，把整只停牌票推进队列会让真正的
   欠账吃不到重试预算（`test_a_code_missing_from_bars_is_not_a_mismatch`）。
2. **没有见证源或不带补数队列的部署不登记 `close-check`**（两个 `test_build_jobs_...` 用例）。
   查出来却没地方排重采，那一趟只剩一句「有 N 只不平」，等于把问题念给人听。

界面一句话都没有新增，只有 job 的 `note`：`"A 2026-11-05 与收盘快照比对：3 只不平，已排重采：…"`。

### 7.3 F3 的两基

`/api/symbol/{market}/{code}/bars` 现在给 `open/high/low/close`（库里那一根，交易所原值）与
`open_hfq/high_hfq/low_hfq/close_hfq`（乘过累计因子的那一根），另加 `basis`。
日线那一支 `basis="hfq"`，分钟那一支 `basis="raw"`（D164 因子恒为 1，两族逐字相等，
`close == close_hfq` 这条旧钉仍在）。`_empty_payload` 同步补键，空态与新形状同形。
`static/app.js` 新增 `candles(payload)`：按 `basis` 挑出蜡烛与量柱上色读的那一族，
`candlestick()`、`paneSeries()` 的量柱判色、以及「有没有蜡烛」那道判据都改读它。

### 7.4 测试

设计 §4 的七条都在，两处与原文不同：

- §4.3 那条（`upsert` 刷新 `updated_at`）是**绿了以后补的钉**，不是先红：文件是
  `tests/storage/test_bars_symbols.py::test_the_settle_repull_stamps_the_row_it_overwrites`
  （§4 原文写的 `tests/storage/test_bars.py` 不存在，日线存储测试一向在这份文件里）。
  F1 的最终实现不读 `updated_at`，这条钉的是「重拉确实覆盖而不是留下旧值」这个前提。
- §4.1 拆成两条：`test_the_nightly_bars_window_reopens_the_settle_margin`（昨日已追平也要回拉 14 天）
  与 `test_a_bar_already_written_today_is_repulled`（`latest == as_of` 时不再跳过）。

新增/改写的用例：

| 文件 | 内容 |
| --- | --- |
| `tests/services/test_scheduler_jobs_data.py` | F1 两条窗口断言 |
| `tests/test_scheduler_acceptance.py` | 验收跑的 `bars@A` 从 2 行改钉 12 行（定稿窗口内的工作日全重写） |
| `tests/services/test_scheduler_close_check.py`（新，7 条） | 登记、相符、相对容差、无见证、缺 bar 不算不平、注册顺序、两种不登记 |
| `tests/storage/test_bars_symbols.py` | `updated_at` 随覆盖刷新 |
| `tests/web/test_symbol.py` | `basis == "hfq"`、`close` 是不复权那一根、四根蜡烛价同基自洽 |
| `tests/web/test_symbol_minute.py` | `basis == "raw"` |

### 7.5 库里那 205 行怎么修好

不需要命令。下一晚 `bars@A` 跑到时 `latest` 已在 11-04，起点回退到 10-21，10-29 被重拉并覆盖成
真正的收盘价；`close-check` 从当晚起每天检查一遍，把 F1 盖不到的（源本身给的数不对）逐晚点名并排 730 天重采。
实机验证按 §8 走。

## 8. 实机验收（待跑）

1. `sams run jobs` 列出 `close-check@A/HK/US`，档位 `DAILY`，排在 `spot` 与 `screener` 之间。
2. `sams run once --job close-check --market A --as-of <最近一个有快照的交易日>`，
   看 `note` 的不平只数是否落在预期（收盘后写入的那天应为 0）。
3. 重启后连跑两晚，确认 `bars` 的 `rows_affected` 是定稿窗口那批而不是 0。
4. 打开 `/symbol/A/600519`：图上最后一根的收盘与 `bars` 的 `close_hfq` 一致，
   头部现价与 `close`（交易所原值）一致。

