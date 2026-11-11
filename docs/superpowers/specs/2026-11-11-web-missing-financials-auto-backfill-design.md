# 网页缺年报的下一步：排进补采队列（D336）

## 需求

R3-02 的原话：`/screen` 与个股 F10 财务卡在缺数据时印一句
`先跑 sams sync financials --market A --code <代码>`（2026-11-03 实测：A 股 3531 / 4942
只如此）。这句话违反已定方向「数据不全自动处理、不跑到用户界面」：读它的人在浏览器里
没有终端，而 `sams serve` 占着库时命令行也只会拿到 `DatabaseLocked`。

排队这条机制已经有一半：

* `symbol_backfills` 从 D330 起有 `kind = financials`，`BackfillConsumer._fetch` 认识它
  （`sync_financials(market, code, HISTORY_START, end)`，0 行是合法答复）；
* 缺的是**网页侧没人往里排**：个股页的财务卡与选股页的无数分组都还在印命令。

这一片把「财务指标缺数」这一种活接到同一条队列上，网页不再出现那一条命令。

## 取舍

1. **个股页：打开页面就排（D283 的规矩）。** 财务卡空态与分红卡同一档：有源但库里
   没有这只票的财务指标时，`ctx.backfills_store.enqueue(market, code, FINANCIALS)`，
   卡片说「已排进后台补采队列」。源本身没有的市场（美股桩、或链上没接 F10）不排，
   照旧说「这条链上还没有财务指标的源」；代码表不认识的票不排（D314），照旧说
   「代码表里没有这只票」——那两句话都不是缺口，是能力边界。
2. **选股页：一个按钮，按下去才排。** 选股页的一屏可能缺 3–5 千只，而队列一拍至多
   消费 3 条。若 GET 渲染时就排，每次刷新都会替同一批票重新排一遍（去重只挡开放行，
   补完即失效），队列会被同一屏的重复请求灌满；而且「自动」在五千只的规模上等于
   「一个没人叫它开始的普查」。按钮把「这是缺口、我要补」这一句留给用户，把逐只取数
   留给后台，网页仍然不出现命令行。
3. **共享串只讲事实，下一步由各自的房间附加。** `_annual_missing_reason` 去掉命令，
   只留「没有已披露年报数据（ROE）」（HK/US 仍是「没有截至快照日能取到的年报数据」）。
   命令行的 `sams screen` 见到可补的分组自己追加 `sams sync financials …`——与
   `NONPOSITIVE_VALUATION` 后面追 `--include-negatives` 是同一条规矩（D283）。
4. **引擎只说「这组缺口能由哪种活补上」，不替页面决定按钮。** `MissingGroup.fix` 带
   队列 kind 名（今天只有 `financials`，空串表示没有队列活能补）。分组键随之变成
   `(field, reason, fix)`：一个组里不许一半可补、一半不可补。`pr_annual` 的
   「PE 缺失」「PE 不为正」「ROE 推不出来」都不是财务缺数，`fix` 留空。

## 契约

### 引擎

| 项 | 值 |
| --- | --- |
| `MissingGroup.fix` | `""` 或 `symbol_backfills.FINANCIALS`（`"financials"`） |
| 置 `financials` 的条件 | 理由就是 `_annual_missing_reason(spec, market)`：这只票在库里没有截至快照日可用的年报行 |
| 不置的条件 | `ROE_DERIVATION_FAILED`（年报在库、缺的是可用 ROE）、PE 缺失/不为正、技术面、快照缺列 |

`_annual_missing_reason` 的新输出（两句都不带命令）：

* A 股等有披露日市场：`没有已披露年报数据（{label}）`
* HK/US：`没有截至快照日能取到的年报数据（{label}）`

### 选股页

* 模板：`group.fix == "financials"` 的分组下面出现
  `把这 N 只加入补采队列` 的 POST 表单（csrf、market、field、page、条件行、开关，
  与加自选表单同一套隐藏字段），旁边一句「后台逐只同步年报，补完刷新这一页」。
* `POST /screen/backfill`：按提交的条件重跑引擎（不信任页面带来的代码表），取
  `field` 相同、`fix == "financials"` 的分组，把代码交给
  `SymbolBackfillRepo.enqueue_many`；303 回同一屏，notice 读
  `backfill=新排 N、已在队列 M`。分组已不存在（页面过期）时 notice 说「这一组现在不在
  可补采的范围里」。
* notice 文案不预告等多久（补完一只是 1–3 秒的网络活，一拍至多 3 只）。

### 仓库

`SymbolBackfillRepo.enqueue_many(market, codes, kind)`：一个事务里先取该市场该 kind 的
全部开放键，再逐条插新行；返回 `(新排, 已在队列)`。选股屏一次可能上千只，逐只
`enqueue` 就是上千次事务。

### 个股页

* `f10_partial` 与 `f10_financials_partial` 共用 `_queue_financials(ctx, market, code, read)`：
  只有在「有源、本地没有这一票的财务数据、代码表在册」时 `enqueue(..., FINANCIALS)`，
  模板据此说「已排进后台补采队列」。
* 估值空态不在本片：HK/US 的估值源本身没有，按 §5-D2 的规矩要把话术改成「这个市场
  暂时没有这个源」，那是另一句需要按市场能力分支的话，不在这一片顺手做半截。

## 验证

* 引擎：财务缺数的分组 `fix == "financials"` 且理由里没有 `sams `；`pr_annual` 的
  「没有年报」有 fix、「PE 缺失」「ROE 不为正」没有；ROE 兜底失败没有。
* CLI：`sams screen` 的文本出口仍给命令（命令行用户要的下一步），JSON 出口不变。
* 选股页：无数分组出现排队按钮、页面没有 `sams sync financials`；POST 后队列真有
  `kind=financials` 的开放行，重复 POST 只报「已在队列」，303 落回同一组条件。
* 个股页：A 股空财务卡 → 队列有 `financials` 行、文案是「已排进后台补采队列」；
  无源市场不排也不说已排；代码表不认识的票不排（D314）；有数据的票不排。
* 串行 pytest、`ruff check src tests`；用生产库副本起临时服务，选股页点一次按钮后
  查队列行数；不操作生产通道。

## 回归结果（2026-11-11）

* 相关套件 138 passed：引擎与 CLI（`tests/services/test_screener.py`、
  `tests/cli/test_screen_spot_cli.py`）56、选股页 53、个股页 29；`ruff check src tests`
  通过。串行跑，没有并行 pytest。
* 引擎：财务缺数的分组 `fix == "financials"` 且理由里没有 `sams `；`pr_annual` 的
  「没有年报」有 fix，「PE 不为正」「ROE 不为正」「ROE 兜底推不出」没有；分组键带
  `fix` 后同一字段的「可补」与「不可补」各成一类。
* CLI（生产库副本，`sams screen spot --where 'roe>=0.1' --market A`）：
  `无数 4884 只  roe: 没有已披露年报数据（年报 ROE）；先跑 sams sync financials --market A --code <代码>`；
  同一份输出里 ROE 推不出的那一组（1 只）没有命令。
* 选股页（临时库、临时服务 8799、真实浏览器登录）：A 股
  `年报市赚率 PR ≤ 0.5 且 资产负债率 ≤ 0.6` 的三个无数组是
  「PE(TTM) 不为正 1558 只」「年报 ROE 不为正 22 只」「没有已披露年报数据 3334 只」；
  只有第三组带按钮，页面上没有 `sams sync financials`。
* 点一次按钮：303 回同一组条件，notice「新排进补采队列 3334 只；后台逐只同步年报，
  补完刷新这一页」；再点一次：`queued=0`，notice「3334 只本来就在队列里」。停服后查
  副本库：`symbol_backfills` 里 `kind=financials` 3334 行，全部 pending 且 `open_key`
  齐全（`A|000017|financials` 在列）。
* 个股页：`/symbol/A/000017` 财务卡写「本地还没有这只票的财务指标，已排进后台补采
  队列：补完刷新这一页」，页面没有财务那一条命令；估值空态仍是命令——那是 §5-D2 的
  下一片。
* 截图：`tmp/verify_fix/41-screen-backfill-queued.png`（1440×950，含回执与三组无数）、
  `tmp/verify_fix/42-symbol-financials-queued.png`（个股页财务卡）。
* 生产进程未重启：网页上的排队按钮要等下一次 `sams serve` 启动后才在 8765 上出现。
