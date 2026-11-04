# 美股年报字段与 ROE 兜底（D228）

**日期**：2026-10-30
**起因**：用户提出三件事——「HK 没有「年报 ROE」这个字段（它只供 A），这种问题也需要
解决」；「ROE 也是可以计算出来的」；「美股也需要考虑这个问题」。D226 当时把美股年报
字段整体关闭，理由是「没有带披露日的财务源」；D225 只给港股开了 PEG 一个口。三项一并
收口：年报字段三个市场都进词表，缺 ROE 时有可复核的兜底公式，两处口径都写在屏幕上。

## 结论

### D228：美股年报走东财 F10，年报按报告类型认，ROE 可兜底

1. **美股财务源改用东财 F10 数据中心**（`eastmoney_us_f10`）。三张报表按 `ORG_TYPE`
   路由：一般企业 `RPT_USF10_FN_GMAININDICATOR`、银行 `…_BMAININDICATOR`、
   保险 `…_IMAININDICATOR`。一次同步两次请求：先用
   `RPT_USF10_DATA_MAININDICATOR` 按代码查到 `SECUCODE`（如 `AAPL.O`）与公司类型，
   再按 `SECUCODE` 拉指标。
   yfinance 不选：`Ticker.income_stmt` 实测为空，`history` 触发
   `YFRateLimitError`，而且按报告期的表没有披露日。

2. **只入年报**。源给 `DATE_TYPE_CODE` / `DATE_TYPE`，只收 `001` / `年报` 的行：
   季报的 ROE、毛利率与 PE(TTM) 不是同一周期的量。实测苹果 FY2025 年报报告期是
   `2025-09-27`，本季报是 `2026-06-27`，两条若混在一起，「年报 ROE」就是假话。

3. **年报口径按 `report_type` 认，不按 12-31 认**。新增
   `FISCAL_YEAR_ANNUAL_MARKETS = {"US"}`，`_basis_clause` 对这些市场写
   `report_type = '年报'`；A 股与港股仍按 `month(report_date)=12 AND day=31`（D62）。
   按日历日判会把苹果这种 9 月末结账的每一行都排除掉。

4. **披露日留 NULL**。实测 `NOTICE_DATE` 是老数据的入库日，不是 10-K 的公开日；
   拿它当 `published_date` 会让 PIT 在回测里提前看到未来。美股因此加入
   `UNDATED_FINANCIAL_MARKETS`：读取按 `report_date <= 快照日` 采信，屏幕上写明
   「没有可核对的披露日」。这与港股 D215 同一条开口，不新开第二条红线。

5. **ROE 口径闸按市场放宽**。默认 `RATIO_BOUNDS["roe"] = 1.0` 是为「百分数没 ÷100」
   设的，而美股回购把股东权益压得很小，苹果 2025 年报 ROE = 171.4% 是真实值。
   新增 `MARKET_RATIO_BOUNDS[("US","roe")] = 5.0` 与 `ratio_bound(column, market=)`，
   仓库写入、选股引擎与兜底公式共用 `out_of_caliber(..., market=)` 这一道闸。
   上界 5.0 仍拦得住 171.4、35.0 这种真正没除数的写法。

6. **年报词表对三个市场开放八个字段**：ROE、营收同比、净利同比、毛利率、净利率、
   ROA、资产负债率、流动比率。三个市场的源都提供这些列（港股用
   `RPT_HKF10_FN_MAININDICATOR`，美股用上面三张表）。**股息率与 ROIC 仍只供 A**：
   港股适配器刻意不塞 `DIVIDEND_RATE`（口径未核实），两条源都不给 ROIC；词表说
   「能选」就等于引擎认，没有数的列必须灰着。PEG 对美股开放（快照有 PE(TTM)、
   年报有净利同比），口径说明改写为「港股与美股的财务源没有披露日」。

7. **ROE 兜底公式**：源没给 ROE，而同一期年报里 ROA 与资产负债率都在时，引擎用
   `ROE ≈ ROA ÷ (1 − 资产负债率)` 估一个。三条守卫：
   * 源值优先——兜底只补缺口，不改写源值；
   * `debt_ratio < 1.0`：分母 ≤ 0 时公式没有意义；
   * 估算结果必须过同一个口径闸：在帧上重写 `abs(x) >= 1` 会让美股的 171% 在兜底
     这条路上被静默置空（D66 的闸只有一个出处）。
   只要真的估出来了，结果的 `basis_note` 就带上「ROE 有 N 只是按 ROA ÷
   (1−资产负债率) 估算」，读者分得清源值与推算值。`roe` 字段的 `needs`
   声明 `("roa", "debt_ratio")`，引擎据此多 join 两列（D222）。

8. **「无数」的理由按市场说**。A 股：「没有已披露年报里的…」；港股/美股：
   「没有截至快照日能取到的年报里的…」——这两个市场没有披露日可核，同一句话在
   那里是无法验证的另一句。ROE 在「有年报、但兜底输入不齐或结果不可信」时给专门
   理由（公式与三个前提），而不是指向「去同步」：那一期已经在库里。

## 为什么不是别的做法

* **不用「报告期 + 45 天」猜披露日**：D61 的红线。港股只开了一个字段（PEG 的净利
  同比）就已经最小，不把它扩大到其他年报字段。
* **不把美股 ROE 上界直接改成 5.0**：A 股 1.0–5.0 那段死区里正好是周期底部的真实
  ROE，全市场放开等于把「百分数没 ÷100」放回来。放宽只针对 `(US, roe)`。
* **不把财务源当成 PR 的输入**：`pr_now` 仍只供 A（D188），美股 PB 也未注册
  （D226）。本次只解决年报字段与 PEG，不改 PR 的票池。

## 涉及的代码

| 文件 | 改动 |
| --- | --- |
| `src/sams/datasource/eastmoney_us_f10.py` | 新增：三张报表路由、只收年报、披露日留 NULL、注入防护 |
| `src/sams/storage/migrations/0030_us_financials.sql` | 把 `eastmoney_us_f10` 种进 `data_sources` 与 US 路由链尾 |
| `src/sams/core/fundamentals.py` | `MARKET_RATIO_BOUNDS`、`ratio_bound`、`FISCAL_YEAR_ANNUAL_MARKETS`、市场化 `out_of_caliber` |
| `src/sams/storage/repos/financials.py` | 年报口径按市场分支；口径闸逐行带市场 |
| `src/sams/core/screener_fields.py` | 八个年报字段与 PEG 对 HK/US 开放；`roe.needs`；两市场口径说明 |
| `src/sams/services/screener.py` | `UNDATED_FINANCIAL_MARKETS` 加 US、分市场口径说明、ROE 兜底与理由 |
| `tests/datasource/test_eastmoney_us_f10.py` | 14 条适配器用例（真实 AAPL/JPM/PGR 响应形状） |
| `tests/storage/test_financials_*.py` | 美股高 ROE 过闸、财年年报认 `report_type` |
| `tests/core/test_screener_fields.py`、`tests/services/test_screener.py` | 三市场词表、兜底公式、口径说明与「无数」理由 |

## 数据更新与验证

```
sams sync financials --market US --all     # 逐只两次请求；需要在 serve 停止时跑
sams screen spot --market US --where "roe>=0.15"
sams screen spot --market HK --where "roe>=0.15"
```

已知限制：股息率、ROIC 在 HK/US 仍为「无数」；美股银行/保险的毛利率、净利率、
流动比率多数为空（源不给），这些票在对应条件上会记「无数」而不是 0。年报覆盖仍由
`financial_coverage` 报出，同步只覆盖一部分票时「命中 N 只」必须与它一起读（D227）。
