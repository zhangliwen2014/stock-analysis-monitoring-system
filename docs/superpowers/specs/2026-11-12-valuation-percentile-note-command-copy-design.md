# 估值分位注记去命令（D350）

## 需求

R3-02 点名网页里的命令行话术。个股页「估值位置」卡（`partials/valuation.html:29`
原样转述 `row.reason`）在库里没有按日估值行时，经**分析层字符串**印 `sams sync valuation`：

* `analysis/valuation/percentile.py` 三处拒绝注记都带命令：
  * `pr_percentile`（264 行）：窗口内一行估值都没有 →
    `窗口 {from}~{to} 内没有任何按日估值行：先同步这只票的估值（\`sams sync valuation\`）`。
  * `metric_percentile`（359 行）：PE/PB 那一支窗口内一行都没有 → 同一句。
  * `metric_percentile`（375 行）：有行但那一列全空 →
    `窗口 {from}~{to} 内没有可用的 {metric}（N 行里 M 行缺失或不为正）：先同步估值数据（\`sams sync valuation\`）`。
* 这三句都属于 `NOT_COVERED`（未覆盖）：下一步是「补这只票的按日估值」。

**这条注记是分析层生成的，被两个渠道共用**，与 D347（业务卡，web-only）、
D349（同业，web-only）不同，必须按 D343 的渠道分流办法处理：

* **网页**：`ValuationService.percentiles()`（`services/valuation.py:284`，**只被
  `web/pages/symbol.py:902 valuation_partial` 消费**）→ `valuation.html:29` 印 `row.reason`。
* **CLI**：`ValuationService.read()`（168 行）→ `cli/analysis.py:816 _percentile_line`
  印 `row.reason`；`--json` 的 `_percentile_payload`（1052 行）也带 `reason`。
* **共用旁路**：`read().plan.notes` 经 `compose.py:487-488` 把 `pr_percentile.reason`
  收进计划注记，再经 `theses.py:541` 上**论文卡（web + CLI 共用）**。`decision.py` /
  `rules/snapshot.py` 只取价位与赔率，不转述分位 `reason`，不在本次范围。

## 取舍

1. **分析层注记改渠道中性：只说事实，不带命令、不带网页卡名、不承诺自动补。**
   三句去掉「：先同步…（`sams sync valuation`）」那一截，留下可复算的事实：
   * 无行（pr / metric）：`窗口 {from}~{to} 内没有任何按日估值行`。
   * 有行但列全空（metric）：`窗口 {from}~{to} 内没有可用的 {metric}（N 行里 M 行缺失或不为正）`。
   分析层不知道本市场有没有估值源、代码表认不认识这只票、它在不在票池，所以**不能**
   无条件说「盘后会自动补」（D336/D347 同一条诚实规矩）；也不该在共用层写网页卡名
   （CLI 读到「见本页…卡」是死路）。事实本身两个渠道都读得通。
2. **网页那一头补「指给权威卡」：`percentiles()` 是 web-only，给 `NOT_COVERED` 的行
   追加 `；估值补采状态见本页「主要财务指标」卡`。** 同页的 F10「主要财务指标」卡
   （`f10_financials.html:50-63`）顶部就是估值一行，且有三态（无源／已排进补采队列／
   代码表没它排不上）；它随页加载时 `_queue_valuation`（`symbol.py:631-643`）在
   「估值空 + 有源 + 在册」时已排 VALUATION（D338）。补采机制本就存在，分位卡只需把
   状态指给那张权威卡，不自己再排一次、也不注入新依赖（D349 同一办法）。
3. **CLI 那一头补回命令：`_percentile_line` 对 `NOT_COVERED` 追加完整可跑命令
   `（先 \`sams sync valuation --market {m} --code {c}\`）`。** `NOT_COVERED` 在
   `percentile.py` 里**只**由这三处「缺估值行」产生（其余拒绝是 NOT_DISCLOSED /
   NOT_APPLICABLE / UNCOMPUTABLE），是干净的单案契约（D343）。命令带 `--market/--code`
   才「复制就能跑」（裸 `sams sync valuation` 会直接报错）；market/code 由
   `_print_valuation(read, …)` 从 `read.market/read.code` 传入。`--json` 的
   `_percentile_payload.reason` 保持中性（它是数据，不是操作指南，D343）。
4. **论文卡旁路（`plan.notes`）拿中性事实即可，不再补命令。** 它解释「那一栏为什么空」，
   不是估值补采的操作指南；web 读到无命令的事实满足 R3-02，CLI 的估值操作指南由
   analyze 的 `_percentile_line` 与既有 `_valuation_line` 给。不给共用旁路写网页卡名。
5. **不新增队列活、不动模板、不动 `missing_kind` 语义。** 模板仍原样转述 `reason`；
   三档拒绝的 `missing_kind` 不变（屏幕标签 `missing_label` 不在本次范围）。

## 契约

* `analysis/valuation/percentile.py`：
  * `pr_percentile` 无行支（264 行）`why` → `f"窗口 {window_from}~{as_of} 内没有任何按日估值行"`。
  * `metric_percentile` 无行支（359 行）`reason` → 同上。
  * `metric_percentile` 列全空支（375 行）`reason` →
    `f"窗口 {window_from}~{as_of} 内没有可用的 {metric}（{len(pairs)} 行里 {dropped} 行缺失或不为正）"`。
  * 三处 `missing_kind=NOT_COVERED` 不变；`evidence` 不动。
* `services/valuation.py::percentiles`：三条读数出门前经一个 web-only 小函数
  `_defer_collection(row)`：`row.ok` 或 `missing_kind != NOT_COVERED` 原样返回；否则
  `replace(row, reason=f"{row.reason}；估值补采状态见本页「主要财务指标」卡")`。
  新增 `from dataclasses import replace`、`from sams.core.missing import NOT_COVERED`。
  `read()` 不动（共用，保持中性事实）。
* `cli/analysis.py::_percentile_line`：签名加 `market, code`；`not row.ok` 且
  `row.missing_kind == NOT_COVERED` 时 `reason += f"（先 \`sams sync valuation --market {market} --code {code}\`）"`。
  调用处（816 行）传 `read.market, read.code`。新增 `from sams.core.missing import NOT_COVERED`。
  `_percentile_payload` 不动（JSON 中性）。
* 模板、`compose.py`、`theses.py` 均无需改。

## 测试

* `tests/services/test_valuation_read.py::test_a_market_with_no_fundamental_rows_refuses_and_names_the_next_command`
  （381 行）：`assert "sams sync valuation" in reading(read, "pr").reason` →
  `assert "sams sync valuation" not in …`，并保留事实断言
  `assert "没有任何按日估值行" in reading(read, "pr").reason`（`read()` 共用，中性）。
* `tests/services/test_valuation_percentiles.py`：新增
  `test_the_web_percentiles_defer_valuation_collection_to_the_financials_card`——
  `service.percentiles("HK", "00700", …)`（无估值行，三条都 NOT_COVERED）：断言每条
  `"估值补采状态见本页「主要财务指标」卡" in row.reason`、`"sams sync valuation" not in row.reason`、
  `"没有任何按日估值行" in row.reason`。
* `tests/web/test_symbol_fundamentals.py`：新增
  `test_valuation_partial_without_rows_defers_to_the_financials_card_not_a_command`——
  `_symbol(ctx)` 只铺主档、不铺估值行，`GET /api/partials/valuation?market=A&code=600519`：
  文本含「没有任何按日估值行」「主要财务指标」，不含 `sams sync valuation`。
* `tests/cli/test_valuation_cli.py`：新增
  `test_the_percentile_line_names_a_runnable_sync_command_when_no_valuation_rows`——
  无估值行的票跑 `sams analysis valuation`（文本）：分位那句含
  `sams sync valuation --market A --code {code}`；`--json` 的 `percentiles[*].reason`
  不含 `sams sync valuation`（中性）。

## 验证

* 串行 pytest（`test_valuation_percentiles.py` + `test_valuation_read.py` +
  `test_valuation_cli.py` + `test_symbol_fundamentals.py` + 分析层
  `test_valuation_percentile.py`）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：挑一只主档在册、无按日估值行的票，估值位置卡三条拒绝
  都指给「主要财务指标」卡、无命令；同页 F10 卡显示估值补采三态。停临时服务释放写锁后，
  对同一票跑 `sams analysis valuation`：分位那句带完整 `--market/--code` 命令，`--json`
  的 reason 中性。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

（待填）