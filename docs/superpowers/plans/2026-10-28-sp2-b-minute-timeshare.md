# SP2 计划 B：分时图 + 分钟 K 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkboxes (`- [ ]`) for tracking.

**Goal:** 给个股页加上「分时」与 `m1/m5/m15/m30/m60/m120` 六档分钟 K，数据来自腾讯与新浪的分钟接口。

**Architecture:** 两条新能力（`minute_series` / `minute_bars`）挂在唯一那份 `CAPABILITIES` 判据上，走现成的降级链 `_walk`；新增 `src/sams/services/minute.py` 负责「谁能答 / 累计量差分成每分钟 / 按 `adj_factor=1` 组装成 `AdjustedSeries`」三件事；Web 层加 `/api/symbol/{market}/{code}/minute` 与 `bars` 的分钟分支，`chart_partial` 多一条「分时 | K线」切换与置灰话术，前端 `app.js` 按 `data-chart-kind` 分两条渲染路径。分钟数据一律**不入库**：它只在被看的那一刻有意义，落库会带来一套永远追不平的增量维护。

**Tech Stack:** Python 3.12 · polars · FastAPI + Jinja + htmx · 本地 vendored ECharts · pytest · uv

**Spec:** `docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md`（D163/D164/D165、§5.1–§5.3、§6 钉 7/8/9、§7 第 2 项）

**House rules（每一条都会在某一步咬人，先照抄）**

- 只用 `uv` 跑东西：`uv run pytest …`、`uv run ruff check --no-cache src/sams tests`。**不许 `ruff format`**。
- 全量测试只由 controller 跑，命令不带管道；单条用例自己跑。
- git 一律普通 Bash 命令、只 add 明确点名的文件，**永不用 `git add -A` / `git add .`**；只做新提交，不 amend；只有 controller 推送。
- 不要碰 `qodercli_resume.sh`，也不要动 `tmp/` 里不是本轮创建的文件。
- 注释只在「为什么」不自明时写：中文、一行。
- TDD：每条行为先跑到红（红的原因必须正是「这条能力还不存在」），再写最小实现到绿。
- 分层守卫 `tests/test_layering.py`：`web` 不许 import `datasource`/`analysis`；`SOLE_CONSUMER` 守住 `hfq_frame`/`day_factors`/`ex_reference`/`group_by_ex_date`——**分钟服务不许调 `hfq_frame`**，它自己用 `pl.lit(1.0)` 组装（D164）。
- 边界处用 `canonical_market` / `canonical_code` 归一；不要把 `TIMESTAMPTZ` 读回 Python。
- 迁移号已用到 `0022`，本期**不新建迁移**（分钟数据不入库）。
- 本机 `sams serve` 跑在 **8765**，它握着 `data/sams.duckdb` 的写锁：CLI 与线上验证的先后顺序见 Task 14。

---

## 0. 先读这份「实测事实」

下面这些是 2026-10-28 上午用真接口量出来的，不是从文档抄的。构造解析时照着它，别按想象改。

**腾讯分时 `https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=<symbol>`**（必须带 `Referer: https://gu.qq.com/`）

- 外层 `{"code":0,"msg":"","data":{<symbol>:…}}`；`data.<symbol>` 的键是 `["data","qt","mx_price"]`（港股那条还有 `vcm`）。
- 分时序列在 `data.<symbol>.data.data`（**`data` 套了两层**），日期在 `data.<symbol>.data.date`。
- 一行是空格分隔的四个字段：`"0930 1354.98 1537 208260426.00"` = 时刻、价格、**累计**量、**累计**额。A 股的量是「手」，成交额单位是元。
- 当天上午实测 98 行（`0930…1107`）；全天收完是 267 行、尾端报到 `1530`，其中 `15:00` 之后那几行不是连续竞价。
- 昨收在 `data.<symbol>.qt[<symbol>]` 的第 5 格（下标 4）。实测 `sh600519` → `1355.90`。**`qt` 里还混着 `market` 与 `v_ff_sh600519` 两个键**，所以只能按符号取，`next(iter(qt))` 会端回一个整数或列表（这条已单独有用例钉住）。
- 港股实测：`0930 384.000 510600 196105924.933`，累计量的单位是**股**；`qt` 第 5 格 = `385.200`（昨收）。收盘后尾巴会报到 `16:08`，比日历的收市时刻晚 8 分钟。
- 美股实测：只有一行三个字段 `1600 347.10 39984395`，而 `data.date` 是**空串**——它答不了「哪一天」。美股分时因此直接不给这一档（D165）。

**腾讯分钟 K `https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=<symbol>,m<scale>,,<count>`**

- **只有裸 `/app/kline/mkline` 这一个端点认**。`/app/kline/mkline/get` 与 `web.` 那个 host 都拿不到数据——别照着日线的 `KLINE_PATHS` 习惯加 `/get`。
- `param` 第三格是复权位，**留空但必须留着那个逗号**；少一个逗号接口答 `{"code":-1,"msg":"param error","data":[]}`。
- 节点键实测 `["m5","prec","qt"]`：序列在 `data.<symbol>.m<scale>`，`prec` 是昨收（实测 `1355.90`，这个字段用不上，别当 `prev_close` 的替身去绕）。
- 一行八格，顺序是 `["202610191405","1333.50","1333.90","1334.13","1333.32","588.00",{},"0.4704"]` = 时刻、开、**收**、高、低、量（手）、一个空对象、成交额。收在第二格——与日线那行「收在第三位」不是同一套位置，照抄会得一根根反蜡烛。
- 最后一格的成交额单位对不上（`0.4704` 对 588 手），与新浪同规模的分钟额（`14644871.9688` 元）差五个数量级。**这一档的 `amount` 一律置 `null`**，并让 Task 3 那条测试反过来钉住它：拿一个单位没核清的字段算出来的东西看起来都正常。
- `m1/m5/m15/m30/m60/m120` 六档实测都能答。
- 港股/美股实测**永远** `{"code":-1,"msg":"param error","data":[]}`。

**新浪分钟 K（复用日线那个 `KLINE_URL`）**

- `params = {"symbol": "sh600519", "scale": "<1|5|15|30|60|120>", "ma": "no", "datalen": "<根数>"}`，`scale` 就是分钟数。
- 行形态：`{"day":"2026-10-28 11:05:00","open":"1331.350","high":"1331.430","low":"1331.280","close":"1331.410","volume":"11000","amount":"14644871.9688"}`。`day` **带秒**，比日线那条 `YYYY-MM-DD` 多一段时间。
- 成交量单位是**股**（与日线一致，`volume_scale=1.0`），成交额单位是**元**。
- 没有「这个 scale 不支持」这种回复：`scale` 填错就只回 `[]`。`count` 是真被 honor 的，所以**不需要**任何「每档上限」表。

---

## 1. 文件结构

| 文件 | 责任 | 本轮 |
| --- | --- | --- |
| `src/sams/datasource/minute.py` | 两条分钟能力的探针、分钟列集常量、空帧、以及那张「愿不愿意答这个市场」的意愿表 | 新建 |
| `src/sams/datasource/capabilities.py` | 唯一那份数据种类清单 | 加两行 |
| `src/sams/datasource/sources.py` | 源注册表 | `_NOT_ADAPTERS` 加 `"minute"` |
| `src/sams/datasource/registry.py` | 降级链 | `_walk`/`_call` 透传 `extra`，加两条 fetch |
| `src/sams/datasource/tencent.py` | 腾讯适配器 | 加 `minute_series` + `minute_bars` |
| `src/sams/datasource/sina.py` | 新浪适配器 | 加 `minute_bars` |
| `src/sams/services/minute.py` | 分钟读取服务：可用性、分时数学、分钟 K 组装与缓存 | 新建 |
| `src/sams/services/sources.py` | `capability_labels` | 加市场维意愿过滤 |
| `src/sams/bootstrap.py` | 装配 | `ServiceContext.minute` |
| `src/sams/web/pages/symbol.py` | 个股页与两个 JSON 出口 | `/minute`、`bars` 的分钟分支、`chart_partial` 的 `view` |
| `src/sams/web/templates/partials/chart.html` | 那一块局部 | 视图切换 + 置灰 + 图注交给变量 |
| `src/sams/web/static/app.js` | 画图 | `samsChart` 按 `data-chart-kind` 分两条 |
| `src/sams/web/static/app.css` | 样式 | `.chart-group a.dim` |
| `tests/support.py` | 测试替身与样本帧 | 三个新件 |
| `tests/web/conftest.py` | Web 测试工厂 | 分时替身 + 工厂 |

测试文件（新建）：`tests/datasource/test_minute.py`、`tests/services/test_minute.py`、`tests/web/test_symbol_minute.py`。其余都改在既有文件里。

---

## Task 1：两条分钟能力的探针与登记

**Files:**
- Create: `src/sams/datasource/minute.py`
- Modify: `src/sams/datasource/capabilities.py`（import 区 + `CAPABILITIES` 末尾）
- Modify: `src/sams/datasource/sources.py:29-40`（`_NOT_ADAPTERS`）
- Test: `tests/datasource/test_minute.py`（新建）、`tests/datasource/test_capabilities.py:24-36,62`、`tests/datasource/test_fundamentals_adapters.py:253-`、`tests/datasource/test_sources_pluggable.py:74`

- [ ] 写失败测试：`tests/datasource/test_minute.py`

```python
"""分钟粒度：两条能力的探针、列集与那张意愿表（计划 B）。

`answers` 必须与适配器自己的拒答共用一次判定，否则「拒绝的适配器」与「链自己挑的源」
是两套说法——屏幕会说「这一档点不动」，日志却记着「腾讯正常应答、只是没数据」。
"""

from __future__ import annotations

from sams.datasource.minute import (
    MINUTE_BARS_COLUMNS,
    MINUTE_SERIES_COLUMNS,
    answers,
    empty_minute_bars,
    empty_minute_series,
    has_minute_bars,
    has_minute_series,
)


class _OnlyBars:
    name = "only_bars"
    markets = frozenset({"A"})

    def minute_bars(self, market, code, start, end, *, scale, count):
        return None


def test_the_two_probes_only_ask_whether_the_method_is_there():
    """探针不碰 `markets`：市场维在 `answers` 那张表里问，两处各管各的。"""
    assert has_minute_bars(_OnlyBars())
    assert not has_minute_series(_OnlyBars())


def test_the_minute_column_sets_share_the_daily_shape_where_it_counts():
    """分时与分钟 K 的列集只有一份出处：适配器、服务、测试三处各拼一遍必漂移。"""
    assert MINUTE_SERIES_COLUMNS[:4] == ["market", "code", "date", "time"]
    assert MINUTE_BARS_COLUMNS[:6] == ["market", "code", "date", "clock", "open", "high"]


def test_the_empty_minute_answer_has_the_same_shape_as_a_real_one():
    """调用方不该为「这个源一条都没给」写分支：空帧的列集与真帧一致。"""
    assert empty_minute_series().columns == MINUTE_SERIES_COLUMNS
    assert empty_minute_bars().columns == MINUTE_BARS_COLUMNS


def test_answers_defaults_to_yes_when_the_source_declares_no_table():
    """没声明 `<方法>_markets` 的源按「都答」处理：漏声明不是拒绝的理由。"""
    assert answers(_OnlyBars(), "minute_bars", "US")


def test_answers_reads_the_per_method_willingness_table():
    """一张表两档各自可关：腾讯答得了分时、答不了美股，也答不了任何市场的分钟 K。"""

    class Fussy(_OnlyBars):
        minute_bars_markets = frozenset({"A"})

    assert answers(Fussy(), "minute_bars", "A")
    assert not answers(Fussy(), "minute_bars", "HK")
    assert answers(Fussy(), "minute_series", "US")  # 没声明 = 不关这一档
```

- [ ] 跑到红

```bash
uv run pytest tests/datasource/test_minute.py -q
```

预期：收集期就失败，`ModuleNotFoundError: No module named 'sams.datasource.minute'`。这就是对的原因——模块还不存在。

- [ ] 实现：新建 `src/sams/datasource/minute.py`

```python
"""分钟粒度两条能力的探针、列集、空帧与意愿表。

与 `quote.py` / `actions.py` 同一位置：这类「一种数据一小节」的模块都只放纯函数与常量，
不 import registry，所以 registry、服务、Web 三层都能拿它做同一份判定。

`answers` 为什么和探针分开：探针答的是「这个适配器有没有这个能力」（`CAPABILITIES`
唯一判据的市场无关那一半），而「这个市场它答不答」按 `<方法>_markets` 走。
同一个 `minute_bars` 方法在腾讯上是 A 股能答、港股不能答，塞成一个判据就必然
在某一侧说谎。
"""

from __future__ import annotations

from typing import Any

import polars as pl

#: 分时的一条记录：某一天某一分钟的一个价（腾讯给的就是这个形状）
MINUTE_SERIES_COLUMNS: list[str] = [
    "market",
    "code",
    "date",
    "time",
    "price",
    "volume",
    "amount",
    "prev_close",
    "source",
]
#: 分钟 K 的一根：`clock` 是这一根的**结束时刻**（"HH:MM"）
MINUTE_BARS_COLUMNS: list[str] = [
    "market",
    "code",
    "date",
    "clock",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
]

_MINUTE_SERIES_TYPES = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "time": pl.Utf8,
    "price": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "prev_close": pl.Float64,
    "source": pl.Utf8,
}
_MINUTE_BARS_TYPES = {
    "market": pl.Utf8,
    "code": pl.Utf8,
    "date": pl.Date,
    "clock": pl.Utf8,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "source": pl.Utf8,
}


def has_minute_series(obj: Any) -> bool:
    return callable(getattr(obj, "minute_series", None))


def has_minute_bars(obj: Any) -> bool:
    return callable(getattr(obj, "minute_bars", None))


def empty_minute_series() -> pl.DataFrame:
    return pl.DataFrame({name: pl.Series(name, [], dtype=_MINUTE_SERIES_TYPES[name]) for name in MINUTE_SERIES_COLUMNS})


def empty_minute_bars() -> pl.DataFrame:
    return pl.DataFrame({name: pl.Series(name, [], dtype=_MINUTE_BARS_TYPES[name]) for name in MINUTE_BARS_COLUMNS})


def answers(obj: Any, method: str, market: str) -> bool:
    """这个源愿不愿意答**这个市场**的这一档；没有 `<方法>_markets` 就算愿意。

    它同时是三处的唯一判据：适配器自己的拒答、健康页那个「仅…」标注、周期条上的置灰。
    """
    wanted = getattr(obj, f"{method}_markets", None)
    return wanted is None or market in wanted
```

- [ ] 登记两条能力：`src/sams/datasource/capabilities.py`

import 区加一行（按字母序排在 `listed_date` 之前）：

```python
from sams.datasource.minute import has_minute_bars, has_minute_series
```

`CAPABILITIES` 的 `"index_daily"` 那行之后追加两行：

```python
    # 分钟粒度：都不落库（spec D163），所以这两条链没有写入方，只有读端
    "minute_series": Capability(label="分时", lacking="不提供分时", probe=has_minute_series),
    "minute_bars": Capability(label="分钟 K", lacking="不提供分钟K", probe=has_minute_bars),
```

- [ ] 把探针模块摘出适配器发现流程：`src/sams/datasource/sources.py:29-40`

`_NOT_ADAPTERS` 那个 `frozenset` 里加一项（`"capabilities"` 之后即可）：

```python
        "capabilities",
        "minute",
```

不这么做会在 `test_discovered_modules_all_register_an_implementation` 上红：`minute.py` 不注册任何实现。

- [ ] 同步那三处闭集断言

`tests/datasource/test_capabilities.py` 的 `test_the_kind_name_is_the_adapter_method_name` 集合里加两项：

```python
        "index_daily",
        "minute_series",
        "minute_bars",
    }
```

同文件 `test_the_lacking_reason_is_per_kind_and_states_the_capability`（约 :56 起）的 label 列表末尾加两项：

```python
        "分时",
        "分钟 K",
```

`tests/datasource/test_fundamentals_adapters.py:253` 那条 `[capability.label for capability in CAPABILITIES.values()] == [...]` 同样在末尾补 `"分时", "分钟 K"`。

`tests/datasource/test_sources_pluggable.py:74` 与 `tests/datasource/test_capabilities.py:62` 这两处 tencent 的元组**这一步不动**（腾讯还没加方法），Task 3 起再改。

- [ ] 跑到绿

```bash
uv run pytest tests/datasource/test_minute.py tests/datasource/test_capabilities.py tests/datasource/test_fundamentals_adapters.py tests/datasource/test_sources_pluggable.py -q
```

预期：全绿。然后

```bash
uv run ruff check --no-cache src/sams tests
```

预期：`All checks passed!`（若 `minute.py` 那两行 dict 推导超行宽，就折成多行，别加 `# noqa`）。

- [ ] 提交

```bash
git add src/sams/datasource/minute.py src/sams/datasource/capabilities.py src/sams/datasource/sources.py tests/datasource/test_minute.py tests/datasource/test_capabilities.py tests/datasource/test_fundamentals_adapters.py tests/datasource/test_sources_pluggable.py
git commit -m "feat(datasource): 登记分时与分钟K两条能力，探针与市场意愿分开问"
```

---

## Task 2：把「要哪一档、要几根」传过降级链

**Files:**
- Modify: `src/sams/datasource/registry.py:363-452`（`_walk` / `_call` / `_fetch_bars_frame`）与 `fetch_index_daily` 之后
- Test: `tests/datasource/test_registry.py`

- [ ] 写失败测试：追加到 `tests/datasource/test_registry.py`

```python
class MinuteFake(FakeAdapter):
    """分时 + 分钟 K 双能力替身：分钟 K 把收到的 `scale`/`count` 留在 `extra` 上。"""

    def __init__(self, name, markets=("A",), *outcomes):
        super().__init__(name, markets, *outcomes)
        self.extra = None

    def minute_series(self, market, code, start, end):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def minute_bars(self, market, code, start, end, *, scale, count):
        self.calls += 1
        self.extra = (scale, count)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_minute_bars_carries_the_scale_and_count_to_the_adapter():
    """`m15` 要真的变成 `scale=15`：适配器猜不到档，而猜错的档看起来完全正常。"""
    fake = MinuteFake("tencent", ("A",), frame("tencent"))
    result = registry(fake).fetch_minute_bars(
        "A", "600519", D1, D2, scale=15, count=240
    )
    assert fake.extra == (15, 240)
    assert result.source == "tencent"


def test_minute_series_asks_the_chain_for_one_day():
    """分时一次只答一天：`start == end == day`，链上不许把它当区间展开。"""
    fake = MinuteFake("tencent", ("A",), frame("tencent"))
    registry(fake).fetch_minute_series("A", "600519", D1)
    assert fake.calls == 1


def test_a_source_without_the_minute_method_is_skipped_with_the_right_words():
    """只有日线的源进 `skipped`，那句话是 `lacking`——不许静默跳过（D38）。"""
    daily = FakeAdapter("akshare", ("A",), frame("akshare"))
    minute = MinuteFake("tencent", ("A",), frame("tencent"))
    result = registry(daily, minute).fetch_minute_series("A", "600519", D1)
    assert result.source == "tencent"
    assert ("akshare", "不提供分时") in result.skipped


def test_empty_minute_result_carries_the_daily_shaped_frame():
    """每个源都答「没有」时 `frame` 是 `EMPTY_BARS`（**日线列集**）：读列名之前必须先看旗标。

    这一条是钉陷阱的：分钟服务若照日线那条路读 `frame["price"]`，撞上的不是「今天休市」
    而是 `ColumnNotFound`，而那次报错会被当成接口故障。
    """
    from sams.datasource.base import BAR_COLUMNS

    empty = MinuteFake("tencent", ("A",), EmptyResponse("tencent", "没有那一天"))
    result = registry(empty).fetch_minute_series("A", "600519", D1)
    assert result.empty_everywhere is True
    assert result.frame.columns == list(BAR_COLUMNS)
```

- [ ] 跑到红

```bash
uv run pytest tests/datasource/test_registry.py -q -k minute
```

预期：`AttributeError: 'AdapterRegistry' object has no attribute 'fetch_minute_bars'`（第一条）与同类失败。

- [ ] 实现：`_walk` / `_call` 透传 `extra`

`src/sams/datasource/registry.py:363-395` 的 `_walk` 改成（新增关键字参数，并把两条既有链的行为原样保留）：

```python
    def _walk(
        self,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str,
        extra: dict[str, object] | None = None,
    ) -> Iterator[_Outcome]:
```

docstring 之后那句调用改成：

```python
                frame = self._call(name, market, code, start, end, method=method, extra=extra)
```

`_call`（约 :600 起，签名以文件现状为准）加同名关键字参数，并把 `invoke` 闭包换成带 `extra` 的调用：

```python
    def _call(
        self,
        name: str,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str = "daily_bars",
        extra: dict[str, object] | None = None,
    ) -> pl.DataFrame:
        adapter = self.adapter(name)
        invoke_bound = getattr(adapter, method)
        if self.bucket is not None:
            self.bucket.acquire()

        calls = 0

        def invoke() -> pl.DataFrame:
            nonlocal calls
            calls += 1
            return invoke_bound(market, code, start, end, **(extra or {}))
```

其余不动——特别是**不许**在 `_call` 里加日期裁剪：`_walk` 的契约是「链只知道四件事」，裁剪属于调用方。

- [ ] 实现：`_fetch_bars_frame` 带上 `extra`，再加两条 fetch

`_fetch_bars_frame`（约 :407）签名加 `extra`，`_walk` 调用透传：

```python
    def _fetch_bars_frame(
        self,
        market: str,
        code: str,
        start: date,
        end: date,
        *,
        method: str,
        extra: dict[str, object] | None = None,
    ) -> FetchResult:
        ...
        for out in self._walk(market, code, start, end, method=method, extra=extra):
```

`fetch_index_daily` 之后插入两条链：

```python
    def fetch_minute_series(self, market: str, code: str, day: date) -> FetchResult:
        """分时链：`start == end == day`。

        复用 `FetchResult` 而不是另立 spec §5.1 写的 `MinuteRead`：`attempts` /
        `skipped` / `empty_everywhere` 这三样在分时上问的是同一组问题，再造一份
        只会让「链上谁被跳过」有两套写法。已回写为「实现时订正」。
        """
        return self._fetch_bars_frame(market, code, day, day, method="minute_series")

    def fetch_minute_bars(
        self, market: str, code: str, start: date, end: date, *, scale: int, count: int
    ) -> FetchResult:
        return self._fetch_bars_frame(
            market,
            code,
            start,
            end,
            method="minute_bars",
            extra={"scale": scale, "count": count},
        )
```

- [ ] 跑到绿

```bash
uv run pytest tests/datasource/test_registry.py tests/datasource -q
```

预期：全绿（其余链没换调用方式，`extra=None` 与今天逐字等价）。

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/datasource/registry.py tests/datasource/test_registry.py
git commit -m "feat(datasource): 降级链透传 scale/count，并挂上分时与分钟K两条链"
```

---

## Task 3：腾讯分时（`minute_series`）

**Files:**
- Modify: `src/sams/datasource/tencent.py`（常量区、`parse_tencent_minute`、`TencentAdapter.minute_series`、末尾 `source(...)` 那行）
- Modify: `tests/datasource/test_capabilities.py:62`、`tests/datasource/test_sources_pluggable.py:74`
- Test: `tests/datasource/test_tencent.py`

- [ ] 写失败测试：追加到 `tests/datasource/test_tencent.py`

现有的 `FakeGet`（约 :170）只读 `params["param"]`，分时那条传的是 `code`，会 `KeyError`。所以先加一个记录整份参数的替身（放在 `FakeGet` 旁边）：

```python
class MinuteGet:
    """分时端点的参数是 `code=`，而 `FakeGet` 只认日线那串 `param=`：另记一份。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append((url, dict(params)))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload
```

样本响应（**照着实测的形状写**，别自创字段）：

```python
def minute_payload(symbol="sh600519", day="20261028", rows=None, quote=None):
    rows = rows if rows is not None else [
        "0930 1354.98 1537 208260426.00",
        "0931 1353.00 3186 430459525.34",
        "0932 1352.10 4000 540000000.00",
    ]
    qt = {"market": "1", symbol: quote or ["1", "600519", "贵州茅台", "1332.08", "1355.90"]}
    qt[f"v_ff_{symbol}"] = ["x"]  # 实测：`qt` 里混着这个键与 `market`，按符号取才不端回整数
    return {"code": 0, "msg": "", "data": {symbol: {"data": {"date": day, "data": rows}, "qt": qt}}}
```

用例：

```python
from datetime import date

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.minute import MINUTE_SERIES_COLUMNS
from sams.datasource.tencent import TencentAdapter, parse_tencent_minute

DAY = date(2026, 10, 28)


def test_minute_series_returns_the_declared_column_set_with_cumulative_volume_in_shares():
    """A 股分时：`date` 是 `20261028` 那一格，量按 手→股 换算（与日线同一口径）。"""
    frame = parse_tencent_minute(
        minute_payload(), market="A", code="600519", symbol="sh600519"
    )
    assert frame.columns == MINUTE_SERIES_COLUMNS
    assert frame["date"].to_list() == [DAY] * 3
    assert frame["time"].to_list() == ["09:30", "09:31", "09:32"]
    assert frame["price"].to_list() == [1354.98, 1353.00, 1352.10]
    # 1537 手 → 153700 股：差两个数量级的量柱在屏幕上读起来完全正常，所以只能钉数
    assert frame["volume"].to_list() == [153700.0, 318600.0, 400000.0]
    assert frame["amount"].to_list()[0] == pytest.approx(208260426.0)
    assert frame["prev_close"].to_list() == [1355.90] * 3
    assert frame["source"].to_list() == ["tencent"] * 3


def test_minute_series_reads_hk_volume_as_shares_without_scaling():
    """港股那格单位是股：乘 100 会把腾讯一天的量放大成它自己的十倍。"""
    frame = parse_tencent_minute(
        minute_payload(
            symbol="hk00700",
            rows=["0930 384.000 510600 196105924.933", "0931 384.200 900000 340000000.000"],
            quote=["1", "00700", "腾讯控股", "384.000", "385.200"],
        ),
        market="HK",
        code="00700",
        symbol="hk00700",
    )
    assert frame["volume"].to_list() == [510600.0, 900000.0]
    assert frame["prev_close"].to_list() == [385.20, 385.20]


def test_minute_series_indexes_qt_by_symbol_not_the_first_key():
    """`qt` 里还挂着 `market` 与 `v_ff_*`：`next(iter(qt))` 会端回一个整数或列表。

    这不是假想的脏数据，是实测的形状，所以它值得一条自己的用例。
    """
    payload = minute_payload()
    payload["data"]["sh600519"]["qt"] = {"market": "1", "v_ff_sh600519": ["a"], "sh600519": ["1", "600519", "贵州茅台", "1332.08", "1355.90"]}
    frame = parse_tencent_minute(payload, market="A", code="600519", symbol="sh600519")
    assert frame["prev_close"].to_list() == [1355.90] * 3


def test_minute_series_refuses_us_before_touching_the_network():
    """美股那一格实测只给三字段、`date` 是空串：它答不了「哪一天」，就直接不答这一档。

    把「没这市场」说成一次空答复会让降级链继续问下去、屏幕显示空白；
    在这里拒掉，健康页与周期条才知道该置灰。
    """
    get = MinuteGet({})
    adapter = TencentAdapter(get=get, clock=FrozenClock(DAY))
    with pytest.raises(EmptyResponse, match="只供"):
        adapter.minute_series("US", "AAPL", DAY, DAY)
    assert get.calls == []


def test_minute_series_refuses_a_session_outside_the_requested_window():
    """接口永远给「最近一个交易日」，请求的那天对不上就是没答：不许静默交回隔壁那天。"""
    adapter = TencentAdapter(get=MinuteGet(minute_payload(day="20261027")), clock=FrozenClock(DAY))
    with pytest.raises(EmptyResponse, match="不在"):
        adapter.minute_series("A", "600519", DAY, DAY)


def test_minute_series_rejects_a_row_that_is_not_four_fields():
    """行少了字段是形态变了（`FieldDrift`），不是「今天没数据」：一次定性、立刻换源。"""
    with pytest.raises(FieldDrift, match="字段"):
        parse_tencent_minute(
            minute_payload(rows=["0930 1354.98 1537"]),
            market="A",
            code="600519",
            symbol="sh600519",
        )
```

- [ ] 跑到红

```bash
uv run pytest tests/datasource/test_tencent.py -q -k minute
```

预期：`ImportError: cannot import name 'parse_tencent_minute'`。

- [ ] 实现：`src/sams/datasource/tencent.py`

常量区（`QUOTE_URL` 附近）加：

```python
#: 分时端点：参数是 `code=`，不是日线那串 `param=`
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
#: 分时一行的四个字段：时刻、价格、**累计**量、**累计**额（腾讯给的就是累计，不是每分钟增量）
_MINUTE_FIELDS = ("time", "price", "cum_volume", "cum_amount")
```

解析函数（放在 `parse_tencent_daily` 之后）：

```python
def parse_tencent_minute(payload: object, *, market: str, code: str, symbol: str) -> pl.DataFrame:
    """纯解析：`minute/query` 的 JSON → `MINUTE_SERIES_COLUMNS`，量已折成股。

    序列在 `data.<symbol>.data.data`：那个键套了两层，照着日线 `data.<symbol>.day`
    的习惯只取一层会拿到一个 dict，然后在 `.split(" ")` 上崩——那 looks like 接口故障。
    """
    if not isinstance(payload, dict):
        raise FieldDrift("tencent", f"分时响应不是对象: {type(payload).__name__}")
    data = payload.get("data")
    body = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise FieldDrift("tencent", f"分时的不是 {symbol}，实际是 {sorted(data or {})}")
    inner = body.get("data") or {}
    raw_day = str(inner.get("date") or "")
    rows = inner.get("data") or []
    if not raw_day or not rows:
        # 美股走的就是这一条：`date` 空串、只有一行三字段——它没答「哪一天」
        raise EmptyResponse("tencent", f"{market} {symbol} 的分时没答出日期或序列")
    if len(raw_day) != 8 or not raw_day.isdigit():
        raise FieldDrift("tencent", f"分时的日期形态变了: {raw_day!r}")

    quote = (body.get("qt") or {}).get(symbol)
    if not isinstance(quote, list) or len(quote) < 5:
        raise FieldDrift("tencent", f"分时响应里没有可读的 qt[{symbol}]")

    records = []
    for row in rows:
        parts = str(row).split()
        if len(parts) != len(_MINUTE_FIELDS):
            raise FieldDrift("tencent", f"分时行字段数不对: {row!r}")
        records.append(dict(zip(_MINUTE_FIELDS, parts, strict=True)))
    frame = pl.DataFrame(records)
    return pl.DataFrame(
        {
            "market": [market] * frame.height,
            "code": [code] * frame.height,
            "date": [datetime.strptime(raw_day, "%Y%m%d").date()] * frame.height,
            # "0930" → "09:30"：屏幕上那条时间轴与缓存键都用带冒号的那一份
            "time": [f"{value[:2]}:{value[2:]}" for value in frame["time"].to_list()],
            "price": frame["price"].cast(pl.Float64).to_list(),
            # 与日线同一口径：A 股的手折成股，港股腾讯本来就给股（实测 510600 对 09:30 一分钟）
            "volume": frame["cum_volume"].cast(pl.Float64)
            * (LOTS_TO_SHARES if market == "A" else 1.0),
            "amount": frame["cum_amount"].cast(pl.Float64),
            "prev_close": [float(quote[4])] * frame.height,
            "source": ["tencent"] * frame.height,
        }
    )
```

`TencentAdapter` 类体里（`markets` 那行之后加声明，`quotes` 之前加方法）：

```python
    #: 分时答哪些市场：美股那一格实测给不出日期（见 `minute_series`）
    minute_series_markets = frozenset({"A", "HK"})
```

```python
    def minute_series(self, market: str, code: str, start: date, end: date) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if market not in self.minute_series_markets:
            raise EmptyResponse(
                "tencent",
                f"腾讯分时只供 {sorted(self.minute_series_markets)}，收到 {market}",
            )
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        # 分时端点的符号与报价同形（美股不带交易所后缀），不走 `tencent_symbol`
        symbol = tencent_quote_symbol(market, code)
        try:
            payload = self._get(MINUTE_URL, {"code": symbol}, timeout=self._timeout)
            frame = parse_tencent_minute(payload, market=market, code=code, symbol=symbol)
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        session = frame["date"].to_list()[0]
        if not start <= session <= end:
            # 接口永远答最近一个交易日：请求的那天对不上就是没答，
            # 静默交回隔壁那一天等于把「7 月 8 日的分时」画成今天的样子
            raise EmptyResponse(
                "tencent", f"{symbol} 的分时给的是 {session}，不在请求的 {start}~{end} 里"
            )
        return frame
```

末尾注册那行跟着改（健康页念的就是这句）：

```python
source(
    "tencent",
    options=("timeout",),
    note="腾讯 gtimg 日线 + A/HK 指数日线 + A/HK 分时 + A 股分钟K，三市场，不复权",
)(TencentAdapter)
```

- [ ] 同步两处闭集断言

`tests/datasource/test_capabilities.py:62` 与 `tests/datasource/test_sources_pluggable.py:74`：

```python
    assert entries["tencent"].capabilities == (
        "daily_bars",
        "quote",
        "index_daily",
        "minute_series",
    )
```

（顺序 = `CAPABILITIES` 的登记顺序；Task 4 会再加一项 `minute_bars`。）

- [ ] 跑到绿

```bash
uv run pytest tests/datasource/test_tencent.py tests/datasource/test_capabilities.py tests/datasource/test_sources_pluggable.py -q
```

- [ ] 提交

```bash
git add src/sams/datasource/tencent.py tests/datasource/test_tencent.py tests/datasource/test_capabilities.py tests/datasource/test_sources_pluggable.py
git commit -m "feat(datasource): 腾讯分时——累计量折成股、qt 按符号取、美股在联网前就拒"
```

---

## Task 4：腾讯分钟 K（`minute_bars`）

**Files:**
- Modify: `src/sams/datasource/tencent.py`（`MINUTE_KLINE_URL`、`minute_kline_param`、`parse_tencent_minute_kline`、`TencentAdapter.minute_bars`）
- Modify: `tests/datasource/test_capabilities.py:62`、`tests/datasource/test_sources_pluggable.py:74`
- Test: `tests/datasource/test_tencent.py`

- [ ] 写失败测试：追加到 `tests/datasource/test_tencent.py`

```python
def minute_k_payload(scale="m5", rows=None, symbol="sh600519"):
    rows = rows if rows is not None else [
        ["202610281055", "1331.35", "1335.25", "1335.83", "1331.02", "669.00", {}, "0.8924"],
        ["202610281100", "1333.10", "1331.41", "1333.10", "1331.00", "902.00", {}, "1.2014"],
    ]
    return {
        "code": 0,
        "msg": "",
        "data": {symbol: {scale: rows, "prec": ["1355.90"], "qt": {symbol: ["1"]}}},
    }


def test_minute_kline_param_keeps_the_empty_adjustment_slot():
    """`sh600519,m5,,320`：那个空复权位少了，接口答 `param error`——那是参数不对，不是没数据。"""
    assert minute_kline_param("sh600519", 5, 320) == "sh600519,m5,,320"


def test_minute_bars_maps_the_row_order_and_trims_to_the_window():
    """行序是 时刻/开/**收**/高/低/量：与日线那行「收在第三位」不是同一套位置。"""
    frame = parse_tencent_minute_kline(
        minute_k_payload(), market="A", code="600519", symbol="sh600519", scale=5
    )
    assert frame.columns == MINUTE_BARS_COLUMNS
    assert frame["clock"].to_list() == ["10:55", "11:00"]
    assert frame["open"].to_list() == [1331.35, 1333.10]
    assert frame["close"].to_list() == [1335.25, 1331.41]
    assert frame["high"].to_list() == [1335.83, 1333.10]
    assert frame["low"].to_list() == [1331.02, 1331.00]
    assert frame["volume"].to_list() == [66900.0, 90200.0]


def test_minute_bars_leaves_amount_null_because_the_unit_is_unreconciled():
    """腾讯最后一格那个 `0.8924` 单位没核准（新浪同规模给的是 `14644871.9688` 元）。

    拿一个单位没核清的字段算出来的东西看起来都正常，所以置 null 并钉住它。
    """
    frame = parse_tencent_minute_kline(
        minute_k_payload(), market="A", code="600519", symbol="sh600519", scale=5
    )
    assert frame["amount"].null_count() == frame.height


def test_minute_bars_refuses_hk_and_us_before_touching_the_network():
    """实测那两边永远 `{"code":-1,"msg":"param error"}`：那是接口不答这一档，不是这只票没数据。"""
    get = MinuteGet({})
    adapter = TencentAdapter(get=get, clock=FrozenClock(DAY))
    for market in ("HK", "US"):
        with pytest.raises(EmptyResponse, match="只供"):
            adapter.minute_bars(market, "00700", DAY, DAY, scale=5, count=240)
    assert get.calls == []


def test_minute_bars_reads_the_node_for_the_requested_scale():
    """节点键就是档位（实测 `["m5","prec","qt"]`）：档位与节点不一致要出声。"""
    with pytest.raises(FieldDrift, match="节点"):
        parse_tencent_minute_kline(
            minute_k_payload(scale="m15"), market="A", code="600519", symbol="sh600519", scale=5
        )
```

- [ ] 跑到红

```bash
uv run pytest tests/datasource/test_tencent.py -q -k "minute_bars or minute_k"
```

预期：`ImportError: cannot import name 'minute_kline_param'`。

- [ ] 实现：`src/sams/datasource/tencent.py`

```python
#: 分钟 K 只有这一个端点：`/mkline/get` 与 `web.` 那个 host 实测都拿不到数据
MINUTE_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
#: 分钟 K 一行八格：第 7 格是腾讯留的空对象，第 8 格是成交额但单位对不上（见 `parse_tencent_minute_kline`）
_MINUTE_K_ORDER = ("moment", "open", "close", "high", "low", "volume", "_extra", "turnover")
```

```python
def minute_kline_param(symbol: str, scale: int, count: int) -> str:
    """`sh600519,m5,,320`：第三格是复权位，**留空但必须留着那个逗号**。"""
    return f"{symbol},m{scale},,{count}"


def parse_tencent_minute_kline(
    payload: object, *, market: str, code: str, symbol: str, scale: int
) -> pl.DataFrame:
    """纯解析：`mkline` 的 JSON → `MINUTE_BARS_COLUMNS`，量已折成股，`amount` 为 null。"""
    if not isinstance(payload, dict):
        raise FieldDrift("tencent", f"分钟K响应不是对象: {type(payload).__name__}")
    if payload.get("code") not in (0, None):
        # A 股那边不会走到这里（适配器先按 `minute_bars_markets` 拒了）：
        # 走到这儿说明符号或参数错了，那句话要说「参数」而不是「这段没数据」
        raise EmptyResponse("tencent", f"腾讯分钟K拒绝参数: {payload.get('msg') or payload}")
    data = payload.get("data")
    body = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise FieldDrift("tencent", f"分钟K的不是 {symbol}，实际是 {sorted(data or {})}")
    node = f"m{scale}"
    if node not in body:
        raise FieldDrift("tencent", f"分钟K响应没有 {node} 节点，实际键 {sorted(body)}")
    rows = body[node]
    if not isinstance(rows, list) or not rows:
        raise EmptyResponse("tencent", f"{market} {code} 的 {node} 是空的")

    records = []
    dates = []
    clocks = []
    for row in rows:
        if not isinstance(row, list) or len(row) < len(_MINUTE_K_ORDER):
            raise FieldDrift("tencent", f"分钟K行字段不足: {row!r}")
        moment = str(row[0])
        if len(moment) != 12 or not moment.isdigit():
            raise FieldDrift("tencent", f"分钟K的时刻形态变了: {moment!r}")
        stamps = datetime.strptime(moment, "%Y%m%d%H%M")
        dates.append(stamps.date())
        clocks.append(stamps.strftime("%H:%M"))
        records.append(dict(zip(_MINUTE_K_ORDER, row[: len(_MINUTE_K_ORDER)], strict=True)))

    frame = pl.DataFrame(records)
    return pl.DataFrame(
        {
            "market": [market] * frame.height,
            "code": [code] * frame.height,
            "date": dates,
            "clock": clocks,
            "open": frame["open"].cast(pl.Float64),
            # 不复权：分钟档不落库，也就没有跨除权可比的问题（D164 的前提在这条链上不成立）
            "high": frame["high"].cast(pl.Float64),
            "low": frame["low"].cast(pl.Float64),
            "close": frame["close"].cast(pl.Float64),
            "volume": frame["volume"].cast(pl.Float64)
            * (LOTS_TO_SHARES if market == "A" else 1.0),
            "amount": pl.Series("amount", [None] * frame.height, dtype=pl.Float64),
            "source": ["tencent"] * frame.height,
        }
    )
```

`TencentAdapter` 里：

```python
    #: 分钟 K 只答 A 股：港美股那边实测永远 `param error`（D165 置灰就念这一份）
    minute_bars_markets = frozenset({"A"})
```

```python
    def minute_bars(
        self, market: str, code: str, start: date, end: date, *, scale: int, count: int
    ) -> pl.DataFrame:
        if market not in self.markets:
            raise ValueError(f"{self.name} 不支持市场 {market}")
        if market not in self.minute_bars_markets:
            raise EmptyResponse(
                "tencent",
                f"腾讯分钟K只供 {sorted(self.minute_bars_markets)}，收到 {market}",
            )
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        symbol = tencent_quote_symbol(market, code)
        params = {"param": minute_kline_param(symbol, scale, count)}
        try:
            payload = self._get(MINUTE_KLINE_URL, params, timeout=self._timeout)
            frame = parse_tencent_minute_kline(
                payload, market=market, code=code, symbol=symbol, scale=scale
            )
        except DatasourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_error(self.name, exc) from exc
        # 请求的是「最近 N 根」，右端永远是最新一根：按窗口裁掉区间外那些
        return frame.filter(pl.col("date").is_between(start, end))
```

- [ ] 两处闭集断言补 `minute_bars`

`tests/datasource/test_capabilities.py:62` 与 `tests/datasource/test_sources_pluggable.py:74` 的 tencent 元组末尾加 `"minute_bars"`。

- [ ] 跑到绿

```bash
uv run pytest tests/datasource/test_tencent.py tests/datasource/test_capabilities.py tests/datasource/test_sources_pluggable.py -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/datasource/tencent.py tests/datasource/test_tencent.py tests/datasource/test_capabilities.py tests/datasource/test_sources_pluggable.py
git commit -m "feat(datasource): 腾讯分钟K——行序按实测的收在第二格，成交额单位没核准就置空"
```

---

## Task 5：新浪分钟 K（第二条来源）

新浪的价值在于它是**另一套基础设施**（与腾讯互不相干的机房与限流策略）：腾讯被限流时，分钟那一档不至于整块瞎掉。它没有分时端点，所以这一轮只加 `minute_bars`。

**Files:**
- Modify: `src/sams/datasource/sina.py`（`parse_sina_minute`、`SinaAdapter.minute_bars`、`minute_bars_markets`、末尾 `source(...)` 的 note）
- Test: `tests/datasource/test_sina.py`

- [ ] 写失败测试：追加到 `tests/datasource/test_sina.py`

本文件的取数替身若只认日线那串参数，就在它旁边加一个记录整份 `params` 的替身（分时要看 `scale` 与 `datalen` 是否被 honor）：

```python
class RecordingGet:
    """记下整份 params：`scale=15` 有没有真的传下去，只有从参数上能证。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, params, *, timeout=15):
        self.calls.append((url, dict(params)))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload
```

样本与用例（**`day` 带秒**是实测形态，与日线的 `YYYY-MM-DD` 不同）：

```python
from datetime import date

import pytest

from sams.core.errors import EmptyResponse, FieldDrift
from sams.datasource.minute import MINUTE_BARS_COLUMNS
from sams.datasource.sina import parse_sina_minute

D28 = date(2026, 10, 28)
D27 = date(2026, 10, 27)


def minute_rows():
    return [
        {
            "day": "2026-10-27 15:00:00",
            "open": "1356.000",
            "high": "1357.000",
            "low": "1352.720",
            "close": "1355.900",
            "volume": "1194508",
            "amount": "1619962803.8680",
        },
        {
            "day": "2026-10-28 10:30:00",
            "open": "1354.980",
            "high": "1354.980",
            "low": "1333.000",
            "close": "1340.950",
            "volume": "1440368",
            "amount": "1936196723.9883",
        },
        {
            "day": "2026-10-28 11:05:00",
            "open": "1331.350",
            "high": "1331.430",
            "low": "1331.280",
            "close": "1331.410",
            "volume": "11000",
            "amount": "14644871.9688",
        },
    ]


def test_parse_sina_minute_splits_day_into_date_and_clock_and_trims_the_window():
    """`day` 带秒：拆成 `date` + `clock`，并按请求区间裁掉区间外那根。"""
    frame = parse_sina_minute(minute_rows(), code="600519", start=D28, end=D28)
    assert frame.columns == MINUTE_BARS_COLUMNS
    assert frame["date"].to_list() == [D28, D28]
    assert frame["clock"].to_list() == ["10:30", "11:05"]
    # 新浪的量本来就是股、额是元：与日线同一口径，不做换算
    assert frame["volume"].to_list() == [1440368.0, 11000.0]
    assert frame["amount"].to_list()[1] == pytest.approx(14644871.9688)


def test_minute_bars_asks_for_the_requested_scale_and_count():
    get = RecordingGet(minute_rows())
    SinaAdapter(get=get, clock=FrozenClock(D28)).minute_bars(
        "A", "600519", D28, D28, scale=15, count=48
    )
    _url, params = get.calls[0]
    assert params["scale"] == "15"
    assert params["datalen"] == "48"
    assert params["symbol"] == "sh600519"


def test_minute_bars_refuses_other_markets_without_a_network_call():
    get = RecordingGet(minute_rows())
    with pytest.raises(EmptyResponse, match="只供"):
        SinaAdapter(get=get, clock=FrozenClock(D28)).minute_bars(
            "HK", "00700", D28, D28, scale=5, count=240
        )
    assert get.calls == []


def test_minute_bars_answers_no_data_when_the_window_is_older_than_the_rows():
    """请求够了、区间内一根没有：那是「这段没数据」，交 `EmptyResponse` 让链问下一个源。"""
    get = RecordingGet(minute_rows()[:1])
    with pytest.raises(EmptyResponse, match="0 根"):
        SinaAdapter(get=get, clock=FrozenClock(D28)).minute_bars(
            "A", "600519", D28, D28, scale=5, count=240
        )


def test_parse_sina_minute_rejects_a_row_without_a_time_part():
    with pytest.raises((FieldDrift, EmptyResponse)):
        parse_sina_minute([{"day": "2026-10-28", "close": "1.0"}], code="600519", start=D28, end=D28)
```

- [ ] 跑到红

```bash
uv run pytest tests/datasource/test_sina.py -q -k minute
```

预期：`ImportError: cannot import name 'parse_sina_minute'`。

- [ ] 实现：`src/sams/datasource/sina.py`

在 `parse_sina_daily` 之后加：

```python
def parse_sina_minute(payload: object, *, code: str, start: date, end: date) -> pl.DataFrame:
    """纯解析：新浪分钟 JSON → `MINUTE_BARS_COLUMNS`，按 `[start, end]` 裁剪。

    `day` 在这一档是 `"2026-10-28 11:05:00"`（带秒），比日线多一段时间；拆成
    `date` + `clock` 是因为 `normalize_bars` 会按 `date` 去重——一分钟一根，
    整场交易会被它压成一根 K，而那看起来像「这个源只给了一天」。
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise DatasourceError(
            "sina", f"分钟响应不是数组: {type(payload).__name__}；原文 {str(payload)[:120]}"
        )
    if not payload:
        raise EmptyResponse("sina", f"{code} 分钟区间无数据")
    rows = [item for item in payload if isinstance(item, dict)]
    if len(rows) != len(payload):
        raise DatasourceError("sina", "分钟响应里混入非对象元素，字段形态已变")

    moments = [str(item.get("day") or "") for item in rows]
    bad = [value for value in moments if len(value) < 16]
    if bad:
        raise DatasourceError("sina", f"分钟行的 `day` 形态不对: {bad[0]!r}")

    frame = pl.DataFrame(
        {
            "market": ["A"] * len(rows),
            "code": [code] * len(rows),
            "date": [value[:10] for value in moments],
            "clock": [value[11:16] for value in moments],
            "open": [item.get("open") for item in rows],
            "high": [item.get("high") for item in rows],
            "low": [item.get("low") for item in rows],
            "close": [item.get("close") for item in rows],
            "volume": [item.get("volume") for item in rows],
            "amount": [item.get("amount") for item in rows],
            "source": ["sina"] * len(rows),
        }
    ).with_columns(
        pl.col("date").str.to_date("%Y-%m-%d"),
        pl.col(name).cast(pl.Float64)
        for name in ("open", "high", "low", "close", "volume", "amount")
    )
    return frame.filter(pl.col("date").is_between(start, end)).sort(["date", "clock"])
```

> 上面那段 `with_columns` 的生成器写法不合法（`with_columns` 要吃序列，不是推导式的裸生成器）。落地时写成 `frame.with_columns(pl.col("date").str.to_date("%Y-%m-%d"), *[pl.col(name).cast(pl.Float64) for name in ("open", "high", "low", "close", "volume", "amount")])`——这里留原样只为说明该转哪些列。

`SinaAdapter` 里加声明与方法：

```python
    #: 分钟 K 只有 A 股：新浪这一路本来就没有港美股
    minute_bars_markets = frozenset({"A"})
```

```python
    def minute_bars(
        self, market: str, code: str, start: date, end: date, *, scale: int, count: int
    ) -> pl.DataFrame:
        if market not in self.minute_bars_markets:
            raise EmptyResponse(
                "sina", f"新浪分钟K只供 {sorted(self.minute_bars_markets)}，收到 {market}"
            )
        if end < start:
            raise ValueError(f"区间反向: {start} > {end}")
        params = {
            "symbol": a_share_symbol(code),
            # `scale` 就是分钟数；`datalen` 真被 honor，所以这里不需要任何「每档上限」表
            "scale": str(scale),
            "ma": "no",
            "datalen": str(count),
        }
        try:
            payload = self._get(KLINE_URL, params, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — 只包网络这一跳
            raise classify_error(self.name, exc) from exc
        frame = parse_sina_minute(payload, code=code, start=start, end=end)
        if frame.is_empty():
            # 参数里 `scale` 填错时接口只回 `[]`：那是「这个源没答」，不是「这只票没数据」
            raise EmptyResponse(
                "sina", f"{code} 的 {scale} 分钟K 在 {start}~{end} 内 0 根（已请求 {count} 根）"
            )
        return frame
```

末尾注册那行的 note 跟上：

```python
source(
    "sina",
    options=("timeout",),
    note="新浪 A 股日线 + 指数日线 + 分钟K，日线只覆盖最近约 1023 根",
)(SinaAdapter)
```

- [ ] 跑到绿

```bash
uv run pytest tests/datasource/test_sina.py tests/datasource -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/datasource/sina.py tests/datasource/test_sina.py
git commit -m "feat(datasource): 新浪分钟K第二来源——day 带秒拆成 date+clock，scale 填错答无数据"
```

---

## Task 6：分钟服务的可用性那一半（D165 话术的唯一出处）

新建 `src/sams/services/minute.py`，这一轮只落「谁能答」：两档各答各的，一句话既给周期条置灰用，也给接口的 `note` 用。屏幕与 JSON 从这里出，就不会各说一套。

**Files:**
- Create: `src/sams/services/minute.py`
- Modify: `tests/support.py`（三个新件）
- Test: `tests/services/test_minute.py`（新建）

- [ ] 写测试替身与样本帧：追加到 `tests/support.py`

```python
def sample_minute_series(
    market: str = "A",
    code: str = "600519",
    day: date = date(2026, 10, 28),
    source: str = "tencent",
) -> pl.DataFrame:
    """三条分时：量与额都是**累计**（腾讯给的就是累计，服务层负责差分成每分钟）。"""
    return pl.DataFrame(
        {
            "market": [market] * 3,
            "code": [code] * 3,
            "date": [day] * 3,
            "time": ["09:30", "09:31", "09:32"],
            "price": [1354.98, 1353.00, 1352.10],
            "volume": [153700.0, 318600.0, 400000.0],
            "amount": [208260426.0, 430459525.34, 540000000.0],
            "prev_close": [1355.90] * 3,
            "source": [source] * 3,
        }
    )


def sample_minute_bars(
    market: str = "A",
    code: str = "600519",
    day: date = date(2026, 10, 28),
    source: str = "tencent",
) -> pl.DataFrame:
    """三根分钟 K：`clock` 是这一根的结束时刻，`amount` 留空（单位没核准）。"""
    return pl.DataFrame(
        {
            "market": [market] * 3,
            "code": [code] * 3,
            "date": [day] * 3,
            "clock": ["10:55", "11:00", "11:05"],
            "open": [1331.35, 1335.25, 1333.10],
            "high": [1335.83, 1335.83, 1333.10],
            "low": [1331.02, 1333.02, 1331.00],
            "close": [1335.25, 1333.09, 1331.41],
            "volume": [66900.0, 66900.0, 11000.0],
            "amount": [None, None, None],
            "source": [source] * 3,
        }
    )


class StubMinuteAdapter(StubAdapter):
    """分时 + 分钟 K 双能力替身：默认照腾讯的意愿表关档（分时答 A/HK、分钟 K 只答 A）。"""

    minute_series_markets = frozenset({"A", "HK"})
    minute_bars_markets = frozenset({"A"})

    def __init__(
        self,
        name: str,
        markets=("A",),
        outcome=None,
        *,
        series=None,
        bars=None,
        series_markets=None,
        bars_markets=None,
    ) -> None:
        super().__init__(name, markets, outcome)
        self.series_frame = series if series is not None else sample_minute_series()
        self.bars_frame = bars if bars is not None else sample_minute_bars()
        if series_markets is not None:
            self.minute_series_markets = frozenset(series_markets)
        if bars_markets is not None:
            self.minute_bars_markets = frozenset(bars_markets)
        self.extra: tuple[int, int] | None = None

    def minute_series(self, market, code, start, end):
        self.calls += 1
        if market not in self.minute_series_markets:
            raise EmptyResponse(self.name, f"分时不答 {market}")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.series_frame

    def minute_bars(self, market, code, start, end, *, scale, count):
        self.calls += 1
        self.extra = (scale, count)
        if market not in self.minute_bars_markets:
            raise EmptyResponse(self.name, f"分钟K不答 {market}")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.bars_frame
```

（`tests/support.py` 顶部若还没 import `EmptyResponse`，补上 `from sams.core.errors import EmptyResponse`。）

- [ ] 写失败测试：新建 `tests/services/test_minute.py`

```python
"""分钟服务的三条职责各钉一枚：谁能答（D165）、累计差分、`adj_factor=1` 组装（D164）。"""

from __future__ import annotations

from datetime import date

import pytest
from support import (
    StubAdapter,
    StubMinuteAdapter,
    sample_bars,
    sample_minute_bars,
    sample_minute_series,
)

from sams.core.errors import EmptyResponse
from sams.datasource.registry import AdapterRegistry
from sams.services.minute import MINUTE_SCALES, MinuteService

DAY = date(2026, 10, 28)


def registry(*adapters, markets=("A",)) -> AdapterRegistry:
    by_name = {a.name: a for a in adapters}
    return AdapterRegistry(
        adapters=by_name,
        market_sources={m: tuple(by_name) for m in markets},
        sleep=lambda seconds: None,
    )


def service(*adapters, markets=("A",)) -> MinuteService:
    return MinuteService(registry(*adapters, markets=markets), calendar=None)


def test_the_six_scales_are_the_only_minute_periods_the_screen_offers():
    assert MINUTE_SCALES == {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60, "m120": 120}


def test_availability_answers_per_kind_and_per_market():
    """两档各答各的：HK 答得了分时、答不了分钟 K，一条链不是一个 is/否。"""
    hk_and_a = StubMinuteAdapter("tencent", ["A", "HK"])
    got_a = service(hk_and_a, markets=("A", "HK"))
    assert (got_a.availability("A").series.ok, got_a.availability("A").bars.ok) == (True, True)
    hk = got_a.availability("HK")
    assert (hk.series.ok, hk.bars.ok) == (True, False)
    assert "分钟" in hk.bars.why_not


def test_availability_says_the_chain_has_no_source_at_all():
    """链上只有日线的源：那句话要说「没有能提供分钟K的源」，不是「这只票没数据」。"""
    answer = service(StubAdapter("akshare", ["A"], sample_bars())).availability("A").bars
    assert not answer.ok
    assert "没有能提供" in answer.why_not


def test_a_source_that_refuses_this_market_does_not_make_the_kind_answerable():
    """意愿表关掉这一档 → 不可答：置灰与接口 `note` 都从这一句出，不许两套说法。"""
    closed = StubMinuteAdapter("sina", ["A", "HK"], bars_markets=frozenset())
    assert not service(closed, markets=("A",)).availability("A").bars.ok
```

- [ ] 跑到红

```bash
uv run pytest tests/services/test_minute.py -q
```

预期：`ModuleNotFoundError: No module named 'sams.services.minute'`。

- [ ] 实现：新建 `src/sams/services/minute.py`

```python
"""分钟粒度：分时与分钟 K 的读取面（D163/D164/D165）。

三条职责，都只读、都不落库：

  1. `availability`——这一市场的两档谁能答、不能答时怎么说。周期条上的置灰与
     接口返回的 `note` 共用这一份，屏上和 JSON 里不会各说一套（D165）。
  2. `timeshare`——把腾讯给的**累计**量差分回每分钟，并算出均价线。
  3. `bars`——把分钟 K 组装成 `AdjustedSeries` 交给 `analysis.metrics`（D164）。

缓存的是**取回来的帧**而不是算好的表：换一组指标勾选不该再打一次外网，
而把 `metrics` 的结果缓存下来会让「同一份数两种指标」在缓存里并存。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.datasource.capabilities import capability
from sams.datasource.minute import answers

#: 屏幕上那六档分钟 K：键 = URL 里那个 `period`，值 = 适配器要的 `scale`（分钟数）
MINUTE_SCALES: dict[str, int] = {
    "m1": 1,
    "m5": 5,
    "m15": 15,
    "m30": 30,
    "m60": 60,
    "m120": 120,
}


@dataclass(frozen=True)
class KindAnswer:
    """一档（分时 / 分钟 K）在某个市场的可用性。"""

    label: str
    ok: bool
    #: 链上有这个方法但关掉了这一市场的源：`(源名, 原因)`
    refused: tuple[tuple[str, str], ...] = ()

    @property
    def why_not(self) -> str:
        """不能答时的那句话。

        两种「不能」必须分开：链上根本没有这个方法的源（配置里就没接），
        与有方法但这个市场不答（接了，腾讯不吃美股的分时）——后者要念出源名，
        否则读的人以为是自己没配。
        """
        if self.refused:
            names = "、".join(name for name, _reason in self.refused)
            return f"{names} 不供这一市场的{self.label}"
        return f"这条链上没有能提供{self.label}的源"


@dataclass(frozen=True)
class MinuteAvailability:
    market: str
    series: KindAnswer
    bars: KindAnswer


class MinuteService:
    def __init__(
        self,
        registry,
        calendar,
        *,
        clock: Clock = SYSTEM_CLOCK,
        ttl_s: float = 60.0,
        now=time.monotonic,
    ) -> None:
        self._registry = registry
        self._calendar = calendar
        self._clock = clock
        self._ttl = ttl_s
        self._now = now
        #: `(market, code, period)` → `(装填时刻, 帧)`：见模块 docstring
        self._cache: dict[tuple[str, str, str], tuple[float, object]] = {}

    def availability(self, market: str) -> MinuteAvailability:
        return MinuteAvailability(
            market,
            self._answer(market, "minute_series"),
            self._answer(market, "minute_bars"),
        )

    def _answer(self, market: str, method: str) -> KindAnswer:
        """`chain_for`（有没有这个方法）× `answers`（答不答这个市场）——两问都得问。

        只查前者会把「腾讯有 minute_bars 但美股不答」算成可用：置灰失效，
        点下去收到的是一句接口错误而不是「这一档不供美股」。
        """
        cap = capability(method)
        chain = self._registry.chain_for(market, method)
        willing: list[str] = []
        refused: list[tuple[str, str]] = []
        for name in chain:
            adapter = self._registry.adapter(name)
            if answers(adapter, method, market):
                willing.append(name)
            else:
                refused.append((name, f"{cap.label}不答 {market}"))
        return KindAnswer(cap.label, bool(willing), tuple(refused))
```

`calendar` 这一轮还没用上（Task 7 的尾巴裁剪才读它），但构造参数一次定下来，避免后面改签名把调用点改两遍。

- [ ] 跑到绿

```bash
uv run pytest tests/services/test_minute.py -q
```

预期：4 条全绿。

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/services/minute.py tests/support.py tests/services/test_minute.py
git commit -m "feat(services): 分钟服务的可用性——有没有这个方法与答不答这个市场是两问"
```

---

## Task 7：分钟 K 读取与 D164 的 `adj_factor=1` 组装

D164 的原话不是「绕过闸」，而是「那条闸的前提在这一档根本不成立」：`analysis.adjust.adjusted_series` 要的是**日线**的除权记录与覆盖确认，分钟帧既没有除权日可跳、也没有覆盖标记可查。硬塞进去只会让 `hfq_frame` 把每根的因子乘一遍 1.0，顺便把「这段历史被谁确认过」这句不成立的话写进结果里。

**Files:**
- Modify: `src/sams/services/minute.py`
- Test: `tests/services/test_minute.py`

- [ ] 写失败测试：追加到 `tests/services/test_minute.py`

```python
def test_minute_bars_are_assembled_with_factor_one_and_hfq_columns():
    """D164：分钟帧自己按 `adj_factor=1` 组装，不借 `adjusted_series`——它要的是日线除权。"""
    read = service(StubMinuteAdapter("tencent", ["A"]), markets=("A",)).bars(
        "A", "600519", period="m5", start=DAY, end=DAY, indicators=("ma",)
    )
    assert read.note is None
    frame = read.series.frame
    assert frame["adj_factor"].to_list() == [1.0] * 3
    assert {"open_hfq", "high_hfq", "low_hfq", "close_hfq"} <= set(frame.columns)
    # 因子是 1：`*_hfq` 与裸价逐字相等，这一条同时钉住「没有偷偷乘过别的」
    assert frame["close_hfq"].to_list() == frame["close"].to_list()
    assert frame["date"].to_list()[-1].strftime("%H:%M") == "11:05"


def test_minute_bars_carry_the_scale_to_the_chain():
    fake = StubMinuteAdapter("tencent", ["A"])
    service(fake, markets=("A",)).bars("A", "600519", period="m15", start=DAY, end=DAY)
    assert fake.extra == (15, 240)


def test_an_unknown_period_is_refused_as_a_bad_request():
    from sams.core.errors import ValidationError

    with pytest.raises(ValidationError, match="不是分钟档"):
        service(StubMinuteAdapter("tencent", ["A"])).bars(
            "A", "600519", period="m7", start=DAY, end=DAY
        )


def test_minute_bars_refusal_is_a_note_not_an_exception():
    """HK 的分钟 K 两个源都不答：接口要 200 + note（§6 钉 8），这里先把那句准备好。"""
    read = service(StubMinuteAdapter("tencent", ["A", "HK"], bars_markets=frozenset())).bars(
        "A", "600519", period="m5", start=DAY, end=DAY
    )
    assert read.series is None
    assert "分钟" in read.note


def test_a_dead_chain_gives_a_note_naming_the_source_that_failed():
    dead = StubMinuteAdapter("tencent", ["A"], outcome=EmptyResponse("tencent", "没有这一档"))
    read = service(dead, markets=("A",)).bars("A", "600519", period="m5", start=DAY, end=DAY)
    assert read.series is None
    assert "每个源都答了" in read.note


def test_the_frame_is_cached_so_a_second_indicator_pick_does_not_refetch():
    """缓存的键不含指标：换勾选读的是同一份数，而再打一次外网不是「换勾选」的语义。"""
    fake = StubMinuteAdapter("tencent", ["A"])
    reader = service(fake, markets=("A",))
    first = reader.bars("A", "600519", period="m5", start=DAY, end=DAY, indicators=("ma",))
    second = reader.bars("A", "600519", period="m5", start=DAY, end=DAY, indicators=("macd", "ma"))
    assert fake.calls == 1
    assert "macd_26" in second.table.columns
    assert "macd_26" not in first.table.columns
```

- [ ] 跑到红

```bash
uv run pytest tests/services/test_minute.py -q -k "minute_bars or cached or unknown_period"
```

预期：`AttributeError: 'MinuteService' object has no attribute 'bars'`。

- [ ] 实现：`src/sams/services/minute.py` 补 `bars`

常量区补齐（放在 `MINUTE_SCALES` 之后）：

```python
#: 一次取多少根：一个交易日 240 分钟，`m1` 刚好一整天，`m120` 是两天
MINUTE_BARS = 240
#: 分钟数据只覆盖最近约一年（腾讯实测 `202610191405` 起）：窗口给到 365 天就是给到底
MINUTE_LOOKBACK_DAYS = 365
#: 一次读数到下一次打外网之间的间隔：分时是「现在」的数，缓存久了就不是了
DEFAULT_TTL_S = 60.0
#: 缓存的条数上限：一屏一张图，400 张够一个交易日里来回点很多次
_CACHE_CAP = 400
```

import 区加：

```python
import polars as pl
from collections.abc import Mapping, Sequence
from datetime import date
from sams.analysis.adjust import PRICE_COLUMNS, AdjustedSeries
from sams.analysis.indicators import metrics
from sams.core.errors import DatasourceError, ValidationError
```

数据类（放在 `MinuteAvailability` 之后）：

```python
@dataclass(frozen=True)
class MinuteBarsRead:
    """分钟 K 的一次读取：`series` 交给指标，`table` 是算完指标的那张宽表。

    拒答时 `series`/`table` 都是 `None` 而 `note` 有那句话——接口层只认 `note`，
    不许把 `None` 当「这段没数据」画成空图。
    """

    period: str
    series: AdjustedSeries | None
    table: pl.DataFrame | None
    source: str
    note: str | None = None

    @classmethod
    def refusal(cls, period: str, note: str) -> "MinuteBarsRead":
        return cls(period=period, series=None, table=None, source="", note=note)
```

`MinuteService` 里的方法：

```python
    def bars(
        self,
        market: str,
        code: str,
        *,
        period: str,
        start: date,
        end: date,
        indicators: Sequence[str] = (),
        windows: Mapping[str, Sequence[int]] | None = None,
    ) -> MinuteBarsRead:
        scale = MINUTE_SCALES.get(period)
        if scale is None:
            raise ValidationError(
                f"{period} 不是分钟档：可选的是 {', '.join(MINUTE_SCALES)}"
            )
        answer = self._answer(market, "minute_bars")
        if not answer.ok:
            return MinuteBarsRead.refusal(period, answer.why_not)
        frame = self._frame(market, code, period, scale, start, end)
        if isinstance(frame, str):
            return MinuteBarsRead.refusal(period, frame)
        series = _assemble(frame, str(frame["source"][0]))
        table = metrics(series, tuple(indicators) or ("ma",), windows=windows)
        # `vol` 那一格画的是裸量柱（`INDICATORS` 里没有 `vol`），指标表得自己带上这一列
        return MinuteBarsRead(period, series, table.with_columns(frame["volume"]), series.covered_by)

    def _frame(self, market, code, period, scale, start, end):
        """取这一档最近 `MINUTE_BARS` 根，并按 `[start, end]` 裁；失败与「都不答」返回一句 note。"""
        key = (market, code, period)
        hit = self._cache.get(key)
        now = self._now()
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1].filter(pl.col("date").is_between(start, end))
        try:
            result = self._registry.fetch_minute_bars(
                market, code, start, end, scale=scale, count=MINUTE_BARS
            )
        except DatasourceError as exc:
            return f"{period} 的分钟K取不出来：{exc}"
        if result.empty_everywhere:
            # 读列名之前必须先看这个旗标：那时 `frame` 装的是**日线**列集的 `EMPTY_BARS`
            return f"{period} 的分钟K这一段没有数：每个源都答了「没有」"
        self._remember(key, (now, result.frame))
        return result.frame.filter(pl.col("date").is_between(start, end))

    def _remember(self, key, entry) -> None:
        """超上限就按插入顺序丢最旧的：分钟数据只活一天，精确的 LRU 不值得那圈记账。"""
        if len(self._cache) >= _CACHE_CAP and key not in self._cache:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = entry
```

模块级函数（D164 的那次组装，注释要把它为什么不是绕过闸说清）：

```python
def _assemble(frame: pl.DataFrame, source: str) -> AdjustedSeries:
    """把分钟帧组装成 `AdjustedSeries`：`adj_factor` 恒为 1（D164）。

    不借 `analysis.adjust.hfq_frame`：那条路要 `day_factors`，而分钟档既没有除权日可跳、
    也没有日线覆盖确认可查——闸的前提在这一档不成立，不是有人把闸绕开了。
    逐档价格都要出 `_hfq` 列：只给 `close_hfq` 等于逼每个指标自己再乘一遍，
    最容易的错法是把裸 high/low 直接喂进 ATR、BOLL、KDJ。
    """
    ordered = frame.sort(["date", "clock"])
    moments = pl.concat_str(
        [
            pl.col("date").cast(pl.Utf8),
            pl.lit(" "),
            # "11:05" → "11:05:00"：`%H:%M` 在 DuckDB/polars 那侧的解析比带秒脆
            pl.col("clock") + pl.lit(":00"),
        ]
    ).str.strptime(pl.Datetime, "%Y-%m-%d %H:%M:%S")
    priced = ordered.with_columns(moments.alias("date")).drop("clock")
    adj = pl.Series("adj_factor", [1.0] * priced.height)
    columns = [name for name in PRICE_COLUMNS if name in priced.columns]
    hfq = [(pl.col(name) * adj).alias(f"{name}_hfq") for name in columns]
    out = priced.with_columns(adj, *hfq)
    days = out["date"].to_list()
    return AdjustedSeries(
        frame=out,
        covered_by=source,
        covered_through=days[-1].date(),
        factors=(),
    )
```

`PRICE_COLUMNS` 若没从 `sams.analysis.adjust` 导出，就在该模块的 `__all__`/定义处确认名字（`indicators.py` 里 `hfq_frame` 的默认参数就是它），照实 import；**不要**为了少一个 import 去手写 `("open","high","low","close")` 第二份。

- [ ] 跑到绿

```bash
uv run pytest tests/services/test_minute.py tests/datasource -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/services/minute.py tests/services/test_minute.py
git commit -m "feat(services): 分钟K读取——按 D164 的 adj_factor=1 组装，缓存取回的帧"
```

---

## Task 8：分时——累计差分成每分钟、均价线、盘后尾巴

腾讯那四个字段里第三个是**累计**量（实测 `0930` 那行 `1537` 手、`0931` 那行 `3186` 手），屏幕上画的是每分钟量柱，所以要差分。均价线用「累计额 ÷ 累计股数」——两个都是累计值，直接相除即可，不需要再累加一次。

尾巴必须裁：港股实测会报到 `16:08`（日历的收市是 16:00），A 股全天 267 行里有 `15:00` 之后那一段。裁的依据是日历给的当日**最晚收市时刻**，日历答不上来时**不裁**——把数据悄悄删掉比留着更坏。

**Files:**
- Modify: `src/sams/services/minute.py`
- Test: `tests/services/test_minute.py`

- [ ] 写失败测试：追加到 `tests/services/test_minute.py`

需要一个最小的日历替身（只答 `sessions`），放在本文件顶部：

```python
class StubCalendar:
    """只答「这一天几点收市」：尾巴裁剪的唯一外部依赖。"""

    def __init__(self, close_hm: str = "15:00", *, raise_missing: bool = False) -> None:
        self.close_hm = close_hm
        self.raise_missing = raise_missing

    def sessions(self, market, day):
        from sams.core.errors import CalendarSourceError

        if self.raise_missing:
            raise CalendarSourceError("日历没配")
        return [SimpleNamespace(name="午后", open=None, close=self.close_hm)]
```

用例：

```python
def test_timeshare_diffs_the_cumulative_volume_into_per_minute():
    """第三条 400000 股是累计：每分钟量是 153700 / 164900 / 81400。"""
    share = MinuteService(
        registry(StubMinuteAdapter("tencent", ["A"])), StubCalendar()
    ).timeshare("A", "600519")
    assert share.times == ("09:30", "09:31", "09:32")
    assert share.volume == (153700.0, 164900.0, 81400.0)
    assert share.price == (1354.98, 1353.00, 1352.10)
    assert share.prev_close == pytest.approx(1355.9)
    assert share.day == DAY


def test_timeshare_avg_is_cumulative_amount_over_cumulative_shares():
    """spec §6 钉 7：均价 = 累计额 ÷（累计量×100）。那 ×100 已在适配器做过，这里就是 ÷ 累计股数。"""
    share = MinuteService(
        registry(StubMinuteAdapter("tencent", ["A"])), StubCalendar()
    ).timeshare("A", "600519")
    assert share.avg[0] == pytest.approx(208260426.0 / 153700.0)
    assert share.avg[2] == pytest.approx(540000000.0 / 400000.0)


def test_timeshare_avg_is_null_when_no_volume_has_traded_yet():
    """第一行累计量为 0（集合竞价还没成交）：除不出数就交 null，而不是一个 0.00 的价。"""
    frame = pl.DataFrame(
        {
            "market": ["A", "A"],
            "code": ["600519", "600519"],
            "date": [DAY, DAY],
            "time": ["09:30", "09:31"],
            "price": [1354.98, 1353.00],
            "volume": [0.0, 100.0],
            "amount": [0.0, 135300.0],
            "prev_close": [1355.90, 1355.90],
            "source": ["tencent", "tencent"],
        }
    )
    reader = MinuteService(
        registry(StubMinuteAdapter("tencent", ["A"], series=frame)), StubCalendar()
    )
    assert reader.timeshare("A", "600519").avg == (None, 1353.0)


def test_timeshare_trims_rows_after_the_calendar_close():
    """港股尾巴实测报到 16:08：按日历给的 16:00 收市裁掉，否则时间轴多出 8 根假分钟。"""
    late = pl.DataFrame(
        {
            "market": ["HK"] * 3,
            "code": ["00700"] * 3,
            "date": [DAY] * 3,
            "time": ["15:59", "16:00", "16:08"],
            "price": [384.0, 384.2, 384.2],
            "volume": [900000.0, 950000.0, 960000.0],
            "amount": [3.4e8, 3.6e8, 3.65e8],
            "prev_close": [385.2] * 3,
            "source": ["tencent"] * 3,
        }
    )
    reader = MinuteService(
        registry(StubMinuteAdapter("tencent", ["A", "HK"], series=late)),
        StubCalendar("16:00"),
    )
    assert reader.timeshare("HK", "00700").times == ("15:59", "16:00")


def test_timeshare_keeps_everything_when_the_calendar_cannot_answer():
    """日历答不上来就不裁：悄悄删数据比留几根晚到的分钟坏得多。"""
    reader = MinuteService(
        registry(StubMinuteAdapter("tencent", ["A"])), StubCalendar(raise_missing=True)
    )
    assert reader.timeshare("A", "600519").times == ("09:30", "09:31", "09:32")


def test_timeshare_refuses_a_market_the_chain_does_not_answer():
    """美股：链上没人答分时——note 说清是谁不答，而不是画一张空图（D165）。"""
    reader = MinuteService(
        registry(StubMinuteAdapter("tencent", ["A", "HK", "US"], series_markets=frozenset({"A", "HK"})), markets=("A", "US")),
        StubCalendar(),
    )
    share = reader.timeshare("US", "AAPL")
    assert share.note and "分时" in share.note
    assert share.times == ()
```

（本文件顶部补 `from types import SimpleNamespace`、`import polars as pl`，并把 `service(...)` 那个 helper 换成显式带 `StubCalendar()` 的写法——上面几条用例里 `MinuteService(registry(...), StubCalendar())` 就是最终姿势。）

- [ ] 跑到红

```bash
uv run pytest tests/services/test_minute.py -q -k timeshare
```

预期：`AttributeError: 'MinuteService' object has no attribute 'timeshare'`。

- [ ] 实现：`src/sams/services/minute.py` 补 `timeshare`

import 区加 `from sams.core.errors import CalendarSourceError`。数据类：

```python
@dataclass(frozen=True)
class Timeshare:
    """一天分时：`times` 与三条序列同长同序，`volume` 已经是每分钟的量。"""

    day: date | None
    times: tuple[str, ...]
    price: tuple[float, ...]
    avg: tuple[float | None, ...]
    volume: tuple[float, ...]
    prev_close: float | None
    source: str
    note: str | None = None

    @classmethod
    def refusal(cls, note: str) -> "Timeshare":
        return cls(
            day=None, times=(), price=(), avg=(), volume=(), prev_close=None, source="", note=note
        )
```

方法：

```python
    def timeshare(self, market: str, code: str) -> Timeshare:
        """一次只答一天：接口本身也只有「最近一个交易日」那一条路（Task 3 的区间校验）。"""
        answer = self._answer(market, "minute_series")
        if not answer.ok:
            return Timeshare.refusal(answer.why_not)
        today = self._clock.today()
        try:
            result = self._registry.fetch_minute_series(market, code, today)
        except DatasourceError as exc:
            return Timeshare.refusal(f"{today} 的分时取不出来：{exc}")
        if result.empty_everywhere:
            return Timeshare.refusal(f"{today} 没有分时：这条链上每个源都答了「没有那一天」")
        frame = self._trim_to_session(result.frame, market, today)
        times = list(frame["time"].to_list())
        if not times:
            return Timeshare.refusal(f"{today} 的分式一行有效数据都没有（可能还没开盘）")
        cum_volume = frame["volume"].to_list()
        cum_amount = frame["amount"].to_list()
        # 每分钟量 = 累计的一阶差分，第一项没有前态可减，就是它自己
        volume = [cum_volume[0]] + [
            later - earlier for earlier, later in zip(cum_volume, cum_volume[1:], strict=True)
        ]
        # 均价：分子分母都是累计值，直接相除；累计量为 0 的那一分钟除不出数，交 null
        avg = [
            (amount / cumulative) if cumulative else None
            for amount, cumulative in zip(cum_amount, cum_volume, strict=True)
        ]
        return Timeshare(
            day=frame["date"].to_list()[0],
            times=tuple(times),
            price=tuple(frame["price"].to_list()),
            avg=tuple(avg),
            volume=tuple(volume),
            prev_close=frame["prev_close"].to_list()[0],
            source=str(frame["source"][0]),
        )

    def _trim_to_session(self, frame, market, day):
        """裁掉收市之后的行：港股实测会报到 16:08，A 股全天 267 行里含 `15:00` 之后那一段。

        裁的依据是日历给的当日**最晚**收市时刻（午休与市场自己的收盘不是一个东西），
        而日历答不上来时原样交回——悄悄删数据比留着几根晚到的分钟坏得多。
        """
        if self._calendar is None:
            return frame
        try:
            sessions = self._calendar.sessions(market, day)
        except CalendarSourceError:
            return frame
        closes = [session.close for session in sessions if session.close]
        if not closes:
            return frame
        cutoff = max(closes)
        # `times` 是 "HH:MM" 的零填充串，与收市时刻格式化后比字符串就是比时刻
        cut = cutoff.strftime("%H:%M") if hasattr(cutoff, "strftime") else str(cutoff)
        trimmed = frame.filter(pl.col("time") <= cut)
        return trimmed if not trimmed.is_empty() else frame
```

`hasattr(cutoff, "strftime")` 那一支是为替身写的（`SimpleNamespace(close="16:00")` 给的是字符串）。落地时把 `StubCalendar` 的 `close` 换成真的 `datetime`（`datetime.combine(day, time(16, 0), tzinfo=ZoneInfo("Asia/Hong_Kong"))`），服务里那一支字符串兜底随之删掉——**测试不许逼生产代码带兼容分支**。

- [ ] 跑到绿

```bash
uv run pytest tests/services/test_minute.py -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/services/minute.py tests/services/test_minute.py
git commit -m "feat(services): 分时把累计量差分成每分钟，按日历收市裁尾巴、日历不答就不裁"
```

---

## Task 9：健康页的市场维 + 把分钟服务装进 `ServiceContext`

两件事一起：`capability_labels` 那句「仅…」的标注得跟着市场问（否则健康页会说腾讯供分时，而它供的是 A/HK 的分时、美股一分都不供）；分钟服务得有个进程级归属。

**Files:**
- Modify: `src/sams/services/sources.py:41-51`（`capability_labels`）
- Modify: `src/sams/bootstrap.py`（`ServiceContext` 字段、构造、传参）
- Test: `tests/services/test_sources.py`、`tests/test_bootstrap.py`

- [ ] 写失败测试：追加到 `tests/services/test_sources.py`

```python
def test_capability_labels_respect_the_per_market_willingness_table(registry_with_minute):
    """US 那一行不许写着「分时、分钟 K」：腾讯有这两个方法，但美股一个都不答。

    探针只答「有没有这个方法」，市场维在 `answers` 那张表里——健康页此前只问了前者。
    """
    labels = capability_labels(registry_with_minute, "US", "tencent")
    assert "分时" not in labels and "分钟 K" not in labels
    assert "分钟 K" in capability_labels(registry_with_minute, "A", "tencent")
    assert "分时" in capability_labels(registry_with_minute, "HK", "tencent")
```

`registry_with_minute` 这个 fixture 照本文件既有的 registry 构造写法加一个（A/HK/US 三条链都指向同一个 `support.StubMinuteAdapter("tencent", ["A","HK","US"])`，链上再放一个只有日线的 `support.StubAdapter("akshare", ["A"], support.sample_bars())`；若本文件已经有可复用的 registry 工厂就直接用它，别新造第二套）。

- [ ] 跑到红

```bash
uv run pytest tests/services/test_sources.py -q -k willingness
```

预期：`assert "分时" not in labels` 失败——现在它只按 `chain_for` 判，美股那行确实带着「分时」。

- [ ] 实现：`src/sams/services/sources.py`

import 区加 `from sams.datasource.minute import answers`，函数体改成：

```python
    return tuple(
        cap.label
        for key, cap in CAPABILITIES.items()
        if key != "daily_bars"
        and name in registry.chain_for(market, key)
        # 有这个方法 ≠ 答这个市场：腾讯的分钟两档都是按市场关着的，健康页要跟着关
        and answers(registry.adapter(name), key, market)
    )
```

（`chain_for` 只接受它自己那份判据，市场维只能在这里补——这也是 `answers` 为什么要公开在 `datasource.minute` 里而不是塞成服务层的私有函数。）

- [ ] 写失败测试：追加到 `tests/test_bootstrap.py`

```python
def test_the_container_carries_a_minute_reader(container):
    """分钟服务住 `ServiceContext`：一条线程一份，与 `analysis` 同一侧（D163）。"""
    assert container.minute.availability("A").bars.label == "分钟 K"
    assert container.minute.availability("A").series.label == "分时"
```

- [ ] 跑到红

```bash
uv run pytest tests/test_bootstrap.py -q -k minute_reader
```

预期：`AttributeError: 'ServiceContext' object has no attribute 'minute'`。

- [ ] 实现：`src/sams/bootstrap.py`

import 区加：

```python
from sams.services.minute import MinuteService
```

`ServiceContext` 的 `analysis: AnalysisService`（约 :218）之后加字段：

```python
    #: 分钟粒度（计划 B）：只读、不落库，所以没有 sync 那条写侧
    minute: MinuteService
```

`_context_on` 里 `analysis = AnalysisService(...)`（约 :470）之后构造：

```python
    minute = MinuteService(registry, calendar, clock=clock)
```

`return ServiceContext(...)`（约 :683）的 `analysis=analysis,` 之后加 `minute=minute,`。

- [ ] 跑到绿

```bash
uv run pytest tests/test_bootstrap.py tests/services/test_sources.py -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/services/sources.py src/sams/bootstrap.py tests/services/test_sources.py tests/test_bootstrap.py
git commit -m "feat(bootstrap): 分钟服务进 ServiceContext，健康页的能力标注按市场问意愿表"
```

---

## Task 10：`/api/symbol/{market}/{code}/minute`

spec §5.2 那张表里这一行参数是「无」——接口本身也只有「最近一个交易日」那一条路（Task 3 的区间校验钉着它），给一个 `day=` 参数等于承诺一件做不到的事。

**Files:**
- Modify: `src/sams/web/pages/symbol.py`
- Modify: `tests/web/conftest.py`
- Test: `tests/web/test_symbol_minute.py`（新建）

- [ ] 先给测试装一台会答分钟的 app：`tests/web/conftest.py`

现在 `make_web_app` 的工厂体是内联的。把它抽成模块级 `_build_app_factory(migrated, sources)`（返回同样的 `make_app` 生成器语义，teardown 一起搬进去），然后 `make_web_app` 只是 `_build_app_factory(migrated, stub_sources)` 的薄壳——这一步不许复制工厂体两遍。再加：

```python
@pytest.fixture
def minute_sources(stub_sources):
    """把分钟两档挂到 A 股链上那个名字（测试配置的 A 链是 akshare → eastmoney）。

    换个新名字（`tencent`）不会进链：`AdapterRegistry` 只按配置的链路由，
    多一个没人引用的适配器是合法的，但它一辈子不被调用。
    """
    made = dict(stub_sources)
    made["akshare"] = support.StubMinuteAdapter(
        "akshare", ["A"], support.sample_bars(source="akshare")
    )
    return made


@pytest.fixture
def make_minute_app(migrated, minute_sources):
    return _build_app_factory(migrated, minute_sources)()


@pytest.fixture
def minute_app(make_minute_app):
    """与 `web_app` 同一台 app，只是 A 股链上那个源会答分时/分钟 K（HK、US 的不会）。"""
    web = make_minute_app()
    context = web.contexts.current()
    context.users.create("alice", "correct-horse-battery", is_admin=True)
    with TestClient(web.app) as client:
        yield SimpleNamespace(
            client=client,
            app=web.app,
            core=web.core,
            contexts=web.contexts,
            context=context,
            runtime=web.app.state.web.runtime,
        )


@pytest.fixture
def logged_minute(minute_app):
    """已登录的 alice：给分时与分钟 K 那几条用例用。"""
    response = minute_app.client.post(
        "/login",
        data={"username": "alice", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    minute_app.csrf = minute_app.client.cookies.get("sams_csrf").rpartition(".")[0]
    return minute_app
```

（`web_app` 与 `logged` 是同一形状的既有夹具；上面两个是它们的分钟版，别去改既有那两个的行为。）

- [ ] 写失败测试：新建 `tests/web/test_symbol_minute.py`

```python
"""分时接口：spec §5.2 那七个键，与 §6 钉 7/8 的姿势（拒绝也答 200 + note）。"""

from __future__ import annotations

import polars as pl

_NAME = "贵州茅台"


def _seed(logged, market="A", code="600519"):
    logged.context.symbols.upsert(
        pl.DataFrame({"market": [market], "code": [code], "name": [_NAME], "currency": ["CNY"]})
    )


def test_the_minute_endpoint_answers_exactly_the_seven_spec_keys(logged_minute):
    _seed(logged_minute)
    payload = logged_minute.client.get("/api/symbol/A/600519/minute").json()
    assert set(payload) == {"date", "times", "price", "avg", "volume", "prev_close", "note"}
    assert payload["times"] == ["09:30", "09:31", "09:32"]
    # 每分钟量是差分出来的：第三条累计 400000 股 → 这一分钟 81400
    assert payload["volume"] == [153700.0, 164900.0, 81400.0]
    assert payload["prev_close"] == 1355.9
    assert payload["note"] is None


def test_minute_refusal_is_200_with_a_note_and_empty_arrays(logged_minute):
    """§6 钉 8：HK 的分钟 K 两个源都不答 → 200 + note，不许画成「这只票没数据」。"""
    _seed(logged_minute, market="HK", code="00700")
    response = logged_minute.client.get("/api/symbol/HK/00700/minute")
    assert response.status_code == 200
    payload = response.json()
    assert payload["times"] == [] and payload["price"] == []
    assert "分时" in payload["note"]


def test_minute_requires_a_session(logged_minute):
    response = logged_minute.client.get(
        "/api/symbol/A/600519/minute", cookies={"sams_session": "", "sams_csrf": ""}
    )
    assert response.status_code in (303, 401)
```

- [ ] 跑到红

```bash
uv run pytest tests/web/test_symbol_minute.py -q
```

预期：前两条 404（路由不存在）→ `assert` 失败；第三条可能已经绿（既有的鉴权兜底），那一条留着当回归钉。

- [ ] 实现：`src/sams/web/pages/symbol.py`

在 `bars_api` 之后加：

```python
@router.get("/api/symbol/{market}/{code}/minute")
def minute_api(request: Request, market: str, code: str):
    """分时：一次只答一天，参数一个都不接（spec §5.2）。

    拒绝也答 200 + `note`：那一格画不出来时屏幕上要说「这条链不供美股的分时」，
    而不是「这只票没数据」——后者会让人去重跑一条本来好好的采集（§6 钉 8）。
    这与 `bars` 那条 `NotAdjustable` 是同一个姿势。
    """
    user = require_user(request)
    web = request.app.state.web
    ctx, market, code, _symbol = _require_symbol(request, user, market, code)
    share = ctx.minute.timeshare(market, code)
    return JSONResponse(
        {
            "date": str(share.day) if share.day else None,
            "times": list(share.times),
            "price": list(share.price),
            "avg": list(share.avg),
            "volume": list(share.volume),
            "prev_close": share.prev_close,
            "note": share.note,
        }
    )
```

`ctx` 的类型是 `ServiceContext`（Task 9 已加 `minute` 字段），所以这里不需要新 import。

- [ ] 跑到绿

```bash
uv run pytest tests/web/test_symbol_minute.py tests/web -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/web/pages/symbol.py tests/web/conftest.py tests/web/test_symbol_minute.py
git commit -m "feat(web): 分时接口——七个键照 spec，链不供时也答 200 加一句原因"
```

---

## Task 11：`bars` 的分钟分支与那份共用的空态

分钟 K 复用 `bars` 那个出口（蜡烛、指标、副图全都同一套键），只有三处不同：`dates` 是「月-日 时:分」、`bars`/`pending`/`anchored_on`/`warmup_floor` 这四键在这一档没有意义、口径是**不复权**。空态那 16 个键此前内联在 `NotAdjustable` 那一条 `except` 里，现在有两个出口要举同一份，抽成函数。

**Files:**
- Modify: `src/sams/web/pages/symbol.py`（`bars_api`、`_empty_payload`、`_minute_bars_payload`、`CHART_PERIODS`、`DEFAULT_WINDOW`）
- Test: `tests/web/test_symbol_minute.py`

- [ ] 写失败测试：追加到 `tests/web/test_symbol_minute.py`

```python
def test_bars_answers_a_minute_period_with_the_daily_key_set(logged_minute):
    """分钟档走出 `bars` 必须是**同一套键**：前端在画图之前就按键取数，缺键先崩。"""
    _seed(logged_minute)
    payload = logged_minute.client.get(
        "/api/symbol/A/600519/bars?period=m5&main=ma&sub=vol"
    ).json()
    assert payload["period"] == "m5"
    assert payload["period_label"] == "5分"
    # 分钟档的标签要带上时刻：同一天的三根只写「10-28」在屏幕上分不出谁是谁
    assert payload["dates"][0].endswith("10:55")
    assert payload["close"] == payload["close_hfq"]
    assert set(payload["main"]["ma"]) and set(payload["sub"]["vol"]) == {"volume"}


def test_minute_bars_reuse_the_empty_shape_when_the_chain_refuses(logged_minute):
    """HK 的分钟 K 不答：200 + 与日线空态同形的 16 个键 + 那句 note。"""
    _seed(logged_minute, market="HK", code="00700")
    payload = logged_minute.client.get("/api/symbol/HK/00700/bars?period=m5").json()
    assert payload["dates"] == [] and payload["main"] == {} and payload["sub"] == {}
    assert payload["note"] and "分钟" in payload["note"]
    assert "warmup_floor" in payload and "anchored_on" in payload


def test_the_minute_periods_are_offered_on_the_chart_bar(logged_minute):
    """周期条上那六档必须存在：置灰的判据是「档在但点不动」，不是「档没了」。"""
    _seed(logged_minute)
    html = logged_minute.client.get("/api/partials/chart?market=A&code=600519").text
    for label in ("1分", "5分", "15分", "30分", "60分", "120分"):
        assert label in html


def test_an_unsupported_minute_period_still_400s(logged_minute):
    """`m7` 不在六档里：与拼错指标名同一姿势，400 并说清可选的是哪些。"""
    _seed(logged_minute)
    response = logged_minute.client.get("/api/symbol/A/600519/bars?period=m7")
    assert response.status_code == 400
```

- [ ] 跑到红

```bash
uv run pytest tests/web/test_symbol_minute.py -q -k "bars or minute_periods or unsupported"
```

预期：第一条 `payload["period"] == "day"`（`parse_period` 不认 `m5`，实际会先 400），即失败原因应为「m5 不是认识的周期」。

- [ ] 实现：`src/sams/web/pages/symbol.py`

`CHART_PERIODS` 那 tuple 的**最前面**插六档（分时那一格是视图切换，不在这张表里，见 Task 12）：

```python
CHART_PERIODS: tuple[tuple[str, str], ...] = (
    ("m1", "1分"), ("m5", "5分"), ("m15", "15分"),
    ("m30", "30分"), ("m60", "60分"), ("m120", "120分"),
    ("day", "日"), ("week", "周"), ("month", "月"), ("quarter", "季"),
    ("year", "年"), ("5d", "5日"), ("10d", "10日"),
)
```

`DEFAULT_WINDOW` 不加分钟档：分钟档的窗口由服务侧的 `MINUTE_LOOKBACK_DAYS` 决定，这里再写一份就是两套数。

空态那份抽出来（放在 `_column` 之前）：

```python
def _empty_payload(period: str, note: str | None) -> dict:
    """空态与真数据**同形**的 16 个键。

    前端在画图之前就读键，缺键会在 `fetch` 之后崩成一片白；这一份现在有两个出口
    （`NotAdjustable` 与分钟档的拒答），所以它只能有一处写法。
    """
    return {
        "dates": [],
        "period": period,
        "period_label": _period_label(period),
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "close_hfq": [],
        "volume": [],
        "main": {},
        "sub": {},
        "bars": [],
        "pending": [],
        "anchored_on": "",
        "warmup_floor": None,
        "note": note,
    }
```

（`_period_label` / `_PERIOD_LABEL` 定义在文件靠后处——把它们连同样那两行注释一起移到 `bars_api` 之前，`_empty_payload` 才不会引用一个还没定义的名字。移动不改行为，测试应保持全绿。）

`bars_api` 里，三道白名单检查之后、窗口数学之前插入分支：

```python
    if period in MINUTE_SCALES:
        # 分钟档不进 `parse_period`：它认的是日历周期，`m5` 到那儿只会得到 400
        return _minute_bars_payload(ctx, market, code, period, end, mains, subs, ma_windows)
```

并把 `except NotAdjustable` 那 20 行内联 dict 换成：

```python
        return JSONResponse(_empty_payload(period, str(exc)))
```

新函数（放在 `bars_api` 之后）：

```python
def _minute_bars_payload(ctx, market, code, period, end, mains, subs, ma_windows):
    """分钟 K 那一档的 `bars` 出参：与日线那份同一套键，只有口径与日期标签不同。"""
    asked = tuple(name for name in (*mains, *(name for name in subs if name != "vol")))
    read = ctx.minute.bars(
        market,
        code,
        period=period,
        start=end - timedelta(days=MINUTE_LOOKBACK_DAYS),
        end=end,
        indicators=asked or ("ma",),
        windows={"ma": ma_windows} if "ma" in mains and ma_windows else None,
    )
    if read.note or read.series is None:
        return JSONResponse(_empty_payload(period, read.note))
    table = read.table
    frame = read.series.frame
    return JSONResponse(
        {
            # 分钟那一档的横轴要带时刻：同一天的三根只写「10-28」在屏幕上分不出谁是谁
            "dates": [value.strftime("%m-%d %H:%M") for value in table["date"].to_list()],
            "period": read.period,
            "period_label": _period_label(read.period),
            # 因子恒为 1（D164），所以 `*_hfq` 与裸价逐字相等：键名照旧给，
            # 前端不必知道这一档没有复权这回事，图注那句才是给人看的口径
            "open": _column(frame, "open_hfq"),
            "high": _column(frame, "high_hfq"),
            "low": _column(frame, "low_hfq"),
            "close": _column(frame, "close_hfq"),
            "close_hfq": _column(frame, "close_hfq"),
            "volume": _column(frame, _VOLUME_COLUMN),
            "main": {name: _group(table, name) for name in mains},
            "sub": {name: _group(table, name) for name in subs},
            # 这四键在这一档没有意义：分钟不重采样、不锚日线、预热按分钟根数自己走
            "bars": [],
            "pending": [],
            "anchored_on": "",
            "warmup_floor": None,
            "note": None,
        }
    )
```

`MINUTE_SCALES` / `MINUTE_LOOKBACK_DAYS` 从 `sams.services.minute` import（Web 允许 import services）：

```python
from sams.services.minute import MINUTE_LOOKBACK_DAYS, MINUTE_SCALES
```

- [ ] 跑到绿

```bash
uv run pytest tests/web -q
```

> `tests/web/test_symbol.py::test_chart_partial_marks_the_current_period` 这一条会红：`CHART_PERIODS` 现在 13 档，而那条用例数的是「一屏只有一个 `aria-current`」。Task 12 会把它按组改写；**这一步不许先改测试凑绿**，把它红着记进 Task 12。

先确认其余的都绿：

```bash
uv run pytest tests/web/test_symbol_minute.py tests/web/test_symbol.py -q -k "not marks_the_current_period"
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/web/pages/symbol.py tests/web/test_symbol_minute.py
git commit -m "feat(web): bars 接口认分钟档——16 键的空态抽成一份，日期标签带上时刻"
```

---

## Task 12：`分时 | K线` 那条视图切换与 D165 的置灰

D158 的那一块图有两种画法，切换走 htmx 拉同一个局部（`/api/partials/chart`）加一个 `view` 参数；
D165 的那句「谁能答、谁不能」在这里落地成周期条上的置灰：档**留在原位**、点了仍回一句 note，
而不是从屏上消失。置灰的判据来自 `MinuteService.availability`（Task 6），前端不自己记市场×档位表。

顺带修一处 Task 11 留下的洞：`chart_partial` 里那句为止声而调的 `parse_period(period)`
不认 `m5`，周期条上刚加上去的六档在**局部渲染**这一路是 400（`bars` 那一路已经认了）。
两条腿必须同一口径，否则「点得动的档」与「画得出的档」不是一张表。

**Files:**
- Modify: `src/sams/web/pages/symbol.py`（`CHART_VIEWS`、`_chart_caption`、`chart_partial`）
- Modify: `src/sams/web/templates/partials/chart.html`（整块改写）
- Test: `tests/web/test_symbol_minute.py`、`tests/web/test_symbol.py`（改写一条既有用例）

- [ ] 写失败测试：追加到 `tests/web/test_symbol_minute.py`

```python
def _group(html: str, name: str) -> str:
    """某一组里的那几个字。

    `aria-current` 的判据是「**一组**只许一个」，整段计数会跟着视图组一起变：
    加一条视图切换就把那条老用例变成恒假，而它想钉的东西一条都没变。
    """
    start = html.index(f'aria-label="{name}"')
    return html[start : html.index("</span>", start)]


def test_the_chart_partial_offers_a_view_switch_with_kline_current(logged_minute):
    """默认看 K 线：视图那一组存在、`K线` 带 `aria-current`，周期条照旧带自己那一个。"""
    _seed(logged_minute)
    html = logged_minute.client.get("/api/partials/chart?market=A&code=600519").text
    assert 'aria-label="视图"' in html
    assert _group(html, "视图").count('aria-current="true"') == 1
    assert "K线" in _group(html, "视图")
    assert _group(html, "周期").count('aria-current="true"') == 1


def test_the_time_view_points_the_chart_at_the_minute_endpoint(logged_minute):
    """切到分时：div 上的 kind 与 url 一起换，图注换成没有复权那回事的那一句（D164/D158）。

    钉 `data-chart-kind` 是因为 `app.js` 就靠它分两条画法（§5.3）——
    这个属性写了却没人读，等于画分时的那条路根本没接上。
    """
    _seed(logged_minute)
    html = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=time"
    ).text
    assert 'data-chart-kind="time"' in html
    assert 'data-chart-url="/api/symbol/A/600519/minute"' in html
    assert "当日分时" in html
    # 分时那一屏没有可切的指标：留着开关会让人以为点下去会换东西
    assert "data-indicator" not in html


def test_an_unknown_view_is_a_400_not_a_silent_kline(logged_minute):
    """与拼错指标名同一姿势：写错的视图名当场说清可选的是哪些，不安静按 K 线画。"""
    _seed(logged_minute)
    response = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=ren"
    )
    assert response.status_code == 400


def test_a_minute_period_opens_the_partial_without_400(logged_minute):
    """周期条上写着 `5分`，那一档的局部就必须点得开：`bars` 认了而局部 400 是最坏的一种不一致。"""
    _seed(logged_minute)
    response = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&period=m5"
    )
    assert response.status_code == 200
    assert 'data-chart-url="/api/symbol/A/600519/bars?period=m5' in response.text


def test_the_time_view_falls_back_to_kline_when_a_period_is_clicked(logged_minute):
    """从分时点日线 = 回到 K 线：周期那一组的链接自己带 `view=kline`。

    否则「在分时视图里点周期」会得到一块照旧画分时的图，选中的档说了假话。
    """
    _seed(logged_minute)
    html = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=time"
    ).text
    assert "view=kline&period=day" in _group(html, "周期")


def test_the_unanswerable_periods_are_greyed_and_still_clickable(logged_minute):
    """D165：US 的分钟 K 链上没有源 → 那一档置灰、说出为什么，但**还在**、还点得动。

    灰的是 `class="dim"` 而不是 `aria-disabled="true"`：后者在那儿说的是「点不动」
    （副图满了那一格才是那个意思），而这一档点下去会回一句 note——
    读屏软件念成「不可用」就等于把那句为什么藏起来了。
    """
    _seed(logged_minute, market="US", code="AAPL")
    html = logged_minute.client.get("/api/partials/chart?market=US&code=AAPL").text
    periods = _group(html, "周期")
    assert "5分" in periods  # 不是消失
    assert 'class="dim"' in periods
    assert "分钟 K" in periods  # title 里那句为什么
    assert "hx-get" in periods


def test_the_time_tab_is_greyed_when_no_source_answers_series(logged_minute):
    """分时那一档同理：腾讯答 A/HK、不答 US，US 这一屏上它是灰的但仍指得到接口。"""
    _seed(logged_minute, market="US", code="AAPL")
    html = logged_minute.client.get("/api/partials/chart?market=US&code=AAPL").text
    views = _group(html, "视图")
    assert 'class="dim"' in views
    assert "分时" in views
```

- [ ] 改写既有用例：`tests/web/test_symbol.py:478-491`

整段换成按组计数（docstring 讲清为什么判据从「整段一个」变成「一组一个」）：

```python
def test_chart_partial_marks_the_current_period(logged):
    """当前档必须看得出来（D158）：换档之后旧档不再带 `aria-current`。

    判据按**组**数而不是整段数：计划 B 加了「视图」那一组（分时 | K线），
    一块屏上从此有两个「当前」——一个说看的是哪种画法，一个说看的是哪档周期。
    整段计数为 1 的那条写法会把这两件事混成一件，而读屏软件要的仍然是「一组里只有一个」。
    指标开关照旧不占这个数：它们带 `aria-pressed`，一排 `aria-current` 会被念成
    「这里有七个『当前』，请选一个」，而它其实是七个复选框。
    """
    _seed(logged)
    week = logged.client.get("/api/partials/chart?market=A&code=600519&period=week")
    assert week.status_code == 200
    assert 'aria-current="true"' in week.text
    assert 'data-chart-url="/api/symbol/A/600519/bars?period=week' in week.text
    day = logged.client.get("/api/partials/chart?market=A&code=600519&period=day")
    for name in ("周期", "视图"):
        start = day.text.index(f'aria-label="{name}"')
        group = day.text[start : day.text.index("</span>", start)]
        assert group.count('aria-current="true"') == 1
    assert day.text.count('aria-pressed="true"') == 2  # 默认那组：主图均线 + 副图成交量
```

- [ ] 跑到红

```bash
uv run pytest tests/web/test_symbol_minute.py tests/web/test_symbol.py::test_chart_partial_marks_the_current_period -q
```

预期：新写的七条全部失败（没有 `aria-label="视图"` 那一组、`?view=time` 与 `?period=m5` 都是 400 或旧文案），
改写的那条在找不到 `aria-label="视图"` 处 `ValueError`。

- [ ] 实现：`src/sams/web/pages/symbol.py`

`CHART_PERIODS` 之后加：

```python
#: 两种画法（D158）。`time` 那一档没有周期可言——它只答最近一个交易日（Task 3 的区间校验钉着）
CHART_VIEWS: tuple[tuple[str, str], ...] = (("kline", "K线"), ("time", "分时"))
```

`_period_label` 之后加：

```python
def _chart_caption(view: str, period: str) -> str:
    """图注那一句跟着视图换：分钟档压根没乘过因子，还写「后复权」就是假话（D164）。"""
    if view == "time":
        return "当日分时 · 昨收为基准线，量是每分钟增量"
    if period in MINUTE_SCALES:
        return "分钟 K · 不复权（一个交易日内因子恒定）"
    return "后复权 · 均线算在同一基上"
```

`chart_partial` 换成（docstring 保留原样，只补 `view` 那一段说明）：

```python
@router.get("/api/partials/chart")
def chart_partial(
    request: Request, market: str, code: str, period: str = "day", view: str = "kline",
    main: str = "ma", sub: str = "vol", ma: str = "",
):
    """个股页那一块图：视图切换 + 周期条 + 指标开关 + 挂着 `data-chart-url` 的那个 div。

    为什么在服务端渲染而不是前端自己切：CSP 是 `'self'`（不许内联脚本），
    而「现在选中的是哪一档」这份状态一旦落在前端，换档后屏上说的就可能是上一档的话（D158）。

    周期名要过 `parse_period`：不认识的名字在这里就说 400，而不是悄悄换成日线——
    那一屏会举着「日」的高亮画一段没人请求过的东西，而 `bars` 接口对同一个写法已经是 400。
    分钟档不走那一道：它认的是日历周期，`m5` 到那儿只会得到 400，而周期条上确实写着这一档。
    指标串同理：局部渲染出去的那条 `data-chart-url` 若注定 400，屏幕上说的是
    「行情序列读不出来。」而不是「副图最多三格」——宁可不画，不说错话。
    """
    require_user(request)
    web = request.app.state.web
    if view not in dict(CHART_VIEWS):
        raise ValidationError(
            f"不认识的视图 {view}：可选的是 {'、'.join(label for _name, label in CHART_VIEWS)}"
        )
    if period not in MINUTE_SCALES:
        parse_period(period)  # 只为止声，选中态仍给调用方那一串
    _split_names(main, MAIN_INDICATORS)
    picked_sub = _split_names(sub, SUB_INDICATORS, limit=MAX_SUB_PANES)
    _windows(ma)
    ctx = web.contexts.current()
    market = canonical_market(market)
    code = canonical_code(market, code)
    if market not in ctx.settings.markets:
        raise HTTPException(status_code=404)
    available = ctx.minute.availability(market)
    return web.render(
        request,
        "partials/chart.html",
        market=market,
        code=code,
        periods=CHART_PERIODS,
        period=period,
        views=CHART_VIEWS,
        view=view,
        minute_scales=tuple(MINUTE_SCALES),
        main=main,
        sub=sub,
        ma=ma,
        mains=MAIN_INDICATORS,
        subs=SUB_INDICATORS,
        labels=CHART_INDICATOR_LABELS,
        # 三格满了就把点不出来的那几个置灰：让人撞上一条 400 不如先让他看见规矩
        sub_full=len(picked_sub) >= MAX_SUB_PANES,
        sub_limit=MAX_SUB_PANES,
        # 置灰的判据只有一份来源（Task 6）：前端记一张市场×档位表，
        # 下一句「谁能答」改了屏幕上就是假灰
        series_ok=available.series.ok,
        series_why=available.series.why_not,
        bars_ok=available.bars.ok,
        bars_why=available.bars.why_not,
        caption=_chart_caption(view, period),
        chart_url=(
            f"/api/symbol/{market}/{code}/minute"
            if view == "time"
            else (
                f"/api/symbol/{market}/{code}/bars"
                f"?period={period}&main={main}&sub={sub}&ma={ma}"
            )
        ),
    )
```

- [ ] 实现：`src/sams/web/templates/partials/chart.html` 整块换成

```html
{% set picked_main = main.split(",") %}
{% set picked_sub = sub.split(",") %}
{% set base = "/api/partials/chart?market=" ~ market ~ "&code=" ~ code %}
<div class="chart-box">
  <div class="chart-bar">
    <span class="chart-group" role="group" aria-label="视图">
      {% for name, label in views %}
      {% set blocked = name == "time" and not series_ok %}
      {% set cls = ("on" if name == view else "") ~ (" dim" if blocked else "") %}
      {% set link = base ~ "&view=" ~ name ~ "&period=" ~ period ~ "&main=" ~ main ~ "&sub=" ~ sub ~ "&ma=" ~ ma %}
      <a hx-get="{{ link }}" hx-target=".chart-box" hx-swap="outerHTML" href="?view={{ name }}"
         {% if blocked %}title="{{ series_why }}"{% endif %}
         {% if name == view %}aria-current="true"{% endif %}
         {% if cls %}class="{{ cls | trim }}"{% endif %}>{{ label }}</a>
      {% endfor %}
    </span>
    <span class="chart-group" role="group" aria-label="周期">
      {% for value, label in periods %}
      {% set blocked = value in minute_scales and not bars_ok %}
      {% set cls = ("on" if value == period and view != "time" else "") ~ (" dim" if blocked else "") %}
      <a hx-get="{{ base }}&view=kline&period={{ value }}&main={{ main }}&sub={{ sub }}&ma={{ ma }}"
         hx-target=".chart-box" hx-swap="outerHTML" href="?period={{ value }}"
         {% if blocked %}title="{{ bars_why }}"{% endif %}
         {% if value == period and view != "time" %}aria-current="true"{% endif %}
         {% if cls %}class="{{ cls | trim }}"{% endif %}>{{ label }}</a>
      {% endfor %}
    </span>
    {% if view != "time" %}
    <span class="chart-group" role="group" aria-label="主图指标">
      {% for name in mains %}
      {% set on = name in picked_main %}
      {% set next_main = ((picked_main | reject("equalto", name)) if on else (picked_main + [name])) | join(",") %}
      <a role="button" data-indicator="{{ name }}" hx-get="{{ base }}&view={{ view }}&period={{ period }}&main={{ next_main }}&sub={{ sub }}&ma={{ ma }}"
         hx-target=".chart-box" hx-swap="outerHTML" href="#"
         aria-pressed="{{ 'true' if on else 'false' }}" {% if on %}class="on"{% endif %}>{{ labels[name] }}</a>
      {% endfor %}
    </span>
    <span class="chart-group" role="group" aria-label="副图指标">
      {% for name in subs %}
      {% set on = name in picked_sub %}
      {% set blocked = sub_full and not on %}
      {% set next_sub = ((picked_sub | reject("equalto", name)) if on else (picked_sub + [name])) | join(",") %}
      {% set link = base ~ "&view=" ~ view ~ "&period=" ~ period ~ "&main=" ~ main ~ "&sub=" ~ next_sub ~ "&ma=" ~ ma %}
      <a role="button" data-indicator="{{ name }}"{% if blocked %} aria-disabled="true"
         title="副图最多 {{ sub_limit }} 个，先关掉一个再开这个"{% else %}
         hx-get="{{ link }}" hx-target=".chart-box" hx-swap="outerHTML" href="#"{% endif %}
         aria-pressed="{{ 'true' if on else 'false' }}" {% if on %}class="on"{% endif %}>{{ labels[name] }}</a>
      {% endfor %}
    </span>
    {% endif %}
  </div>
  <div id="chart" style="width:100%;height:480px" data-chart-kind="{{ view }}"
       data-chart-period="{{ period }}" data-chart-url="{{ chart_url }}"></div>
  <p class="muted chart-caption">{{ caption }}</p>
</div>
```

> 两组链接的灰都只写 `class="dim"` + `title`，**不写** `aria-disabled="true"`，也**不删** `hx-get`：
> 副图满了那一格用的才是那一个属性（那里真的点不动，服务端会 400），而分钟档与分时这一灰点下去
> 仍有一句 note 可听（D165）。把两种灰混成一种，读屏软件就会替人回答「这一档没用」，
> 而屏幕上那句话本来是想告诉他「为什么没用、找谁才有」。**别**为了让两处形状统一去共用属性。
> `cls` 用 `| trim` 是因为拼接留下的那个前导空格会写成 `class=" dim"`，测试与 CSS 都按 `dim` 认。

- [ ] 实现：`src/sams/web/static/app.css`

`.chart-group a.on` 那行之后加一条（既有的 `a[aria-disabled="true"]` 只管副图那种「点不动」的灰，
视图与周期的灰要连选中态一起表达，所以给一个自己的类）：

```css
.chart-group a.dim { color: var(--muted); opacity: .55; }
.chart-group a.dim:hover { border-color: transparent; }
```

- [ ] 跑到绿

```bash
uv run pytest tests/web -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 提交

```bash
git add src/sams/web/pages/symbol.py src/sams/web/templates/partials/chart.html src/sams/web/static/app.css tests/web/test_symbol_minute.py tests/web/test_symbol.py
git commit -m "feat(web): 分时 | K线 视图切换与 D165 置灰——灰档仍点得到，周期条不再 400"
```

---

## Task 13：`app.js` 的 `time` 那一条画法（分时双线 + 昨收基准 + 对称涨跌轴）

CSP 是 `'self'`，脚本没有测试运行器可跑（仓库里没有 JS 测试链，引一套是另一个工程），
所以这一段的红/绿不靠 unit test：**先钉「模板与脚本认同一个 kind」这条能钉的**，
再用一个本地无头 Chrome 的**渲染断言**（画完之后 `canvas` 上真有像素、`option` 里真有两条线）
当真正的验收。harness 只活在 `tmp/harness/`，不进仓库。

**Files:**
- Modify: `src/sams/web/static/app.js`（`samsChart` 拆成 `klineChart` / `timeChart`）
- Test: `tests/web/test_assets.py`（加一条）、`tests/web/test_symbol_minute.py`（加一条）
- Create（不提交）: `tmp/harness/index.html`、`tmp/harness/minute.json`

- [ ] 写失败测试：追加到 `tests/web/test_assets.py`

```python
def test_app_js_dispatches_on_the_chart_kind_the_template_writes():
    """`data-chart-kind` 不再是写了没人读的装饰（§5.3 的实现时订正）。

    这一条只钉「脚本按 kind 分了两条路」这个事实：分时的画法对不对，
    由 `tmp/harness` 那条无头渲染断言看像素，不靠文本匹配假装看过图。
    """
    text = (WEB / "static" / "app.js").read_text()
    assert "dataset.chartKind" in text
    assert "function timeChart" in text
    assert "function klineChart" in text
```

- [ ] 写失败测试：追加到 `tests/web/test_symbol_minute.py`

```python
def test_the_time_view_carries_the_kind_the_script_switches_on(logged_minute):
    """局部里那个 div 的 kind 必须是 `time`：脚本按它选画法，写错就是拿蜡烛的键去读分时。"""
    _seed(logged_minute)
    html = logged_minute.client.get(
        "/api/partials/chart?market=A&code=600519&view=time"
    ).text
    assert 'data-chart-kind="time"' in html
    assert 'data-chart-period="day"' in html  # 分时不看周期：那一个属性留着旧值就说明没换视图
```

> 这一条在 Task 12 之后已经绿。留着它是**回归钉**：Task 13 只改脚本，模板不该动，
> 改动了就说明有人把 kind 的取值改成脚本不认的那一个。

- [ ] 跑到红

```bash
uv run pytest tests/web/test_assets.py -q
```

预期：`assert "dataset.chartKind" in text` 失败（现脚本没读它，`function timeChart` 也不存在）。

- [ ] 实现：`src/sams/web/static/app.js`

`live` 那一行之后加两个常量：

```js
  var PRICE = "#2563eb"; // 分时线：与涨跌无关，它要说的是「走到哪」，红绿会把它读成涨或跌
  var AVG = "#e8a33d"; // 均价线：交易所口径的均价，与分时线交叉处就是「强于均价/弱于均价」
```

把现有 `function samsChart(el)` 里 `fetch` 之后那一大段（从 `var dates = payload.dates || []`
到 `if (note) note.textContent = note.textContent + caption(payload);`）**整段搬进**
`function klineChart(el, payload)`，函数头自己取 `note`：

```js
  function klineChart(el, payload) {
    var box = el.parentNode;
    var note = box && box.querySelector(".chart-caption");
    var dates = payload.dates || [];
    // ……以下逐字照搬原实现，不改一行：subNames / grids / xAxes / yAxes / series /
    //     setOption / 最后那句 caption 追加
  }
```

新增分时的画法：

```js
  function timeChart(el, payload) {
    var box = el.parentNode;
    var note = box && box.querySelector(".chart-caption");
    var times = payload.times || [];
    if (!times.length) {
      if (note) note.textContent = "这一段没有数可画。";
      return;
    }
    var baseline = payload.prev_close;
    // 昨收没给（源里那一格空）就退回价格区间的中点当原点：按 0 算百分比会画出一条 0% 在天外的轴
    var haveBase = typeof baseline === "number" && baseline > 0;
    var prices = payload.price || [];
    var lo = Infinity;
    var hi = -Infinity;
    prices.forEach(function (value) {
      if (typeof value !== "number") return;
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    });
    if (lo === Infinity) { lo = hi = haveBase ? baseline : 0; }
    var anchor = haveBase ? baseline : (lo + hi) / 2;
    // 两条轴共用同一个「离原点多远」：只按价格取范围时涨 2% 与跌 2% 的视觉斜率不同，
    // 而分时图给人看的正是那个斜率
    var span = Math.max(hi - anchor, anchor - lo) * 1.05 || 0.01;
    var pctSpan = haveBase ? (span / baseline) * 100 : 1;
    var grids = [
      { top: TOP + "%", height: BAND - SUB - GAP + "%" },
      // 主图下沿 + 间隙 = `14 + 70 - 11` = 81%：那一格只有 11% 高，再留 GAP 就顶到滑块
      { top: TOP + BAND - SUB + "%", height: SUB + "%" },
    ];
    var xAxes = [
      { gridIndex: 0, type: "category", data: times, boundaryGap: false,
        axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } },
      { gridIndex: 1, type: "category", data: times, boundaryGap: false,
        axisLabel: { show: true, margin: 10 }, axisTick: { show: false }, axisLine: { show: true } },
    ];
    var yAxes = [
      { gridIndex: 0, type: "value", min: anchor - span, max: anchor + span,
        axisLabel: { show: true, hideOverlap: true }, splitLine: { show: true } },
      // 第二条轴写百分比：盯盘的人看的是「现在离昨收几个点」，那条轴就在右手边
      { gridIndex: 0, type: "value", position: "right", min: -pctSpan, max: pctSpan,
        axisLabel: { show: true, hideOverlap: true, formatter: "{value}%" }, splitLine: { show: false } },
      { gridIndex: 1, type: "value", axisLabel: { show: true, fontSize: 10, hideOverlap: true,
        formatter: wan }, splitLine: { show: false } },
    ];
    var series = [
      {
        name: "分时", type: "line", data: prices,
        xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
        lineStyle: { width: 1, color: PRICE },
        // 昨收那条横基准线是分时的原点：没有它，屏幕上的高低只剩「比右边那格高」
        markLine: haveBase
          ? {
              symbol: "none", silent: true, animation: false,
              data: [{ yAxis: baseline }],
              lineStyle: { type: "dashed", color: "#8a8a94" },
              label: { formatter: "昨收 " + baseline.toFixed(2), position: "insideStartTop" },
            }
          : undefined,
      },
      {
        name: "均价", type: "line", data: payload.avg || [],
        xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
        lineStyle: { width: 1, color: AVG },
      },
      {
        // 每分钟的量：服务侧已经把累计量差分过（Task 8），前端不能再 diff 一遍
        name: "成交量", type: "bar", data: payload.volume || [],
        xAxisIndex: 1, yAxisIndex: 2,
        itemStyle: {
          color: function (params) {
            var now = prices[params.dataIndex];
            var prev = params.dataIndex > 0 ? prices[params.dataIndex - 1] : anchor;
            return now >= prev ? UP : DOWN;
          },
        },
      },
    ];
    if (live) live.dispose();
    var chart = echarts.init(el);
    live = chart;
    chart.setOption({
      legend: { type: "scroll", top: 0 },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        { type: "slider", xAxisIndex: [0, 1], height: 18, bottom: 2 },
      ],
      series: series,
    });
    if (note && payload.date) {
      // 分时只答最近一个交易日（Task 3 的区间校验钉着），日期不写在屏上会让人以为这是「今天」
      note.textContent = note.textContent + "：" + payload.date;
    }
  }
```

`samsChart` 的 `fetch` 回调只剩分档：

```js
  function samsChart(el) {
    if (typeof echarts === "undefined" || el.dataset.samsChart === "on") return;
    var url = el.dataset.chartUrl;
    if (!url) return;
    el.dataset.samsChart = "on";
    fetch(url)
      .then(function (resp) { return resp.json(); })
      .then(function (payload) {
        var box = el.parentNode;
        var note = box && box.querySelector(".chart-caption");
        if (payload.note) {
          if (note) note.textContent = payload.note;
          el.textContent = "";
          return;
        }
        // 两份 payload 的键不一样：分时时 `payload.times`，蜡烛时 `payload.dates`，
        // 拿一套键去读另一套会得到一条空白曲线而不是报错——那比空白更难查
        if (el.dataset.chartKind === "time") timeChart(el, payload);
        else klineChart(el, payload);
      })
      .catch(function () {
        el.textContent = "行情序列读不出来。";
      });
  }
```

> 搬完之后 `grep -c "function klineChart\|function timeChart" src/sams/web/static/app.js` 应为 2，
> 且 `live` 那一个全局仍然只有一处赋值点（两张图不能同时活着，`resize` 只挂一次）。

- [ ] 跑到绿

```bash
uv run pytest tests/web -q
```

```bash
uv run ruff check --no-cache src/sams tests
```

- [ ] 无头渲染断言（不进仓库）：`tmp/harness/`

先把一份真的分时数据落到本地——**服务在 8765 上跑着，读它的接口，不去碰库**：

```bash
mkdir -p tmp/harness
curl -s -c tmp/harness/jar -L -X POST http://127.0.0.1:8765/login \
  -d "username=sp1check" -d "password=Sp1-1027-verify" > /dev/null
curl -s -b tmp/harness/jar "http://127.0.0.1:8765/api/symbol/A/600519/minute" -o tmp/harness/minute.json
head -c 200 tmp/harness/minute.json
```

> `minute.json` 里若是 `"note"` 而不是 `times`，说明这一趟链上没给数（盘后或未配分钟源）：
> 那就换一只确实有数的票，或先跑一拍采集。**不许**手编 JSON 让它「看起来有数」——
> 那等于用假数据验真画法。

`tmp/harness/index.html`（把 `data-chart-url` 指到本地那份 JSON，脚本与库都从仓库里原路径取）：

```html
<!doctype html>
<meta charset="utf-8">
<link rel="stylesheet" href="../../src/sams/web/static/app.css">
<script src="../../src/sams/web/static/vendor/echarts.min.js"></script>
<div class="chart-box">
  <div id="chart" style="width:1180px;height:480px" data-chart-kind="time"
       data-chart-url="./minute.json"></div>
  <p class="muted chart-caption">当日分时 · 昨收为基准线，量是每分钟增量</p>
</div>
<script src="../../src/sams/web/static/app.js"></script>
```

跑一次并**看回来说话**：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --virtual-time-budget=4000 \
  --window-size=1280,900 --screenshot=tmp/harness/shot.png \
  "file://$PWD/tmp/harness/index.html" 2>&1 | tail -3
```

判据（逐条说出来，别只说「看起来对」）：
1. `shot.png` 里主图那格有**两条线**（蓝与橙），且有一条**水平虚线**——昨收基准。
2. 横轴**只在下面那格**写时刻，中间没有午休那一段空白（A 股 11:30→13:00 是接上的）。
3. 下面那一格的量柱**有红有绿**，且右侧那条轴写的是 `%`。
4. 浏览器控制台无报错（`--enable-logging=stderr --v=0` 再看一遍 stderr）。

```bash
rm -rf tmp/harness
```

- [ ] 提交

```bash
git add src/sams/web/static/app.js tests/web/test_assets.py tests/web/test_symbol_minute.py
git commit -m "feat(web): 分时画法——双线加昨收基准线，涨跌轴与价格轴同一个半径"
```

---

## Task 14：全量绿、真接口验收、把 spec 的「计划 B」那几行订正成实现

前三件事分开做，顺序不能换：**先全量测试（不占库）→ 再重启服务 → 最后用真票验**（要占写锁的 CLI
一步都不在 serve 活着的时候跑）。

- [ ] 全量测试（controller 跑，**不带管道**，此时先停掉 8765 上的 serve）

```bash
uv run pytest -q
```

预期全绿。若有个别用例要碰 DuckDB 而报「数据库被占用」，那是 serve 还活着，不是代码坏了——
先按下面那条命令停，再重跑。

```bash
lsof -t data/sams.duckdb | xargs -r kill
```

- [ ] 真接口跑一遍数据层（这三条只读网络，不写库；离线机器上会红，那就要人点头再跳）

```bash
uv run pytest -q -m live tests/datasource/test_minute_probe.py
```

> `pyproject.toml` 的 `addopts = "-m 'not live'"` 已经把真网络用例挡在全量之外，
> 上面那条 `-m live` 是显式覆盖它。本轮新写的 live 用例必须挂 `@pytest.mark.live`
> （既有写法见 `tests/datasource/test_tencent.py:316`）——不挂 marker 的用例会在一台没网的机器上
> 把全量测试拖成红，而那是最容易被当成「代码有问题」去查半天的一种红。

- [ ] 起服务并人眼验一次

```bash
SAMS_SECRET_KEY="$(cat tmp/.sams_secret)" nohup uv run sams serve > tmp/serve.log 2>&1 &
```

```bash
curl -s -c tmp/jar -L -X POST http://127.0.0.1:8765/login \
  -d "username=sp1check" -d "password=Sp1-1027-verify" > /dev/null
curl -s -b tmp/jar "http://127.0.0.1:8765/api/symbol/A/600519/minute" | head -c 300
curl -s -b tmp/jar "http://127.0.0.1:8765/api/symbol/A/600519/bars?period=m5" | head -c 300
curl -s -b tmp/jar "http://127.0.0.1:8765/api/partials/chart?market=US&code=AAPL" | grep -o 'class="dim"' | wc -l
rm tmp/jar
```

逐条说出看到的东西，不许只报「通了」：

1. `/minute` 给的是 `times`/`price` 两条等长数组，还是 `note`？给 `note` 就把那句话原文抄出来，
   并确认它说的是「哪一段不供」而不是「这只票没数据」（D165 的判据就在这句措辞上）。
2. `bars?period=m5` 的 `period_label` 是「5分」，`dates` 每一项带 `时:分`。
3. US 那一屏上 `class="dim"` 的个数 ≥ 7（六个分钟周期档 + 分时视图），且它们**都还在** HTML 里。
4. 浏览器开 `http://127.0.0.1:8765/symbol/A/600519`：点「分时」出双线，点「5分」出蜡烛，
   来回切五次不留一块死图（`live.dispose` 那条）。

- [ ] 交易专家验收（既有那三个账号）

```bash
uv run sams user list
```

把分时那一屏与 `m5` 那一屏各截一张图发给 `sp1expert1`–`sp1expert3`，问一句固定话术：
「这张分时图上的两条线，你能不能不看说明就说出哪条是均价？」
答不出就是图例或配色没做到位，回来改 `app.js` 的 `legend`，不要改数据。

> ⚠️ 这一步是**只读**的：`sams user list` 要开库，得先停 serve（同上一条 `lsof` 那一招）。

- [ ] spec 写回

在 `docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md` 的对应条目下
补「实现时订正」（**只加不删**，历史决定不改写）：

- §5.1 `MinuteRead` → 实际**没有**这个类型：分钟帧复用既有的 `FetchResult`，
  时刻列叫 `clock`、与 `date` 一起存，`prev_close` 走 `AdjustedSeries.frame` 之外的旁路。
  少一个类型就少一处「两个名字指同一件事」。
- §5.2 `/api/partials/chart` 的参数表加 `view`（`kline` | `time`）。
- §5.3 那条「`data-chart-kind` 已经写在 div 上但 `app.js` 还没读它」的订正，本轮**已被实现追平**：
  现在读它，并且读的是视图名而不是画法名（`kline`/`time` 两个值）。
- D163 的「来源只答最近那一段」在实现里落成三件事：`MINUTE_LOOKBACK_DAYS=365`、进程内
  60 秒缓存、以及分时**只答最近一个交易日**（区间反向那一道闸在适配器里）。
- D164 的 `adj_factor=1` 在屏幕上有自己的那句话：分钟档的图注写「不复权」，不再沿日线那句「后复权」。
- D165 的置灰在实现里多带一条：灰档**仍可点**，点下去回一句 `note`——
  「不给你点」与「点了告诉你为什么不给」是两种屏幕，后者才让人不再回来试。
- 新的编号决定：**D170 分钟 K 的成交额一律置空**（腾讯那一格单位与新浪差五个数量级，
  没核清的字段算出来的东西看起来都正常）；**D171 分时的量是累计量差出来的每分钟量**
  （源给的是累计，前端再差分一遍就会得出一条递减的假量柱）。

- [ ] 提交

```bash
git add docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md docs/superpowers/plans/2026-10-28-sp2-b-minute-timeshare.md
git commit -m "docs(spec): 计划 B 的实现时订正——MinuteRead 没落地、view 参数、置灰仍可点（D170/D171）"
```

- [ ] 勾掉本文档里所有 `- [ ]`（实现过程中每步都勾，这里只核对没漏）

---

## 覆盖自检（本文档写完后对着 spec 逐条量一遍）

| spec 里的东西 | 落点 |
| --- | --- |
| D163 两条能力、都登记进 `CAPABILITIES` 一处、都不入库 | Task 1（登记 + 探针）、Task 3/4/5（三个适配器方法）、无迁移 |
| D163 腾讯主、新浪备 | Task 3/4（腾讯两档）、Task 5（新浪 `minute_bars`）；链序在 `settings.toml` 的既有路由里，本轮不改配置 |
| D164 分钟不过复权闸、`adj_factor=1` 组装 | Task 7 的 `_assemble`；`tests/test_layering.py` 的 `SOLE_CONSUMER` 由该 Task Step「不许调 `hfq_frame`」守 |
| D165 答不了就直说、置灰但留在原位、话术不写死在前端 | Task 6（唯一出处 `why_not`）、Task 12（灰 + `title` + 仍可点）、Task 10/11（拒绝也回 200 + note） |
| §5.2 `/minute` 那七个键 | Task 10 第一条用例逐字钉住 |
| §6 钉 7：均价 = 累计额 ÷ 累计量、昨收取自 `qt`、不给 500 | Task 8（差分与均价数学）、Task 3（`qt[<symbol>][4]`）、Task 10（拒绝也 200） |
| §6 钉 8：HK/US 拒绝 → 200 + note + 那一档仍在 | Task 10 第二条、Task 12 的 `..._greyed_and_still_clickable` |
| §6 钉 9：一组一个 `aria-current`、开关各自 `aria-pressed` | Task 12 改写的那条既有用例（按组数） |
| §5.3 `app.js` 按 `data-chart-kind` 分两条 | Task 13（含 `tests/web/test_assets.py` 那条钉 + 无头渲染验收） |
| §7 第 2 项列出的五件事 | 分时图=Task 13、分钟 K=Task 4/5/7、两条能力=Task 1、`/minute`=Task 10、置灰话术=Task 6/12 |
| D167 本期不做的（五日分时、不复权切换…） | 本文档一处都没实现，也不许顺手做 |

**本文档没有新增迁移**（分钟数据不入库），所以下一轮（计划 C 的 `0023_chart_prefs.sql`）
号已经排好；写迁移前先看一眼 `ls src/sams/storage/migrations/`，迁移号只写不读，撞号在启动时炸校验和。



