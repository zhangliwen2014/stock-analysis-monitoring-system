# 归因口径：基准同市场同窗口、占位汇率、台账历史起点 — 设计

**日期**：2026-11-04
**来源**：第三轮专家实机报告 R3-05 / R3-18 / R3-19（`2026-11-03-expert-testing-round3.md`），开放决策 D7
**任务**：#372（T3）
**状态**：设计待批

---

## 1. 三条都在实机上复现了

这一节的每一句都是从当前库（`data/sams.duckdb`，只读）与当前构建的 CLI 里读出来的，不是推测。

**R3-05 —— 全港股的账户对标上证指数。**
`文-中国银河`（`a50aa9f3…`）两只持仓全是 HK（华沿机器人 01021、美团 03690），当前构建跑 `sams attribution` 得到：

```
标的      区间均权  贡献    价格段  换汇段  分红
华沿机器人 HK 01021  29.84%  -2.84%  -2.78%  -0.06%  —
美团－Ｗ   HK 03690  25.18%   1.42%   1.47%  -0.05%  —
区间收益 -1.42%（2026-10-29 ~ 2026-11-04，共 7 天）
基准 上证指数 0.95% · 超额 -2.37%
```

「赚的是判断的钱还是行情的钱」这一格，拿 A 股指数去比一个 HKD 仓位的涨跌，那个 −2.37% 不是超额，是两个市场的 β 之差。

**R3-18 —— 分红账户的净值这条链没走通。**
`验收-S4分红`（`827caebe…`）：建账 `created_at = 2026-11-01 18:51`，但里面有 2026-10-20 买入的 601088（100 股）；`corp_actions` 里 601088 有 2026-10-26 派 0.98/股。库里现在的事实是：

- `sim_cash_moves` 里这个账户只有一笔 dividend：`A:301536:2026-11-02`（60 CNY），**601088 那笔 10-26 的 98 CNY 没有入账**；
- `nav_daily` 只有 11-01 ~ 11-04 四天，10-20 ~ 10-31 整段没有行。

于是「分红进净值」这条链在它身上从来没有被真实数据走过一遍：派息没入账，入不了账的那几天也没净值行，归因表的分红列自然全是「—」。

**R3-19 —— 结算汇率录成 1.0000。**
同一个银河账户里有两笔 `fx_rate_ref = 1.0`（03690 于 09-10、01021 于 10-29），报告币种是 CNY、标的币种是 HKD。持仓卡现在按 D212 在那一行写：「…若当时录错，撤销那笔买入并按实际汇率重记」——把系统的历史脏数据变成用户的手工活。

## 2. 根因（四处，都指到行）

| # | 位置 | 问题 |
|---|---|---|
| 1 | `services/attribution.py:214` `self._benchmark(account.home_market, run)` | 基准市场取自**账户的主市场**，而 `sim_accounts.home_market` 说的是「这个账户主要在哪开户」。全 HK 持仓的账户 home_market 仍是 A ⇒ 对标上证指数。持仓自己在 `entries` 里已经带 `market`（:196-210），却没有参与选基准 |
| 2 | `services/attribution.py:266-281` | 基准覆盖不全时只是「注明并列」：`missing` 与首尾日不齐都只进 `note`，`value` 照给、超额照算。而 `missing` 是靠 `trading_days_between(calendar, …)` **估**出来的——这里的日历是 `WeekdayCalendar`，`known_status` 恒返回 `None`，估的是工作日，不是交易日历 |
| 3 | `services/dividends.py:91-93` | 入账资格先看 `opened_on = account.opened_at.date()`，而 `opened_at = created_at`（`storage/repos/ledger.py:87`）= **建账那一刻**。把真实券商历史回填进一个新账户（先建账、再补记去年和前天的成交）是本仓库支持的用法，于是除权日早于建账日的派息被整条 `continue` 掉。真正的判据本来就有：`_held_before(held, ex_date)`（:98、:159-165）算的是「除权日前累计持股」 |
| 4 | `services/nav.py:116` `start = max(start, self._opened_on(account.opened_at, …))` | 同一个 `created_at` 又当了一次净值起点：早于建账日的成交永远进不了曲线。这条与 #3 是同一个错误的两个出口 |
| — | `storage/repos/ledger.py:548-556` | 占位汇率 1.0 在**写入口已经拦住了**（D213，2026-10-29 落地；`sim_orders` 只有这一处 INSERT，网页与 CLI 都走它，`web/pages/portfolio.py:686-689` 会把这句 `SamsError` 以 400 留在表单上）。所以 R3-19 剩下的不是「再拦一遍」，而是**库里已有的那两行** |

## 3. 修法

### F1 基准：这一段持仓说了算（R3-05 / D7）

`_benchmark` 的入参从 `account.home_market` 换成**这一段的持仓市场**，并在覆盖不齐时不给超额。

- 市场来自 `entries`：`used = {row.market for row in entries if row.weight > 0}`（`AttributionService.evaluate` 里 :196-214 已经算好，直接传）。
  - 恰好一个市场 → 该市场配置的第一个指数（沿用 D268，`settings.indices.for_market(market)[0]`）。
  - 多于一个市场 → `value=None`，`note` 说明「这一段持仓跨 A、HK 两个市场，没有哪个单一指数配当这段的基准，只出绝对收益」。
  - 一个都没有（纯现金）→ `value=None`，「这一段没有持仓，超额不评」。
- 窗口用净值日自己判，不再拿工作日去估：把 `closes` 收成 `{day: close}`，`missing = [d for d in run if d 不在 closes]`。
  - `missing` 非空 → `value=None`，`note` 点名缺哪几天（最多列三天，后面「等 N 天」），并说「只出绝对收益」。
  - 覆盖齐了才算 `last_close / first_close - 1`。
- 顺带把只服务这一估算法的 `calendar` 依赖摘掉：`trading_days_between` 在 attribution 里若只剩这一处用途（现确认为 :268 一处），就把构造参数与 `WeekdayCalendar` 的实例化一起去掉，`bootstrap` 与测试同步改。留着一条只会给出「估的」缺天数的依赖，比删掉它更贵。
- 卡片与 CLI：`Benchmark.value is None` 那一路已经渲染成「—」，本轮要保证**基准与超额一起变成「—」**（`excess` 已由 :228 跟着 `value`），并把那句 `note` 紧跟在基准格后面。

`home_market` 不参与选基准，但它仍然是净值与分红算本地日的时区来源（`nav.py:_opened_on`、`dividends.py:90`）——本轮不改它这个用途。

### F2 占位汇率：系统自己按当日汇率回算（R3-19）

写入口不动（已拦）。存量按 D283 那条「数据不全系统自动补」的同一口径处理，不再让用户撤销重录。

新增 `LedgerService.repair_placeholder_rates(account_id, as_of)`：

1. 取该账户 `fx_rate_ref = 1.0` 且 `currency != account.report_currency` 的成交（这个组合在今天的写入口已经不可能再产生，所以匹配到的必然是占位值）。
2. 按成交的市场本地日（`order.ts` 落到该市场时区）从 `fx_rates` 取 `currency → report_currency` 当日价；取不到当日就退到该日之前最近的一条，超过表里那套 staleness 判据就**不修**，留给下一晚。
3. 取到 → 改写 `fx_rate_ref` 与 `fx_rate_applied` 为新值，并按「原结算额 − 应结算额」补一条现金流水：`kind="adjust"`、`actor_id="system"`、`ts` = 成交当日 15:00（市场本地），`reason` 完整到能独立复核：
   `回算 <day> 那笔 HK 01021 买入的结算汇率：1.0000 → 0.9217，<CNY> 口袋补 −1,234.56`。
   **补流水而不是改历史那行**：现金余额是重算出来的，改历史行会把「谁在什么时候动过钱」这条审计线抹掉。
4. 幂等：改写之后不再匹配条件（`fx_rate_ref != 1.0`），重跑返回 0 笔。
5. `CASH_KINDS`（`storage/repos/ledger.py:37`）加 `"adjust"`，且它不带 `ref`（`_REF_KINDS` 不动）；`_REF_KINDS` 那句「只有可幂等的自动入账能带 ref」继续成立——repair 靠的是汇率本身变了，不靠 ref 去重。
   TWR 这边不用改：`nav.py:44` 的 `_EXTERNAL_KINDS` 是白名单 `{deposit, withdraw}`，新分类落在名单外就是内部变动，不会被当成一笔外部流入而把 TWR 算歪。

挂载：`services/scheduler/jobs.py` 的 DAILY 序列里，紧挨 `run_dividends`（:384）之后、`run_nav`（:419）之前——两者都是「按账户自动动现金」的夜间趟，而净值必须在修完之后再算，否则那晚的曲线还带着占位汇率。

持仓卡那句话（`services/portfolio.py:256-267`）：回算成功后 `rate_base != 1.0`，这个分支自然不再触发，无需新文案；本轮只把它的措辞改成陈述句，把「撤销那笔买入并按实际汇率重记」这个手工指令删掉（与 #370 同一类话术，这里是第三处），换成「系统会在汇率补齐后自动回算」。

### F3 台账历史起点：资格看成交，不看建账（R3-18）

两处都改判据，两处都不引入新配置。

- `services/dividends.py:91-93`：**删掉账户级日期闸**。`row["ex_date"] < opened_on` 这一半去掉，只留 `kind != dividend`。除权日前有没有股，交给 `_held_before`（:98）判：`qty <= 0 → skipped_no_shares`。这句话本来就是「除权日当天才买进来的股不享有这次派息」——模块 docstring 第 8-9 行写的就是这个判据，账户 created_at 从来不是它的依据。
- `services/nav.py:116`：起点钳制从 `account.opened_at` 改成**这个账户最早的一条台账流水/成交的市场本地日**：`ledger_start = min(最早 order.ts, 最早 cash move 的 ts)`（没有流水就没有净值，`min` 空集时退回 `opened_at`）。`start = max(start, min(opened_on, ledger_start))`。
  - 这会让「先买后入金」的账户在曲线头部出现现金为负的几天。那是台账真实的样子，不修，也不藏：这一轮的口径是「算不出来要说不通，算得出来就照实画」。
- 归因随之自动拿到分红列：`_dividends_by_day`（`attribution.py:311-330`）读的就是这些 `kind="dividend"` 流水，10-26 那笔进了净值窗口才会出现在分红列，窗口起点仍是 `run[0]` 不含起点的规则（:189-194）不改。

### F4 存量数据：跑一遍，把话说完

实现落地后在实机跑（不是让用户跑）：

1. `run_dividends` 那趟（或直接 `sams portfolio dividends`，实机核对用）→ 601088 的 98 CNY 入 `sim_cash_moves`；
2. `nav rebuild` 起点提前到 10-20 → 10-20 ~ 11-04 连续行；
3. `sams attribution --account 827caebe…` → 分红列出现 601088 那一笔；
4. `repair_placeholder_rates` → 银河账户两笔回算、CNY 口袋补差、持仓卡那句手工指令不再出现；
5. 全港股账户的归因 → 基准变成恒生系列，或（若缺天）基准与超额一起是「—」。

## 4. 测试（先红后绿，一条行为一个）

服务层

1. 全 HK 持仓 + home_market=A ⇒ 基准取 HK 的配置指数，`Benchmark.market == "HK"`（钉住 R3-05 那笔）。
2. 持仓跨 A 与 HK ⇒ `value is None`、`excess is None`，`note` 里有「只出绝对收益」。
3. 基准缺窗口中间一天 ⇒ `value is None`（不再「按现有的 M 根连乘」），`note` 点名缺的那天。
4. 基准首/尾日与 `run[0]/run[-1]` 不齐 ⇒ 同样不给超额。
5. 这一段没有持仓 ⇒ 不给基准，note 说没有持仓。
6. 除权日早于建账日、但晚于该标的第一笔成交 ⇒ 派息入账（今天这条会被 `continue`）。
7. 除权日之前一股没买（成交在除权日之后）⇒ 不入账，`skipped_no_shares` 计数（钉住「删日期闸不等于来者不拒」）。
8. `nav` 起点：最早成交早于 `created_at` ⇒ 曲线从那天起有行；没有任何流水 ⇒ 仍从 `opened_on` 起。
9. `repair_placeholder_rates`：有当日汇率 ⇒ 两列改写 + 一条 `adjust` 流水 + CNY 余额补差正确；重跑返回 0 笔。
10. 汇率表里那天没有 ⇒ 一行都不改、一条流水都不写（不许猜汇率）。

网页 / CLI

11. `/ledger/fill` 用 `fx_rate=1.0` 记一笔 HK 买入 ⇒ 400，且表单上出现「结算汇率不会是 1.0000」那句（今天只有存储层的测试钉着，HTTP 这层没人守）。
12. 持仓卡那句话不再出现「撤销那笔买入并按实际汇率重记」。
13. 归因卡片：跨市场时基准与超额两格都是「—」，note 跟在后头（`tests/web/test_portfolio_attribution_web.py`）。
14. CLI 归因输出的基准行与市场分组一致（`tests/cli/test_attribution_cli.py`）。

实机（F4）：跑完那四步，把数抄回本节。

## 5. 这一轮不做

- **谁是权威收盘源**（报价↔日线对账）→ #373。F1 只保证「基准缺天就不给超额」，缺天本身由 #375 那趟当晚补齐去消灭；这两条一起看才收敛，本轮先把不收敛时说谎的那半堵住。
- **红利税建模**：继续 `TAX_NOTE`（税前毛额），A 股按持有期补扣不在这轮。
- **给负现金的账户自动补期初余额**：F3 会让它露出来，但那是台账录入问题，不由系统凭空造一笔存入。
- **多基准 / 加权基准**（例如 A 70% + HK 30%）：跨市场时只出绝对收益，不做合成基准。

## 6. 决策

- **D291** 归因的基准市场由**这一段实际持有的市场**决定，不由 `home_market` 决定。`home_market` 说的是开户主场，基准要的是「这几天的钱压在哪个市场」。
- **D292** 基准必须覆盖这一段的**每一个净值日**才出超额；缺一天就整段不给，只出绝对收益。「已注明但并列」不接受——一句 note 不能把两个市场的 β 之差洗成超额。缺天数以净值日自己为参照来数，不再用工作日估交易日历。
- **D293** 存量占位汇率（`fx_rate_ref = 1.0` 且币种 ≠ 报告币种）由后台按成交日的汇率表回算，改写汇率两列并**补一条 `adjust` 现金流水**（不改写历史流水），取不到汇率就不动；屏幕上不再出现「撤销重录」这类手工指令。
- **D294** 分红资格与净值起点的判据是**台账本身**（最早的成交/流水、除权日前的持股），不是 `sim_accounts.created_at`。把真实券商历史回填进新建的账户是支持的用法，`created_at` 不是这段历史的起点。
- **D295** 允许曲线头部出现负现金（先买后入金的真实样子），不自动补期初余额、也不隐藏那几天。
