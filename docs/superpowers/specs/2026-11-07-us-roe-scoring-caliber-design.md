# 美股 ROE 评分复用市场上界（D334）

## 问题与证据

D228 已将美股 ROE 的可信绝对值上界设为 5.0，其他市场仍是 1.0，达到边界即拒绝。
D333 将市场传入 PR，但 `roe_score()` 仍直接使用默认上界。

通过 `ValuationService.read("us", "aapl")` 的回归用例复现：报告期 2023-09-30、
源标年报、披露日为空、ROE=1.714 的美股财务行被正常读取，PR 有值，但 ROE 分项为
None，并退出合成权重。测试在 `None == 100.0` 处失败，不是数据未入库或财年识别失败。

## 决策

采用最小透传方案，而不是整体放宽默认上界或另建市场评分表：

- `analysis/valuation/scoring.py` 的 `roe_score(value, *, period="", market=None)`
  将市场传入 `out_of_caliber`，拒绝理由通过 `ratio_bound` 显示实际生效的上界。
- `services/valuation.py` 的 `_items()` 从 `FundamentalRead.market` 传入规范化市场。
- 默认调用、A 股、港股保持上界 1.0；美股采用既有上界 5.0。
- 有效的负 ROE 仍得 0 分，不是缺失；高正 ROE 按原锚点封顶 100 分，不增加奖励。
- 无数据、越界拒绝、合成分的覆盖权重规则均保持不变。

不改数据库、采集、配置、评分锚点、增速评分或页面布局。本项可经 CLI 和规则读数生效；
当前 Web 估值区使用分位接口，不是合成评分接口，不能声称修复了 Web 评分页。

## 验证

1. 服务回归先红后绿：US 非 12-31 年报、无披露日、ROE=1.714；PR 有值，
   ROE 分项为 100、不再位于 `composite.missing`，证据保留实际 ROE。
2. 评分边界：US 1.0、1.714、4.999 有分；±5.0 及更大绝对值拒绝且显示上界 5；
   有效负 ROE 得 0；默认/A/HK 的 ±1.0 仍拒绝，0.20 仍为 85，None 仍缺失。
3. 串行运行评分、估值服务、CLI 及相关规则回归；运行 `ruff check src tests`。
4. 用正式 DB 与 WAL 的临时副本运行实际 `sams analysis valuation` 命令，检查
   AAPL 的 ROE 分项及缺失项。不得直接打开线上数据库，或借验证修改线上账户与配置。

## 回归结果（2026-11-07）

```bash
.venv/bin/python -m pytest tests/analysis/test_valuation_scoring.py tests/analysis/test_valuation_percentile.py tests/analysis/test_pr.py tests/services/test_valuation_read.py tests/services/test_fundamentals_read.py tests/services/test_rules_snapshot.py tests/services/test_rules_recheck.py tests/cli/test_valuation_cli.py -q -p no:cacheprovider
.venv/bin/ruff check src tests
```

185 passed，100 条既有 DuckDB Arrow 复制提示；ruff 通过。本次未重跑全库测试。
独立代码审查未发现阻塞项。

## 实际命令验证（2026-11-07）

在正式 DB 与 WAL 的临时副本上运行：

```bash
sams analysis valuation --market US --code AAPL --as-of 2026-11-06 --json
sams analysis valuation --market HK --code 00700 --as-of 2026-11-06 --json
```

- AAPL 在评分之前拒绝：2025-09-29~2026-11-05 缺公司行为覆盖确认，因此不能声称
  正式数据的美股完整评分链路已跑通；未修改覆盖记录或绕过校验。
- 00700 的 ROE=0.2113474644，分项为 86.7021，未被误列入缺失。
  命令仍返回 1，因为买入区缺 PR 历史分位，不是 ROE 分项失败。
- 另在独立测试库中铺入有覆盖确认的 300 日日线、非日历年报、无披露日、ROE=1.714，
  用真实 `sams analysis valuation` 进程分别跑文本和 JSON。两种输出均给 ROE 100 分、
  权重 15%，缺失列表不含 ROE；合成分为 76.0461。测试数据不代表当前 AAPL 实际评分。
  命令仍返回 1：无披露日的历史 PR 分位拒绝，买入区无法判断。

## 单独跟进

历史 PR 分位仍要求披露日，并用日历 12-31 识别年报；美股无披露日或非 12-31 财年
可能没有 PR 分位。这与当前 ROE 分项的市场上界是两个问题，本次不放宽历史 PIT。
AAPL 公司行为覆盖缺口也保留为独立的后台补采问题。
