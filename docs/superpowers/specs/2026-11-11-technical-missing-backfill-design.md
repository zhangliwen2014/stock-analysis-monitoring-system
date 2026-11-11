# 技术面缺日线/公司行为的下一步：同一队列，三种活（D337）

## 需求

R3-02 的原话：`/screen` 与个股 F10 财务卡在缺数据时印一句
`先跑 sams sync financials --market A --code <代码>`。D336 已经把那一种活接到
`symbol_backfills` 上了，但选股页的技术面缺口还留着两句「下一步」：

* `TechnicalService._need()` 里写着
  ``先跑 `sams sync bars --market A --code <代码>` ``；
* `NO_COVERAGE` / `NOT_ADJUSTABLE` 里写着「打开该股页面会登记后台补采」「先补齐该票的
  公司行为」——前一句把动作指给了一个页面，后一句是给运维的口气，而不是事实。

这和已定方向「数据不全自动处理、不跑到用户界面」还是同一条冲突：网页用户没有终端，
`serve` 占着库时命令行也只会拿到 `DatabaseLocked`。这一片把技术面这两类缺口也接到
同一条队列上，三种活（`bars` / `actions` / `financials`）共用 D336 的形状。

## 取舍

1. **引擎说「哪种活能补」，不说「去哪跑」。** `TechnicalSnapshot` 新增逐格的
   `fixes: dict[(field, code), kind]`；`_need` / `NO_COVERAGE` / `NOT_ADJUSTABLE`
   的串改成纯事实，不再带命令、不再指页面。选股引擎把这份 kind 填进
   `MissingGroup.fix`，分组键仍是 `(field, reason, fix)`——一个组里不许一半可补、
   一半不可补。
2. **能补的三种缺口各归一种活。** 日线根数不够是 `bars`；覆盖没确认、复权被拒是
   `actions`（消费者按全历史重拉并重写覆盖确认，这是队列里唯一能做的机器动作）。
   量比遇送转、52 周无波动、均线非正、缺成交量列、港股美股「v0 只对 A 股开放」
   都不是队列活能补的，`fix` 留空。
3. **选股页沿用 D336 的按钮形状。** 一个组一个按钮、按下去才排；表单多带一个
   `kind`，POST 按提交的条件重跑引擎，取 `field` 相同且 `fix == kind` 的组。notice
   与按钮旁的说明按 kind 说话（补日线 / 同步公司行为 / 同步年报）。
4. **CLI 仍给命令。** `sams screen spot` 见到 `fix` 就追加对应的一条：`bars` →
   `sams sync bars …`，`actions` → `sams sync actions …`，`financials` 保持 D336
   的 `sams sync financials …`。JSON 出口不变。
5. **个股页不新做。** 打开个股页本来就走 `SymbolInterest.ensure`：缺日线排 `bars`、
   覆盖没确认排 `actions`（D283）。技术面缺口只出现在选股页，那里才是「没人在队列里
   替这些票登记」的地方。

## 契约

### 引擎

| 项 | 值 |
| --- | --- |
| `TechnicalSnapshot.fixes` | `(字段, 代码) → "" / bars / actions` |
| `_need(name)` 的 kind | `bars` |
| `NO_COVERAGE` 的 kind | `actions` |
| `NOT_ADJUSTABLE` 的 kind | `actions` |
| 其余技术面理由的 kind | `""` |
| `MissingGroup.fix` 取值 | `""` / `"bars"` / `"actions"` / `"financials"` |

`_need` 的新输出（不带命令）：

* `日线不足 {N} 根：技术面字段按快照日及之前的日线现算`

两条覆盖理由的新输出（不带页面指路）：

* `公司行为覆盖未确认，无法复权`
* `公司行为无法折算或有未标记跳变，复权序列被拒绝`

### 选股页

* 模板：`group.fix` 非空的分组下面出现 `把这 N 只加入补采队列` 的 POST 表单，
  隐藏字段在 D336 那一套之外多一个 `kind`；按钮旁一句
  `后台逐只{补日线|同步公司行为|同步年报}，补完刷新这一页。`
* `POST /screen/backfill`：读 `kind`，按提交的条件重跑引擎，取
  `field` 相同且 `fix == kind` 的分组，代码交给 `SymbolBackfillRepo.enqueue_many`；
  303 回同一屏，notice 读 `backfill=新排 N、已在队列 M`（文案按 kind 说哪一种活）。
* 分组已不存在（页面过期、或 `kind` 与这一组的活不符）时 notice 说
  「这一组现在不在可补采的范围里」，队列里不多行。

### CLI

`_FIX_COMMANDS` 一张表，`_print_spot` 按 `missing_group.fix` 追加
``；先跑 `<命令>` ``：

| fix | 命令 |
| --- | --- |
| `bars` | `sams sync bars --market {market} --code <代码>` |
| `actions` | `sams sync actions --market {market} --code <代码>` |
| `financials` | `sams sync financials --market {market} --code <代码>` |

## 验证

* 引擎：缺日线的组 `fix == "bars"`、理由里没有 `sams `；无覆盖的组
  `fix == "actions"`、理由里没有「打开该股页面」；量比遇送转的组 `fix == ""`。
* CLI：`--where ma20_gap>0` 的文本出口给出 `sams sync bars --market A --code <代码>`；
  无覆盖的夹具给出 `sams sync actions …`；JSON 出口不变。
* 选股页：缺日线分组出现按钮、页面没有 `sams sync bars`；POST 后队列有
  `kind=bars` 的开放行，notice 说「后台逐只补日线」；重复 POST 只报「已在队列」。
  无覆盖分组的按钮排的是 `kind=actions`。
* 串行 pytest、`ruff check src tests`；用生产库副本起临时服务，选股页点一次按钮后
  查队列行数；不操作生产通道。

## 回归结果（2026-11-11）

* 测试：`tests/services/test_screener.py`、`test_screener_technical.py`、
  `tests/cli/test_screen_spot_cli.py`、`tests/web/test_screen_web.py` 串行 122 passed；
  `ruff check src tests` 通过。
* 红：引擎三条新断言在实现前失败（`fix` 仍是空串）；CLI 两条、选股页三条在把对应
  源码回退后失败，恢复后转绿。
* 真实数据（生产库副本 `tmp/verify_d337`，临时服务 8799）：
  `ma20_gap>=0` 得两组——覆盖未确认 31 只、日线不足 4857 只。页面两个按钮，文案是
  「后台逐只同步公司行为 / 补日线」，页面没有 `sams sync bars`；第一次按日线按钮
  `queued=4857`，第二次 `queued=0&already=4857`；按公司行为按钮
  `queued=29&already=2`。停服后库里 4857 条 pending `bars` + 29 条 pending
  `actions`。
* CLI 同一份数据：两条「无数」后面分别追加 `sams sync actions --market A --code
  <代码>` 与 `sams sync bars --market A --code <代码>`。
* 生产通道未操作（副本验证）；提交后网页进程已重启，8765 上 healthz/login 均 200。
