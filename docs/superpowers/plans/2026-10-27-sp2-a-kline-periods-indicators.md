# SP2 计划 A：标准 K 线 + 多周期 + 多指标 + 自选行内移除

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把个股页那一块图换成标准蜡烛 K 线，周期从日线到年线（含 5 日/10 日）可切，主图与副图各自能同时挂多个技术指标；顺带把自选的「移除」收回票那一行的最右侧。

**Architecture:** 分析层已有 12 组指标与 day/week/month 重采样，本计划只做三件事：①`resample()` 的周期从两种扩到五种（季/年/N 日）②`bars_api` 把已经算好的 OHLC 与指标交出去并接收周期/指标名/均线窗口 ③前端换成蜡烛 + 多格副图，选择器是 htmx 拉一个服务端渲染的 chart 局部。分钟粒度与分时图在计划 B，本计划不引任何新数据源。

**Tech Stack:** Python 3.13 + Polars（重采样）、FastAPI + Jinja + htmx、vendored ECharts 全量包（`candlestick` 已在包内）、pytest。

**Spec:** `docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md`（D157–D168；本计划落 D158/D159/D160/D161/D162/D166/D168，D163–D165 属计划 B/C）。

**House rules（每条都绑，违反即返工）:** 只用 `uv`；**绝不跑 `ruff format`**；lint 用 `uv run ruff check --no-cache src/sams tests`；全量套件只由控制者跑且不加管道；不许 `git add -A`/`.`；不动未跟踪的 `qodercli_resume.sh` 与 `tmp/`；只在 main 上做；每条行为先红（且必须是因为缺这件事而红）后绿；注释只写一句「为什么」。

---

## 文件结构

| 文件 | 本计划里它负责什么 |
| --- | --- |
| `src/sams/analysis/resample.py` | 周期分组：`week/month` 之外加 `quarter/year/nday`（D161） |
| `src/sams/analysis/indicators.py` | `metrics()` 收每族的窗口（「均线自己设」的落点，D162） |
| `src/sams/services/analysis.py` | `parse_period()` 把 `5d` 拆成 `("nday", 5)`；`series()` 转发窗口；`PERIODS` 扩名单 |
| `src/sams/cli/analysis.py` | `--period` 的取值表跟着 `PERIODS` 走（不放宽，只对齐） |
| `src/sams/web/pages/symbol.py` | `bars_api` 的新参数与新出参（D159/D160/D162/D168）；`/api/partials/chart` 局部（D158） |
| `src/sams/web/pages/dashboard.py` | `quotes_partial` 的 `manage` 标志（D166） |
| `src/sams/web/templates/partials/chart.html` | 周期条、指标开关、图注、`<div data-chart-url>` |
| `src/sams/web/templates/symbol.html` | 用 chart 局部换掉那一块裸 div |
| `src/sams/web/templates/partials/quotes.html` | `manage` 时的末列移除表单 + 备注并进名称列 |
| `src/sams/web/templates/watchlist.html` | 删掉重复的管理表；`?manage=1` |
| `src/sams/web/static/app.js` | 蜡烛 + 多 grid 副图 + 红涨绿跌 |
| `src/sams/web/static/app.css` | 周期条与开关的样式、`aria-current` 高亮 |

---

### Task 1: `resample()` 认得 quarter 与 year

**Files:**
- Modify: `src/sams/analysis/resample.py`（常量在 `:55`，`_period_end` 在 `:69`，`resample` 在 `:83`）
- Test: `tests/analysis/test_resample.py`

- [ ] **Step 1: 写失败测试**（追加到「分组口径」那一节末尾，紧邻 `test_a_month_bar_is_named_after_the_last_day_of_that_month`）

```python
def test_a_quarter_bar_is_named_after_the_last_day_of_that_quarter():
    """季线沿用周月那条口径 1：名字是日历季末日，实际最后一根在 `last` 里。
    3-29 提前收市那一季仍然叫 3-31，否则「本季」会随长假漂移。"""
    days = [date(2024, 1, 3), date(2024, 2, 15), date(2024, 3, 28)]
    out = resample(daily(days), "quarter", as_of=END)
    assert out.frame["date"].to_list() == [date(2024, 3, 31)]
    assert out.frame["first"].to_list() == [date(2024, 1, 3)]
    assert out.frame["last"].to_list() == [date(2024, 3, 28)]


def test_a_year_bar_collects_every_quarter_of_that_year():
    days = [date(2024, month, 15) for month in range(1, 13)]
    out = resample(daily(days), "year", as_of=END)
    assert out.frame["date"].to_list() == [date(2024, 12, 31)]
    assert out.frame["bars"].to_list() == [12]


def test_quarters_split_across_a_year_boundary():
    days = [date(2023, 12, 15), date(2024, 1, 15), date(2024, 2, 15)]
    out = resample(daily(days), "quarter", as_of=END)
    assert out.frame["date"].to_list() == [date(2023, 12, 31), date(2024, 3, 31)]
```

- [ ] **Step 2: 跑到红**

Run: `uv run pytest tests/analysis/test_resample.py -q -k "quarter or year"`
Expected: FAIL —`ValidationError: 重采样周期只认 \`week\` 与 \`month\``（红的原因必须是周期名不认识）

- [ ] **Step 3: 实现**

`resample.py` 里把常量与两个分支补齐：

```python
WEEK = "week"
MONTH = "month"
QUARTER = "quarter"
YEAR = "year"

#: `resample()` 能处理的周期名。`services` 层组装 `PERIODS` 取值表时引用这一份，
#: 不重新写字符串——两处各管一份会在有人只改一处的时候静默漂移
RESAMPLE_PERIODS: tuple[str, ...] = (WEEK, MONTH, QUARTER, YEAR)
```

`_period_end` 换成按周期分支（日历季末日与年末，月日沿用既有那句「下个月一号减一天」的写法）：

```python
def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _period_end(day: date, period: str) -> date:
    """该日所在周期的**日历收市日**：周五（周）、当月末（月）、3/6/9/12 月末（季）、12-31（年）。"""
    if period == WEEK:
        return day + timedelta(days=4 - day.weekday())
    if period == QUARTER:
        # 该季最后一个月：(month-1)//3*3+3 —— 直接 `month//3*3` 在 1/4/7/10 月会算出月份 0
        return _month_end(day.replace(month=((day.month - 1) // 3) * 3 + 3))
    if period == YEAR:
        return date(day.year, 12, 31)
    return _month_end(day)
```

分组键（`_quarter_group` / `_year_group` 两个小函数，紧跟 `_week_group`），`resample()` 内的两处判断：

```python
def _quarter_group(day: date) -> date:
    return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)


def _year_group(day: date) -> date:
    return date(day.year, 1, 1)


#: 周期 → 分组键。N 日线不在这张表里：它的键不是日历日而是根数（D161）
_GROUP_OF = {WEEK: _week_group, MONTH: _month_start, QUARTER: _quarter_group, YEAR: _year_group}
```

`resample()` 里：

```python
    if period not in RESAMPLE_PERIODS:
        raise ValidationError(
            f"重采样周期只认 {', '.join(RESAMPLE_PERIODS)}，收到的是 {period!r}："
            '写成 "weekly" 静默当成 week 等于让调用方以为自己要的东西被给了'
        )
    ...
    group_of = _GROUP_OF[period]
```

（`MONTH` 原本用内联 `lambda day: day.replace(day=1)`，现在提出来叫 `_month_start`，四个键同一形状。）

- [ ] **Step 4: 跑到绿**

Run: `uv run pytest tests/analysis/test_resample.py -q`
Expected: 全绿（既有的周月用例一枚不许改）

- [ ] **Step 5: 提交**

```bash
git add src/sams/analysis/resample.py tests/analysis/test_resample.py
git commit -m "feat(analysis): 重采样认得季线与年线，日历收市日口径与周月同一条（D161）"
```

---

### Task 2: `resample()` 认得 nday（5 日线 / 10 日线）

**Files:**
- Modify: `src/sams/analysis/resample.py`
- Test: `tests/analysis/test_resample.py`

- [ ] **Step 1: 写失败测试**

```python
def test_nday_counts_bars_from_the_first_one_and_labels_each_group_by_its_last_day():
    """N 日线没有日历收市日可言（D161 的例外）：从送进来的第一根起每 n 根一组，
    那一根的名字就是组内最后一个交易日。"""
    days = WEEKDAYS[:12]
    out = resample(daily(days), "nday", as_of=END, n=5)
    assert out.frame["date"].to_list() == [days[4], days[9], days[11]]
    assert out.frame["bars"].to_list() == [5, 5, 2]


def test_a_short_nday_group_is_pending():
    """最后一组只盖了两天 ⇒ 这根 5 日线没走完。不足就是不足，不冒充一根完整的。"""
    out = resample(daily(WEEKDAYS[:12]), "nday", as_of=END, n=5)
    assert out.frame["pending"].to_list() == [False, False, True]


def test_nday_needs_a_bar_count():
    with pytest.raises(ValidationError, match="n"):
        resample(daily(WEEKDAYS[:12]), "nday", as_of=END)


def test_nday_refuses_a_non_positive_bar_count():
    for bad in (0, -3):
        with pytest.raises(ValidationError, match="正整数"):
            resample(daily(WEEKDAYS[:12]), "nday", as_of=END, n=bad)
```

- [ ] **Step 2: 跑到红**

Run: `uv run pytest tests/analysis/test_resample.py -q -k nday`
Expected: FAIL —`TypeError: resample() got an unexpected keyword argument 'n'`（红在「这一档还不存在」）

- [ ] **Step 3: 实现**

签名加 `n`，`NDAY = "nday"` 常量（不进 `RESAMPLE_PERIODS` 的那个「周期名」语义？进——它是 `resample` 能处理的周期，只是还要一个 `n`；`RESAMPLE_PERIODS` 加它，校验分支单独要 `n`）：

```python
NDAY = "nday"
RESAMPLE_PERIODS: tuple[str, ...] = (WEEK, MONTH, QUARTER, YEAR, NDAY)


def _nday_labels(days: list[date], n: int) -> list[date]:
    """每根所属那一组的**标签**（组内最后一个交易日）。

    从首根起往后数：边界随读取窗口的首根移动，这一档本来就没有日历锚可依附，
    说清楚比假装它稳定好（D161）。
    """
    total = len(days)
    return [days[min((index // n) * n + n - 1, total - 1)] for index in range(total)]
```

`resample()` 内：

```python
    if period == NDAY:
        if not isinstance(n, int) or n <= 0:
            raise ValidationError(
                f"`nday` 要一个正整数 `n`（每几根日线一根 K），收到的是 {n!r}"
            )
    elif n is not None:
        raise ValidationError(f"周期 {period} 不按根数分组，`n` 该留空（现在是 {n!r}）")
```

分组键那一列：`period == NDAY` 时用 `_nday_labels(days, n)`，其余用 `_GROUP_OF[period]`。
`pending` 一列：`NDAY` 走「组内根数 < n」（根数就是已经聚合出来的 `bars` 列），其余保持 `end > as_of`：

```python
    if period == NDAY:
        counts = grouped["bars"].to_list()
        pending = [count < n for count in counts]
        out = grouped.with_columns(
            pl.Series("date", grouped["_group"].to_list(), dtype=pl.Date),
            pl.Series("pending", pending, dtype=pl.Boolean),
        ).drop("_group")
    else:
        ends = [_period_end(g, period) for g in grouped["_group"].to_list()]
        out = grouped.with_columns(...)   # 既有那两行
```

docstring 补一句 nday 的口径（不足 n 根 ⇒ `pending`，边界随窗口首根移动）。

- [ ] **Step 4: 跑到绿**

Run: `uv run pytest tests/analysis/test_resample.py tests/analysis/test_indicators.py -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add src/sams/analysis/resample.py tests/analysis/test_resample.py
git commit -m "feat(analysis): N 日线（5d/10d）按根数分组，不足 n 根判 pending（D161）"
```

---

### Task 3: 服务层接上这些周期，`metrics()` 收均线窗口

**Files:**
- Modify: `src/sams/services/analysis.py:53`（`PERIODS`）、`:177`（`series`）、`:256`（重采样那一跳）
- Modify: `src/sams/analysis/indicators.py:331`（`INDICATORS` 的三个 lambda）、`:351`（`metrics`）
- Modify: `src/sams/cli/analysis.py:304`（`--period` 的 `choices`）
- Test: `tests/analysis/test_indicators.py`、`tests/services/test_analysis.py`

- [ ] **Step 1: 写失败测试（指标窗口）**

```python
def test_metrics_honours_a_custom_window_per_indicator():
    """「均线自己设」的落点：窗口从外面进来，列名跟着窗口变（D162）。
    默认那组（`MA_WINDOWS`）不许受影响——同一条 `metrics(s, ("ma",))` 的出列变了，
    所有在用它的人会拿到一批对不上号的列名。"""
    series = daily(WEEKDAYS[:12])
    default = metrics(series, ("ma",))
    custom = metrics(series, ("ma",), windows={"ma": (3, 7)})
    assert [c for c in default.columns if c != "date"] == [f"ma{w}" for w in MA_WINDOWS]
    assert [c for c in custom.columns if c != "date"] == ["ma3", "ma7"]
    assert custom["ma3"].to_list()[:2] == [None, None]  # 三根才够第一行，两根不够
```

Run: `uv run pytest tests/analysis/test_indicators.py -q -k custom_window` → FAIL（`metrics()` 没有 `windows` 参数）

- [ ] **Step 2: 实现 `metrics(..., windows=)`**

`INDICATORS` 里那三个包一层的 lambda 换成函数本身（`ma`/`ema`/`volume_ma` 本来就收 `*windows`，
包一层只是让 `metrics` 没法传参）：

```python
INDICATORS: dict[str, Callable[..., pl.DataFrame]] = {
    "ma": ma,
    "ema": ema,
    ...
    "volume_ma": volume_ma,
}
```

`metrics()` 签名与取数那两行：

```python
def metrics(
    series: object,
    names: Sequence[str] = DEFAULT_INDICATORS,
    *,
    windows: Mapping[str, Sequence[int]] | None = None,
) -> pl.DataFrame:
    ...
    for name in names:
        built = INDICATORS[name](series, *(windows or {}).get(name, ()))
```

（`tests/analysis/test_indicators.py` 里已经 import 了 `MA_WINDOWS` 与 `metrics` 就用那两个名字；
没 import 的话补上，不改别的用例。）

- [ ] **Step 3: 写失败测试（服务层的周期）**

追加到 `tests/services/test_analysis.py`（沿用该文件既有的 `service`/种子函数，不新造夹具）：

```python
def test_parse_period_splits_the_bar_count_periods():
    from sams.services.analysis import parse_period

    assert parse_period("day") == ("day", None)
    assert parse_period("quarter") == ("quarter", None)
    assert parse_period("5d") == ("nday", 5)
    assert parse_period("10d") == ("nday", 10)
    with pytest.raises(ValidationError, match="周期只认"):
        parse_period("weekly")
    with pytest.raises(ValidationError, match="周期只认"):
        parse_period("0d")


def test_series_can_read_a_five_day_bar(service):
    """5 日线吃的是库里的日线，不新增数据源（D160）：11 根日线 ⇒ 两根 5 日 K，
    最后一根只盖了一天 ⇒ 它在 `pending_periods` 里。"""
    read = service.series("A", CODE, start=START, end=END, period="5d", indicators=("ma",))
    assert read.period == "5d"
    assert read.table["bars"].to_list() == [5, 5, 1]
    assert read.pending_periods == [read.table["date"][-1]]
```

Run: `uv run pytest tests/services/test_analysis.py -q -k "parse_period or five_day"` → FAIL

- [ ] **Step 4: 实现服务层**

`src/sams/services/analysis.py`：

```python
#: 日线及以上的周期名。`5d`/`10d` 这类 N 日线不在名单里，由 `parse_period` 现拆
PERIODS: tuple[str, ...] = (_DAY, WEEK, MONTH, QUARTER, YEAR)
_NDAY_RE = re.compile(r"^([1-9][0-9]{0,2})d$")


def parse_period(value: str) -> tuple[str, int | None]:
    """把周期名拆成 (重采样周期, 每几根一组)。

    `5d` 是「5 日线」而不是「5 天」：这一档的分组按**根数**走，与日历无关。
    不认识的名字一律拒绝，不静默按日线算——那等于给一段不是对方请求的行情。
    """
    match = _NDAY_RE.match(value or "")
    if match:
        return NDAY, int(match.group(1))
    if value in PERIODS:
        return value, None
    raise ValidationError(
        f"周期只认 {', '.join(PERIODS)} 或 5d/10d 这样的『N 日线』，收到的是 {value!r}："
        "写成 \"weekly\" 静默当成 week 等于让调用方以为自己要的东西被给了"
    )
```

`series()` 里把 `if period not in PERIODS: raise ...` 那段换成：

```python
        kind, bars_per_group = parse_period(period)
```

重采样那一跳与转发窗口：

```python
        if kind != _DAY:
            series = resample(series, kind, n=bars_per_group, as_of=today)
        ...
        table = frame.select([...]).hstack(
            metrics(series, names, windows=windows or {}).drop("date")
        )
```

`series()` 加关键字参数 `windows: Mapping[str, Sequence[int]] | None = None`（透传给 `metrics`），
`SeriesRead.period` 仍存**调用方给的那一串**（`"5d"`），不要存拆开的结果——屏幕上要说的是用户点的那一档。

`src/sams/cli/analysis.py:304` 的 `choices=list(PERIODS)` 去掉 `choices`，`help` 里写清
「`day/week/month/quarter/year` 或 `5d`/`10d` 这样的 N 日线」；不认识时服务层那句报错就是屏幕上那句话（CLI 不许自己再拼一份文案）。

- [ ] **Step 5: 跑到绿**

Run: `uv run pytest tests/services/test_analysis.py tests/analysis tests/cli -q`
Expected: 全绿。若某条既有 CLI 用例钉了 `--period` 的 `choices` 报错文案，把它对准 `parse_period` 那句改一处，别加第二份文案。

- [ ] **Step 6: 提交**

```bash
git add src/sams/analysis/indicators.py src/sams/services/analysis.py src/sams/cli/analysis.py tests/analysis/test_indicators.py tests/services/test_analysis.py tests/cli
git commit -m "feat(analysis): 周期扩到季/年/N 日线，指标窗口可从外面给（D160/D161/D162）"
```

---

### Task 4: `bars_api` 交出蜡烛与多指标

**Files:**
- Modify: `src/sams/web/pages/symbol.py:162`（`bars_api`）
- Test: `tests/web/test_symbol.py`

- [ ] **Step 1: 写失败测试（OHLC 四档 + 周期）**

```python
def test_bars_api_gives_the_four_candle_prices_on_the_adjusted_basis(logged):
    """蜡烛要四档，而四档都得在与均线同一基上（D159）。
    复权之后 `high ≥ max(open, close)`、`low ≤ min(open, close)` 仍要成立，
    否则画出来的是一根实体穿过影线的假蜡烛。"""
    _seed(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?days=120").json()
    for column in ("open", "high", "low", "close"):
        assert payload[column], f"{column} 该有数"
        assert len(payload[column]) == len(payload["dates"])
    last = -1
    assert payload["high"][last] >= max(payload["open"][last], payload["close"][last])
    assert payload["low"][last] <= min(payload["open"][last], payload["close"][last])
    assert payload["close"] == payload["close_hfq"], "图上那一档收价就是后复权那一档"


def test_bars_api_5d_resamples_from_the_daily_bars(logged):
    """5 日线吃的是库里的日线，不新增数据源（D160）：三根日线 ⇒ 一根没走完的 5 日 K。

    这里点 `5d` 而不是 `week`：`_seed` 写的是「今天 -6/-5/-4 天」这连续三日，周五和周六
    那两天它会跨进两个自然周，`period=week` 的断言就会跟着跑测试的那天星期几漂移。
    N 日线按根数分组，与星期几无关，因此能钉死。
    """
    _seed(logged, code="600520")
    payload = logged.client.get("/api/symbol/A/600520/bars?days=120&period=5d").json()
    assert payload["bars"] == [3]
    assert payload["pending"], "只盖了三根的 5 日 K 不许冒充走完的那一根"


def test_bars_api_period_week_groups_the_same_daily_bars(logged):
    """周线那一档也要证明它只是聚合：几根 K 线加起来必须正好是三根日线。"""
    _seed(logged, code="600521")
    payload = logged.client.get("/api/symbol/A/600521/bars?days=120&period=week").json()
    assert sum(payload["bars"]) == 3


def test_bars_api_rejects_an_unknown_period_with_400(logged):
    _seed(logged)
    assert logged.client.get("/api/symbol/A/600519/bars?period=weekly").status_code == 400


def test_bars_api_rejects_an_unknown_indicator_with_400_and_names_the_options(logged):
    """拼错一个指标名不该让人去看 500 的空白页：分析层那句「可选的是 …」要上屏（D162）。"""
    _seed(logged)
    resp = logged.client.get("/api/symbol/A/600519/bars?sub=macdd")
    assert resp.status_code == 400
    assert "可选的是" in resp.text
```

- [ ] **Step 2: 跑到红**

Run: `uv run pytest tests/web/test_symbol.py -q -k "candle_prices or resamples or unknown_period or unknown_indicator"`
Expected: FAIL — 出参里没有 `open`/`high`/`low`（红在「接口还没给」）

- [ ] **Step 3: 实现**

`bars_api` 的新形状（**OHLC 从 `read.series.frame` 取**，不放宽 `services/analysis.py` 里那张给终端的 `keep` 白名单——图的出口是序列本身）：

```python
#: 主图与副图各自能挂哪些指标。这份名单同时是屏幕上那排开关的来源，
#: 加一种指标只改这里（前端读局部给出的名字，不自己记一份）
MAIN_INDICATORS: tuple[str, ...] = ("ma", "boll", "ema")
SUB_INDICATORS: tuple[str, ...] = (
    "vol", "macd", "kdj", "rsi", "atr", "wr", "dmi", "cci", "obv",
)
#: 每档周期的默认窗口（日历日）：周线月线要看的是「这几年」，日线看的是「这半年」（D160）
DEFAULT_WINDOW: dict[str, int] = {
    "day": 180,
    "week": 730,
    "month": 1825,
    "quarter": 2000,
    "year": 2000,
    # N 日线要看的是「这些年」：一根 5 日 K 只盖五个工作日，180 天窗口只剩三十来根，
    # 形态读不出来（同一档窗口在季/年那一行也是为这个才给到上限的）
    "5d": 2000,
    "10d": 2000,
}
#: 副图最多三格：再多就不是看图而是刷屏（D162）
MAX_SUB_PANES = 3
#: `vol` 不是 `INDICATORS` 里的名字：那一格画的是裸量柱（量不复权，`indicators` 模块第 3 条）
_VOLUME_COLUMN = "volume"


def _split_names(raw: str, allowed: tuple[str, ...], *, limit: int | None = None) -> tuple[str, ...]:
    """把 `ma,boll` 这样的串切成白名单里的名字：重复的合并，不在名单里的当场拒。

    拒的那句要报出可选的是哪些：屏幕上打错一个字母的人不该只看到「400」。
    """
    given = [item.strip().lower() for item in (raw or "").split(",")]
    picked = tuple(dict.fromkeys(item for item in given if item))
    unknown = [item for item in picked if item not in allowed]
    if unknown:
        raise ValidationError(
            f"不认识的指标 {', '.join(unknown)}：可选的是 {', '.join(allowed)}"
        )
    if limit is not None and len(picked) > limit:
        raise ValidationError(f"副图最多同时 {limit} 个，收到的是 {len(picked)} 个")
    return picked
```

`bars_api` 主体（`period`、`main`、`sub`、`ma` 四个新参数）：

```python
@router.get("/api/symbol/{market}/{code}/bars")
def bars_api(
    request: Request,
    market: str,
    code: str,
    days: int = 0,
    period: str = "day",
    main: str = "ma",
    sub: str = "vol",
    ma: str = "",
):
    user = require_user(request)
    web = request.app.state.web
    ctx, market, code, _symbol = _require_symbol(request, user, market, code)
    mains = _split_names(main, MAIN_INDICATORS)
    subs = _split_names(sub, SUB_INDICATORS, limit=MAX_SUB_PANES)
    windows = {"ma": _windows(ma)} if "ma" in mains else None
    end = web.core.clock.now_utc().date()
    window = days or DEFAULT_WINDOW.get(period, DEFAULT_WINDOW["day"])
    start = end - timedelta(days=max(1, min(window, 2000)))
    asked = tuple(name for name in (*mains, *[s for s in subs if s != "vol"]))
    try:
        read = ctx.analysis.series(
            market, code, start=start, end=end, period=period,
            indicators=asked or ("ma",), windows=windows,
        )
    except (NotAdjustable,) as exc:
        return JSONResponse({...既有的空形状，多带 "period": period...})
    frame = read.series.frame.filter(pl.col("date") >= start)
    table = read.table
    ...
```

（`asked` 为空时仍要问 `("ma",)` 一次：`metrics(series, ())` 会给一张只有 `date` 的表，
而我们要的那张表本来就有列，这条路不该把人带进 `hstack` 的空表分支。）

出参（列名 = 指标层给的那些：`ma5/ma20/…`、`boll_mid/boll_up/boll_low`、`macd/macd_signal/macd_hist`、
`kdj_k/kdj_d/kdj_j`、`rsi6/rsi14`、`atr`、`wr`、`pdi/mdi/adx`、`cci`、`obv`）：

```python
    def column(name: str) -> list:
        return [None if value is None else value for value in frame[name].to_list()]

    payload = {
        "dates": [str(value) for value in table["date"].to_list()],
        "period": read.period,
        # 四档一律后复权：均线与副图指标也算在同一列上（D159 那条同基规矩的延长线）
        "open": column("open_hfq"),
        "high": column("high_hfq"),
        "low": column("low_hfq"),
        "close": column("close_hfq"),
        "close_hfq": column("close_hfq"),
        "volume": column(_VOLUME_COLUMN) if _VOLUME_COLUMN in frame.columns else [],
        "main": {name: _group(table, name) for name in mains},
        "sub": {name: _group(table, name) for name in subs if name != "vol"},
        "bars": table["bars"].to_list() if "bars" in table.columns else [],
        "pending": [str(value) for value in read.pending_periods],
        # 口径行（D168）：锚与预热被截断的那一格，屏幕上第一次用到这两个字段
        "anchored_on": str(read.anchored_on),
        "warmup_floor": str(read.warmup_floor) if read.warmup_floor else None,
        "note": None,
    }
```

`_group(table, name)`：把该指标族的那几列挑出来（族名 → 列名前缀的映射写成一个模块常量，
`{"ma": "ma", "boll": "boll_", "ema": "ema", "macd": "macd", "kdj": "kdj_", "rsi": "rsi", "atr": "atr", "wr": "wr", "dmi": "", "cci": "cci", "obv": "obv"}`——
`dmi` 的三列是 `pdi/mdi/adx`，前缀不是族名，这一处单独列出来，别用 `startswith` 蒙）。

`_windows(raw)`：`"5,10,20,60"` → `(5,10,20,60)`；空串 → `()`（用默认那组）；
非正整数或超过 6 个窗口 ⇒ `ValidationError("均线窗口要 1..500 之间的正整数，最多六条")`。

- [ ] **Step 4: 跑到绿**

Run: `uv run pytest tests/web/test_symbol.py -q`
Expected: 全绿。`test_bars_api_plots_the_close_on_the_basis_the_mas_use` 那枚钉的是**口径**（图上收价与均线同基），不是 JSON 形状：出参改成 `main` 分组后只把取值路径换成 `payload["main"]["ma"]["ma20"]`，断言与文档字符串一字不动。`test_bars_api_returns_dates_and_close` 读的还是 `close_hfq`，不用改。

（另外两处形状必须一起改到：`NotAdjustable` 那条空响应现在要带上 `open/high/low/close/bars/main/sub/period`，前端在空态也要能拿到同形的 JSON；`asked` 为空时传 `("ma",)` 给 `series()`，避免走进 `metrics(series, ())` 那张只有 `date` 的空表。）

- [ ] **Step 5: 提交**

```bash
git add src/sams/web/pages/symbol.py tests/web/test_symbol.py
git commit -m "feat(web): bars 接口交出蜡烛四档后复权价、周期与多指标（D159/D160/D162/D168）"
```

---

### Task 5: chart 局部——周期条、指标开关、口径行

**Files:**
- Create: `src/sams/web/templates/partials/chart.html`
- Modify: `src/sams/web/pages/symbol.py`（局部路由与两张常量表都放这里）
- Modify: `src/sams/web/templates/symbol.html:12-13`
- Modify: `src/sams/web/static/app.css`
- Test: `tests/web/test_symbol.py`

（路由为什么落在 `symbol.py` 而不是 `dashboard.py`：`symbol.py` 已经 `from sams.web.pages.dashboard import _market_open`，
反过来 import `MAIN_INDICATORS`/`SUB_INDICATORS` 会成环。局部与它读的白名单、窗口表同处，
加一种指标仍只改一个文件。）

- [ ] **Step 1: 写失败测试**

```python
def test_chart_partial_marks_the_current_period(logged):
    """当前档必须看得出来（D158）：换档之后旧档不再带 `aria-current`。
    钉属性而不是钉整段文本——文案改了这枚用例不该跟着红。"""
    _seed(logged)
    week = logged.client.get("/api/partials/chart?market=A&code=600519&period=week")
    assert week.status_code == 200
    assert 'aria-current="true"' in week.text
    assert 'data-chart-url="/api/symbol/A/600519/bars?period=week' in week.text
    day = logged.client.get("/api/partials/chart?market=A&code=600519&period=day")
    assert day.text.count('aria-current="true"') == 1


def test_chart_partial_lists_the_indicator_switches_from_the_server_names(logged):
    """开关上的名字来自服务端那份白名单（D162）：前端不自己记一份，
    否则分析层加一个指标，屏幕上永远少一个。"""
    _seed(logged)
    text = logged.client.get("/api/partials/chart?market=A&code=600519").text
    for name in ("ma", "boll", "macd", "kdj", "rsi", "vol"):
        assert f"data-indicator=\"{name}\"" in text


def test_symbol_page_uses_the_chart_partial(logged):
    _seed(logged)
    page = logged.client.get("/symbol/A/600519")
    assert "/api/partials/chart?market=A&amp;code=600519" in page.text or "api/partials/chart" in page.text


def test_chart_partial_requires_a_login(web_app):
    """未登录拿不到这一块：路由第一行就 `require_user`，别让它先去摸数据库。
    钉 303 与跳转目的地，不钉 401——本站的未登录姿势是重定向去登录页（`app.py:207`）。"""
    response = web_app.client.get("/api/partials/chart?market=A&code=600519", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
```

- [ ] **Step 2: 跑到红**

Run: `uv run pytest tests/web/test_symbol.py -q -k chart`
Expected: FAIL —404（局部路由还不存在）

- [ ] **Step 3: 实现**

`src/sams/web/pages/symbol.py`（路由与它读的白名单、窗口表、标签表同处；`web.render` 的姿势照 `dashboard.py` 的 `quotes_partial`）：

```python
@router.get("/api/partials/chart")
def chart_partial(
    request: Request, market: str, code: str, period: str = "day",
    main: str = "ma", sub: str = "vol", ma: str = "",
):
    """个股页那一块图：周期条 + 指标开关 + 挂着 `data-chart-url` 的那个 div。

    为什么在服务端渲染而不是前端自己切：CSP 是 `'self'`（不许内联脚本），
    而「现在选中的是哪一档」这份状态一旦落在前端，换档后屏上说的就可能是上一档的话（D158）。

    周期名要过 `parse_period`：不认识的名字在这里就说 400，而不是悄悄换成日线——
    那一屏会举着「日」的高亮画一段没人请求过的东西，而 `bars` 接口对同一个写法已经是 400。
    """
    require_user(request)
    web = request.app.state.web
    kind, _n = parse_period(period)  # 只为止声，选中态仍存调用方给的那一串
    ctx = web.contexts.current()
    market = canonical_market(market)
    code = canonical_code(market, code)
    if market not in ctx.settings.markets:
        raise HTTPException(status_code=404)
    return web.render(
        request,
        "partials/chart.html",
        market=market,
        code=code,
        periods=CHART_PERIODS,
        period=period,
        main=main,
        sub=sub,
        ma=ma,
        mains=MAIN_INDICATORS,
        subs=SUB_INDICATORS,
        labels=CHART_INDICATOR_LABELS,
    )
```

`CHART_PERIODS` 与 `CHART_INDICATOR_LABELS`（都放 `symbol.py`，值与标签同处，别在模板里写死）：

```python
#: 周期条：(参数值, 屏幕上那几个字)。分钟档在计划 B，那时再来这张表里加行
CHART_PERIODS: tuple[tuple[str, str], ...] = (
    ("day", "日"), ("week", "周"), ("month", "月"), ("quarter", "季"),
    ("year", "年"), ("5d", "5日"), ("10d", "10日"),
)
#: 开关上那几个字。白名单加了名字这里就必须加一行，缺行会在模板上留一个空按钮
CHART_INDICATOR_LABELS: dict[str, str] = {
    "ma": "均线", "boll": "布林", "ema": "指数均线",
    "vol": "成交量", "macd": "MACD", "kdj": "KDJ", "rsi": "RSI", "atr": "ATR",
    "wr": "威廉", "dmi": "动向", "cci": "CCI", "obv": "OBV",
}
```

（`CHART_PERIODS` 里的值必须与 `bars_api` 的 `DEFAULT_WINDOW` 那七个键一致——这一步不自动校验，
`tests/web/test_symbol.py` 里那条周期用例走的是真实接口，加了档忘了配窗口会当场露出来。）

（要补的 import 只有一行：`from sams.services.analysis import parse_period`。`MAIN_INDICATORS`、
`SUB_INDICATORS`、`DEFAULT_WINDOW` 是 Task 4 就落在 `symbol.py` 的常量，局部路由读同一份，
屏幕上能点的档位与接口肯收的档位因此永远是同一份。）

`partials/chart.html`（选择器是 `<a hx-get>` 换整块；`aria-current` 给当前档；
「在就摘掉、不在就加上」用 Jinja 自带的 `difference` 算，不新增只在模板外面用的 Python helper）：

```html
{% set picked_main = main.split(",") %}
{% set picked_sub = sub.split(",") %}
{% set base = "/api/partials/chart?market=" ~ market ~ "&code=" ~ code %}
<div class="chart-box">
  <div class="chart-bar">
    <span class="chart-group" role="group" aria-label="周期">
      {% for value, label in periods %}
      <a hx-get="{{ base }}&period={{ value }}&main={{ main }}&sub={{ sub }}&ma={{ ma }}"
         hx-target=".chart-box" hx-swap="outerHTML" href="?period={{ value }}"
         {% if value == period %}aria-current="true" class="on"{% endif %}>{{ label }}</a>
      {% endfor %}
    </span>
    <span class="chart-group" role="group" aria-label="主图指标">
      {% for name in mains %}
      {% set on = name in picked_main %}
      {% set next_main = ((picked_main | difference([name])) if on else (picked_main + [name])) | join(",") %}
      <a data-indicator="{{ name }}" hx-get="{{ base }}&period={{ period }}&main={{ next_main }}&sub={{ sub }}&ma={{ ma }}"
         hx-target=".chart-box" hx-swap="outerHTML" href="#"
         {% if on %}aria-current="true" class="on"{% endif %}>{{ labels[name] }}</a>
      {% endfor %}
    </span>
    <span class="chart-group" role="group" aria-label="副图指标">
      {% for name in subs %}
      {% set on = name in picked_sub %}
      {% set next_sub = ((picked_sub | difference([name])) if on else (picked_sub + [name])) | join(",") %}
      <a data-indicator="{{ name }}" hx-get="{{ base }}&period={{ period }}&main={{ main }}&sub={{ next_sub }}&ma={{ ma }}"
         hx-target=".chart-box" hx-swap="outerHTML" href="#"
         {% if on %}aria-current="true" class="on"{% endif %}>{{ labels[name] }}</a>
      {% endfor %}
    </span>
  </div>
  <div id="chart" style="width:100%;height:480px" data-chart-kind="kline"
       data-chart-period="{{ period }}"
       data-chart-url="/api/symbol/{{ market }}/{{ code }}/bars?period={{ period }}&main={{ main }}&sub={{ sub }}&ma={{ ma }}"></div>
  <p class="muted chart-caption">后复权 · 均线算在同一基上；最后一根若没走完会在图注里说</p>
</div>
```

（`difference` 是 Jinja 自带 filter，结果按左边那份的顺序。副图超过三个不必在模板里拦：
接口那侧 400，htmx 会把错误页换进来，不会静默。`labels` 缺一个名字会在模板上留一个空按钮，
所以加指标时白名单与标签表同处改。）

`symbol.html` 的 `:12-13` 那两行换成：

```html
<div hx-get="/api/partials/chart?market={{ market }}&code={{ code }}" hx-trigger="load"
     hx-swap="outerHTML"><p class="muted">图加载中…</p></div>
```

`app.css` 补三条：`.chart-bar` flex+wrap、`.chart-group` gap、`a.on`（或 `[aria-current="true"]`）
加底色与描边——当前档要一眼看出。

- [ ] **Step 4: 跑到绿**

Run: `uv run pytest tests/web/test_symbol.py tests/web/test_dashboard.py -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add src/sams/web/templates/partials/chart.html src/sams/web/pages/symbol.py src/sams/web/templates/symbol.html src/sams/web/static/app.css tests/web/test_symbol.py
git commit -m "feat(web): 个股图换成服务端渲染的 chart 局部——周期条与指标开关（D158/D162）"
```

---

### Task 6: 前端画蜡烛与多格副图

**Files:**
- Modify: `src/sams/web/static/app.js`

这一步没有 JS 单测可用（站内没有前端测试夹具），验收是**跑起来看**：
`uv run sams serve` 之后开 `http://127.0.0.1:8765/symbol/A/000001`，逐档点周期与指标。

- [ ] **Step 1: 实现 `samsChart` 的 kline 路径**

要点（全部落在 `app.js` 一处，CSP 不许内联）：

```javascript
  // 红涨绿跌：与 app.css 的 .up=--danger / .down=--ok 同一对颜色
  var UP = "#b42318", DOWN = "#067647";

  function candleSeries(payload) {
    var rows = payload.dates.map(function (d, i) {
      return [payload.open[i], payload.close[i], payload.low[i], payload.high[i]];
    });
    return { name: "K线", type: "candlestick", data: rows,
             itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN } };
  }
```

（ECharts 的 `candlestick` 数据顺序是 **[open, close, low, high]**，不是 OHLC——写反会得到
一根实体与影线互相穿过的假蜡烛。）

网格与轴按「1 个主图 + N 个副图」动态生成：`grids`、`xAxis`（同一份 `dates`，
`axisLabel` 只在最后一格显示）、`yAxis`（主图 `scale:true`，副图各自 `scale:true`）、
`dataZoom: [{type:"inside"}, {type:"slider"}]` 带 `xAxisIndex: 全部`。
副图那一族的画法：`vol` ⇒ 柱（按该根涨跌上色）；`macd` ⇒ `macd_hist` 柱 + `macd`/`macd_signal` 两条线；
`kdj`/`rsi`/`dmi` ⇒ 各自那几条线；`atr`/`wr`/`cci`/`obv` ⇒ 单线。

- [ ] **Step 2: pending 与口径行**

拿到 `payload.pending` 非空时，把 `payload.dates` 里那几个（最后一根）在图注里点名：
「最后一根 {period} 只走到 {dates[-1]}」。`payload.anchored_on` 与 `warmup_floor` 拼进
`chart-caption` 那一行（D168）。`payload.note` 有话时仍走既有那条插入 `<p>` 的路。

- [ ] **Step 3: 跑起来看**

```bash
pkill -f "sams serve" || true
SAMS_SECRET_KEY="$(cat tmp/.sams_secret)" nohup uv run sams serve > tmp/serve.log 2>&1 &
sleep 6; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/login
```
浏览器（或 `run`/`verify` skill）逐档验：日/周/月/季/年/5日/10日 六档都有蜡烛、
`ma`+`boll` 同开时主图两条族都在、副图开 `macd,kdj,vol` 时三格都在且能拖动缩放；
`period=5d` 那一档最后一根若不足 5 根，图注要说它只走了几天。**看到的不等于代码写的：
这一步不许跳过。**

- [ ] **Step 4: 提交**

```bash
git add src/sams/web/static/app.js
git commit -m "feat(web): 个股图画标准蜡烛 + 主图多族与最多三格副图（D159/D162）"
```

---

### Task 7: 自选行内移除（D166）

**Files:**
- Modify: `src/sams/web/templates/partials/quotes.html`、`src/sams/web/templates/watchlist.html`、`src/sams/web/pages/dashboard.py`（`quotes_partial`）
- Test: `tests/web/test_dashboard.py`

- [ ] **Step 1: 写失败测试**

```python
def test_watchlist_page_carries_the_remove_button_on_the_row_itself(logged):
    """移除在那一行最右侧，不再另开一张表（D166）：
    同一只票在同一屏上出现两遍的日子到此为止。"""
    _add_watch(logged, "A", "600519")
    page = logged.client.get("/watchlist")
    assert page.text.count("/watchlist/remove") == 1
    assert "600519" in page.text


def test_watchlist_remove_survives_the_ten_second_refresh(logged):
    """局部刷新会把 `#quote-board` 整块换掉：移除表单必须活过这一次替换，
    所以 `manage` 是那个 GET 的参数，不是只写在页面里的一段静态表格。"""
    _add_watch(logged, "A", "600519")
    partial = logged.client.get("/api/partials/quotes?manage=1")
    assert "/watchlist/remove" in partial.text


def test_overview_board_has_no_remove_buttons(logged):
    """看板那一屏不带 `manage`：看板上摆一排删除按钮是要人误点的（D166）。"""
    _add_watch(logged, "A", "600519")
    assert "/watchlist/remove" not in logged.client.get("/api/partials/quotes").text
    assert "/watchlist/remove" not in logged.client.get("/").text


def test_watchlist_page_shows_the_note_inline(logged):
    """备注并进名称那一列：删掉那张管理表不能顺手把「为什么加这只票」删了。"""
    _add_watch(logged, "A", "600519", note="拿长线")
    assert "拿长线" in logged.client.get("/watchlist").text
```

（`_add_watch` 这个夹具函数若 `tests/web/test_dashboard.py` 里已有同名/同义的加自选 helper，
沿用它，别再造一份；没有就按该文件既有的 `logged.client.post("/watchlist/add", ...)` 姿势写一个。）

- [ ] **Step 2: 跑到红**

Run: `uv run pytest tests/web/test_dashboard.py -q -k "remove or note_inline"`
Expected: FAIL —`count("/watchlist/remove") == 2`（页面既有板下表 + 表单）或 0（局部里没有）

- [ ] **Step 3: 实现**

`quotes_partial` 收 `manage`：

```python
@router.get("/api/partials/quotes")
def quotes_partial(request: Request, manage: int = 0):
    ...
    return web.render(
        request,
        "partials/quotes.html",
        rows=_quote_rows(ctx, web.core, user.id, now),
        manage=bool(manage),
    )
```

`partials/quotes.html`：`manage` 时表头多一格 `<th></th>`，每行末尾多一列移除表单，名称列带备注：

```html
    <td>{{ row.name }}{% if manage and row.note %} <span class="muted">{{ row.note }}</span>{% endif %}</td>
    ...
    {% if manage %}
    <td class="num">
      <form method="post" action="/watchlist/remove" class="inline-form">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <input type="hidden" name="market" value="{{ row.market }}">
        <input type="hidden" name="code" value="{{ row.code }}">
        <button type="submit" class="link">移除</button>
      </form>
    </td>
    {% endif %}
```

`watchlist.html`：`hx-get="/api/partials/quotes"` 改成 `...?manage=1`（`hx-trigger` 那串不动），
第 22–41 行那张 `{% if rows %}<table>…</table>{% endif %}` **整块删掉**（加票表单与候选面板留着）。
`/watchlist` 页面本身的 `rows` 若只为那张表而传，一并从 `_watch_page` 的 render 参数里去掉——
留着没人读的那一份查询是白付的。

- [ ] **Step 4: 跑到绿**

Run: `uv run pytest tests/web -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add src/sams/web/templates/partials/quotes.html src/sams/web/templates/watchlist.html src/sams/web/pages/dashboard.py tests/web/test_dashboard.py
git commit -m "feat(web): 自选的移除回到那一行最右侧，重复的管理表删掉（D166）"
```

---

### Task 7b: 移除改成「折起」，数据与历史照旧（D169）

Task 7 把移除的**位置**搬到了那一行最右边；这一节改的是它的**语义**。查过的事实：
`WatchlistRepo.remove()` 是硬 `DELETE`，而 `services/scheduler/universe.py:73` 每天采哪些票
读的就是「自选 + 持仓 + 启用规则 + 论点」，所以砍掉一行等于同时砍掉这只票的后续更新。
备注、批注、论点、规则、告警都不该跟着一次误点走。

**Files:**
- Create: `src/sams/storage/migrations/0022_watchlist_hidden.sql`
- Modify: `src/sams/storage/repos/watchlists.py`、`src/sams/web/pages/dashboard.py`
- Modify: `src/sams/web/templates/watchlist.html`
- Test: `tests/storage/test_watchlists.py`、`tests/storage/test_migration_0022.py`（新建，照 `test_migration_0020.py` 的形状）、`tests/web/test_dashboard.py`

- [ ] **Step 1: 写失败测试（仓库层的四种说法）**

追加到 `tests/storage/test_watchlists.py`（沿用该文件的 `repo` 夹具与 `A_ID`/`B_ID` 常量）：

```python
def test_remove_hides_the_row_but_keeps_it_for_the_sync_universe(repo):
    """折起只影响「看得到」，不影响「还要不要采」：`list_all()` 是采集清单的来源，
    它必须继续看见这一行，否则恢复那天起的历史就断了（D169）。"""
    repo.add(A_ID, "A", "600519", note="白酒龙头")
    assert repo.remove(A_ID, "A", "600519") is True
    assert repo.list(A_ID) == []
    assert repo.codes(A_ID) == []
    assert {(i.user_id, i.code) for i in repo.list_all()} == {(A_ID, "600519")}
    assert {(i.user_id, i.code) for i in repo.hidden(A_ID)} == {(A_ID, "600519")}


def test_restore_puts_it_back_with_the_note_it_had(repo):
    repo.add(A_ID, "A", "600519", note="拿长线")
    repo.remove(A_ID, "A", "600519")
    assert repo.restore(A_ID, "A", "600519") is True
    assert repo.restore(A_ID, "A", "600519") is False, "没折起的行不叫恢复成功"
    assert repo.list(A_ID)[0].note == "拿长线"


def test_purge_really_deletes_the_row(repo):
    """`purge` 才是那一刀：折起的行继续占采集配额没意义，人明确要求删就得真删。"""
    repo.add(A_ID, "A", "600519")
    repo.remove(A_ID, "A", "600519")
    assert repo.purge(A_ID, "A", "600519") is True
    assert repo.purge(A_ID, "A", "600519") is False
    assert repo.list_all() == []


def test_adding_a_hidden_symbol_brings_it_back_with_the_new_note(repo):
    """重新加一只折起的票 ⇒ 回到看板并换备注，且不产生第二行（`INSERT OR REPLACE` 顺带把
    `removed_at` 抹回 NULL）。"""
    repo.add(A_ID, "A", "600519", note="旧备注")
    repo.remove(A_ID, "A", "600519")
    repo.add(A_ID, "A", "600519", note="新备注")
    assert repo.count(A_ID) == 1
    assert [(i.code, i.note) for i in repo.list(A_ID)] == [("600519", "新备注")]
    assert repo.hidden(A_ID) == []
```

Run: `uv run pytest tests/storage/test_watchlists.py -q` → 前四枚 FAIL（`hidden`/`restore`/`purge` 不存在，`remove` 之后 `list_all()` 空）。

- [ ] **Step 2: 迁移 + 仓库实现**

`src/sams/storage/migrations/0022_watchlist_hidden.sql`：

```sql
-- 0022：自选的「移除」改成折起（D169）。一列时间戳，只写不读回 Python
ALTER TABLE watchlists ADD COLUMN removed_at TIMESTAMPTZ;
```

`watchlists.py`：`remove()` 的 SQL 换成带条件的 UPDATE。判据先看「这一行原来可见吗」——
`contains()` 只认未折起的行，所以它天然就是那个答案，不必先 `SELECT` 再 `UPDATE`：

```python
    def remove(self, user_id: str, market: str, code: str) -> bool:
        """折起：从看板与规则展开里消失，但仍在 `list_all()`（= 每日采集清单）里（D169）。

        返回值的含义与硬删除时代一致：「这一行原来是可见的」。第二次折同一只票
        返回 False——它本来就折着，没有发生任何变化，界面不该报「刚刚移掉了」。
        """
        market, code = _norm(market, code)
        user_id = _require_user(user_id)
        if not self.contains(user_id, market, code):
            return False
        self.db.execute(
            "UPDATE watchlists SET removed_at = current_timestamp "
            "WHERE user_id = ? AND market = ? AND code = ?",
            [user_id, market, code],
        )
        return True
```

新增三个方法（`list()` 与 `contains()` 的 SQL 都加 `AND removed_at IS NULL`；`codes()` 走 `list()`
所以自动跟着变；`count()` **不加**——上限数的是「这只票在不在我的清单里」，折起来不腾出配额；
`list_all()` **一条 WHERE 都不加**，它就是采集清单的来源）：

```python
    def hidden(self, user_id: str) -> list[WatchItem]:
        """折起的那几行：只给自选页底部那一栏用，看板与采集都不读它。"""

    def restore(self, user_id: str, market: str, code: str) -> bool:
        """`removed_at = NULL`。折起来的东西必须能一句话放回去，这是这一条存在的理由。"""

    def purge(self, user_id: str, market: str, code: str) -> bool:
        """真删。只有确认屏那一步会走到它（D169）。"""
```

`WatchItem` 不加 `removed_at` 字段（屏幕上不需要那个时刻，加了就要把 TIMESTAMPTZ 取回 Python）。

Run: `uv run pytest tests/storage/test_watchlists.py tests/storage/test_migrations.py -q` → 全绿。
**别忘了既有的 `test_add_list_remove`**：它断言「移除两次 ⇒ 第二次是 False」，软删之后仍然成立；
`test_admin_can_read_everything_but_not_write` 断言 `list_all()` 看见全部——这正是这一条要的形状。

- [ ] **Step 3: 写失败测试（屏幕上那一栏与两个按钮）**

追加到 `tests/web/test_dashboard.py`（`_add_watch` 由 Task 7 建好，沿用）：

```python
def test_removed_symbol_moves_to_the_hidden_section(logged):
    """折起之后它不该在行情板里，但要在页底那一栏里带着备注露出来（D169）。"""
    _add_watch(logged, "A", "600519", note="白酒龙头")
    logged.client.post("/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"})
    page = logged.client.get("/watchlist").text
    assert "已移除" in page and "白酒龙头" in page
    assert "/watchlist/restore" in page


def test_restore_puts_the_symbol_back_on_the_board(logged):
    _add_watch(logged, "A", "600519")
    logged.client.post("/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"})
    resp = logged.client.post(
        "/watchlist/restore", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    assert resp.status_code == 303
    assert "600519" in logged.client.get("/watchlist").text


def test_purge_asks_first_and_then_really_goes(logged):
    """真删之前要有一屏确认：那一刀会连采集清单一起放手，值得多点一下（D169）。"""
    _add_watch(logged, "A", "600519")
    logged.client.post("/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"})
    confirm = logged.client.get("/watchlist/A/600519/purge")
    assert confirm.status_code == 200
    assert "彻底删除" in confirm.text
    assert "/watchlist/remove" not in confirm.text, "确认屏上不许再有第二个删除入口"
    logged.client.post("/watchlist/purge", data={"csrf": logged.csrf, "market": "A", "code": "600519"})
    assert "600519" not in logged.client.get("/watchlist").text
    assert logged.client.get("/watchlist/A/600519/purge").status_code == 404


def test_hidden_symbol_leaves_the_quote_board(logged):
    """看板（首屏与 10 秒那一刷）都不该再看见折起的行：`manage` 只管要不要按钮。
    「采集清单继续包含它」那一半已经由 Step 1 的 `list_all()` 用例钉住，屏幕上这一半
    只需要钉住反向：不在板上。"""
    _add_watch(logged, "A", "600519")
    logged.client.post("/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"})
    assert "600519" not in logged.client.get("/api/partials/quotes?manage=1").text
    assert "600519" not in logged.client.get("/").text
```

Run: `uv run pytest tests/web/test_dashboard.py -q -k "hidden or restore or purge"` → FAIL。

- [ ] **Step 4: 屏幕与端点**

`dashboard.py` 三个新端点（都 `require_user` + `csrf_protect`，成功一律 303 回 `/watchlist`）：

```python
@router.post("/watchlist/restore")   # market/code 走 Form，仓库 restore()
@router.get("/watchlist/{market}/{code}/purge")  # 确认屏：`_watch_page` 之外的一个小模板
@router.post("/watchlist/purge")     # market/code 走 Form，仓库 purge()
```

确认屏那一个模板 `watchlist_purge.html` 只说三件事：这只票是谁（代码 + 名称）、
它的备注、以及「彻底删除之后：它离开采集清单，本地已存的日线与批注论点都不动」。
**页面上只放那一个表单**（`action="/watchlist/purge"`），别再摆第二个删除入口。
进不去的票（没折起的、库里没有的）给 404，不给一张空确认屏。

`watchlist.html`：Task 7 删掉那张管理表的位置换成「已移除（N）」那一栏，`{% if hidden %}` 起头，
每行给「恢复」表单 + 一个 `<a href="/watchlist/{{ row.market }}/{{ row.code }}/purge">彻底删除</a>`。
`_watch_page` 多传一份 `hidden=ctx.watchlists.hidden(user.id)`。

`partials/quotes.html` 不带 `hidden` 那一栏：它是 10 秒一刷的行情板，把恢复按钮放进去
会让人以为「已移除」也在实时刷新。

- [ ] **Step 5: 跑到绿 + 提交**

Run: `uv run pytest tests/storage tests/web -q`、`uv run ruff check --no-cache src/sams tests`

```bash
git add src/sams/storage/migrations/0022_watchlist_hidden.sql src/sams/storage/repos/watchlists.py \
  src/sams/web/pages/dashboard.py src/sams/web/templates/watchlist.html \
  src/sams/web/templates/watchlist_purge.html \
  tests/storage/test_watchlists.py tests/web/test_dashboard.py
git commit -m "feat(web): 自选的移除改成折起——备注、批注与每日采集都留下，恢复一句话（D169）"
```

---

### Task 8: 全量回归 + 实测 + 文档收尾

- [ ] **Step 1:** `uv run ruff check --no-cache src/sams tests` → All checks passed
- [ ] **Step 2:** 全量套件（控制者跑，不加管道，约 17 分钟）：`uv run pytest -q`
- [ ] **Step 3:** 起服务实测（同 Task 6 Step 3 的命令），拿平安银行 `A/000001` 逐档走完：
  六档周期 × 主图 `ma,boll` 同开 × 副图三格；自选项点一次「移除」确认真的那一行没了；
  看板那一屏确认没有移除按钮。
- [ ] **Step 4:** 规格回写：`docs/superpowers/specs/2026-10-27-chart-...md` 里 D160/D162 若与本计划
  实现出来的名字有差（例如 `sub` 里 `vol` 的特殊地位），以代码为准改文档那一段，不改代码迁就文档。
- [ ] **Step 5:** `git add docs/superpowers/specs/... && git commit -m "docs(specs): 计划 A 落地的名字与实现对齐"`

---

## Self-review 记录

- **Spec 覆盖**：D157 已单独落地（提交 `4732396`）；D158→T5、D159→T4/T6、D160→T3/T4、
  D161→T1/T2/T3、D162→T3/T4/T5/T6、D166→T7、D168→T4/T6。D163/D164/D165（分钟与分时）属计划 B，
  D167 是「不做」清单，而「自己设置」那半句（把均线窗口与周期条排序存成每人一份偏好）
  是 spec §7 里的计划 C——本计划有意不含：先让窗口串能从 URL 给进来，落库才有意义。
- **占位符**：Task 5 的 `hx-get` 里 `...&main={{ toggle(...) }}` 那一处省略号是给执行者的
  同形复制点（每条 `<a>` 的 query 串只是参数不同），不是 TBD；Task 6 因为是前端渲染，
  给了必须遵守的数据顺序与配色，具体 option 结构由实现按 ECharts 文档写。
- **名字一致**：`RESAMPLE_PERIODS`（分析层）/`PERIODS`+`parse_period`（服务层）/`CHART_PERIODS`
  （屏幕上那张表）三层各有其名且互不重复定义；`MAIN_INDICATORS`/`SUB_INDICATORS`/`MAX_SUB_PANES`
  只在 `symbol.py` 出现一次；`_split_names`/`_windows`/`_group`/`column` 是 `symbol.py` 内的私有 helper。
