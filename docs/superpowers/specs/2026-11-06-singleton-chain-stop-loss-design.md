# 孤源 job 的止损：一趟内连败 10 次就把剩下的顺延（#365，D330）

**目标**：夜里那几趟逐票 job 在碰到「只有一个源能答、而它正坏着」时，不把 500 只票
一只只撞完；连败到阈值就停止本趟，把没跑的票记进补数队列，下一趟再试。

**范围**：`financials` / `valuation` / `flow` 三条按只 job（实测受害最深的两趟就在这里）。

---

## 1. 问题

D279 的源冷却只盖多源链：链上只剩一个能答的源时**故意不冷却**——冷掉它等于整晚
什么都拿不到。代价是孤源坏着的夜里，`_walk` 每只票仍会把这一家真调一遍（一次判定
3 个请求 + 1/4 秒退避），而它早就定性了。

2026-11-02 00:00–04:18 的 `task_runs`（读库）：

| 一趟 job | 时长 | 真正拿到行 |
| --- | --- | --- |
| `financials A` | 26.6–46.0 分钟 | 494 / 505 |
| `flow A` | 13.6–35.5 分钟 | — |

#387 补源之后（2026-11-06 能力矩阵实测）仍然只有 1 个源的格子：

| 能力 × 市场 | 孤源 | 是否逐票 × 500 |
| --- | --- | --- |
| 资金流 A | `akshare` | 是（本文覆盖） |
| 按日估值 US | `yfinance` | 是（本文覆盖） |
| 公司行为 US | `yfinance` | 是（不在本文：归 `actions` 那一趟，见 §5） |
| 个股新闻 A/HK | `eastmoney_f10` | 是（不在本文：新闻 job 的失败面另算） |
| 公司概况 / 公告 HK | `eastmoney_f10` | 是（不在本文） |
| 分时 / 汇率 / 代码表 / A 快照 | 各 1 | 否（一趟只调一次或按需） |

财务 A 与估值 HK 已在 #387 补到 2 个源（`akshare`+`sina`、`yfinance`+`baidu_valuation`），
D279 的冷却已经覆盖它们；**剩下真正会整夜撞墙的是 `flow A` 与 `valuation US`**。

## 2. 决策 D330

**一趟 job 内，某市场某能力的链只有 1 个源、且这个源连续失败 10 次，就停止该市场
剩余单位，把没跑的票以 `deferred` 写进 `symbol_backfills`。**

* 阈值 10 与 D279 的 `COOLDOWN_AFTER_FAILURES` 同一口径：一次判定 3 个请求，
  10 次 ≈ 30 个请求 + 半分钟退避，足够说清「它今天就是不答」。
* **行为改变**：被顺延的票今晚确实没有数据，由下一趟 job 与补数消费者负责补。
  这是用户批准过的取舍（2026-11-06）。
* 只有**连败**才停：中途有任何一次成功（含「已经追平、没有请求」的空转）就把计数清零。
  孤源链的抖动通常表现为连续失败；被零星空转打断的失败不应该触发停止。

## 3. 机制

### 3.1 队列

`symbol_backfills` 的 `kind` 增加三种：`financials`、`valuation`、`flow`。表上本来
就是 VARCHAR + 默认 `bars`，没有 CHECK，不需要迁移。

新增 `SymbolBackfillRepo.defer(market, code, kind, reason)`：

* 直接插一行 `status='deferred'`、`tries=0`、`last_error=reason`，`open_key` 照旧；
* 同一票同一 kind 已有开放行时返回 False（去重键不变）；
* 之所以不先 `enqueue` 再 `note_failure`：那会写 3 次才变 deferred，而且失败计数
  会假装消费者真试过 3 次。

### 3.2 `_collect` 的止损

`_collect` 新增三个参数：

* `capability`：能力名（`financials` / `valuation` / `funding_flow`），用来问链长；
* `give_up_after`：默认 10，测试可调；
* `give_up_kind`：顺延时写进队列的 kind。

循环里维护连败计数；命中时：

1. `deps.registry.chain_for(market, capability)` 长度为 1 才算孤源（整块没配源时
   `chain_for` 抛 `DatasourceError`，按「不是孤源」处理，job 本来就不会登记）；
2. `stopped = index + 1`（当前这只已经问过、记进 `errors`，不在顺延名单里）；
3. 剩下的单位逐条 `defer(..., kind, f"{market} 孤源 {name} 连败 {n} 次")`；
4. note 写「A 孤源 akshare 连败 10 次，其余 490 只已顺延」，`truncated=True`，
   `errors` 保留那 10 条（**这不是成功**，健康页要看见这一趟坏了）。

与 D311 的 deadline 截断**分开**：deadline 那条仍走 `defer_kind`（只有 bars/actions
有），财务三条的 deadline 行为不变（不排队、留待下一轮）。`give_up_kind` 只服务止损。

### 3.3 下一趟与补数消费者

* 三条 job 开跑时 `rearm_deferred(kind)`（与 `run_actions` 同一条规矩），note 带
  「N 条顺延的补采重新排队」；
* `BackfillConsumer` 认识三种新 kind：
  * `financials` → `sync_financials(HISTORY_START, end)`；
  * `valuation` / `flow` → 按 `latest_date` 定窗口（与 job 的 `_window` 同口径），
    已追平就无事可做；
  * 0 行不是失败（D36：没有披露/没有数据是合法答复），只有抛错才 `note_failure`；
* 三条 job 每成功处理一只票（含已追平的空转）就 `mark_done_by_code(kind)`：夜间
  job 走过一遍之后，队列里那条顺延就算结清。

### 3.4 不改的东西

* 不冷却孤源（D279 的取舍不动）：止损只在**这一趟**内停止，下一趟照样问；
* 不改 `run_bars` / `run_actions` / profile / announcements / news 的行为；
  其中 `actions` 已有自己的 kind 与超时交接，若以后要加止损，复用同一组参数即可；
* 不区分 D322 的「同源不算独立故障域」：HK/US 财务那两格链长是 2，不会触发。
  等数据源目录有了机器可读的域标记再说，本文不引入启发式。

## 4. 测试（红→绿）

1. `SymbolBackfillRepo.defer`：直接落 deferred、带原因、去重；
2. `run_financials` 单源链连续 10 次失败 → 只问 10 只，其余 `deferred`，
   note 写「孤源 … 连败 10 次」「已顺延」，`errors` 恰好 10 条；
3. 链长 ≥ 2 时不触发（同 10 次失败仍把整批跑完）；
4. 中途成功一次就清零：9 败 + 1 成 + 9 败不触发；
5. 已追平的空转（窗口返回 None）也算一次成功、清零；
6. `BackfillConsumer` 能消费三种新 kind：financials 走全历史、valuation/flow 按
   `latest_date`；抛错进 `note_failure`，0 行算 done；
7. 三条 job 开跑时把昨天的 deferred 重新排上（用 valuation 钉一条）；
8. 既有 D311 用例 `test_a_fundamentals_job_truncates_without_a_queue` 保持绿
   （deadline 截断不排队），证明这次没有顺手改掉 deadline 行为。

## 5. 明确不做

* `actions` US（孤源 `yfinance`）的止损：它已有 `actions` kind 与 deadline 交接，
  但本次不为它接 `give_up_kind`——先把最深的两趟（资金流、估值）止血。
* profile / announcements / news 的孤源止损：同样留到需要时，按同一组参数接。
* 同源域判定（D322）：见 §3.4。

## 6. 验收

* 全量回归通过；
* 实机：在测试库上把 `flow A` 的孤源配成一个必失败的桩，跑 `run_flow`，确认
  第 10 只之后停下、队列出现 deferred 行；恢复桩后跑下一趟，确认队列被 rearm
  并由 job 结清。线上不制造故障，等真实失败自然验证。

### 实施记录（2026-11-06）

* `_collect` 的三个参数按 §3.2 落地；`SINGLETON_GIVE_UP = 10` 与 D279 同口径。
* 三条 job 开跑时 `rearm_deferred(kind)`，每成功一只（含 `_window` 返回 None 的空转）
  `mark_done_by_code(kind)`；`run_actions` 的 rearm/note 抽成同一对助手，行为不变。
* `BackfillConsumer` 新增可选 `fundamentals=`；`financials` 走全历史，
  `valuation`/`flow` 复用 job 的 `_window` 续上；0 行一律算 done，未接服务时
  抛 `DatasourceError` 而不是把行标成 done。
* 验收的「实机」一条以测试库替身完成（`tests/services/test_scheduler_jobs_data.py`
  与 `test_backfill_consumer.py`）：单源 10 连败 → 只问 10 只、其余 deferred；
  下一趟 rearm；消费者按 kind 分派。线上不做故障注入。
* 测试期间发现一处口径：`RateLimited` 会被 `call_with_backoff` 一次吃掉三个桩答复，
  按「一只票一个脚本答复」写的用例改用不可重试的 `DatasourceError`，否则脚本与
  票会错位、把「成功清零」误测成「连续失败」。

