# M4-A-2 盘中规则 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让九类规则在盘中轮询里用实时报价判（spec §5.1–§5.5）：报价过新鲜度闸 → 盖到日线上 → 引擎照旧走完过滤/冷却/投递，发送挪进独立的通知线程，写面全部接上版本闸。

**Architecture:** 不新造一张规则引擎——`RuleEngine.check` 加一个 `intraday+quotes` 入口，日线读数照旧（加一层 `SnapshotCache`），纯函数 `apply_quote` 把报价盖在日线上。发送拆成「落库（同步）+ 投递（通知线程）」，队列满就 abandon。每条规则写动作 bump `VersionGate`，缓存整表失效。

**Tech Stack:** Python 3.13、DuckDB、pytest、queue/threading（标准库）。

---

## 硬约束（每一条都在本仓库真实发生过）

1. 一切命令走 `uv run`，不用裸 `python`/`pytest`。
2. **永不 `ruff format`**。只有 `uv run ruff check . --no-cache`（行宽 100，规则 E,F,I,UP,B,SIM）。手写代码保持 100 列内。
3. 已应用的迁移文件不许修改。本计划**没有**新迁移（`alerts.is_test` 由 plan-1 的 0019 加好）。
4. 测试只用内存库（conftest 的 `db` 夹具）或 `tmp/`，绝不碰 `data/sams.duckdb`。
5. 本计划全部离线；不要写任何联网测试。
6. 每个 task 收尾：`git fetch origin main && git merge --ff-only origin/main && git push origin main`。不许 `--force`、不许 `--amend`。push 前 `git status` 过一眼；**绝不 `git add -A`**，逐个点名文件。工作区里可能躺着未追踪的 `tmp/`、`qodercli_resume.sh`、上一份计划文件——它们谁都不许进提交。

## 前置

- `docs/superpowers/plans/2026-10-23-m4a-1-runtime-quotes.md` 的 Task 1–16 必须已全部落地。本计划引用它的：`core/quotes.py`（`Quote`、`QuoteBatch`）、`services/quotes.py`（`QuoteBoard`、`QuoteService`、`QuotePollResult`）、`services/intraday.py`（`IntradayMonitor`、`MarketPoll`）、`core/version.py`（`VersionGate`）、迁移 0019（`alerts.is_test`、`Settings.quote_stale_s`）、`services/runtime.py`（`Runtime`）。
- spec：`docs/superpowers/specs/2026-10-23-m4a-intraday-dashboard-design.md` §5（§5.1 九类、§5.2 报价覆盖、§5.3 求值链、§5.4 缓存、§5.5 发送拆分）。
- 分层红线（`tests/test_layering.py`）：`monitor/` 只认 `core`+`storage`；`services/rules/*` 同层互认；`bootstrap.py` 是装配根。`monitor/queue.py` 因此不 import 任何 `services`。

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `src/sams/core/rules.py` | 改 | `Template.intraday` 标记 + `is_intraday()` |
| `src/sams/core/quotes.py` | 改 | `MARKET_ZONES`、`local_day`、`cross_baseline`、`source_label` |
| `src/sams/services/rules/snapshot.py` | 改 | `Snapshot.cross_from`、纯函数 `apply_quote` |
| `src/sams/services/rules/templates.py` | 改 | `_cross` 的另一侧从「前收盘」扩成「上一轮/前收盘」 |
| `src/sams/services/quotes.py` | 改 | `QuoteBoard.previous()` |
| `src/sams/storage/repos/rules.py` | 改 | `list_enabled()` |
| `src/sams/core/notify.py` | 改 | `AlertMessage` 盘中三件套 |
| `src/sams/storage/repos/alerts.py` | 改 | is_test 落列、报价元数据进 payload、`latest_open` 排除测试行 |
| `src/sams/services/health.py` | 改 | 健康积压排除测试告警 |
| `src/sams/services/rules/engine.py` | 改 | `check(intraday=…, quotes=…)`、新鲜度闸、缓存接线、报价正文 |
| `src/sams/services/rules/cache.py` | 新 | `SnapshotCache`（版本闸整表失效 + TTL 兜底） |
| `src/sams/services/rules/intraday.py` | 新 | `IntradayEvaluator.after_poll` |
| `src/sams/services/intraday.py` | 改 | `IntradayMonitor(on_poll=…)` |
| `src/sams/monitor/router.py` | 改 | `record` / `deliver_existing` / `abandon`，`dispatch` 变两步 |
| `src/sams/monitor/queue.py` | 新 | `AlertQueue`、`QueuedAlertSender`、`NotifyWorker`、`SystemAlarmRelay` |
| `src/sams/services/rules/maintenance.py`、`derive.py`、`theses.py`、`ledger.py`、`scheduler/types.py`、`scheduler/jobs.py` | 改 | 版本闸 bump |
| `src/sams/bootstrap.py` | 改 | 四上下文、队列、缓存、`NotifyWorker` 装配 |
| `src/sams/services/runtime.py` | 改 | 第四条线程 `notify` |

---

### Task 1: 九类盘中标记（`core/rules.py`）

**Files:**
- Modify: `src/sams/core/rules.py`（`Template` 数据类与 `TEMPLATES` 表）
- Test: `tests/core/test_rules_intraday.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/core/test_rules_intraday.py`：

```python
"""§5.1 的九个盘中 kind：名单钉在模板声明上，测试只认名单。

五条日线专属（stop.time 等）留在 False：盘中一趟不该去读打分与赔率——
那一头的输入（合成、复盘）只有收盘后才更新，盘中判它等于用昨天的分说今天的话。
"""

from __future__ import annotations

import pytest

from sams.core.rules import TEMPLATES, is_intraday

INTRADAY = {
    "stop.fixed",
    "stop.pct",
    "stop.trailing",
    "stop.atr",
    "stop.ma",
    "stop.breakeven",
    "tp.tiered",
    "price.cross_below",
    "price.cross_above",
}
DAILY = {
    "stop.time",
    "stop.thesis_drift",
    "thesis.odds_below",
    "thesis.drift",
    "scheduled.reevaluate",
}


def test_nine_kinds_are_intraday():
    assert {k for k, t in TEMPLATES.items() if t.intraday} == INTRADAY


def test_the_other_five_stay_daily_only():
    # 两枚测试合起来钉死整张表：多出第 15 个 kind 而没标，这里就会红
    assert {k for k, t in TEMPLATES.items() if not t.intraday} == DAILY


def test_is_intraday_answers_the_same_question():
    assert is_intraday("stop.fixed") is True
    assert is_intraday("stop.time") is False


def test_an_unknown_kind_is_a_keyerror_not_a_false():
    # 野 kind 在 `template_for` 也是炸：这里让它同样炸，False 会把「名字打错了」
    # 伪装成「这条不该盘中判」
    with pytest.raises(KeyError):
        is_intraday("stop.madeup")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/core/test_rules_intraday.py -q`
Expected: FAIL，`ImportError: cannot import name 'is_intraday'`

- [ ] **Step 3: 实现**

`src/sams/core/rules.py`：

1. `Template` 数据类在 `judged` 字段之后追加一个字段：

```python
    #: 这一条拿快照里**哪几列人填的价**做判断（§10.2）。`anchor.py` 靠它定「搬不动时
    #: 的后果」：拿它做判断 ⇒ 整条不判，只用于显示 ⇒ 屏幕上注明那一列可能偏高。
    #: 与 `needs_position` 不是一件事：`stop.thesis_drift` 要账户，看的是打分掉档
    judged: frozenset[str] = frozenset()
    #: 盘中轮询里判（§5.1）。放模板上而不是引擎里的名单：新 kind 想进盘中，
    #: 在声明处说了才算
    intraday: bool = False
```

（只加最后三行，`judged` 原样保留。）

2. `TEMPLATES` 里给这九个 kind 的 `Template(...)` 构造各加一行 `intraday=True,`（**最后一个关键字参数之后**；这九条里 `stop.fixed`、`stop.trailing`、`stop.ma`、`price.cross_below`、`price.cross_above` 没有 `judged`，别照着有 `judged` 的那条画瓢）：`stop.fixed`、`stop.pct`、`stop.trailing`、`stop.atr`、`stop.ma`、`stop.breakeven`、`tp.tiered`、`price.cross_below`、`price.cross_above`。例如 `stop.fixed` 那条改完后形如（原构造一行不动，只加最后一行）：

```python
    "stop.fixed": Template(
        "stop.fixed", "跌破绝对价即报",
        (_number("price", "止损价", required=True, minimum=0.0, unit="元"),),
        severity="strong", stages=True, targets=("symbol", "watchlist", "thesis", "position"),
        intraday=True,
    ),
```

其余五个 kind（`stop.time`、`stop.thesis_drift`、`thesis.odds_below`、`thesis.drift`、`scheduled.reevaluate`）的构造**一个字都不动**——`intraday` 默认 False。

3. `template_for` 函数之后加模块级函数：

```python
def is_intraday(kind: str) -> bool:
    """这个 kind 是否在盘中轮询里判（§5.1）。kind 必须在 `TEMPLATES` 里：
    野 kind 在 `template_for` 也会被拒，这里同样让它炸 KeyError 而不是悄悄 False。"""
    return TEMPLATES[kind].intraday
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/core/test_rules_intraday.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/core/rules.py tests/core/test_rules_intraday.py --no-cache
git status   # 确认只暂存下面点名的文件
git add src/sams/core/rules.py tests/core/test_rules_intraday.py
git commit -m "feat(rules): 九类盘中标记 intraday 与 is_intraday（§5.1）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 2: 报价的日期与来源词汇（`core/quotes.py`）

**Files:**
- Modify: `src/sams/core/quotes.py`（plan-1 Task 3 建的那份）
- Test: `tests/core/test_quotes_baseline.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/core/test_quotes_baseline.py`：

```python
"""cross_baseline 的三个分支与两个词汇函数：上一轮同日才算「上一根」。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.quotes import (
    MARKET_ZONES,
    Quote,
    cross_baseline,
    local_day,
    source_label,
)

ZONE = MARKET_ZONES["A"]


def quote(**over):
    base = {
        "market": "A",
        "code": "601088",
        "ts": datetime(2026, 10, 23, 3, 0, tzinfo=UTC),
        "price": 9.4,
        "source": "tencent",
    }
    base.update(over)
    return Quote(**base)


def test_same_day_previous_is_the_baseline():
    earlier = quote(ts=datetime(2026, 10, 23, 2, 0, tzinfo=UTC), price=9.95)
    assert cross_baseline(quote(), earlier, zone=ZONE) == 9.95


def test_yesterdays_round_is_not_today_first_jump():
    # 跨了日的那一轮是昨天的行情：拿它当「上一根」，今天第一次穿过就永远不会报
    yesterday = quote(ts=datetime(2026, 10, 22, 6, 0, tzinfo=UTC), price=9.95)
    assert cross_baseline(quote(), yesterday, zone=ZONE) is None


def test_no_previous_is_none():
    assert cross_baseline(quote(), None, zone=ZONE) is None


def test_local_day_uses_the_market_zone():
    # UTC 03:00 = 上海 11:00；UTC 19:00 = 上海次日 03:00——日子跟着市场走
    assert local_day(datetime(2026, 10, 23, 3, 0, tzinfo=UTC), ZONE).isoformat() == "2026-10-23"
    assert local_day(datetime(2026, 10, 23, 19, 0, tzinfo=UTC), ZONE).isoformat() == "2026-10-24"


def test_source_label_prefers_the_chinese_name():
    assert source_label("tencent") == "腾讯"
    assert source_label("sina_quote") == "新浪"
    assert source_label("mystery") == "mystery"
    assert source_label("") == "未知源"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/core/test_quotes_baseline.py -q`
Expected: FAIL，`ImportError: cannot import name 'MARKET_ZONES'`

- [ ] **Step 3: 实现**

`src/sams/core/quotes.py` 模块级追加（放在 `Quote` 定义之前均可；import 区缺什么补什么——需要 `from datetime import date` 与 `from zoneinfo import ZoneInfo`）：

```python
#: 三个市场各自的本地时区。core 不认识 `Settings`（分层红线），所以这里自己放一份：
#: 与 `MarketCfg.zone()` 同源，配置改了这里必须跟着改——`test_local_day_uses_the_market_zone`
#: 钉的就是这三行。
MARKET_ZONES: dict[str, ZoneInfo] = {
    "A": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}

#: 注册名 → 屏幕上的名字。认识的说人话，不认识的显示注册名本身
SOURCE_LABELS = {"tencent": "腾讯", "sina_quote": "新浪"}


def source_label(name: str | None) -> str:
    """告警正文那一段「· 腾讯」。空名是「未知源」，不是空串——那一行总得有个出处。"""
    if not name:
        return "未知源"
    return SOURCE_LABELS.get(name, name)


def local_day(ts: datetime, zone: ZoneInfo) -> date:
    """报价时刻在市场本地是哪一天。US 的收盘在北京是次日凌晨，日子必须按市场算。"""
    return ts.astimezone(zone).date()


def cross_baseline(current: Quote, previous: Quote | None, *, zone: ZoneInfo) -> float | None:
    """「穿」的另一侧价（§5.2）：**同一个市场本地日**的上一轮报价才有资格当「上一根」。

    跨了日（或根本没有上一轮）返回 None：`price.cross_*` 会落到「前收盘」那一侧，
    而不是拿昨天下午的价冒充今天的第一跳。
    """
    if previous is None:
        return None
    if local_day(previous.ts, zone) != local_day(current.ts, zone):
        return None
    return previous.price
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/core/test_quotes_baseline.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/core/quotes.py tests/core/test_quotes_baseline.py --no-cache
git add src/sams/core/quotes.py tests/core/test_quotes_baseline.py
git commit -m "feat(quotes): 市场时区、本地日与跨轮基准 cross_baseline"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 3: 报价盖到日线上（`services/rules/snapshot.py`）

**Files:**
- Modify: `src/sams/services/rules/snapshot.py`
- Test: `tests/services/test_snapshot_quote.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_snapshot_quote.py`：

```python
"""apply_quote：盖的是报价给的，留的是日线有的；缺现价那句在盖完之后必须消失。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sams.core.quotes import Quote
from sams.services.rules.snapshot import NO_PRICE_MISSING, Snapshot, apply_quote

ZONE = ZoneInfo("Asia/Shanghai")
TS = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def snap(**over):
    base = {
        "market": "A",
        "code": "601088",
        "price": 39.0,
        "price_on": date(2026, 10, 22),
        "price_age_days": 1,
        "prev_close": 40.0,
        "holding_high": 45.0,
        "missing": (NO_PRICE_MISSING,),
    }
    base.update(over)
    return Snapshot(**base)


def quote(**over):
    base = {
        "market": "A",
        "code": "601088",
        "ts": TS,
        "price": 38.2,
        "open": 38.8,
        "prev_close": 39.5,
        "high": 39.1,
        "source": "tencent",
    }
    base.update(over)
    return Quote(**base)


def test_the_quote_fields_win_and_the_day_follows_the_market():
    got = apply_quote(snap(), quote=quote(), previous=None, zone=ZONE)
    assert got.price == 38.2
    assert got.price_on == date(2026, 10, 23)  # UTC 03:00 的上海日
    assert got.price_age_days == 0
    assert got.day_open == 38.8
    assert got.prev_close == 39.5  # 报价给了就盖：urgent 的跳空判据要真昨收


def test_fields_the_source_did_not_give_keep_the_daily_value():
    got = apply_quote(snap(), quote=quote(open=None, prev_close=None), previous=None, zone=ZONE)
    assert got.day_open is None  # 日线本来就没有当日开盘
    assert got.prev_close == 40.0


def test_holding_high_extends_but_never_invents():
    assert apply_quote(snap(), quote=quote(), previous=None, zone=ZONE).holding_high == 45.0
    assert apply_quote(snap(holding_high=None), quote=quote(), previous=None, zone=ZONE).holding_high is None


def test_the_no_price_note_is_stripped_once_a_quote_arrives():
    got = apply_quote(snap(), quote=quote(), previous=None, zone=ZONE)
    assert got.missing == ()


def test_cross_from_comes_from_the_previous_round_of_the_same_day():
    previous = quote(price=39.9, ts=datetime(2026, 10, 23, 2, 0, tzinfo=UTC))
    got = apply_quote(snap(), quote=quote(), previous=previous, zone=ZONE)
    assert got.cross_from == 39.9
    yesterday = quote(price=39.9, ts=datetime(2026, 10, 22, 6, 0, tzinfo=UTC))
    got = apply_quote(snap(), quote=quote(), previous=yesterday, zone=ZONE)
    assert got.cross_from is None


def test_without_a_zone_nothing_cross_related_is_set():
    got = apply_quote(snap(), quote=quote(), previous=None, zone=None)
    assert got.cross_from is None
    assert got.price_on == TS.date()  # 没有市场时区就按 UTC 日兜底


def test_the_original_snapshot_is_untouched():
    before = snap()
    apply_quote(before, quote=quote(), previous=None, zone=ZONE)
    assert before.price == 39.0 and before.missing == (NO_PRICE_MISSING,)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_snapshot_quote.py -q`
Expected: FAIL，`ImportError: cannot import name 'NO_PRICE_MISSING'`

- [ ] **Step 3: 实现**

`src/sams/services/rules/snapshot.py`：

1. import 区补：

```python
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from sams.core.quotes import Quote, cross_baseline, local_day
```

（`replace`/`ZoneInfo` 若已 import 则不重复；`TYPE_CHECKING` 块不动。）

2. 常量区（`TRAILING_MIN_BARS` 附近）加：

```python
#: `RuleData.read` 与 `apply_quote` 共用的一句话：这一趟没有可判价格的读数。
#: 收成常量是因为「盖完报价之后它必须消失」——`apply_quote` 靠字面匹配把它挑出去
NO_PRICE_MISSING = "没有过闸的现价：这一趟不判任何价格类条件"
```

并把 `read()` 里那一句 `missing.append("没有过闸的现价：这一趟不判任何价格类条件")` 改成 `missing.append(NO_PRICE_MISSING)`。

3. `Snapshot` 末尾（`moved` 字段之后）追加：

```python
    #: 这一趟搬了哪几个数（每个都带它跨了哪些除权日，见 `anchor.Moved`）
    moved: tuple[Moved, ...] = ()
    #: 「穿」的另一侧价：盘中来自上一轮报价（§5.2），日线一趟永远是 None。
    #: 放在快照上而不是参数上传：模板签名（`Ask`）不必为此改
    cross_from: float | None = None
```

（`moved` 原样保留，只追加 `cross_from` 一行。）

4. 文件末尾（纯算式区）加模块级函数：

```python
def apply_quote(
    snapshot: Snapshot, *, quote: Quote, previous: Quote | None, zone: ZoneInfo | None
) -> Snapshot:
    """把这一轮的实时报价盖到日线上（§5.2）。纯函数：原快照不动，返回新的一份。

    只有报价**给了**的字段才盖（`open`/`prev_close` 缺了就保留日线的值）；`holding_high`
    取日线与报价高点的较大者，日线没有持仓高点时仍是 None——盘中冒出一个当天高点
    不能替历史最高价作数。`price_age_days=0` 是「这一趟的价就是现在」的声明。
    """
    day = local_day(quote.ts, zone) if zone is not None else quote.ts.date()
    missing = tuple(line for line in snapshot.missing if line != NO_PRICE_MISSING)
    holding_high = snapshot.holding_high
    if holding_high is not None:
        holding_high = max(
            holding_high, quote.high if quote.high is not None else quote.price
        )
    return replace(
        snapshot,
        price=quote.price,
        price_on=day,
        price_age_days=0,
        day_open=quote.open if quote.open is not None else snapshot.day_open,
        prev_close=quote.prev_close if quote.prev_close is not None else snapshot.prev_close,
        holding_high=holding_high,
        cross_from=(
            cross_baseline(quote, previous, zone=zone) if zone is not None else None
        ),
        missing=missing,
    )
```

- [ ] **Step 4: 跑测试确认通过，且既有快照用例不红**

Run: `uv run pytest tests/services/test_snapshot_quote.py tests/services/test_rules_snapshot.py -q`
Expected: PASS（两个文件全绿；`test_rules_snapshot.py` 若名字不同，用 `uv run pytest tests/services -q -k snapshot` 找到并确认）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/services/rules/snapshot.py tests/services/test_snapshot_quote.py --no-cache
git add src/sams/services/rules/snapshot.py tests/services/test_snapshot_quote.py
git commit -m "feat(rules): apply_quote 把实时报价盖到日线快照上（§5.2）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 4: 「穿」的另一侧可以是上一轮（`services/rules/templates.py`）

**Files:**
- Modify: `src/sams/services/rules/templates.py`（`_cross`，457 行起）
- Test: `tests/services/test_rules_cross_baseline.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_rules_cross_baseline.py`：

```python
"""`price.cross_*` 的另一侧：日线还是前收盘，盘中是同一交易日的上一轮报价。

「盘中不再退回前收盘」那一条是这一枚的核心：上一轮 10.05、线 10.04、现价 10.02
在上一轮坐标里没穿过，退回昨收就会误报——报的是两轮之前的行情。
"""

from __future__ import annotations

from sams.services.rules.snapshot import Snapshot
from sams.services.rules.templates import price_cross_above, price_cross_below


def snap(**over):
    base = {
        "market": "A",
        "code": "601088",
        "currency": "CNY",
        "price": 10.02,
        "prev_close": 10.00,
    }
    base.update(over)
    return Snapshot(**base)


def test_daily_path_still_says_prev_close():
    got = price_cross_above(type("Ask", (), {"snapshot": snap(), "params": {"price": 10.0}})())
    assert got.hit is not None
    assert got.hit.why.startswith("前收盘 10.00")


def test_intraday_uses_the_previous_round_and_says_so():
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(cross_from=9.95), "params": {"price": 10.0}},
    )()
    got = price_cross_above(ask)
    assert got.hit is not None
    assert got.hit.why.startswith("上一轮 9.95")


def test_intraday_does_not_fall_back_to_prev_close():
    # cross_from=10.05、线 10.04、现价 10.02：上一轮坐标里没穿过。若退回 prev_close=10.00
    # 就会误判成「上穿」——报的是两轮之前的行情
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(cross_from=10.05, prev_close=10.00), "params": {"price": 10.04}},
    )()
    assert price_cross_above(ask).hit is None


def test_no_baseline_at_all_says_why():
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(prev_close=None, cross_from=None), "params": {"price": 10.0}},
    )()
    got = price_cross_above(ask)
    assert got.hit is None
    assert "另一侧" in got.why_not


def test_downward_cross_uses_the_same_origin():
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(cross_from=10.05, price=10.03), "params": {"price": 10.04}},
    )()
    got = price_cross_below(ask)
    assert got.hit is not None
    assert got.hit.why.startswith("上一轮 10.05")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_rules_cross_baseline.py -q`
Expected: FAIL（`test_intraday_uses_the_previous_round_and_says_so`：cross_from=9.95 时旧代码看 prev_close=10.00，`10.00 <= 10.0 < 10.02` 成立但 why 说「前收盘」——断言 startswith("上一轮") 失败）

- [ ] **Step 3: 实现**

把 `src/sams/services/rules/templates.py` 里的 `_cross`（457 行起）整体替换为：

```python
def _cross(ask: Ask, *, upward: bool) -> Outcome:
    """两个 `price.cross_*` 的共用体：穿过去 = 上一根还在另一侧，这一根到了这一侧。

    「上一根」有两处来处（§5.2）：盘中是**同一个交易日的上一轮报价**（`cross_from`），
    日线是前收盘。`cross_from` 在手时**不**退回前收盘——那一跳该用两轮行情比，
    退回昨收盘报的是更早那一段。`>=` / `<=` 里带等号：另一侧**正好**压在线上也算
    从那侧起跳——那是最典型的回踩确认，把它算成「已经穿过了」会让这一天不报。
    """
    snapshot = ask.snapshot
    if snapshot.price is None:
        return Outcome(why_not=_no_price(snapshot))
    if snapshot.cross_from is not None:
        baseline, origin = snapshot.cross_from, "上一轮"
    else:
        baseline, origin = snapshot.prev_close, "前收盘"
    if baseline is None:
        return Outcome(
            why_not="没有可比较的另一侧（没有昨收，也没有上一轮报价）："
            "「穿」说的是从上一根到这一跳，只用现价判的话，一条早就在下面的线"
            "每个交易日都会重报一次"
        )
    line = float(ask.params["price"])
    if upward:
        crossed = baseline <= line < snapshot.price
    else:
        crossed = baseline >= line > snapshot.price
    if not crossed:
        return Outcome()
    verb = "上穿" if upward else "下穿"
    return Outcome(
        Verdict(
            stage=HIT,
            value=snapshot.price,
            threshold=line,
            value_text=money(snapshot.price, snapshot.currency),
            threshold_text=money(line, snapshot.currency),
            why=f"{origin} {money(baseline, snapshot.currency)} 在线 "
            f"{money(line, snapshot.currency)} 的另侧，现价 "
            f"{money(snapshot.price, snapshot.currency)} 已{verb}",
        )
    )
```

- [ ] **Step 4: 跑测试确认通过，模板全绿**

Run: `uv run pytest tests/services/test_rules_cross_baseline.py tests/services/test_rules_templates.py -q`
Expected: PASS（`test_rules_templates.py` 若名字不同，用 `uv run pytest tests/services -q -k template` 定位并确认全绿——日线措辞「前收盘」不许变）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/services/rules/templates.py tests/services/test_rules_cross_baseline.py --no-cache
git add src/sams/services/rules/templates.py tests/services/test_rules_cross_baseline.py
git commit -m "feat(rules): price.cross_* 的另一侧接上一轮报价（§5.2）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 5: 行情板记得上一轮（`services/quotes.py`）

**Files:**
- Modify: `src/sams/services/quotes.py`（plan-1 Task 9 建的 `QuoteBoard`）
- Test: `tests/services/test_board_previous.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_board_previous.py`：

```python
"""QuoteBoard.previous：被换下来的那一轮是「穿」的另一侧（§5.2）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.quotes import Quote
from sams.services.quotes import QuoteBoard

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def quote(price, **over):
    base = {"market": "A", "code": "601088", "ts": NOW, "price": price}
    base.update(over)
    return Quote(**base)


def test_the_displaced_round_becomes_previous():
    board = QuoteBoard()
    board.update("A", (quote(9.95),), now=NOW)
    board.update("A", (quote(9.40),), now=NOW)
    previous = board.previous("A", "601088")
    assert previous is not None and previous.price == 9.95
    assert board.get("A", "601088").price == 9.40


def test_the_first_round_has_no_previous():
    board = QuoteBoard()
    board.update("A", (quote(9.4),), now=NOW)
    assert board.previous("A", "601088") is None


def test_seed_does_not_write_previous():
    # 开局灌的是日线最后收盘：那不是「上一轮」，把它当上一轮会让 9:30 的第一跳误判
    board = QuoteBoard()
    board.seed((quote(9.95),))
    board.update("A", (quote(9.40),), now=NOW)
    assert board.previous("A", "601088") is None


def test_fail_and_skip_do_not_write_previous():
    board = QuoteBoard()
    board.update("A", (quote(9.4),), now=NOW)
    board.update("A", (quote(9.3),), now=NOW)
    board.fail("A", "源挂了", now=NOW)
    board.skip("HK", "非交易时段", now=NOW)
    previous = board.previous("A", "601088")
    assert previous is not None and previous.price == 9.4


def test_previous_is_keyed_by_market_and_code():
    board = QuoteBoard()
    board.update("A", (quote(9.4), quote(199.0, market="HK", code="00700")), now=NOW)
    board.update("A", (quote(9.3), quote(198.0, market="HK", code="00700")), now=NOW)
    assert board.previous("A", "601088").price == 9.4
    assert board.previous("HK", "00700").price == 199.0
    assert board.previous("HK", "09988") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_board_previous.py -q`
Expected: FAIL，`AttributeError: 'QuoteBoard' object has no attribute 'previous'`

- [ ] **Step 3: 实现**

`src/sams/services/quotes.py` 的 `QuoteBoard`：

1. `__init__` 加一个字典：

```python
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[tuple[str, str], Quote] = {}
        self._previous: dict[tuple[str, str], Quote] = {}
        self._marks: dict[str, _Mark] = {}
```

2. `update()` 的加锁块里，**先**把被换下来的那一轮搬进 `_previous` **再**覆盖：

```python
        with self._lock:
            for quote in quotes:
                key = (quote.market, quote.code)
                displaced = self._quotes.get(key)
                if displaced is not None:
                    # 被换下来的那一轮是「穿」的另一侧（§5.2）。seed/fail/skip 不写它：
                    # 那三处没有「新的一轮」这回事
                    self._previous[key] = displaced
                self._quotes[key] = quote
            mark = self._marks.setdefault(market, _Mark())
            mark.last_poll_at = now
            mark.last_error = error
            mark.skip_reason = None
```

3. `get()` 之后加：

```python
    def previous(self, market: str, code: str) -> Quote | None:
        """上一轮报价（§5.2）：`price.cross_*` 的「上一根」。开局第一轮是 None。"""
        with self._lock:
            return self._previous.get((market, code))
```

`seed()`、`fail()`、`skip()`、`snapshot()` 一律不动。

- [ ] **Step 4: 跑测试确认通过，板块既有用例不红**

Run: `uv run pytest tests/services/test_board_previous.py -q && uv run pytest tests/services -q -k "board or quote"`
Expected: PASS

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/services/quotes.py tests/services/test_board_previous.py --no-cache
git add src/sams/services/quotes.py tests/services/test_board_previous.py
git commit -m "feat(quotes): QuoteBoard.previous 记住被换下来的上一轮报价"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 6: 盘中那一趟的规则入口（`storage/repos/rules.py`）

**Files:**
- Modify: `src/sams/storage/repos/rules.py`（`list_for` 之后）
- Test: `tests/storage/test_rules_repo_enabled.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/storage/test_rules_repo_enabled.py`：

```python
"""list_enabled：盘中一趟的入口。跨用户、只启用的、次序稳定。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.storage.repos.rules import RuleRepo

NOW = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)


def add(repo, user_id, name, *, enabled=True):
    return repo.create(
        user_id,
        kind="stop.fixed",
        name=name,
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
        enabled=enabled,
        params_on=NOW.date(),
    )


def test_enabled_rules_across_users_in_a_stable_order(db):
    repo = RuleRepo(db)
    a1 = add(repo, "u-a", "甲一")
    b1 = add(repo, "u-b", "乙一")
    add(repo, "u-a", "甲停", enabled=False)
    b2 = add(repo, "u-b", "乙二")
    got = repo.list_enabled()
    # ORDER BY user_id, id：u-a 的在前；同一用户内按 id 排
    assert [r.id for r in got] == [a1.id] + sorted([b1.id, b2.id])
    assert [r.user_id for r in got] == ["u-a", "u-b", "u-b"]


def test_a_disabled_rule_never_shows_up(db):
    repo = RuleRepo(db)
    add(repo, "u-a", "开着")
    rule = add(repo, "u-a", "关着")
    repo.set_enabled(rule.id, False, user_id="u-a")
    assert [r.name for r in repo.list_enabled()] == ["开着"]


def test_rules_from_other_tables_are_not_leaked(db):
    # `set_enabled` 之后回读的形状必须和 `list_enabled` 同源：都是 `_to_rule`
    repo = RuleRepo(db)
    rule = add(repo, "u-a", "一条")
    got = repo.list_enabled()[0]
    assert got.id == rule.id and got.kind == "stop.fixed" and got.enabled is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_rules_repo_enabled.py -q`
Expected: FAIL，`AttributeError: 'RuleRepo' object has no attribute 'list_enabled'`

- [ ] **Step 3: 实现**

`src/sams/storage/repos/rules.py` 的 `list_for`（454 行）之后加：

```python
    def list_enabled(self) -> list[Rule]:
        """所有用户的启用规则（§5.3 盘中那一趟的入口）。

        与 `list_for` 差在两维：跨全部用户、只 `enabled = TRUE`。次序按 user 再 id：
        盘中一趟按用户逐个求值，读进来就聚好，调用方不必再排。
        """
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM rules WHERE enabled = TRUE ORDER BY user_id, id"
        )
        return [_to_rule(row) for row in rows.to_dicts()]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_rules_repo_enabled.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/storage/repos/rules.py tests/storage/test_rules_repo_enabled.py --no-cache
git add src/sams/storage/repos/rules.py tests/storage/test_rules_repo_enabled.py
git commit -m "feat(rules): 仓库层 list_enabled，盘中求值的规则入口（§5.3）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 7: 告警带上报价三件套与测试标记（`core/notify.py`、`repos/alerts.py`、`services/health.py`）

**Files:**
- Modify: `src/sams/core/notify.py`（`AlertMessage`）
- Modify: `src/sams/storage/repos/alerts.py`（`Alert`、`_ALERT_COLUMNS`、`create`、`latest_open`、`_to_alert`）
- Modify: `src/sams/services/health.py`（积压两条查询）
- Test: `tests/storage/test_alerts_quote_meta.py`

无新迁移：`alerts.is_test` 列由 plan-1 的 0019 建好；报价元数据进 `payload_json`（D120）。

- [ ] **Step 1: 写失败测试**

新建 `tests/storage/test_alerts_quote_meta.py`：

```python
"""D120/D131 的两件事：报价三件套随行落库，测试告警不进 no_open_alert 与健康积压。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sams.datasource.registry import AdapterRegistry
from sams.services.health import HealthService
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.calendar import CalendarRepo

MOMENT = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def row(repo, **over):
    base = dict(
        rule_id="r1",
        rule_name="跌破 10 元",
        account_id=None,
        account_name="",
        market="A",
        code="601088",
        symbol_name="中国神华",
        severity="mid",
        stage="hit",
        value_text="9.40 CNY",
        threshold_text="10.00 CNY",
        action_note="按计划处置",
        triggered_at=MOMENT,
    )
    base.update(over)
    return repo.create("u1", **base)


def test_quote_metadata_round_trips(db):
    repo = AlertRepo(db)
    created = row(repo, intraday=True, quote_ts=MOMENT, quote_source="tencent")
    got = repo.get(created.id)
    assert got.intraday is True
    assert got.quote_ts == MOMENT
    assert got.quote_source == "tencent"
    assert got.is_test is False


def test_is_test_round_trips_and_defaults_to_false(db):
    repo = AlertRepo(db)
    assert row(repo).is_test is False
    assert repo.get(row(repo, is_test=True).id).is_test is True


def test_latest_open_ignores_test_rows(db):
    # no_open_alert 的输入（§10.1）：/settings/test-alert 发的那条不该把真告警挡一整天。
    # 测试行故意更「新」：latest_open 按 triggered_at 挑最新，新的测试行不许压住真的
    repo = AlertRepo(db)
    real = row(repo)
    row(repo, is_test=True, stage="pre", triggered_at=MOMENT + timedelta(minutes=5))
    got = repo.latest_open("u1", "A", "601088")
    assert got is not None and got.id == real.id


def test_latest_open_on_a_test_only_stream_is_none(db):
    repo = AlertRepo(db)
    row(repo, is_test=True)
    assert repo.latest_open("u1", "A", "601088") is None


def test_health_backlog_ignores_test_rows(db, settings):
    service = HealthService(
        db,
        AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _s: None),
        CalendarRepo(db),
        settings,
    )
    repo = AlertRepo(db)
    row(repo)
    row(repo, code="600519", is_test=True)
    assert service.report().notify.unhandled == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alerts_quote_meta.py -q`
Expected: FAIL，`TypeError: create() got an unexpected keyword argument 'intraday'`

- [ ] **Step 3: 实现**

`src/sams/core/notify.py` 的 `AlertMessage`：在 `alert_id` 字段之后追加：

```python
    alert_id: str = ""
    #: 盘中三件套（D120）：这一条是盘中判的、报价是哪一刻、出自哪个源。
    #: 放在消息上而不是事后查库：通知线程拿到的是重建出来的那份，别让它再回头查
    intraday: bool = False
    quote_ts: datetime | None = None
    quote_source: str | None = None
```

（`alert_id` 原样保留；`__post_init__` 不动。）

`src/sams/storage/repos/alerts.py`：

1. `Alert` 数据类在 `channel_ids` 之后追加：

```python
    channel_ids: list[str] = field(default_factory=list)
    #: `alerts.is_test` 那一列（0019）：测试告警不进 no_open_alert 与健康积压（D131）
    is_test: bool = False
    #: 这三个来自 payload（D120）：要显示与回放的事实，不占列——占列的话措辞改版又要动表
    intraday: bool = False
    quote_ts: datetime | None = None
    quote_source: str | None = None
```

2. `_ALERT_COLUMNS` 追加一列：

```python
_ALERT_COLUMNS = (
    "id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
    " status, payload_json, severity, handled_at, handle_note, dismiss_reason, is_test"
)
```

3. `create()`：签名在 `extra: Mapping[str, object] | None = None,` 之后追加四个参数：

```python
        extra: Mapping[str, object] | None = None,
        is_test: bool = False,
        intraday: bool = False,
        quote_ts: datetime | None = None,
        quote_source: str | None = None,
    ) -> Alert:
```

payload 字典追加四个键（放在 `"extra": …` 之后）：

```python
            "extra": dict(extra or {}),
            "is_test": bool(is_test),
            "intraday": bool(intraday),
            "quote_ts": None if quote_ts is None else _moment(quote_ts).isoformat(),
            "quote_source": quote_source,
        }
```

INSERT 语句改为带 `is_test` 列：

```python
        self.db.execute(
            "INSERT INTO alerts"
            " (id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
            " status, payload_json, severity, is_test)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
            [
                alert_id,
                user_id,
                rule_id,
                account_id,
                market,
                code,
                stage,
                moment,
                json.dumps(payload, ensure_ascii=False),
                severity,
                bool(is_test),
            ],
        )
```

4. `latest_open()`：WHERE 追加一条，docstring 末尾补一句：

```python
        rows = self.db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts"
            " WHERE user_id = ? AND market = ? AND code = ? AND status = 'new'"
            " AND coalesce(is_test, FALSE) = FALSE"
            " ORDER BY triggered_at DESC, id DESC LIMIT 1",
            [user_id, market, code],
        )
```

docstring 追加：`测试告警（D131）不进来：/settings/test-alert 发的那条不该把真告警挡一整天。`

5. `_to_alert()`：返回的 `Alert(...)` 追加四个字段：

```python
            channel_ids=list(payload.get("channel_ids") or []),
            is_test=bool(row["is_test"]),
            intraday=bool(payload.get("intraday") or False),
            quote_ts=_parse_ts(payload.get("quote_ts")),
            quote_source=(
                str(payload["quote_source"]) if payload.get("quote_source") else None
            ),
        )
```

模块级加一个小解析器（`_moment` 附近）：

```python
def _parse_ts(raw: object) -> datetime | None:
    """payload 里存的 ISO 时刻。存坏了的那一行不能让整个收件箱读不出来。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("payload 里的 quote_ts 读不懂：%r", raw)
        return None
```

6. `src/sams/services/health.py` 的两条用户积压查询（347 行起）各加一个条件：

```python
        # 测试告警（D131）不算「没人看」：/settings/test-alert 发的那条
        # 不该让未处理计数常年 +1。系统侧那两条查询不动——SYS 行永远不会是测试行
        unhandled = int(
            self.db.scalar(
                "SELECT count(*) FROM alerts WHERE status = 'new' AND market <> ?"
                " AND coalesce(is_test, FALSE) = FALSE",
                [SYSTEM_MARKET],
            )
            or 0
        )
```

`oldest` 那条同样在 `market <> ?` 之后加 `" AND coalesce(is_test, FALSE) = FALSE"`。

- [ ] **Step 4: 跑测试确认通过，相邻面不红**

Run: `uv run pytest tests/storage/test_alerts_quote_meta.py tests/monitor/test_router.py tests/services/test_health.py -q`
Expected: PASS（`create()` 是加关键字参数，既有调用方不受影响）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/core/notify.py src/sams/storage/repos/alerts.py src/sams/services/health.py tests/storage/test_alerts_quote_meta.py --no-cache
git add src/sams/core/notify.py src/sams/storage/repos/alerts.py src/sams/services/health.py tests/storage/test_alerts_quote_meta.py
git commit -m "feat(alerts): 报价三件套进 payload，测试告警退出 no_open_alert 与健康积压（D120/D131）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 8: 引擎的盘中那一趟（`services/rules/engine.py`）

**Files:**
- Modify: `src/sams/services/rules/engine.py`
- Modify: `tests/support_rules.py`
- Test: `tests/services/test_rules_intraday_engine.py`

- [ ] **Step 1: 替身先行（`tests/support_rules.py`）**

1. `StubData` 加读数记录——`__init__` 末尾加 `self.as_of_seen: list[date] = []`，`read()` 开头加 `self.as_of_seen.append(as_of)`。
2. 新增替身（放在 `StubData` 之后）：

```python
class StubQuotes:
    """QuoteBoard 的替身：引擎只问 `get` 与 `previous`（§5.3 的 `QuoteView`）。"""

    def __init__(self, current=None, previous=None) -> None:
        self.current = dict(current or {})
        self.previous_map = dict(previous or {})
        self.gets: list[tuple[str, str]] = []

    def get(self, market, code):
        self.gets.append((market, code))
        return self.current.get((market, code))

    def previous(self, market, code):
        return self.previous_map.get((market, code))
```

3. `Harness.engine` **这一步不动**：`cache` 参数属于 Task 9（引擎那时才有这个形参），
   现在透传一个 `cache=None` 只会让每个既有用例在构造引擎时炸 TypeError。
   本任务的所有用例都调 `h.engine()`，不带缓存。

- [ ] **Step 2: 写失败测试**

新建 `tests/services/test_rules_intraday_engine.py`：

```python
"""盘中那一趟（§5.3）：闸在缓存之前，日线读数的日期跟着报价走，正文多一行出处。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sams.core.errors import ValidationError
from sams.core.quotes import Quote
from support_rules import NOW, USER, Harness, StubQuotes

#: 比引擎时钟新 5 秒（上海 15:29:55）：staleness 闸按「引擎时钟 − 报价 ts」算岁数，
#: 报价时刻写死成将来时的话， age 是负数，按岁数拒掉的那一枚永远测不到
TS = NOW - timedelta(seconds=5)


def quote(**over):
    base = {
        "market": "A",
        "code": "601088",
        "ts": TS,
        "price": 9.4,
        "open": 9.8,
        "prev_close": 10.2,
        "source": "tencent",
    }
    base.update(over)
    return Quote(**base)


def board(q, prev=None):
    previous = {(q.market, q.code): prev} if prev is not None else {}
    return StubQuotes({(q.market, q.code): q}, previous)


def test_without_a_quote_the_target_is_skipped(db, settings):
    h = Harness(db, settings)
    h.add()
    engine = h.engine()
    got = engine.check(USER, intraday=True, quotes=StubQuotes())
    assert got.rules == 1 and len(got.skipped) == 1
    assert "盘中还没有这只票的报价" in got.skipped[0].reason
    assert engine.data.as_of_seen == []  # 闸在取数之前：连日线都没读


def test_a_stale_quote_is_refused_at_the_gate(db, settings):
    h = Harness(db, settings)
    h.add()
    engine = h.engine()
    got = engine.check(USER, intraday=True, quotes=board(quote(is_stale=True)))
    assert "stale" in got.skipped[0].reason
    got = engine.check(
        USER, intraday=True, quotes=board(quote(ts=TS - timedelta(seconds=120)))
    )
    assert "超过上限 60 秒" in got.skipped[0].reason
    assert engine.data.as_of_seen == []


def test_a_fresh_quote_overlays_and_flows_to_a_hit(db, settings):
    h = Harness(db, settings)
    h.add()
    got = h.engine().check(USER, intraday=True, quotes=board(quote()))
    assert len(got.raised) == 1
    message = h.sender.sent[0]
    assert message.intraday is True
    assert message.quote_ts == TS
    assert message.quote_source == "tencent"
    assert "报价 11:00:00 · 腾讯 · 延迟未标注" in message.body


def test_the_daily_read_lands_on_the_quote_day(db, settings):
    h = Harness(db, settings)
    h.add()
    engine = h.engine()
    engine.check(USER, intraday=True, quotes=board(quote()))
    assert engine.data.as_of_seen == [TS.date()]


def test_only_intraday_kinds_run_in_the_intraday_pass(db, settings):
    h = Harness(db, settings)
    h.add()
    h.add(kind="stop.time", name="到期复核", params={"days": 30})
    got = h.engine().check(USER, intraday=True, quotes=board(quote()))
    assert got.rules == 1  # stop.time 被名单滤掉，压根不展开


def test_the_daily_path_still_runs_without_quotes(db, settings):
    h = Harness(db, settings)
    h.add()
    got = h.engine().check(USER)
    assert len(got.raised) == 1
    assert h.sender.sent[0].intraday is False


def test_intraday_without_a_board_is_refused(db, settings):
    h = Harness(db, settings)
    h.add()
    with pytest.raises(ValidationError):
        h.engine().check(USER, intraday=True)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/services/test_rules_intraday_engine.py -q`
Expected: FAIL，`TypeError: check() got an unexpected keyword argument 'intraday'`

- [ ] **Step 4: 实现（`src/sams/services/rules/engine.py`）**

1. import 区改两行、加三行：

```python
from typing import Protocol
from zoneinfo import ZoneInfo

from sams.core.quotes import Quote, local_day, source_label
from sams.core.rules import Verdict, escalates, is_intraday, template_for
from sams.services.rules.snapshot import RuleData, Snapshot, Target, apply_quote
```

（`Verdict, escalates, template_for` 与 `RuleData, Snapshot, Target` 是在原有行上补名，不是新行。）

2. 常量区（`SESSION_LOOKBACK_DAYS` 之后）加协议：

```python
class QuoteView(Protocol):
    """盘中一趟向引擎开口的两个问题：这只票现在什么价，上一轮是什么价。

    `QuoteBoard` 天然长成这个形状；替身与它同构（`support_rules.StubQuotes`）。
    """

    def get(self, market: str, code: str) -> Quote | None: ...

    def previous(self, market: str, code: str) -> Quote | None: ...
```

3. `check()`：签名追加两个关键字参数，函数体开头加闸，选择与传递改三处：

```python
    def check(
        self,
        user_id: str,
        *,
        as_of: date | None = None,
        market: str | None = None,
        dry_run: bool = False,
        rule_id: str | None = None,
        intraday: bool = False,
        quotes: QuoteView | None = None,
    ) -> RunReport:
```

```python
        now = self.clock.now_utc()
        day = as_of if as_of is not None else self.clock.today()
        if intraday and quotes is None:
            # 不给默认空板：一个忘传的 None 会让整趟「一只票都没报价」悄悄变绿，
            # 那是装配错误，不是业务结论
            raise ValidationError("盘中一趟必须带行情板：quotes 是这一次求值的现价出处")
        if market is not None:
            market = canonical_market(market)
```

循环里两处（`_one` 现有调用里那个 `cache=cache` 是趟内去重的既有参数，原样留着；
这里新加的只有 `quotes=quotes`，跨趟缓存属于 Task 9）：

```python
        selected = self._select(user_id, rule_id, intraday=intraday)
```

```python
                fate = self._one(
                    rule,
                    target,
                    position=held.get((target.market, target.code)),
                    day=day,
                    now=now,
                    dry_run=dry_run,
                    cache=cache,
                    quotes=quotes,
                )
```

4. `_select()` 加关键字参数与盘中分支：

```python
    def _select(
        self, user_id: str, rule_id: str | None, *, intraday: bool = False
    ) -> list[Rule]:
        """点名一条规则时绕过 `enabled`：用户刚停用一条又 `--rule` 点它，
        要的显然是「这条如果开着会判出什么」，回一句空列表最没用。

        盘中那一趟走另一扇门（§5.3）：跨全部用户的启用规则里挑出 intraday 的那半，
        再按用户切——名单来自 `is_intraday`，不是这里再抄一份。
        """
        if intraday:
            return [
                rule
                for rule in self.rules.list_enabled()
                if rule.user_id == user_id and is_intraday(rule.kind)
            ]
        listed = self.rules.list_for(user_id, enabled_only=rule_id is None)
        if rule_id is None:
            return listed
        return [rule for rule in listed if rule.id == rule_id]
```

5. `_one()`：签名加 `quotes: QuoteView | None = None`，开头插闸、日期跟着报价走：

```python
    def _one(
        self,
        rule: Rule,
        target: Target,
        *,
        position: Position | None,
        day: date,
        now: datetime,
        dry_run: bool,
        cache: _Cache,
        quotes: QuoteView | None = None,
    ) -> Raised | Suppressed | Skipped | None:
        quote: Quote | None = None
        zone: ZoneInfo | None = None
        if quotes is not None:
            quote = quotes.get(target.market, target.code)
            if quote is None:
                # 没报价不判（§5.3）：连「 stale 」都谈不上——这只票今天压根没被问过价
                return self._skipped(
                    rule, target.market, target.code,
                    "盘中还没有这只票的报价：这一趟不判",
                )
            stale = self._stale_note(quote)
            if stale:
                return self._skipped(rule, target.market, target.code, stale)
            # 日线读到报价说自己是哪天：US 的收盘在北京是次日凌晨
            zone = self._zone(target.market)
            day = local_day(quote.ts, zone) if zone is not None else quote.ts.date()
        # ——以下与原实现相同：TABLE.get → template_for → read（NotAdjustable）→ …——
```

`read` 那一段在 except 之后、`move_prices` 之前插一次覆盖：

```python
        try:
            snapshot = self.data.read(target, as_of=day, position=position)
        except NotAdjustable as exc:
            return self._skipped(
                rule, target.market, target.code, f"读不到这个对象：{exc}"
            )
        if quote is not None:
            snapshot = apply_quote(
                snapshot,
                quote=quote,
                previous=quotes.previous(target.market, target.code),
                zone=zone,
            )
        placed = move_prices(rule, built, snapshot)
```

（从 `placed = move_prices(...)` 起到函数尾与原实现一致，只改最后 dispatch 那一行：）

```python
        try:
            return self._dispatch(rule, snapshot, verdict, built, now, quote=quote)
        except SamsError as exc:
            return self._skipped(rule, snapshot.market, snapshot.code, f"送不出去：{exc}")
```

6. 新增两个私有方法（`_local_day` 一带）：

```python
    def _zone(self, market: str) -> ZoneInfo | None:
        try:
            return self.settings.market_cfg(market).zone()
        except ValidationError:
            return None

    def _stale_note(self, quote: Quote) -> str:
        """盘中那一道新鲜度闸（§5.2）：陈旧的报价宁可跳过，也不能拿来判止损。"""
        if quote.is_stale:
            return "报价源把这一笔标成了不可信（stale）：这一趟不判"
        age = (self.clock.now_utc() - quote.ts).total_seconds()
        if age > self.settings.quote_stale_s:
            return (
                f"报价距今 {int(age)} 秒，超过上限 {self.settings.quote_stale_s} 秒："
                "这一趟不判"
            )
        return ""
```

并把 `_local_day` 改成走 `_zone`（行为不变）：

```python
    def _local_day(self, market: str, now: datetime) -> date | None:
        zone = self._zone(market)
        return None if zone is None else local_day(now, zone)
```

7. 投递三件的签名与实现（threading quote）：

```python
    def _dispatch(
        self, rule: Rule, snapshot: Snapshot, verdict: Verdict, built, now: datetime,
        *, quote: Quote | None = None,
    ) -> Raised:
        message = self._message(rule, snapshot, verdict, built, now, quote=quote)
        # ……其余与原实现相同……
```

```python
    def _message(
        self, rule: Rule, snapshot: Snapshot, verdict: Verdict, built, now: datetime,
        *, quote: Quote | None = None,
    ) -> AlertMessage:
        return AlertMessage(
            market=snapshot.market,
            code=snapshot.code,
            severity=rule.severity,
            triggered_at=now,
            name=snapshot.name,
            account_name=snapshot.account_name,
            rule_name=rule.name,
            value_text=verdict.value_text,
            threshold_text=verdict.threshold_text,
            action_note=action_for(rule.kind, rule.name),
            stage=verdict.stage,
            body=self._body(rule, built, snapshot, verdict, quote=quote),
            intraday=quote is not None,
            quote_ts=quote.ts if quote is not None else None,
            quote_source=quote.source if quote is not None else None,
        )
```

`_body` 加关键字参数并在第一行之后插报价行：

```python
    def _body(
        self, rule: Rule, built, snapshot: Snapshot, verdict: Verdict,
        *, quote: Quote | None = None,
    ) -> str:
        """……原 docstring 保留，末尾补一句：
        盘中那一趟的第二行是报价出处（D120）：几点的价、哪个源、延迟标没标。
        """
        lines = [f"{built.label}：{verdict.why}" if verdict.why else built.label]
        if quote is not None:
            lines.append(_quote_line(quote, self._zone(snapshot.market)))
        # ……其余与原实现相同……
```

8. 模块级函数（`ACTIONS` 常量附近）：

```python
def _quote_line(quote: Quote, zone: ZoneInfo | None) -> str:
    """正文第二行（D120/D134）：报价的时刻与来源，市场本地时区，秒级。

    「延迟未标注」也要写：没标注不等于保证实时，写出来用户才知道该信几分。
    """
    shown = quote.ts if zone is None else quote.ts.astimezone(zone)
    parts = [f"报价 {shown:%H:%M:%S}", source_label(quote.source)]
    if quote.delayed is True:
        parts.append("延迟")
    elif quote.delayed is None:
        parts.append("延迟未标注")
    return " · ".join(parts)
```

- [ ] **Step 5: 跑测试确认通过，引擎既有用例全绿**

Run: `uv run pytest tests/services/test_rules_intraday_engine.py tests/services/test_rules_engine.py tests/test_adversarial_services.py -q`
Expected: PASS（收盘那一趟的行为一个都不许变）

- [ ] **Step 6: 检查与提交**

```bash
uv run ruff check src/sams/services/rules/engine.py tests/support_rules.py tests/services/test_rules_intraday_engine.py --no-cache
git add src/sams/services/rules/engine.py tests/support_rules.py tests/services/test_rules_intraday_engine.py
git commit -m "feat(rules): 引擎的盘中一趟——新鲜度闸、报价覆盖与正文出处（§5.3）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 9: 盘中的日线缓存（`services/rules/cache.py`）

**Files:**
- Create: `src/sams/services/rules/cache.py`
- Modify: `src/sams/services/rules/engine.py`（构造参数、`_one` 的读法）
- Test: `tests/services/test_rules_cache.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_rules_cache.py`：

```python
"""SnapshotCache：版本闸整表失效、日子变了当没有、TTL 兜底；引擎两轮只读一次日线。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sams.core.quotes import Quote
from sams.core.version import VersionGate
from sams.services.rules.cache import CacheKey, SnapshotCache
from support_rules import NOW, USER, Harness, StubQuotes

#: 与引擎时钟同一分钟：staleness 闸不能抢在缓存之前把这一趟拒掉
TS = NOW - timedelta(seconds=5)
KEY = CacheKey(user_id="u1", rule_id="r1", market="A", code="601088")
DAY = TS.date()


def cache(monotonic=lambda: 0.0) -> SnapshotCache:
    return SnapshotCache(VersionGate(), monotonic=monotonic)


def test_roundtrip_within_the_same_day():
    got_cache = cache()
    got_cache.put(KEY, "snap", day=DAY)
    assert got_cache.get(KEY, day=DAY) == "snap"


def test_a_different_day_is_a_miss():
    got_cache = cache()
    got_cache.put(KEY, "snap", day=DAY)
    assert got_cache.get(KEY, day=DAY + timedelta(days=1)) is None


@pytest.mark.parametrize("kind", ["rules", "theses", "ledger", "bars"])
def test_any_version_bump_clears_everything(kind):
    # 分 kind 失效省下的那点读，不值「哪一版忘了 bump」的风险——四个键一个待遇
    gate = VersionGate()
    got_cache = SnapshotCache(gate, monotonic=lambda: 0.0)
    got_cache.put(KEY, "snap", day=DAY)
    gate.bump(kind)
    assert got_cache.get(KEY, day=DAY) is None


def test_ttl_expires():
    # put 记下 0.0；第一取 0.0（岁数 0，命中）；第二取 301.0（岁数 > 300，过期）
    ticks = iter([0.0, 0.0, 301.0])
    got_cache = SnapshotCache(VersionGate(), monotonic=lambda: next(ticks))
    got_cache.put(KEY, "snap", day=DAY)
    assert got_cache.get(KEY, day=DAY) == "snap"
    assert got_cache.get(KEY, day=DAY) is None


def test_two_intraday_rounds_share_one_daily_read(db, settings):
    h = Harness(db, settings)
    h.add()
    tick = {"t": 0.0}
    engine = h.engine(cache=SnapshotCache(VersionGate(), monotonic=lambda: tick["t"]))

    def q(price):
        return Quote(
            market="A", code="601088", ts=TS, price=price, source="tencent"
        )

    board = StubQuotes({("A", "601088"): q(9.4)})
    first = engine.check(USER, intraday=True, quotes=board)
    assert len(first.raised) == 1
    board.current[("A", "601088")] = q(9.1)
    second = engine.check(USER, intraday=True, quotes=board)
    assert len(second.raised) == 1
    assert "9.10" in second.raised[0].value_text  # 判的是新价，不是缓存里的旧价
    assert len(engine.data.as_of_seen) == 1  # 第二轮没再读日线


def test_a_bump_makes_the_next_round_read_again(db, settings):
    h = Harness(db, settings)
    h.add()
    gate = VersionGate()
    engine = h.engine(cache=SnapshotCache(gate, monotonic=lambda: 0.0))

    def q(price):
        return Quote(market="A", code="601088", ts=TS, price=price, source="tencent")

    board = StubQuotes({("A", "601088"): q(9.4)})
    engine.check(USER, intraday=True, quotes=board)
    gate.bump("rules")
    engine.check(USER, intraday=True, quotes=board)
    assert len(engine.data.as_of_seen) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_rules_cache.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'sams.services.rules.cache'`

- [ ] **Step 3: 实现缓存模块**

新建 `src/sams/services/rules/cache.py`：

```python
"""盘中一趟共用的日线读数缓存（spec §5.4）。

缓存里只有日线的部分，从来不是报价：报价每一轮都变，把两种东西存进同一张表，
「改一条规则要不要清缓存」就没法回答了。失效只有两种：任何一版变更
（rules/theses/ledger/bars 之一被 bump）→ 清**全部**；日子变了或 TTL 到了 → 单键当没有。

不加锁：读方只有盘中那一个线程；收盘那一趟从不经过它。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sams.services.rules.snapshot import Snapshot

if TYPE_CHECKING:
    from sams.core.version import VersionGate

#: 盘中一层兜底：版本闸没碰上（比如有人绕过服务层直接写库）时，一份读数最多信五分钟
DEFAULT_TTL_S = 300.0


@dataclass(frozen=True)
class CacheKey:
    """一份日线读数属于谁（§5.4）：去重键那六元组减掉账户无关的两维。

    `account_id`/`thesis_id` 取自展开后的对象（`Target`），不是规则上的那一列——
    快照里带着的是前者的值。
    """

    user_id: str
    rule_id: str
    market: str
    code: str
    account_id: str | None = None
    thesis_id: str | None = None


@dataclass(frozen=True)
class _Entry:
    snapshot: Snapshot
    day: date
    at: float


class SnapshotCache:
    def __init__(
        self,
        versions: VersionGate,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._versions = versions
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._stamp = versions.snapshot()
        self._entries: dict[CacheKey, _Entry] = {}

    def get(self, key: CacheKey, *, day: date) -> Snapshot | None:
        self._settle()
        entry = self._entries.get(key)
        if entry is None or entry.day != day:
            return None
        if self._monotonic() - entry.at > self._ttl_s:
            return None
        return entry.snapshot

    def put(self, key: CacheKey, snapshot: Snapshot, *, day: date) -> None:
        self._settle()
        self._entries[key] = _Entry(snapshot, day, self._monotonic())

    def _settle(self) -> None:
        """版本闸动过就整表清空：什么时候动过的不必知道，字典一换就是新的。"""
        stamp = self._versions.snapshot()
        if stamp != self._stamp:
            self._entries.clear()
            self._stamp = stamp
```

- [ ] **Step 4: 引擎接线**

`src/sams/services/rules/engine.py`：

1. import 加 `from sams.services.rules.cache import CacheKey, SnapshotCache`。
2. 构造参数在 `settings` 之后加 `cache: SnapshotCache | None = None`，函数体加 `self._cache = cache`（放在 `self.settings = settings` 之后）。
3. 新增方法（放在 `_one` 之前）：

```python
    def _daily(
        self,
        rule: Rule,
        target: Target,
        *,
        position: Position | None,
        day: date,
    ) -> Snapshot:
        """日线读数的那一次 IO：盘中线程走缓存，收盘那一趟直读。"""
        if self._cache is None:
            return self.data.read(target, as_of=day, position=position)
        key = CacheKey(
            user_id=rule.user_id,
            rule_id=rule.id,
            market=target.market,
            code=target.code,
            account_id=target.account_id,
            thesis_id=target.thesis_id,
        )
        got = self._cache.get(key, day=day)
        if got is not None:
            return got
        snapshot = self.data.read(target, as_of=day, position=position)
        self._cache.put(key, snapshot, day=day)
        return snapshot
```

（`_daily` 不收 `cache` 参数：它只认 `self._cache`。引擎里已有一个同名的 `_Cache`——
那是 `check` 里一趟一建的去重临时表，与这份跨趟缓存毫无关系，别把一个传给另一个。）

4. `_one` 里把读数那一行换成 `_daily`（NotAdjustable 的 try/except 原样包住它；
   `_one` 现有签名里那个 `cache: _Cache` 参数是趟内去重用的，原样保留）：

```python
        try:
            snapshot = self._daily(rule, target, position=position, day=day)
        except NotAdjustable as exc:
            return self._skipped(
                rule, target.market, target.code, f"读不到这个对象：{exc}"
            )
```

5. `tests/support_rules.py` 的 `Harness.engine` 透传缓存（上面两枚集成用例从 `h.engine(cache=…)` 进）：

```python
    def engine(self, *, sender=None, cache=None) -> RuleEngine:
        data = StubData(self.snapshot, self.held, self.thesis)
        data.read_raises = self.read_raises
        return RuleEngine(
            rules=self.rules,
            alerts=self.alerts,
            data=data,
            calendar=self.calendar,
            fx=self.fx,
            clock=self.clock,
            bus=self.bus,
            sender=sender or self.sender,
            settings=self.settings,
            cache=cache,
        )
```

- [ ] **Step 5: 跑测试确认通过，引擎全绿**

Run: `uv run pytest tests/services/test_rules_cache.py tests/services/test_rules_engine.py tests/services/test_rules_intraday_engine.py -q`
Expected: PASS

- [ ] **Step 6: 检查与提交**

```bash
uv run ruff check src/sams/services/rules/cache.py src/sams/services/rules/engine.py tests/support_rules.py tests/services/test_rules_cache.py --no-cache
git add src/sams/services/rules/cache.py src/sams/services/rules/engine.py tests/support_rules.py tests/services/test_rules_cache.py
git commit -m "feat(rules): 盘中日线缓存 SnapshotCache——版本闸整表失效加 TTL 兜底（§5.4）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 10: 每一轮轮询之后的求值器（`services/rules/intraday.py`）

**Files:**
- Create: `src/sams/services/rules/intraday.py`
- Test: `tests/services/test_rules_intraday_evaluator.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_rules_intraday_evaluator.py`：

```python
"""IntradayEvaluator：哪几个市场活了、每个用户×市场各判一趟、坏了不拖垮别人。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sams.core.errors import StorageError
from sams.core.quotes import Quote
from sams.services.intraday import MarketPoll
from sams.services.quotes import QuoteBoard, QuotePollResult
from sams.services.rules.intraday import IntradayEvaluator
from support_rules import NOW, Harness

TS = NOW - timedelta(seconds=5)


def quote(code="601088", **over):
    base = {"market": "A", "code": code, "ts": TS, "price": 9.4, "source": "tencent"}
    base.update(over)
    return Quote(**base)


def live_board(q) -> QuoteBoard:
    board = QuoteBoard()
    board.update(q.market, (q,), now=NOW)
    return board


def outcome(q):
    return (
        MarketPoll(q.market, result=QuotePollResult(quotes=(q,), skipped=(), errors=(), sources=())),
    )


def test_a_round_with_quotes_evaluates_and_counts_hits(db, settings):
    h = Harness(db, settings)
    h.add()
    q = quote()
    evaluator = IntradayEvaluator(h.rules, h.engine(), live_board(q))
    got = evaluator.after_poll(outcome(q))
    assert got.markets == ("A",)
    assert got.rules == 1
    assert got.hits == 1


def test_markets_without_a_result_are_not_evaluated(db, settings):
    h = Harness(db, settings)
    h.add()
    evaluator = IntradayEvaluator(h.rules, h.engine(), live_board(quote()))
    got = evaluator.after_poll((MarketPoll("A", skipped="非交易时段"),))
    assert got.markets == () and got.rules == 0 and got.hits == 0
    assert h.sender.sent == []


def test_an_empty_result_is_not_a_live_market(db, settings):
    h = Harness(db, settings)
    h.add()
    evaluator = IntradayEvaluator(h.rules, h.engine(), live_board(quote()))
    got = evaluator.after_poll(
        (MarketPoll("A", result=QuotePollResult()),)
    )
    assert got.markets == () and got.hits == 0


def test_each_user_gets_its_own_pass_per_market(db, settings):
    h = Harness(db, settings)
    h.add()
    h.rules.create(
        "u-two",
        kind="stop.fixed",
        name="另一条",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
    )
    calls: list[tuple[str, str]] = []

    class CountingEngine:
        def check(self, user_id, **kw):
            calls.append((user_id, kw["market"]))
            return SimpleNamespace(raised=())

    q = quote()
    got = IntradayEvaluator(h.rules, CountingEngine(), live_board(q)).after_poll(outcome(q))
    assert set(calls) == {(USER, "A"), ("u-two", "A")}
    assert got.rules == 2 and got.hits == 0


def test_a_sams_error_is_isolated_not_fatal(db, settings):
    class ExplodingRules:
        def list_enabled(self):
            raise StorageError("库写不进")

    q = quote()
    evaluator = IntradayEvaluator(ExplodingRules(), Harness(db, settings).engine(), live_board(q))
    got = evaluator.after_poll(outcome(q))  # 不抛
    assert got.markets == ("A",) and got.rules == 0 and got.hits == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_rules_intraday_evaluator.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'sams.services.rules.intraday'`

- [ ] **Step 3: 实现**

新建 `src/sams/services/rules/intraday.py`：

```python
"""每一轮报价之后的那一趟求值（spec §5.3）：它自己不碰网络，也不碰库以外的东西。

输入是 `IntradayMonitor.poll_once` 的产物，输出是一张小结。逐用户 × 逐活市场调
`RuleEngine.check(intraday=True)`：求值链、过滤、冷却、投递全在引擎与 router 那两头，
这里只负责「叫谁、叫几次、坏了怎么办」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sams.core.errors import SamsError
from sams.core.rules import is_intraday
from sams.services.quotes import QuoteBoard
from sams.storage.repos.rules import RuleRepo

logger = logging.getLogger("sams.services.rules.intraday")


@dataclass(frozen=True)
class IntradayRound:
    """一轮的账。`rules` 是这一趟**看过**几条（不是触发几条），`hits` 才是触发。"""

    markets: tuple[str, ...] = ()
    rules: int = 0
    hits: int = 0


class IntradayEvaluator:
    def __init__(self, rules: RuleRepo, engine, board: QuoteBoard) -> None:
        self.rules = rules
        self.engine = engine
        self.board = board

    def after_poll(self, outcomes: tuple) -> IntradayRound:
        """一轮报价之后的求值。有报价的市场才叫「活」：skip 与 fail 的市场不动。"""
        live = sorted(
            {
                poll.market
                for poll in outcomes
                if poll.result is not None and poll.result.quotes
            }
        )
        if not live:
            return IntradayRound()
        try:
            enabled = [
                rule
                for rule in self.rules.list_enabled()
                if is_intraday(rule.kind)
            ]
        except SamsError:
            # 读不到启用规则等于这一趟全盲：记下来，别让轮询线程跟着死
            logger.exception("盘中规则评估这一轮失败：读不到启用的规则")
            return IntradayRound(markets=tuple(live))
        users = sorted({rule.user_id for rule in enabled})
        hits = 0
        for user_id in users:
            for market in live:
                try:
                    report = self.engine.check(
                        user_id, market=market, intraday=True, quotes=self.board
                    )
                except SamsError:
                    # 一个用户/一个市场的失败不该吞掉其余的：下一轮还会再来
                    logger.exception("%s/%s 这一趟盘中求值失败", user_id, market)
                    continue
                hits += len(report.raised)
        return IntradayRound(markets=tuple(live), rules=len(enabled), hits=hits)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/services/test_rules_intraday_evaluator.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/services/rules/intraday.py tests/services/test_rules_intraday_evaluator.py --no-cache
git add src/sams/services/rules/intraday.py tests/services/test_rules_intraday_evaluator.py
git commit -m "feat(rules): IntradayEvaluator 把每轮报价接进引擎（§5.3）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 11: 轮询之后的挂钩（`services/intraday.py`）

**Files:**
- Modify: `src/sams/services/intraday.py`（plan-1 Task 10 建的 `IntradayMonitor`）
- Test: `tests/services/test_intraday_on_poll.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_intraday_on_poll.py`：

```python
"""on_poll 这道缝：求值挂在 poll_once 之后。坏了只许记日志，不许拖垮轮询。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import SamsError
from sams.core.quotes import Quote
from sams.services.intraday import IntradayMonitor
from sams.services.quotes import QuotePollResult
from support import FrozenClock

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
QUOTE = Quote(market="A", code="601088", ts=NOW, price=9.4, source="tencent")


class _Board:
    def __init__(self) -> None:
        self.skipped: list[tuple[str, str]] = []

    def skip(self, market, reason, *, now):
        self.skipped.append((market, reason))

    def fail(self, market, error, *, now):
        self.failed = (market, error)


class _Quotes:
    def __init__(self) -> None:
        self.board = _Board()

    def poll(self, market, codes, *, now=None):
        return QuotePollResult(
            quotes=(QUOTE,), skipped=(), errors=(), sources=()
        )


class _Calendar:
    def is_trading_day(self, market, day):
        return True

    def is_market_open(self, market, moment):
        return True


class _Universe:
    def build(self):
        # poll_once 先问 build() 拿到「此刻的宇宙」，再逐市场问 codes
        return self

    def codes(self, market):
        return ("601088",)


def monitor(settings, on_poll) -> IntradayMonitor:
    return IntradayMonitor(
        settings, _Quotes(), _Calendar(), _Universe(),
        clock=FrozenClock(NOW), on_poll=on_poll,
    )


def test_poll_once_calls_on_poll_with_the_round(settings):
    seen: list = []
    got = monitor(settings, seen.append).poll_once(now=NOW)
    assert len(seen) == 1
    assert seen[0] == got and len(got) == 1 and got[0].market == "A"


def test_a_sams_error_from_the_hook_does_not_break_the_poll(settings):
    def broken(outcomes):
        raise SamsError("评估这一轮做不成")

    got = monitor(settings, broken).poll_once(now=NOW)  # 不抛
    assert len(got) == 1


def test_a_programming_error_still_propagates(settings):
    def broken(outcomes):
        raise RuntimeError("真 bug")

    with pytest.raises(RuntimeError):
        monitor(settings, broken).poll_once(now=NOW)


def test_without_a_hook_nothing_happens(settings):
    got = monitor(settings, None).poll_once(now=NOW)
    assert len(got) == 1
```

（`FrozenClock` 若没有 `today()`，给 `_Calendar`/轮询用不到它就无妨；`IntradayMonitor` 只调 `clock.now_utc()`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_intraday_on_poll.py -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'on_poll'`

- [ ] **Step 3: 实现**

`src/sams/services/intraday.py` 的 `IntradayMonitor`：

1. import 加 `from collections.abc import Callable`（`Sequence` 等已有则不重复）。
2. `__init__` 签名在 `interval_s=None` 之后加 `on_poll: Callable[[tuple], object] | None = None`，函数体加 `self.on_poll = on_poll`（放在 `self.interval_s = …` 之后）。
3. `poll_once` 的 `return tuple(outcomes)` 之前插：

```python
        if self.on_poll is not None:
            try:
                self.on_poll(tuple(outcomes))
            except SamsError:
                # 求值失败不回收这轮行情：板子上的价照常更新，下一轮还会再判
                logger.exception("盘中规则评估这一轮失败")
        return tuple(outcomes)
```

- [ ] **Step 4: 跑测试确认通过，轮询既有用例不红**

Run: `uv run pytest tests/services/test_intraday_on_poll.py -q && uv run pytest tests/services -q -k intraday`
Expected: PASS

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/services/intraday.py tests/services/test_intraday_on_poll.py --no-cache
git add src/sams/services/intraday.py tests/services/test_intraday_on_poll.py
git commit -m "feat(intraday): IntradayMonitor.on_poll 挂钩——求值失败不拖垮轮询"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 12: 发送拆成两步（`monitor/router.py`）

**Files:**
- Modify: `src/sams/monitor/router.py`
- Test: `tests/monitor/test_router.py`（追加用例）

- [ ] **Step 1: 写失败测试**

在 `tests/monitor/test_router.py` 末尾追加：

```python
# ---------------------------------------------------------------- 两步发送（§5.5）


def test_record_then_deliver_sends_the_same_thing_as_dispatch(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    direct = world.notify.dispatch(msg(), user_id=user.id)
    alert = world.notify.record(msg(), user_id=user.id)
    again = world.notify.deliver_existing(alert.id)
    assert [d.outcome for d in again.deliveries] == [d.outcome for d in direct.deliveries]
    first, second = world.made["wecom"].sent[-2:]
    assert second.body == first.body
    assert second.alert_id == alert.id


def test_quote_metadata_survives_the_trip_through_the_row(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    moment = datetime.now(UTC)
    alert = world.notify.record(
        msg(intraday=True, quote_ts=moment, quote_source="tencent"), user_id=user.id
    )
    row = world.alerts.get(alert.id)
    assert row.intraday is True and row.quote_source == "tencent"
    world.notify.deliver_existing(alert.id)
    sent = world.made["wecom"].sent[-1]
    assert sent.intraday is True
    assert sent.quote_ts == moment
    assert sent.quote_source == "tencent"


def test_a_context_provider_extends_the_body(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    alert = world.notify.record(msg(), user_id=user.id)
    world.notify.deliver_existing(
        alert.id, context_provider=lambda a: "当时你写过：这条线是神华的坑"
    )
    assert world.made["wecom"].sent[-1].body.endswith("当时你写过：这条线是神华的坑")


def test_abandon_writes_why_it_never_went_out(world):
    user = world.user()
    alert = world.notify.record(msg(), user_id=user.id)
    world.notify.abandon(alert.id, reason="通知队列已满：发送被丢弃")
    delivery = world.alerts.deliveries(alert.id)[0]
    assert delivery.outcome == "no_channel"
    assert "队列已满" in delivery.error


def test_abandon_without_a_reason_is_refused(world):
    user = world.user()
    alert = world.notify.record(msg(), user_id=user.id)
    with pytest.raises(ValidationError):
        world.notify.abandon(alert.id, reason="  ")


def test_deliver_existing_refuses_an_unknown_alert(world):
    with pytest.raises(StorageError):
        world.notify.deliver_existing("nope")
```

文件头 import 补两处：`sams.core.errors` 那行加 `StorageError`；`from sams.storage.repos.alerts import AlertRepo` 已有则不动。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/monitor/test_router.py -q`
Expected: FAIL，`AttributeError: 'NotifyService' object has no attribute 'record'`

- [ ] **Step 3: 实现**

`src/sams/monitor/router.py`：

1. import 补：`sams.core.errors` 那行加 `StorageError`；`from sams.storage.repos.alerts import Alert`（与 `AlertRepo` 同行补名即可）。
2. `dispatch`（108 行起）整体替换为三个方法 + `dispatch` 变两步：

```python
    def record(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids: Sequence[str] = (),
        rule_id: str | None = None,
        account_id: str | None = None,
        is_test: bool = False,
    ) -> Alert:
        """只落库，不发送：发送在 `deliver_existing`，两者之间可以隔着一条队列。

        `is_test` 是 `/settings/test-alert` 那条链的标记（D131）：测试告警不进
        `no_open_alert` 与健康积压，收件箱里带「测试」徽标。
        """
        user = self.users.get(user_id)
        if user is None:
            raise ValidationError(f"没有用户 {user_id}：告警没有接收人就不能发")
        return self.alerts.create(
            user_id,
            rule_id=rule_id,
            rule_name=message.rule_name,
            account_id=account_id,
            account_name=message.account_name,
            market=message.market,
            code=message.code,
            symbol_name=message.name,
            severity=message.severity,
            stage=message.stage,
            value_text=message.value_text,
            threshold_text=message.threshold_text,
            action_note=message.action_note,
            triggered_at=message.triggered_at,
            channel_ids=list(channel_ids),
            extra={"body": message.body},
            is_test=is_test,
            intraday=message.intraday,
            quote_ts=message.quote_ts,
            quote_source=message.quote_source,
        )

    def deliver_existing(
        self, alert_id: str, *, context_provider: Callable[[Alert], str] | None = None
    ) -> DispatchResult:
        """把库里已有的一条告警走完路由与投递。正文从库里重建，不信任内存里的对象。

        `context_provider` 是 M4-B 的缝：把「这条论当时的批注」接在正文后面，
        队列这一侧不需要知道上下文长什么样。
        """
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise StorageError(f"告警 {alert_id} 不存在：发送必须落在一条真实告警上")
        message = _message_from(alert)
        note = context_provider(alert) if context_provider is not None else ""
        if note:
            message = replace(message, body=f"{message.body}\n{note}")
        user = self.users.get(alert.user_id)
        if user is None:
            raise ValidationError(f"没有用户 {alert.user_id}：告警没有接收人就不能发")
        dnd = self._dnd_note(user, message)
        if dnd:
            self.alerts.record(alert.id, None, SUPPRESSED, error=dnd, attempts=0)
            return DispatchResult(alert.id, (DeliveryReport("", None, SUPPRESSED, dnd, 0),))
        targets, refusal = self._route(user, message.severity, list(alert.channel_ids))
        if not targets:
            self.alerts.record(alert.id, None, NO_CHANNEL, error=refusal, attempts=0)
            return DispatchResult(alert.id, (DeliveryReport("", None, NO_CHANNEL, refusal, 0),))
        return DispatchResult(
            alert.id, tuple(self._deliver(alert.id, message, node) for node in targets)
        )

    def abandon(self, alert_id: str, *, reason: str) -> None:
        """宣告放弃：这条告警不再会发了，但那一行留在收件箱里可以重发。"""
        text = (reason or "").strip()
        if not text:
            raise ValidationError(
                "abandon 必须带原因：屏幕上那句「为什么没发出去」要能指到一件事"
            )
        self.alerts.record(alert_id, None, NO_CHANNEL, error=text, attempts=0)

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids: Sequence[str] = (),
        rule_id: str | None = None,
        account_id: str | None = None,
    ) -> DispatchResult:
        """记录这条告警，然后把它送出去。**只写 `notify_log`，不碰 `alerts.status`**。"""
        alert = self.record(
            message,
            user_id=user_id,
            channel_ids=channel_ids,
            rule_id=rule_id,
            account_id=account_id,
        )
        return self.deliver_existing(alert.id)
```

3. 模块级函数（`DND_WORD` 常量附近）：

```python
def _message_from(alert: Alert) -> AlertMessage:
    """从库里那一行重建要发的话：发送可以发生在写入之后很久（队列、重发）。"""
    return AlertMessage(
        market=alert.market,
        code=alert.code,
        severity=alert.severity,
        triggered_at=alert.triggered_at,
        name=alert.symbol_name,
        account_name=alert.account_name,
        rule_name=alert.rule_name,
        value_text=str(alert.payload.get("value_text") or ""),
        threshold_text=str(alert.payload.get("threshold_text") or ""),
        action_note=str(alert.payload.get("action_note") or ""),
        stage=alert.stage,
        body=str((alert.payload.get("extra") or {}).get("body") or ""),
        alert_id=alert.id,
        intraday=alert.intraday,
        quote_ts=alert.quote_ts,
        quote_source=alert.quote_source,
    )
```

- [ ] **Step 4: 跑测试确认通过，全 router 面不红**

Run: `uv run pytest tests/monitor/test_router.py -q`
Expected: PASS（CLI 的 `dispatch` 行为不变——它就是 `record + deliver_existing`）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/monitor/router.py tests/monitor/test_router.py --no-cache
git add src/sams/monitor/router.py tests/monitor/test_router.py
git commit -m "feat(notify): 发送拆成 record/deliver_existing/abandon，正文从库里重建（§5.5）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 13: 通知队列与工作线程（`monitor/queue.py`）

**Files:**
- Create: `src/sams/monitor/queue.py`
- Test: `tests/monitor/test_queue.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/monitor/test_queue.py`：

```python
"""队列的三个承诺：满不阻塞、逐单隔离、停机排得干（§5.5）。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sams.core.errors import StorageError
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import AlertMessage
from sams.monitor.queue import (
    AlertJob,
    AlertQueue,
    CallbackJob,
    NotifyWorker,
    QueuedAlertSender,
    SystemAlarmRelay,
)


class FakeNotify:
    def __init__(self) -> None:
        self.recorded: list[AlertMessage] = []
        self.abandoned: list[tuple[str, str]] = []
        self.delivered: list[str] = []
        self.next_id = 1

    def record(
        self, message, *, user_id, channel_ids=(), rule_id=None, account_id=None,
        is_test=False,
    ):
        alert_id = f"a{self.next_id}"
        self.next_id += 1
        self.recorded.append(message)
        return SimpleNamespace(id=alert_id)

    def abandon(self, alert_id, *, reason):
        self.abandoned.append((alert_id, reason))

    def deliver_existing(self, alert_id, *, context_provider=None):
        self.delivered.append(alert_id)


def msg():
    return AlertMessage(
        market="A",
        code="601088",
        severity="mid",
        triggered_at=datetime.now(UTC),
        name="中国神华",
        account_name="主账户",
        rule_name="PR 低",
        value_text="PR=0.31",
        threshold_text="PR≤0.4",
        action_note="按计划处置",
        stage="hit",
        body="正文",
    )


def test_offer_reports_full_instead_of_raising():
    q = AlertQueue(maxsize=1)
    assert q.offer(AlertJob("a1")) is True
    assert q.offer(AlertJob("a2"), timeout=0.01) is False
    assert q.qsize() == 1
    assert q.get().alert_id == "a1"


def test_sender_records_then_offers():
    notify = FakeNotify()
    q = AlertQueue()
    sender = QueuedAlertSender(notify, q)
    alert_id = sender.dispatch(msg(), user_id="u1")
    assert alert_id == "a1"
    assert q.qsize() == 1 and notify.delivered == []  # 记了、递了工作单、没发
    q.get().run(notify)
    assert notify.delivered == ["a1"]


def test_a_full_queue_abandons_instead_of_losing_the_alert():
    notify = FakeNotify()
    q = AlertQueue(maxsize=1)
    q.offer(AlertJob("occupied"))
    sender = QueuedAlertSender(notify, q)
    alert_id = sender.dispatch(msg(), user_id="u1")
    assert alert_id == "a2"  # 引擎那一侧的账不因投递方式而不同
    assert len(notify.abandoned) == 1
    assert "队列已满" in notify.abandoned[0][1]
    assert "重发" in notify.abandoned[0][1]


def test_worker_drains_after_stop():
    notify = FakeNotify()
    q = AlertQueue()
    for i in range(3):
        q.offer(AlertJob(f"a{i}"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=5.0).run(stop=stop)
    assert sorted(notify.delivered) == ["a0", "a1", "a2"]
    assert q.empty()


def test_worker_gives_up_when_the_deadline_passes():
    class SlowNotify(FakeNotify):
        def deliver_existing(self, alert_id, *, context_provider=None):
            time.sleep(0.05)
            super().deliver_existing(alert_id)

    notify = SlowNotify()
    q = AlertQueue()
    for i in range(100):
        q.offer(AlertJob(f"a{i}"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=0.1).run(stop=stop)
    assert q.qsize() > 0  # 排水到点就走：剩下的留在队列里，进程退出不为一杯牛奶等牛


def test_a_callback_job_runs_its_handle_in_the_worker():
    seen: list[object] = []
    notify = FakeNotify()
    q = AlertQueue()
    q.offer(CallbackJob(seen.append, "alarm"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=2.0).run(stop=stop)
    assert seen == ["alarm"]


def test_one_bad_job_does_not_stop_the_worker():
    notify = FakeNotify()
    calls: list[str] = []

    def deliver(alert_id, *, context_provider=None):
        calls.append(alert_id)
        if alert_id == "bad":
            raise StorageError("库坏了")

    notify.deliver_existing = deliver
    q = AlertQueue()
    q.offer(AlertJob("bad"))
    q.offer(AlertJob("good"))
    stop = threading.Event()
    stop.set()
    NotifyWorker(notify, q, drain_s=2.0).run(stop=stop)
    assert calls == ["bad", "good"]


def test_system_alarms_land_in_the_queue_as_callback_jobs():
    bus = EventBus()
    q = AlertQueue()
    handle_calls: list[SystemAlarm] = []
    SystemAlarmRelay(q, handle_calls.append).attach(bus)
    bus.publish(SystemAlarm(component="akshare", message="连挂三次", ts=datetime.now(UTC)))
    assert q.qsize() == 1  # 订阅回调只入队：EventBus 的订阅方跑在发布线程里
    job = q.get()
    assert isinstance(job, CallbackJob)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/monitor/test_queue.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'sams.monitor.queue'`

- [ ] **Step 3: 实现**

新建 `src/sams/monitor/queue.py`：

```python
"""把「发送」挪出求值线程的那条队列（spec §5.5）。

规则引擎那一侧只做两件快的事：告警落库、offer 进队列。网络重试、免打扰、路由
全在通知线程里跑。队列满 ⇒ abandon：告警留在收件箱可以重发，而阻塞求值线程会让
同一轮里排在后面的市场全等它。

这一层只认识 core 与 storage：monitor 内部的分层红线（`tests/test_layering.py`）。
"""

from __future__ import annotations

import logging
import queue as queue_mod
import time
from collections.abc import Callable
from dataclasses import dataclass

from sams.core.errors import SamsError
from sams.core.events import EventBus, SystemAlarm

logger = logging.getLogger("sams.monitor.queue")

DEFAULT_QUEUE_SIZE = 1000
OFFER_TIMEOUT_S = 1.0
GET_TIMEOUT_S = 0.5
#: 停机后还愿意等多久把剩下的发完。超时没发完的留在队列里：进程退出不该为一杯牛奶等牛
DRAIN_S = 10.0


@dataclass(frozen=True)
class AlertJob:
    """「把库里这一条告警发完」的工作单。只有 id：正文从库里重建，不背内存对象。"""

    alert_id: str

    def run(self, notify) -> None:
        notify.deliver_existing(self.alert_id)


@dataclass(frozen=True)
class CallbackJob:
    """系统告警的搬运单（`SystemAlarmRelay`）：真正的处理在通知线程里跑。"""

    callback: Callable[[object], None]
    event: object

    def run(self, notify) -> None:
        self.callback(self.event)


class AlertQueue:
    """有界队列的一层皮：`offer` 把「满」折成 False，让调用方好决策。"""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue: queue_mod.Queue = queue_mod.Queue(maxsize=maxsize)

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def offer(self, item, *, timeout: float = OFFER_TIMEOUT_S) -> bool:
        try:
            self._queue.put(item, timeout=timeout)
        except queue_mod.Full:
            return False
        return True

    def get(self, *, timeout: float = GET_TIMEOUT_S):
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()


class QueuedAlertSender:
    """`AlertSender` 的队列版：落库 + offer，两步都不碰网络。

    队列满时不是丢告警：`abandon` 落一行 no_channel，理由里写清「可在收件箱重发」。
    两种结局都返回 alert_id——引擎那一侧的账（去重键、冷却）不因投递方式而不同。
    """

    def __init__(self, notify, queue: AlertQueue) -> None:
        self.notify = notify
        self.queue = queue

    def dispatch(
        self,
        message,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
    ) -> str:
        alert = self.notify.record(
            message,
            user_id=user_id,
            channel_ids=list(channel_ids),
            rule_id=rule_id,
            account_id=account_id,
        )
        if self.queue.offer(AlertJob(alert.id)):
            return alert.id
        self.notify.abandon(
            alert.id,
            reason=(
                f"通知队列已满（上限 {self.queue.maxsize}）：发送被丢弃，"
                "告警已落库，可在收件箱重发"
            ),
        )
        return alert.id


class NotifyWorker:
    """通知线程的主循环：取工作单 → 交给 NotifyService。停机后排水 ≤ DRAIN_S。"""

    def __init__(self, notify, queue: AlertQueue, *, drain_s: float = DRAIN_S) -> None:
        self.notify = notify
        self.queue = queue
        self.drain_s = drain_s

    def run(self, *, stop) -> None:
        deadline: float | None = None
        while True:
            if stop.is_set() and deadline is None:
                deadline = time.monotonic() + self.drain_s
            try:
                job = self.queue.get(timeout=GET_TIMEOUT_S)
            except queue_mod.Empty:
                if deadline is not None and (
                    self.queue.empty() or time.monotonic() >= deadline
                ):
                    return
                continue
            try:
                job.run(self.notify)
            except SamsError:
                # 一条发不出去不该把线程带走：后面还有全市场的告警在排队
                logger.exception("通知工作单 %r 失败", job)
            if deadline is not None and (
                self.queue.empty() or time.monotonic() >= deadline
            ):
                return


class SystemAlarmRelay:
    """总线上的系统告警 → 队列。订阅回调只入队：EventBus 的订阅方跑在发布线程里，
    在那里发网络请求会把每个发布者都拖慢。"""

    def __init__(self, queue: AlertQueue, handle: Callable[[object], None]) -> None:
        self._queue = queue
        self._handle = handle

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(SystemAlarm, self._on_alarm)

    def _on_alarm(self, event) -> None:
        self._queue.offer(CallbackJob(self._handle, event))
```

- [ ] **Step 4: 跑测试确认通过，monitor 全绿**

Run: `uv run pytest tests/monitor -q`
Expected: PASS

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/monitor/queue.py tests/monitor/test_queue.py --no-cache
uv run pytest tests/test_layering.py -q
git add src/sams/monitor/queue.py tests/monitor/test_queue.py
git commit -m "feat(notify): 有界告警队列、队列版 sender 与通知线程（§5.5）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 14: 写面接上版本闸（maintenance / derive / theses / ledger / scheduler）

**Files:**
- Modify: `src/sams/services/rules/maintenance.py`
- Modify: `src/sams/services/rules/derive.py`
- Modify: `src/sams/services/theses.py`
- Modify: `src/sams/services/ledger.py`
- Modify: `src/sams/services/scheduler/types.py`
- Modify: `src/sams/services/scheduler/jobs.py`
- Test: `tests/services/test_version_bumps.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/services/test_version_bumps.py`：

```python
"""写面动过谁，版本闸就得响谁：盘中缓存整表失效的输入就是这几次 bump。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.version import VersionGate
from sams.services.ledger import LedgerService
from sams.services.rules.derive import RuleDeriver
from sams.services.rules.maintenance import RuleService
from sams.services.scheduler.types import JobDeps
from sams.storage.crypto import cipher_for
from sams.storage.repos.channels import ChannelRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

T0 = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)
NAME, PWD = "u-bump", "long-enough-to-hash"


class NoTargets:
    def expand(self, rule):
        return []


def make_service(db, settings, gate) -> RuleService:
    return RuleService(
        rules=RuleRepo(db),
        theses=ThesisRepo(db),
        accounts=SimAccountRepo(db),
        channels=ChannelRepo(db, cipher=cipher_for(settings)),
        users=UserRepo(db),
        data=NoTargets(),
        db=db,
        settings=settings,
        versions=gate,
    )


def test_rule_writes_bump_the_rules_version(db, settings):
    gate = VersionGate()
    service = make_service(db, settings, gate)
    uid = service.users.create(NAME, PWD).id
    base = gate.current("rules")
    rule = service.add(
        uid,
        kind="stop.fixed",
        name="跌破 10 元",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
    )
    assert gate.current("rules") == base + 1
    service.set_enabled(uid, rule.id, False)
    assert gate.current("rules") == base + 2
    service.remove(uid, rule.id)
    assert gate.current("rules") == base + 3


def test_edit_bumps_too(db, settings):
    gate = VersionGate()
    service = make_service(db, settings, gate)
    uid = service.users.create(NAME, PWD).id
    rule = service.add(
        uid,
        kind="stop.fixed",
        name="线",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
    )
    base = gate.current("rules")
    service.edit(uid, rule.id, params={"price": 11.0})
    assert gate.current("rules") == base + 1


def test_deriver_bumps_only_when_it_created_something(db, settings):
    gate = VersionGate()
    theses = ThesisRepo(db)
    user = UserRepo(db).create(NAME, PWD)
    thesis = theses.create(
        user_id=user.id,
        market="A",
        code="601088",
        style="value",
        actor_id=user.id,
        now=T0,
        status="proposed",
        entry_low=32.0,
        entry_high=36.0,
        stop_price=28.0,
        target_price=52.0,
    )
    deriver = RuleDeriver(RuleRepo(db), theses, versions=gate)
    first = deriver.derive(user.id, thesis.id)
    assert first.created and gate.current("rules") == 1
    deriver.derive(user.id, thesis.id)
    assert gate.current("rules") == 1  # 第二趟全是 already：库没变，不该把缓存全清


def test_ledger_writes_bump_the_ledger_version(db, settings):
    gate = VersionGate()
    users = UserRepo(db)
    user = users.create(NAME, PWD)
    service = LedgerService(
        SimAccountRepo(db), SimOrderRepo(db), ThesisRepo(db), users, settings,
        versions=gate,
    )
    base = gate.current("ledger")
    account = service.open_account(NAME, "SIM-1")
    assert gate.current("ledger") == base + 1
    service.deposit(
        account.id, "CNY", 10000.0, username=NAME, reason="初始入金", actor_id=user.id
    )
    assert gate.current("ledger") == base + 2


def test_jobdeps_carries_a_gate_by_default():
    deps = JobDeps(
        settings=None, registry=None, sync=None, corp_actions=None, fundamentals=None,
        fx=None, calendar=None, rules=None, recheck=None, users=None,
    )
    assert isinstance(deps.versions, VersionGate)
```

（`theses.create` 全部关键字与仓库签名一致：`user_id/market/code/style/actor_id/now/status`
加四个价位 levels。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_version_bumps.py -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'versions'`

- [ ] **Step 3: 实现**

1. `src/sams/services/rules/maintenance.py`：
   - import 加 `from sams.core.version import VersionGate`。
   - `__init__` 的 `clock: Clock = SYSTEM_CLOCK` 之后加 `versions: VersionGate | None = None`，函数体在 `self.clock = clock` 之后加 `self.versions = versions`。
   - 类里加：

```python
    def _bump(self) -> None:
        """写面动过就 bump：盘中缓存整表失效（§5.4）。没接闸的构造点静默跳过。"""
        if self.versions is not None:
            self.versions.bump("rules")
```

   - 四个写方法各加一行（放在各自的 return 之前）：
     - `add`：`return self.view(rule)` 改为

```python
        self._bump()
        return self.view(rule)
```

     - `edit`：`with self.db.transaction():` 块结束之后、`return EditReport(...)` 之前加 `self._bump()`。
     - `set_enabled`：

```python
    def set_enabled(self, user_id: str, rule_id: str, enabled: bool) -> RuleView:
        row = self._require(user_id, rule_id)
        moved = self.rules.set_enabled(row.id, bool(enabled), user_id=user_id)
        self._bump()
        return self.view(moved)
```

     - `remove`：

```python
    def remove(self, user_id: str, rule_id: str) -> bool:
        row = self._require(user_id, rule_id)
        removed = self.rules.remove(row.id, user_id=user_id)
        self._bump()
        return removed
```

2. `src/sams/services/rules/derive.py`：
   - import 加 `from sams.core.version import VersionGate`。
   - `__init__(self, rules, theses)` 改为 `def __init__(self, rules, theses, *, versions: VersionGate | None = None) -> None:`，函数体加 `self.versions = versions`。
   - `derive()` 的 `return DeriveReport(...)` 之前加：

```python
        if created and self.versions is not None:
            # 只有真建了规则才 bump：第二趟全是 already，库没变，不该把缓存全清
            self.versions.bump("rules")
```

3. `src/sams/services/theses.py`：
   - import 加 `from sams.core.version import VersionGate`。
   - `__init__(self, theses, users, valuation, settings, clock=SYSTEM_CLOCK, orders=None)` 在 `orders=None` 之后加 `versions: VersionGate | None = None`，函数体在 `self.orders = orders` 之后加 `self.versions = versions`。
   - 类里加（与 maintenance 同一句理由，bump 的是 `"theses"`）：

```python
    def _bump(self) -> None:
        if self.versions is not None:
            self.versions.bump("theses")
```

   - `add`：`thesis = self.theses.create(...)` 之后、`return self._report(...)` 之前加 `self._bump()`。
   - `advance`：

```python
        user = self._actor(username)
        moved = self.theses.advance(
            user.id, thesis_id, to, actor_id=user.id, now=self.clock.now_utc(), note=note
        )
        self._bump()
        return moved
```

   - `revise`：`thesis = self.theses.set_levels(...)` 之后、`return self._report(...)` 之前加 `self._bump()`。

4. `src/sams/services/ledger.py`：
   - import 加 `from sams.core.version import VersionGate`。
   - `__init__(self, accounts, orders, theses, users, settings)` 末尾加 `*, versions: VersionGate | None = None`，函数体加 `self.versions = versions`。
   - 类里加 `_bump`（bump 的是 `"ledger"`，docstring 与 maintenance 同句）。
   - `open_account` / `deposit` / `withdraw`：把直接 `return self.accounts.open(...)` / `return self.accounts.adjust_cash(...)` 改成先接住再 bump：

```python
        account = self.accounts.open(
            …原有实参全部不动…
        )
        self._bump()
        return account
```

```python
        self._require_account(resolve_actor(self.users, username), account_id)
        balance = self.accounts.adjust_cash(
            account_id, currency, abs(amount), reason=reason, actor_id=actor_id
        )
        self._bump()
        return balance
```

（`withdraw` 那处照抄，只是 `abs(amount)` 换成 `-abs(amount)`——符号语义原样保留。）

   - `_fill`：`return self.orders.record_fill(...)` 改为

```python
        order = self.orders.record_fill(
            …原有实参全部不动…
        )
        self._bump()
        return order
```

5. `src/sams/services/scheduler/types.py`：
   - import：把 `from dataclasses import dataclass` 补成 `from dataclasses import dataclass, field`；另加 `from sams.core.version import VersionGate`。
   - `JobDeps` 最后一个字段（`users` 之后）加：

```python
    users: UserRepo
    #: 盘中缓存的失效闸：日线/公司行为写入后 bump，缓存整表清（§5.4）
    versions: VersionGate = field(default_factory=VersionGate)
```

6. `src/sams/services/scheduler/jobs.py` 的 `run_bars` / `run_actions` 末行各改为：

```python
    outcome = _collect(_units(ctx), one, "日线")
    if outcome.rows_affected > 0:
        deps.versions.bump("bars")
    return outcome
```

```python
    outcome = _collect(_units(ctx), one, "公司行为")
    if outcome.rows_affected > 0:
        # 公司行为改的是复权口径：缓存的日线读数同样作废
        deps.versions.bump("bars")
    return outcome
```

（这两行不单独写用例：`test_jobdeps_carries_a_gate_by_default` 保证 `deps.versions` 永远在手，行为由全量套件与 Task 15 的端到端兜住。）

- [ ] **Step 4: 跑测试确认通过，四个服务的既有用例全绿**

Run: `uv run pytest tests/services/test_version_bumps.py tests/services/test_rules_maintenance.py tests/services/test_rules_derive.py tests/services/test_theses.py tests/services/test_ledger.py -q`
Expected: PASS（文件名若不同，用 `uv run pytest tests/services -q` 全跑）

- [ ] **Step 5: 检查与提交**

```bash
uv run ruff check src/sams/services tests/services/test_version_bumps.py --no-cache
git add src/sams/services/rules/maintenance.py src/sams/services/rules/derive.py src/sams/services/theses.py src/sams/services/ledger.py src/sams/services/scheduler/types.py src/sams/services/scheduler/jobs.py tests/services/test_version_bumps.py
git commit -m "feat(services): 全部写面接上 VersionGate——盘中缓存失效的输入（§5.4）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

### Task 15: 装配（`bootstrap.py`、`services/runtime.py`）与端到端

**Files:**
- Modify: `src/sams/services/runtime.py`（plan-1 Task 14 建的那份）
- Modify: `src/sams/bootstrap.py`
- Modify: `tests/services/test_runtime.py`
- Modify: `tests/test_bootstrap.py`（含端到端新用例）

- [ ] **Step 1: Runtime 的第四条线程（`src/sams/services/runtime.py`）**

1. import 加 `from typing import Protocol`；在 `Runtime` 类之前加：

```python
class _Worker(Protocol):
    """通知线程的形状（结构化）：`NotifyWorker` 与测试替身各长各的。"""

    def run(self, *, stop: threading.Event) -> None: ...
```

2. `__init__` 的关键字参数在 `prune: PruneService,` 之后加 `worker: _Worker | None = None,`，函数体在 `self.prune = prune` 之后加 `self.worker = worker`。
3. `start()`：三线程字典之后、`for thread in self._threads.values():` 之前插：

```python
        if self.worker is not None:
            self._threads["notify"] = threading.Thread(
                target=self.worker.run,
                kwargs={"stop": self.stop_event},
                name="sams-notify",
                daemon=True,
            )
```

4. `stop()`：`timeouts` 字典加一项 `"notify": WORKER_STOP_TIMEOUT_S,`；`for name in (...)` 改为：

```python
        for name in ("scheduler", "intraday", "notify", "maintenance"):
```

（notify 排在 intraday 之后、maintenance 之前：先停产出告警的线程，再排空队列，最后才轮到维护。）

- [ ] **Step 2: 更新 Runtime 测试（`tests/services/test_runtime.py`）**

1. 文件里加替身（`FakePrune` 附近）：

```python
class FakeWorker:
    """通知线程的替身：形状照 `_Worker` 协议，记录起止。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self, *, stop):
        self.started.set()
        stop.wait(5)
        self.stopped.set()
```

2. `make_runtime` 的关键字参数加 `worker=None`，构造 `Runtime(...)` 的调用处加 `worker=worker,`。
3. 把 `test_start_runs_three_threads_and_stop_joins_them` 整个替换为：

```python
def test_start_runs_four_threads_and_stop_joins_them(settings):
    scheduler = FakeScheduler()
    monitor = FakeMonitor()
    prune = FakePrune()
    worker = FakeWorker()
    runtime = make_runtime(
        settings, scheduler=scheduler, monitor=monitor, prune=prune, worker=worker
    )

    runtime.start()
    assert scheduler.started.wait(2) and monitor.started.wait(2)
    assert prune.called.wait(2), "维护线程启动即清一次"
    assert set(runtime.threads) == {"scheduler", "intraday", "notify", "maintenance"}

    runtime.stop()
    assert scheduler.stopped.wait(2) and monitor.stopped.wait(2)
    assert worker.started.is_set() and worker.stopped.is_set()
    assert not any(thread.is_alive() for thread in runtime.threads.values())
    runtime.stop()  # 幂等：停机流程被重复调用不该抛


def test_without_a_worker_there_is_no_notify_thread(settings):
    runtime = make_runtime(settings)
    runtime.start()
    assert "notify" not in runtime.threads
    runtime.stop()
```

Run: `uv run pytest tests/services/test_runtime.py -q`
Expected: PASS

- [ ] **Step 3: bootstrap 装配（`src/sams/bootstrap.py`）**

1. import 区追加：

```python
from sams.core.version import VersionGate
from sams.monitor.queue import AlertQueue, NotifyWorker, QueuedAlertSender, SystemAlarmRelay
from sams.services.rules.cache import SnapshotCache
from sams.services.rules.intraday import IntradayEvaluator
```

2. `AppCore`：在 plan-1 加的 `board: QuoteBoard` 字段之后追加两个字段：

```python
    #: 进程级共享：三条线程的写面与盘中线程的缓存闸拿的是同一份
    versions: VersionGate
    #: 告警队列：引擎侧 offer、通知线程取——四条线程里只有它横跨三条
    alerts_queue: AlertQueue
```

`build_core` 的 return 处对应加 `versions=VersionGate(), alerts_queue=AlertQueue(),`。

3. `build_context`：签名在 `notifier_factory=None` 之后加 `queue: AlertQueue | None = None, cache: SnapshotCache | None = None`。函数体三处：
   - `sender = _NotifyAlertSender(notify)` 改为：

```python
    sender = (
        QueuedAlertSender(notify, queue)
        if queue is not None
        else _NotifyAlertSender(notify)
    )
```

   - RuleEngine 构造调用加一行 `cache=cache,`。
   - plan-1 的 `intraday = IntradayMonitor(cfg, quotes, calendar, universe_builder, clock=clock)` 改为：

```python
    on_poll = (
        None
        if queue is None
        else IntradayEvaluator(rules_store, rule_engine, core.board).after_poll
    )
    intraday = IntradayMonitor(
        cfg, quotes, calendar, universe_builder, clock=clock, on_poll=on_poll
    )
```

`build_container` 一行不动：它不传 queue/cache，CLI 仍然是直发路径。

4. `build_serve` 整体替换为（基于 plan-1 的版本改四处：三改四、队列与缓存、worker、系统告警走队列）：

```python
def build_serve(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    adapters: dict[str, BarAdapter] | None = None,
    providers: dict[str, CalendarProvider] | None = None,
    notifier_factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
) -> ServeParts:
    """`sams serve` 的装配：一条进程级 core + 四条线程上下文 + 一个运行时。

    四个上下文依序造、依序记在 `opened` 里：造到一半时炸掉，前面几条也得关。
    半成品留着连接时，重试会撞上自己的锁，而报错指的是「另一个进程」。
    """
    cfg = settings or Settings.load()
    core = build_core(cfg, clock=clock, bus=bus, adapters=adapters)
    opened: list[ServiceContext] = []
    try:
        for index in range(4):
            intraday = index == 1
            opened.append(
                build_context(
                    core,
                    providers=providers,
                    notifier_factory=notifier_factory,
                    # 只有盘中那一份走队列与缓存：其余三份照旧直发（§5.5）
                    queue=core.alerts_queue if intraday else None,
                    cache=SnapshotCache(core.versions) if intraday else None,
                )
            )
        scheduler_ctx, intraday_ctx, notify_ctx, maintenance_ctx = opened
        prune = PruneService(
            maintenance_ctx.db,
            quotes=maintenance_ctx.quotes_store,
            task_runs=maintenance_ctx.task_runs,
            heartbeats=maintenance_ctx.heartbeats,
            clock=core.clock,
        )
        worker = NotifyWorker(notify_ctx.notify, core.alerts_queue)
        runtime = Runtime(
            cfg,
            scheduler=scheduler_ctx.scheduler,
            # 每次新开连接的读法：HTTP 线程不借调度线程的连接（D113）
            schedule_reading=FreshScheduleProbe(
                cfg, specs=scheduler_ctx.scheduler.jobs, clock=core.clock
            ),
            monitor=intraday_ctx.intraday,
            prune=prune,
            board=core.board,
            database=FreshDbProbe(cfg),
            clock=core.clock,
            worker=worker,
        )
        # 系统告警也走队列：relay 把 `SystemAlarm` 折成工作单，`alerter.handle`
        # 在通知线程里跑。**只挂 relay 这一次**——`alerter.attach(bus)` 会把 handle
        # 直接订阅到发布线程上，两个都挂的话同一条系统告警会变成两条
        alerter = SystemAlerter(
            alerts=notify_ctx.alerts_store,
            users=notify_ctx.users,
            sender=QueuedAlertSender(notify_ctx.notify, core.alerts_queue),
            settings=cfg,
            clock=core.clock,
        )
        SystemAlarmRelay(core.alerts_queue, alerter.handle).attach(core.bus)
        return ServeParts(
            core=core,
            contexts=(scheduler_ctx, intraday_ctx, notify_ctx, maintenance_ctx),
            runtime=runtime,
        )
    except BaseException:
        for context in opened:
            context.close()
        core.close()
        raise
```

5. `ServeParts`：`contexts` 的类型注解改为四元组，docstring 的顺序说明改为「调度线程、盘中线程、通知线程、维护线程」：

```python
@dataclass
class ServeParts:
    """`sams serve` 的四条线程上下文与那一个 `Runtime`。

    `contexts` 的顺序就是创建顺序：调度线程、盘中线程、通知线程、维护线程。维护
    那一份只被用来建 `PruneService`（它的连接随后归维护线程独占），四份都不是
    「主上下文」；通知线程的 notify 仓库由 `NotifyWorker` 持有。
    """

    core: AppCore
    contexts: tuple[ServiceContext, ServiceContext, ServiceContext, ServiceContext]
    runtime: Runtime

    def prepare(self) -> int:
        """迁移、再灌板。顺序不能反：`load_board` 读的是 0019 的列。"""
        self.contexts[0].migrate()
        return self.contexts[1].quotes.load_board()

    def close(self) -> None:
        for context in self.contexts:
            context.close()
        self.core.close()
```

6. `attach_system_alerter` 的 docstring 更新一句（行为不动，CLI 那条路照旧）：
   「`serve` 在通知队列接入后由 `Runtime` 挂一次（计划 2）」改为
   「`serve` 那条挂在 `build_serve`：经 `SystemAlarmRelay` 只入队，处理在通知线程（计划 2）」。

- [ ] **Step 4: 装配测试与端到端（`tests/test_bootstrap.py`）**

1. 文件头 import 补齐（已有的不重复）：

```python
import threading
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.quotes import Quote
from sams.monitor.queue import QueuedAlertSender
from sams.services.intraday import MarketPoll
from sams.services.quotes import QuotePollResult
```

2. 把 plan-1 的 `test_serve_builds_three_contexts_on_one_core` 整个替换为：

```python
def test_serve_builds_four_contexts_on_one_core(migrated):
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        core, contexts, runtime = parts.core, parts.contexts, parts.runtime
        assert len(contexts) == 4
        assert len({id(ctx.db) for ctx in contexts}) == 4
        assert contexts[0].registry is contexts[1].registry
        assert contexts[0].bus is contexts[1].bus
        assert contexts[0].requests is contexts[1].requests
        assert contexts[1].quotes.board is core.board
        assert contexts[1].intraday.quotes is contexts[1].quotes
        assert contexts[1].intraday.on_poll is not None
        assert contexts[0].intraday.on_poll is None
        assert runtime.board is core.board
        assert runtime.monitor is contexts[1].intraday
        assert runtime.worker is not None
    finally:
        parts.close()


def test_only_the_intraday_context_sends_through_the_queue(migrated):
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        scheduler_ctx, intraday_ctx, notify_ctx, maintenance_ctx = parts.contexts
        assert isinstance(intraday_ctx.rule_engine.sender, QueuedAlertSender)
        assert not isinstance(scheduler_ctx.rule_engine.sender, QueuedAlertSender)
    finally:
        parts.close()
```

3. 文件末尾追加端到端（全链：板 → 闸 → 引擎 → 队列 → 通知线程 → 收件箱）：

```python
def test_an_intraday_hit_flows_from_the_board_to_the_queue(migrated):
    parts = build_serve(migrated, adapters=stub_adapters())
    try:
        parts.prepare()
        core = parts.core
        intraday_ctx = parts.contexts[1]
        notify_ctx = parts.contexts[2]

        user = intraday_ctx.users.create("trader", "correct-horse-battery")
        intraday_ctx.rules_store.create(
            user.id,
            kind="stop.fixed",
            name="跌破 10 元",
            target_type="symbol",
            target_ref="A/601088",
            params={"price": 10.0},
        )

        # 「今天」跟着报价的市场本地日走（引擎读日线就按它）：机器时区若是 UTC，
        # date.today() 与上海日可能差一天，日线就会落在 as_of 之外
        moment = datetime.now(UTC)
        today = moment.astimezone(ZoneInfo("Asia/Shanghai")).date()
        bars = pl.DataFrame(
            {
                "market": ["A"] * 3,
                "code": ["601088"] * 3,
                "date": [today - timedelta(days=2), today - timedelta(days=1), today],
                "open": [10.6, 10.5, 10.05],
                "high": [10.8, 10.6, 10.1],
                "low": [10.2, 10.1, 9.9],
                "close": [10.5, 10.4, 9.9],
                "volume": [1e6] * 3,
                "amount": [1e7] * 3,
                "source": ["tencent"] * 3,
            }
        )
        intraday_ctx.rule_data.bars.upsert(bars)

        quote = Quote(
            market="A",
            code="601088",
            ts=moment - timedelta(seconds=5),
            price=9.4,
            open=9.8,
            prev_close=10.2,
            source="tencent",
        )
        core.board.update("A", (quote,), now=moment)

        # 直接灌一轮结果：poll_once 的时段/宇宙闸在 Task 10 之前的用例里盖过了
        got = intraday_ctx.intraday.on_poll(
            (
                MarketPoll(
                    "A",
                    result=QuotePollResult(
                        quotes=(quote,), skipped=(), errors=(), sources=()
                    ),
                ),
            )
        )
        assert got.hits == 1
        assert core.alerts_queue.qsize() == 1

        rows = intraday_ctx.alerts_store.list_for(user.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.stage == "urgent"  # 开 9.8 < 线 10 ≤ 昨收 10.2：跳空破位
        assert row.intraday is True
        assert "报价" in row.payload["extra"]["body"]

        stop = threading.Event()
        stop.set()
        parts.runtime.worker.run(stop=stop)  # 同步排水：工作单全部跑完
        assert core.alerts_queue.empty()
        deliveries = notify_ctx.alerts_store.deliveries(row.id)
        assert deliveries and deliveries[0].outcome == "no_channel"  # 没配通道，落了账
    finally:
        parts.close()
```

- [ ] **Step 5: 跑装配与端到端**

Run: `uv run pytest tests/test_bootstrap.py tests/services/test_runtime.py -q`
Expected: PASS

- [ ] **Step 6: 全量离线套件与静态检查**

```bash
uv run pytest -q -m "not live"
uv run ruff check . --no-cache
```

Expected: 全绿、零告警。**再提醒一次：不许跑 `ruff format`。**

- [ ] **Step 7: 提交**

```bash
git status   # 只暂存下面点名的文件；未追踪的 tmp/、qodercli_resume.sh 不许进
git add src/sams/services/runtime.py src/sams/bootstrap.py tests/services/test_runtime.py tests/test_bootstrap.py
git commit -m "feat(serve): 四线程装配——队列、缓存、NotifyWorker 与系统告警入队（§5.5）"
git fetch origin main && git merge --ff-only origin/main && git push origin main
```

---

## 收尾核对（本计划做完后）

1. `uv run pytest -q -m "not live"` 全绿；`uv run ruff check . --no-cache` 零告警。
2. spec §5 的五节各有落点：§5.1 → Task 1；§5.2 → Tasks 2/3/4/5；§5.3 → Tasks 6/8/10/11；§5.4 → Tasks 9/14；§5.5 → Tasks 7/12/13/15。
3. M4-B 需要的三样都已是现成的缝：`deliver_existing(context_provider=…)`（Task 12）、`alerts.is_test` 全链（Task 7）、`record/abandon`（Task 12）。
4. 盘中链路上没有任何一条新迁移——0019（plan-1）已给 `alerts.is_test`。


