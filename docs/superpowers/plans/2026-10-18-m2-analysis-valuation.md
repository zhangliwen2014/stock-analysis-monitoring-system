# M2 分析与估值实施计划

> **For agentic workers:** 逐任务执行，每个任务一次提交。步骤用 `- [ ]` 勾选。

**Goal:** 让 M1 的库能回答「这只票现在值不值得做」——正确的复权价、可解释的分项打分、合理区间/买入区/止损/目标/赔率，以及承载它们的 thesis 生命周期。

**Architecture:** 新增 `analysis` 层（分层守卫 §3.1 已预留，允许 import core/storage/services，被 monitor/trading 复用）。三层职责：**`datasource` 只负责把外部响应变成公司行为事实**（不猜因子）；**`storage` 只负责存事实与覆盖区间**（不解释）；**`analysis` 才做推导**（hfq 现算、指标、打分），且任何「不可信就不给数」的判定都发生在这一层并带上原因。CLI 仍是薄壳，编排走 `services`。

**Tech Stack:** Python 3.13（uv）、Polars、DuckDB、akshare/东财/yfinance/腾讯 gtimg、pytest + ruff。

**Spec:** `docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md` §8（指标与估值）、§9（thesis）、§14 M2 验收、§18 U4/U12/U13/U14。

---

## 本期的第一性问题：裸价算区间收益是错的

专家复核 #1（U12）：601088 在 2024-07-08 用裸价算区间涨跌得 −3.81%，交易所公布 +1.26%，差额是当天除息。**指标、估值、赔率的输入都是区间收益，所以除权链路（A 块）必须先闭合，指标与估值才有资格动。**

这条链上有三种做法，我选第三种：

1. *只算不复权价的比例，界面提示「未复权」* —— 提示会被忽略，而错误数字会被写进快照当成历史事实。
2. *用来源给的涨跌幅列（akshare 的 `涨跌幅`）* —— 那是来源的口径，跨源不一致，且我们已在 `normalize_bars` 里丢弃它；拿它当区间收益等于把口径交给运气。
3. *自己从 `corp_actions` 现算 hfq，并且在「不知道该票有没有除权」时拒绝出数* —— 唯一能同时满足 D9（反未来函数）与「算不出要说算不出」的做法。代价是要有一张覆盖区间表，见 A-2。

## 口径决定（开工前定死，写进 spec）

| # | 决定 | 为什么 |
|---|---|---|
| D34 | 一个因子公式覆盖全部公司行为：`ratio` = 除权后/除权前股数乘数，`cash_per_share` = 每股收到的现金，`price`（新列）= 每股新股需支付的现金。`ex_ref = (prev_close − cash + price×(ratio−1)) / ratio`，`hfq_t = raw_t × ∏(ex_date ≤ t 的 prev_close/ex_ref)` | 拆股/送股/转增/配股在同一列里各写各的语义就是 U4 记的那条坑。一个公式让四种行为退化成同一件事，测试也有手算基准 |
| D35 | `corp_actions` 只回答「有没有除权」，不回答「复权因子是多少」；因子在 `analysis` 现算，且**必须有前收才推得动**——所以 hfq 只在有真实前一根 K 线时可用 | 因子依赖 `prev_close`，把它写进库等于把某个时点的价格当成永久事实，除权一改就漂 |
| D36 | 新增 `corp_actions_coverage(market, code, covered_from, covered_to, src, fetched_at)`：采集成功后 upsert 覆盖区间。**区间收益只在覆盖到位时给出，否则 `NotAdjustable(原因)`** | 从未分红的票在 `corp_actions` 里就是 0 行，和「没采集」长得一模一样。没有覆盖标记就没有资格说「我不能算」 |
| D37 | A 股额外一道硬证据：窗口内出现超过涨跌停的裸价跳变 ⇒ 一定有未标记的除权 ⇒ 即使声称覆盖也拒绝 | 沪深主板 ±10%、创业/科创 ±20%、ST ±5%、北交所 ±30%；港美股没有涨跌停，这道判不了，只依赖 D36 的覆盖标记 |
| D38 | 采集器按能力而非按源分类：一个 `@source(kind)` 类可以实现 `BarAdapter`、`CorpActionAdapter` 或两者。降级链与市场路由沿用 M1 的两张表，**缺能力的源在 actions 链里显式跳过并打印**，不静默 | M1 已经把「源是什么」和「源能干什么」分开了；静默跳过会让「没有备源」再次看不出来 |

---

## 文件结构

```
新增
src/sams/storage/migrations/0008_corp_action_price.sql      corp_actions 加 price 列（D34）
src/sams/storage/migrations/0009_corp_actions_coverage.sql  覆盖区间表（D36）
src/sams/datasource/actions.py                              CorpActionAdapter 协议 + 标准列 + 规范化
src/sams/datasource/akshare_actions.py                      A 股：akshare 东财分红送配明细（每10股口径换算）
src/sams/datasource/yfinance_actions.py                     港美股：yfinance Ticker.actions（Dividends/Splits）
src/sams/analysis/__init__.py                               新层入口（分层守卫已允许）
src/sams/analysis/prices.py                                 hfq 因子现算 + 只读现算序列
src/sams/analysis/returns.py                                区间收益守卫（D36/D37）
src/sams/analysis/indicators.py                             MA/EMA/MACD/RSI/BOLL/KDJ/ATR/OBV/DMI/CCI/WR/量能均线
src/sams/analysis/resample.py                               周/月 K 重采样（不入库）
src/sams/analysis/valuation/__init__.py
src/sams/analysis/valuation/styles.py                       value/trend 模板与权重
src/sams/analysis/valuation/scoring.py                      分项打分 + evidence
src/sams/analysis/valuation/compose.py                      fair/entry/stop/target/reward_risk 合成
src/sams/services/corp_actions.py                           用例：采集入库 + 覆盖标记（编排 registry 与仓库）
src/sams/services/analysis.py                               用例：读价→指标→估值→写快照
src/sams/services/theses.py                                 用例：状态机 + 事件留痕
src/sams/storage/repos/corp_actions_coverage.py             覆盖区间仓库
src/sams/storage/repos/financials.py                        financials 仓库
src/sams/storage/repos/funding_flow.py                      funding_flow 仓库
src/sams/storage/repos/theses.py                            theses + thesis_events 仓库（用户隔离）
src/sams/storage/repos/valuation_snapshots.py               快照仓库
src/sams/cli/thesis.py                                      `sams thesis add/list/show/advance/close`
tests/…                                                     镜像上述每个文件

修改
src/sams/datasource/registry.py     加 actions 链与跳过说明
src/sams/storage/repos/corp_actions.py   字段语义按 D34 收紧、list_for 的 until 语义定案
src/sams/bootstrap.py               新仓库/服务接线
src/sams/cli/{sync,health}.py       `sync actions` 子命令与 `[数据]` 段的覆盖状态
src/sams/datasource/{akshare_cn,eastmoney,yfinance_us,tencent,sina}.py  按需实现 CorpActionAdapter
tests/test_layering.py              已预留 analysis，无需放宽（确认即可）
docs/superpowers/specs/…design.md   §5.5/§7/§8/§18 同步 D34–D38 与 U4/U12/U13/U14 状态
```

---

## 任务

### A 块：除权链路（闭合 U12）

**A-1 字段语义与迁移**

- [ ] 写失败测试：`ratio` 只接受 >0 的乘数语义、`price` 只允许与 `rights` 同时出现、`dividend` 必须有 `cash_per_share`、同一 `(market, code, ex_date, kind)` 重写覆盖
- [ ] `0008_corp_action_price.sql`：`ALTER TABLE corp_actions ADD COLUMN price DOUBLE`（DuckDB 支持 `ADD COLUMN IF NOT EXISTS`，重放安全）
- [ ] `CorpAction` 加 `price` 字段；`upsert` 的校验按 D34 重写，`list_for` 返回带 `price`
- [ ] 跑 `uv run pytest -q tests/storage/test_corp_actions.py` 全绿 → 提交

**A-2 覆盖区间仓库**

- [ ] 失败测试：`covered_through(market, code)` 在未采集时返回 `None`、upsert 后区间取并集、`(market, code)` 唯一、跨源采集不丢原区间
- [ ] `0009_corp_actions_coverage.sql` + `repos/corp_actions_coverage.py`
- [ ] 提交

**A-3 hfq 现算与区间守卫**

- [ ] 黄金样例用例（手算对齐）：纯现金分红、送股、拆股 1→2、合股、配股各一条，`ex_ref` 与因子逐位对齐
- [ ] 多事件连乘、`ex_date` 当天生效（≤ t）、无 corp_actions ⇒ 序列等于裸价
- [ ] `returns.py`：覆盖不足 ⇒ `NotAdjustable`；A 股超涨跌停跳变 ⇒ 即使声称覆盖也拒绝（D37）；601088 的 −3.81% vs +1.26% 写成回归用例
- [ ] 提交

**A-4 A 股采集器**

- [ ] 读 akshare 安装包源码确认函数与列名（不靠记忆）：`stock_fhps_detail_em` 的返回列与「每10股」口径
- [ ] `CorpActionAdapter` 协议进 `datasource/actions.py` + `normalize_actions`（列集、类型、日期解析、被丢行计数）
- [ ] 纯解析函数 + 录制 payload 的离线用例；单位换算（每10股 → 每股）用「10派X元(含税)」描述列交叉验证
- [ ] `live` 用例：拉一只真实票，与交易所公布的除权参考价对齐
- [ ] 提交

**A-5 港美股采集器 + registry 双能力链**

- [ ] `yfinance Ticker.actions` → Dividends（每股）/Stock Splits（比率），exchange-local 日期索引的时区处理
- [ ] registry：`fetch_actions` 链、缺能力的源显式跳过并在结果里记 `skipped`；`Attempt.tries` 只算真调用
- [ ] `services/corp_actions.py` 编排采集→入库→覆盖标记；`sams sync actions --market A --code 601088 --start … --end …`
- [ ] 提交

**A-6 交易员 agent 试用（A 块收口）**：并行派「对抗测试 agent」+「架构师评审 agent」，收口后派交易员 agent，报告问题逐条修复或记入 §18。

### B 块：指标引擎（§8.2）

- [ ] `indicators.py` 全部为纯函数（输入 `pl.DataFrame`，输出带新列的帧），入口拒绝未复权序列
- [ ] 黄金样例：MA/MACD/RSI/ATR 与手算/已知值逐位对齐；KDJ/DMI/CCI/WR 至少一条与公开样例对齐
- [ ] `resample.py`：周/月 K 由日线现算（周五收市口径、非交易日跳过），不入库
- [ ] **反未来函数专项**（spec §13）：篡改 T+1 及之后数据，T 日每个指标逐位不变；盘中未完成 K 线参与计算时结果带 `intraday=true` 标记
- [ ] 三道闸 + 提交

### C 块：基本面与资金流接入

- [ ] `financials`/`funding_flow` 仓库（幂等 upsert、`report_date` 口径、缺失字段存 null 不填 0）
- [ ] 采集器：A 股 akshare/东财、港美股 yfinance。**PE-PB 历史分位的分子必须是「发布日 ≤ T」的口径**（否则未来函数）
- [ ] `sams health` 的 `[数据]` 段加基本面覆盖一行
- [ ] 三道闸 + 提交

### D 块：估值合成（§8.3）

- [ ] `styles.py`：内置 `value`/`trend` 两套权重，参数从配置读、可复制
- [ ] `scoring.py`：技术面（均线位置/周线趋势/布林位置/量能）+ 基本面（分位/ROE/增速）+ 资金面（主力净流入/北向），**每项带 `evidence` 指明来源字段与计算窗口**
- [ ] `compose.py`：fair_low/high、entry、stop（固定/pct/ATR2/周MA20 四种）、target、reward_risk；`win_rate` 留空并显示「未回测」；`win_rate_samples < 30` 不得参与排序
- [ ] 快照落库含 `intraday` 标记；黄金样例：一条手算赔率对齐 + 每个分数都能追溯到 evidence
- [ ] 三道闸 + 提交

### E 块：thesis 生命周期 + `sams thesis show`（§9.1，M2 验收项）

- [ ] `repos/theses.py`：状态机合法跳转表在 storage 层强制，非法跳转拒绝；`thesis_events` 每次变更写一条；用户隔离同 watchlists
- [ ] `services/theses.py`：创建（observing）、提出（proposed）、建仓（opened，校验止损/目标/赔率齐备）、归档拒绝
- [ ] `sams thesis show` 文本报告：合理区间、买入区、止损、目标、赔率 + 分项打分与 evidence 展开
- [ ] 验收：任一自选股输出估值报告，每个分数能点开看到数据来源
- [ ] 三道闸 + 提交

---

## 验收清单（spec §14 M2）

1. `sams sync actions --market A --code 601088` 后，跨 2024-07-08 的区间收益与交易所公布值一致；未采集该股时同样调用被拒绝并说明原因。
2. `sams sync --market A --code 600519` 后 `sams thesis show` 输出估值报告：fair/entry/stop/target/reward_risk + 分项打分，每项带 evidence。
3. 篡改任意一只票 T+1 之后的日线，T 日全部指标与估值输出逐位不变。
4. 周线指标由日线现算，库里没有 `bars_weekly` 之类的新表。
5. 非法状态跳转（未建仓直接归档）在仓库层被拒；每次合法跳转都有 `thesis_events`。
6. `uv run pytest -q` 全绿；`uv run ruff check .` 干净；分层守卫不因新层放宽。

## 三道闸的执行形态（每块都走一遍）

| 闸 | 派什么 | 关注点 |
|---|---|---|
| 测试 | `Agent(Explore 或 general)` 对抗测试本块改动 | 造脏数据、越权调用、时间边界；不接受「理论上不会发生」 |
| 架构评审 | 独立架构师 agent | 边界是否清晰、新层有没有开后门、口径是否只有一处定义 |
| 交易员试用 | 股票交易专家 agent | 拿真实输出做决策：数字可信吗、算不出时说的是不是人话、有没有被静默误导 |
