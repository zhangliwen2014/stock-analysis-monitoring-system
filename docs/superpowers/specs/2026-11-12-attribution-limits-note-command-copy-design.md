# 归因卡与限额注记去命令（D343）

## 需求

R3-02 点名网页里的命令行话术。D342 清了净值卡模板与汇率提示，但组合页还有两处经
**服务层字符串**渲染上网页的 `sams nav rebuild`：

* 归因卡空态（`services/attribution.py::evaluate` 的 `skipped`）：净值没重建时印
  `也可以跑 sams nav rebuild --account {id}`，由 `partials/portfolio_attribution.html`
  原样转述。
* 组合限额的回撤注记（`services/limits.py::_drawdown`）：没有净值时印同一句，经
  `pages/portfolio.py` 的 `limits`/`notes` 上下文上网页。

这两条与 D342 那两处不同：它们不是模板里的死字符串，而是服务注记，**有的还被 CLI
共用**。网页用户没有终端，命令在网页上是死路；CLI 用户有终端，命令是合法操作指南。

## 取舍

1. **服务注记改渠道中性：只说情况 + 盘后自动 job，不带命令。** `run_nav` 每晚按账户
   重建最近 `NAV_WINDOW_DAYS` 天（D342 已确认），净值不是「没人跑命令才没有」。两处
   注记去掉 `也可以跑 sams nav rebuild …` 那一截：
   * `attribution.skipped` → `这段时间还没有连续两个能算出来的净值日：盘后 job 每晚重建最近 {N} 天；缺价/缺汇率的日子不并入`
   * `limits._drawdown` → `还没有算得出来的净值：盘后 job 每晚重建最近 {N} 天；缺价或缺汇率的日子整日不计`
2. **limits 注记是 web-only，直接去命令。** `src/sams/cli` 没有 limits 出口（只有
   attribution/nav），回撤注记只经组合页上网页，没有 CLI 消费者要保。
3. **归因注记 web + CLI 共用：CLI 文本自己补回命令。** `cli/attribution.py::_print`
   打印 `skipped` 时用 `report.account_id` 拼上 `（也可以跑 sams nav rebuild --account {id}）`，
   保住 CLI 那句「复制就能跑」的操作指南（`test_attribution_cli.py` 仍钉着命令）。
   JSON 出口的 `skipped` 字段保持中性（它是数据，不是操作指南）。服务注记里不再写
   账户 id——id 由 CLI 那一头从 `report.account_id` 取，网页那一头本就不需要它。
4. **不新增队列活。** 与 D342 同：`run_nav` 已是自动 job，去命令即可，不叠床架屋。

## 契约

* `services/attribution.py`：`skipped` 去命令、去账户 id，按 §1 改写；更新上方注释
  （说明注记渠道中性、命令由 CLI 补）。
* `services/limits.py::_drawdown`：`series.latest is None` 那一支按 §1 改写。
* `cli/attribution.py::_print`：`if report.skipped:` 那一行打印时补回命令后缀。
* 模板无需改（仍原样转述 `skipped`/注记，只是字符串里不再有命令）。

## 测试

* `test_portfolio_attribution_web.py::test_the_card_gives_a_way_out_before_the_nav_is_rebuilt`：
  `assert f"sams nav rebuild --account {account_id}" in text` → `assert "sams nav rebuild" not in text`；
  「还没有连续两个能算出来的净值日」「盘后」仍在。
* `test_limits.py::test_a_drawdown_line_without_any_nav_says_how_to_build_it`：
  `assert "sams nav rebuild" in row.note` → `not in`；保留「盘后」断言，去掉账户 id 断言
  （注记不再带 id）。
* `test_attribution.py::test_without_two_usable_days_the_whole_card_refuses`：
  `assert "nav rebuild" in report.skipped` → `not in`；保留「盘后」。
* `test_attribution_cli.py::test_attribution_says_what_to_run_before_any_nav_exists`：**不动**
  （CLI 文本仍带命令，由 `_print` 补回）。

## 验证

* 串行 pytest（归因 web/cli/service + limits service + 组合 web）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：新开一个空账户，归因卡与限额回撤注记都无命令；CLI
  `sams attribution` 对同一账户仍带命令（在副本上跑，不操作生产通道）。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

2026-11-12 实测（TDD：先转换三处断言见证 RED——3 failed / CLI 1 passed，再实现 GREEN）。

* 串行 pytest：`test_portfolio_attribution_web.py` + `test_attribution_cli.py` +
  `test_attribution.py` + `test_limits.py` + `test_portfolio_web.py` +
  `test_portfolio_nav_web.py` 共 **124 passed**；`ruff check src tests` All checks passed。
* 生产库副本（`tmp/d343.duckdb`）+ 临时服务 8799，登录 sp1check 新开空账户
  `94fbf177…`（cash=0，无净值）：
  * 归因卡空态印「还没有连续两个能算出来的净值日：盘后 job 每晚重建最近 400 天；
    缺价/缺汇率的日子不并入」——`sams nav rebuild` **不在**文中，「盘后」在。
  * 组合页限额回撤注记印「还没有算得出来的净值：盘后 job 每晚重建最近 400 天；
    缺价或缺汇率的日子整日不计」——同样无命令。
  * 停临时服务释放写锁后，对同一账户跑 `sams attribution --window 90`：文本末尾补回
    「（也可以跑 sams nav rebuild --account 94fbf177…）」，带真账户 id；`--json` 的
    `skipped` 字段保持中性（无命令）。
* 验证毕清理副本；生产 `sams serve` 重启后 `/healthz`、`/login` 均 200。