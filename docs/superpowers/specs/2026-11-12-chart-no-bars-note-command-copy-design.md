# K 线空态标题注记去命令（D346）

## 需求

R3-02 点名网页里的命令行话术。个股页 K 线读不出时，图表标题（`.chart-caption`）经
**服务层字符串**印 `sams sync daily`：

* `services/analysis.py::_why_no_bars`（约 340-367 行）把「读不出能用的 K 线」拆成三种
  拒绝，每种都带 `sync_hint = sams sync daily --market … --code … --start … --end …`。
  它经 `series()` 抛 `NotAdjustable` → `pages/symbol.py::bars_api`（401 行）捕获 →
  `_empty_payload(period, str(exc))` 的 JSON `note` → `static/app.js:478` 写进
  `.chart-caption`。同一份字符串也被 CLI `sams analysis` 系列命令的 `NotAdjustable`
  出口打印（`cli/analysis.py:362/599/796`）。

## 取舍

1. **三分支分别改成渠道中性，按各自诚实的下一步说话：**
   * **代码写错**（全库 0 根 + 主档无此码）：`…代码多半写错了（指数、或没收录的标的
     也是这个形状）。确认代码后再试一次`。**不承诺自动补**——代码错了，没有可补的活。
   * **从未采集**（主档有它、全库 0 根）：`…还没采集过；盘后任务会自动补采这只票的日线`。
     开个股页时 `symbol_interest.ensure` 已排 BARS（D283），盘后 bars job 也盖 universe，
     这句是真的。
   * **区间缺段**（全库有、这段少）：`…把区间挪到有数据的那一段；缺的日线盘后任务会
     自动补采`。
   去掉 `sync_hint` 变量与三处 `先跑/再跑 {sync_hint}`。
2. **不在 CLI 补回命令（与 D343 归因不同，与 D344 同）。** CLI 的 `NotAdjustable` 出口是
   **多态**的：覆盖确认、无法折算、裸价变动、缺收盘价等都走同一个 `except`，盲目补一句
   `sams sync daily` 会给「该补公司行为」的拒绝指错命令。没有安全的补回点，故不补。
   具体命令仍在 `sams sync daily --help`；覆盖缺失那一支 CLI 已单独给 `sams sync actions`
   （`cli/analysis.py:355-360`），不受影响。
3. **不新增队列活。** BARS 开页已排（D283）+ 盘后 job，去命令即可。
4. **守卫不动。** `_require_ordered`（起日晚于止日）那条 `ValidationError` 本就不带命令，
   不碰；它正是为了不让空区间文案教人跑一条同样空的命令（D 见 test_analysis.py:201）。

## 契约

* `services/analysis.py::_why_no_bars`：删 `sync_hint`，三分支按 §1 改写。函数签名与
  其余逻辑不动。
* 模板、app.js、CLI、JSON 出口均无需改（原样转述 `str(exc)`/`note`）。

## 测试

* `tests/services/test_analysis.py`：
  * `test_a_code_the_database_has_never_seen_says_the_code_is_unknown`：追加
    `assert "sams sync daily" not in str(exc.value)`（代码错那一支不承诺自动补，也不印命令）。
  * `test_a_registered_code_with_no_bars_says_it_has_never_been_collected`：追加
    `assert "sams sync daily" not in str(exc.value)` 与 `assert "盘后" in str(exc.value)`。
  * `test_a_window_outside_the_collected_range_says_where_the_bars_are`：追加同样两条。
* `tests/web/`（个股 K 线接口）：新增一条——主档无此码、无日线，`GET /api/symbol/A/000001/bars`：
  JSON `note` 含「还没采集过」「盘后」，不含 `sams sync daily`（开页 ensure 会建主档行，
  故走「从未采集」支）。

## 验证

* 串行 pytest（`test_analysis.py` + 个股 K 线 web 测试 + `test_cli.py` 确认 CLI 出口仍绿）、
  `ruff check src tests`。
* 生产库副本 + 临时服务 8799：挑一只无日线的主档票（或构造），K 线接口 note 无命令、
  有盘后自动补采。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

2026-11-12 实测（TDD：先改三处服务断言 + 新增网页测试见证 RED——4 failed，三分支与
K 线接口 note 都含 `sams sync daily`，再实现 GREEN）。

* 串行 pytest：`test_analysis.py` + `test_analysis_series.py` + `test_symbol.py` +
  `test_cli.py` 共 **171 passed**（CLI 各 `NotAdjustable` 出口仍绿——中性注记原样打印）；
  `ruff check src tests` All checks passed。
* 生产库副本（`tmp/d346.duckdb`）+ 临时服务 8799，登录 sp1check：在副本上查得代码表在册
  但无日线的 A 股 605177（东亚药业），`GET /api/symbol/A/605177/bars?days=120` 的 JSON
  `note` 印「A/605177（东亚药业）在主档里，但全库一根 K 线都没有：还没采集过；盘后任务
  会自动补采这只票的日线」——`sams sync daily` **不在**文中，「盘后」「还没采集过」在。
* 该注记 web + CLI 共用，CLI 经多态 `NotAdjustable` 出口原样打印中性句（无安全补回点，
  不补回），已由 `test_cli.py` 覆盖。验证毕清理副本；生产 `sams serve` 重启后
  `/healthz`、`/login` 均 200。