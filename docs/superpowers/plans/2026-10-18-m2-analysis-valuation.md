# M2 分析与估值实施计划

> **For agentic workers:** 逐任务执行，每个任务一次提交。步骤用 `- [ ]` 勾选。

**Goal:** 让 M1 的库能回答「这只票现在值不值得做」——正确的复权价、可解释的分项打分、合理区间/买入区/止损/目标/赔率，以及承载它们的 thesis 生命周期。

**Architecture:** 新增 `analysis` 层（分层守卫 §3.1 已预留，只允许 import core/storage——
services 调 analysis，反向不通；被 monitor/trading 复用）。三层职责：**`datasource` 只负责把外部响应变成公司行为事实**（不猜因子）；**`storage` 只负责存事实与覆盖区间**（不解释）；**`analysis` 才做推导**（hfq 现算、指标、打分），且任何「不可信就不给数」的判定都发生在这一层并带上原因。CLI 仍是薄壳，编排走 `services`。

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
| D39 | 发生过但折不出数的公司行为记 `kind=unknown`（三个数值全空 + `note` 必须带源原话），读取层见到它**拒绝**出数 | 港股的实物分派（分美团股/分阅文股）与认股权证既不是每股现金也不是股数乘数。硬凑一个数会把猜测乘进序列；丢掉它等于「这天没发生」，而跨那天的涨跌是错数。A 股还有 D37 兜着，港股没有涨跌停边界，只能靠这条 |

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
src/sams/services/analysis.py                               用例：读价→指标→估值→写快照（A 块只落区间涨跌那半）
src/sams/services/theses.py                                 用例：状态机 + 事件留痕
src/sams/storage/repos/corp_actions_coverage.py             覆盖区间仓库
src/sams/storage/repos/financials.py                        financials 仓库
src/sams/storage/repos/funding_flow.py                      funding_flow 仓库
src/sams/storage/repos/theses.py                            theses + thesis_events 仓库（用户隔离）
src/sams/storage/repos/valuation_snapshots.py               快照仓库
src/sams/cli/analysis.py                                    `sams analysis return`（守卫唯一的人类出口，D44）
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

**A 块落地后的四处偏离计划**（下面的勾选按实际文件为准）：

- `datasource/yfinance_actions.py` 没单独成文件——港美股的公司行为与日线同一个源、同一个
  上游会话，拆两个模块会让 `@source` 注册表里出现两个名字指向同一故障域，管理后台就会把
  它们当成互为备源（D38 禁止的正是这个）。解析函数 `expand_actions` 放在 `yfinance_us.py` 里。
- `analysis/prices.py` → `analysis/adjust.py`：`prices` 说的是「价格」，而这个模块给的其实是
  **因子**（`day_factors`/`hfq_frame`）。名字错了的下一个人会在指标引擎里再写一份价格读取。
- 港股公司行为是**新增的第 6 个源** `datasource/akshare_hk.py` + 迁移 `0010_hk_actions_source.sql`，
  计划里没有这一条：A-5 做完才发现 HK 的 actions 链上一个能给的源都没有（Yahoo 在本机长期 429），
  而漏一次港股除权没有 D37 那样的硬证据兜底。A 股同理补了第 7 个源 `akshare_ths.py` + 迁移 `0011`
  （交易员闸：东财对部分票抛 `TypeError`，单源意味着那些票永远零覆盖）。
- `tests/test_layering.py` 那行「无需放宽（确认即可）」是错的：交易员闸补 `services/analysis.py`
  读用例时必须放开 **services → analysis** 单向。这不违反 §3.1（它禁的是反向），
  但没有这条边，守卫就没有人能敲的出口，`interval_return` 会成为零消费者函数（实际就是）。

---

## 任务

### A 块：除权链路（闭合 U12）

**A-1 字段语义与迁移** ✅（提交 `f4e59fd`）

- [x] 写失败测试：`ratio` 只接受 >0 的乘数语义、`price` 只允许与 `rights` 同时出现、`dividend` 必须有 `cash_per_share`、同一 `(market, code, ex_date, kind)` 重写覆盖
- [x] `0008_corp_action_price.sql`：`ALTER TABLE corp_actions ADD COLUMN price DOUBLE`（DuckDB 支持 `ADD COLUMN IF NOT EXISTS`，重放安全）
- [x] `CorpAction` 加 `price` 字段；`upsert` 的校验按 D34 重写，`list_for` 返回带 `price`
- [x] 跑 `uv run pytest -q tests/storage/test_corp_actions.py` 全绿 → 提交

**A-2 覆盖区间仓库** ✅

- [x] 失败测试：`covered_through(market, code)` 在未采集时返回 `None`、upsert 后区间取并集、`(market, code)` 唯一、跨源采集不丢原区间
- [x] `0009_corp_actions_coverage.sql` + `repos/corp_actions_coverage.py`
- [x] 提交

落地时改了两处计划里的写法：**主键加 `src`**（`(market, code, src)`）——A 股主源确认过的区间
与备源确认过的区间不是一回事，合成一段等于凭空造出一个谁都没看过的覆盖面；以及
`full_history` 标记（D36 的「确认到今天，今天越界即失效」）。

**A-3 hfq 现算与区间守卫** ✅（`analysis/adjust.py` + `analysis/returns.py`）

- [x] 黄金样例用例（手算对齐）：纯现金分红、送股、拆股 1→2、合股、配股各一条，`ex_ref` 与因子逐位对齐
- [x] 多事件连乘、`ex_date` 当天生效（≤ t）、无 corp_actions ⇒ 序列等于裸价
- [x] `returns.py`：覆盖不足 ⇒ `NotAdjustable`；A 股超涨跌停跳变 ⇒ 即使声称覆盖也拒绝（D37）；601088 的 −3.81% vs +1.26% 写成回归用例
- [x] 提交

**这里发现的一条契约错**（真实数据抓出来的，不是用例抓出来的）：跨 2008-06-04 那次除权的区间
报 `库里没有它前一根 K 线`。原因是 `interval_return` 原本要求 `bars` 覆盖整个历史，而区间涨跌
只吃两端那两根 K 线、因子只吃落在两端之间的记录——更早的除权对两端是同一个常数，会约掉。
改为窗口内取因子（`ex_date ∈ (first, last]`），并把「窗口内的除权日必须在库里对得上 K 线」
单独做成守卫（D39 的前身）。绝对 hfq 序列仍然只能从**全历史**首根开始乘，这一点写进了文档：
拿窗口化的 `close_hfq` 当绝对价格，等于宣称「首根那天的因子是 1」。

**A-4 A 股采集器** ✅（`datasource/actions.py` + `datasource/akshare_actions.py`，提交 `c02f2d4`）

- [x] 读 akshare 安装包源码确认函数与列名（不靠记忆）：`stock_fhps_detail_em` 的返回列与「每10股」口径
- [x] `CorpActionAdapter` 协议进 `datasource/actions.py` + `normalize_actions`（列集、类型、日期解析、被丢行计数）
- [x] 纯解析函数 + 录制 payload 的离线用例；单位换算（每10股 → 每股）用「10派X元(含税)」描述列交叉验证
- [x] `live` 用例：拉一只真实票，与交易所公布的除权参考价对齐
- [x] 提交

三条口径都是这个模块存在的全部理由：单位是「每 10 股」（神华 2024 真实值 10派22.60元 ⇒ 每股
2.26 元）；同一行的派息与送转是两个独立事实必须拆行；只有「实施分配」才是发生过的事——
未实施的行没有除权日，把没发生的除权乘进序列比漏一次更难被发现。**配股不在这里**（见 spec U16）。

**A-5 港美股采集器 + registry 双能力链** ✅（提交 `e718c29`、`0d89d06`）

- [x] yfinance `Ticker.get_actions` → Dividends（每股）/Stock Splits（比率）/Capital Gains，exchange-local 日期索引的时区处理（**函数并进 `yfinance_us.py`**，不单独建 `yfinance_actions.py`，理由见上面的「三处改名」）
- [x] registry：`fetch_actions` 链、缺能力的源显式跳过并在结果里记 `skipped`；`Attempt.tries` 只算真调用
- [x] `services/corp_actions.py` 编排采集→入库→覆盖标记；`sams sync actions --market A --code 601088 --start … --end …`
- [x] 提交

**A-6 港股公司行为源 + D39** ✅（新增块，计划外——A-5 做完才看清缺口的大小）

派 A-5 的活去跑真实港股时发现：**0007 之后 HK 链上两个源都只有日线能力**，
`sams sync actions --market HK` 的症状是「这只票从没分过红」。而港股没有涨跌停，D37 那道
证据守卫在那里不生效，漏一次除权在图上什么都不留下。于是补了：

- [x] `datasource/akshare_hk.py`（东财港股分红派息，**只有公司行为能力**）+ 迁移 `0010_hk_actions_source.sql`
      播种进 HK 链尾；金额一律取港币等价（没有等价 ⇒ 不猜汇率），「每 N 股派送 M 股权证」不是送股
- [x] D39：`kind=unknown` 的词汇、仓库校验（数值必须全空 + `note` 必须有原话）、`adjust` 见到它拒绝出数
- [x] 按能力数备源：`registry.chain_for(market, method)` 成为 `sams health` 的 ⚠ 判据与
      `sams source` 的「不许弄瞎市场」判据——只报公司行为的源不算日线的备源
- [x] 实测：00700 全历史 26 条（4 条实物分派 unknown：分京东/分美团/分阅文/分腾讯音乐），
      yfinance 被 429 时降级到 akshare_hk 并显式跳过 tencent；跨 2023-01-05 那天的区间收益拒绝出数

**A-7 三道闸收口（对抗测试 agent + 架构师评审 agent + 交易员 agent）**——前两道 ✅（提交 `15fc8c6`），第三道见下

对抗测试 agent 查出 6 条、架构师评审 agent 查出 2 条，全部先写复现用例再修：

| # | 症状（真实数据能复现的那句） | 处置 |
|---|---|---|
| 1 | 按窗口同步 `601088 2024-01-01~03-01` 之后，跨 2015 年那次除权的区间数字照样变了——库里只剩窗口内的除权事实，而覆盖标记声称「全历史到今天」 | **D40**：全历史源不再按请求窗口切片（三个适配器各删一行 `filter`），`covered_from` 改取实际入库的最小 `ex_date`。切片让库与自己的声明互相矛盾，守卫只能给出裸价 |
| 2 | 同一 `(ex_date, kind)` 的两行（10 送 10 与 10 转 10 同日）静默丢弃一行 ⇒ 少乘一次 | **D41**：`merge_same_day` 相加现金与新增股数，`price` 按新增股数加权（推导端算的是 `price×(ratio−1)`）；整行重复先 `.unique()` 去掉，不重复计数 |
| 3 | 已记录在案的 10 送 10（裸价 −50%）被 D37 判成「一定有未记录的公司行为」而拒绝出数 | **D37 改写**：跳变若对得上当天记录的除权参考价（`abs(close − ex_ref) ≤ ex_ref×limit`）即豁免——守卫的指控对象是「没记录」，不是「跌得多」 |
| 4 | 上市首日的豁免按「第 N 根 K 线」数，窗口化的帧数根本不代表从上市起过了几根 | 改成日历天宽限（`_IPO_GRACE_DAYS = 10`），且**只有调用方传了 `listed_on` 才豁免**；不传就是拒绝，理由里说明可以补 |
| 5 | `close` 为空 ⇒ traceback，而不是「这只票的序列我不能用」 | 第五道拒绝 `require_real_closes`（null 与非有限一起查：先 `is_null()` 再 `~is_finite()`，null 过 `~` 会漏） |
| 6 | 守卫只挂在 `interval_return` 上；`hfq_frame` 把裸的 open/high/low 留给调用方，指标引擎一接手就会拿到未复权的四列 | **D42**：`analysis/adjust.adjusted_series()` 是唯一出数入口（过闸后生成四列 `_hfq`）；`hfq_frame` 降级为「无守卫的原语，只用于单测因子算术」，文档写明 |
| 7 | 架构师：`analysis` 允许 import `services` 与 spec §3.1 相反 | `tests/test_layering.py` 收紧为 `analysis: {core, storage}`，`guard.py` 只按 `TYPE_CHECKING` 引用 `adjust` 的类型以免成环 |
| 8 | 架构师：`is_covered`/`ConfirmedSpan`/`_reject_unmarked_ex_dates` 长在 `returns.py` 里，等于守卫挂在某个调用方的习惯上 | 抽出 `analysis/guard.py`（三道闸 + 收盘价体检），`returns.py` 重写 65% 后只剩窗口过滤与两端读数 |

**修完留下的四条债**（都是有意推迟，不是漏掉；下一块开工前先读这几行）：

- [ ] **M2-C 开工前**：能力声明有两份（`registry.CAPABILITIES` 与各适配器的 `_capabilities`）、
      降级循环有两份近乎重复（`fetch_daily` / `fetch_actions`）。基本面源是第三次复制同一段循环——
      先收敛再往里加数据种类。
- [ ] **M3 开工前**：拒绝只以异常表达。批量调用方（监控扫描、回测）拿到的是 try/except 循环而不是
      结果集合；M3 需要 `Ok | Refused` 的并集类型，且不能靠异常文案分支。
- [ ] `normalize_actions` 的空输入会抛 `EmptyResponse`，而「这只票确实没分过红」应当给空帧：
      已把契约写进 docstring（空答案走 `empty_actions()`），没有重构——重构会牵动三个适配器。
- [ ] `corp_actions_coverage` 在「不相交窗口重复同步」时会把区间写窄还是写宽，只有一条用例覆盖。
      M2-C 若复用这张表的形状（基本面也要覆盖标记），先补齐这个判据再复用。

实测验收（本机真实网络）：`sync actions A 601088` 全历史落库 21 条（2008-06-04~2026-07-13）、
`covered_from=2008-06-04`；跨 2016-07-04 那次除权的区间 **裸价 +1.4873% / 后复权 +3.8270%**；
把 `today` 传到覆盖终点之后 ⇒ D36 按预期拒绝（新鲜度失效），改回当天 ⇒ 出数。

**A-7 第三道闸：交易员试用（股票交易专家 agent）** ✅（提交 `e3fabfe`、`7c75b75`）

verdict 是「**谨慎用**」：9 个真实区间的对账全部对齐，数字没错，但**能拿到数的那条路径本身是错的**——
下面第 7 条是这一轮最重的一条。

| # | 症状（真实数据能复现的那句） | 处置 |
|---|---|---|
| 1 | `probe A 601088 2024-01-02 2024-12-31` 返回 −6.24%，而它实际算的是 06-20~07-31 那段 | `returns.py` 的锚点体检：两端 15 个日历日内算常态（周末、长假、短暂停牌）并在输出里说明锚点了哪两根，再多就拒绝。`IntervalReturn.start/end` 一直诚实，但读的人知道自己请求了什么、不知道库里有什么 |
| 2 | `sams health` 的「`corp_actions` 47 行」说明不了 600028 有没有除权，而读取层对这种票只会拒绝 | `MarketData.codes_without_actions`（`bars_daily LEFT JOIN corp_actions_coverage`）＋「⚠ N 只有 K 线但除权记录从没确认过」，并给出该跑的那条命令 |
| 3 | 所有源都失败时 `sync daily` 打印「所有源都回答没有数据」并以 0 结尾 | 区分「源故障」与「这只票真没数据」：前者逐源打错误并以 1 结尾——对着一个空库什么也不做，是最贵的一种误读 |
| 4 | `sync actions --market A --code 002450/600636/600608` 全部 `TypeError: 'NoneType' object is not subscriptable`，而同花顺同一页有 8~28 条除权记录 | 崩溃不是源故障：akshare 内部对空响应做 `None` 下标。直连一次只读 GET 取东财自己的判据（`result is None && code == 9201`）来分类，「查无此票」与「这个源挂了」从此长得不一样 |
| 5 | 第 4 条修完暴露出一个更坏的：0 行被当成答案，于是写下「源=akshare 取回=0 确认至 2026-10-17（全历史）」——**一条没根据的覆盖确认** | **D43**：一个源的「查无记录」既不终止降级链也不单独成确认；只有链上每个有能力的源都答了「无」，才逐源写确认，中途有源失败则一个都不写。删掉本次自测写坏的那两行（`002450`/`600636`，`corp_actions` 行数不变）后重跑 ⇒ 两行都是 `akshare_ths` 的真记录 |
| 6 | A 股公司行为单源：解析器一崩那只票就永远零覆盖，区间涨跌一路拒绝 | 新增 `datasource/akshare_ths.py`（同花顺分红送配，**仅公司行为**）+ 迁移 `0011` 播种到 A 链尾。方案说明是一段自由文本，形状全部从真实响应枚举，读不出来记 `unknown`（D39）而不是猜一个数；`price` 恒空（配股仍无来源，U16） |
| 7 | 「`interval_return` 在 src/ 里零消费者」——命令行根本拿不到复权数，人能用的路径只有裸价相除 | **D44**：`services/analysis.py` 读用例（K 线 + `until=today` 的公司行为 + 覆盖确认，三份事实一起交给同一个闸）+ `sams analysis return`。分层放开 `services → analysis` 单向，`analysis` 不得反向认识任何服务 |
| 8 | 对账证据（不是待修项，记在这里证明守卫给的数是对的） | 601088 2024-07-05→07-08 复权 **+1.2605%** = 交易所参考价公式；00700 2025-05-15→05-16 复权 **−1.4549%** = 港交所除权口径；多段与新浪 hfq 差在 0.03pp 内；美团-B 实物分派与模拟的 10 送 10 均按预期拒绝 |

**这一轮之后仍然没修的**（逐条写进 spec §18，不是遗漏）：

- [ ] U18：`symbols` 没有「上市首日」列 ⇒ D37 的上市豁免永远走不到（`listed_on` 恒缺，后果是多拒不放行）。
      M2-C 的基本面源顺手给这一列最便宜。
- [ ] U19：拒绝文案给的下一步在 US 跑不通（公司行为只有 Yahoo 一家），`sams source probe` 的入参形态
      与 §7.6 不一致。两者都要把「这个源此刻能不能用」读回来拼进文案 ⇒ 与 M3 的告警文案一起做。
- [ ] U20：命令行只出区间涨跌，还出不了「一条复权序列」⇒ M2-B 指标引擎落地时一并给 `series`，
      别让列名与窗口语义在指标口径定下来之前被写死。
- [ ] U16 更新：A 股现在两个源，但**两家都不给配股**，且两个源只对齐「都说无」、不对齐「某一家漏了一条」。
      漏记一笔已存在的现金分红仍然看不见（典型 10 派 22.6 元 ⇒ 跳变 −5%，在 15% 的证据阈值之内）。

实测验收（本机真实网络，提交 `7c75b75`）：`sams analysis return --market A --code 601088
--start 2024-07-05 --end 2024-07-08` ⇒ **复权 +1.26% / 裸价 −3.81%**（交易所公布 +1.26%）；
`2024-07-01~07-15` ⇒ 复权 −1.44% / 裸价 −6.38%；`2024-01-01~12-31` ⇒ 拒绝并说明库里只有 07-01~07-15；
`--market HK --code 00700 --start 2022-12-01 --end 2023-03-01` ⇒ 拒绝（2023-01-05 那条实物分派
`kind=unknown`，D39）；`sh601088` 与 `601088` 给同一个数。离线 `uv run pytest -q` 905 passed / 8 deselected。

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
