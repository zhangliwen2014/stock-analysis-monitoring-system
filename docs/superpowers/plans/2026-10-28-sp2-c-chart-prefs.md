# SP2 计划 C：每用户图表偏好 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 周期条的顺序与均线窗口按人存进库：设置页一个卡片能改、能清空；个股图打开时就用这个人自己那一份。

**Architecture:** 新表 `chart_prefs`（迁移 0025）一行一人，两列 JSON 短列表；没有行=用默认。
词表与校验只写一份（`web/chart_prefs.py`）：`CHART_PERIODS` 从 `pages/symbol.py` 搬进去，
`_windows` 改名为 `parse_windows` 一并搬过去，设置页保存与个股图局部渲染都念这一份。
`/api/partials/chart` 按偏好重排周期条，并把 `ma=` 的默认值填成这个人的窗口；
`bars` 接口不读偏好——显式参数永远赢，直接打接口的人拿到的是默认窗口。

**Tech Stack:** FastAPI + Jinja（CSP `'self'`，无内联脚本）、DuckDB、既有 `ThreadContexts` 装配。

**设计出处:** `docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md`
§7 计划 C（D162「`ma=` 串可从外给，落库成每人一份偏好」、D166 的「自己设置」）；迁移号按
SP3 计划 E 之后的实际最大值取 **0025**。

**执行约定（房规）:**
- 只在 `main` 上做，不建分支；`uv run` 一律走 uv；**绝不** `ruff format`；lint 命令是
  `uv run ruff check --no-cache src/sams tests`。
- 迁移编号用 **0025**（当前最大 0024）。
- 不用 `git add -A`/`git add .`；一次只加本任务涉及的文件。
- 提交信息用现有风格：`feat(chart): …` / `test(chart): …` / `fix(chart): …`。
- 一条行为一轮红绿：先写测试、跑出**正确原因的失败**，再写最小实现。
- 全量测试只由主控跑，不接 `head/tail/grep`。

---

## 文件结构（本计划要动的全部）

| 文件 | 责任 |
| --- | --- |
| `src/sams/web/chart_prefs.py` | **新建**。`CHART_PERIODS`（从 `symbol.py` 搬来）、`parse_windows`（原 `_windows`）、`parse_periods`、`ordered_periods`、`windows_text` |
| `src/sams/storage/migrations/0025_chart_prefs.sql` | **新建**。`chart_prefs(user_id PK, periods_json, ma_json, updated_at)` |
| `src/sams/storage/repos/chart_prefs.py` | **新建**。`ChartPref` + `ChartPrefRepo.get/save/clear` |
| `src/sams/bootstrap.py` | 上下文加 `chart_prefs`（一条连接一份仓库，D113） |
| `src/sams/web/pages/settings.py` | 「图表偏好」卡片 + `POST /settings/chart` |
| `src/sams/web/templates/settings.html` | 那一张卡片 |
| `src/sams/web/pages/symbol.py` | `CHART_PERIODS`/`_windows` 改为 import；`chart_partial` 读偏好 |
| `tests/storage/test_migrate.py` | `EXPECTED_TABLES` 加 `chart_prefs` |
| `tests/storage/test_chart_prefs.py` | **新建**。仓库往返、覆盖、清空、跨用户 |
| `tests/web/test_chart_prefs.py` | **新建**。纯函数的校验与重排 |
| `tests/web/test_chart_prefs_web.py` | **新建**。设置页卡片与图表局部 |

---

### Task 1: 词表与校验搬进 `web/chart_prefs.py`

**Files:**
- Create: `src/sams/web/chart_prefs.py`
- Modify: `src/sams/web/pages/symbol.py`（删 `CHART_PERIODS` 与 `_windows`，改为 import）
- Test: `tests/web/test_chart_prefs.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/web/test_chart_prefs.py`：

```python
"""图表偏好的词表：档名怎么排序、均线窗口怎么读。纯函数，不起 app。"""

import pytest

from sams.core.errors import ValidationError
from sams.web.chart_prefs import (
    CHART_PERIODS,
    ordered_periods,
    parse_periods,
    parse_windows,
    windows_text,
)

KEYS = tuple(name for name, _label in CHART_PERIODS)


def test_an_empty_pair_of_fields_means_the_defaults():
    assert parse_periods("", KEYS) == ()
    assert parse_windows("") == ()
    assert windows_text(()) == ""


def test_the_period_order_must_name_every_period_exactly_once():
    assert parse_periods(",".join(reversed(KEYS)), KEYS) == tuple(reversed(KEYS))
    with pytest.raises(ValidationError):
        parse_periods(",".join(KEYS[:-1]), KEYS)  # 少一档
    with pytest.raises(ValidationError):
        parse_periods(",".join([*KEYS, KEYS[0]]), KEYS)  # 多一档
    with pytest.raises(ValidationError):
        parse_periods("day,week,nope", KEYS)  # 不认识


def test_ma_windows_are_positive_unique_and_at_most_six():
    assert parse_windows(" 5, 10,20 ,5 ") == (5, 10, 20)
    assert windows_text((5, 10, 20)) == "5,10,20"
    for bad in ("0", "-5", "a", "1,2,3,4,5,6,7"):
        with pytest.raises(ValidationError):
            parse_windows(bad)


def test_a_stored_order_reorders_and_appends_periods_it_does_not_know():
    known = (("day", "日"), ("week", "周"), ("month", "月"))
    assert ordered_periods(known, ("month", "day")) == (
        ("month", "月"),
        ("day", "日"),
        ("week", "周"),
    )
    # 旧偏好里有一个已经下架的档：跳过它，不炸
    assert ordered_periods(known, ("gone", "week")) == (("week", "周"), ("day", "日"), ("month", "月"))
```

- [ ] **Step 2: 跑测试，确认是「模块不存在」的失败**

```bash
uv run pytest tests/web/test_chart_prefs.py -q
```

Expected: `ModuleNotFoundError: No module named 'sams.web.chart_prefs'`

- [ ] **Step 3: 新建 `src/sams/web/chart_prefs.py`**

```python
"""图表偏好的词表与校验（D193）：周期条顺序、均线窗口。

档名与窗口的规矩只写这一次：设置页保存、个股图局部渲染、`bars` 接口的白名单都念
这一份，屏幕上不会出现「存进去的档名图上不认」。
"""

from __future__ import annotations

from sams.core.errors import ValidationError

#: 周期条：(参数值, 屏幕上那几个字)。分时那一格是视图切换，不在这张表里
CHART_PERIODS: tuple[tuple[str, str], ...] = (
    ("m1", "1分"),
    ("m5", "5分"),
    ("m15", "15分"),
    ("m30", "30分"),
    ("m60", "60分"),
    ("m120", "120分"),
    ("day", "日"),
    ("week", "周"),
    ("month", "月"),
    ("quarter", "季"),
    ("year", "年"),
    ("5d", "5日"),
    ("10d", "10日"),
)

#: 均线窗口最多六条：与 `MA_WINDOWS` 的默认条数同一个上限
MAX_MA_WINDOWS = 6
_MA_MESSAGE = "均线窗口要 1..500 之间的正整数，最多六条"


def parse_windows(raw: str) -> tuple[int, ...]:
    """`5,10,20,60` → `(5, 10, 20, 60)`；留空 = 用指标自己的那一组。

    句子里带「最多六条」与「1..500」是因为这三类填错（非数字、0 或负数、堆十几条）
    在同一个人手里会反复出现：`int()` 抛的 `ValueError` 不折成 `ValidationError`
    就越过全局处理器，换回一屏 500。
    """
    picked: list[int] = []
    for item in (raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError as exc:
            raise ValidationError(_MA_MESSAGE) from exc
        if not 1 <= value <= 500:
            raise ValidationError(_MA_MESSAGE)
        picked.append(value)
    deduped = tuple(dict.fromkeys(picked))
    if len(deduped) > MAX_MA_WINDOWS:
        raise ValidationError(_MA_MESSAGE)
    return deduped


def parse_periods(raw: str, known: tuple[str, ...]) -> tuple[str, ...]:
    """周期条顺序：逗号分隔的档名，必须把 `known` 每个**恰好一次**；留空 = 默认顺序。

    排序不是裁剪：少写一个档等于把它从屏幕上藏掉，而「这一档我这一市场用不到」
    该由置灰去说（D165），不该由偏好去藏。
    """
    text = (raw or "").strip()
    if not text:
        return ()
    picked = tuple(part.strip() for part in text.split(",") if part.strip())
    unknown = [name for name in picked if name not in known]
    if unknown:
        raise ValidationError(f"不认识的周期档 {'、'.join(unknown)}：可选的是 {', '.join(known)}")
    missing = [name for name in known if name not in picked]
    repeated = [name for name in known if picked.count(name) > 1]
    if missing or repeated:
        raise ValidationError(
            f"周期条要 {len(known)} 档、每档正好一次："
            f"缺 {'、'.join(missing) or '无'}，多 {'、'.join(repeated) or '无'}"
        )
    return picked


def ordered_periods(
    periods: tuple[tuple[str, str], ...], pref: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    """按偏好重排；空偏好=原顺序。

    库里存过的档可能在新版本里下了架：跳过不认识的，新加进词表的档接在末尾——
    偏好是排序，不是一份会过期的白名单。
    """
    if not pref:
        return periods
    labels = dict(periods)
    head = tuple((name, labels[name]) for name in pref if name in labels)
    tail = tuple(item for item in periods if item[0] not in pref)
    return head + tail


def windows_text(windows: tuple[int, ...]) -> str:
    """库里的窗口 → 输入框里那一串；空=默认，框里就空着。"""
    return ",".join(str(value) for value in windows)
```

- [ ] **Step 4: 改 `src/sams/web/pages/symbol.py`**

删掉 `CHART_PERIODS` 定义（约 190–196 行）与 `_windows` 函数（约 232–255 行），在 import 区加：

```python
from sams.web.chart_prefs import CHART_PERIODS, parse_windows
```

把文件里两处 `_windows(` 调用改成 `parse_windows(`（`bars_api` 的取窗口、
`chart_partial` 里那次只为止声的校验）。`_PERIOD_LABEL = dict(CHART_PERIODS)` 不动。

- [ ] **Step 5: 跑测试与相关回归**

```bash
uv run pytest tests/web/test_chart_prefs.py tests/web/test_symbol_minute.py tests/web/test_symbol_chart.py -q
```

Expected: 全绿（找不到 `test_symbol_chart.py` 就只跑存在的那个文件）。

- [ ] **Step 6: lint 与提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/web/chart_prefs.py src/sams/web/pages/symbol.py tests/web/test_chart_prefs.py
git commit -m "refactor(chart): 周期表与窗口校验搬到 web/chart_prefs（D193）"
```

---

### Task 2: 迁移 0025 与表登记

**Files:**
- Create: `src/sams/storage/migrations/0025_chart_prefs.sql`
- Modify: `tests/storage/test_migrate.py`

- [ ] **Step 1: 写失败断言**

`tests/storage/test_migrate.py` 的 `EXPECTED_TABLES` 里按字母序加一项：

```python
        "chart_prefs",
```

- [ ] **Step 2: 跑测试，确认失败点是「表没建」**

```bash
uv run pytest tests/storage/test_migrate.py -q
```

Expected: FAIL，缺 `chart_prefs`。

- [ ] **Step 3: 写迁移**

```sql
-- 0025：每人的图表偏好（D193）。周期条顺序与均线窗口都是短列表，JSON 存：
-- 加一档周期不必改 schema；没有行=用默认，清空就是删掉这一行
CREATE TABLE IF NOT EXISTS chart_prefs (
    user_id      VARCHAR PRIMARY KEY REFERENCES users(id),
    periods_json VARCHAR NOT NULL,
    ma_json      VARCHAR NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
```

- [ ] **Step 4: 跑测试转绿**

```bash
uv run pytest tests/storage/test_migrate.py -q
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/sams/storage/migrations/0025_chart_prefs.sql tests/storage/test_migrate.py
git commit -m "feat(chart): chart_prefs 表（0025，D193）"
```

---

### Task 3: `ChartPrefRepo`

**Files:**
- Create: `src/sams/storage/repos/chart_prefs.py`
- Test: `tests/storage/test_chart_prefs.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/storage/test_chart_prefs.py`（`db` 夹具与 `tests/storage/test_screener_presets.py` 同一套）：

```python
"""图表偏好仓库：一行一人，保存即覆盖，清空即回到默认。"""

from datetime import UTC, datetime

from support import FrozenClock

from sams.storage.repos.chart_prefs import ChartPrefRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def users(db):
    repo = UserRepo(db)
    return repo.create("alice", "correct-horse"), repo.create("bob", "correct-horse")


def repo(db):
    return ChartPrefRepo(db, clock=FrozenClock(NOW))


def test_a_user_without_a_row_falls_back_to_the_defaults(db):
    alice, _ = users(db)
    assert repo(db).get(alice.id) is None


def test_saving_then_reading_back_keeps_the_order_and_the_windows(db):
    alice, _ = users(db)
    repo(db).save(alice.id, ("day", "week", "m1"), (5, 10, 20))
    saved = repo(db).get(alice.id)
    assert saved is not None
    assert saved.periods == ("day", "week", "m1")
    assert saved.windows == (5, 10, 20)
    assert saved.updated_at == NOW


def test_saving_twice_replaces_the_one_row(db):
    alice, _ = users(db)
    repo(db).save(alice.id, ("day",), (5,))
    repo(db).save(alice.id, ("week",), (7, 14))
    assert repo(db).get(alice.id).windows == (7, 14)
    assert db.scalar("SELECT count(*) FROM chart_prefs") == 1


def test_clear_drops_the_row_and_says_whether_there_was_one(db):
    alice, _ = users(db)
    repo(db).save(alice.id, ("day",), (5,))
    assert repo(db).clear(alice.id) is True
    assert repo(db).get(alice.id) is None
    assert repo(db).clear(alice.id) is False


def test_one_users_preference_is_invisible_to_another(db):
    alice, bob = users(db)
    repo(db).save(alice.id, ("day",), (5,))
    assert repo(db).get(bob.id) is None
```

- [ ] **Step 2: 跑测试，确认模块不存在**

```bash
uv run pytest tests/storage/test_chart_prefs.py -q
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: 写仓库**

```python
"""每人的图表偏好（D193）：周期条顺序与均线窗口。

一行一人。没有行=用默认（`CHART_PERIODS` 的注册顺序、`MA_WINDOWS`）——不存一份
「默认值副本」，默认改了所有人跟着改；清空就是删掉这一行。
两列都是短列表，JSON 存：加一档周期不必改 schema。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database


@dataclass(frozen=True)
class ChartPref:
    user_id: str
    periods: tuple[str, ...]
    windows: tuple[int, ...]
    updated_at: datetime


class ChartPrefRepo:
    TABLE = "chart_prefs"

    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def get(self, user_id: str) -> ChartPref | None:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ?", [_user(user_id)]
        )
        return _to_pref(rows.to_dicts()[0]) if not rows.is_empty() else None

    def save(
        self, user_id: str, periods: tuple[str, ...], windows: tuple[int, ...]
    ) -> ChartPref:
        user_id = _user(user_id)
        now = self.clock.now_utc()
        self.db.execute(
            f"INSERT INTO {self.TABLE} (user_id, periods_json, ma_json, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
            " periods_json = excluded.periods_json,"
            " ma_json = excluded.ma_json,"
            " updated_at = excluded.updated_at",
            [user_id, json.dumps(list(periods)), json.dumps(list(windows)), now],
        )
        saved = self.get(user_id)
        assert saved is not None
        return saved

    def clear(self, user_id: str) -> bool:
        user_id = _user(user_id)
        existed = bool(
            self.db.scalar(
                f"SELECT count(*) FROM {self.TABLE} WHERE user_id = ?", [user_id]
            )
        )
        if not existed:
            return False
        self.db.execute(f"DELETE FROM {self.TABLE} WHERE user_id = ?", [user_id])
        return True


def _user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


def _to_pref(row: dict) -> ChartPref:
    return ChartPref(
        user_id=row["user_id"],
        periods=tuple(json.loads(row["periods_json"] or "[]")),
        windows=tuple(json.loads(row["ma_json"] or "[]")),
        updated_at=row["updated_at"],
    )
```

- [ ] **Step 4: 跑测试转绿**

```bash
uv run pytest tests/storage/test_chart_prefs.py -q
```

Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/sams/storage/repos/chart_prefs.py tests/storage/test_chart_prefs.py
git commit -m "feat(chart): 图表偏好仓库（D193）"
```

---

### Task 4: 装进线程上下文

**Files:**
- Modify: `src/sams/bootstrap.py`

- [ ] **Step 1: 加字段与构造**

`ServiceContext` 里 `screener_presets` 之后加：

```python
    #: 图表偏好（D193）：周期条顺序与均线窗口，一行一人
    chart_prefs: ChartPrefRepo
```

import 区按字母序加 `from sams.storage.repos.chart_prefs import ChartPrefRepo`。
`_context_on` 里在 `screener_presets = …` 之后加：

```python
    chart_prefs = ChartPrefRepo(database, clock=clock)
```

传给 `ServiceContext(...)`：

```python
        chart_prefs=chart_prefs,
```

- [ ] **Step 2: 跑现有 web 测试确认没装坏**

```bash
uv run pytest tests/web/test_settings.py -q
```

Expected: 全绿

- [ ] **Step 3: 提交**

```bash
git add src/sams/bootstrap.py
git commit -m "feat(chart): 偏好仓库进线程上下文（D193）"
```

---

### Task 5: 设置页卡片与保存端点

**Files:**
- Modify: `src/sams/web/pages/settings.py`
- Modify: `src/sams/web/templates/settings.html`
- Test: `tests/web/test_chart_prefs_web.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/web/test_chart_prefs_web.py`：

```python
"""设置页的「图表偏好」卡片：两张输入框，改完这一页与个股图都跟着变。"""

from sams.web.chart_prefs import CHART_PERIODS

KEYS = tuple(name for name, _label in CHART_PERIODS)


def _alice(web):
    return web.context.users.get_by_username("alice")


def _save(client, csrf, periods, ma):
    return client.post(
        "/settings/chart",
        data={"csrf": csrf, "periods": periods, "ma": ma},
        follow_redirects=False,
    )


def test_the_card_starts_empty_and_lists_every_period(logged):
    page = logged.client.get("/settings")
    assert page.status_code == 200
    assert "图表偏好" in page.text
    assert 'name="periods"' in page.text
    assert 'name="ma"' in page.text
    assert "1分" in page.text and "10日" in page.text


def test_saving_keeps_the_typed_order_and_windows(logged):
    response = _save(logged.client, logged.csrf, ",".join(reversed(KEYS)), "5,20")
    assert response.status_code == 303
    page = logged.client.get("/settings")
    assert 'value="' + ",".join(reversed(KEYS)) + '"' in page.text
    assert 'value="5,20"' in page.text
    pref = logged.context.chart_prefs.get(_alice(logged).id)
    assert pref.periods == tuple(reversed(KEYS))
    assert pref.windows == (5, 20)


def test_a_bad_order_is_refused_and_the_typed_text_stays(logged):
    response = _save(logged.client, logged.csrf, ",".join(KEYS[:-1]), "5,20")
    assert response.status_code == 400
    assert "每档正好一次" in response.text
    assert 'value="' + ",".join(KEYS[:-1]) + '"' in response.text
    assert logged.context.chart_prefs.get(_alice(logged).id) is None


def test_a_bad_window_is_refused(logged):
    response = _save(logged.client, logged.csrf, ",".join(KEYS), "0")
    assert response.status_code == 400
    assert "1..500" in response.text


def test_clearing_both_fields_goes_back_to_the_defaults(logged):
    _save(logged.client, logged.csrf, ",".join(reversed(KEYS)), "5,20")
    response = _save(logged.client, logged.csrf, "", "")
    assert response.status_code == 303
    assert logged.context.chart_prefs.get(logged.context.users.find("alice").id) is None
```

- [ ] **Step 2: 跑测试确认 404/缺卡片**

```bash
uv run pytest tests/web/test_chart_prefs_web.py -q
```

Expected: FAIL（卡片与端点都还没有）

- [ ] **Step 3: 改 `settings.py`**

import 区加：

```python
from sams.web.chart_prefs import CHART_PERIODS, parse_periods, parse_windows, windows_text
```

`_page` 增加参数 `chart: dict[str, str] | None = None`，并在 `ctx = web.contexts.current()`
之后算默认值：

```python
    pref = ctx.chart_prefs.get(user.id)
    chart_rows = chart or {
        "periods": ",".join(pref.periods) if pref else "",
        "ma": windows_text(pref.windows) if pref else "",
    }
```

渲染时多传两个键：

```python
        chart=chart_rows,
        chart_keys="、".join(f"{label}={name}" for name, label in CHART_PERIODS),
```

新增端点（放在 `save_dnd` 之后）：

```python
@router.post("/settings/chart")
def save_chart(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _form_body,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    periods_text = one(data, "periods")
    ma_text = one(data, "ma")
    try:
        periods = parse_periods(periods_text, tuple(name for name, _label in CHART_PERIODS))
        windows = parse_windows(ma_text)
    except ValidationError as exc:
        # 被拒的那一屏要还是人刚敲的那份：照着重打一遍才叫「改这一行」
        return _page(
            request,
            status_code=400,
            error=str(exc),
            chart={"periods": periods_text, "ma": ma_text},
        )
    if periods or windows:
        ctx.chart_prefs.save(user.id, periods, windows)
    else:
        ctx.chart_prefs.clear(user.id)
    audit(request, action="settings.chart", target=user.id, ok=True)
    return RedirectResponse("/settings?saved=chart", status_code=303)
```

- [ ] **Step 4: 在 `settings.html` 加卡片**

放在「免打扰」卡片之后、「通知通道」之前：

```html
<section class="card">
  <h2>图表偏好</h2>
  <p class="muted">周期条顺序与均线窗口按人存；两格都留空=恢复默认。可选档：{{ chart_keys }}</p>
  <form method="post" action="/settings/chart">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>周期条顺序 <input type="text" name="periods" value="{{ chart.periods }}" size="60"
      placeholder="{{ chart_keys }}"></label>
    <label>均线窗口 <input type="text" name="ma" value="{{ chart.ma }}" placeholder="5,10,20,60,120,250"></label>
    <button type="submit">保存</button>
  </form>
  <p class="muted">窗口是 1..500 的正整数、最多六条；顺序里每一档都要写、且只写一次。</p>
</section>
```

- [ ] **Step 5: 跑测试转绿**

```bash
uv run pytest tests/web/test_chart_prefs_web.py tests/web/test_settings.py -q
```

Expected: 全绿

- [ ] **Step 6: lint 与提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/web/pages/settings.py src/sams/web/templates/settings.html tests/web/test_chart_prefs_web.py
git commit -m "feat(chart): 设置页的图表偏好卡片（D193）"
```

---

### Task 6: 个股图按偏好排序并填默认均线

**Files:**
- Modify: `src/sams/web/pages/symbol.py`
- Test: `tests/web/test_chart_prefs_web.py`（追加）

- [ ] **Step 1: 追加失败测试**

在 `tests/web/test_chart_prefs_web.py` 末尾加：

```python
def _period_bar(html: str) -> str:
    """周期条那一段：视图链接里也有 `period=`，只能按 aria-label 切。"""
    return html.split('aria-label="周期"', 1)[1].split("</span>", 1)[0]


def test_the_period_bar_follows_the_saved_order(logged):
    _save(logged.client, logged.csrf, ",".join(reversed(KEYS)), "")
    bar = _period_bar(logged.client.get("/api/partials/chart?market=A&code=600519").text)
    assert bar.index(">10日<") < bar.index(">日<") < bar.index(">1分<")


def test_the_chart_defaults_to_the_saved_ma_windows(logged):
    _save(logged.client, logged.csrf, ",".join(KEYS), "5,20")
    html = logged.client.get("/api/partials/chart?market=A&code=600519").text
    assert "ma=5,20" in html


def test_an_explicit_ma_wins_over_the_preference(logged):
    _save(logged.client, logged.csrf, ",".join(KEYS), "5,20")
    html = logged.client.get("/api/partials/chart?market=A&code=600519&ma=3,7").text
    assert "ma=3,7" in html
    assert "ma=5,20" not in html
```

- [ ] **Step 2: 跑测试确认按默认顺序/空 ma 渲染**

```bash
uv run pytest tests/web/test_chart_prefs_web.py -q
```

Expected: 三条新用例 FAIL

- [ ] **Step 3: 改 `chart_partial`**

import 区加 `ordered_periods` 与 `windows_text`：

```python
from sams.web.chart_prefs import CHART_PERIODS, ordered_periods, parse_windows, windows_text
```

把 `require_user(request)` 改成 `user = require_user(request)`，在 `ctx = web.contexts.current()`
之后加：

```python
    pref = ctx.chart_prefs.get(user.id)
    # 偏好是默认值，不是覆盖：URL 里的 `ma=` 永远赢（同一条链接换指标时不丢人手敲的窗口）
    if not ma and pref is not None:
        ma = windows_text(pref.windows)
    periods = ordered_periods(CHART_PERIODS, pref.periods if pref is not None else ())
```

渲染时把 `periods=CHART_PERIODS` 改成 `periods=periods`。

- [ ] **Step 4: 跑测试转绿**

```bash
uv run pytest tests/web/test_chart_prefs_web.py tests/web/test_symbol_minute.py -q
```

Expected: 全绿

- [ ] **Step 5: lint 与提交**

```bash
uv run ruff check --no-cache src/sams tests
git add src/sams/web/pages/symbol.py tests/web/test_chart_prefs_web.py
git commit -m "feat(chart): 个股图按偏好排周期条、填默认均线（D193）"
```

---

### Task 7: lint、全量、真页面验收与文档回写

**Files:**
- Modify: `docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md`（实测回写）
- Modify: `docs/superpowers/plans/2026-10-28-sp2-c-chart-prefs.md`（交付记录）

- [ ] **Step 1: lint 与相关测试**

```bash
uv run ruff check --no-cache src/sams tests
uv run pytest tests/web/test_chart_prefs.py tests/web/test_chart_prefs_web.py tests/storage/test_chart_prefs.py tests/storage/test_migrate.py tests/web/test_settings.py tests/web/test_symbol_minute.py -q
```

Expected: 全绿。

- [ ] **Step 2: 全量（主控跑，不加管道）**

```bash
uv run pytest -q
```

Expected: 0 failed。

- [ ] **Step 3: 真页面（先停 serve，迁移 0025 随启动自动应用）**

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN | awk 'NR>1 {print $2}' | xargs -r kill
SAMS_SECRET_KEY="$(cat tmp/.sams_secret)" nohup uv run sams serve > tmp/serve.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}\n' localhost:8765/healthz
```

Expected: `200`。

- [ ] **Step 4: 浏览器或 curl 登录走一遍**

- `/settings` 出现「图表偏好」卡片，两格空着；
- 存 `periods`=把「日、周、月」挪到最前、`ma=5,20` → 303 且回显；
- 打错一档（少写一个）→ 400，框里还是刚敲的那串；
- 打开 `/symbol/A/600519`：周期条按存的顺序、均线画在 5/20；把 URL 换成
  显式 `ma=3,7` 后主图是 3/7（偏好只做默认）；
- 两格清空 → 回到默认顺序与 6 条默认均线。

- [ ] **Step 5: 回写 spec 与计划**

spec §7 计划 C 那条末尾加一行：

```markdown
- 计划 C 已落地（`docs/superpowers/plans/2026-10-28-sp2-c-chart-prefs.md`，D193）：
  `chart_prefs`（迁移 0025）一行一人；`/api/partials/chart` 按偏好排周期条并把 `ma=`
  默认成这个人的窗口，显式参数仍赢；设置页「图表偏好」卡片两格都空=删行回默认。
```

计划末尾加「交付记录」：实际新增测试数、验收偏差与修法（没有就写「无」）。

- [ ] **Step 6: 提交**

```bash
git add docs/superpowers/specs/2026-10-27-chart-timeshare-kline-indicators-design.md docs/superpowers/plans/2026-10-28-sp2-c-chart-prefs.md
git commit -m "docs(chart): 计划 C 验收与实测回写"
```

---

## 完成标准

- 设置页能保存、回显、清空周期条顺序与均线窗口；两格都空=删行回默认；
- 周期条顺序必须把现有每一档写且只写一次，未知档名与重复都 400，被拒时框里留着刚敲的字；
- 均线窗口 1..500、最多六条，与 `bars` 接口同一句错话（`parse_windows` 只有一份）；
- 个股图的周期条按这个人的顺序排；`ma=` 默认取这个人的窗口，URL 里显式给了就听 URL 的；
- 一个人的偏好看不见另一个人的；库里没有行时屏幕与今天完全一样；
- lint 绿、全量绿、真页面上存/清各走一遍。
