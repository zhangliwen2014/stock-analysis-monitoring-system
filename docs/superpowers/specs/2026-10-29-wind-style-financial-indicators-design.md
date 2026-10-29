# 个股页主要财务指标：Wind 式多期表（D217 / D218）

- 日期：2026-10-29
- 状态：设计已确认，待实施
- 关联：D58（库里只有小数口径）、D60（报表期与当日倍数分开）、D61（PIT）、
  D214–D216（F10 资料块）、D215（港股指标无披露日，只给看数）

## 1. 目标与边界

把个股页「主要财务指标」从现在的「8 期 × 5 列」改成 Wind 金融终端那种
**指标做行、报告期做列**的多期表：一眼横着读趋势，竖着看同一期的规模、增长、
盈利、每股、偿债与运营。

做到：

- A 股 13 项新指标 + 原有 ROE/营收同比/净利同比/股息率，共 17 行；
- 港股能从同一族东财接口拿到的项全部上榜，拿不到的留「—」；
- 「全部报告期 / 仅年报」一键切换；
- 金额按亿元、比率按百分数、倍数与周转率按「倍/次」，单位写进指标名；
- 披露日单独一行（A 有，港股「未给」并保留 D215 脚注）。

不做（本轮明确排除）：

- **不做营收/净利/ROE 趋势图**（用户选择先看表；图表另起一轮）；
- **不做利润表/资产负债表/现金流量表**：那是另一套数据契约、存储与口径核实；
- **不把新指标接进选股、市赚率、规则**：它们是展示列，不是 PIT 口径；
- **不做美股**：现有链上没有 F10 财务源，继续沿用「本地没有 + 命令」的空态；
- **不补港股股息率**：`DIVIDEND_RATE` 的时点与口径仍未核实（D215 的同一条底线）。

## 2. 现状与差距

当前 `financials` 表只存 `roe / revenue_yoy / profit_yoy / dividend_yield / market_cap`，
个股页的 `recent(limit=8)` 直接摊成一张窄表。源其实一次给 141 列（A）或 80 余列（HK），
我们只取了 4 个比率——Wind 感缺的是「同一期到底多大、多赚、欠多少」这几组数。

## 3. 数据：字段映射（只取可核对的口径）

### 3.1 A 股（东财 F10 `RPT_F10_FINANCE_MAINFINADATA`，经 akshare）

| 展示名 | 源字段 | 存储列 | 单位/换算 |
|---|---|---|---|
| 营业总收入 | `TOTALOPERATEREVE` | `revenue` | 本币原值，显示 ÷1e8 为亿元 |
| 归母净利润 | `PARENTNETPROFIT` | `net_profit` | 同上 |
| 营收同比 | `TOTALOPERATEREVETZ` | `revenue_yoy`（已有） | 源百分数 ÷100 |
| 归母净利同比 | `PARENTNETPROFITTZ` | `profit_yoy`（已有） | 源百分数 ÷100 |
| ROE | `ROEJQ` | `roe`（已有） | 源百分数 ÷100 |
| 总资产净利率 ROA | `ZZCJLL` | `roa` | 源百分数 ÷100 |
| 投入资本回报率 ROIC | `ROIC` | `roic` | 源百分数 ÷100 |
| 毛利率 | `XSMLL` | `gross_margin` | 源百分数 ÷100 |
| 净利率 | `XSJLL` | `net_margin` | 源百分数 ÷100 |
| 基本每股收益 | `EPSJB` | `eps` | 元/股，原值 |
| 每股净资产 | `BPS` | `bps` | 元/股，原值 |
| 每股经营现金流 | `MGJYXJJE` | `ocf_per_share` | 元/股，原值 |
| 资产负债率 | `ZCFZL` | `debt_ratio` | 源百分数 ÷100 |
| 流动比率 | `LD` | `current_ratio` | 倍，原值 |
| 速动比率 | `SD` | `quick_ratio` | 倍，原值 |
| 存货周转率 | `CHZZL` | `inventory_turnover` | 次，原值 |
| 总资产周转率 | `TOAZZL` | `total_asset_turnover` | 次，原值 |
| 报告期类型 | `REPORT_TYPE` | `report_type` | 年报/中报/一季报/三季报 |
| 披露日 | `NOTICE_DATE` | `published_date`（已有） | date |

应收账款周转率（`YSZKZZL`）不取：实测小分母会给出 5 万次这种数，屏幕上只会误导。

### 3.2 港股（东财 `RPT_HKF10_FN_MAININDICATOR`）

| 展示名 | 源字段 | 存储列 | 单位/换算 |
|---|---|---|---|
| 营业总收入 | `OPERATE_INCOME` | `revenue` | 本币原值 |
| 股东应占溢利 | `HOLDER_PROFIT` | `net_profit` | 本币原值 |
| 营收同比 | `OPERATE_INCOME_YOY` | `revenue_yoy`（已有） | 源百分数 ÷100 |
| 股东应占溢利同比 | `HOLDER_PROFIT_YOY` | `profit_yoy`（已有） | 源百分数 ÷100 |
| ROE | `ROE_AVG` | `roe`（已有） | 源百分数 ÷100 |
| ROA | `ROA` | `roa` | 源百分数 ÷100 |
| 毛利率 | `GROSS_PROFIT_RATIO` | `gross_margin` | 源百分数 ÷100 |
| 净利率 | `NET_PROFIT_RATIO` | `net_margin` | 源百分数 ÷100 |
| 基本每股收益 | `BASIC_EPS` | `eps` | 港元/股，原值 |
| 每股净资产 | `BPS` | `bps` | 港元/股，原值 |
| 每股经营现金流 | `PER_NETCASH_OPERATE` | `ocf_per_share` | 港元/股，原值 |
| 资产负债率 | `DEBT_ASSET_RATIO` | `debt_ratio` | 源百分数 ÷100 |
| 流动比率 | `CURRENT_RATIO` | `current_ratio` | 倍，原值 |
| 报告期类型 | `REPORT_TYPE` | `report_type` | 归一成 年报/中报/一季报/三季报 |
| 披露日 | — | `published_date` | **NULL**（D215，不猜） |

港股拿不到的：速动比率、存货周转率（源给的是「天」不是「次」）、总资产周转率
（同为天数）、ROIC（`ROIC_YEARLY` 是年度口径，与季度列混排会撒谎）。这些列留 NULL，
屏幕显示「—」，不拿天数倒数硬凑周转率。

## 4. 存储：扩展 `financials`，不另开表（D217）

迁移 `0028_financial_indicators.sql` 给 `financials` 加 14 个可空列：

```
report_type TEXT
revenue, net_profit, eps, bps, ocf_per_share DOUBLE
gross_margin, net_margin, roa, roic, debt_ratio DOUBLE
current_ratio, quick_ratio, inventory_turnover, total_asset_turnover DOUBLE
```

理由：

1. **同一实体**：粒度仍是 `(market, code, report_date)`，一份报表一期，不该拆成两张
   同键同期的表；
2. **PIT 不动**：`load_as_of` 的闸门只有 `published_date`，新列不参与任何筛选；
   `financials` 早就有展示列（`dividend_yield`、`market_cap`）的先例；
3. **只取一次数**：A/HK 适配器已经在解析同一份 payload，加列不增加网络调用；
4. **读侧零改动**：`recent()`/`history()` 都是 `SELECT *`，新列自动带出。

约束：

- 新比率列进各自适配器的 `percent_columns`，库里仍然只有小数（D58）；
- 金额列按本币原值存，币种取 `symbols.currency`，不在库里换算；
- 新列**不进** `screener_fields.FIELDS`，选股与市赚率看不到它们；
- 新列缺失按「可选」处理：映射列不存在时补 NULL 并 warn，不让展示指标拖垮
  ROE/增速这条 PIT 链；原有四个必需列仍然缺一即 `FieldDrift`。

## 5. 读侧：`F10Service.read` 多一个视图参数

```python
@dataclass(frozen=True)
class F10Read:
    ...
    financials: tuple[dict, ...]      # 已按视图裁剪、报告期倒序
    annual: bool = False
    periods: int = 0                  # 本次摆了几列，模板不再数
```

- 原始取数：`financials.recent(market, code, limit=24)`（约 6 年季报）；
- `annual=True`：只留「年报」列（`report_type == "年报"`；老行无 `report_type` 时按
  `report_date.month == 12` 兜底），取最近 5 期；
- `annual=False`：取最近 6 期（季报/中报混排，Wind 的「报告期」默认视图）。

行值格式化在模板层做：金额 `value / 1e8` 保留两位；比率走现有的 `ratio_pct`；
倍数/周转率保留两位；每股原值保留两位并带币种名；NULL 一律「—」。

## 6. 页面：一张表 + 一个开关（D218）

`#f10-financials` 卡片内：

1. 估值行不变（最近交易日 PE/PB/股息率/市值）；
2. 开关：`全部报告期` / `仅年报`，用 htmx 局部刷新，只换这张卡；
3. 表格：

```
指标                 2026-06-30   2026-03-31   2025-12-31 ...
                     中报         一季报       年报
营业总收入（亿元）      922.78       401.24       ...
归母净利润（亿元）      445.17       ...
营收同比              1.30%        ...
...
披露日                2026-08-15   2026-04-25   ...
```

- 指标按五组排：规模 / 增长 / 盈利能力 / 每股 / 偿债与运营，组间用一行细标题隔开；
- 首列 `position: sticky`，窄屏横向滚动（新 CSS 类 `.fin-table` / `.fin-scroll`）；
- 港股在表格下保留 D215 脚注；A 股不显示；
- 没有 `report_type` 的老行按报告期月份推标签，不显示空白表头；
- 完全没有财务行时，空态与命令不变（「本地还没有这只票的财务指标。采一次：…」）；
- 美股：没有新列，表格只显示源能给的行，其余「—」；无行时沿用现有空态。

接口：

- `GET /api/partials/f10?market&code` 首次加载四张卡（不变）；
- 新增 `GET /api/partials/f10/financials?market&code&annual=0|1`，只返回这张卡，
  供开关局部替换；
- 两个入口同一套归一/404（`_require_symbol`），键形态只有一种（D50）。

## 7. 诚实与降级

- 源改版导致新列整列消失：warn 一次，新列 NULL，页面「—」；ROE/增速仍照常入库，
  不让「看数」的字段把「选股」的字段一起拖下水；
- 迁移后、重采前的老行：新列全是 NULL。上线顺序里安排一次重采，避免用户先看到
  一屏「—」；
- 港股 `published_date` 仍为 NULL：它们继续不参与 PR/选股（D215）；
- 页面不显示「约」「近」这类估算词；源没给就是「—」。

## 8. 测试

- 迁移 0028：新列存在且可空，老库迁移不丢行；
- 仓库：新列写入/读出往返，`_empty` 形状包含新列；
- 适配器：A 14 列映射与 ÷100 只发生一次；HK 映射与缺失列留 NULL；`report_type` 归一；
- 服务：`annual=True` 只留年报、老行按 12 月兜底、列数上限；
- 网页：多期表头与指标行、金额亿元、比率百分数、NULL「—」、开关 URL、港股脚注、
  A 股无脚注、US 空态不变；
- 分层与版本：`tests/test_layering.py`、`tests/services/test_version_bumps.py` 照旧通过。

## 9. 上线顺序

1. 迁移 0028（停服执行，DuckDB 单写）；
2. `sams sync financials --market A`（504 只同步池）与 `--market HK`（3 只）重采，
   把新列填上；
3. 起服，抽查 A 600519、HK 00700、US AAPL 三张卡；
4. 夜间 `financials` job 从此自动带新列，无需新增调度任务。
