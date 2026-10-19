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
src/sams/storage/repos/valuation_snapshots.py               快照仓库（**没建**：D 块不落快照，D76；表在 0001 里已有，写入方是 M3）
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

**A-7 第四轮：三道闸再各走一遍（对抗测试 agent + 架构师评审 agent + 交易员 agent）** ✅（提交 `85c2d6e`、`b7bfe0e`）

三个 agent 这一轮查出的都不是「算错了」，而是**守卫能在某些敲法下静默不生效**——最难发现的那一类。
全部先写复现用例再修：

| # | 症状（能复现的那句） | 处置 |
|---|---|---|
| 1 | 对抗：降级到备源、而备源对同一 `(ex_date, kind)` 给了另一个数（2.10 → 3.00），后写覆盖先写，覆盖确认照旧写 ⇒ 放行一个看起来正常的错数 | `repos/corp_actions` 写侧冲突判定：相差 ≤0.005 视为同一条记录的修正（照旧覆盖），差得多 ⇒ `StorageError` 并把两个数与两个源都点名；**整批先校验后写**，一条冲突不陪其余数据进半截库。同日两件事要真正支持得换主键 ⇒ **U21** |
| 2 | 架构师：`is_covered(..., today=None)` 那个默认值等于「忘了传时钟也能出数」，而且出来的数与新鲜确认过的一模一样 | `guard.is_covered` / `require_coverage` / `adjusted_series` / `interval_return` 的 `today`、`coverage` 全部去掉默认值；`test_the_clock_and_the_confirmation_cannot_be_forgotten` 把「没有默认值」这件事本身钉住（否则下次有人「顺手加个默认」就抹掉了） |
| 3 | 架构师：`ALLOWED["analysis"]` 含 `storage`，等于允许守卫自己去库里看一眼 | 收紧为 `analysis: {core}`。三份事实全是参数：「能 import storage」与「会顺手查一眼库」只差一个习惯，而后者让守卫变成装饰 |
| 4 | 架构师：`hfq_frame` 是不过闸的裸原语，只靠 docstring 写「请勿出数」 | `tests/test_layering.py::SOLE_CONSUMER` 机器校验它只有 `analysis/adjust.py` 一个消费者，别处出现即失败 |
| 5 | 交易员：`--market a` 让 `daily_price_limit` 返回 None ⇒ D37 那道闸静默关掉，同时三个仓库各查一张空表 | `core.codes.canonical_market()` 上收到**服务边界**：`AnalysisService.interval_return`、`CorpActionSyncService.sync_actions`、`SyncService.sync_daily` 在入口归一（否则覆盖确认按 `a` 写进去，「同步成功」与「读数继续拒绝」同时成立）。空市场名 ⇒ `ValidationError`，CLI 打「错误:」并退 1 |
| 6 | 交易员：「在 X~Y 不足两根 K 线」把三种情况说成一种，而下一步该敲的命令完全不同 | `services/analysis.py::_why_no_bars` 分诊：主档没这个代码（多半写错）/ 主档有、全库 0 根（还没采集）/ 全库 N 根、这段 0 根（并报最新到哪天） |
| 7 | 交易员：锚点挪了却断言「（周末、长假、停牌）」——那是无依据的猜测，且恰好把最危险的一种缺口（开市日没采到）说成常态 | `CalendarService.known_status()`：只读库、绝不顺手打来源，日历没铺到就回答 None。CLI 按 开市/休市/未知 分类措辞，并引入**退出码 3** =「数给了，但不是请求那个区间的数」；`0` 现在只表示「请求的那段」 |
| 8 | 交易员：`+2.44% vs +2.44%，采信复权`——两个数在两位小数下长成同一个字符串，那行输出没有信息量，而被舍掉的那一位正是差别 | 复权与裸价在两位小数下相同且确实不同时，补一行四位小数 |
| 9 | 交易员：`ex_dates=0` 就打印「区间内没有除权日」，而起点那根本身可能就是除权日（效果已在起点价里，因此不进因子） | `IntervalReturn.start_is_ex_date`（`analysis/returns.py`），文案分三支；顺带纠正「两个数相等是巧合」那句——首尾之间没因子时它们是**必然**相等 |
| 10 | 交易员：`--start 2024-7-1` 吐一段栈 | `cli/common.iso_date()` 把 `fromisoformat` 的 `ValueError` 换成 `ValidationError` 并带上正确示例；同一条规则让 `canonical_code` 的空代码也不再是裸 `ValueError` |

**第四轮之后新增的债**：见 spec §18 **U21**（同日两件事的真除权需要换主键；现在宁可拒绝也不合并）。

实测验收（本机真实网络，`601088` 2024-07）：`--market a` 与 `--market A` 给同一个数；
`--start 2024-07-01 --end 2024-07-20` 在日历没铺到 2024 年时答「5 天无法判断」并以 3 结尾，
`sams calendar refresh --market A --year 2024`（242 个交易日）之后同一句改成
「4 个开市日没有 K 线（07-16/17/18 等）＋1 天休市」，仍然 3；`--code 000001` ⇒ 「主档里也没有这个代码」；
`--start 2024-09-01` ⇒ 「全库有 11 根，最新到 2024-07-15」。离线 `uv run pytest -q` 1000 passed / 8 deselected。

**A-7 第五轮：同一道闸再走一遍，查的是「归一为什么还会漏」** ✅（代码 `f3d8544`、文档 `5a77d0e`；其中 D50 的「七个仓库同一条规则」被第六轮的 D51 补了一半）

上一轮的 D46 把市场名归一放在**服务边界**，第五轮三个 agent 一致指出那是错的层：主键形态的正确性
于是取决于「调用方走没走那条路」。全部先写复现用例再修，一条一条列在这里：

| # | 症状（能复现的那句） | 处置 |
|---|---|---|
| 1 | 对抗 + 架构师：仓库层原样收下 `market`——`WatchlistRepo.add(user, "a", "601088")` 把 `a` 直接写进主键（那一层只归一了 code；自选清单的命令行要到 M5 才接上，所以这条路今天只有调用方能走）。那一行在清单里看得见、行情按 `A` 查 ⇒ 覆盖确认查出 0 行，输出「首尾之间没有除权日」并照实用裸价出一个**错数**而不是报错；D37 也抓不住（4% 的派息跳变在 15% 阈值之下） | **D50**：主键的形态由**定义主键的那一层**保证，不该取决于哪个调用方记得归一。`bars_daily`/`symbols`/`corp_actions`/`corp_actions_coverage`/`watchlists`/`trading_calendar`/`data_source_routes` 七个仓库的写侧归一 `market` 与 code 形态，读侧用同一条规则处理入参（`_form()` / `canonical_market()`）；D46 降为第二重。实测本机库 18 张带 `market`、13 张带 `code` 的表都没有非规范行 ⇒ **不做一次性迁移** |
| 2 | 架构师：写侧大写了键、读侧还按调用方给的原样查——这比第 1 条更隐蔽，因为修了一半的归一看起来像修完了 | 带市场入参的读方法全部先归一再进 SQL：`bars.latest_date/load/count`、`symbols.get/list_market`、`corp_actions.list_for`、`calendar.get/trading_days/rows_for_year/source_of/year_covered`、`coverage.get/for_code`、`watchlists.contains/remove`、`data_sources.set_route/reorder/remove_route`，钉在 `tests/storage/` 那四个文件里 |
| 3 | 架构师：`CalendarService` 的 providers 按原样字符串 key，配置里同时写 `a` 与 `A` 时后一条静默盖掉前一条；CLI 的 `calendar check/refresh --market a` 也照原样回显，人以为铺开的是另一个市场 | 构造时先 `canonical_market` 再建映射，撞车 ⇒ `CalendarSourceError`（点名两个形态）；`provider()/refresh()/day()/known_status()/gap()` 入口各归一；`cli/calendar.py` 打印归一后的形态（回显即归一，D46 的那条规矩） |
| 4 | 交易员：Ctrl-C 打一次 `sync`，降级链把 `KeyboardInterrupt` 当成「这个源坏了，换下一个」，于是中断要等整条链每个源都试完才生效，还把一次取消记成源故障、扣掉那个源的降级计数 | `datasource/registry.py` 的兜底从 `BaseException` 收窄到 `Exception`（`# noqa: BLE001` 说明为什么）；`test_interrupt_is_not_a_source_failure` 断言备源 `calls == 0` 且健康里 `ok is None`（**从没被路由过**，不是 `ok=False`） |
| 5 | 交易员：缺口区间怎么算、哪一堆缺口该升级成退出码 3，全写在 `cli/analysis.py` 里——命令行自己算了两次区间算术 | 判定挪出命令行：`core/timeutil.gap_days()`（纯日期算术）+ `services/calendar.GapKinds`（按开市/休市/未知分堆）+ `CalendarService.gap()`（`known_status` 判不了的那一堆不能替它担保），**`matters` 就是那道判定**；CLI 只措辞、只换成退出码 |
| 6 | 架构师：`storage/repos/corp_actions._TOL` 与 `datasource/akshare_actions._TOLERANCE` 数字都是 0.005 而量的是两件事（前者比两个源的意见，后者比同一条记录里文本与数值是否自洽），改一个容易被「顺手同步」改掉 | 两处各加一条互指注释：数字相同是巧合，语义不同，改一个不必动另一个 |
| 7 | 我自己在上一轮写下的注释与文档断言了一条**不存在的机制**（「`sams source add --markets a` 会种出一条小写路由」）——实际走不到：`services/sources.py` 的市场成员检查先把 `a` 判成「实现不支持」并抛错。同一轮里我还把第 1 条写成「`sams watch add` 敲出来的」，而 CLI 至今没有 `watch` 命令 | 注释与 docstring 全部改成**能当场验的那条路径**：仓库层的 `WatchlistRepo.add` 收小写 `market`（第 1 条）。**在注释里断言一条错误的机制，和在有缺陷的代码里工作是完全同一类问题**：下一轮 agent 会照它去修，而那里没有东西可修 |
| 8 | 交易员 + 架构师：打印的口径声明写着「现金到手不再投资」，而产出那个数的算式是因子法——它在数学上就是按除权参考价把分红买回该股 | **D48**：改文案而不是改算式（锚点是交易所公布的当日参考价，那是唯一的外部证据）。单测把两个口径各算一遍钉住：601088 2024-07-01~07-15 再投资 `−1.4413%`、现金到手 `−1.3914%`；只取除权日当天给出 `+1.2605%` = 交易所公布值。要不要并排打第二个数 ⇒ **U22** |
| 9 | 交易员：采集完成后的第二天，一段 2024 年的窗口从「给数」变成「拒绝出数」——那段区间的除权记录一条都没变，变的只是时钟 | **D49**：`is_covered()` 只问请求窗口落在确认区间内，`covered_to` 语义改为「查到哪一天为止」而非「哪一天作废」；换来的诚实是 `covered_through` 随数出行（`AdjustedSeries` → `IntervalReturn` → CLI「记录截至 …」）。何时该重新问源，归 M3/M4 的同步调度（修订 D36） |
| 10 | 架构师：`analysis` 层的守卫与 `services` 层各存一份「这段有没有除权记录」的判断，而 `hfq_frame` 那条不过闸的裸原语只靠 docstring 约束 | `tests/test_layering.py::SOLE_CONSUMER` 白名单随 D44/D50 的新读路径一并复核收紧；`analysis` 仍只依赖 `core` |

**第五轮之后新增的债**：spec §18 **U22**（现金到手口径要先定送转/配股的语义才谈并排打印）。

实测验收（本机真实网络，库为本机 `data/sams.duckdb` 的一份副本，命令行只读数）：
`--market a` 与 `--market A` 给**同一个数**（601088 2024-07-01~07-15 ⇒ 复权 −1.44% / 裸价 −6.38%），
口径那行现在写「按除权参考价再投资……与交易所公布的当日涨跌幅同一算法」并明说「现金到手」那个数
这里不给（D48）；采信那行末尾带着「覆盖确认：akshare，记录截至 2026-10-17」（D49 换来的可审计性）；
`2024-07-05~07-08` ⇒ **+1.26%**（交易所公布的除权日涨跌幅）；
`2024-07-01~07-20` ⇒ 同一对锚点出数、明说算的是 07-01~07-15、缺口 5 天里「4 个开市日没有 K 线
（07-16/17/18 等）＋1 天休市」，以 **3** 结尾（分堆与判定已在 `CalendarService.gap()`，命令行只措辞）；
`--market HK --code 700 --start 2025-05-15` ⇒ 拒绝并说明「全库有 59 根，最新到 2023-03-01」——本机这份
库里 00700 只铺到 2023 年（第三轮那句 2025-05 的对账数不是在现在这份库里取的）；同一段用 `700` 与
`00700` 敲，得到同一条拒绝，而 `2022-12-01~2023-03-01` 仍按 D39 拒绝（实物分派 `kind=unknown`）。离线 `uv run pytest -q` **1021 passed / 8 deselected**，
`uv run ruff check .` 全绿。

**A-7 第六轮：拿第五轮的修法当靶子** ✅（代码 `3b43797`）

对抗 agent 这一轮的题目就是刚改完的那五处，结果是**其中一处自己留了洞**。全部先写复现用例再修：

| # | 症状（能复现的那句） | 处置 |
|---|---|---|
| 1 | `sams source unroute a sina` ⇒ 「错误: 市场 a 摘掉 sina 就没有能取日线的源了；先加备源」，而 A 股那时四个日线源都还在；同一条命令用 `A` 敲则成功 | D50 只写进注释里的那句「两重遮挡」是假的：`route/reorder/probe` 因为先过 `_require_routable` 侥幸挡住了小写，`unroute` 是唯一一条**直接进字符串比对**的（`_bars_sources_in` 的 `m == market` 对不上库里的大写行 ⇒ 集合为空）。四个动词的入口各 `canonical_market`，报错文案从此点名的是真市场 |
| 2 | `bars.upsert(market="HK", code="700")` 落库 `700`，而 `corp_actions`/`watchlists` 落 `00700` ⇒ `bars.load("HK", ["00700"])` 查出 0 行，「这天的除权在 K 线上没有对应记录」那条闸为一只**存在**的票报警 | D50 的「七个仓库同一条规则」当时只对了一半：bars 与 symbols 只 `upper()`。两处改调 `canonical_code`（写侧经 `repos/_keys.normalize_keys`，读侧直接调它），空代码行与空市场一样丢弃并 warn；`bars.count(code=…)` 不给市场时**拒绝**——没有市场就不知道该按哪种形态归一，蒙一个是同一个洞换边 |
| 3 | `analysis return --start 2024-07-15 --end 2024-07-01` 走到 `_why_no_bars`，打出「把区间挪到有数据的那一段，或先跑 `sams sync daily … --start 2024-07-15 --end 2024-07-01`」——**那条命令自己也是空区间** | `AnalysisService.interval_return` 在读库之前拒反向区间（`ValidationError`：这是空区间，不是「这段时间没有行情」）。守卫给的下一步必须跑得通，否则它把用户钉在同一个敲错的参数上 |
| 4 | 计划第五轮 row 2/3 点了三个不存在的名字（`bars.get`、`calendar.known_status`、`test_d50_*.py`）和一个不存在的命令（`calendar show`） | 全部改成 `grep`/`--help` 当场能验的名字。这是第五轮第 7 条那个缺陷的复发——**我自己写下的东西也要过同一道闸** |
| 5 | 修完第 1 条之后自己敲出来的：`sams source unroute a sina` 现在成功了，但回显打印「**a** 链里已移除 sina」——屏幕上说的形态与库里那一行不是同一个字（`calendar` 第四轮就定了「回显即归一」这条规矩，`source` 的三个动词漏了） | `route_`/`unroute_`/`move_` 的入参与回显都用 `canonical_market`；顺带把 `_markets`（`--markets a,HK`）自己那套 `strip().upper()` 也换成同一条规则——CLI 里再写一遍归一，等于允许「`--markets` 接受的形态」与「路由表里的形态」从此不一样 |
| 6 | 架构闸：第 2 条把 `code` 收到一个名字上，而 `market` 那半边在 `bars`/`symbols` 里**仍是第二种实现**（Polars 的 `strip_chars().to_uppercase()` 与 `canonical_market()` 各写一遍）。今天两条算出的结果恰好一样，所以这一轮没有错数——而「恰好一样」正是它危险的地方：域规则将来多一条，只有调函数的仓库跟着变 | **D52**：抽出 `storage/repos/_keys.normalize_keys()`，两个仓库的批量写侧共用它归一 `market` 与 `code`；顺手把「键的一半为空 ⇒ 丢弃 + warn」与 D46 那句「空市场名 ⇒ `ValidationError`」的差别写进文档（spec §5.5）而不是留给读者猜。`grep str\.to_uppercase\(\) src/sams` 现在零命中 |
| 7 | 架构闸（可选加固）：`SOLE_CONSUMER` 只扫 AST 里的标识符，`getattr(adjust, "hfq_frame")` 那个名字不作为 Name/Attribute 节点出现——绕过守卫的那条路连一行可疑代码都不留 | `_used_names` 增加 `getattr` 第二实参的字符串形态，并加一条**守卫自己的测试**（`test_the_guard_notices_a_reach_that_is_not_written_as_a_name`）：别名与字符串两种写法都要挡。起别名本来就挡得住（`ast.alias` 记的是原名），这条测试把「本来就」变成写下来的东西 |
| — | 复核通过的：`covered_to` 放宽挡得住（窗口超出断言范围、`full_history=False`、多段 span、非 A 市场逐条试过）；`gap_days`/`GapKinds`/退出码 0-1-3 的每种区间形状都对得上锚点；`grep except BaseException\|except:` 在 src 里零命中；本机库非规范行仍为 0 | 不动 |

实测验收（本机真实网络，库为 `data/sams.duckdb` 的一份副本，命令行只读数与做一次路由试演）：
`source unroute a sina` ⇒ 「A 链里已移除 sina」（rc 0），`route a sina --priority 4` ⇒ 「已把 sina
排到 A 的第 4 位」，`move a …` ⇒ 「A 链已重排」+ 大写形态的链；`analysis return --start 2024-07-15
--end 2024-07-01` ⇒ 「错误: 起日 2024-07-15 晚于止日 2024-07-01：这是一个空区间，不是「这段时间没有
行情」」（rc 1，不再教人跑一条同样空的 `sync daily`）；`--code HK00700` 与 `--code 700` 给同一条
D39 拒绝；`--market a` 的 601088 2024-07-01~07-15 仍是 **复权 −1.44%**（D50 之后 `HK 700 → 00700`
这一路改动没有挪动任何已有的数）。离线 `uv run pytest -q` **1028 passed / 8 deselected**，
`uv run ruff check .` 全绿。架构闸的复测（第 6/7 条）之后是 **1033 passed / 8 deselected**，
`grep -rn "str\.to_uppercase()" src/sams` 零命中，两半键的写侧实现只剩一份。

**A-7 第七轮：股票交易专家 agent 复测读数出口** ✅（代码 `c32d090`+`2dc549a`、文档 `7f90a9a`）

这一轮的题目不是「数算得对不对」——六个数全部与交易所口径对上了——而是**屏幕替读的人说了哪些没根据的话**。
八条里有一条是上一轮修法留下的（第 6 条的 `market` 那半边），其余七条全是「看起来正常的错数」的最后一类：
**数对、话错**。全部先写复现用例再修：

| # | 症状（能复现的那句） | 处置 |
|---|---|---|
| 1 | `--market HK --code 00700` 跨 2023-05-19 打「复权 −1.24% ← 采信这个 / 裸价 −1.94% ← 跨除权日时它是错数」，加一句「这与交易所公布的当日涨跌幅同一算法」 | **D53**：HKEX 从不为现金分红复权前收盘价，板上那天**就是** −1.94%。`core.codes.exchange_folds_dividend_into_daily_change()` 成为这条分叉的唯一来源：裸价行的标签、口径那两行、「这两个数该跟谁核对」三处一起按市场走。D48 说「声明要跟算式一致」，这一轮是它的另一半：**声明还得跟那个市场的规则一致** |
| 2 | `… --json; echo $?` 拿得到 3，而 `… \| grep 复权; echo $?` 恒为 0——筛选循环处处是管道 | **D57**：`--json` 进 payload 一个与进程退出码同值的 `exit` 字段。没有结构化出口时机器唯一的路是正则中文文案，而 0 与 3 那种最要紧的区分正好写在文案里——**让脚本读中文 prose 等于把文案变成接口** |
| 3 | `复权 −1.44% / 裸价 −6.38%` 这一对数从不说是哪件事造成的 | **D54**：`IntervalReturn.factors`（`DayFactor` 补 `cash`/`paid`/`multiplier`）出门，命令行逐条打印「2024-07-08 前收 45.10 → 参考价 42.84，每股现金 2.2600，因子 1.0528」。读的人要核对就得自己找公告、自己算参考价——而这三步守卫全都走过了，算完不出去等于白算 |
| 4 | 那个现金是**税前**额（源 `note` 写着「10派22.60元(含税,扣税后20.34元)」），屏幕上与交易员实际到手差 10–20% 的分红，而两者长得一模一样 | 同上（D54）：A 股跟着数打一条 ⚠ 说明含税折算与红利税。这一条同时把 **U22** 从「要不要加第三个数」改成「现在这个数属于哪一种持有期」——程序不知道持有期，只能给人一个显式开关 |
| 5 | 送转/拆股让股数变了，跨那天的「每股成本」不可直接比，而屏幕上没有任何一句话提醒 | 同上（D54）：`multiplier ≠ 1` 时打「这段里股数变了（×1.1000）……每股成本跨那天不可直接比」 |
| 6 | `拒绝出数：2023-01-05 有一条无法折算的公司行为` 没有主语——同时筛几只票时不知道是谁被拒；按建议跑 `sync actions` 回来后还是同一条拒绝 | **D55**：`adjusted_series` 给因子层的 `NotAdjustable` 补 `market/code：` 前缀；D39 那句明说 `unknown` 就是采集结果、重采不会变，并给出口（`止日 ≤ 前一日，或起日 ≥ 除权日`） |
| 7 | 把止日收到 01-04 就得到一个复权=裸价、rc 0 的「干净」数，而那天之后一条 ~5% 的实物分派折不出数——**躲开拒绝不等于穿过守卫**，屏幕在奖励收窄窗口这个动作 | 同上（D55）：`IntervalReturn.nearby_unpriceable` 把止日之后 7 个日历日内的 `unknown` 作为 ⚠ 随数打印 |
| 8 | D37 的拒绝写着「请传 `listed_on` 豁免」，而命令行没有这个参数、服务层也从不传（U18）——一句做不到的建议比没有建议更坏 | **D56**：`sams analysis return --listed-on YYYY-MM-DD` 一路透传到 `adjusted_series`；用了就跟着数打印「这个数没有 D37 那条证据」。豁免不是免费的，必须显式索取并显式回报。**U18 随之改写**：CLI 这条路通了，机器仍给不出那个值（库里没有 `symbols.listed_on`） |
| 9 | 第六轮第 6 条把 `code` 收到一个名字上，`market` 那半边留了第二种实现（Polars `strip().upper()` vs `canonical_market()`）——今天算得一样，所以这轮没有错数 | **D52**（上一轮已修，本轮复核）：`storage/repos/_keys.normalize_keys()` 是那条规则唯一的一份实现；`SOLE_CONSUMER` 另加 `getattr` 字符串形态的自检用例 |
| — | 复核通过、本轮不动的：六个锚点数（601088 与 00700 两段、除权日单日、反向区间、大小写市场、`700`/`00700`）一字未变——**这一轮改的全是措辞与出口，没有一个数被挪动**，这也是它能在一天内收口的原因 | 不动 |
| 11 | 交易员提出量/流动性、费用与基准对照（赔率的分母）——守卫管的是「这个数是不是真的」，不管「这个数能不能下单」 | 属 M2-B（指标与量能）与 M3（监控与阈值）的范围，本轮不夹带 |

**第七轮之后新增的债**：spec §18 **U23**（港美股的交易所为拆股复权、不为现金分红复权 ⇒ A 股那条
D37 跳变证据在 HK/US 只覆盖一半跳变，而双源到位前加这道闸等于让源自己查自己）；**U22** 改写为
「含税/到手属于现在这个数本身，不是第三个数」。

实测验收（本机真实网络，库为 `data/sams.duckdb` 的一份副本，命令行只读数）：
601088 `2024-07-01~07-15` ⇒ **复权 −1.44% / 裸价 −6.38%**（两个数与上一轮一字不差），下面多一行
`除权 窗口内 1 个除权日：2024-07-08 前收 45.10 → 参考价 42.84，每股现金 2.2600，因子 1.0528`
与一条含税 ⚠；`--market HK --code 00700 --start 2022-12-20 --end 2023-01-04` ⇒ 两个数都是 **+16.68%**、
裸价行改口为「交易所公布的那个数就是它：跨除权日时它不含分红，所以不是这段的总收益」，并带
`⚠ 止日之后 2023-01-05 有一条无法折算的公司行为（特别分配，1 美团 B 每 10 股派 1 股）：这段给得出数，
把止日挪到它当天或之后就给不出了`（rc 0 出数、话不说满）；600636 那段送转打「×1.1000」；
`--start 2024-07-10 --end 2024-07-15` 跨除权 ⇒ 拒绝文案带 `A/601088：` 主语与 `止日 ≤ …`/`起日 ≥ …` 出口；
`2024-07-01~07-20 --json` ⇒ `exit: 3`、`gap.trading` 为 07-16/17/18/19、`anchors` 与 `factors` 结构化；
`--market A --code 601999 --start 2024-07-01 --end 2024-07-15`（新股跳变）不加参数 ⇒ rc 1 且文案里就写着
`--listed-on`，加上 ⇒ 出数并打「这个数没有 D37 那条『不可能跳变』的证据」。
离线 `uv run pytest -q` **1049 passed / 8 deselected**，`uv run ruff check .` 全绿。
（这份副本里 HK 只铺到 2023-03-01，所以第 1 条用 2022-12-20~2023-01-04 那对锚点验，
而不是交易员报的 2023-05-19——**报告里的数不在当前库里时，用库里真有的那一段重跑同一条断言**。）

### B 块：指标引擎（§8.2）

先写死的三条约束（A 块这几轮的成果，别让 B 块重新踩）：

- **指标不许自己建读路径**：唯一取数出口是 `AnalysisService`（D44 的那三个事实一起进同一个闸）。
  绕过它去调 `bars.load` + `corp_actions.list_for`，等于把守卫变成装饰——第四轮已经为此收紧过分层。
- **入口类型是 `AdjustedSeries`，不是 `pl.DataFrame`**：否则「拒绝未复权序列」这句只能靠自觉，
  而 `hfq_frame` 的存在证明自觉靠不住（要靠 `SOLE_CONSUMER` 机器校验）。
- **先给 U20 那条序列出口定形**：`sams analysis series` 与指标共用同一条取数路径，列名与窗口语义
  在这一步定下来，不要各自发明。

- [x] `indicators.py` 全部为纯函数（输入已复权序列，输出带新列的帧），入口拒绝未复权序列
- [x] 黄金样例：MA/MACD/RSI/ATR 与手算/已知值逐位对齐；KDJ/DMI/CCI/WR 至少一条与公开样例对齐
- [x] `resample.py`：周/月 K 由日线现算（周五收市口径、非交易日跳过），不入库
- [x] **反未来函数专项**（spec §13）：篡改 T+1 及之后数据，T 日每个指标逐位不变（`test_tampering_with_the_future_leaves_the_present_untouched` / `test_computing_up_to_t_and_computing_the_whole_thing_agree_at_t` 按指标参数化）。**`intraday=true` 那半句没做，也不该在 B 块做**：它标的是「盘中未定型的日线参与合成」，而本系统还没有盘中快照采集器（`quotes_snapshot` 有表无写入方，U7），那一半**不在 D 块做**：D 块定过读数侧不落快照（D76），`intraday` 跟着 M3 的盘中采集与 `quotes_snapshot` 的写入方一起落
- [x] 三道闸 + 提交

**B 块已完成**（2026-10-19 收口，提交 `6dd97a3` 与三道闸修复 `e0a0d08`）：`sams analysis series
--period {day,week,month} --indicators …` 与指标共用 `adjusted_series()` 那一条取数路径，U20 闭合。

### C 块：基本面与资金流接入 ✅（2026-10-19，新增 D58–D65，闭合 U18）

按 `~/.qoder/plans/tranquil-pebble-shrew.md` 的 T-0…T-12 执行，任务与提交一一对应：

- [x] **T-0** 降级链与能力声明收敛成一份：`datasource/capabilities.py` 的 `CAPABILITIES` 是唯一词表，`registry._walk()` 是唯一的「按路由顺序、跳过缺能力的源、限速、归类失败/空答复」循环，`fetch_daily`/`fetch_actions` 各自那套**空答复语义**一字不动地改为调用它（提交 `d0e530c`）。上面那条「M2-C 开工前」的旧债就此勾掉。
- [x] **T-1** 迁移 0012：`financials.published_date`、`valuation_daily`、`funding_flow`、`symbols.listed_on`（`8196330`；**没有改任何已应用的迁移**，0004 的校验和闸会当场拒绝）
- [x] **T-2** `core/fundamentals.py`：比率/倍数/金额三份列名单、`from_percent`（**后来删掉了**，见闸次 7）、`RATIO_BOUNDS`、`caliber_note()`、`roe_basis_note()`（D58/D60，`cc73733`）
- [x] **T-3** `storage/repos/financials.py`（`FinancialRepo`/`ValuationRepo`/`FundingFlowRepo`）与 `scan_results.py`：幂等 upsert、PIT 读（`published_date <= as_of`，为 NULL 的行排除并计数成 `dropped_unpublished`）、缺字段存 null 不填 0，而 `financials` 收到 `pe_ttm`/`pb` 抛 `StorageError`（D60/D61，`df0032b`）
- [x] **T-4** `datasource/fundamentals.py`：`FinancialAdapter`/`ValuationAdapter`/`FundingFlowAdapter` 三个协议 + `normalize_rows()`——`÷100` 从此只在这一个函数里发生（D58，`5234d53`）
- [x] **T-5** 适配器：akshare A 股三链接通（按报告期财务指标 / 按交易日估值 / 资金流）、yfinance 港美股**当日**估值快照与无披露日的期间财务表（`672ea79`）
- [x] **T-6** `services/fundamentals.py`：`FundamentalSyncService`（三条链各走一次降级）与 `FundamentalService.read`（两个粒度在这里合成 PR，算不出的原因分三条诊）（`a647f84`）
- [x] **T-7** `analysis/fundamentals.py::pr()`：两条分支、四种拒绝、`basis` 随数出门、算不出不给 0（D59，`1d0d991`）
- [x] **T-8** `sams analysis fundamentals`——用户要的「查询时增加一个参数 PR」（`ad957f8`）
- [x] **T-9** `sams screen pr --pr-max 0.4`——用户要的「按市赚率选股」：第一行是真实边界、命中/落选/排除三堆（D64，`7e94f07`；`d7a7e1a` 补上「样本比主档多」那个方向，此前打印「范围外的 -1 只」）
- [x] **T-9.5**（中途的用户决策）「可以设置，默认用年报？」⇒ **D62**：`--roe-basis annual|latest`，默认年报，且那句话由 `core.roe_basis_note()` 生成、与两个口径各自的实测数一起进文档（`99c6059`）
- [x] **T-10** `sams health` 按市场报**只数**不报行数，并说出资金流与 `screen pr` 的能力缺口（D65，`ec45602`）；U18 闭合：上市日期由 `sync financials` 填进主档那一格、读用例自动取它并把**来源**随数打印，三处都取不到仍按 D56 拒绝（D63，`4843911`）
- [x] **T-11** 文档：新建 `specs/2026-10-18-market-profit-ratio-design.md`（用户点名要的那份，`1f09f66`）、本 spec 补 D58–D65 + §8.3 的 PR 那一行 + U 表（U18/U20 闭合、新增 U24/U25）、本计划 B/C 块按实际执行改写
- [x] **T-12** 三道闸（测试 / 架构 / 交易员）——**七轮 + 真实网络那一组，一轮没跳**（`995b6af` / `50fa780` / `3f07faf` / `14843f4` / `b8a7566` + 文档这一轮）。每轮修的都是同一类东西：一个「看起来完全正常的错数」或一句「屏幕上像实话而机器读者拿不到」的话
  - 闸次 1（口径闸）：`RATIO_BOUNDS` 那道小数死区改成**含边界**、单个谓词 `out_of_caliber`，`pr()` 自己挡非有限值，`--help` 崩溃修掉 ⇒ **D66**
  - 闸次 2（universe）：把「同步过而从未披露」的票算进输入面，`--codes` 里库中没有财务行的代码进第四堆 `unmatched`，对账等式 `hits + priced_out + excluded == universe` 才成立（D64）
  - 闸次 3（开关名实相符）：`--include-pb-branch` 真按名字生效，跨支不可比在屏幕上出声而不是排进同一个顺序里
  - 闸次 4（陈旧出声）：`analysis fundamentals` 打印 PE 那天离 `as_of` 几天并把「旧过 7 天」进 `--json` 的 `warnings` ⇒ **D67**（那个 7 从此两个出口读同一个 `STALE_VALUATION_DAYS`）
  - 闸次 5（存档身份）：`scan_results` 的规则身份**不含 `codes`**，所以 `--codes` 子集扫描**根本不写存档**且当场说出来；`--as-of` 落在今天之后按未来函数拒绝；`--pr-max` 要有限且不在比较精度下 round 成 0 ⇒ **D68**
  - 闸次 6（D60 双向）：`_prepare` 对三张表都拒未登记的列（原来是 `select()` 静默丢一列，「写成功而读回来没有」比一次 `StorageError` 贵），`valuation_daily` 反向拒比率列（否则市赚率会拿今天的 ROE 去配任何一天的 PE）
  - 闸次 7（文档与死代码诚实，写债时当场验出来的两条）：`core.from_percent()` **删掉**——模块文档与 PR 设计稿都把它说成「÷100 的那一处」，而生产里没有任何调用点（换算实际发生在 `normalize_rows()` 对着 `percent_columns` 的那一次；yfinance 那侧最终压根没映射比率列，`percent_columns=()`）。它自己那条单测的行为早就在 `tests/datasource/test_fundamentals_adapters.py::test_zero_stays_zero_while_unknown_stays_unknown` 里按真站点断言过，所以删函数不丢覆盖。另一条：**「这一期是不是年报」那句谓词原来抄了三遍**（`storage/repos/financials.py:58` 的 SQL、`cli/screen.py:_is_year_end`、`cli/analysis.py:_is_year_end`，后两份逐字相同），收成 `core.fundamentals.is_year_end()` 一处 + SQL 那一处的 docstring 点名它，并补一条先红的用例钉住 `None` 不是「年末」而是「不知道」
  - 真实网络那一组：修掉两条**自己写坏**的 live 用例（`corp_actions` 给全史而用例按 `[0]` 取窗口内那期；sina 那条按 `today~today` 请求，于是盘前/周末/假期必红），A 股资金流端点连续两轮通过后去掉 xfail（U25 收窄），Yahoo 429 那条改成只在 `RateLimited` 时 skip ⇒ 离线 **1522 passed / 13 deselected**，live **12 passed / 1 skipped**，ruff 干净

**T-12 之后剩下的债（P2，都还没咬人，但每一条都得在这里写下来才不算被抹掉）**：

- **退出码↔`--json` 的 `exit` 是九处手写映射**（D57 要求两者同值），散在 `cli/analysis.py` 七处与 `cli/screen.py` 两处。现在靠用例守着，没有机器闸：新增一个出口时忘记写 `exit`，`$?` 与 payload 就会各说一套。该收成一个「出口对象自带 exit，payload 从它生成」的形状（M3 之前）。
- **三份单位名单没有生产调用方，而真正的 ÷100 名单在采集侧另写了一份**：`RATIO_COLUMNS`/`MULTIPLE_COLUMNS`/`MONEY_COLUMNS` 只被 `tests/core/test_fundamentals_caliber.py` 的「三族不重叠 + 每列都有归属」那两条读；生产里那一次换算用的是 `akshare_cn.FINANCIAL_PERCENT = ("roe", "revenue_yoy", "profit_yoy")`——它是 `RATIO_COLUMNS` 去掉 `dividend_yield` 的手写子集，差那一列的理由写在模块文档里（东财那一列已经给的是别的形态），但**没有任何一条用例检查这个子集关系**。给 `RATIO_COLUMNS` 加一列而忘了 `FINANCIAL_PERCENT`，症状是新那一列静默不 ÷100。`ValuationRepo.covered_codes()`（`within_days` 那个参数）同样只有 `tests/storage/test_financials_repo.py` 调，采集端还没用它做「已覆盖就不重采」——名单先于消费者存在是对的，`covered_codes` 不是：到 M3 排程化仍没人调就该删。
- **`_walk()` 的四种结局由三个调用方各自分类**（`fetch_daily` / `fetch_actions` / `_frame()`，`registry.py:327`、`:373`、`:470`）。这是 T-0 **有意**留下的：链不许替取数种类决定「0 行算不算成功」（`_walk` 的 docstring 写了理由），第五种数据进来时要在**调用方**加语义而不是在链里加 `if`。代价是同一份 `skipped/failed/no_answer` 判断抄了三遍，加第四种取数就是第四遍——真要收敛，收敛点是「分类器 + 各自的后处理」，不是把判定塞回链里。
- **顶层 `sams --help` 里 `sync` 那一行只点名了 `daily`/`actions`**：`sams sync --help` 五条都列（`{daily,actions,financials,valuation,flow}`），所以这不是命令缺失，是**顶层帮助落后于子命令**。为什么算债：D64/D67 的下一步提示（`先跑 sams sync valuation`、`先跑 sams sync financials`）是用户从帮助里抄的，而帮助那一行读起来像「采集只有两件事」。修法要么把那句改成不逐个点名，要么补全五条——顺手可修，不该由一条 PR 顺带做。
- **主档那一格上市日不记来源**：`SymbolRepo.fill_listed_on` 只在 `listed_on IS NULL` 时写，`services/fundamentals._fill_listed_on` 把「这次是哪个源给的」只写进**当次日志**（`logger.info`）。库里 `symbols` 没有 `listed_on_source` 这一列，所以两只票由不同源填入时事后看不出区别，而 D37 的豁免窗口跟着这个日子走。这是**有意**的（覆盖会带来更大的错：换一家源就换一天），但读的人该知道库里答不了这个问题——真要补，补的是那一列而不是那个覆盖。
- **`services/health.py` 用三段裸 SQL 重算聚合**（`_COVERAGE_QUERIES`，`services/health.py:111-139`）。它同时 import `Database`、`storage.migrate.pending` 与别的仓库（`services → storage` 这一条边分层闸是放行的，所以这不是过闸漏洞），但「只数」这条口径因此在仓库之外又写了一遍：`flow_latest` 那种「交易日 vs 日历日」的差别将来一改就容易只改一处。收进仓库层是 M3 的事（`sams health` 的出口形状那时也要跟着通知层定）。

**计划里定过而有意不做的四件事**（不是漏掉；每一件的理由都要能当场验）：

- `--watchlist` / `--keep-empty`（原 T-9 列过）：自选股是**用户域**的票池，而这条命令的第一句诚实话是「输入是同步过基本面的票」（D64）。先加 `--watchlist`，读的人会以为「我的自选都扫过了」，而库里多数自选票根本没有财务行——那正是这条命令最要出声的一种沉默。`--codes` 已经能表达同一件事且不带那种误读；真要做，得连「自选里有几只压根没同步过」一起说清（M3 排程化时一并定）。
- `--stale-after-days` 只做成了 `ScreenService.pr_max` 的参数（默认取 `core.STALE_VALUATION_DAYS = 7`），没做成命令行开关：屏幕第一行已经把「旧过 7 天的票进排除」说出口，而改这个数的需求还没出现（YAGNI）。闸次 4 之后这个 7 是**两个出口共用的一份**（D67）：读 PE 的那条命令与按 PE 选股的那条各写一个字面量时，同一个 PE 会在一处叫「旧过」、在另一处叫「新鲜」，而屏幕上两句都像实话。
- 原 T-5 的「迁移 0013 给 `data_sources` 补这三个方法的能力位」：**没有 0013**。能力位由 `capabilities.provided_by(适配器类)` 在注册时从「方法是否存在」推导，库里再存一份就是第二份声明——那正是 T-0 收敛掉的东西（D65）。漂移的方向恰好是「看起来有这个能力，调用了才知道没有」。
- 原 T-9 写「空 universe 时退出码 0」，实现是 **1**：「扫了而篮子空」是 0 并打印「空篮子不等于市场不便宜」，「压根没有输入」是 1 并给采集命令。把两者都记成 0，脚本就无法区分「今天不便宜」与「今天没查」——这是 D43 那条「0 行 ≠ 成功」在选股出口的另一半。

### D 块：估值合成（§8.3）✅（2026-10-19，新增 D69–D76）

范围在这里收过一次刀：只做**读数侧**（分析层 + 一个命令行出口），合成结果不落库、不接告警（D76）——落库会把一个随输入补齐而变的数变成历史事实，而 M3 的触发器要的是「规则在某时刻命中」并自带存档身份（D68）。

- [x] **T-13** `analysis/valuation/percentile.py`：PR / PE / PB 三条 PIT 历史分位（`b9ee822`）。`MIN_SAMPLES = 250`（样本不足时当场拒，不报一个「0 分位」）、窗口 `DEFAULT_WINDOW_YEARS = 5`、并列计入分子、**截至日自己不投票**（`samples = 天数 - 1`）。PR 那条逐日挑分母那一期用的是与 `analysis fundamentals` 完全同一个谓词（`published_date ≤ 那一天`，`NULL` 不采信且单独计数）⇒ **D69**
- [x] **T-14** `styles.py`：内置 `value` / `trend` 两套权重与参数词表（`e8c871a`）。权重和必须为 1、`min_covered_weight` 不得 > 1、参数从配置读且 `with_overrides` 可复制；`trend` 压根不问布林位置那一格（那正是 D72 要防的「同一个 %B 两套解释」）
- [x] **T-15** `scoring.py` 十二个分项 + `compose.py` 五个价位（`2bbd2dc` / `3a644d2`）。每个分数带 `evidence`（来源字段 + 计算窗口）；缺输入 ⇒ 逐项拒绝、**不补一个中性的 50**、`score = Σ(分×权重) ÷ 覆盖权重`、覆盖不到地板就整分不出（**D70**）；入场那道分位闸门与权重表解耦（**D71**）；布林只读回调、突破归 M3（**D72**）；周MA20/ATR/前高按「现价 ÷ 当日后复权收盘」折回现价口径（**D73**）；资金流窗口 `FLOW_WINDOW_DAYS = 5`、「这几天没行」与「这列不存在」分两句、「过去 N 日」说真查过的根数（**D74**）。`win_rate` 留空并明说「未回测」，`win_rate_samples < 30` 不许参与排序（M5 回填）
- [x] **T-16** `services/valuation.py` 那一个读取用例 + `sams analysis valuation`（`e0a2974` / `bcef2f5`）：四路输入（过闸日线、日线现算的周线、按日估值、按报告期财务、资金流）在这里决定取哪几个数；CLI 把合成分拆成一张逐项表打印，缺的那几行是 `–` 而不是 0，`--json` 每个出口都带 `exit`（D57）。容器里 `ValuationService` 接在 `FundamentalService` / `AnalysisService` **之上**，不是并列的第三路取数
- [x] **T-17** 三道闸：
  - **测试闸**：把 §14 第 3 条欠的「估值那一半」补上——`test_a_read_at_t_does_not_look_at_anything_after_t` 把 T 之后改写成另一段行情（PE 掉到 1、价格继续上坡、一期**尚未披露**的年报、一笔资金流），T 日那份读数（现价、PR 与它那一期的分母、三条分位的样本数/窗口/当前值、合成分与 12 行证据、五个价位、三个技术项、`flow_days`、`notes`）**逐位不变**；同一段数在 `as_of` 挪过去之后第一次被读到，所以那条不是空跑。黄金用例 `test_trade_plan_hand_computed_value_chain`（T-15 就写）把整条数链拿计算器对上
  - **交易员闸**：真跑 `sams analysis valuation --market A --code 601088 --as-of 2024-07-11` 那张屏。两条改的都是屏幕而不是算式：合成分那行实测 1500 字、把整张缺项表复读一遍 ⇒ 只报「有没有分、几分、几成权重、缺几项」，全文留 `--json`（**D75**，`34fa9fd`）；`volume_follow` 的证据里 `-0.0007192519779428697` 要读的人先数零才知道是跌 0.07% ⇒ 改成 `-.2%` 与带千分位的量
  - **架构闸**：`tests/test_layering.py` 仍绿，`cli` 只 import `core` + `services`，所以样式名经 `services/valuation.STYLE_NAMES` 再出口；样本数据抽进 `tests/support_valuation.py` 一份，命令行侧与读数侧对着**同一批数**点头（分叉的两份样本各自验各自的数）
  - 离线 **1657 passed / 13 deselected**，`uv run ruff check src tests` 干净

**D 块定过而有意不做的三件事**：

- **快照落库 / `intraday` 标记**（D76，仍属 U7）：写入方是 M3 的盘中采集，那里还要带「这次是盘中还是收盘后」。
- **告警接线**（§11）：合成分不是一个可告警的事件——它每天随输入补齐而变，而告警要的是一句能被复述的「规则在某时刻命中」。
- **`boll_position` 读突破**（D72）：那是跨日事件，需要读数侧存前一日状态；读数用例保持无状态，事件归 §10.3 的规则模板。


### E 块：thesis 生命周期 + `sams thesis show`（§9.1，M2 验收项）

- [x] `repos/theses.py`：状态机合法跳转表在 storage 层强制，非法跳转拒绝；`thesis_events` 每次变更写一条；用户隔离同 watchlists（T-18，D77；时间线次序见 D80）
- [x] `services/theses.py`：创建（observing）、提出（proposed）、建仓（opened，校验止损/目标/赔率齐备）、归档拒绝（T-19；价位只落算得出的那一栏，D78）
- [x] `sams thesis show` 文本报告：合理区间、买入区、止损、目标、赔率 + 分项打分与 evidence 展开（`show` 并排列「库里那次 / 当前这次」两次测量并明说不判漂移，D79；完整 evidence 出口在 `sams analysis valuation`，屏幕上那条命令带着 market/code/style 可直接敲）
- [x] 验收：任一自选股输出估值报告，每个分数能点开看到数据来源
- [ ] 三道闸 + 提交

---

## 验收清单（spec §14 M2）

1. ✅（M2-A）`sams sync actions --market A --code 601088` 后，跨 2024-07-08 的区间收益与交易所公布值一致；未采集该股时同样调用被拒绝并说明原因。
2. ✅（M2-E）`sams sync --market A --code 600519` 后 `sams thesis show` 输出估值报告：fair/entry/stop/target/reward_risk + 分项打分，每项带 evidence。屏幕上那四栏是「库里那次 / 当前这次」两次测量并排（D78、D79），合成分与 PR 在同一行；逐项 evidence 的完整出口是这一屏末尾那条 `sams analysis valuation --market … --code … --style …`（D75），它当场可敲、不需要用户再拼参数。
3. ✅指标那一半（M2-B）+ ✅估值那一半（M2-D T-17）：篡改 T+1 之后的日线，T 日全部指标逐位不变（两条参数化用例）；把 T 之后改写成另一段行情（PE 掉到 1、价格上坡、一期尚未披露的年报、一笔资金流），T 日那份估值读数逐位不变（`test_a_read_at_t_does_not_look_at_anything_after_t`），且同一段数在 `as_of` 挪过去之后第一次被读到。
4. ✅（M2-B）周线指标由日线现算，库里没有 `bars_weekly` 之类的新表。
5. ✅（M2-E）非法状态跳转（未建仓直接归档）在仓库层被拒（`StorageError` ⇒ 退出码 1，认不出的状态名 ⇒ `ValidationError` ⇒ 2）；每次合法跳转都有 `thesis_events`，开局那条 `from_status` 为 NULL，次序按 `ts, rowid`（D77、D80）。
6. `uv run pytest -q` 全绿；`uv run ruff check .` 干净；分层守卫不因新层放宽。
7. ✅（M2-C，用户要的第三条与第四条）`PR = PE ÷ ROE ÷ 100`（PE 不为正时 `PB ÷ ROE ÷ ROE ÷ 100`）在 `sams analysis fundamentals` 里有出口，`sams screen pr --pr-max 0.4` 出一篮子并按 PR 升序；单位自检（`PE=20 / ROE=0.20 ⇒ 1.00`）与「把 `--as-of` 挪到披露日之前，那一期的 ROE 与由它算出的 PR 一起消失」都有用例钉住。

## 三道闸的执行形态（每块都走一遍）

| 闸 | 派什么 | 关注点 |
|---|---|---|
| 测试 | `Agent(Explore 或 general)` 对抗测试本块改动 | 造脏数据、越权调用、时间边界；不接受「理论上不会发生」 |
| 架构评审 | 独立架构师 agent | 边界是否清晰、新层有没有开后门、口径是否只有一处定义 |
| 交易员试用 | 股票交易专家 agent | 拿真实输出做决策：数字可信吗、算不出时说的是不是人话、有没有被静默误导 |
