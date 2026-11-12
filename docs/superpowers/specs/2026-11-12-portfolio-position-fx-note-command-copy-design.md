# 组合页持仓缺汇率注记去命令（D344）

## 需求

R3-02 点名网页里的命令行话术。D342 清了汇率表单提示（`_rate_hint`）、D343 清了归因卡
与限额回撤注记，但组合页还有一处经**服务层字符串**渲染上网页的 `sams sync fx`：

* `services/portfolio.py::_rates`（约 316-320 行）：某笔持仓的计价币种换不成报告币种
  （`fx.latest` 取不到行情日或之前那条汇率）时，给一句话挂在 `PositionRow.unavailable_note`
  上，并并进 `AccountSummary.notes`。现在那句尾巴是「这一行不计入汇总（可运行
  `sams sync fx` 或手工补一条）」。

它经 `pages/portfolio.py::_account_view` 上 `/portfolio`，由 `portfolio.html` 的
`row.note` 与账户 `notes` 两处原样转述；同一份字符串也被 CLI `sams ledger positions`
（`cli/ledger.py:476`）与 `sams ledger summary`（`:506`）打印。

## 取舍

1. **注记改渠道中性：只说情况 + 盘后自动 job，不带命令。** `run_fx` 已按币种对自动补
   汇率（D342 确认），缺汇率不是「没人跑命令才没有」。那句尾巴改成与 D342 汇率提示
   同一口径：`这一行不计入汇总；盘后任务会自动补这一对的汇率`。前半句（哪只票、什么
   币种、取不到哪天的汇率）不动——它是「这个合计为什么不完整」的出处，必须保留。
2. **不在 CLI 文本补回命令（与 D343 归因不同）。** 归因 CLI 的模块契约写明「算不出来时
   那句话里带着那条命令」，且有测试钉着；这条持仓注记没有这种 CLI 命令契约
   （`tests/cli` 里钉 `sams sync fx` 的是 `sams health`，与本注记无关）。CLI 的手工出口
   `sams ledger fx set` 已在 `--help`（`cli/ledger.py:129`）里说明，注记只需说清情况与
   自动补。注记在 CLI 有两处打印点（持仓行 / 汇总尾行）且与「缺基准汇率」「重复持仓」
   等注记混排，按打印点补命令既脆弱又无契约要求，不做。
3. **只改缺汇率那一支。** 同函数里「缺买入基准汇率」那一支（约 322-324 行）本就不带
   命令，不动。
4. **不新增队列活。** 与 D342/D343 同：`run_fx` 已是自动 job，去命令即可。

## 契约

* `services/portfolio.py::_rates`：`found is None` 那一支的注记尾巴按 §1 改写
  （`…这一行不计入汇总；盘后任务会自动补这一对的汇率`），去掉 `可运行 sams sync fx
  或手工补一条`。前半句与函数其余部分不动。
* 模板、CLI、JSON 出口均无需改（原样转述 `unavailable_note`/`notes`，只是字符串里
  不再有命令）。

## 测试

* `tests/services/test_portfolio_service.py::test_a_usd_position_without_a_rate_contributes_nothing`：
  追加 `assert "sams sync fx" not in row.unavailable_note` 与 `assert "盘后" in
  row.unavailable_note`；原有「汇率」断言保留。
* `tests/web/test_portfolio_web.py`：新增一条——开 CNY 户、买一笔 US 票（带买入基准
  汇率）、落 US 日线但**不存** USD→CNY 汇率，`GET /portfolio`：断言页面持仓注记里
  `sams sync fx` 不在、「盘后任务会自动补」在、「这一行不计入汇总」在。

## 验证

* 串行 pytest（`test_portfolio_service.py` + `test_portfolio_web.py` + `test_cli.py`
  确认 health 那条仍绿）、`ruff check src tests`。
* 生产库副本 + 临时服务 8799：构造一笔缺汇率的外币持仓（在副本上），组合页持仓注记
  无命令、有盘后自动补；CLI `sams ledger positions` 对同一账户印同一句中性注记。
* 生产 `sams serve` 重启后回归 `/healthz` 与 `/login`。

## 回归结果

（待填）