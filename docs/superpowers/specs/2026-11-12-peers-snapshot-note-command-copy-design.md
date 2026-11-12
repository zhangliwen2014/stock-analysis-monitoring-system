# 同业卡市值快照注记去命令（D345）

## 需求

R3-02 点名网页里的命令行话术。个股详情页 `/symbol/{m}/{c}` 的同业对比卡（异步局部
`/api/partials/peers`）在快照陈旧或缺失时，经**服务层字符串**印 `sams sync spot`：

* `services/peers.py::compare`（约 310-315 行）：`snapshot_fresh` 为假时往
  `PeerComparison.notes` 追加「最新快照 {seen} 距截至日超过 {SNAPSHOT_STALE_DAYS} 天或
  缺失：市值一列不出数（先跑 `sams sync spot`）」，由 `partials/peers.html` 的
  `report.notes` 原样转述。

研究确认这条是 **web-only**：`peers.compare` 的 notes 只经同业卡上网页（也喂业务卡的
`_position_draft`，仍是 web），CLI 没有打印同业 notes 的出口。

## 取舍

1. **注记改渠道中性：只说情况 + 盘后自动 job，不带命令。** 市值快照由 `run_spot`
   （`scheduler/jobs.py`，DAILY，**全市场** active 代码，`MarketSnapshotService.sync`
   整日覆盖写）每晚自动采集，不依赖队列、也不需要用户跑命令。那句尾巴改成
   `市值一列不出数；盘后任务会自动采集最新快照`。前半句（最新快照是哪天、超过几天）
   不动——它是「市值这一列为什么空」的出处。
2. **不声称「已排进队列」。** `symbol_backfills` 没有 spot 这个 kind，消费者也没有 spot
   分支；快照走的是全市场盘后 job，不是按只排队。说「已排进后台补采队列」会是假承诺
   （D336）。这里只说盘后自动采集。
3. **不在 CLI 补回命令。** 这条 web-only，没有 CLI 消费者要保（与 D344 同理）。
4. **只改快照陈旧那一支。** 同函数其余 notes（行业放宽、未披露期、兜底排序）本就不带
   命令，不动。`peers.py:493/644`（营收/行业缺失）涉及「概况行在但字段空」与「行缺失」
   的分情况，另立目标处理，本轮不碰。
5. **不新增队列活。** `run_spot` 已是自动 job，去命令即可。

## 契约

* `services/peers.py::compare`：`if not snapshot_fresh:` 那一支的注记尾巴按 §1 改写
  （`…市值一列不出数；盘后任务会自动采集最新快照`），去掉 `（先跑 sams sync spot）`。
* 模板、CLI、JSON 出口均无需改。

## 测试

* `tests/services/test_peers_service.py::test_without_a_snapshot_market_cap_is_missing_and_selection_uses_revenue`：
  `assert "sams sync spot" in "；".join(report.notes)` → `not in`；追加
  `assert "盘后" in "；".join(report.notes)`；市值全 None 与同业集合断言保留。
* `tests/web/test_symbol_fundamentals.py`：新增一条——`_six_baijiu(ctx,
  snapshot_day=today-20天)`（超过 `SNAPSHOT_STALE_DAYS=7`），`GET /api/partials/peers?market=A&code=600519`：
  断言「市值一列不出数」在、`sams sync spot` 不在、「盘后任务会自动采集」在。

## 验证

* 串行 pytest（`test_peers_service.py` + `test_symbol_fundamentals.py`）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：挑一只快照陈旧/缺失的 A 股（或构造），同业卡注记无命令、
  有盘后自动采集。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

2026-11-12 实测（TDD：先转换服务断言 + 新增网页测试见证 RED——2 failed，服务注记与
同业卡都含 `sams sync spot`，再实现 GREEN）。

* 串行 pytest：`test_peers_service.py` + `test_symbol_fundamentals.py` +
  `test_back_links_web.py` + `test_screen_web.py` 共 **104 passed**；`ruff check src tests`
  All checks passed。
* 生产库副本（`tmp/d345.duckdb`）+ 临时服务 8799，登录 sp1check：生产 A 股快照新鲜
  （最新 2026-11-11），600519 同业卡不触发陈旧注记；停服务后在副本上删除 A 股
  `snapshot_daily` 中 day>2026-11-01 的行（最新降到 2026-10-30，距截至日 13 天 > 7），
  重启服务后 `GET /api/partials/peers?market=A&code=600519` 印「最新快照 2026-10-30
  距截至日超过 7 天或缺失：市值一列不出数；盘后任务会自动采集最新快照」——
  `sams sync spot` **不在**文中，「盘后任务会自动采集」「市值一列不出数」在。
* 该注记 web-only，无 CLI 消费者，不需 CLI 回归。验证毕清理副本；生产 `sams serve`
  重启后 `/healthz`、`/login` 均 200。