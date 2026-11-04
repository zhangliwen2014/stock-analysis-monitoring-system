# 归因口径：基准同市场同窗口、占位汇率、台账历史起点 — 设计

**日期**：2026-11-04
**来源**：第三轮专家实机报告 R3-05 / R3-18 / R3-19（`2026-11-03-expert-testing-round3.md`），开放决策 D7
**任务**：#372（T3）
**状态**：已实现（F4 实机数见 §7）

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
- **D296** 「全部」窗口的起点是**台账最早那一笔**（成交或流水），不是 `sim_accounts.created_at`。D294 说的是同一件事：`created_at` 记的是按下建账按钮的时刻，不是这段历史的开头；卡片与命令行两处各钳一次，就会出现一个说「全部」、另一个少一段。起点由 `NavService.curve_start` 一处给。

## 7. 实现记录

四个改动按 F1 → F3 → F2 的顺序落地（F2 依赖 F3 把「回填历史」这条路走通，否则回算
发生在一条本来就不完整的曲线上）。

| 改动 | 落点 | 提交 |
| --- | --- | --- |
| F1 基准跟持仓市场走、缺任一净值日就不给超额 | `services/attribution.py:_benchmark`、`bootstrap.py`（不再传 `calendar`） | `940d7d1` |
| F3a 分红资格看除权日前的持股 | `services/dividends.py`（删账户级 `opened_on` 日期闸） | `b93ba51` |
| F3b 净值起点 = min(建账日, 最早流水, 最早成交) | `services/nav.py:_curve_start` | `b93ba51` |
| F2 占位汇率后台按成交日回算 | `storage/repos/ledger.py:repave_rate` + `settled_amount`、`services/ledger.py:repair_placeholder_rates`、`services/scheduler/jobs.py:run_rate_repairs` | `bac1928` |
| D212 持仓卡话术改为「系统自动回算」 | `services/portfolio.py` | `bac1928` |
| §4-13/14 介质层基准行 | `tests/web/test_portfolio_attribution_web.py`、`tests/cli/test_attribution_cli.py` | `0235b0f` |
| D296 「全部」窗口从台账最早一笔起 | `services/nav.py:curve_start`、`cli/attribution.py`、`web/pages/portfolio.py`（两处各钳一次 `created_at` 的重复消掉） | 本轮 |

实现里定下来的三件事，写在这里而不是只留在代码里：

- **回算写新流水，不改历史流水**：`repave_rate` 在同一事务里改 `sim_orders` 的两列汇率，
  并补一条 `kind = "adjust"` 的现金移动。`adjust` 不进 `nav.py` 的 `_EXTERNAL_KINDS`
  （那里是 `deposit`/`withdraw` 白名单），所以 TWR 不会被这笔内部移动扭曲。
- **两种情况整笔跳过**：成交日往前找不到汇率（不许猜数字）、或补差会让账户透支
  （半笔修复比不修更糟）。跳过时返回的计数如实反映没动的笔数。
- **`SYSTEM_ACTOR` 挪到 `services/actors.py`**：分红入账与占位汇率回算都是「系统替谁动的账」，
  这个判断只该有一份。

§4 第 11 项不用新写用例：`tests/web/test_portfolio_web.py::test_a_cross_currency_rate_of_one_is_refused_with_the_real_rate_hint`
早就同时断言了 400、「不会是 1」那句话和不落单，HTTP 这一层并不是没人守。

### 实机（F4）

在 `data/sams.duckdb`（当前库）上跑的，命令与读数：

1. `sams run jobs` → 注册 60 个 job，三个市场都是 `dividends → rate-repair → nav` 这个顺序。
2. `sams portfolio dividends --account 827caebe… --as-of 2026-11-04`
   → `rows=1`：601088 除权日 2026-10-26、持股 100 股、每股 0.98、入账 **98.00 CNY**（税前毛额）；
   `skipped_no_shares=23`、`skipped_existing=1`——删掉日期闸之后「除权日前一股没买」仍然是不入账的。
3. `sams nav rebuild --account 827caebe… --days 400`
   → 写入 **16 天**（此前是 4 天），曲线从 **2026-10-20**（最早一笔成交）起，而不是 11-01（建账那天）。
   头部那几天 `cash = -3,800`、10-26 起 `-3,702`（98 的派息落在除权日）：先买后入金的样子照印，不补期初余额（D295）。
4. `sams run once --job rate-repair` → `rate-repair@HK ok：6 个账户回算 2 笔占位汇率`（A/US 各 0 笔）。
   银河账户（`a50aa9f3…`）两笔：
   * 03690 买入 2026-09-10：`1.0000 → 0.855330`，`kind='adjust'` 流水 **+1,086.48 CNY**，`actor_id=system`；
   * 01021 买入 2026-10-29：`1.0000 → 0.849012`，adjust **+1,740.44 CNY**。
   全库现在 `currency <> 'CNY' and (fx_rate_ref = 1.0 or fx_rate_applied = 1.0)` 的行数是 **0**。
5. `sams nav rebuild --account a50aa9f3… --days 400` → 写入 **56 天**（此前 7 天），从 2026-09-10 起。
6. `sams attribution --account a50aa9f3… --window 0`
   → 基准行是 `基准 恒生指数 — · 超额 —`，note「恒生指数 缺 2026-09-12、2026-09-13、2026-09-19 等 18 天 的收盘，
   这一段不完整，只出绝对收益」——D291（跟持仓市场走）与 D292（缺天不给超额）都在实机上成立；
   缺的那 18 天本身归 #375 那趟当晚补采。

### 实机跑出来的两条新问题（不在本轮范围，已开决策）

* **D297**（任务 #376）：同一条归因输出里 `区间收益 −92,016.96%`、`区间均权 87,546.31%`。
  根因是这个账户 2026-09-10 ~ 10-28 的净值是**负数**（先买后入金），而 `_run_ending_at_last_complete`
  只做了真值判断，负净值照样进链乘当分母。净值起点提前（F3b）把这段暴露出来了——本轮改动自己踩出来的坑，
  修法与非正净值日的回撤（同一段在持仓卡上印出 `max_drawdown = 1291.52`，即 129,152%）一起做。
* **任务 #377**：银河账户 2026-10-29 的 `sim_cash_moves` 里有**两条** `kind='buy'` −11,527，
  而 `sim_orders` 只剩一笔 01021——撤销/重记那条路留下的孤儿流水，CNY 口袋多扣了一次。
  与今天的回算无关（回算只新增 adjust 行），但它决定「余额能不能从流水复原」这条账能不能信。

