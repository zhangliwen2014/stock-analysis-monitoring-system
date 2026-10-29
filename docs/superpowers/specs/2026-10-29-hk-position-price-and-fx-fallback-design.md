# 港股持仓「取不到现价」与汇率断供的修复设计（2026-10-29）

## 症状

`/portfolio` 上港股账户「文-中国银河」的两行持仓：

- `HK 01021 取不到现价（库里没有行情），这一行不计入盈亏`
- `HK 03690 取不到现价（库里没有行情），这一行不计入盈亏`

## 根因（两件事，任一件都足以让这一行不计入）

1. **日线没进库**。这两笔成交在 `symbol_interest` 里排了补数，但补数在**下一拍调度
   tick 的开头**才消费；那一拍被港股日线链上反复 429 的 yfinance 拖成了 81 分钟
   （心跳 12:28:49 → 13:49:31，`tick=60s`）。补数还没轮到，行情自然没有。
2. **汇率表是空的**。`fx_rates` 一行都没有：汇率链上只有 yfinance 一家提供
   `fx_rate`，而它此刻整条被限流。就算日线补上，HKD 计价的持仓仍会换成另一句话
   「取不到 HKD→CNY 汇率：这一行不计入汇总」。

## 修复

### A. 数据：把两只港股的不复权日线补齐（运维动作）

腾讯这一路可用（`yfinance✗ → tencent✓`），已用
`sams sync daily --market HK --code 03690 --code 01021 --start 2026-07-01 --end 2026-10-29`
各补 84 根，`symbol_backfills` 两行随之 `done`。

### B. 代码：新浪汇率日线进降级链（新能力）

给 `SinaAdapter` 加 `fx_rate`。源是新浪外汇的日 K JSONP：

`https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_{symbol}=/NewForexService.getDayKLine?symbol={symbol}`

- 代码形态 `fx_s` + 两个币种小写，**方向由拼接顺序决定**：`fx_shkdcny` 与
  `fx_scnyhkd` 是两个不同的序列。所以解析时核对响应里的变量名必须是请求的那一个，
  对不上算改版（`FieldDrift`），不把反方向的数收下。
- 一行五格：`日期,开盘(昨收),最低,最高,收盘,`。**取收盘（第 5 格）**，
  它与新浪即时报价的「昨收」逐日对齐（0.8496682046 两边一致）。
- 全史约 3981 行（2014-11 起）；`registry._call_fx` 已按 `[start, end]` 裁剪，
  适配器不再自带窗口逻辑。
- 覆盖 `X→CNY` 与 `CNY→X` 两个方向；其它组合源不提供时回 `EmptyResponse`，
  链继续问 yfinance。
- 传输错误照旧 `classify_error`；429 归 `RateLimited` 让注册表退避重试。

这样即使 yfinance 整条被限流，`sams sync fx` 与每晚的 `fx` job 也能落库；
`PortfolioService` 锚在行情日的换算（D73）随之成立。

## 明确不做

- **不改用户那两笔成交**。两笔 HK 买入的 `fx_rate_ref` 都是 `1.0`（用户录入时的
  基准汇率），按股数加权后 `fx_pnl` 会把 HKD 当 CNY 算，出现约 −15% 的假汇兑损益。
  按「录错就撤销重录」的既有出口（D205）处理，系统不静默改账。
- **不取倒数**。新浪两个方向都直接给；缺哪个方向就换源，不拿 1/rate 冒充。
- **不引入中行牌价**：`currency_boc_sina` 的折算价与即期差约 0.8%，混进同一条
  `fx_rates` 序列会让 `fx_pnl` 在换源那天跳一下。

## 测试

- 解析：收盘取第 5 格、日期落成 `date`、`source=sina`、方向两向各一例。
- 变量名不匹配（把 `fx_shkdcny` 的响应当 `CNY→HKD` 用）→ `FieldDrift`。
- 空行体 → `EmptyResponse`（是答复，链继续）。
- 适配器：请求的 symbol 跟随方向；连接错误 → `TransientError`。
- 注册表：yfinance 抛 `RateLimited` 时链落到 sina，`source=sina`、`degraded=True`。
- live：最近两周的 `HKD→CNY` 至少一行、为正数。

## 验收

1. `sams sync fx` 在真实库落下 `HKD→CNY` 与 `USD→CNY` 若干行。
2. `/portfolio` 两行港股给出 HKD 现价，价格段进汇总；若成交明细的基准汇率为 1.0，
   换汇段会明显负——那是录入值，不是读数错。
3. `sams health` 的 `[账本]` 一节给出 HKD→CNY 的最后一天。
