# 个股页技术面数值卡（R3-15 / D356）

**关联**：R3-15（第三轮专家实测）、R3-01（复权闸拒数不掀桌）、R3-03（技术面覆盖）、
D36/D37（复权闸）、D49（覆盖只问窗口）、D251–D253（技术面读模型）、
D283（网页不出现命令行，缺数走补采队列）、D290（图算不出来就说那一段没数可画）。

## 背景

R3-15：`/symbol/A/600519` 与其它个股页**没有任何技术面数值**——MA5/10/20/60、量比、
52 周位置、区间涨跌幅全无。MA 只在图上画成线，而那条线的基是后复权，与头部不复权
现价不可比；「新鲜度」只写「11-03 收盘」，不知道本地是什么时候写进来的。

选股页已经有六个技术面字段（`services/technical.py`），但那是「相对均线的偏离」，
不回答「均线在哪、这只票在 52 周里的哪个位置、最近 20/60 个交易日涨了多少」。
它们也没有上到个股页。

## 现状与约束

1. 图表走 `AnalysisService.series`：加载库内全部日线，按覆盖确认决定预热下界
   （`_warmup_span`），过 `adjusted_series` 后算指标，再把表头截到请求窗口。
   所以同一只票无论用户选多长的窗口，`close_hfq` 与均线的绝对水平都是同一个基
   （全历史确认时锚在库内首根）。
2. 头部现价是**不复权**的报价（盘中是实时价、休市是最后一天裸收盘），与均线不同基。
   这一条不能靠「把均线乘回去」解决：乘回去的数字没有任何交易所口径可对账。
3. 打开个股页已经在 `_require_symbol` 里调 `symbol_interest.ensure`，覆盖未确认时会
   `enqueue(..., ACTIONS)`（D283/D144）。所以「已排进后台补采队列」在页面上是一句
   现在就能兑现的承诺。
4. 网页侧不许出现 `sams …` 命令（D283）。

## 方案

### 服务层：`symbol_metrics`

在 `services/technical.py` 增加一个**吃已过闸序列的纯函数**（单票读数）：

```python
@dataclass(frozen=True)
class SymbolMetrics:
    values: dict[str, float | None]     # ma5/ma10/ma20/ma60/chg_20d/chg_60d/rsi14/vol_ratio_5d/pos_52w
    reasons: dict[str, str]             # 缺数的那几格为什么没有数
    as_of: date                         # 最后一根 K 线的日期
    written_at: datetime | None         # 那根 K 线本地最后写入时刻（bars_daily.updated_at）

def symbol_metrics(read: SeriesRead, actions: pl.DataFrame) -> SymbolMetrics
```

实现要点：

* **与图同基**：函数不碰仓库、不自己拼复权序列，只吃页面已经拿到的那一份
  `SeriesRead`（页面调 `AnalysisService.series` 拿，见下）。同一份序列让卡片上的
  MA 与图上那条线逐字相等；`close_hfq` 的锚点、预热下界、覆盖闸全部由图表那条路复用，
  不存在第二套口径，也不需要改 `TechnicalService` 的构造参数或 `ServiceContext`。
* MA 取 `read.series.frame` 末行（`ma5/ma10/ma20/ma60` 已由 `series(indicators=...)`
  算好）；`chg_20d`/`chg_60d` 用 `close_hfq` 的第 −21/−61 根；`rsi14` 来自同一张指标表；
  `vol_ratio_5d` 用**不复权** `volume` 末 6 根，并保留「近 6 个交易日有送转/拆股则口径
  不成立」那道检查（与选股共用算法，抽成小函数，不复制第二份）；`pos_52w` 用
  `close_hfq` 末 250 根。
* 缺口逐格说：MA60 要 60 根、RSI 要 15 根、量比要 6 根、52 周位置要 250 根，
  各写各的「日线不足 N 根」；不因长窗口缺数抹掉短窗口。
* `as_of`/`written_at` 取末行的 `date` 与 `updated_at`（列不存在时 `written_at` 为 None）。

### 页面层：`/symbol/{market}/{code}` 的技术面卡

取数（只在 A 股）：

```python
latest = ctx.bars.latest_date(market, code)          # None ⇒ 不画卡（空态卡已说）
start = min(day, latest) - timedelta(days=400)
read = ctx.analysis.series(market, code, start=start, end=day,
                           indicators=("ma", "rsi"),
                           windows={"ma": (5, 10, 20, 60)})
metrics = symbol_metrics(read, ctx.corp_actions.list_for(market, code, until=day))
```

卡片在图表之后、资料块之前加一张 `id="technical"` 的卡片（服务端渲染，不新增异步端点）：

* 表头：`技术面 <span class="muted">后复权 · 与图中的均线同基</span>`；
* 九行：MA5 / MA10 / MA20 / MA60（后复权均线值，两位小数）、20 日涨跌 / 60 日涨跌
  （后复权收益率）、RSI14、量比（×，不复权成交量）、52 周位置（百分比）；
* 缺数的格写 `—`，原因写在同一行的说明列（不把「无数」变成无解释的横杠）；
* 卡片脚注：`K 线截至 <as_of>（本地最后写入 <written_at>）；量比用不复权成交量，
  均线与涨跌用后复权收盘，不是头部现价`；
* 卡片整体状态（`state`）：
  * `""`：出表；
  * `coverage`（`CoverageMissing`）：`公司行为覆盖未确认：打开本页已把它排进后台
    补采队列，补完这里自动出数。`
  * `rejected`（其它 `NotAdjustable`）：`复权序列被拒绝：原因见图表上方那一段，
    技术面读数等它恢复后自动出现。`（**不转述异常原文**——原文里有上市首日豁免命令）
  * `no_bars`（有主档但库里一根 K 线都没有）：不出卡，页面已有空态卡说这件事；
  * `market`（港美股）：`技术面字段 v0 只对 A 股开放。`
* 页面所有字符串不含 `sams `。

## 接口与不变式

1. 卡片数值只到 `clock.today()`：`series(end=day)` 是同一把 PIT 锁。
2. 复权只有一个出口：卡片不直接拿 `bars` 算价格类字段。
3. 后复权口径上屏必须标注；卡片不得把均线值与头部现价并排成「同一把尺」。
4. 缺数逐格说原因；整片缺数说一句可兑现的下一步（队列/图表说明/市场不支持）。
5. 页面不出现命令行（D283）。
6. 本次不改选股那六个字段的算法与名单（R3-03 的覆盖普查是另一件事）。

## 测试（先红后绿）

`tests/services/test_technical_metrics.py`（新）：

* 60 根已知收盘 + 全历史确认：MA5/10/20/60 与手算一致；`chg_20d`/`chg_60d`、
  `pos_52w`、`vol_ratio_5d` 与算法定义一致；`as_of` 是末根日期，`written_at` 非空。
* **同基**：同一只票用 `AnalysisService.series` 取 `ma20` 末值与
  `symbol_metrics(...).values["ma20"]` 相等（这条钉的就是「图上的线落成数」）。
* 30 根：MA60/`pos_52w` 为 `None` 且原因写「日线不足」；MA5/MA20 照常有数。
* 近 6 个交易日有拆股：量比没有数，原因是「量比口径不成立」。
* 港美股：页面不取序列、卡片直接说 v0 只对 A 股开放（页面用例钉）。

`tests/web/test_symbol.py`：

* A 股页面 200 且包含「技术面」、`后复权`、`MA20` 与当日数值、`K 线截至`；
  全页不存在 `sams `。
* `coverage=False` 的页面 200，卡片写「已把它排进后台补采队列，补完这里自动出数」，
  且 `backfills_store.open_for(..., ACTIONS)` 非空（已有用例的断言上加卡片措辞）。
* 未标记跳变导致的 `NotAdjustable`：卡片写「复权序列被拒绝」，全文仍无 `sams `。
* HK 页面 200，卡片写「技术面字段 v0 只对 A 股开放」，不出现 MA 数值。

## 验证

* 串行：目标三组（technical metrics / symbol web / screener technical）、ruff、全量 pytest。
* 生产库副本：`/symbol/A/600519` 与 `/symbol/HK/00700`（`sams serve` 指向副本端口），
  核对卡片上 MA20 与 `/api/symbol/A/600519/bars` 返回的 `main.ma.ma20` 末值一致；
  核对 `as_of` 与库内 `max(date)` 一致、`written_at` 不是空串。
* 生产重启后 `/symbol/A/600519` 200、`/healthz` 200。

## 范围外

* R3-03 的覆盖普查（让技术面从 9% 的市场扩开）：需要 D1 的产品决策，不在本步。
* 港美股技术面：日线与公司行为覆盖都不足，v0 维持「只对 A 股」。
* 图上悬停读数与卡片联动（前端交互）：本步只做服务端卡片。
* 用户自定义均线窗口是否同步到卡片：本步固定 5/10/20/60，与 R3-15 点名的四个窗口一致。
