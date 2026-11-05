# 数据不全时后台自己补：把「让用户跑命令行」换成「排队补采」

日期：2026-11-03　归属：SP1 尾（数据完整性）+ D281 的同一条线　状态：已实施（D283）
前置：`docs/superpowers/specs/2026-11-01-data-completeness-and-symbol-pick-design.md`、
`docs/superpowers/specs/2026-11-01-s5-attribution-design.md`（D281、D282）

## 1. 问题（生产上真实发生的形状）

用户在个股页/决策卡看到的是这一句：

> A/300750 的 2025-09-25~2026-11-02 没有公司行为覆盖确认：`corp_actions` 里 0 行既可能是这只票
> 从没分过红，也可能是没查过它，裸价区间涨跌在第二种情况下是错数。先跑
> `sams sync actions --market A --code 300750`

前半句是对的（拒绝出裸价错数，这条闸不能松），后半句是三宗错：

1. **网页用户没有命令行**。把运维手册印在卡上，等于告诉用户「这个数你拿不到，去学一门工具」。
2. **就算他有命令行也跑不了**：DuckDB 单写者，`sams serve` 占着 `data/sams.duckdb`，
   `sams sync actions` 只会拿到 `DatabaseLocked`（`storage/db.py:open`）。这条建议在系统
   「保持运行」的时段里**必然失败**。300750 今天就是靠停服 30 秒才补上的：
   `源=akshare 取回=11 入库=11 确认至=2026-11-03`——这只票不是没分红，是**从没查过**。
3. **系统早就有自动补数的通路，只是没接到公司行为上**：`symbol_backfills` 队列 +
   `BackfillConsumer` 每拍消费 3 条（`services/scheduler/backfill.py`）。**实施时纠正了这里的一处
   误判**：`SymbolInterest.ensure` 当时只在「本地没主档」那条分支被调用（`web/pages/symbol.py`
   的 404 检查里），一只已在主档、只是缺公司行为的票**根本走不到登记**。所以本片把
   `_require_symbol` 改成无条件 `ensure`（去重键保证一次渲染不会重复排），否则分红卡上那句
   「打开本页时它已排进后台补采队列」对最常见的情况是假话。
   缺的于是是两件事：「覆盖没确认」没人排队 + 页面渲染没登记。

## 2. 目标与非目标

**目标**：任何人（或任何页）表达「我要看这只票」时，把它缺的数据自动排进后台队列；界面只说
「后台已排队，出数会自己补上」，不再出现命令行文案。

**非目标**（§6 展开）：不为整个 A 股池子做普查；不放松复权闸；不做「等不及」的同步补数按钮。

## 3. 口径

### 3.1 什么算「缺公司行为覆盖」

与今晚 `actions` job 用的是**同一个判据**（`services/scheduler/jobs.py:116-122`）：
该票没有任何一条 `full_history` 且 `covered_to >= 当地今天` 的覆盖确认，就算缺。

不用「请求窗口 ⊆ 覆盖区间」那条更严的判据：那一判需要知道调用方要哪几天，而登记兴趣的时刻
还没有窗口。取**保守且便宜**的一条：先把全历史确认拉到手，窗口够不够由 `guard.require_coverage`
继续把关。闸不动，动的是「谁去补」。

### 3.2 谁有权登记

只有已经在用的那个入口：`SymbolInterest.ensure(market, code, actor=...)`。它的六个调用方
（个股页、立即排一拍、加自选、建规则、建论点、建仓成交）都是**用户表达兴趣**的动作，
一次页面渲染不会重复排（`open_key` 唯一索引去重）。**不**从选股结果页批量登记：那等于把
§6 的普查偷偷做掉。

### 3.3 队列要能分清「补什么」

`symbol_backfills` 今天只有日线一种活（`BackfillConsumer` 只调 `sync_daily`），去重键
`open_key = '{market}|{code}'` 也按这个假设写死。加一列 `kind`：

* `bars`：现有语义，一字不改（默认值就是它）；
* `actions`：公司行为覆盖确认。

去重键带上 kind（`'{market}|{code}|{kind}'`），同一票允许同时挂着一条待补日线 + 一条待补公司行为：
它们是两份不同的活，混成一条会让先完成的那件把另一件也标成 done。

### 3.4 失败与顺延

沿用现有三态：`pending → （3 次失败）deferred → done`。`deferred` 的原文承诺是
「今天的重试预算用光，等今晚盘后任务收尾」——对 `bars` 成立（盘后日线任务会
`mark_done_by_code` 结案），对 `actions` **不成立**：今晚的 `actions` job 只跑 universe
（自选/持仓/规则/论点）里的票，队列里那条非 universe 的 `actions` 行会永远躺在 deferred。
所以补一环：今晚 `actions` job 开跑前，把 `kind='actions'` 的 deferred 行顺延回 pending
并把 `tries` 清零（一天一份预算）。这不是新增状态，是把「明天再来」这句话真的做出来。

（与 #365 的区别说清楚：#365 问的是**一趟 job 内**源链连败要不要整趟顺延；这里是**跨天**的
队列生命周期，两件事不互相覆盖。）

## 4. 改动清单

| 位置 | 改什么 |
| --- | --- |
| `storage/migrations/0046_backfill_kind.sql` | `ALTER TABLE symbol_backfills ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'bars'`；同一趟把开放行的 `open_key` 归一成 `'{market}\|{code}\|bars'`（历史行只有 bars 一种活，归一后新旧键同一形状） |
| `storage/repos/symbol_backfills.py` | `enqueue(market, code, kind="bars")`、`open_for(..., kind)`、`BackfillRow.kind`、`take_pending` 带出 kind；新增 `rearm_deferred(kind)` |
| `services/scheduler/backfill.py` | 按 kind 分派：`actions` → `corp_actions.sync_actions(market, code, HISTORY_START, today)`，其余走原路 |
| `services/scheduler/jobs.py:run_actions` | 开跑前 `rearm_deferred("actions")`，结案话术带上补采队列 |
| `services/symbol_interest.py` | 覆盖没确认 → `backfills.enqueue(market, code, kind="actions")`；`EnsureReport` 带出这次排了什么 |
| `analysis/guard.py:require_coverage` | 拒绝文案去掉命令行，改成「后台已登记补采，补完自动出数」 |
| `services/technical.py:55` `NO_COVERAGE` | 同上（选股页那一组「无数」的话术） |
| `web/pages/symbol.py` `_require_symbol` | 主档存在时也登记兴趣（原来只在缺主档分支里 `ensure`）；`symbol_sync` 里那句重复 `ensure` 一并收掉 |
| `web/pages/symbol.py:567` + `partials/f10.html:67` | 分红卡那句「采一次：`sams sync actions …`」换成「后台已排队补采」；context 里去掉 `actions_command` |
| `cli/analysis.py` | 三处拒绝点各加一个 `except CoverageMissing`：JSON 带 `next`，文本带「命令行要补覆盖：」——命令只给命令行用户 |
| `bootstrap.py` | `BackfillConsumer` 多接一个 `corp_actions`；`SymbolInterest` 多接 coverage 读面与 clock |

**闸本身一字不改**：`require_coverage` 继续拒绝，`adjusted_series` 继续是唯一出数口。

## 5. 界面文案（实施后的原话）

* 决策卡 / 图表（`analysis/guard.py:require_coverage`）：`… 的 {start}~{end} 没有公司行为覆盖确认：
  corp_actions 里 0 行既可能是这只票从没分过红，也可能是没查过它，裸价区间涨跌在第二种情况下是
  错数，所以这里不给数。打开这只票的页面会把它登记进后台补采队列，补完这里自动出数。`
* 选股页技术面那一组（`services/technical.py:NO_COVERAGE`）：`公司行为覆盖未确认，无法复权：
  打开该股页面会登记后台补采`
* 个股页分红卡（`partials/f10.html`）：`本地还没有这只票的分红送转记录——打开本页时它已排进
  后台补采队列，补完这张表自动出现。`

**选股页那句和本文初稿不一样，是有意改的**：初稿写「已登记后台补采」，而 §3.2 明确不从选股结果
批量登记（否则就是 §6 拒绝的普查）。写「已登记」对选股池里的票是假话，所以改成条件句
「打开该股页面会登记」——它承诺的是用户下一个动作能得到的东西，不是没发生的事。
同理，决策卡那句也是「打开页面会登记」，只有分红卡那句能写完成时（那张卡本身就在个股页里，
渲染它的那一刻 `ensure` 已经跑过）。

三句都不再出现 `sams …`。运维仍要手动补时，命令行照旧存在（`sams sync actions`）：
`CoverageMissing(NotAdjustable)` 这个子类只让它自己往 JSON/文本里附加 `next` 提示，
共享给网页的那半句始终不含命令。这与 D281 是同一条规矩：**卡上的操作指南必须是网页用户真能做的动作**。

## 6. 明确不做

* **A 股全市场覆盖普查**：今天 531 只有日线的 A 票里 35 只零覆盖（`corp_actions_coverage` 左连
  查得），而选股池可以远大于 531。普查是另一张单（D227 已经说过「补覆盖要往 `--code` 里塞几千次」
  这个量级不该由这里顺手承担）。健康页那格「有日线无覆盖」的读数已经在报这件事。
* **同步补数按钮**（点了就等的「现在就采」）：单写者之下它只能在调度线程里做，需要一条
  请求→完成的回显链路，本片不做。
* **放松复权闸**：0 行永远不等于「没有分红」。
* 把 `bars` 队列的语义也一并重写：`kind` 加默认值，旧行为旧键形状归一，不动它。
* **本片没有把 f10 其余五句命令行也换掉**（`profile/announcements/news/financials/valuation`
  的 `*_command`，`web/pages/symbol.py:565-569`）：队列里现在只有 `bars` 和 `actions` 两种活，
  给那五个写「已排队补采」是第二句假话。正确顺序是先把 `kind` 扩到那五种（各自的 sync 面已经存在），
  再把话术换上——这是一片独立的活，不塞进这里顺手做半截。
  扩哪几种、按什么判据算「缺」，#369 已把那五种各自挡了谁写清楚（`2026-11-03-expert-testing-round3.md`
  R3-02 逐条原话 + §5-D2）：`nav rebuild` 走页面按钮触发重算，`bars/financials` 扩 `kind` 进同一条队列，
  `channel probe` 改页面「测试通道」，`--include-negatives` 改页面勾选框（T1a 已在 11-06 先做：
  勾选框本来就在表单里，共享句不再提命令行，CLI 自己把开关名接在后面），
  `profile/valuation`（HK/US）源本身没有，话术改成「这个市场暂时没有这个源」，不承诺补采。
  五种一起排是每天每票五条队列行的吞吐，而一拍至多消费 3 条。
* **卡片不预告补采要等多久**（实机测得的两段）：`BackfillConsumer` 排在一拍的**开头**（`requests.drain()`
  之后、周期 job 之前），一拍至多消费 3 条。于是一行的等待时间不是「下一拍 ≈ 60 秒」，而是
  **「当前这一拍还剩多久」**——实测 17:16 起的那一拍里逐票 `按日估值` 要跑 500+ 只、每只 1–5 秒，
  我在其中排的一行轮询 117 次 / 约 58 分钟仍没被消费；重启后新的一拍 6 秒就吃到它
  （`18:12:46 启动 → 18:12:52 A 601398 公司行为 23 条`）。
  所以「补完自动出数」这句没错，但它可能意味着几十分钟。要不要把补采提到队首（或给个股页那一次
  单独开一口），见 #369 报告 §5-D10。

## 7. 测试（红→绿，中文名留在 docstring）

1. `enqueue` 同一票两种 kind 各成一行；同 kind 重复排返回 False（去重键真的带 kind）。
2. 迁移后既有开放行的 `open_key` 是 `'{market}|{code}|bars'`，且旧调用方（不传 kind）行为不变。
3. `BackfillConsumer` 遇到 `kind='actions'` 调的是 `sync_actions`，遇到 `bars` 调的还是 `sync_daily`
   ——用假 sync 记下调用（这两条是同一件事的两面，一条测试一次调用）。
4. `actions` 失败三次转 deferred，`run_actions` 开跑前把它顺延回 pending 且 `tries=0`。
5. `SymbolInterest.ensure`：无覆盖确认 → 排上 `actions`；已有 `full_history` 且确认到今天 → 不排；
   覆盖有但只到上周 → 排（每天顶多一条，不是每次渲染一条）。
6. 文案三条各钉一处：渲染出的卡片/JSON 里**不含** `sams ` 开头的命令串
   （`tests/web/test_symbol_f10.py`、`tests/services/test_screener_technical.py`、
   `tests/analysis/test_adjust.py`）。这一条是给「不许再回到用户界面」上锁的。
   其中分红卡那条同时钉住 §1.3 的纠正：先 GET 个股页（主档已在），再要分红卡，
   断言队列里出现 `actions` 行——如果 `ensure` 还挂在缺主档分支上，这条会红。
7. 全量回归 + 实机：挑一只零覆盖的 A 票开个股页 → 队列里出现 `actions` 行 → 调度补采 → 覆盖确认落库
   → 那张卡从拒绝变成出数。
   **已实机走通（2026-11-03 重启后 6 秒）**：`A 601398` 与 `A 000858` 先由页面登记（`排队公司行为=True`，
   后续渲染为 `False` = 去重生效），新的一拍开头 `sams.services.corp_actions` 记到
   「A 601398 公司行为 23 条（源 akshare，确认至 2026-11-03，全历史）」，再开这一页时
   那句「没有公司行为覆盖确认」已经不在了——卡前进到下一个真缺的输入
   （「没有已披露的财报：财务闸过不了」，那是 R3-02 的 `financials` 那一路，不是这里的锅）。
8. D283 让「打开页面就登记」成为默认，于是空态那一屏的「没有排队」那一档在这条路上再也走不到：
   原来钉住它的 `tests/web/test_symbol.py::test_no_queued_backfill_says_so`（D146）改写成
   `test_a_bare_symbol_page_now_always_says_queued`——页面说「已排队，下一拍自动开始」时，
   断言队列里真有一条开放日线行。**话与队列必须一致**，这是同一条规矩的两面：
   旧断言（「没排队就不许说会自动补」）当时是对的，现在把它原样留着就是在钉一个不可达状态。

## 8. 决策记录

* **D283**（生产上真实发生的形状）：**数据不全不是用户的活**。任何「这个数算不出来」的界面文案，
  如果它给的下一步是命令行，而系统本来就有一条自动通路能补齐，那句话就是 bug 而不是提示。
  本片把公司行为覆盖缺口接到 `symbol_backfills`（`kind='actions'`）：登记兴趣的那一刻排队，
  调度每拍消费，今晚的 `actions` job 把昨天没补上的顺延行重新武装；闸（`require_coverage`）
  一字不改，继续拒绝对外的裸价错数。**改变的是谁去补，不是补不补。**
  推论两条已经落在代码形状里：① 页面渲染必须真的登记（`_require_symbol` 无条件 `ensure`），
  否则「已排队」是假话；② 同一判据只能有一处实现（`CorpActionsCoverageRepo.full_history_confirmed`），
  登记侧和 job 侧各写一遍时，症状不是报错而是队列永不结案或每次渲染重排一条。
* **D285**（#369 实机，15 只蓝筹里 6 只整页 400）：**闸拒的是一个数，不是这一屏**。
  `require_coverage` 抛出的 `NotAdjustable` 一路冒穿出个股页，把日线、报价、F10、同业、决策卡
  全部拖成 400 错误页——而 D283 那句话（「打开这只票的页面会把它登记进后台补采队列」）
  在 400 页上是做不到的承诺：登记发生在渲染之中，异常打断在登记之后、其余各卡之前。
  改在 `DecisionService.card` 的边界上：`except NotAdjustable` → 退回「证据不足」那一态，
  把那句拒绝原文写进 gates，其余各卡照常渲染。**不在页面层兜**：CLI、复检、试算走的是同一个服务，
  在 service 出口退一次，三个调用方一起不再炸。
  钉住它的测试是 `test_a_coverage_refusal_degrades_the_card_not_the_whole_page`
  （要凑 320 根日线才走得到那道闸，`bars < MIN_BARS=300` 更早就退，所以这条的夹具必须先喂够）。
  400 变 200 ≠ 有数：那 6 只票的技术面还是空的，那是 §6 的普查问题（#369 报告 R3-03）。
