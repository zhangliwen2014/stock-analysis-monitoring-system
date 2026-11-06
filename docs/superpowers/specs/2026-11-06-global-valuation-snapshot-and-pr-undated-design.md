# 港美股按日估值补一家、PR 读侧认「无披露日」（#387 余波，D331–D333）

**目标**：US 的按日估值不再只有 yfinance 一家（它一 429，整条链就 0 行）；HK 的估值链
多一家备源；`sams screen pr` 与 `sams fundamentals` 不再把 HK/US 的每一期按 D61 挡掉。

**范围**：`eastmoney_global` 新增 `valuation` 能力；`screen.pr_max` 与
`FundamentalService.read` 的 PIT 读取开口；随之要改的文案与迁移 0053。不碰 A 股；
不碰 web 的全市场选股（那条路早已按 D215/D228 开口）。

---

## 1. 问题（2026-11-06 读库 + 实网）

### 1.1 按日估值：US 一行都没有，HK 只有一家

`valuation_daily` 的现状（库副本 `tmp/audit/sams.duckdb`）：

| 市场 | 行数 | 票数 | 源 | 最新一天 |
| --- | --- | --- | --- | --- |
| A | 239,350 | 503 | `akshare` | 2026-11-05 |
| HK | 1,683 | 3 | `baidu_valuation` | 2026-11-05 |
| US | **0** | 0 | — | — |

过去 24 小时的 `task_runs`（同一份库）：

| job × 市场 | 失败次数 | 最后一次 | 失败原文（截断） |
| --- | --- | --- | --- |
| `valuation US` | 106 | 11-06 15:22 | `US AAPL 取不到按日估值 … eastmoney_global: 不提供按日估值 … yfinance: Too Many Requests` |
| `valuation HK` | 70 | 11-06 08:55 | `HK 00700 取不到按日估值 … yfinance: Too Many Requests` |

也就是说：US 的估值链只有 yfinance 一家真的能答（D330 的孤源止损因此一直推着它），
HK 的 3 只票实际只有 `baidu_valuation` 在答。`eastmoney_global` 明明已经用同一个
`push2` 域名在给分钟 K、HK 指数和全市场快照取数，却因为没实现 `valuation` 被链跳过。

### 1.2 PR 读侧把 HK/US 的每一期都按 D61 挡掉

`FinancialRepo.load_as_of` 早有 `include_undated`（D215），web 的 `Screener` 用了它，
但 `ScreenService.pr_max` 与 `FundamentalService.read` 两条**读 PR 的路**一直没传：

* `screen.py:217`（采信期）与 `screen.py:274`（`_newest_periods`）；
* `fundamentals.py:249`（采信期）与 `fundamentals.py:259`（最新一期）。

后果（11-06 实跑）：

* `sams screen pr --market US --pr-max 0.5 --as-of 2026-11-05` → 命中 0，屏幕上
  「5303 只连 PR 都算不出」，每一只的原因都是「N 期的披露日是空的（D61…）」；
* `--market HK` 同样：2803 只，同一句原因。

这不是「市场不便宜」，是读侧把 D215/D228 已经放开的行又挡了回去——同一份库、
同一批行，web 选股页算得出，CLI 算不出。

---

## 2. 决策 D331：东财 push2 的报价快照进港美股估值链

### 2.1 实测字段（2026-11-06）

同一个 `push2.eastmoney.com/api/qt/stock/get`，参数 `fltt=2&invt=2`：

| 票 | secid | f164 PE(TTM) | f167 PB | f116 总市值（本币） | f86 最新行情时间 |
| --- | --- | --- | --- | --- | --- |
| AAPL | `105.AAPL` | 37.84 | 44.62 | 5,001,541,078,940 | 1793912400（11-05 16:00 纽约） |
| 00700 | `116.00700` | 14.30 | 2.92 | 3,817,092,299,214 | 1793952485（11-06 16:08 香港） |
| BRK.B | `106.BRK_B` | 14.28 | 1.64 | 1,225,192,646,445 | 1793912400 |
| SPY | `107.SPY` | `"-"` | `"-"` | `"-"` | 1793912400 |
| 不存在的代码 | `105.ZZZZQQ` | `data:null` | — | — | — |

三条参数上的事实：

* **`invt=2` 必须带**：不带时缺值是 `0.0` 而不是 `"-"`，一个 0 的 PE 会走进 PR 算式；
* **`fltt=2` 必须带**：不带时价格是分（整数），倍数也带缩放；
* **`data:null`** 是「查无此票」的答复（`rc:100`），与 `data` 在但三格全是 `"-"`（SPY，
  一个 ETF 没有 PE/PB）是两件事：前者可继续问，后者也是可继续问，但都不是「0 倍」。

### 2.2 适配器

`EastmoneyGlobalAdapter.valuation(market, code, start, end)`：

1. `market` 只认 HK/US（与本适配器一致），区间反向是 `ValueError`；
2. secid 复用 `_secid_for`：HK `116.<五位>`，US 探 `105/106/107`（结果按符号缓存）；
3. 一次 `fetch_quote(secid)`（新函数，复用 `STOCK_URL`）；
4. `parse_em_valuation`：
   * `data` 缺失/为 null → `EmptyResponse`（`_payload_data`）；
   * `f86` 解析不出 → `FieldDrift`（每一行报价都该有时间，换了形状要出声）；
   * 日期 = `f86` 按 `MARKET_ZONES[market]` 折回本地的**那一天**（港股本币、美股折纽约）；
   * `f164/f167/f116` 各自 `float` 失败（`"-"`、空串）→ NULL；
   * 三个数**全空** → `EmptyResponse`（与 yfinance 同一条：空白页不是「它没有估值」，
     写成 0 行会让链把「没读到」当「查无记录」发确认，D43）；
   * **负 PE/PB 原样保留**（D59 靠它们分叉到 PB 那一支）；
   * `market_cap` 不做单位换算：`caliber_note` 已声明「金额是本币原值」，f116 就是本币；
   * `dividend_yield` 这一路没有，留 NULL（显式给列，避免 `normalize_rows` 的缺列告警）；
   * 出帧后按 `[start, end]` 裁：快照日期在窗口外时给 0 行（是「这段没有」，不是故障）。

### 2.3 链上的位置与后果

* **US**：链从 `yfinance` 孤源变成 `yfinance → eastmoney_global`。yfinance 429 时，
  东财的当日快照能顶上；D330 的孤源止损不再在白天把剩下的票顺延。
* **HK**：链上原本实际只有 `baidu_valuation` 在答，现在多一家东财（优先级 3，
  排在 baidu 的 5 之前）。**日常增量**的一行会先由东财回答；**长窗口回补**由 §3 的
  窗口闸交给 baidu——它是唯一能给三年逐日序列的源。
* 两家都答的日子只留先答那一家：`valuation_daily` 的主键是 `(market, code, date)`，
  `INSERT OR REPLACE` 不会把两家的数混在同一行里。

### 2.4 迁移 0053

`data_sources.note` 里补上「按日估值」，并保留「同源」那句（0052 的测试按它断言）：

```
东方财富港美股（分钟K、HK指数日线、HK上市日期、HK/US基本面与按日估值快照；…同源；…）
```

用旧文案做 `WHERE` 条件（运维手改过的备注不被覆盖），与 0052 同一写法。

---

## 3. 决策 D332：快照源对长窗口说「答不了」，不让 1 行冒充整个窗口

`_fetch_frame` 的语义是「拿到非空帧 = 整个窗口的答案，链停」。一条报价快照只有
一天，所以：

* 窗口 ≤ 7 天（`SNAPSHOT_WINDOW_DAYS`）：正常应答，日期在窗口内就给那一行；
* 窗口 > 7 天：抛 `EmptyResponse`（「快照只能回答一天，答不了这个窗口」），链继续
  问下一个源。

7 天是为了容纳「最新一行 +1 到下一个交易日」的正常补数（周末 3 天、长假后 4–10 天），
不是精确边界。没有这道闸，HK 一次 `--start 2023-01-01` 的回补会被东财用「今天这一行」
答掉，baidu 的三年序列根本不会被问到，而屏幕上看起来是一次成功的同步。

`EmptyResponse` 在 `_call` 里**不记源失败**（registry 对它的处理是「正常应答、
只是这段没有」），所以长窗口回补不会把东财推进 D279 冷却。

---

## 4. 决策 D333：PR 读侧按 D215/D228 采信无披露日的期，并把口径写进输出

### 4.1 唯一名单

`UNDATED_FINANCIAL_MARKETS = frozenset({"HK", "US"})` 从 `services/screener.py`
移到 `core/fundamentals.py`（与 `FISCAL_YEAR_ANNUAL_MARKETS` 并列）。screener 的
`UNDATED_FINANCIAL_NOTES` 留在原处（那段话是屏幕字段的说明，带 PEG 细节）。

### 4.2 读取开口

| 位置 | 改动 |
| --- | --- |
| `ScreenService.pr_max` | 两次 `load_as_of` 都传 `include_undated=market in UNDATED_FINANCIAL_MARKETS` |
| `ScreenService._newest_periods` | 同上 |
| `FundamentalService.read` | 两次 `load_as_of` 同上 |

`include_undated` 不是关掉 PIT：报告期仍不能晚于截至日；同一只票存在带披露日的行时，
带日期的那期优先（见 `load_as_of` 的 docstring）。

### 4.3 文案（这些句子是数据，不是装饰）

* `roe_basis_note(basis, market=None)`：
  * 美股年报口径不再说「12-31 那一报告期」，改成「源标为年报的那一期（美股财年
    止日可以不是 12-31）」——按 `FISCAL_YEAR_ANNUAL_MARKETS` 判断；
  * `market in UNDATED_FINANCIAL_MARKETS` 时追加一句：「该市场的财务源不提供披露日
    （D215/D228）：该期按『报告期 ≤ 截至日』采信，公开时点无从核对」。
  * 默认 `market=None` 时 A 股那句一字不变（现有测试与输出不动）。
* `no_usable_period`：HK/US 不再说「披露日是空的（D61：不知何时公开即不采信）」；
  库里没有期 → 给出同步命令；有期 → 说「都晚于截至日」，下一步是换 `--as-of`。
* `_missing_roe_reason`：两处「披露日为空的行不会被采信」按市场换词。
* `cli/screen.py` 的 `_warnings`：`dropped_unpublished` 在 HK/US 下是「报告期晚于
  截至日的行」（未来函数闸门），不再说成「没有披露日（D61）」。
* `cli/analysis.py` 的同一句同上；`_period_line` 的「无披露日」在 HK/US 下说成
  「源不提供披露日（D215/D228）」；`interim` 警告里的「披露 None」一并修掉。
* `_hit_line(hit, market)`：美股年报那一期不再是「9 月末期」，而是「年报（财年止 09-27）」。

---

## 5. 落点

| 文件 | 改动 |
| --- | --- |
| `src/sams/datasource/eastmoney_global.py` | 模块文档、`fetch_quote`、`_quote_number`、`_quote_day`、`parse_em_valuation`、`valuation()`、`quote_fetch` 注入、`source()` 的 note |
| `src/sams/storage/migrations/0053_*.sql` | `eastmoney_global` 的 note（带旧文案条件） |
| `src/sams/core/fundamentals.py` | `UNDATED_FINANCIAL_MARKETS`、`roe_basis_note(basis, market=None)` |
| `src/sams/services/screener.py` | 改为 import 上面那个集合 |
| `src/sams/services/screen.py` | `include_undated`、`no_usable_period` 分市场 |
| `src/sams/services/fundamentals.py` | `include_undated`、`_missing_roe_reason` 分市场 |
| `src/sams/cli/screen.py` | `roe_basis_note(..., market)`、`_hit_line(hit, market)`、`_warnings` |
| `src/sams/cli/analysis.py` | `roe_basis_note(..., market)`、披露日文案、`dropped_unpublished` 文案 |
| `tests/datasource/test_eastmoney_global.py` | 新能力的解析/适配器用例；`provided_by` 期望值加 `valuation` |
| `tests/services/test_screen_pr.py` / `test_fundamentals_read.py` | HK/US 的 PR 能算出来；原因文案不再提 D61 |
| `tests/cli/test_screen_json_adversarial.py` 等 | `basis_note` / warnings 的市场差别 |

## 6. 验证

1. 单测（红 → 绿）：解析件、适配器窗口/空值/负 PE、`provided_by`、PR 读侧、CLI 文案。
2. `ruff check src tests`，全量 `pytest`（不并发）。
3. 实网：
   * `sams sync valuation --market US --code AAPL --start 2026-11-05 --end 2026-11-06`
     应写入 1 行（AAPL PE 37.84 / PB 44.62，源 `eastmoney_global`）；
   * `sams screen pr --market US --pr-max 0.5 --codes AAPL --as-of 2026-11-05`：
     AAPL 应算得出 PR（约 37.84 ÷ 1.714 ÷ 100 ≈ 0.22）并命中，而不是「算不出」；
   * `sams screen pr --market HK --pr-max 100 --codes 00700 --as-of 2026-11-05`：
     应落在「落选」而不是「排除」；
   * 健康页 / `sams source list` 能看到东财的「按日估值」。
4. 线上 serve 需重启才会加载新方法（能力按方法存在与否判定，注册表在启动时定下）。

## 7. 不在本期范围

* 全市场 US/HK 估值普查：`sync valuation` 仍按票跑，没有 `--all`（D227）。
* yfinance 429 的根因（请求节流/代理）——本期只是让链上有第二个能答的源。
* 港股三年逐日序列的保留：baidu 仍是唯一来源，本期不动它的窗口策略。
* 美股拆股/分红的总回报口径（corp actions US）。
* web 选股页把 PR 做成一个可组合条件（另一份 spec）。
