# SP3 计划 E：选股页 `/screen` + 筛选方案实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已完成的选股引擎接成网页：条件可加可删可组合、结果三个数与无数原因照报、勾选/全部加自选、六动作维护筛选方案。

**Architecture:** 页面只读库（`snapshot_daily`），条件走 URL（GET 可收藏），加减行是服务端拼好的链接；整张结果表是一个 `<form>`，三种加自选是同一表单里 value 不同的提交按钮（CSP 禁内联脚本）。方案落在新表 `screener_presets`（迁移 0024），per-user 隔离在仓库层兜底；重跑写 `scan_results`（D183），列表里的「上次跑」从那里读。

**Tech Stack:** FastAPI + Starlette 模板（Jinja2）、DuckDB、polars、pytest + TestClient。

---

## 约束（开工前先读）

- 只跑 `uv run ...`；**永远不跑 `ruff format`**。
- lint：`uv run ruff check --no-cache src/sams tests`；测试只跑相关文件，全量由主控最后跑（不加管道）。
- git 一律用普通 bash `git add <具体文件>` / `git commit`；不建分支、不 amend、不 `git add -A`、不 push。
- 迁移编号：本计划取 **0024**（0023 已被计划 D 的 `market_snapshot` 用掉；计划 C 顺延 0025）。
- TDD：每条行为先写失败的测试、看它因正确的原因红，再写最小实现转绿。
- 注释只写「为什么」，一行以内，中文。

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `src/sams/storage/migrations/0024_screener_presets.sql` | `screener_presets` 表 |
| `src/sams/storage/repos/screener_presets.py` | 方案的增删改查（per-user、名字唯一、条件 JSON 往返） |
| `src/sams/core/screener_fields.py` | 加 `OP_LABELS` / `describe` / `describe_all`（方案摘要的唯一出处） |
| `src/sams/services/screener.py` | 加 `preset_strategy_id`（`scan_results` 的键） |
| `src/sams/bootstrap.py` / `ServiceContext` | 把 `screener_presets` 仓库接进上下文 |
| `src/sams/web/pages/screen.py` | `/screen` 与方案、加自选、重采的全部端点 |
| `src/sams/web/templates/screen.html` | 页面 |
| `src/sams/web/templates/base.html` | 导航加「选股」 |
| `src/sams/web/app.py` | 注册 router；`moment_label` 进 Jinja 全局 |
| `tests/storage/test_screener_presets.py` | 仓库层 |
| `tests/core/test_screener_fields.py` | 摘要文案 |
| `tests/services/test_screener.py` | `preset_strategy_id` |
| `tests/web/conftest.py` | 支持快照能力的 app 夹具（「立即重采」才有 job 可排） |
| `tests/web/test_screen_web.py` | 页面全部行为 |
| `tests/storage/test_migrate.py` | 表清单补 `snapshot_daily` / `screener_presets` |

---

### Task 1: 迁移 0024 与方案仓库

**Files:**
- Create: `src/sams/storage/migrations/0024_screener_presets.sql`
- Create: `src/sams/storage/repos/screener_presets.py`
- Create: `tests/storage/test_screener_presets.py`

- [ ] **Step 1: 写失败的仓库测试**

```python
"""筛选方案：per-user 的一张表，名字在一个人手里唯一（D182）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition
from sams.storage.repos.screener_presets import ScreenerPresetRepo
from sams.storage.repos.users import UserRepo
from support import FrozenClock

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CONDITIONS = [
    Condition("pr_now", "<=", 0.5),
    Condition("turnover_rate", ">=", 1.0),
]


def users(db):
    repo = UserRepo(db)
    alice = repo.create("alice", "correct-horse")
    bob = repo.create("bob", "correct-horse")
    return alice, bob


def repo(db, clock=None):
    return ScreenerPresetRepo(db, clock=clock or FrozenClock(NOW))


def test_create_reads_back_the_conditions_and_the_times(db):
    alice, _ = users(db)
    saved = repo(db).create(alice.id, "便宜", "a", CONDITIONS)
    assert saved.name == "便宜" and saved.market == "A"
    assert saved.conditions == tuple(CONDITIONS)
    assert saved.created_at == NOW and saved.updated_at == NOW
    got = repo(db).get(alice.id, saved.id)
    assert got == saved


def test_between_survives_the_json_round_trip(db):
    alice, _ = users(db)
    saved = repo(db).create(alice.id, "区间", "A", [Condition("price", "between", (1.0, 5.0))])
    assert repo(db).get(alice.id, saved.id).conditions == (Condition("price", "between", (1.0, 5.0)),)


def test_the_same_name_is_unique_within_one_user_but_not_across_users(db):
    alice, bob = users(db)
    repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    with pytest.raises(ValidationError, match="已有同名方案"):
        repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    bob_saved = repo(db).create(bob.id, "便宜", "A", CONDITIONS)
    assert repo(db).list(bob.id)[0].id == bob_saved.id


def test_empty_conditions_are_refused_because_that_means_the_whole_market(db):
    alice, _ = users(db)
    with pytest.raises(ValidationError, match="一条条件都没有"):
        repo(db).create(alice.id, "空", "A", [])


def test_another_users_preset_is_invisible(db):
    alice, bob = users(db)
    saved = repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    assert repo(db).get(bob.id, saved.id) is None
    assert repo(db).delete(bob.id, saved.id) is False
    assert repo(db).get(alice.id, saved.id) is not None


def test_rename_moves_updated_at_but_not_created_at(db):
    alice, _ = users(db)
    clock = FrozenClock(NOW)
    repo(db, clock).create(alice.id, "便宜", "A", CONDITIONS)
    clock.advance(hours=1)
    saved = repo(db).list(alice.id)[0]
    assert repo(db, clock).rename(alice.id, saved.id, "很便宜")
    got = repo(db).get(alice.id, saved.id)
    assert (got.name, got.created_at, got.updated_at) == ("很便宜", NOW, clock.moment)


def test_rename_onto_another_name_is_refused_but_onto_itself_is_fine(db):
    alice, _ = users(db)
    first = repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    repo(db).create(alice.id, "更便宜", "A", CONDITIONS)
    with pytest.raises(ValidationError, match="已有同名方案"):
        repo(db).rename(alice.id, first.id, "更便宜")
    assert repo(db).rename(alice.id, first.id, "便宜")


def test_overwriting_conditions_keeps_the_name(db):
    alice, _ = users(db)
    clock = FrozenClock(NOW)
    saved = repo(db, clock).create(alice.id, "便宜", "A", CONDITIONS)
    clock.advance(minutes=5)
    assert repo(db, clock).update_conditions(
        alice.id, saved.id, [Condition("pr_now", "<=", 0.3)]
    )
    got = repo(db).get(alice.id, saved.id)
    assert got.name == "便宜" and got.conditions == (Condition("pr_now", "<=", 0.3),)
    assert got.updated_at == clock.moment


def test_delete_removes_one_row_only(db):
    alice, _ = users(db)
    saved = repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    assert repo(db).delete(alice.id, saved.id) is True
    assert repo(db).delete(alice.id, saved.id) is False
    assert repo(db).list(alice.id) == []
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/storage/test_screener_presets.py -q
```

Expected: ERROR/FAIL —— `sams.storage.repos.screener_presets` 不存在。

- [ ] **Step 3: 写迁移**

```sql
-- 0024：筛选方案（D182）。条件以 JSON 存：形状跟着词表走，加字段不必改 schema
CREATE TABLE IF NOT EXISTS screener_presets (
    id              VARCHAR PRIMARY KEY,
    user_id         VARCHAR NOT NULL REFERENCES users(id),
    name            VARCHAR NOT NULL,
    market          VARCHAR NOT NULL,
    conditions_json VARCHAR NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, name)
);
```

- [ ] **Step 4: 写仓库**

```python
"""筛选方案的增删改查（D182）：per-user，名字在一个人手里唯一。

条件以 JSON 存：形状跟着词表走，加一个字段不必改 schema。`updated_at` 只被「改名」
与「改条件」碰，重跑不碰——它回答的是「这条方案现在是什么时候这样写的」，
「上次跑」是 `scan_results` 那一列的事（D183）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_market
from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition, validate
from sams.storage.db import Database

MAX_NAME = 40


@dataclass(frozen=True)
class Preset:
    id: str
    user_id: str
    name: str
    market: str
    conditions: tuple[Condition, ...]
    created_at: datetime
    updated_at: datetime


class ScreenerPresetRepo:
    TABLE = "screener_presets"

    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def create(
        self, user_id: str, name: str, market: str, conditions: list[Condition]
    ) -> Preset:
        user_id = _user(user_id)
        name = _name(name)
        market = canonical_market(market)
        checked = _checked(market, conditions)
        existing = self.find(user_id, name)
        if existing is not None:
            raise ValidationError(
                f"已有同名方案「{name}」（id={existing.id}）：要覆盖就点那一行的「覆盖为当前条件」，"
                "要保留它请换个名字"
            )
        preset_id = uuid4().hex
        now = self.clock.now_utc()
        self.db.execute(
            f"INSERT INTO {self.TABLE}"
            " (id, user_id, name, market, conditions_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [preset_id, user_id, name, market, _to_json(checked), now, now],
        )
        saved = self.get(user_id, preset_id)
        assert saved is not None
        return saved

    def list(self, user_id: str) -> list[Preset]:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ? ORDER BY updated_at DESC, name",
            [_user(user_id)],
        )
        return [_to_preset(row) for row in rows.to_dicts()]

    def get(self, user_id: str, preset_id: str) -> Preset | None:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ? AND id = ?",
            [_user(user_id), preset_id],
        )
        return _to_preset(rows.to_dicts()[0]) if not rows.is_empty() else None

    def find(self, user_id: str, name: str) -> Preset | None:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ? AND name = ?",
            [_user(user_id), _name(name)],
        )
        return _to_preset(rows.to_dicts()[0]) if not rows.is_empty() else None

    def rename(self, user_id: str, preset_id: str, name: str) -> bool:
        user_id = _user(user_id)
        name = _name(name)
        existing = self.find(user_id, name)
        if existing is not None and existing.id != preset_id:
            raise ValidationError(f"已有同名方案「{name}」：换个名字，或先删掉那一条")
        if self.get(user_id, preset_id) is None:
            return False
        self.db.execute(
            f"UPDATE {self.TABLE} SET name = ?, updated_at = ?"
            " WHERE user_id = ? AND id = ?",
            [name, self.clock.now_utc(), user_id, preset_id],
        )
        return True

    def update_conditions(
        self, user_id: str, preset_id: str, conditions: list[Condition]
    ) -> bool:
        user_id = _user(user_id)
        preset = self.get(user_id, preset_id)
        if preset is None:
            return False
        checked = _checked(preset.market, conditions)
        self.db.execute(
            f"UPDATE {self.TABLE} SET conditions_json = ?, updated_at = ?"
            " WHERE user_id = ? AND id = ?",
            [_to_json(checked), self.clock.now_utc(), user_id, preset_id],
        )
        return True

    def delete(self, user_id: str, preset_id: str) -> bool:
        user_id = _user(user_id)
        exists = self.db.scalar(
            f"SELECT count(*) FROM {self.TABLE} WHERE user_id = ? AND id = ?",
            [user_id, preset_id],
        )
        if not exists:
            return False
        self.db.execute(
            f"DELETE FROM {self.TABLE} WHERE user_id = ? AND id = ?", [user_id, preset_id]
        )
        return True


def _user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


def _name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("方案要一个名字")
    if len(cleaned) > MAX_NAME:
        raise ValidationError(f"方案名最多 {MAX_NAME} 个字，收到 {len(cleaned)} 个")
    return cleaned


def _checked(market: str, conditions: list[Condition]) -> tuple[Condition, ...]:
    if not conditions:
        raise ValidationError("一条条件都没有：空条件等于「全市场都通过」，那不是一个能重跑的方案")
    return validate(market, list(conditions))


def _to_json(conditions: tuple[Condition, ...]) -> str:
    return json.dumps(
        [{"field": item.field, "op": item.op, "value": item.value} for item in conditions],
        ensure_ascii=False,
    )


def _to_preset(row: dict) -> Preset:
    conditions = []
    for item in json.loads(row["conditions_json"] or "[]"):
        value = item["value"]
        conditions.append(
            Condition(item["field"], item["op"], tuple(value) if isinstance(value, list) else value)
        )
    return Preset(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        market=row["market"],
        conditions=tuple(conditions),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
```

- [ ] **Step 5: 跑测试确认绿**

```bash
uv run pytest tests/storage/test_screener_presets.py tests/storage/test_migrate.py -q
```

Expected: PASS。`test_migrate.py` 的表清单还没加新表，这一步不应因新表失败（它只查缺表）。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/migrations/0024_screener_presets.sql src/sams/storage/repos/screener_presets.py tests/storage/test_screener_presets.py
git commit -m "feat(screener): 筛选方案表与仓库（D182）：per-user、名字唯一、条件 JSON 往返"
```

---

### Task 2: 把方案仓库接进上下文

**Files:**
- Modify: `src/sams/bootstrap.py`
- Modify: `tests/storage/test_migrate.py`

- [ ] **Step 1: 写失败的表清单断言**

在 `tests/storage/test_migrate.py` 的 `EXPECTED_TABLES` 里补两行：

```python
    "snapshot_daily",
    "screener_presets",
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/storage/test_migrate.py -q
```

Expected: FAIL —— 缺失的表只有 `screener_presets`（`snapshot_daily` 是计划 D 建的，本来就该在清单里）。

- [ ] **Step 3: 接进 `ServiceContext` 与 `_context_on`**

`src/sams/bootstrap.py`：

1. import 区加：

```python
from sams.storage.repos.screener_presets import ScreenerPresetRepo
```

2. `class ServiceContext` 里、`snapshot_store` 之后加：

```python
    #: 筛选方案（D182）：条件与名字都在这一张表，页面六个动作只经它
    screener_presets: ScreenerPresetRepo
```

3. `_context_on` 里 `snapshot_store = SnapshotRepo(database)` 之后加：

```python
    screener_presets = ScreenerPresetRepo(database, clock=clock)
```

4. `return ServiceContext(...)` 参数里、`snapshot_store=snapshot_store,` 之后加：

```python
        screener_presets=screener_presets,
```

- [ ] **Step 4: 跑测试确认绿**

```bash
uv run pytest tests/storage/test_migrate.py tests/storage/test_screener_presets.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/bootstrap.py tests/storage/test_migrate.py
git commit -m "feat(screener): 方案仓库进线程上下文；迁移表清单补 0023/0024"
```

---

### Task 3: 条件摘要与 `scan_results` 的键

**Files:**
- Modify: `src/sams/core/screener_fields.py`
- Modify: `src/sams/services/screener.py`
- Modify: `tests/core/test_screener_fields.py`
- Modify: `tests/services/test_screener.py`

- [ ] **Step 1: 写失败测试**

`tests/core/test_screener_fields.py` 末尾加：

```python
def test_describe_all_is_the_only_readable_spelling_of_a_condition_set():
    """方案列表要一句人话，不是 JSON；单位也必须在这句里（D58）。"""
    from sams.core.screener_fields import describe_all

    text = describe_all(
        [
            Condition("pr_now", "<=", 0.5),
            Condition("turnover_rate", ">=", 1.0),
            Condition("price", "between", (1.0, 5.0)),
        ]
    )
    assert text == (
        "现值市赚率 PR（小数比值（0.5 = 50%）） ≤ 0.5"
        " 且 换手率（百分数（0.31 = 0.31%）） ≥ 1"
        " 且 现价（元） 介于 1..5"
    )
```

`tests/services/test_screener.py` 末尾加：

```python
def test_preset_strategy_id_moves_only_when_the_conditions_or_the_name_move():
    from sams.services.screener import preset_strategy_id

    base = [Condition("pr_now", "<=", 0.5)]
    assert preset_strategy_id("便宜", base) == preset_strategy_id(
        "便宜", [Condition("pr_now", "<=", 0.5)]
    )
    assert preset_strategy_id("便宜", base) != preset_strategy_id(
        "便宜", [Condition("pr_now", "<=", 0.4)]
    )
    assert preset_strategy_id("便宜", base) != preset_strategy_id("更便宜", base)
    assert preset_strategy_id("便宜", base).startswith("screen:便宜:")
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/core/test_screener_fields.py tests/services/test_screener.py -q
```

Expected: FAIL —— `describe_all` / `preset_strategy_id` 不存在。

- [ ] **Step 3: 实现**

`src/sams/core/screener_fields.py`：`OPS` 之后加

```python
#: 界面上比较符的中文写法。摘要与下拉都念这一份，屏幕上不会出现两种 `<=`
OP_LABELS = {">=": "≥", ">": ">", "<=": "≤", "<": "<", "=": "=", "between": "介于"}
```

`Condition` 之后加

```python
def describe(condition: Condition, *, registry: tuple[Field, ...] = FIELDS) -> str:
    """一条条件的可读写法。单位跟在这一句里：只说 `≤ 0.5` 会让人再猜一次小数还是百分数。"""
    spec = field(condition.field, registry=registry)
    if condition.op == "between":
        low, high = condition.value  # type: ignore[misc]
        return f"{spec.label}（{spec.unit}） 介于 {low:g}..{high:g}"
    return (
        f"{spec.label}（{spec.unit}） "
        f"{OP_LABELS.get(condition.op, condition.op)} {float(condition.value):g}"
    )


def describe_all(
    conditions: list[Condition] | tuple[Condition, ...], *, registry: tuple[Field, ...] = FIELDS
) -> str:
    return " 且 ".join(describe(item, registry=registry) for item in conditions)
```

`src/sams/services/screener.py`：`from __future__` 之后补 import

```python
import hashlib
import json
```

文件末尾加

```python
def preset_strategy_id(name: str, conditions) -> str:
    """方案重跑写进 `scan_results` 的键：名字 + 条件哈希（D183）。

    条件一改就是另一句话：旧结果仍按旧哈希查得到，而方案那一行本身不随时间变。
    """
    payload = json.dumps(
        [[item.field, item.op, item.value] for item in conditions],
        ensure_ascii=False,
        default=list,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"screen:{name}:{digest}"
```

- [ ] **Step 4: 跑测试确认绿**

```bash
uv run pytest tests/core/test_screener_fields.py tests/services/test_screener.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/core/screener_fields.py src/sams/services/screener.py tests/core/test_screener_fields.py tests/services/test_screener.py
git commit -m "feat(screener): 条件摘要与方案扫描键（D58/D183）"
```

---

### Task 4: `/screen` 骨架——范围行与条件编辑区

**Files:**
- Create: `src/sams/web/pages/screen.py`
- Create: `src/sams/web/templates/screen.html`
- Modify: `src/sams/web/app.py`
- Modify: `src/sams/web/templates/base.html`
- Modify: `tests/web/conftest.py`
- Create: `tests/web/test_screen_web.py`

- [ ] **Step 1: 夹具——带快照能力的 app 与已登录客户端**

`tests/web/conftest.py` 顶部补 import：

```python
import polars as pl

from sams.datasource.spot import SPOT_SCHEMA
```

`support` 之后加：

```python
class StubSnapshotAdapter(support.StubAdapter):
    """日线替身 + 一条全市场快照能力：`/screen` 的「立即重采」才排得进 `spot` job。"""

    market_snapshot_markets = frozenset({"A"})

    def market_snapshot(self, market, codes):
        return pl.DataFrame(schema=SPOT_SCHEMA)
```

文件末尾加：

```python
@pytest.fixture
def screen_app(migrated, stub_sources):
    """A 股链上那个源会答全市场快照；其余夹具形状与 `web_app` 相同。"""
    sources = dict(stub_sources)
    sources["akshare"] = StubSnapshotAdapter(
        "akshare", ["A"], support.sample_bars(source="akshare")
    )
    yield from _build_app_factory(migrated, sources)


@pytest.fixture
def logged_screen(screen_app):
    screen_app.context.users.create("alice", "correct-horse-battery", is_admin=True)
    client = TestClient(screen_app.app)
    response = client.post(
        "/login",
        data={"username": "alice", "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    screen_app.csrf = client.cookies.get("sams_csrf").rpartition(".")[0]
    screen_app.client = client
    yield screen_app
    client.close()
```

> `TestClient` 不放进 `with`：夹具要在测试体内一直拿着它，退出时再关。

- [ ] **Step 2: 写失败的页面测试**

创建 `tests/web/test_screen_web.py`：

```python
"""选股页：范围行、条件编辑、三个数、无数原因、加自选、方案（D180/D182/D184）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def snap(code, *, pe_dyn=5.0, pb=1.0, turnover=1.0):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": DAY,
        "price": 10.0,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def seed(context, *rows, codes=None):
    codes = codes or [row["code"] for row in rows]
    SymbolListRepo(context.db).upsert_batch(
        "A",
        [
            {"code": code, "name": code, "py_initials": code, "py_full": code, "currency": "CNY"}
            for code in codes
        ],
    )
    frame = pl.DataFrame(
        [{"market": "A", "taken_at": TAKEN, **row} for row in rows],
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(context.db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)


def default_seed(logged_screen):
    seed(
        logged_screen.context,
        snap("600519", pe_dyn=5.0, turnover=1.0),   # PR 0.25 通过
        snap("000001", pe_dyn=6.0, turnover=3.0),   # PR 0.36 通过
        snap("300750", pe_dyn=10.0, turnover=1.0),  # PR 1.00 不通过
        snap("600000", pe_dyn=-5.0, turnover=1.0),  # 亏损 → 无数
    )


def test_the_range_line_speaks_before_any_condition_is_chosen(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params={"market": "A"})
    assert response.status_code == 200
    assert "快照 4 行" in response.text
    assert "加一条条件" in response.text


def test_a_new_page_has_one_blank_row_and_the_unit_on_every_field(logged_screen):
    response = logged_screen.client.get("/screen", params={"market": "A"})
    assert '<option value="">（选字段）</option>' in response.text
    assert "现值市赚率 PR（小数比值（0.5 = 50%））" in response.text


def test_adding_and_removing_a_row_are_links_that_keep_the_other_rows(logged_screen):
    params = [
        ("market", "A"),
        ("f", "pr_now"),
        ("o", "<="),
        ("v", "0.5"),
        ("f", "turnover_rate"),
        ("o", ">="),
        ("v", "1"),
    ]
    response = logged_screen.client.get("/screen", params=params)
    assert response.status_code == 200
    assert "再加一条" in response.text
    assert "删掉这一条" in response.text


def test_a_market_that_has_no_snapshot_says_so_instead_of_showing_an_empty_table(logged_screen):
    response = logged_screen.client.get("/screen", params={"market": "HK"})
    assert response.status_code == 200
    assert "还没有全市场快照" in response.text
```

- [ ] **Step 3: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q
```

Expected: FAIL —— 404（路由不存在），部分用例会先在 `logged_screen` 夹具上失败。

- [ ] **Step 4: 实现页面骨架**

创建 `src/sams/web/pages/screen.py`：

```python
"""选股页 /screen（D172/D180/D182/D184）。

只做三件事：把条件翻成引擎的一篮、把那一篮说清（范围 / 三个数 / 无数原因）、
把人的一句话（方案）存起来。取数一律读库：行上那些 PE/PB 是快照那一天的那一份
（D181）；「立即重采」只排队，不在这条线程上打外网（D176）。
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from sams.core.errors import ValidationError
from sams.core.screener_fields import FIELDS, OPS, Condition, fields_for
from sams.storage.repos.watchlists import MAX_ITEMS_PER_USER
from sams.web.auth import csrf_protect, require_user
from sams.web.display import moment_label
from sams.web.forms import form_data, many, one

router = APIRouter()

PAGE_SIZE = 50
MISSING_CODES = 20
STALE_DAYS = 3

_FORM_BODY = Depends(form_data)


@router.get("/screen")
def screen_page(request: Request):
    user = require_user(request)
    query = request.query_params
    market, preset, rows = _state(request, user.id, query)
    return _render(
        request,
        user,
        market=market,
        preset=preset,
        rows=rows,
        page=_page_number(query.get("page")),
        confirm_all=query.get("confirm") == "all",
        notice=_notice(query),
    )


def _state(request: Request, user_id: str, query) -> tuple[str, object | None, list[dict]]:
    web = request.app.state.web
    ctx = web.contexts.current()
    markets = sorted(ctx.settings.markets)
    preset_id = (query.get("preset") or "").strip()
    if preset_id:
        preset = _preset(ctx, user_id, preset_id)
        return preset.market, preset, [_draft_of(item) for item in preset.conditions]
    market = (
        query.get("market") or ("A" if "A" in markets else (markets[0] if markets else ""))
    ).upper()
    if market not in markets:
        raise HTTPException(
            status_code=400, detail=f"市场 {market or '（空）'} 没配：只有 {', '.join(markets)}"
        )
    rows = _rows_from(query)
    return market, None, rows or [_draft()]


def _render(
    request: Request,
    user,
    *,
    market: str,
    preset,
    rows: list[dict],
    page: int = 1,
    confirm_all: bool = False,
    error: str = "",
    notice: str = "",
):
    web = request.app.state.web
    ctx = web.contexts.current()
    markets = sorted(ctx.settings.markets)
    specs = {item.name: item for item in FIELDS}
    snapshot = None
    stale = False

    conditions: tuple[Condition, ...] = ()
    result = None
    if not error:
        try:
            conditions = _conditions(rows)
            if conditions:
                result = ctx.screener.run(market, conditions)
        except ValidationError as exc:
            error = str(exc)
    if result is None:
        day = ctx.snapshot_store.latest_day(market)
        if day is not None:
            frame = ctx.snapshot_store.load_day(market, day)
            snapshot = {
                "day": day,
                "taken_at": frame["taken_at"].max() if not frame.is_empty() else None,
                "rows": frame.height,
            }
            stale = (ctx.clock.today() - day).days > STALE_DAYS

    enabled = {item.name for item in fields_for(market)}
    field_options = [
        {
            "name": item.name,
            "label": item.label,
            "unit": item.unit,
            "enabled": item.name in enabled,
            "why": ""
            if item.name in enabled
            else f"{market} 没有这一列（{'/'.join(sorted(item.markets))} 才有）",
        }
        for item in FIELDS
    ]
    add_url = _url(market, [*rows, _draft()])
    remove_urls = [_url(market, rows[:i] + rows[i + 1 :]) for i in range(len(rows))]
    if not rows:
        add_url = _url(market, [_draft()])
    return web.render(
        request,
        "screen.html",
        markets=markets,
        market=market,
        preset=preset,
        rows=rows,
        add_url=add_url,
        remove_urls=remove_urls,
        field_options=field_options,
        ops=[(op, _op_label(op)) for op in OPS],
        result=result,
        labels=[specs[item.field] for item in conditions],
        snapshot=snapshot,
        stale=stale,
        stale_days=STALE_DAYS,
        error=error,
        notice=notice,
        page=page,
    )


def _op_label(op: str) -> str:
    from sams.core.screener_fields import OP_LABELS

    return OP_LABELS.get(op, op)


def _page_number(text) -> int:
    try:
        return max(1, int(text or 1))
    except ValueError:
        return 1


def _rows_from(query) -> list[dict]:
    names = query.getlist("f")
    ops = query.getlist("o")
    values = query.getlist("v")
    return [
        _draft(name.strip(), op.strip(), value.strip())
        for name, op, value in zip(names, ops, values)
    ]


def _posted_rows(data: dict[str, list[str]]) -> list[dict]:
    rows = [
        _draft(name.strip(), op.strip(), value.strip())
        for name, op, value in zip(data.get("f", []), data.get("o", []), data.get("v", []))
    ]
    return rows or [_draft()]


def _draft(name: str = "", op: str = "<=", value="") -> dict[str, str]:
    return {"field": name, "op": op if op in OPS else "<=", "value": _text(value)}


def _draft_of(condition: Condition) -> dict[str, str]:
    return _draft(condition.field, condition.op, condition.value)


def _text(value) -> str:
    if isinstance(value, tuple):
        return f"{value[0]:g}..{value[1]:g}"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _conditions(rows: list[dict]) -> tuple[Condition, ...]:
    return tuple(_condition(row) for row in rows if row["field"])


def _condition(row: dict) -> Condition:
    name, op, text = row["field"], row["op"], row["value"]
    if op == "between":
        low, _, high = text.partition("..")
        return Condition(name, "between", (_number(low, name), _number(high, name)))
    return Condition(name, op, _number(text, name))


def _number(text: str, name: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValidationError(f"{name} 的值要一个数，收到 {text!r}") from None


def _url(
    market: str,
    rows: list[dict],
    *,
    preset=None,
    page: int | None = None,
    confirm: bool = False,
    notice: str = "",
    extra: dict | None = None,
) -> str:
    pairs: list[tuple[str, str]] = [("market", market)]
    if preset is not None:
        pairs.append(("preset", preset.id))
    else:
        for row in rows:
            pairs += [("f", row["field"]), ("o", row["op"]), ("v", row["value"])]
    if page is not None and page > 1:
        pairs.append(("page", str(page)))
    if confirm:
        pairs.append(("confirm", "all"))
    if notice:
        pairs.append(("notice", notice))
    for key, value in (extra or {}).items():
        pairs.append((key, str(value)))
    return f"/screen?{urlencode(pairs)}"


def _preset(ctx, user_id: str, preset_id: str):
    preset = ctx.screener_presets.get(user_id, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="没有这条筛选方案")
    return preset


def _notice(query) -> str:
    kind = query.get("notice") or ""
    if kind == "queued":
        return "已把全市场快照排进下一拍：跑完刷新这一页"
    return ""


@router.post("/screen/recollect")
def recollect(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    try:
        ctx.scheduler.request_run(job="spot", market=market)
    except ValidationError as exc:
        return _render(
            request, request.state.sams_user, market=market, preset=None, rows=rows, error=str(exc)
        )
    return RedirectResponse(_url(market, rows, notice="queued"), status_code=303)
```

创建 `src/sams/web/templates/screen.html`（Task 4 的版本；后续任务往里加区块）：

```html
{% extends "base.html" %}
{% block title %}选股{% endblock %}
{% block content %}
<h1>选股</h1>
{% if notice %}<p class="flash-ok">{{ notice }}</p>{% endif %}
{% if error %}<p class="flash-error">{{ error }}</p>{% endif %}

<section class="card">
  <h2>{{ market }} 股 · 快照</h2>
  {% if snapshot %}
  <p>快照 {{ snapshot.rows }} 行 · 采集 {{ moment_label(snapshot.taken_at, display_tz) }}</p>
  {% else %}
  <p class="muted">{{ market }} 股还没有全市场快照：点「立即重采」排队，或先跑
    <code>sams sync spot --market {{ market }}</code></p>
  {% endif %}
  <form method="post" action="/screen/recollect" class="inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <input type="hidden" name="market" value="{{ market }}">
    {% for row in rows %}<input type="hidden" name="f" value="{{ row.field }}"><input type="hidden" name="o" value="{{ row.op }}"><input type="hidden" name="v" value="{{ row.value }}"{% endfor %}>
    <button type="submit">立即重采</button>
  </form>
</section>

<form method="get" action="/screen" class="panel">
  <label>市场
    <select name="market">
      {% for item in markets %}<option value="{{ item }}"{% if item == market %} selected{% endif %}>{{ item }}</option>{% endfor %}
    </select>
  </label>
  {% for row in rows %}
  <div class="grid">
    <label>字段
      <select name="f">
        <option value="">（选字段）</option>
        {% for item in field_options %}
        <option value="{{ item.name }}"{% if item.name == row.field %} selected{% endif %}{% if not item.enabled %} disabled title="{{ item.why }}"{% endif %}>{{ item.label }}（{{ item.unit }}）</option>
        {% endfor %}
      </select>
    </label>
    <label>比较符
      <select name="o">
        {% for value, label in ops %}<option value="{{ value }}"{% if value == row.op %} selected{% endif %}>{{ label }}</option>{% endfor %}
      </select>
    </label>
    <label>值 <input name="v" value="{{ row.value }}" placeholder="between 写 1..5"></label>
    <a href="{{ remove_urls[loop.index0] }}">删掉这一条</a>
  </div>
  {% endfor %}
  <p><button type="submit">筛选</button> <a href="{{ add_url }}">再加一条</a></p>
</form>

{% if not rows or not rows[0].field %}
<p class="muted">加一条条件，按「筛选」。</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: 接进 app 与导航**

`src/sams/web/app.py`：

1. import 列表里加 `screen`：

```python
from sams.web.pages import (
    admin,
    admin_annotations,
    alerts,
    annotations,
    dashboard,
    portfolio,
    review,
    rules,
    screen,
    settings,
    symbol,
    theses,
)
```

2. `_templates()` 的 globals 里加：

```python
        moment_label=moment_label,
```

import 区补 `moment_label`：

```python
from sams.web.display import (
    action_label,
    alert_status,
    dismiss_label,
    hms,
    moment_label,
    money,
    pct,
    state_label,
)
```

3. `app.include_router(alerts.router)` 之后加：

```python
    app.include_router(screen.router)
```

`src/sams/web/templates/base.html` 导航里、`自选` 之后加：

```html
      <a href="/screen">选股</a>
```

- [ ] **Step 6: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py tests/web/test_assets.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/sams/web/pages/screen.py src/sams/web/templates/screen.html src/sams/web/app.py src/sams/web/templates/base.html tests/web/conftest.py tests/web/test_screen_web.py
git commit -m "feat(screener): /screen 骨架——范围行、条件加减行、字段置灰（D179/D188）"
```

---

### Task 5: 结果区——三个数、命中表、无数原因

**Files:**
- Modify: `src/sams/web/pages/screen.py`
- Modify: `src/sams/web/templates/screen.html`
- Modify: `tests/web/test_screen_web.py`

- [ ] **Step 1: 写失败测试**

`tests/web/test_screen_web.py` 追加：

```python
def _condition_params(**extra):
    pairs = [
        ("market", "A"),
        ("f", "pr_now"),
        ("o", "<="),
        ("v", "0.5"),
    ]
    pairs.extend(extra.items())
    return pairs


def test_every_condition_reports_passed_failed_missing_and_the_hits(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert response.status_code == 200
    assert "通过 2 / 不通过 1 / 无数 1" in response.text
    assert "600519" in response.text and "000001" in response.text
    assert "300750" not in response.text


def test_two_conditions_are_anded_and_the_table_carries_both_values(logged_screen):
    default_seed(logged_screen)
    params = _condition_params() + [("f", "turnover_rate"), ("o", ">="), ("v", "2")]
    response = logged_screen.client.get("/screen", params=params)
    assert "000001" in response.text
    assert "600519" not in response.text


def test_a_bad_value_keeps_the_draft_instead_of_losing_what_was_typed(logged_screen):
    response = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "pr_now"), ("o", "<="), ("v", "abc")],
    )
    assert response.status_code == 200
    assert "的值要一个数" in response.text
    assert 'value="abc"' in response.text


def test_an_unknown_field_is_refused_with_the_vocabulary(logged_screen):
    response = logged_screen.client.get(
        "/screen",
        params=[("market", "A"), ("f", "nope"), ("o", "<="), ("v", "1")],
    )
    assert response.status_code == 200
    assert "不认识的字段" in response.text and "可选的是" in response.text


def test_missing_codes_are_named_with_their_class(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.get("/screen", params=_condition_params())
    assert "无数 1 只 · pr_now：亏损（PE(动) ≤ 0）" in response.text
    assert "600000" in response.text
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q
```

Expected: FAIL —— 页面不显示三个数与命中。

- [ ] **Step 3: 实现结果计算**

`src/sams/web/pages/screen.py` 的 `_render` 里，`result` 求出之后、`return web.render` 之前补：

```python
    hits = result.hits if result is not None else ()
    total = len(hits)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    shown = hits[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    held = {code for item_market, code in ctx.watchlists.codes(user.id) if item_market == market}
    view = [_hit_row(hit, conditions, held) for hit in shown]
    counts_view = (
        [
            {"label": specs[item.field].label, "unit": specs[item.field].unit, **item.__dict__}
            for item in result.fields
        ]
        if result is not None
        else []
    )
    pager = {
        "prev": _url(market, rows, preset=preset, page=page - 1) if page > 1 else "",
        "next": _url(market, rows, preset=preset, page=page + 1) if page < pages else "",
    }
```

并在 `web.render(...)` 参数里补：

```python
        counts_view=counts_view,
        view=view,
        total=total,
        pages=pages,
        pager=pager,
        missing=result.missing if result is not None else (),
        missing_codes=MISSING_CODES,
```

文件末尾加：

```python
def _hit_row(hit, conditions: tuple[Condition, ...], held: set[str]) -> dict:
    return {
        "code": hit.code,
        "name": hit.name or "—",
        "cells": [
            "—" if hit.values.get(item.field) is None else f"{hit.values[item.field]:g}"
            for item in conditions
        ],
        "price": "—" if hit.values.get("price") is None else f"{hit.values['price']:g}",
        "held": hit.code in held,
    }
```

- [ ] **Step 4: 模板加结果区**

`src/sams/web/templates/screen.html` 在条件表单之后、空状态提示之前插入：

```html
{% if result %}
<p class="muted">
  {% for item in labels %}{{ item.label }}（{{ item.unit }}）{% if not loop.last %}；{% endif %}{% endfor %}
</p>
{% for counts in counts_view %}
<p>条件 {{ counts.label }}：通过 {{ counts.passed }} / 不通过 {{ counts.failed }} / 无数 {{ counts.missing }}</p>
{% endfor %}
<p>命中 {{ total }} 只</p>

<table>
  <tr><th>代码</th><th>名称</th>{% for item in labels %}<th class="num">{{ item.label }}</th>{% endfor %}<th class="num">现价</th></tr>
  {% for row in view %}
  <tr>
    <td>{{ row.code }}</td>
    <td>{{ row.name }}</td>
    {% for cell in row.cells %}<td class="num">{{ cell }}</td>{% endfor %}
    <td class="num">{{ row.price }}</td>
  </tr>
  {% endfor %}
</table>
<p class="muted">第 {{ page }} / {{ pages }} 页
  {% if pager.prev %}<a href="{{ pager.prev }}">上一页</a>{% endif %}
  {% if pager.next %}<a href="{{ pager.next }}">下一页</a>{% endif %}
</p>

{% if missing %}
<section class="card">
  <h2>无数出局的票</h2>
  {% for group in missing %}
  <p>无数 {{ group.codes|length }} 只 · {{ group.field }}：{{ group.reason }}</p>
  <p class="muted">{{ group.codes[:missing_codes]|join("、") }}{% if group.codes|length > missing_codes %}…（{{ group.codes|length - missing_codes }} 只未列）{% endif %}</p>
  {% endfor %}
</section>
{% endif %}
{% endif %}
```

- [ ] **Step 5: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/web/pages/screen.py src/sams/web/templates/screen.html tests/web/test_screen_web.py
git commit -m "feat(screener): 结果区——每条件三个数、命中表、无数原因点名（D180）"
```

---

### Task 6: 分页——一页 50 行，越界回到最后一页

**Files:**
- Modify: `tests/web/test_screen_web.py`

- [ ] **Step 1: 写失败测试**

```python
def test_results_are_paginated_fifty_at_a_time_and_page_two_keeps_the_conditions(logged_screen):
    codes = [f"{600000 + i:06d}" for i in range(55)]
    seed(
        logged_screen.context,
        *(snap(code) for code in codes),
        codes=tuple(codes),
    )
    first = logged_screen.client.get("/screen", params=_condition_params())
    assert "第 1 / 2 页" in first.text
    assert codes[49] in first.text and codes[50] not in first.text
    second = logged_screen.client.get(
        "/screen", params=_condition_params() + [("page", "2")]
    )
    assert "第 2 / 2 页" in second.text
    assert codes[50] in second.text and codes[0] not in second.text


def test_a_page_number_past_the_end_lands_on_the_last_page(logged_screen):
    seed(logged_screen.context, snap("600519"))
    response = logged_screen.client.get(
        "/screen", params=_condition_params() + [("page", "99")]
    )
    assert "第 1 / 1 页" in response.text and "600519" in response.text
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q -k paginated or past_the_end
```

Expected: FAIL —— `第 1 / 2 页` 不存在（Task 5 已渲染页码，但越界钳位与翻页链接的行为要先看它挂在哪；若已绿，说明 Task 5 提前实现了，删掉本任务重复步骤并在提交信息里说明）。

- [ ] **Step 3: 确认并补上缺失的一小块**

`_render` 里已有 `page = min(max(page, 1), pages)`；`pager` 已拼好。若测试红在别处，按失败信息修；**不要**改引擎的 `limit`（页面一页 50 行是显示层的事）。

- [ ] **Step 4: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/web/test_screen_web.py src/sams/web/pages/screen.py
git commit -m "test(screener): 结果分页 50 行一页、越界落回最后一页"
```

---

### Task 7: 加自选——单行 / 勾选 / 全部（先 dry-run）

**Files:**
- Modify: `src/sams/web/pages/screen.py`
- Modify: `src/sams/web/templates/screen.html`
- Modify: `tests/web/test_screen_web.py`

- [ ] **Step 1: 写失败测试**

```python
def _alice(logged_screen):
    return logged_screen.context.users.get_by_username("alice").id


def test_one_row_adds_through_the_watchlist_write_point(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/watchlist",
        data=[
            ("market", "A"),
            ("f", "pr_now"),
            ("o", "<="),
            ("v", "0.5"),
            ("action", "one:600519"),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert logged_screen.context.watchlists.contains(_alice(logged_screen), "A", "600519")


def test_checked_codes_are_added_and_unticked_ones_are_not(logged_screen):
    default_seed(logged_screen)
    logged_screen.client.post(
        "/screen/watchlist",
        data=[
            ("market", "A"),
            ("f", "pr_now"),
            ("o", "<="),
            ("v", "0.5"),
            ("action", "checked"),
            ("codes", "600519"),
            ("codes", "000001"),
        ],
        follow_redirects=False,
    )
    context = logged_screen.context
    assert context.watchlists.contains(_alice(logged_screen), "A", "600519")
    assert context.watchlists.contains(_alice(logged_screen), "A", "000001")
    assert not context.watchlists.contains(_alice(logged_screen), "A", "300750")


def test_add_all_first_shows_a_dry_run_and_writes_nothing(logged_screen):
    default_seed(logged_screen)
    context = logged_screen.context
    context.watchlists.add(_alice(logged_screen), "A", "600519")
    preview = logged_screen.client.post(
        "/screen/watchlist",
        data=[
            ("market", "A"),
            ("f", "pr_now"),
            ("o", "<="),
            ("v", "0.5"),
            ("action", "all"),
        ],
        follow_redirects=False,
    )
    assert preview.status_code == 303 and "confirm=all" in preview.headers["location"]
    page = logged_screen.client.get(preview.headers["location"])
    assert "全部 2 只里有 1 只已在自选" in page.text
    assert "这次会加入 1 只" in page.text
    assert not context.watchlists.contains(_alice(logged_screen), "A", "000001")


def test_add_all_confirmed_re_runs_the_conditions_on_the_server(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/watchlist",
        data=[
            ("market", "A"),
            ("f", "pr_now"),
            ("o", "<="),
            ("v", "0.5"),
            ("action", "all"),
            ("confirm", "yes"),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    context = logged_screen.context
    assert context.watchlists.contains(_alice(logged_screen), "A", "600519")
    assert context.watchlists.contains(_alice(logged_screen), "A", "000001")
    assert not context.watchlists.contains(_alice(logged_screen), "A", "300750")
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q -k "one_row or checked or add_all"
```

Expected: FAIL —— 404（`/screen/watchlist` 不存在）。

- [ ] **Step 3: 实现端点**

`src/sams/web/pages/screen.py` 的 `_render` 里、`counts_view` 之后补 dry-run 计划：

```python
    plan = None
    if confirm_all and result is not None:
        already = sum(1 for hit in hits if hit.code in held)
        quota = max(0, MAX_ITEMS_PER_USER - ctx.watchlists.count(user.id))
        plan = {
            "total": len(hits),
            "already": already,
            "will_add": min(len(hits) - already, quota),
            "capped": max(0, len(hits) - already - quota),
            "max": MAX_ITEMS_PER_USER,
        }
```

`web.render(...)` 参数里补：

```python
        plan=plan,
        confirm_all=confirm_all,
```

文件末尾加：

```python
@router.post("/screen/watchlist")
def add_to_watchlist(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    """三个入口最后都落在 `watchlists.add`（D184）：单行、勾选、全部。

    「全部」越过分页：服务端按这组条件重跑，不把屏幕上那 50 行提交回来。
    先一次 dry-run 页（`confirm=all`），按下去才真写。
    """
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    action = one(data, "action")
    if action == "all":
        if one(data, "confirm") != "yes":
            return RedirectResponse(_url(market, rows, confirm=True), status_code=303)
        try:
            result = ctx.screener.run(market, _conditions(rows))
        except ValidationError as exc:
            return _render(request, user, market=market, preset=None, rows=rows, error=str(exc))
        codes = [hit.code for hit in result.hits]
    elif action == "checked":
        codes = [code.strip() for code in many(data, "codes") if code.strip()]
    elif action.startswith("one:"):
        codes = [action.partition(":")[2].strip()]
    else:
        raise HTTPException(status_code=400, detail="不认识的加自选动作")
    added = already = capped = 0
    for code in dict.fromkeys(codes):
        if ctx.watchlists.contains(user.id, market, code):
            already += 1
            continue
        try:
            ctx.watchlists.add(user.id, market, code)
        except ValidationError:
            capped += 1
            continue
        ctx.symbol_interest.ensure(market, code, actor=user.username)
        added += 1
    return RedirectResponse(
        _url(
            market,
            rows,
            notice="added",
            extra={"added": added, "already": already, "capped": capped},
        ),
        status_code=303,
    )
```

`_notice` 里补：

```python
    if kind == "added":
        added = _count(query.get("added"))
        already = _count(query.get("already"))
        capped = _count(query.get("capped"))
        parts = [f"加入自选 {added} 只"]
        if already:
            parts.append(f"{already} 只原本就在")
        if capped:
            parts.append(f"{capped} 只因自选上限 {MAX_ITEMS_PER_USER} 只没加")
        return "；".join(parts)
```

文件末尾加：

```python
def _count(text) -> int:
    try:
        return max(0, int(text or 0))
    except ValueError:
        return 0
```

- [ ] **Step 4: 模板——整张表一个表单**

把 Task 5 的 `<table>` 区块替换为：

```html
{% if plan %}
<p class="flash-ok">全部 {{ plan.total }} 只里有 {{ plan.already }} 只已在自选：这次会加入 {{ plan.will_add }} 只{% if plan.capped %}，另有 {{ plan.capped }} 只因自选上限 {{ plan.max }} 只没加{% endif %}。</p>
<form method="post" action="/screen/watchlist" class="inline-form">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <input type="hidden" name="market" value="{{ market }}">
  {% for row in rows %}<input type="hidden" name="f" value="{{ row.field }}"><input type="hidden" name="o" value="{{ row.op }}"><input type="hidden" name="v" value="{{ row.value }}"{% endfor %}>
  <input type="hidden" name="action" value="all">
  <input type="hidden" name="confirm" value="yes">
  <button type="submit">确认加入 {{ plan.will_add }} 只</button>
</form>
{% endif %}

<form method="post" action="/screen/watchlist">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <input type="hidden" name="market" value="{{ market }}">
  {% for row in rows %}<input type="hidden" name="f" value="{{ row.field }}"><input type="hidden" name="o" value="{{ row.op }}"><input type="hidden" name="v" value="{{ row.value }}"{% endfor %}>
  <p>
    <button type="submit" name="action" value="all">全部 {{ total }} 只加自选</button>
    <button type="submit" name="action" value="checked">勾选的加自选</button>
  </p>
  <table>
    <tr><th></th><th>代码</th><th>名称</th>{% for item in labels %}<th class="num">{{ item.label }}</th>{% endfor %}<th class="num">现价</th><th></th></tr>
    {% for row in view %}
    <tr>
      <td><input type="checkbox" name="codes" value="{{ row.code }}"></td>
      <td>{{ row.code }}</td>
      <td>{{ row.name }}</td>
      {% for cell in row.cells %}<td class="num">{{ cell }}</td>{% endfor %}
      <td class="num">{{ row.price }}</td>
      <td>{% if row.held %}<span class="badge">已在自选</span>{% else %}<button type="submit" name="action" value="one:{{ row.code }}">加自选</button>{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  <p class="muted">第 {{ page }} / {{ pages }} 页
    {% if pager.prev %}<a href="{{ pager.prev }}">上一页</a>{% endif %}
    {% if pager.next %}<a href="{{ pager.next }}">下一页</a>{% endif %}
  </p>
</form>
```

- [ ] **Step 5: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/web/pages/screen.py src/sams/web/templates/screen.html tests/web/test_screen_web.py
git commit -m "feat(screener): 三种加自选与全部 dry-run（D184）"
```

---

### Task 8: 「立即重采」只排队

**Files:**
- Modify: `tests/web/test_screen_web.py`

- [ ] **Step 1: 写失败测试**

```python
def test_recollect_queues_the_spot_job_and_says_so(logged_screen):
    default_seed(logged_screen)
    response = logged_screen.client.post(
        "/screen/recollect",
        data=[("market", "A")],
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = logged_screen.client.get(response.headers["location"])
    assert "排进下一拍" in page.text
    assert logged_screen.context.scheduler.pending_requests == 1
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q -k recollect
```

Expected: 若 Task 4 已把端点写好，这一条可能直接绿；那就是「端点已在、但排队语义没有测试钉住」——保留这条测试并在提交信息里写明。若红，按失败信息修端点（只排队，不在请求线程里采集）。

- [ ] **Step 3: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py -q -k recollect
```

Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add tests/web/test_screen_web.py
git commit -m "test(screener): 立即重采只排进下一拍，不在请求线程里打外网（D176）"
```

---

### Task 9: 方案六个动作

**Files:**
- Modify: `src/sams/web/pages/screen.py`
- Modify: `src/sams/web/templates/screen.html`
- Modify: `tests/web/test_screen_web.py`

- [ ] **Step 1: 写失败测试**

```python
def _save(logged_screen, name="便宜", *, market="A", f="pr_now", o="<=", v="0.5"):
    return logged_screen.client.post(
        "/screen/presets",
        data=[("market", market), ("f", f), ("o", o), ("v", v), ("name", name)],
        follow_redirects=False,
    )


def test_save_then_the_list_reads_back_as_a_sentence_not_json(logged_screen):
    default_seed(logged_screen)
    response = _save(logged_screen)
    assert response.status_code == 303
    page = logged_screen.client.get("/screen")
    assert "便宜" in page.text
    assert "现值市赚率 PR（小数比值（0.5 = 50%）） ≤ 0.5" in page.text


def test_saving_no_conditions_is_refused(logged_screen):
    response = logged_screen.client.post(
        "/screen/presets",
        data=[("market", "A"), ("f", ""), ("o", "<="), ("v", ""), ("name", "空")],
    )
    assert response.status_code == 200 and "一条条件都没有" in response.text
    assert logged_screen.context.screener_presets.list(_alice(logged_screen)) == []


def test_a_duplicate_name_is_an_error_not_a_silent_overwrite(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    response = _save(logged_screen)
    assert response.status_code == 200
    assert "已有同名方案" in response.text
    assert len(logged_screen.context.screener_presets.list(_alice(logged_screen))) == 1


def test_run_links_back_to_the_preset_and_conditions_show_up_in_the_form(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    response = logged_screen.client.post(
        f"/screen/presets/{preset.id}/run", follow_redirects=False
    )
    assert response.status_code == 303 and "preset=" in response.headers["location"]
    page = logged_screen.client.get(response.headers["location"])
    assert "通过 2 / 不通过 1 / 无数 1" in page.text


def test_rename_overwrite_and_delete(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    assert logged_screen.client.post(
        f"/screen/presets/{preset.id}/rename", data=[("name", "很便宜")], follow_redirects=False
    ).status_code == 303
    page = logged_screen.client.get("/screen")
    assert "很便宜" in page.text and "便宜" not in page.text.split("筛选方案")[1]

    assert logged_screen.client.post(
        f"/screen/presets/{preset.id}",
        data=[("market", "A"), ("f", "pr_now"), ("o", "<="), ("v", "0.3")],
        follow_redirects=False,
    ).status_code == 303
    page = logged_screen.client.get(f"/screen?preset={preset.id}")
    assert "0.3" in page.text

    assert logged_screen.client.post(
        f"/screen/presets/{preset.id}/delete", follow_redirects=False
    ).status_code == 303
    assert logged_screen.context.screener_presets.list(_alice(logged_screen)) == []


def test_another_users_preset_is_a_404_without_saying_whose_it_is(logged_screen):
    other = logged_screen.context.users.create("bob", "correct-horse-battery")
    foreign = logged_screen.context.screener_presets.create(
        other.id, "别人的", "A", [Condition("pr_now", "<=", 0.5)]
    )
    for path in (
        f"/screen/presets/{foreign.id}/run",
        f"/screen/presets/{foreign.id}/delete",
    ):
        response = logged_screen.client.post(path, follow_redirects=False)
        assert response.status_code == 404
    assert logged_screen.context.screener_presets.list(other.id) != []
```

`tests/web/test_screen_web.py` 顶部补 import：

```python
from sams.core.screener_fields import Condition
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q -k "save or duplicate or run_links or rename or another_users"
```

Expected: FAIL —— 404（方案端点不存在）。

- [ ] **Step 3: 实现六个动作**

`src/sams/web/pages/screen.py` 顶部 import 补：

```python
from sams.core.screener_fields import describe_all
```

`_render` 里、`return web.render` 之前补方案列表：

```python
    presets_view = []
    for item in ctx.screener_presets.list(user.id):
        presets_view.append(
            {
                "id": item.id,
                "name": item.name,
                "market": item.market,
                "summary": describe_all(item.conditions),
                "updated": moment_label(item.updated_at, ctx.settings.server.display_tz),
            }
        )
```

`web.render(...)` 参数里补：

```python
        presets=presets_view,
```

文件末尾加：

```python
@router.post("/screen/presets")
def save_preset(
    request: Request,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    try:
        ctx.screener_presets.create(user.id, one(data, "name"), market, list(_conditions(rows)))
    except ValidationError as exc:
        return _render(request, user, market=market, preset=None, rows=rows, error=str(exc))
    return RedirectResponse(_url(market, rows, notice="saved"), status_code=303)


@router.post("/screen/presets/{preset_id}/run")
def run_preset(request: Request, preset_id: str, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    try:
        ctx.screener.run(preset.market, list(preset.conditions))
    except ValidationError as exc:
        rows = [_draft_of(item) for item in preset.conditions]
        return _render(
            request, user, market=preset.market, preset=preset, rows=rows, error=str(exc)
        )
    return RedirectResponse(_url(preset.market, [], preset=preset, notice="ran"), status_code=303)


@router.post("/screen/presets/{preset_id}/rename")
def rename_preset(
    request: Request,
    preset_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    rows = [_draft_of(item) for item in preset.conditions]
    try:
        ctx.screener_presets.rename(user.id, preset.id, one(data, "name"))
    except ValidationError as exc:
        return _render(
            request, user, market=preset.market, preset=preset, rows=rows, error=str(exc)
        )
    return RedirectResponse(
        _url(preset.market, [], preset=preset, notice="renamed"), status_code=303
    )


@router.post("/screen/presets/{preset_id}")
def overwrite_preset(
    request: Request,
    preset_id: str,
    _csrf: None = Depends(csrf_protect),
    data: dict[str, list[str]] = _FORM_BODY,
):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    market = one(data, "market").strip().upper()
    rows = _posted_rows(data)
    if market != preset.market:
        return _render(
            request,
            user,
            market=market,
            preset=preset,
            rows=rows,
            error=f"方案的市场不能改（现在是 {preset.market}）：要换市场请另存一条",
        )
    try:
        ctx.screener_presets.update_conditions(user.id, preset.id, list(_conditions(rows)))
    except ValidationError as exc:
        return _render(
            request, user, market=market, preset=preset, rows=rows, error=str(exc)
        )
    return RedirectResponse(
        _url(market, rows, preset=preset, notice="updated"), status_code=303
    )


@router.post("/screen/presets/{preset_id}/delete")
def delete_preset(request: Request, preset_id: str, _csrf: None = Depends(csrf_protect)):
    user = require_user(request)
    ctx = request.app.state.web.contexts.current()
    preset = _preset(ctx, user.id, preset_id)
    ctx.screener_presets.delete(user.id, preset.id)
    return RedirectResponse(_url(preset.market, [], notice="deleted"), status_code=303)
```

`_notice` 里补：

```python
    if kind == "saved":
        return "方案已保存"
    if kind == "renamed":
        return "方案已改名"
    if kind == "updated":
        return "方案的条件已覆盖为当前这一组"
    if kind == "deleted":
        return "方案已删除（已加进自选的票不受影响）"
    if kind == "ran":
        return "方案已按今天的快照重跑"
```

- [ ] **Step 4: 模板加方案区**

`screen.html` 末尾、`{% endblock %}` 之前加：

```html
<section class="card">
  <h2>筛选方案</h2>
  {% if rows and rows[0].field %}
  <form method="post" action="/screen/presets" class="inline-form">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <input type="hidden" name="market" value="{{ market }}">
    {% for row in rows %}<input type="hidden" name="f" value="{{ row.field }}"><input type="hidden" name="o" value="{{ row.op }}"><input type="hidden" name="v" value="{{ row.value }}"{% endfor %}>
    <label>名字 <input name="name" maxlength="40"></label>
    <button type="submit">保存当前条件</button>
  </form>
  {% else %}
  <p class="muted">先在上面填一条条件，才能存成方案。</p>
  {% endif %}
  <table>
    <tr><th>名字</th><th>市场</th><th>条件</th><th>更新</th><th>动作</th></tr>
    {% for item in presets %}
    <tr>
      <td>{{ item.name }}</td>
      <td>{{ item.market }}</td>
      <td>{{ item.summary }}</td>
      <td class="muted">{{ item.updated }}</td>
      <td>
        <form method="post" action="/screen/presets/{{ item.id }}/run" class="inline-form">
          <input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">重跑</button>
        </form>
        <form method="post" action="/screen/presets/{{ item.id }}/rename" class="inline-form">
          <input type="hidden" name="csrf" value="{{ csrf }}">
          <input name="name" value="{{ item.name }}" size="10"><button type="submit">改名</button>
        </form>
        {% if rows and rows[0].field %}
        <form method="post" action="/screen/presets/{{ item.id }}" class="inline-form">
          <input type="hidden" name="csrf" value="{{ csrf }}">
          <input type="hidden" name="market" value="{{ market }}">
          {% for row in rows %}<input type="hidden" name="f" value="{{ row.field }}"><input type="hidden" name="o" value="{{ row.op }}"><input type="hidden" name="v" value="{{ row.value }}"{% endfor %}>
          <button type="submit">覆盖为当前条件</button>
        </form>
        {% endif %}
        <form method="post" action="/screen/presets/{{ item.id }}/delete" class="inline-form">
          <input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">删除</button>
        </form>
      </td>
    </tr>
    {% else %}
    <tr><td colspan="5" class="muted">还没有方案。</td></tr>
    {% endfor %}
  </table>
</section>
```

- [ ] **Step 5: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/web/pages/screen.py src/sams/web/templates/screen.html tests/web/test_screen_web.py
git commit -m "feat(screener): 方案六个动作——保存/列表/重跑/改名/覆盖/删除（D182）"
```

---

### Task 10: 重跑写 `scan_results`，列表显示「上次跑」

**Files:**
- Modify: `src/sams/web/pages/screen.py`
- Modify: `src/sams/web/templates/screen.html`
- Modify: `tests/web/test_screen_web.py`

- [ ] **Step 1: 写失败测试**

```python
def test_running_a_preset_records_the_basket_for_that_day(logged_screen):
    from sams.services.screener import preset_strategy_id

    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    logged_screen.client.post(f"/screen/presets/{preset.id}/run", follow_redirects=False)
    strategy = preset_strategy_id(preset.name, preset.conditions)
    records = logged_screen.context.scan_store.recent(market="A", strategy_id=strategy, limit=1)
    assert records and records[0]["date"] == DAY
    assert {hit["code"] for hit in records[0]["hits"]} == {"600519", "000001"}


def test_the_list_says_when_the_current_version_last_ran(logged_screen):
    default_seed(logged_screen)
    _save(logged_screen)
    preset = logged_screen.context.screener_presets.list(_alice(logged_screen))[0]
    page = logged_screen.client.get("/screen")
    assert "没跑过" in page.text
    logged_screen.client.post(f"/screen/presets/{preset.id}/run", follow_redirects=False)
    page = logged_screen.client.get("/screen")
    assert "没跑过" not in page.text
```

- [ ] **Step 2: 跑一遍确认红**

```bash
uv run pytest tests/web/test_screen_web.py -q -k "records_the_basket or last_ran"
```

Expected: FAIL —— 重跑没有写 `scan_results`；列表没有这一列。

- [ ] **Step 3: 实现**

`src/sams/web/pages/screen.py` 顶部 import 补：

```python
from sams.services.screener import preset_strategy_id
```

`run_preset` 里，在 `ctx.screener.run(...)` 之后、`return RedirectResponse(...)` 之前补：

```python
    result = ctx.screener.run(preset.market, list(preset.conditions))
    if result.day is not None:
        ctx.scan_store.record(
            on=result.day,
            market=preset.market,
            strategy_id=preset_strategy_id(preset.name, preset.conditions),
            universe_size=result.universe_size,
            hits=[{"code": hit.code, "name": hit.name, **hit.values} for hit in result.hits],
        )
```

（`try/except` 里那次 `run` 是用来接校验错的；实现时只跑一次：先 `try: result = ...`，成功后再写。）

`_render` 的 `presets_view` 循环里补「上次跑」：

```python
        strategy = preset_strategy_id(item.name, item.conditions)
        runs = ctx.scan_store.recent(market=item.market, strategy_id=strategy, limit=1)
        presets_view.append(
            {
                "id": item.id,
                "name": item.name,
                "market": item.market,
                "summary": describe_all(item.conditions),
                "updated": moment_label(item.updated_at, ctx.settings.server.display_tz),
                "last_run": moment_label(runs[0]["created_at"], ctx.settings.server.display_tz)
                if runs
                else "",
            }
        )
```

模板表头加 `<th>上次跑</th>`，行里加：

```html
      <td class="muted">{{ item.last_run or "没跑过" }}</td>
```

（`colspan` 从 5 改成 6。）

- [ ] **Step 4: 跑测试确认绿**

```bash
uv run pytest tests/web/test_screen_web.py tests/services/test_screener.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/web/pages/screen.py src/sams/web/templates/screen.html tests/web/test_screen_web.py
git commit -m "feat(screener): 重跑写 scan_results，方案列表显示上次跑（D183）"
```

---

### Task 11: lint、全量、真页面验收

**Files:**
- Modify: `docs/superpowers/specs/2026-10-28-screener-market-snapshot-design.md`（实测回写）
- Modify: `docs/superpowers/plans/2026-10-28-sp3-e-screener-page-presets.md`（交付记录）

- [ ] **Step 1: lint 与相关测试**

```bash
uv run ruff check --no-cache src/sams tests
uv run pytest tests/storage/test_screener_presets.py tests/core/test_screener_fields.py tests/services/test_screener.py tests/web/test_screen_web.py tests/storage/test_migrate.py -q
```

Expected: lint 全绿；测试全绿。

- [ ] **Step 2: 全量（主控跑，不加管道）**

```bash
uv run pytest -q
```

Expected: 0 failed。总数 ≈ 4335 + 本计划新增（约 35）。

- [ ] **Step 3: 真页面（先停 serve，迁移 0024 再起）**

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN | awk 'NR>1 {print $2}' | xargs -r kill
SAMS_SECRET_KEY="$(cat tmp/.sams_secret)" nohup uv run sams serve > tmp/serve.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}\n' localhost:8765/healthz
```

Expected: `200`（迁移在打开库时自动应用，`schema_migrations` 里出现 0024）。

- [ ] **Step 4: 用真快照走一遍（浏览器或 curl 登录）**

用管理员账号登录后打开 `http://localhost:8765/screen`：

- 范围行应写 `A 股 · 快照 2026-10-28 · 票池 5577 只`；
- 选 `现值市赚率 PR ≤ 0.5` → 通过 173 / 不通过 3945 / 无数 1459，无数三组（亏损 1451 / 净资产为负 2 / ROE 过界 6）；
- 「全部 173 只加自选」先出 dry-run 数，再确认；同一页「已在自选」标记出现；
- 存一个方案「便宜」→ 重跑 → 列表出现「上次跑」时间；改名、覆盖、删除各点一次；
- 「立即重采」点一次，页面提示已排队（任务跑到下一拍时 `snapshot_daily` 的 `taken_at` 会变）。

把实测偏差记下来：页面数字与 CLI 不一致就以 CLI 的引擎输出为准查页面。

- [ ] **Step 5: 回写 spec 与计划**

spec §3.1 末尾加一行：

```markdown
- 2026-10-28 页面验收：`/screen` 在 5577 行真快照上 `pr_now ≤ 0.5` 显示
  通过 173 / 不通过 3945 / 无数 1459（亏损 1451、净资产为负 2、ROE 过界 6）；
  方案保存/重跑/改名/覆盖/删除各走一遍，「全部加自选」的 dry-run 数与实际加入数一致。
```

计划末尾加「交付记录」：实际新增测试数、页面验收时发现的偏差与修法（没有就写「无」）。

- [ ] **Step 6: 提交**

```bash
git add docs/superpowers/specs/2026-10-28-screener-market-snapshot-design.md docs/superpowers/plans/2026-10-28-sp3-e-screener-page-presets.md
git commit -m "docs(screener): 计划 E 页面验收与实测回写"
```

---

## 完成标准

- `/screen` 打开即报范围（市场、快照日、采集时刻、票池三个数），没有条件时不假装有结果；
- 条件可加、可删、可组合，字段下拉按词表生成，非 A 股字段置灰且写明为什么；
- 每条条件报通过 / 不通过 / 无数，三者相加 = 票池；无数票按类归组并点名；
- 结果分页 50 行；加自选三种入口都经 `watchlists.add`，「全部」先 dry-run、服务端重跑、越过 500 上限时点名没加的只数；
- 方案六动作齐备，跨用户 404，撞名出声不静默覆盖；重跑写 `scan_results` 并显示「上次跑」；
- 「立即重采」只排队，不在请求线程里采；
- lint 绿、全量绿、真快照页面上数字与 CLI 一致。

## 交付记录（2026-10-28）

- **新增测试 36 条**：`tests/storage/test_screener_presets.py` 9 条、
  `tests/web/test_screen_web.py` 25 条、`tests/core/test_screener_fields.py` 1 条
  （`describe_all` 是条件唯一的可读拼法）、`tests/services/test_screener.py` 1 条
  （`preset_strategy_id` 只在名字或条件动时动）；`tests/storage/test_migrate.py`
  只多登记 `screener_presets` 一张表。
- **与计划的偏差**：
  1. POST 用例的负载一律用 dict，不用计划里的 `data=[("k", "v"), …]`：httpx 把元组串当
     **流**发，`csrf` 不在表单里，真实结果是 403（`DeprecationWarning` 也指向这一点）。
     多值字段（`f/o/v`、`codes`）用 list。
  2. 采集时刻先在 Python 里 `moment_label(…, display_tz)` 成 `taken_label` 再进模板：
     模板里没有 `display_tz`，时区换算也不该写在模板里。
  3. 票池读 `ctx.symbol_list_store.active_codes(market)`：计划写的 `ctx.symbols` 是
     `SymbolRepo`，没有这个方法。
  4. 范围行在「有结果」时也得给出：实现之初只在 `result is None` 里回库读快照，真页面上
     有 173 条结果的那一页反而写「还没有全市场快照」。修法见 `ba0c62b`——有结果时直接用
     引擎读到的那一份（日、`taken_at`、票池），并补一条回归用例
     `test_the_range_line_survives_a_successful_screen`。
  5. 「改名 / 覆盖」之后「上次跑」会空：它的键是 `preset_strategy_id(name, conditions)`，
     名字或条件一换就是另一条策略的历史。这是 D183 的口径，不是 bug。
- **页面验收实测**（真库，快照 5577 行）：`pr_now ≤ 0.5` 通过 173 / 不通过 3945 /
  无数 1459（亏损 1451、净资产为负 2、ROE 过界 6），与 CLI 同一组数；「全部加自选」
  dry-run 173/0/0，确认后实际也是 173/0/0；方案的保存、重跑（写 `scan_results`、列表出现
  「上次跑」）、改名、覆盖、删除各走一遍；「立即重采」303 回页面并提示已排队。
- **遗留（不属本期）**：上一台 serve 刚停就重启时，新进程的调度线程可能因旧心跳还新鲜而
  `SchedulerBusy` 退出且不重试；这段时间里「立即重采」排队后不会被执行。等旧心跳过期再
  重启一次即恢复，下一步单独修重试。
