# 补采队列的「顺延」每期只翻一次（D355）

**关联**：D283（打开页面登记补采）、D330（孤源止损顺延）、D48/D49（覆盖确认）、
D43（「查无记录」要每个有能力的源都答）、D279（源冷却）、#380 F3（按只循环的洗牌）。

## 背景

用户报告：HK/00100 的 2026-05-21~2026-11-16 没有公司行为覆盖确认，页面按 D283 的
话术说「打开这只票的页面会把它登记进后台补采队列，补完这里自动出数」，但打开页面
之后一直不出数。要回答的是「排队这条路到底有没有把这行活干完」。

## 实查（2026-11-17，生产库只读副本 `tmp/hk100`）

1. `symbol_backfills` 里 HK/00100 的 `actions` 行创建于 2026-11-16 14:48，到 11-17
   14:02 仍是 `pending`、`tries=0`、`last_error=NULL`——**一次都没被消费者取到**。
   同一票的 `bars` 行早已 `done`（11-16 17:57），说明泵本身在动。
2. 队列 `pending` 共 13 条 `actions` + 1 条 `valuation`。队头三条是 11-03 创建的
   HK 03690 / US AAPL / HK 01021，`tries=2`；00100 排在第 10 位。
3. `task_runs` 里 `actions A/HK/US` 从早到晚每约 10 分钟重试一次，但每一趟只跑
   **1~17 秒**（所有源都很快失败），note 形如
   `501/503 只公司行为，2 只失败，3/6/9/12 条顺延的补采重新排队`。
4. 泵（`BackfillPump`）两次真动手之间至少 20 秒，且只在 `_collect` 的按只循环里
   被叫到。一趟 15 秒的 job 最多让泵动手**一次**，即消费 3 条。
5. 根因由此闭合：`run_actions` 在**每次**开跑时都调 `rearm_deferred(ACTIONS)`。同一周期
   的重试（间隔 10 分钟）把刚失败转成 `deferred` 的队头三条又翻回 `pending`、
   `tries=0`；它们的 `created_at` 仍是最早，于是下一趟泵取到的还是这三条。队头
   永远是自己，00100 永远轮不到。9→12 条的「顺延」就是同一批行被反复翻面的样子。
6. 另一层事实（不是本步要修的代码问题）：HK 公司行为链上有能力的源是 `yfinance`
   与 `akshare_hk`。`yfinance` 在本环境被 Yahoo 以 429/403 拒绝（`source_health`
   连败 9031 次、`last_ok_at` 为空），`akshare_hk` 对 00100 答「查无记录」。D43 规定
   只有每个有能力的源都答「没有」才能写覆盖确认，所以即使队列把这行取到，覆盖
   仍然写不成。这条限制保留，不靠改判据绕过（见「范围外」）。

## 方案

把 D283/D330 写下的「**今晚开跑前**顺延一次」按字面实现：一个周期（一天）里只有
第一次尝试允许把 `deferred` 翻回 `pending`；同一周期的重试不再翻。

* `JobContext` 新增 `first_attempt: bool = True`。默认 `True` 让没走 runner 的调用方
  （CLI 手跑、既有测试）保持原语义。
* `JobRunner.execute` 已有 `first_verdict = previous is None or previous.status == RUNNING`
  （它现在只服务告警连败），把同一个值传进 `JobContext(first_attempt=first_verdict)`。
  上一行是 `FAILED` 的重试因此拿到 `False`；成功的周期不再被重跑；崩溃留下的
  `RUNNING` 仍是「这一周期的第一个结论」，照旧 `True`。
* `_rearm(deps, kind)` 改成 `_rearm(deps, kind, *, first)`：`first=False` 时直接返回 0。
  四个调用点（`run_actions` / `run_financials` / `run_valuation` / `run_flow`）都传
  `ctx.first_attempt`。`_note_rearmed` 因此只在该周期第一次尝试的 note 里出现。
* 重试仍会消费 `pending`：泵、`take_pending`、`note_failure`、`defer` 都不动。
  修掉的是「队头被反复翻回」，不是「重试不该补数」。

## 接口与不变式

1. 同一 `(job, market, period_key)` 内，只有第一次尝试能把该 kind 的 `deferred`
   重置为 `pending`/`tries=0`；之后的重试只消费队列现有 `pending`。
2. 一个周期第一次尝试的判定与告警连败用的是同一个 `first_verdict`（一处定义）。
3. 没经过 runner 的 `JobContext` 构造仍是第一次尝试（默认 `True`）。
4. `rearm_deferred` 仓库方法本身不变：它的语义仍是「把这些顺延行放回队列」，
   「什么时候调用」由 job 这一层决定。

## 测试（先红后绿）

* `tests/services/test_scheduler_runner.py`
  * 新增：失败后的第一轮 `ctx.first_attempt is True`；间隔到点后的同周期重试是
    `False`；推进到下一周期的第一次是 `True`（时钟推进到次日）。
* `tests/services/test_scheduler_jobs_data.py`
  * `ctx()` 辅助函数增加 `first_attempt: bool = True` 参数。
  * 新增：`ctx(first_attempt=False)` 跑 `run_actions`，一条已 `deferred` 的 `actions`
    行保持 `(DEFERRED, 3)`、note 不含「顺延的补采重新排队」；随后
    `ctx(first_attempt=True)` 再跑，行变回 `(PENDING, 0)` 且 note 含「1 条」。
  * 新增：`valuation` 的同一条重试不翻（四个调用点共用 `_rearm`，至少钉两条不同 job）。
  * 既有 `test_actions_job_rearms_yesterday_deferred_rows` /
    `test_the_valuation_job_rearms_yesterdays_deferred_rows` 不改判据，只靠默认值继续绿。

## 验证

* 依次串行：`ruff check src tests`；目标三组 pytest（scheduler runner / scheduler jobs
  data / backfill consumer）；最后全量 `pytest`。
* 生产库副本 `tmp/d355`：
  * 复刻队头三条 `tries=2`、00100 在队尾的状态；用注入的假时钟/假 job 跑一次
    「首跑 + 同周期重试」序列（不外呼网络；`run_actions` 的 `one()` 用测试替身），
    断言重试不翻 `deferred`、`take_pending` 的队头在重试后前进。
  * 观测生产（重启后）：一次失败后的 10 分钟重试 note 不再出现「N 条顺延的补采重新
    排队」；队列 `pending` 的队头从 11-03 那三条向后推进。
* 生产 `sams serve` 重启后 `/healthz`、`/login` 200。

## 范围外

* **Yahoo 在本环境不可达**（429/403，`last_ok_at` 从未有值）：HK/00100 这类
  「akshare_hk 说查无记录、yfinance 答不上来」的票，覆盖确认仍会被 D43 挡住。
  可选的处理路径三条，都需要单独决策：
  1. 把 `yfinance` 从 HK 路由里摘掉（管理后台可改，改完不必重启）：HK 公司行为
     只剩 `akshare_hk` 一个有能力源，它的「查无记录」即可确认；代价是不再采集
     Yahoo 的港股除权记录，Yahoo 恢复后要记得加回。
  2. 接第二个可达的 HK 公司行为源（新浪/港交所），保住「两个源都答」的判据；
     工作量是一次新适配器与真数据核验。
  3. 网络层放通 Yahoo（本机出口被 403）。
* 不放松 D43/D36：不把单个源的「查无记录」写成覆盖确认。
* 不给补采队列加优先级/队首插队（D283 §3.17 的开放问题），本步只修「队头被反复
  翻回」这一条。
* `_collect` 的按只循环、泵的 20 秒节流、每拍 3 条的吞吐都不改。

## 回归结果

* `ruff check src tests`：All checks passed。
* 目标三组（`test_scheduler_runner.py`、`test_scheduler_jobs_data.py`、
  `test_backfill_consumer.py`）：92 passed（1:53）。
* 全量串行 `pytest -q -p no:cacheprovider`：5939 passed, 21 deselected（1:12:25）。
* 生产库副本 `tmp/d355`（2026-11-17 14:23 拷贝，不外呼网络）：
  * 改前快照：`actions` 的 `deferred` 15 条；队头三条（HK 03690 / US AAPL / HK 01021，
    11-03 创建）全是 `pending/tries=0`，正是被每趟重试翻过面的样子；最近几趟 note
    分别是 `9/6/3 条顺延的补采重新排队`（14:19 A / 14:08 A / 14:10 US）。
  * 真 `SymbolBackfillRepo` + 空 universe 跑 `run_actions`：
    `ctx(first_attempt=False)` 的 note 只有 `0/0 只公司行为`，15 条 `deferred` 原样
    留在 `deferred`；`ctx(first_attempt=True)` 的 note 变成
    `0/0 只公司行为，15 条顺延的补采重新排队`，行全部回到 `pending/tries=0`。
  * 泵算术：每拍 `take_pending(3)`、每条三次失败后转 `deferred`，逐拍取到
    `[03690,AAPL,01021] → [920162,001246,02495] → [03388,02590,00068] →
    [00100,02513,NVDA]`，HK/00100 在第 4 拍被取到；改前它永远排在同一批被翻回
    的行后面。
