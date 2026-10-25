# M4-B 计划 1：批注与闭环的存储/服务（seam） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M4-B 的地基一次铺完——迁移 0020、告警闭环状态机与 `alert_events`、批注仓库/服务、Markdown 渲染与纯文本抽取、`[annotations]` 配置与组装接线。做完本计划，库里能写、能读、能审一条私有批注，告警能 ack/act/dismiss/defer/undo，但还没有任何网页入口。

**Architecture:** 迁移只加列/加表/加索引，不改既有行。状态机在仓库层强制（D81），每次迁移往 `alert_events` 写一行；批注的可见性过滤下沉到 SQL，目标归属校验放在服务层；Markdown 是 `services` 里的纯函数模块，禁用原始 HTML/图片、链接只放行 http/https/mailto。`monitor` 不认识批注——推送上下文在计划 3 通过注入的 provider 接入。

**Tech Stack:** Python 3.13、DuckDB、polars、pytest、ruff、uv、`markdown-it-py`（本计划新增）。

**前置：** M4-A 三个计划已完成并合入（队列、运行时、Web 都在）。当前 HEAD 应为 `fcd207a` 或其后。

**计划切分（本文件只做第 1 份）：**

1. **本计划**：迁移 0020 + 告警状态机/事件 + 批注仓库/服务 + Markdown + 配置 + 组装（spec 步骤 1–2）。
2. 计划 2：附件（spec 步骤 3）、推送上下文（步骤 5）、闭环动作服务与成交原子流程（步骤 6 的服务层）。
3. 计划 3：批注 Web 面板、告警闭环 UI、`/review`（步骤 4、6 的页面、7）。页面切片可 2–3 路并行。
4. 计划 4：B2 团队共享、管理页、break-glass、全局搜索（步骤 8）。

---

## File Structure

**新建**

| 路径 | 职责 |
|---|---|
| `src/sams/storage/migrations/0020_annotations_review.sql` | 0020 的全部 DDL |
| `src/sams/storage/repos/alert_events.py` | 闭环动作历史：写入与读取 |
| `src/sams/storage/repos/annotations.py` | 批注与编辑历史的读写、可见性 SQL 过滤 |
| `src/sams/services/annotations.py` | 五类目标的归属校验、标签规范化、编辑/置顶/软删用例 |
| `src/sams/services/markdown.py` | 服务端 Markdown 渲染（安全配置）与推送纯文本抽取 |
| `tests/storage/test_migration_0020.py` | 旧库幂等、旧行不被改写、默认值 |
| `tests/storage/test_alert_events_repo.py` | 事件表契约 |
| `tests/storage/test_annotations_repo.py` | 批注仓库：可见性、标签、revision、软删、置顶上限 |
| `tests/services/test_annotations_service.py` | 五类目标授权、正文/标签校验、编辑历史 |
| `tests/services/test_markdown.py` | XSS、图片、链接白名单、纯文本抽取 |

**修改**

| 路径 | 改动 |
|---|---|
| `src/sams/core/errors.py` | 新增 `NotFound`、`Conflict` |
| `src/sams/core/config.py` | 新增 `AnnotationsCfg`、`ServerCfg.public_base_url`、`Settings.annotations` |
| `src/sams/storage/repos/alerts.py` | 状态扩为四值、新列、`acknowledge/act/dismiss/defer/undo`、`latest_open` 含 acknowledged |
| `src/sams/services/health.py` | 积压四条查询把 `acknowledged` 算作未处理 |
| `src/sams/cli/alert.py` | 状态词表与帮助文本加 `acknowledged` |
| `src/sams/web/display.py` | `ALERT_STATUS_LABELS` 加 `acknowledged` |
| `src/sams/bootstrap.py` | `annotations_store` / `annotations` 装进 `ServiceContext` |
| `config/settings.example.toml` | 追加 `[annotations]` 与 `[server].public_base_url` |
| `tests/storage/test_alerts_repo.py` | 冻结时钟夹具、状态机用例 |
| `tests/services/test_health.py` | 积压口径改为 new+acknowledged |
| `tests/cli/test_alert_cli.py` | `--status acknowledged` 显示「已读」 |
| `tests/core/test_config.py` | `[annotations]` 与 `public_base_url` 的加载/校验 |
| `tests/test_bootstrap.py` | 容器里批注服务可用 |

**命名约定（后续计划沿用）：**
- 状态常量：`NEW, ACKNOWLEDGED, HANDLED, DISMISSED`。
- 动作常量：`ACTION_KINDS = ("trade", "thesis", "rule", "watch", "other")`；忽略原因 `DISMISS_CODES = ("misreport", "expected", "data_issue", "not_applicable", "duplicate", "other")`。
- 事件动作：`("ack", "defer", "act", "dismiss", "undo")`。
- `target_ref`：symbol 用 `A:600519`（创建时也接受 `A/600519`，落库统一成冒号）。

---

### Task 1: 迁移 0020

**Files:**
- Create: `src/sams/storage/migrations/0020_annotations_review.sql`
- Test: `tests/storage/test_migration_0020.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/storage/test_migration_0020.py`：

```python
"""0020：批注、闭环与审计的存储面（M4-B spec §3）。

这一份钉三件事：新表/新列真的在；策略默认值真的是 `on`；**旧行不被改写**——
迁移只加列，一条 M3 时代的 `handled` 在升级后仍然是 `action_kind = NULL`，
复盘里它会单列「旧版」，而不是被回填成一个我们猜的动作。
"""

from __future__ import annotations

import shutil

from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations


def fresh(tmp_path) -> Database:
    db = Database.open(str(tmp_path / "x.duckdb"))
    apply_migrations(db)
    return db


def test_new_tables_exist(tmp_path):
    db = fresh(tmp_path)
    names = db.table_names()
    for table in ("alert_events", "annotation_attachments", "app_settings", "admin_access_log"):
        assert table in names, table


def test_alerts_gains_the_closure_columns(tmp_path):
    db = fresh(tmp_path)
    assert {
        "acknowledged_at",
        "action_kind",
        "action_ref",
        "dismiss_code",
        "thesis_id",
        "deferred_until",
    } <= db.columns("alerts")


def test_annotations_gain_snapshot_and_tombstone_columns(tmp_path):
    db = fresh(tmp_path)
    assert {"target_label", "deleted_at", "deleted_by", "purged_at"} <= db.columns("annotations")
    assert "meta_json" in db.columns("annotation_revisions")
    assert "context_json" in db.columns("notify_log")


def test_team_sharing_defaults_to_on(tmp_path):
    db = fresh(tmp_path)
    value = db.scalar("SELECT value FROM app_settings WHERE key = 'annotations.team_sharing'")
    assert value == "on"


def test_an_old_handled_row_is_not_rewritten(tmp_path):
    """先只应用 0001–0019，写一条旧式 handled，再应用 0020。"""
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0020":
            shutil.copy(path, partial / path.name)
    db = Database.open(str(tmp_path / "old.duckdb"))
    apply_migrations(db, partial)
    db.execute(
        "INSERT INTO alerts (id, user_id, rule_id, account_id, market, code, stage,"
        " triggered_at, payload_json, status, handled_at, handle_note, dismiss_reason,"
        " severity) VALUES ('a1', 'u1', 'r1', NULL, 'A', '601088', '入池',"
        " TIMESTAMPTZ '2026-10-20 14:30:00+00', '{}', 'handled',"
        " TIMESTAMPTZ '2026-10-20 14:31:00+00', '已挂单', NULL, 'mid')"
    )

    shutil.copy(MIGRATIONS_DIR / "0020_annotations_review.sql", partial)
    assert apply_migrations(db, partial) == ["0020_annotations_review.sql"]

    row = db.query(
        "SELECT status, handled_at, action_kind, acknowledged_at, dismiss_code"
        " FROM alerts WHERE id = 'a1'"
    ).to_dicts()[0]
    assert row["status"] == "handled"
    assert row["handled_at"] is not None
    assert row["action_kind"] is None
    assert row["acknowledged_at"] is None
    assert row["dismiss_code"] is None


def test_reapplying_is_a_noop(tmp_path):
    db = fresh(tmp_path)
    assert apply_migrations(db) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_migration_0020.py -q`
Expected: FAIL —— `assert table in names` / 列缺失。

- [ ] **Step 3: 写迁移**

创建 `src/sams/storage/migrations/0020_annotations_review.sql`：

```sql
-- 0020：批注、闭环动作与管理员授权的存储面（M4-B spec §3，D121–D139）。
--
-- 1) `alerts` 的六列都是**可空**。它们描述的是「人对这条告警做了什么」：
--    `acknowledged_at` 是看过，`action_kind/action_ref` 是处置到了哪，`dismiss_code`
--    是忽略原因的结构化部分，`deferred_until` 是「到点再来」，`thesis_id` 是规则经
--    观点展开时留下的快照。旧行没有这些信息，回填任何值都是伪造历史。
--
-- 2) `alert_events` 是写多读少的审计表：`alerts` 上的列是**当前结论**，这张表是
--    「谁在什么时候把它从什么改成了什么」。没有它，undo 找不到上一态，复盘也答不出
--    「先已读后处置」与「直接处置」的区别。动作字符串与仓库层 `ACTIONS` 同一组。
--
-- 3) `notify_log.context_json` 记这一次发送实际带入了哪几条批注（计划 3 写入）。
--
-- 4) `annotations.target_label` 是创建时的显示名快照：标的改名、规则删除、成交页
--    不可达之后，批注列表仍能说清「这是关于什么的」。DuckDB 不支持
--    `ADD COLUMN ... NOT NULL DEFAULT ...`（0019 的实测），所以照旧走
--    可空列 → SET DEFAULT → 回填三步；读侧仍用 coalesce 兜底。
--
-- 5) `annotation_attachments` 与 `annotations.attachments_json` 并存：旧列保持写
--    `'[]'`，不删（迁移只增不改）；附件的唯一出处是新表。
--
-- 6) `app_settings` 是运行时策略（D127）。写入键必须有白名单（服务层），这里只给
--    唯一一个首期策略一个默认值 `on`。
--
-- 7) `admin_access_log` 既是 break-glass 的审计，也是授权与限次的唯一依据：
--    授权时写一行，`granted_until` 决定该条是否仍可读；不存正文与附件内容。

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS action_kind VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS action_ref VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS dismiss_code VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS thesis_id VARCHAR;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS deferred_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS alert_events (
    id          VARCHAR PRIMARY KEY,
    alert_id    VARCHAR NOT NULL,
    user_id     VARCHAR NOT NULL,
    action      VARCHAR NOT NULL,
    from_status VARCHAR NOT NULL,
    to_status   VARCHAR NOT NULL,
    note        VARCHAR,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert
    ON alert_events (alert_id, ts);

ALTER TABLE notify_log ADD COLUMN IF NOT EXISTS context_json VARCHAR;

ALTER TABLE annotations ADD COLUMN IF NOT EXISTS target_label VARCHAR;
ALTER TABLE annotations ALTER COLUMN target_label SET DEFAULT '';
UPDATE annotations SET target_label = '' WHERE target_label IS NULL;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_by VARCHAR;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;

ALTER TABLE annotation_revisions ADD COLUMN IF NOT EXISTS meta_json VARCHAR;
ALTER TABLE annotation_revisions ALTER COLUMN meta_json SET DEFAULT '{}';
UPDATE annotation_revisions SET meta_json = '{}' WHERE meta_json IS NULL;

CREATE TABLE IF NOT EXISTS annotation_attachments (
    id            VARCHAR PRIMARY KEY,
    annotation_id VARCHAR NOT NULL,
    user_id       VARCHAR NOT NULL,
    filename      VARCHAR NOT NULL,
    media_type    VARCHAR NOT NULL,
    size_bytes    BIGINT NOT NULL,
    sha256        VARCHAR NOT NULL,
    storage_path  VARCHAR NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_annotations_target
    ON annotations (target_type, target_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_annotations_user
    ON annotations (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_annotation_attachments_annotation
    ON annotation_attachments (annotation_id, position);

CREATE TABLE IF NOT EXISTS app_settings (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR
);
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('annotations.team_sharing', 'on');

CREATE TABLE IF NOT EXISTS admin_access_log (
    id            VARCHAR PRIMARY KEY,
    admin_user_id VARCHAR NOT NULL,
    annotation_id VARCHAR NOT NULL,
    owner_user_id VARCHAR NOT NULL,
    action        VARCHAR NOT NULL DEFAULT 'break_glass',
    reason        VARCHAR NOT NULL,
    granted_until TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_access_admin
    ON admin_access_log (admin_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_admin_access_annotation
    ON admin_access_log (annotation_id, created_at);
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_migration_0020.py -q`
Expected: 6 passed。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache tests/storage/test_migration_0020.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/migrations/0020_annotations_review.sql tests/storage/test_migration_0020.py
git commit -m "feat(storage): 0020 批注、闭环事件与管理员授权的表（M4-B §3）"
```

---

### Task 2: 冲突/不存在异常与 AlertEventRepo

**Files:**
- Modify: `src/sams/core/errors.py`
- Create: `src/sams/storage/repos/alert_events.py`
- Test: `tests/storage/test_alert_events_repo.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/storage/test_alert_events_repo.py`：

```python
"""`alert_events`：闭环动作的不可变历史。

这张表回答的是「谁在什么时候把它从什么改成了什么」。它不参与当前状态的读取——
`alerts` 上的列才是结论——所以这里的断言只看写入形状与读取次序。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.alert_events import ACTIONS, AlertEventRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def test_add_returns_the_row_and_stamps_the_clock(db, clock):
    repo = AlertEventRepo(db, clock=clock)
    event = repo.add(
        "a1", user_id="u1", action="ack", from_status="new", to_status="acknowledged"
    )
    assert event.ts == NOW
    assert event.note == ""
    assert repo.list_for("a1") == [event]


def test_unknown_action_is_refused(db):
    with pytest.raises(ValidationError, match="action"):
        AlertEventRepo(db).add(
            "a1", user_id="u1", action="look", from_status="new", to_status="new"
        )


def test_actions_are_read_in_time_order(db, clock):
    repo = AlertEventRepo(db, clock=clock)
    repo.add("a1", user_id="u1", action="ack", from_status="new", to_status="acknowledged")
    clock.advance(seconds=5)
    repo.add(
        "a1", user_id="u1", action="act", from_status="acknowledged", to_status="handled",
        note="挂了条件单",
    )
    rows = repo.list_for("a1")
    assert [r.action for r in rows] == ["ack", "act"]
    assert rows[1].note == "挂了条件单"


def test_last_can_narrow_to_the_terminal_actions(db, clock):
    repo = AlertEventRepo(db, clock=clock)
    repo.add("a1", user_id="u1", action="ack", from_status="new", to_status="acknowledged")
    repo.add(
        "a1", user_id="u1", action="dismiss", from_status="acknowledged", to_status="dismissed"
    )
    assert repo.last("a1").action == "dismiss"
    assert repo.last("a1", actions=("act", "dismiss")).action == "dismiss"
    assert repo.last("a1", actions=("act",)) is None
    assert repo.last("ghost") is None


def test_the_action_vocabulary_is_closed(db):
    assert ACTIONS == ("ack", "defer", "act", "dismiss", "undo")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alert_events_repo.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.storage.repos.alert_events`。

- [ ] **Step 3: 加异常类型**

修改 `src/sams/core/errors.py`，在 `StorageError` 之后追加：

```python
class NotFound(SamsError):
    """要的东西不存在，或者存在但不该让当前用户知道。

    两者用同一个异常：对屏幕外的探测者，「别人的私有批注」与「没有这条批注」
    必须长得一样，否则一条 404 与一条 403 就把「谁写过什么」泄露了一半。
    """


class Conflict(SamsError):
    """当前状态不允许这个动作：已处理的告警再处置一次、编辑时版本已被别人改过。

    Web 层把它映射成 409，CLI 映射成退出码 1。单独一个类型而不是 `ValidationError`：
    参数本身没敲错，错的是这一刻的库——重试同样的表单不会成功，要先刷新。
    """
```

- [ ] **Step 4: 写事件仓库**

创建 `src/sams/storage/repos/alert_events.py`：

```python
"""`alert_events`：闭环动作的不可变历史（M4-B spec §8.1）。

`alerts` 上的列是当前结论，这张表是「谁在什么时候把它从什么改成了什么」。
两个用途：undo 要读终态前那一条的 `from_status`；详情页要显示动作次序。
表不可改：没有 update/delete 方法——恢复状态是在 `AlertRepo` 里再写一行 undo。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

#: 与 spec §8.1 的状态图一一对应。多一个取值就是漏一个分支。
ACTIONS = ("ack", "defer", "act", "dismiss", "undo")
ACK, DEFER, ACT, DISMISS, UNDO = ACTIONS

_COLUMNS = "id, alert_id, user_id, action, from_status, to_status, note, ts"


@dataclass(frozen=True)
class AlertEvent:
    id: str
    alert_id: str
    user_id: str
    action: str
    from_status: str
    to_status: str
    note: str
    ts: datetime


class AlertEventRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def add(
        self,
        alert_id: str,
        *,
        user_id: str,
        action: str,
        from_status: str,
        to_status: str,
        note: str = "",
    ) -> AlertEvent:
        if action not in ACTIONS:
            raise ValidationError(f"未知动作 action={action!r}：只有 {list(ACTIONS)}")
        if not alert_id or not user_id:
            raise ValidationError("动作历史必须说清是哪条告警、谁按的")
        event_id = new_id()
        moment = self.clock.now_utc()
        text = (note or "").strip()
        self.db.execute(
            "INSERT INTO alert_events"
            " (id, alert_id, user_id, action, from_status, to_status, note, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [event_id, alert_id, user_id, action, from_status, to_status, text, moment],
        )
        return AlertEvent(
            id=event_id,
            alert_id=alert_id,
            user_id=user_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            note=text,
            ts=moment,
        )

    def list_for(self, alert_id: str) -> list[AlertEvent]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM alert_events WHERE alert_id = ? ORDER BY ts, id",
            [alert_id],
        )
        return [self._to_event(row) for row in rows.to_dicts()]

    def last(
        self, alert_id: str, *, actions: Sequence[str] | None = None
    ) -> AlertEvent | None:
        clauses = ["alert_id = ?"]
        params: list[object] = [alert_id]
        if actions:
            marks = ", ".join("?" for _ in actions)
            clauses.append(f"action IN ({marks})")
            params.extend(actions)
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM alert_events WHERE {' AND '.join(clauses)}"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            params,
        )
        return self._to_event(rows.to_dicts()[0]) if rows.height else None

    def _to_event(self, row: dict) -> AlertEvent:
        return AlertEvent(
            id=str(row["id"]),
            alert_id=str(row["alert_id"]),
            user_id=str(row["user_id"]),
            action=str(row["action"]),
            from_status=str(row["from_status"]),
            to_status=str(row["to_status"]),
            note=str(row["note"] or ""),
            ts=row["ts"],
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_alert_events_repo.py -q`
Expected: 5 passed。

- [ ] **Step 6: ruff**

Run: `uv run ruff check --no-cache src/sams/core/errors.py src/sams/storage/repos/alert_events.py tests/storage/test_alert_events_repo.py`
Expected: All checks passed。

- [ ] **Step 7: 提交**

```bash
git add src/sams/core/errors.py src/sams/storage/repos/alert_events.py tests/storage/test_alert_events_repo.py
git commit -m "feat(storage): alert_events 与 NotFound/Conflict 两个异常（M4-B §8.1）"
```

---

### Task 3: 告警状态扩为四值、新列上模型

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`
- Modify: `src/sams/web/display.py`
- Modify: `src/sams/cli/alert.py`
- Test: `tests/storage/test_alerts_repo.py`
- Test: `tests/cli/test_alert_cli.py`

- [ ] **Step 1: 写失败测试**

在 `tests/storage/test_alerts_repo.py` 顶部把时钟冻住（新增夹具并让 `repo` 用它）：

```python
from support import FrozenClock

from sams.storage.repos.alerts import (
    ACKNOWLEDGED,
    DISMISSED,
    HANDLED,
    NEW,
    STATUS,
    AlertRepo,
)


NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock):
    return AlertRepo(db, clock=clock)
```

在文件末尾追加：

```python
def test_status_vocabulary_grows_one_middle_state():
    """ack 与 acted 分开，才能回答「看到了但没做」与「根本没看到」（D124）。"""
    assert STATUS == ("new", "acknowledged", "handled", "dismissed")


def test_create_carries_the_thesis_snapshot(repo, seeded_user):
    """规则经观点展开时留下 `thesis_id`：推送上下文与详情跳转都要它，但它不是归属键。"""
    alert = repo.create(seeded_user, thesis_id="t1", **CREATE_KW)
    assert alert.thesis_id == "t1"


def test_list_for_accepts_the_new_status(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    assert [row.id for row in repo.list_for(seeded_user, status=ACKNOWLEDGED)] == [alert.id]
```

同时把文件里旧的 `"handled"`/`"dismissed"` 字面量改成导入的常量（可选，但提交前 ruff 不强制）。

在 `tests/cli/test_alert_cli.py` 末尾追加（复用该文件已有的 `container` 夹具与建告警助手；若没有建告警助手，直接用 `container.alerts_store.create` 照该文件现有形状写）：

```python
def test_acknowledged_is_spoken_in_chinese(container, capsys) -> None:
    """状态词表是封闭枚举：多一个状态而 CLI 没这个词，`alert show` 会 KeyError。"""
    from datetime import UTC, datetime

    from sams.storage.repos.alerts import ACKNOWLEDGED

    user = container.users.create("reader", "correct-horse-battery")
    alert = container.alerts_store.create(
        user.id,
        rule_id=None,
        rule_name="跌破",
        account_id=None,
        account_name="",
        market="A",
        code="601088",
        symbol_name="中国神华",
        severity="mid",
        stage="入池",
        value_text="10.1",
        threshold_text="10",
        action_note="",
        triggered_at=datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
    )
    container.alerts_store.acknowledge(alert.id, user_id=user.id)

    code = run_cli(container, ["alert", "show", "--user", "reader", alert.id])
    assert code == 0
    assert "已读" in capsys.readouterr().out
    assert ACKNOWLEDGED == "acknowledged"
```

> 若该测试文件没有 `run_cli` 这个助手，按文件里已有的调用方式（`main([...], container)` 或 `cli.main`）改写这一行；**不要**引入新的测试入口。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alerts_repo.py tests/cli/test_alert_cli.py -q`
Expected: FAIL —— `ImportError: cannot import name 'ACKNOWLEDGED'`。

- [ ] **Step 3: 扩状态与模型**

修改 `src/sams/storage/repos/alerts.py`：

```python
#: 与 0001/0020 的 `alerts.status` 一致：人对这条告警做了什么，不是发没发出去。
#: `acknowledged` 是「看过」，`handled` 是「做完了」，`dismissed` 是「决定不做」。
STATUS = ("new", "acknowledged", "handled", "dismissed")
NEW, ACKNOWLEDGED, HANDLED, DISMISSED = STATUS

#: 处置时必须选一个动作类型（D125）。`trade` 引用新成交，`thesis`/`rule` 引用本人的对象，
#: `watch` 无引用，`other` 要说明。
ACTION_KINDS = ("trade", "thesis", "rule", "watch", "other")
#: 忽略原因码（spec §8.5）。枚举而不是自由文本：复盘要按它统计。
DISMISS_CODES = ("misreport", "expected", "data_issue", "not_applicable", "duplicate", "other")

#: undo 只开 5 分钟：再长，审计表就会变成可以随意改写的编辑器（D138）。
UNDO_WINDOW = timedelta(minutes=5)

_ALERT_COLUMNS = (
    "id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
    " status, payload_json, severity, handled_at, handle_note, dismiss_reason, is_test,"
    " acknowledged_at, action_kind, action_ref, dismiss_code, thesis_id, deferred_until"
)
```

`Alert` 数据类追加（放在 `is_test` 之后）：

```python
    #: 0020 的闭环列。旧行全是 None：那不代表「没看过」，只代表「M4-B 之前没记过」
    acknowledged_at: datetime | None = None
    action_kind: str | None = None
    action_ref: str | None = None
    dismiss_code: str | None = None
    thesis_id: str | None = None
    deferred_until: datetime | None = None
```

`create` 签名加 `thesis_id: str | None = None`（放在 `is_test` 之后），INSERT 列与值同步：

```python
        self.db.execute(
            "INSERT INTO alerts"
            " (id, user_id, rule_id, account_id, market, code, stage, triggered_at,"
            " status, payload_json, severity, is_test, thesis_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
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
                thesis_id,
            ],
        )
```

`latest_open` 的 SQL 与文档改成：

```python
        """该 (用户, 市场, 代码) 上最新一条**未处理**告警——`no_open_alert` 的输入（§10.1）。

        `acknowledged` 也算未处理（M4-B §8.1）：已读不等于已处置，否则点一下「已读」
        会让同票其他规则重新开火。`deferred` 不改状态，因此也在这里。
        """
        rows = self.db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts"
            " WHERE user_id = ? AND market = ? AND code = ?"
            " AND status IN ('new', 'acknowledged')"
            " AND coalesce(is_test, FALSE) = FALSE"
            " ORDER BY triggered_at DESC, id DESC LIMIT 1",
            [user_id, market, code],
        )
```

`_to_alert` 的构造追加：

```python
            acknowledged_at=row["acknowledged_at"],
            action_kind=row["action_kind"],
            action_ref=row["action_ref"],
            dismiss_code=row["dismiss_code"],
            thesis_id=row["thesis_id"],
            deferred_until=row["deferred_until"],
```

`set_status` 顶部加一道闸（旧入口不许写 ack；ack 要走 `acknowledge`）：

```python
        #: 旧入口只管终态与「重开」。`acknowledged` 有专门的 `acknowledge`：
        #: 它不写 handled_at，而这一个函数的语义是「关上它」。
        legacy = (NEW, HANDLED, DISMISSED)
        if status not in legacy:
            raise ValidationError(
                f"set_status 不接受 {status!r}：已读请走 acknowledge()，"
                f"它不写 handled_at（一条已读的告警仍算未处理）"
            )
```

- [ ] **Step 4: 状态词表**

修改 `src/sams/cli/alert.py`：

```python
STATUS_HELP = "new/acknowledged/handled/dismissed（默认 new = 只看没人处理过的），或 all"
_STATUS_WORDS = {
    "new": "未处理",
    "acknowledged": "已读",
    "handled": "已处理",
    "dismissed": "已忽略",
}
```

修改 `src/sams/web/display.py`：

```python
ALERT_STATUS_LABELS = {
    NEW: "未处理",
    ACKNOWLEDGED: "已读",
    HANDLED: "已处理",
    DISMISSED: "已驳回",
}
```

（确认文件顶部已从 `sams.storage.repos.alerts` import 常量；没有就补 `ACKNOWLEDGED`。）

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_alerts_repo.py tests/cli/test_alert_cli.py tests/web/test_alerts_web.py -q`
Expected: 全绿。若 `test_alerts_repo.py` 里有「未知状态只认三个」一类断言，把期望值同步成四个值——那不是放宽，是状态机多了一个合法中间态。

- [ ] **Step 6: ruff**

Run: `uv run ruff check --no-cache src/sams/storage/repos/alerts.py src/sams/cli/alert.py src/sams/web/display.py tests/storage/test_alerts_repo.py tests/cli/test_alert_cli.py`
Expected: All checks passed。

- [ ] **Step 7: 提交**

```bash
git add src/sams/storage/repos/alerts.py src/sams/cli/alert.py src/sams/web/display.py tests/storage/test_alerts_repo.py tests/cli/test_alert_cli.py
git commit -m "feat(alerts): 状态扩为四值，模型带上闭环六列与 thesis 快照（M4-B §8.1）"
```

---

### Task 4: ack 幂等、积压口径

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`
- Modify: `src/sams/services/health.py`
- Test: `tests/storage/test_alerts_repo.py`
- Test: `tests/services/test_health.py`

- [ ] **Step 1: 写失败测试**

在 `tests/storage/test_alerts_repo.py` 末尾追加：

```python
def test_acknowledge_marks_it_read_without_closing_it(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.acknowledge(alert.id, user_id=seeded_user)
    assert moved.status == ACKNOWLEDGED
    assert moved.acknowledged_at == clock.moment
    assert moved.handled_at is None
    assert [e.action for e in repo.events.list_for(alert.id)] == ["ack"]


def test_acknowledge_is_idempotent(repo, seeded_user, clock):
    """手机连点两下不该留下两条「已读」事件。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    first = repo.acknowledge(alert.id, user_id=seeded_user)
    clock.advance(seconds=9)
    second = repo.acknowledge(alert.id, user_id=seeded_user)
    assert second.acknowledged_at == first.acknowledged_at
    assert len(repo.events.list_for(alert.id)) == 1


def test_acknowledge_refuses_a_closed_alert(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="已挂单")
    with pytest.raises(Conflict, match="已"):
        repo.acknowledge(alert.id, user_id=seeded_user)


def test_an_acknowledged_alert_still_blocks_a_second_fire(repo, seeded_user):
    """已读不等于已处置：同票第二条规则不该因为点了一下「已读」就重新开火。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    assert repo.latest_open(seeded_user, "A", "601088").id == alert.id
```

在 `tests/services/test_health.py` 里修改 `make_alerts`：把 `status != "new"` 的分支改成支持 `acknowledged`：

```python
        if status == "acknowledged":
            container.alerts_store.acknowledge(alert.id, user_id=user.id)
        elif status != "new":
            container.alerts_store.set_status(
                alert.id, status, user_id=user.id, note="测试"
            )
```

并把 `test_unhandled_counts_only_the_ones_nobody_has_looked_at` 改成：

```python
def test_unhandled_counts_read_but_unfinished_alerts(db, settings):
    """已读也算积压：点一下「已读」不说明这件事有人管了（M4-B §8.1）。"""
    container = make_container(db, settings)
    make_alerts(container, ["new", "acknowledged", "handled", "dismissed"])

    report = container.health.report()
    assert report.notify.unhandled == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alerts_repo.py tests/services/test_health.py -q`
Expected: FAIL —— `AttributeError: 'AlertRepo' object has no attribute 'acknowledge'`；健康页积压仍是 1。

- [ ] **Step 3: 实现 ack 与健康口径**

在 `AlertRepo.__init__` 里挂上事件仓库：

```python
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock
        #: 动作历史与当前状态同库同事务：ack/act/dismiss 各写一行，undo 靠它找上一态
        self.events = AlertEventRepo(db, clock=clock)
```

文件顶部加 `from sams.storage.repos.alert_events import ACT, AlertEventRepo`（`ACT` 在 Task 5 用；本任务只 import `AlertEventRepo` 以免 F401）。

在 `set_status` 前插入：

```python
    def acknowledge(self, alert_id: str, *, user_id: str) -> Alert:
        """「看到了」。重复调用是幂等的：手机连点两下不该多出一条历史。"""
        row = self._owned(alert_id, user_id)
        if row.status == ACKNOWLEDGED:
            return row
        if row.status != NEW:
            raise Conflict(
                f"告警 {alert_id} 已经是 {row.status}，不能再标已读：已读只是中间态，"
                "关上它请走处置或忽略"
            )
        moment = self.clock.now_utc()
        with self.db.transaction():
            self.db.execute(
                "UPDATE alerts SET status = ?, acknowledged_at = ? WHERE id = ?",
                [ACKNOWLEDGED, moment, alert_id],
            )
            self.events.add(
                alert_id,
                user_id=user_id,
                action="ack",
                from_status=NEW,
                to_status=ACKNOWLEDGED,
            )
        return self._reload(alert_id)

    def _owned(self, alert_id: str, user_id: str) -> Alert:
        row = self.get(alert_id)
        if row is None:
            raise StorageError(f"告警 {alert_id} 不存在（动作必须落在一条真实告警上）")
        if row.user_id != user_id:
            raise StorageError(
                f"告警 {alert_id} 不属于 {user_id}：那是发给另一个人的，"
                "代他按下去的每一个动作都会改写他的收件箱"
            )
        return row

    def _reload(self, alert_id: str) -> Alert:
        row = self.get(alert_id)
        assert row is not None  # 刚写完，同一条连接里一定读得回来
        return row
```

`src/sams/services/health.py` 那四处 `status = 'new'` 改成 `status IN ('new', 'acknowledged')`（用户数与最早、系统组件数与最早共四条），并把上方注释改成：

```python
        # 未处理 = new + acknowledged（M4-B §8.1）：点过「已读」但没人处置的告警
        # 仍然占着人的注意力，漏掉它会让健康页在最该响的时候显示 0。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_alerts_repo.py tests/services/test_health.py tests/test_adversarial_rules.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/storage/repos/alerts.py src/sams/services/health.py tests/storage/test_alerts_repo.py tests/services/test_health.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/alerts.py src/sams/services/health.py tests/storage/test_alerts_repo.py tests/services/test_health.py
git commit -m "feat(alerts): 已读幂等且仍算未处理，健康积压同口径（M4-B §8.1）"
```

---

### Task 5: 处置与忽略

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`
- Test: `tests/storage/test_alerts_repo.py`

- [ ] **Step 1: 写失败测试**

在 `tests/storage/test_alerts_repo.py` 末尾追加：

```python
def test_act_requires_an_action_kind(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="action_kind"):
        repo.act(alert.id, user_id=seeded_user, action_kind="done", note="看过了")


def test_act_with_trade_reference_closes_the_alert(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.act(
        alert.id, user_id=seeded_user, action_kind="trade", action_ref="o1", note="补记卖出"
    )
    assert moved.status == HANDLED
    assert moved.action_kind == "trade" and moved.action_ref == "o1"
    assert moved.handled_at == clock.moment
    assert moved.handle_note == "补记卖出"
    assert [e.action for e in repo.events.list_for(alert.id)] == ["act"]


def test_act_from_acknowledged_records_both_events(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    repo.act(alert.id, user_id=seeded_user, action_kind="watch",
             note="加自选等回踩")
    assert [e.action for e in repo.events.list_for(alert.id)] == ["ack", "act"]


def test_a_referencing_action_requires_its_reference(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    for kind in ("trade", "thesis", "rule"):
        with pytest.raises(ValidationError, match="引用"):
            repo.act(alert.id, user_id=seeded_user, action_kind=kind, note="x")


def test_other_needs_a_sentence(repo, seeded_user):
    """「已处置」如果只是一句话，复盘时仍然不知道做了什么（D125）。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="说明"):
        repo.act(alert.id, user_id=seeded_user, action_kind="other", note=" ")


def test_a_terminal_alert_refuses_a_second_action(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.act(alert.id, user_id=seeded_user, action_kind="other", note="已处理")
    with pytest.raises(Conflict):
        repo.act(alert.id, user_id=seeded_user, action_kind="other", note="再处理")


def test_dismiss_requires_a_code(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="dismiss_code"):
        repo.dismiss(alert.id, user_id=seeded_user, code="", reason="看过")


def test_misreport_and_other_need_a_reason(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    for code in ("misreport", "other"):
        with pytest.raises(ValidationError, match="说明"):
            repo.dismiss(alert.id, user_id=seeded_user, code=code, reason="")


def test_dismiss_records_the_code_and_reason(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    moved = repo.dismiss(
        alert.id, user_id=seeded_user, code="expected", reason="财报前的正常波动"
    )
    assert moved.status == DISMISSED
    assert moved.dismiss_code == "expected"
    assert moved.dismiss_reason == "财报前的正常波动"
    assert moved.handled_at is not None
    assert [e.action for e in repo.events.list_for(alert.id)] == ["dismiss"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alerts_repo.py -q`
Expected: FAIL —— `AttributeError: ... has no attribute 'act'`。

- [ ] **Step 3: 实现 act/dismiss**

在 `acknowledge` 之后、`_owned` 之前插入：

```python
    def act(
        self,
        alert_id: str,
        *,
        user_id: str,
        action_kind: str,
        action_ref: str | None = None,
        note: str = "",
    ) -> Alert:
        """处置：说清「做了什么」，并把它折算成一个可跳转的引用（D125）。"""
        if action_kind not in ACTION_KINDS:
            raise ValidationError(
                f"未知动作类型 action_kind={action_kind!r}：只有 {list(ACTION_KINDS)}"
            )
        ref = (action_ref or "").strip() or None
        if action_kind in ("trade", "thesis", "rule") and ref is None:
            raise ValidationError(
                f"{action_kind} 必须带动作引用：复盘要从这条告警跳到那笔成交/那条观点/那条规则"
            )
        text = (note or "").strip()
        if action_kind == "other" and len(text) < 2:
            raise ValidationError(
                "other 必须写说明（至少 2 个字）：一句「已处置」在复盘时仍然不知道做了什么"
            )
        row = self._owned(alert_id, user_id)
        if row.status not in (NEW, ACKNOWLEDGED):
            raise Conflict(
                f"告警 {alert_id} 已经是 {row.status}，终态不可回退；"
                "要改结论请在批注里说明，或 5 分钟内撤销"
            )
        moment = self.clock.now_utc()
        with self.db.transaction():
            self.db.execute(
                "UPDATE alerts SET status = ?, handled_at = ?, handle_note = ?,"
                " action_kind = ?, action_ref = ?, dismiss_code = NULL,"
                " dismiss_reason = NULL, deferred_until = NULL WHERE id = ?",
                [HANDLED, moment, text, action_kind, ref, alert_id],
            )
            self.events.add(
                alert_id,
                user_id=user_id,
                action="act",
                from_status=row.status,
                to_status=HANDLED,
                note=f"{action_kind}:{ref or ''} {text}".strip(),
            )
        return self._reload(alert_id)

    def dismiss(
        self, alert_id: str, *, user_id: str, code: str, reason: str = ""
    ) -> Alert:
        """忽略：原因码必选；误报与「其他」必须写说明（spec §8.5）。"""
        if code not in DISMISS_CODES:
            raise ValidationError(
                f"未知忽略原因 dismiss_code={code!r}：只有 {list(DISMISS_CODES)}"
            )
        text = (reason or "").strip()
        if code in ("misreport", "other") and len(text) < 2:
            raise ValidationError(
                f"{code} 必须写说明（至少 2 个字）：这一栏是复盘时唯一能解释「为什么关掉」的地方"
            )
        row = self._owned(alert_id, user_id)
        if row.status not in (NEW, ACKNOWLEDGED):
            raise Conflict(
                f"告警 {alert_id} 已经是 {row.status}，终态不可回退；"
                "要改结论请在批注里说明，或 5 分钟内撤销"
            )
        moment = self.clock.now_utc()
        with self.db.transaction():
            self.db.execute(
                "UPDATE alerts SET status = ?, handled_at = ?, dismiss_code = ?,"
                " dismiss_reason = ?, handle_note = NULL, action_kind = NULL,"
                " action_ref = NULL, deferred_until = NULL WHERE id = ?",
                [DISMISSED, moment, code, text, alert_id],
            )
            self.events.add(
                alert_id,
                user_id=user_id,
                action="dismiss",
                from_status=row.status,
                to_status=DISMISSED,
                note=f"{code} {text}".strip(),
            )
        return self._reload(alert_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_alerts_repo.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/storage/repos/alerts.py tests/storage/test_alerts_repo.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/alerts.py tests/storage/test_alerts_repo.py
git commit -m "feat(alerts): 处置必须带动作类型与引用，忽略必须带原因码（M4-B §8.2/§8.5）"
```

---

### Task 6: 稍后与撤销

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`
- Test: `tests/storage/test_alerts_repo.py`

- [ ] **Step 1: 写失败测试**

在 `tests/storage/test_alerts_repo.py` 末尾追加：

```python
def test_defer_keeps_the_status_and_sets_a_wakeup(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    until = clock.moment + timedelta(hours=1)
    moved = repo.defer(alert.id, user_id=seeded_user, until=until)
    assert moved.status == NEW
    assert moved.deferred_until == until
    event = repo.events.list_for(alert.id)[0]
    assert event.action == "defer" and event.from_status == event.to_status == NEW


def test_a_past_wakeup_is_refused(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="未来"):
        repo.defer(alert.id, user_id=seeded_user, until=clock.moment)


def test_a_naive_wakeup_is_refused(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(ValidationError, match="时区"):
        repo.defer(alert.id, user_id=seeded_user, until=datetime(2026, 10, 26, 1, 0))


def test_defer_refuses_a_terminal_alert(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.dismiss(alert.id, user_id=seeded_user, code="expected")
    with pytest.raises(Conflict):
        repo.defer(alert.id, user_id=seeded_user, until=clock.moment + timedelta(hours=1))


def test_undo_returns_to_the_previous_state_and_clears_the_conclusion(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.acknowledge(alert.id, user_id=seeded_user)
    repo.act(alert.id, user_id=seeded_user, action_kind="watch", note="等回踩")
    clock.advance(minutes=1)
    moved = repo.undo(alert.id, user_id=seeded_user)
    assert moved.status == ACKNOWLEDGED
    assert moved.handled_at is None
    assert moved.action_kind is None and moved.action_ref is None
    assert moved.deferred_until is None
    assert [e.action for e in repo.events.list_for(alert.id)] == ["ack", "act", "undo"]


def test_undo_after_five_minutes_is_refused(repo, seeded_user, clock):
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.dismiss(alert.id, user_id=seeded_user, code="expected")
    clock.advance(minutes=5, seconds=1)
    with pytest.raises(Conflict, match="5 分钟"):
        repo.undo(alert.id, user_id=seeded_user)


def test_undo_needs_a_recorded_transition(repo, seeded_user):
    """旧入口 set_status 不写事件：那条告警没有可信的上一态，不许猜。"""
    alert = repo.create(seeded_user, **CREATE_KW)
    repo.set_status(alert.id, HANDLED, user_id=seeded_user, note="旧入口")
    with pytest.raises(Conflict, match="动作记录"):
        repo.undo(alert.id, user_id=seeded_user)


def test_undo_refuses_an_open_alert(repo, seeded_user):
    alert = repo.create(seeded_user, **CREATE_KW)
    with pytest.raises(Conflict):
        repo.undo(alert.id, user_id=seeded_user)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_alerts_repo.py -q`
Expected: FAIL —— `AttributeError: ... 'defer'`。

- [ ] **Step 3: 实现 defer/undo**

在 `dismiss` 之后插入：

```python
    def defer(self, alert_id: str, *, user_id: str, until: datetime) -> Alert:
        """稍后提醒：状态不变，只加一个到点时间；不计忽略率（D138）。"""
        moment = _moment(until)
        row = self._owned(alert_id, user_id)
        if row.status not in (NEW, ACKNOWLEDGED):
            raise Conflict(
                f"告警 {alert_id} 已经是 {row.status}，不能再稍后："
                "要重新打开它，请先撤销或写一条批注"
            )
        if moment <= self.clock.now_utc():
            raise ValidationError(
                "稍后的时间必须在未来：一个已经过去的提醒等于没设，而屏幕上会显示成设过了"
            )
        with self.db.transaction():
            self.db.execute(
                "UPDATE alerts SET deferred_until = ? WHERE id = ?", [moment, alert_id]
            )
            self.events.add(
                alert_id,
                user_id=user_id,
                action="defer",
                from_status=row.status,
                to_status=row.status,
                note=moment.isoformat(),
            )
        return self._reload(alert_id)

    def undo(self, alert_id: str, *, user_id: str) -> Alert:
        """终态后 5 分钟内回到上一态；超过窗口只许写批注（D138）。"""
        row = self._owned(alert_id, user_id)
        if row.status not in (HANDLED, DISMISSED):
            raise Conflict(f"告警 {alert_id} 还是 {row.status}，没有可撤销的终态")
        terminal_at = row.handled_at
        if terminal_at is None:
            raise Conflict(f"告警 {alert_id} 没有终态时刻，无法判断是否还在 5 分钟内")
        if self.clock.now_utc() - terminal_at > UNDO_WINDOW:
            raise Conflict(
                f"告警 {alert_id} 关闭已超过 5 分钟：撤销窗口过了。"
                "要在复盘里更正，请在批注里写清楚——审计表不是可以随意改写的编辑器"
            )
        last = self.events.last(alert_id, actions=("act", "dismiss"))
        if last is None:
            raise Conflict(
                f"告警 {alert_id} 没有可撤销的动作记录（可能是旧版命令写下的）："
                "没有可信的上一态就不猜"
            )
        previous = last.from_status
        acknowledged_at = None if previous == NEW else row.acknowledged_at
        with self.db.transaction():
            self.db.execute(
                "UPDATE alerts SET status = ?, acknowledged_at = ?, handled_at = NULL,"
                " handle_note = NULL, action_kind = NULL, action_ref = NULL,"
                " dismiss_code = NULL, dismiss_reason = NULL, deferred_until = NULL"
                " WHERE id = ?",
                [previous, acknowledged_at, alert_id],
            )
            self.events.add(
                alert_id,
                user_id=user_id,
                action="undo",
                from_status=row.status,
                to_status=previous,
            )
        return self._reload(alert_id)
```

文件顶部 import 补 `ACT`（若 Task 4 没加）：

```python
from sams.storage.repos.alert_events import ACT, AlertEventRepo
```

`last(..., actions=("act", "dismiss"))` 里那两个字符串与 `alert_events` 的常量同义；任务 5 已定义 `DISMISS`，这里用字面量是为了让 `alert_events` 不反向依赖 `alerts`（那会成环）。若 ruff 没意见，保留。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_alerts_repo.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/storage/repos/alerts.py tests/storage/test_alerts_repo.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/alerts.py tests/storage/test_alerts_repo.py
git commit -m "feat(alerts): 稍后不改状态、五分钟内可撤销终态（M4-B §8.1）"
```

---

### Task 7: 批注仓库：创建、读取、可见性过滤

**Files:**
- Create: `src/sams/storage/repos/annotations.py`
- Test: `tests/storage/test_annotations_repo.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/storage/test_annotations_repo.py`：

```python
"""批注仓库：可见性过滤在 SQL 里，不在调用方那边（M4-B §4）。

「先查全量再让上层删掉别人的」这种接口，第一个忘了删的入口不会报错，只会把别人的
私有笔记显示在屏幕上。所以 `list_visible` 的第一个参数就是「谁在看」。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import NotFound, ValidationError
from sams.storage.repos.annotations import AnnotationRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
OWNER, OTHER = "u1", "u2"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock) -> AnnotationRepo:
    return AnnotationRepo(db, clock=clock)


def add(repo: AnnotationRepo, user: str = OWNER, **kw):
    values = {
        "target_type": "symbol",
        "target_ref": "A:600519",
        "target_label": "贵州茅台",
        "body_md": "渠道库存见底",
        "tags": ["逻辑"],
    }
    values.update(kw)
    return repo.create(user, **values)


def test_create_round_trips(repo):
    made = add(repo, pinned=True)
    assert made.tags == ["逻辑"]
    assert made.visibility == "private"
    assert repo.get(made.id) == made


def test_unknown_target_type_is_refused(repo):
    with pytest.raises(ValidationError, match="target_type"):
        add(repo, target_type="stock")


def test_an_empty_body_is_refused(repo):
    with pytest.raises(ValidationError, match="正文"):
        add(repo, body_md="   ")


def test_private_annotations_of_other_people_are_invisible(repo):
    mine = add(repo)
    theirs = add(repo, user=OTHER, body_md="别人的笔记")
    visible = repo.list_visible(OWNER)
    assert [row.id for row in visible] == [mine.id]
    assert theirs.id not in {row.id for row in visible}


def test_team_rows_are_only_visible_when_the_policy_is_on(repo):
    team = add(repo, user=OTHER, visibility="team", target_type="symbol", target_label="贵州茅台")
    assert repo.list_visible(OWNER, team_sharing=False) == []
    assert [row.id for row in repo.list_visible(OWNER, team_sharing=True)] == [team.id]


def test_team_rows_never_leak_from_private_targets(repo):
    """D121：team 只对 symbol 生效。一条 team 批注挂到别人的 thesis 上，
    等于用一条批注泄露「这个 thesis 存在」。"""
    add(repo, user=OTHER, visibility="team", target_type="thesis", target_ref="t1")
    assert repo.list_visible(OWNER, team_sharing=True) == []


def test_mine_only_beats_team_sharing(repo):
    add(repo)
    add(repo, user=OTHER, visibility="team")
    assert [row.user_id for row in repo.list_visible(OWNER, team_sharing=True, mine_only=True)] == [
        OWNER
    ]


def test_deleted_rows_are_hidden_even_from_their_author(repo):
    mine = add(repo)
    repo.soft_delete(mine.id, user_id=OWNER)
    assert repo.list_visible(OWNER) == []
    assert repo.get(mine.id) is None
    assert repo.get(mine.id, include_deleted=True).deleted_at == NOW


def test_target_filter_and_pinned_order(repo, clock):
    older = add(repo, body_md="第一条")
    clock.advance(minutes=1)
    pinned = add(repo, body_md="置顶", pinned=True)
    clock.advance(minutes=1)
    add(repo, body_md="别的票", target_ref="A:601088")
    rows = repo.list_visible(OWNER, target_type="symbol", target_ref="A:600519")
    assert [row.id for row in rows] == [pinned.id, older.id]


def test_search_is_a_literal_substring(repo):
    add(repo, body_md="毛利率 50% 以上")
    add(repo, body_md="渠道库存")
    assert len(repo.list_visible(OWNER, q="50%")) == 1
    assert len(repo.list_visible(OWNER, q="%")) == 1  # `%` 不是通配符


def test_tag_match_is_case_insensitive_and_exact(repo):
    add(repo, tags=["Risk", "逻辑"])
    add(repo, tags=["逻辑链"])
    assert len(repo.list_visible(OWNER, tag="risk")) == 1
    assert len(repo.list_visible(OWNER, tag="逻辑")) == 1  # 不是「逻辑链」


def test_pagination_keeps_the_order_stable(repo, clock):
    for index in range(3):
        add(repo, body_md=f"第 {index} 条")
        clock.advance(minutes=1)
    first = repo.list_visible(OWNER, limit=2)
    second = repo.list_visible(OWNER, limit=2, offset=2)
    assert [row.body_md for row in first] == ["第 2 条", "第 1 条"]
    assert [row.body_md for row in second] == ["第 0 条"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/storage/test_annotations_repo.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.storage.repos.annotations`。

- [ ] **Step 3: 写仓库**

创建 `src/sams/storage/repos/annotations.py`：

```python
"""批注仓库：五个挂载目标的读写、可见性过滤与编辑历史（M4-B §4）。

可见性过滤下沉到 SQL：先按 `visibility/user_id` 过滤，再按目标过滤。author 校验
同样在这里（写成 `user_id = ?`），服务层只负责回答「这个目标是不是你的」。
「查全量再让上层过滤」的接口形态一旦存在，第一个忘了过滤的入口不会报错，
只会把别人的私有笔记显示在屏幕上。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import Conflict, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

logger = logging.getLogger("sams.storage.annotations")

#: 五个挂载目标（spec §4.1）。`team` 只对 symbol 生效，判据写在读取 SQL 里。
TARGET_TYPES = ("symbol", "thesis", "rule", "alert", "order")
SYMBOL, THESIS, RULE, ALERT, ORDER = TARGET_TYPES
VISIBILITIES = ("private", "team")
PRIVATE, TEAM = VISIBILITIES
MAX_PINNED = 3

_ANNOTATION_COLUMNS = (
    "id, user_id, target_type, target_ref, target_label, body_md, tags_json, pinned,"
    " visibility, created_at, updated_at, deleted_at, deleted_by, purged_at"
)
_REVISION_COLUMNS = "id, annotation_id, user_id, before_md, after_md, meta_json, edited_at"

#: 标签精确匹配、大小写不敏感。`%`/`_` 在这里没有特殊含义——这正是「搜索不是 LIKE」。
_TAG_MATCH = (
    "list_contains(list_transform("
    "CAST(from_json(tags_json, '[\"VARCHAR\"]') AS VARCHAR[]), x -> lower(x)), lower(?))"
)


@dataclass(frozen=True)
class Annotation:
    id: str
    user_id: str
    target_type: str
    target_ref: str
    target_label: str
    body_md: str
    tags: list[str]
    pinned: bool
    visibility: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    purged_at: datetime | None = None


@dataclass(frozen=True)
class Revision:
    id: str
    annotation_id: str
    user_id: str
    before_md: str
    after_md: str
    meta: dict
    edited_at: datetime


def _require_user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


def _moment(value: datetime) -> datetime:
    """带时区才收（与 `repos/alerts.py` 同一条规则）。"""
    if value.tzinfo is None:
        raise ValidationError("时刻必须带时区（例如 2026-10-25T09:00+08:00）")
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    """库里读出来的值按 UTC 解释；裸值只可能来自旧行，不当成本地时间。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _json_list(raw: object) -> list[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
    except json.JSONDecodeError:
        logger.warning("批注的 tags_json 解不开：%r", raw)
        return []
    return [str(item) for item in parsed or []]


def _json_dict(raw: object) -> dict:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
    except json.JSONDecodeError:
        logger.warning("批注 revision 的 meta_json 解不开：%r", raw)
        return {}
    return dict(parsed or {})


class AnnotationRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    # --- 写 -----------------------------------------------------------------------------

    def create(
        self,
        user_id: str,
        *,
        target_type: str,
        target_ref: str,
        target_label: str,
        body_md: str,
        tags: Sequence[str] = (),
        pinned: bool = False,
        visibility: str = PRIVATE,
    ) -> Annotation:
        _require_user(user_id)
        if target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知批注目标 target_type={target_type!r}：只有 {list(TARGET_TYPES)}"
            )
        ref = (target_ref or "").strip()
        if not ref:
            raise ValidationError("target_ref 不能为空：一条批注必须说清它是关于什么的")
        if visibility not in VISIBILITIES:
            raise ValidationError(f"未知可见性 {visibility!r}：只有 {list(VISIBILITIES)}")
        body = body_md or ""
        if not body.strip():
            raise ValidationError("批注正文不能为空")
        moment = self.clock.now_utc()
        annotation_id = new_id()
        self.db.execute(
            "INSERT INTO annotations"
            " (id, user_id, target_type, target_ref, target_label, body_md, tags_json,"
            " pinned, visibility, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                annotation_id,
                user_id,
                target_type,
                ref,
                (target_label or "").strip(),
                body,
                json.dumps(list(tags), ensure_ascii=False),
                bool(pinned),
                visibility,
                moment,
                moment,
            ],
        )
        created = self.get(annotation_id)
        assert created is not None  # 刚写完，同一条连接里一定读得回来
        return created

    def update(
        self,
        annotation_id: str,
        *,
        user_id: str,
        body_md: str,
        tags: Sequence[str],
        visibility: str,
        expected_updated_at: datetime | None = None,
    ) -> Annotation:
        """编辑正文/标签/可见性，并在同一个事务里写一条 revision。

        乐观并发：表单带回来的 `updated_at` 与服务端不一致就 409，不静默覆盖——
        两个人同时开着编辑器时，后提交的那个人的整段改写会吞掉前一个人的理由。
        """
        row = self.get(annotation_id)
        if row is None:
            raise ValidationError(f"批注 {annotation_id} 不存在或已删除")
        if row.user_id != user_id:
            raise ValidationError(f"批注 {annotation_id} 不属于 {user_id}")
        if expected_updated_at is not None and _moment(expected_updated_at) != _utc(
            row.updated_at
        ):
            raise Conflict(
                f"批注 {annotation_id} 在你编辑期间被改过：刷新后再改，"
                "这里不合并、不覆盖"
            )
        if visibility not in VISIBILITIES:
            raise ValidationError(f"未知可见性 {visibility!r}：只有 {list(VISIBILITIES)}")
        body = body_md or ""
        if not body.strip():
            raise ValidationError("批注正文不能为空")
        moment = self.clock.now_utc()
        meta = {
            "before": {"tags": row.tags, "visibility": row.visibility},
            "after": {"tags": list(tags), "visibility": visibility},
        }
        with self.db.transaction():
            self.db.execute(
                "UPDATE annotations SET body_md = ?, tags_json = ?, visibility = ?,"
                " updated_at = ? WHERE id = ?",
                [body, json.dumps(list(tags), ensure_ascii=False), visibility, moment,
                 annotation_id],
            )
            self.db.execute(
                "INSERT INTO annotation_revisions"
                " (id, annotation_id, user_id, before_md, after_md, meta_json, edited_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    new_id(),
                    annotation_id,
                    user_id,
                    row.body_md,
                    body,
                    json.dumps(meta, ensure_ascii=False),
                    moment,
                ],
            )
        return self._reload(annotation_id)

    def set_pinned(self, annotation_id: str, *, user_id: str, pinned: bool) -> Annotation:
        """置顶不是编辑：不写 revision，也不动 `updated_at`（开着编辑器的人不该因此 409）。"""
        row = self.get(annotation_id)
        if row is None:
            raise ValidationError(f"批注 {annotation_id} 不存在或已删除")
        if row.user_id != user_id:
            raise ValidationError(f"批注 {annotation_id} 不属于 {user_id}")
        if bool(row.pinned) == bool(pinned):
            return row
        if pinned:
            count = int(
                self.db.scalar(
                    "SELECT count(*) FROM annotations"
                    " WHERE target_type = ? AND target_ref = ? AND pinned"
                    " AND deleted_at IS NULL AND id <> ?",
                    [row.target_type, row.target_ref, annotation_id],
                )
                or 0
            )
            if count >= MAX_PINNED:
                raise ValidationError(
                    f"同一个目标最多置顶 {MAX_PINNED} 条：再多，置顶就不再是「先看这个」"
                )
        self.db.execute(
            "UPDATE annotations SET pinned = ? WHERE id = ?", [bool(pinned), annotation_id]
        )
        return self._reload(annotation_id)

    def soft_delete(self, annotation_id: str, *, user_id: str) -> None:
        """软删除：列表与目标页立即隐藏；正文保留 30 天，prune 再硬清除（D136）。"""
        row = self.get(annotation_id)
        if row is None:
            raise ValidationError(f"批注 {annotation_id} 不存在或已删除")
        if row.user_id != user_id:
            raise ValidationError(f"批注 {annotation_id} 不属于 {user_id}")
        self.db.execute(
            "UPDATE annotations SET deleted_at = ?, deleted_by = ?, updated_at = ?"
            " WHERE id = ?",
            [self.clock.now_utc(), user_id, self.clock.now_utc(), annotation_id],
        )

    # --- 读 -----------------------------------------------------------------------------

    def get(self, annotation_id: str, *, include_deleted: bool = False) -> Annotation | None:
        sql = f"SELECT {_ANNOTATION_COLUMNS} FROM annotations WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL AND purged_at IS NULL"
        rows = self.db.query(sql, [annotation_id])
        return self._to_annotation(rows.to_dicts()[0]) if rows.height else None

    def list_visible(
        self,
        viewer_id: str,
        *,
        team_sharing: bool = False,
        mine_only: bool = False,
        target_type: str | None = None,
        target_ref: str | None = None,
        q: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Annotation]:
        _require_user(viewer_id)
        if target_type is not None and target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知批注目标 target_type={target_type!r}：只有 {list(TARGET_TYPES)}"
            )
        if limit <= 0:
            raise ValidationError("limit 必须为正数")
        clauses = ["deleted_at IS NULL", "purged_at IS NULL"]
        params: list[object] = []
        if mine_only or not team_sharing:
            clauses.append("user_id = ?")
            params.append(viewer_id)
        else:
            # team 只对 symbol 生效：别的目标上一条 team 批注等于泄露「这个对象存在」
            clauses.append(
                "(user_id = ? OR (visibility = 'team' AND target_type = 'symbol'))"
            )
            params.append(viewer_id)
        if target_type is not None:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_ref is not None:
            clauses.append("target_ref = ?")
            params.append(target_ref)
        if q:
            clauses.append("contains(lower(body_md), lower(?))")
            params.append(q)
        if tag:
            clauses.append(_TAG_MATCH)
            params.append(tag)
        sql = (
            f"SELECT {_ANNOTATION_COLUMNS} FROM annotations"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY pinned DESC, created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params += [int(limit), int(offset)]
        return [self._to_annotation(row) for row in self.db.query(sql, params).to_dicts()]

    def revisions(self, annotation_id: str) -> list[Revision]:
        rows = self.db.query(
            f"SELECT {_REVISION_COLUMNS} FROM annotation_revisions"
            " WHERE annotation_id = ? ORDER BY edited_at, id",
            [annotation_id],
        )
        return [
            Revision(
                id=str(row["id"]),
                annotation_id=str(row["annotation_id"]),
                user_id=str(row["user_id"]),
                before_md=str(row["before_md"] or ""),
                after_md=str(row["after_md"] or ""),
                meta=_json_dict(row["meta_json"]),
                edited_at=row["edited_at"],
            )
            for row in rows.to_dicts()
        ]

    def _reload(self, annotation_id: str) -> Annotation:
        row = self.get(annotation_id)
        assert row is not None  # 刚写完，同一条连接里一定读得回来
        return row

    def _to_annotation(self, row: dict) -> Annotation:
        return Annotation(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            target_type=str(row["target_type"]),
            target_ref=str(row["target_ref"]),
            target_label=str(row["target_label"] or ""),
            body_md=str(row["body_md"] or ""),
            tags=_json_list(row["tags_json"]),
            pinned=bool(row["pinned"]),
            visibility=str(row["visibility"] or PRIVATE),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
            deleted_by=row["deleted_by"],
            purged_at=row["purged_at"],
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_annotations_repo.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/storage/repos/annotations.py tests/storage/test_annotations_repo.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/annotations.py tests/storage/test_annotations_repo.py
git commit -m "feat(storage): 批注仓库，可见性过滤下沉到 SQL（M4-B §4）"
```

---

### Task 8: 编辑历史、置顶上限与软删的仓库契约

**Files:**
- Test: `tests/storage/test_annotations_repo.py`

- [ ] **Step 1: 写失败测试**

在 `tests/storage/test_annotations_repo.py` 末尾追加：

```python
def test_update_writes_a_revision_with_the_before_and_after_tags(repo, clock):
    made = add(repo)
    clock.advance(seconds=1)
    edited = repo.update(
        made.id,
        user_id=OWNER,
        body_md="渠道库存见底，等回款",
        tags=["逻辑", "风险"],
        visibility="private",
        expected_updated_at=made.updated_at,
    )
    assert edited.body_md.endswith("等回款")
    assert edited.tags == ["逻辑", "风险"]
    revisions = repo.revisions(made.id)
    assert len(revisions) == 1
    assert revisions[0].before_md == "渠道库存见底"
    assert revisions[0].after_md == "渠道库存见底，等回款"
    assert revisions[0].meta["before"]["tags"] == ["逻辑"]
    assert revisions[0].meta["after"]["tags"] == ["逻辑", "风险"]


def test_a_stale_updated_at_is_a_conflict(repo, clock):
    made = add(repo)
    clock.advance(seconds=1)
    repo.update(
        made.id,
        user_id=OWNER,
        body_md="第一改",
        tags=[],
        visibility="private",
        expected_updated_at=made.updated_at,
    )
    with pytest.raises(Conflict, match="改过"):
        repo.update(
            made.id,
            user_id=OWNER,
            body_md="第二改",
            tags=[],
            visibility="private",
            expected_updated_at=made.updated_at,
        )


def test_only_the_author_can_edit_or_delete(repo):
    made = add(repo)
    with pytest.raises(ValidationError, match="不属于"):
        repo.update(
            made.id, user_id=OTHER, body_md="改别人的", tags=[], visibility="private"
        )
    with pytest.raises(ValidationError, match="不属于"):
        repo.soft_delete(made.id, user_id=OTHER)


def test_pinning_is_capped_at_three_per_target(repo):
    for index in range(3):
        add(repo, body_md=f"置顶 {index}", pinned=True)
    extra = add(repo, body_md="第四条")
    with pytest.raises(ValidationError, match="最多置顶"):
        repo.set_pinned(extra.id, user_id=OWNER, pinned=True)


def test_pinning_does_not_move_the_edit_clock(repo):
    """置顶不是编辑：一个开着编辑器的窗口不该因为别人置顶而 409。"""
    made = add(repo)
    pinned = repo.set_pinned(made.id, user_id=OWNER, pinned=True)
    assert pinned.updated_at == made.updated_at


def test_editing_a_deleted_annotation_is_a_validation_error(repo):
    made = add(repo)
    repo.soft_delete(made.id, user_id=OWNER)
    with pytest.raises(ValidationError, match="不存在或已删除"):
        repo.update(made.id, user_id=OWNER, body_md="x", tags=[], visibility="private")


def test_soft_delete_keeps_a_tombstone_for_later_purge(repo):
    made = add(repo)
    repo.soft_delete(made.id, user_id=OWNER)
    tombstone = repo.get(made.id, include_deleted=True)
    assert tombstone.deleted_by == OWNER
    assert tombstone.purged_at is None
```

- [ ] **Step 2: 跑测试确认失败或通过**

Run: `uv run pytest tests/storage/test_annotations_repo.py -q`
Expected: 若 Task 7 的实现已经覆盖这些行为则直接 PASS；任何一条 FAIL 都说明 Task 7 的实现不完整，**改实现而不是改断言**。最可能缺的是置顶不动 `updated_at` 与 revision 的 meta 形状。

- [ ] **Step 3: 修正实现（仅在失败时）**

按 Task 7 的代码核对；这一任务的价值是把「编辑=正文+标签+可见性+一条 revision」与「置顶不算编辑」钉成契约。不要为通过测试新增接口。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/storage/test_annotations_repo.py -q`
Expected: 全绿。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache tests/storage/test_annotations_repo.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add tests/storage/test_annotations_repo.py src/sams/storage/repos/annotations.py
git commit -m "test(storage): 钉住批注编辑历史、置顶上限与软删墓碑（M4-B §4.3）"
```

---

### Task 9: 批注服务：五类目标授权与标签规范化

**Files:**
- Create: `src/sams/services/annotations.py`
- Test: `tests/services/test_annotations_service.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/services/test_annotations_service.py`：

```python
"""批注用例：目标是不是你的、标签怎么归一、正文多长（M4-B §4）。

仓库只认「这一行的 user_id 是不是你」；「这个 thesis/rule/alert/order 是不是你的」
要问各自的仓库，所以这道闸在这里。目标不存在与不属于你统一 404（NotFound）：
两种回答都告诉探测者同一件事——这里没有你能用的东西。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from support import FrozenClock

from sams.core.errors import Conflict, NotFound, ValidationError
from sams.services.annotations import AnnotationService, normalize_tags
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
USER, OTHER = "trader", "keeper"
PWD = "correct-horse-battery"


@pytest.fixture
def world(db, settings):
    clock = FrozenClock(NOW)
    users = UserRepo(db)
    user = users.create(USER, PWD)
    other = users.create(OTHER, PWD)
    symbols = SymbolRepo(db)
    theses = ThesisRepo(db)
    rules = RuleRepo(db, clock=clock)
    alerts = AlertRepo(db, clock=clock)
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=symbols)
    service = AnnotationService(
        AnnotationRepo(db, clock=clock),
        users,
        symbols,
        theses,
        rules,
        alerts,
        accounts,
        orders,
        settings,
    )
    return SimpleNamespace(
        clock=clock,
        users=users,
        user=user,
        other=other,
        theses=theses,
        rules=rules,
        alerts=alerts,
        accounts=accounts,
        orders=orders,
        service=service,
    )


def make_thesis(world, user_id: str | None = None):
    return world.theses.create(
        user_id=user_id or world.user.id,
        market="A",
        code="601088",
        style="value",
        actor_id=user_id or world.user.id,
        now=NOW,
    )


def make_rule(world, user_id: str | None = None):
    return world.rules.create(
        user_id or world.user.id,
        kind="price.cross_below",
        name="跌破 10 元",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
        account_id=None,
    )


def make_alert(world, user_id: str | None = None):
    return world.alerts.create(
        user_id or world.user.id,
        rule_id=None,
        rule_name="跌破 10 元",
        account_id=None,
        account_name="",
        market="A",
        code="601088",
        symbol_name="中国神华",
        severity="mid",
        stage="入池",
        value_text="10.1",
        threshold_text="10",
        action_note="",
        triggered_at=NOW,
    )


def make_order(world, user_id: str | None = None):
    owner = user_id or world.user.id
    account = world.accounts.open(owner, "主账户", report_currency="CNY")
    world.accounts.adjust_cash(account.id, "CNY", 100_000.0, reason="初始", actor_id=owner)
    return world.orders.record_fill(
        account_id=account.id,
        user_id=owner,
        market="A",
        code="601088",
        side="buy",
        qty=100,
        price=30.0,
        currency="CNY",
    )


def test_symbol_target_needs_no_master_row(world):
    """symbol 是全实例共享的研究对象：标的还没同步进 `symbols`，也可以先写研究笔记。"""
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A/600519", body_md="先看图"
    )
    assert made.target_ref == "A:600519"
    assert made.target_label == "600519"


def test_symbol_ref_accepts_the_slash_form(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="a/600519", body_md="先看图"
    )
    assert made.target_ref == "A:600519"


def test_a_bad_symbol_ref_is_a_validation_error(world):
    with pytest.raises(ValidationError, match="市场:代码"):
        world.service.create(
            world.user.id, target_type="symbol", target_ref="600519", body_md="x"
        )


def test_your_own_targets_are_accepted(world):
    thesis = make_thesis(world)
    rule = make_rule(world)
    alert = make_alert(world)
    order = make_order(world)
    for target_type, target_ref in (
        ("thesis", thesis.id),
        ("rule", rule.id),
        ("alert", alert.id),
        ("order", order.id),
    ):
        made = world.service.create(
            world.user.id, target_type=target_type, target_ref=target_ref, body_md="备注"
        )
        assert made.target_type == target_type
        assert made.target_label


def test_someone_elses_targets_are_not_found(world):
    thesis = make_thesis(world, world.other.id)
    rule = make_rule(world, world.other.id)
    alert = make_alert(world, world.other.id)
    order = make_order(world, world.other.id)
    for target_type, target_ref in (
        ("thesis", thesis.id),
        ("rule", rule.id),
        ("alert", alert.id),
        ("order", order.id),
    ):
        with pytest.raises(NotFound):
            world.service.create(
                world.user.id, target_type=target_type, target_ref=target_ref, body_md="x"
            )


def test_a_missing_target_is_the_same_not_found(world):
    with pytest.raises(NotFound):
        world.service.create(
            world.user.id, target_type="thesis", target_ref="ghost", body_md="x"
        )


def test_an_unknown_target_type_is_refused(world):
    with pytest.raises(ValidationError, match="target_type"):
        world.service.create(
            world.user.id, target_type="stock", target_ref="A:600519", body_md="x"
        )


def test_the_body_is_bounded(world, settings):
    with pytest.raises(ValidationError, match="正文"):
        world.service.create(
            world.user.id, target_type="symbol", target_ref="A:600519", body_md=" "
        )
    too_long = "x" * (settings.annotations.max_body_chars + 1)
    with pytest.raises(ValidationError, match="最多"):
        world.service.create(
            world.user.id, target_type="symbol", target_ref="A:600519", body_md=too_long
        )


def test_tags_are_normalized_and_deduped_case_insensitively():
    assert normalize_tags("#逻辑,  risk ,  RISK,逻辑") == ["逻辑", "risk"]


def test_too_many_or_too_long_tags_are_refused():
    with pytest.raises(ValidationError, match="10"):
        normalize_tags(",".join(f"t{index}" for index in range(11)))
    with pytest.raises(ValidationError, match="20"):
        normalize_tags("x" * 21)


def test_list_target_hides_other_peoples_private_notes(world):
    mine = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="我的"
    )
    world.service.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    rows = world.service.list_target(world.user.id, "symbol", "A:600519")
    assert [row.id for row in rows] == [mine.id]


def test_get_refuses_someone_elses_private_note(world):
    made = world.service.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    with pytest.raises(NotFound):
        world.service.get(world.user.id, made.id)
```

> 注：`normalize_tags` 的签名是 `normalize_tags(raw)`，既能吃字符串也能吃序列；不要给它加默认参数之外的开关。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/services/test_annotations_service.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.services.annotations`。

- [ ] **Step 3: 写服务**

创建 `src/sams/services/annotations.py`：

```python
"""批注用例（M4-B §4）：目标归属、标签、正文上限与读取可见性。

B1 只开私有批注：`team` 的两列已经在 schema 里，但写入口不给它开口子——UI 没开的
开关比一个能写进去却没人看得见的值诚实。B2 接入策略后由同一个服务放宽写入口。

目标归属在这里问各自的仓库（thesis/rule/alert/order），因为仓库只认「这一行是不是
你的」。目标不存在与不属于当前用户统一 `NotFound`：两种回答都告诉探测者同一件事。
"""

from __future__ import annotations

from collections.abc import Sequence

from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import NotFound, ValidationError
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import (
    ALERT,
    ORDER,
    PRIVATE,
    RULE,
    SYMBOL,
    TARGET_TYPES,
    THESIS,
    Annotation,
    AnnotationRepo,
)
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

MAX_TAGS = 10
MAX_TAG_CHARS = 20


def normalize_tags(raw: str | Sequence[str] | None) -> list[str]:
    """逗号分隔或序列 → 去空白、去前导 `#`、大小写不敏感去重（保留第一次的写法）。"""
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else [str(item) for item in raw]
    seen: set[str] = set()
    tags: list[str] = []
    for part in parts:
        text = part.strip()
        if text.startswith("#"):
            text = text[1:].strip()
        text = " ".join(text.split())
        if not text:
            continue
        if len(text) > MAX_TAG_CHARS:
            raise ValidationError(f"单个标签最多 {MAX_TAG_CHARS} 字：{text!r}")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(text)
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"一条批注最多 {MAX_TAGS} 个标签（收到 {len(tags)} 个）")
    return tags


class AnnotationService:
    def __init__(
        self,
        annotations: AnnotationRepo,
        users: UserRepo,
        symbols: SymbolRepo,
        theses: ThesisRepo,
        rules: RuleRepo,
        alerts: AlertRepo,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        settings: Settings,
    ) -> None:
        self.annotations = annotations
        self.users = users
        self.symbols = symbols
        self.theses = theses
        self.rules = rules
        self.alerts = alerts
        self.accounts = accounts
        self.orders = orders
        self.settings = settings

    # --- 写 -----------------------------------------------------------------------------

    def create(
        self,
        user_id: str,
        *,
        target_type: str,
        target_ref: str,
        body_md: str,
        tags: str | Sequence[str] | None = (),
    ) -> Annotation:
        if self.users.get(user_id) is None:
            raise ValidationError(f"用户 {user_id} 不存在：批注必须有一个作者")
        ref = self._normalize_ref(target_type, target_ref)
        label = self._target_label(user_id, target_type, ref)
        return self.annotations.create(
            user_id,
            target_type=target_type,
            target_ref=ref,
            target_label=label,
            body_md=self._body(body_md),
            tags=normalize_tags(tags),
            visibility=PRIVATE,
        )

    def edit(
        self,
        user_id: str,
        annotation_id: str,
        *,
        body_md: str,
        tags: str | Sequence[str] | None = (),
        expected_updated_at=None,
    ) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        return self.annotations.update(
            annotation_id,
            user_id=user_id,
            body_md=self._body(body_md),
            tags=normalize_tags(tags),
            visibility=made.visibility,
            expected_updated_at=expected_updated_at,
        )

    def pin(self, user_id: str, annotation_id: str, *, pinned: bool) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        return self.annotations.set_pinned(annotation_id, user_id=user_id, pinned=pinned)

    def delete(self, user_id: str, annotation_id: str) -> None:
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        self.annotations.soft_delete(annotation_id, user_id=user_id)

    # --- 读 -----------------------------------------------------------------------------

    def get(
        self, viewer_id: str, annotation_id: str, *, team_sharing: bool = False
    ) -> Annotation:
        made = self.annotations.get(annotation_id)
        if made is None or not self._readable(made, viewer_id, team_sharing):
            raise NotFound(f"批注 {annotation_id} 不存在或不可见")
        return made

    def list_target(
        self,
        viewer_id: str,
        target_type: str,
        target_ref: str,
        *,
        team_sharing: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Annotation]:
        ref = self._normalize_ref(target_type, target_ref)
        return self.annotations.list_visible(
            viewer_id,
            team_sharing=team_sharing,
            target_type=target_type,
            target_ref=ref,
            limit=limit,
            offset=offset,
        )

    def list_mine(self, viewer_id: str, *, limit: int = 20, offset: int = 0) -> list[Annotation]:
        return self.annotations.list_visible(
            viewer_id, mine_only=True, limit=limit, offset=offset
        )

    def revisions(self, user_id: str, annotation_id: str):
        made = self.annotations.get(annotation_id)
        if made is None or made.user_id != user_id:
            raise NotFound(f"批注 {annotation_id} 不存在或不属于当前用户")
        return self.annotations.revisions(annotation_id)

    # --- 内部 ---------------------------------------------------------------------------

    def _readable(self, made: Annotation, viewer_id: str, team_sharing: bool) -> bool:
        if made.user_id == viewer_id:
            return True
        # D121：team 只对 symbol 生效；别的目标上的 team 行一律按私有处理
        return bool(
            team_sharing
            and made.visibility == "team"
            and made.target_type == SYMBOL
        )

    def _body(self, body_md: str) -> str:
        text = (body_md or "").strip()
        if not text:
            raise ValidationError("批注正文不能为空")
        limit = self.settings.annotations.max_body_chars
        if len(text) > limit:
            raise ValidationError(f"批注正文最多 {limit} 字（收到 {len(text)} 字）")
        return text

    def _normalize_ref(self, target_type: str, target_ref: str) -> str:
        if target_type not in TARGET_TYPES:
            raise ValidationError(
                f"未知批注目标 target_type={target_type!r}：只有 {list(TARGET_TYPES)}"
            )
        ref = (target_ref or "").strip()
        if not ref:
            raise ValidationError("target_ref 不能为空：一条批注必须说清它是关于什么的")
        if target_type != SYMBOL:
            return ref
        market, _, code = ref.replace("/", ":").partition(":")
        try:
            market = canonical_market(market)
            code = canonical_code(market, code)
        except ValidationError as exc:
            raise ValidationError(f"标的键要写成 市场:代码（例如 A:600519）：{exc}") from exc
        return f"{market}:{code}"

    def _target_label(self, user_id: str, target_type: str, ref: str) -> str:
        if target_type == SYMBOL:
            market, _, code = ref.partition(":")
            row = self.symbols.get(market, code)
            return str((row or {}).get("name") or code)
        if target_type == THESIS:
            made = self.theses.get(user_id, ref)
            if made is None:
                raise NotFound("观点不存在或不属于当前用户")
            return f"{made.market}:{made.code} 观点"
        if target_type == RULE:
            made = self.rules.get(ref, user_id=user_id)
            if made is None:
                raise NotFound("规则不存在或不属于当前用户")
            return made.name
        if target_type == ALERT:
            made = self.alerts.get(ref, user_id=user_id)
            if made is None:
                raise NotFound("告警不存在或不属于当前用户")
            return made.rule_name or made.symbol_name or f"{made.market}:{made.code} 告警"
        if target_type == ORDER:
            for account in self.accounts.list_for(user_id):
                order = self.orders.order(account.id, ref)
                if order is not None:
                    side = "买入" if order.side == "buy" else "卖出"
                    return f"{side} {order.market}:{order.code} {order.qty:g}@{order.price:g}"
            raise NotFound("成交不存在或不属于当前用户")
        raise ValidationError(f"未知批注目标 target_type={target_type!r}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/services/test_annotations_service.py -q`
Expected: 全绿。若 `rules.create` 的参数形状与测试不符，按 `tests/storage/test_rules_repo.py` 的 `add()` 夹具对齐参数，**不要**放宽服务。

- [ ] **Step 5: ruff**

Run: `uv run ruff check --no-cache src/sams/services/annotations.py tests/services/test_annotations_service.py`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/services/annotations.py tests/services/test_annotations_service.py
git commit -m "feat(annotations): 五类目标归属校验、标签归一与正文上限（M4-B §4）"
```

---

### Task 10: 编辑/置顶/软删的服务契约

**Files:**
- Test: `tests/services/test_annotations_service.py`

- [ ] **Step 1: 写失败测试**

在 `tests/services/test_annotations_service.py` 末尾追加：

```python
def test_edit_records_history_and_bumps_updated_at(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="第一版"
    )
    world.clock.advance(seconds=2)
    edited = world.service.edit(
        world.user.id, made.id, body_md="第二版", tags="#逻辑", expected_updated_at=made.updated_at
    )
    assert edited.body_md == "第二版"
    assert edited.updated_at != made.updated_at
    history = world.service.revisions(world.user.id, made.id)
    assert [(row.before_md, row.after_md) for row in history] == [("第一版", "第二版")]


def test_a_stale_edit_is_a_conflict(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="第一版"
    )
    world.clock.advance(seconds=2)
    world.service.edit(
        world.user.id, made.id, body_md="第二版", expected_updated_at=made.updated_at
    )
    with pytest.raises(Conflict):
        world.service.edit(
            world.user.id, made.id, body_md="第三版", expected_updated_at=made.updated_at
        )


def test_only_the_author_can_edit_pin_or_delete(world):
    made = world.service.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    for action in (
        lambda: world.service.edit(world.user.id, made.id, body_md="x"),
        lambda: world.service.pin(world.user.id, made.id, pinned=True),
        lambda: world.service.delete(world.user.id, made.id),
        lambda: world.service.revisions(world.user.id, made.id),
    ):
        with pytest.raises(NotFound):
            action()


def test_delete_hides_the_annotation_from_every_read_path(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="要删"
    )
    world.service.delete(world.user.id, made.id)
    assert world.service.list_target(world.user.id, "symbol", "A:600519") == []
    with pytest.raises(NotFound):
        world.service.get(world.user.id, made.id)
    with pytest.raises(NotFound):
        world.service.edit(world.user.id, made.id, body_md="复活")


def test_pin_is_idempotent(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="置顶"
    )
    assert world.service.pin(world.user.id, made.id, pinned=True).pinned is True
    assert world.service.pin(world.user.id, made.id, pinned=True).pinned is True
```

- [ ] **Step 2: 跑测试确认结果**

Run: `uv run pytest tests/services/test_annotations_service.py -q`
Expected: PASS（Task 9 已实现这些行为）。任何 FAIL 都改实现，不改断言。

- [ ] **Step 3: ruff**

Run: `uv run ruff check --no-cache tests/services/test_annotations_service.py`
Expected: All checks passed。

- [ ] **Step 4: 提交**

```bash
git add tests/services/test_annotations_service.py src/sams/services/annotations.py
git commit -m "test(annotations): 钉住编辑历史、并发冲突与软删后的读路径（M4-B §4.3）"
```

---

### Task 11: Markdown 渲染与推送纯文本

**Files:**
- Create: `src/sams/services/markdown.py`
- Test: `tests/services/test_markdown.py`
- Modify: `pyproject.toml`、`uv.lock`（`uv add markdown-it-py`）

- [ ] **Step 1: 加依赖**

Run: `uv add markdown-it-py`
Expected: `pyproject.toml` 的 `dependencies` 出现 `markdown-it-py`，`uv.lock` 更新。

Run: `uv run python -c "from markdown_it import MarkdownIt; print(MarkdownIt('commonmark').render('**ok**').strip())"`
Expected: `<p><strong>ok</strong></p>`

- [ ] **Step 2: 写失败测试**

创建 `tests/services/test_markdown.py`：

```python
"""批注的 Markdown 是用户输入：渲染必须先去能力，而不是先渲染再清洗（D122）。

这里钉的是「执行面为零」：原始 HTML、图片语法、危险协议都不能产生可点击/可执行的
元素；表格与代码块要能用；推送给手机的那一句是同一份源码压出来的纯文本。
"""

from __future__ import annotations

from sams.services.markdown import plain_text, render_markdown


def test_raw_html_is_escaped():
    html = render_markdown("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_event_handler_in_html_is_escaped():
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "onerror" in html  # 作为被转义的文本出现，而不是属性
    assert "&lt;img" in html


def test_a_javascript_link_is_plain_text():
    html = render_markdown("[点我](javascript:alert(1))")
    assert "javascript:" not in html
    assert "href" not in html
    assert "点我" in html


def test_a_data_link_is_plain_text():
    html = render_markdown("[证据](data:text/html;base64,PHNjcmlwdD4=)")
    assert "data:" not in html
    assert "href" not in html


def test_a_safe_link_opens_in_a_new_tab_without_a_referrer():
    html = render_markdown("[公告](https://example.com/a)")
    assert 'href="https://example.com/a"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_mailto_is_allowed():
    html = render_markdown("[写信](mailto:a@example.com)")
    assert 'href="mailto:a@example.com"' in html


def test_markdown_images_are_disabled():
    """截图走附件：Markdown 图片会让一张外链图在别人的浏览器里发请求。"""
    html = render_markdown("![日线](https://example.com/x.png)")
    assert "<img" not in html


def test_tables_and_code_blocks_render():
    html = render_markdown("| 项 | 值 |\n| - | - |\n| PE | 8 |\n\n```\nx = 1\n```")
    assert "<table>" in html
    assert "<pre>" in html and "<code>" in html


def test_plain_text_flattens_markup_and_links():
    text = plain_text("## 结论\n\n**渠道**库存见底，[公告](https://example.com)\n\n- 第二行")
    assert text == "结论 渠道库存见底，公告 第二行"


def test_plain_text_truncates_at_one_hundred_twenty_chars():
    text = plain_text("字" * 200)
    assert len(text) == 120
    assert text.endswith("…")


def test_plain_text_is_one_line():
    assert plain_text("第一行\n第二行") == "第一行 第二行"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/services/test_markdown.py -q`
Expected: FAIL —— `ModuleNotFoundError: sams.services.markdown`。

- [ ] **Step 4: 写渲染器**

创建 `src/sams/services/markdown.py`：

```python
"""服务端 Markdown：去能力之后再渲染（D122）。

`html=False` 让原始 HTML 一律转义；`image` 规则被禁用（截图走附件）；链接只放行
http/https/mailto，其余协议把 `link_open/link_close` 换成空文本——**文本保留、链接消失**，
不会出现半截 `</a>`。推送用的纯文本从同一棵 token 树抽取，渲染与摘要因此不会两套口径。
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

#: 只有这三种协议能变成可点击的东西。相对链接也不放行：批注里没有「当前页面」，
#: 一个 `/admin` 会让读者以为这是应用自己的入口。
_SAFE_LINK = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)

PINNED_LIMIT = 120

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_MD.enable("table")
_MD.enable("strikethrough")
_MD.disable("image")


def _neutralize(token) -> None:
    """把一个链接标记改成空文本：嵌套的文字照常渲染，标签与属性都不留。"""
    token.type = "text"
    token.tag = ""
    token.nesting = 0
    token.attrs = {}
    token.content = ""
    token.markup = ""


def _sanitize_links(tokens) -> None:
    for token in tokens:
        children = token.children or ()
        stack: list[bool] = []
        for child in children:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                unsafe = not _SAFE_LINK.match(href)
                stack.append(unsafe)
                if unsafe:
                    _neutralize(child)
                else:
                    child.attrSet("target", "_blank")
                    child.attrSet("rel", "noopener noreferrer")
            elif child.type == "link_close":
                unsafe = stack.pop() if stack else False
                if unsafe:
                    _neutralize(child)


def render_markdown(source: str) -> str:
    """渲染成 HTML 片段。任何输入都不该抛：读不懂的部分最差也只是文本。"""
    tokens = _MD.parse(source or "")
    _sanitize_links(tokens)
    return _MD.renderer.render(tokens, _MD.options, {})


def plain_text(source: str, *, limit: int = PINNED_LIMIT) -> str:
    """推送与摘要用的一行纯文本：标题/段落/列表项/代码块取文字，链接取链接文字。"""
    parts: list[str] = []
    for token in _MD.parse(source or ""):
        if token.type == "inline":
            for child in token.children or ():
                if child.type in ("text", "code_inline"):
                    parts.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    parts.append(" ")
        elif token.type in ("fence", "code_block"):
            parts.append(token.content)
    text = " ".join(" ".join(parts).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/services/test_markdown.py -q`
Expected: 全绿。若 `test_plain_text_flattens_markup_and_links` 的分隔与实现不一致，按实现的实际分词调整断言（**只调空白**），不要保留横线/标记字符。

- [ ] **Step 6: ruff**

Run: `uv run ruff check --no-cache src/sams/services/markdown.py tests/services/test_markdown.py`
Expected: All checks passed。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml uv.lock src/sams/services/markdown.py tests/services/test_markdown.py
git commit -m "feat(annotations): 服务端 Markdown 渲染与推送纯文本抽取（D122）"
```

---

### Task 12: 配置 `[annotations]` 与 `public_base_url`

**Files:**
- Modify: `src/sams/core/config.py`
- Modify: `config/settings.example.toml`
- Test: `tests/core/test_config.py`

- [ ] **Step 1: 写失败测试**

在 `tests/core/test_config.py` 末尾追加：

```python
def test_annotations_defaults():
    from sams.core.config import AnnotationsCfg

    cfg = AnnotationsCfg()
    assert cfg.attachments_dir == ""
    assert cfg.max_attachments == 5
    assert cfg.max_attachment_mb == 5
    assert cfg.max_user_total_mb == 512
    assert cfg.max_body_chars == 20000
    assert cfg.break_glass_minutes == 15
    assert cfg.break_glass_max_per_hour == 5


def test_annotations_section_is_read(tmp_path):
    text = BASE + """
[annotations]
attachments_dir = "/srv/sams/attachments"
max_attachments = 3
max_body_chars = 5000
"""
    settings = Settings.load(write(tmp_path, text))
    assert settings.annotations.attachments_dir == "/srv/sams/attachments"
    assert settings.annotations.max_attachments == 3
    assert settings.annotations.max_body_chars == 5000


@pytest.mark.parametrize(
    "line",
    (
        "max_attachments = 0",
        "max_attachment_mb = 0",
        "max_user_total_mb = 0",
        "max_body_chars = 0",
        "break_glass_minutes = 0",
        "break_glass_max_per_hour = 0",
    ),
)
def test_annotation_limits_must_be_positive(tmp_path, line):
    text = BASE + "\n[annotations]\n" + line + "\n"
    with pytest.raises(ValidationError):
        Settings.load(write(tmp_path, text))


def test_public_base_url_is_trimmed_and_optional(tmp_path):
    settings = Settings.load(write(tmp_path))
    assert settings.server.public_base_url == ""

    text = BASE + '\n[server]\npublic_base_url = "https://sams.example.com/"\n'
    settings = Settings.load(write(tmp_path, text))
    assert settings.server.public_base_url == "https://sams.example.com"


def test_public_base_url_must_be_http_or_https(tmp_path):
    text = BASE + '\n[server]\npublic_base_url = "ftp://example.com"\n'
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings.load(write(tmp_path, text))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/core/test_config.py -q`
Expected: FAIL —— `ImportError: cannot import name 'AnnotationsCfg'`。

- [ ] **Step 3: 加配置**

修改 `src/sams/core/config.py`：

在 `ServerCfg` 里加字段（放在 `display_tz` 之后）：

```python
    #: 推送深链的根地址；空 = 不附链接（D126）。只接受 http/https，末尾斜杠在加载时去掉
    public_base_url: str = ""
```

`ServerCfg.validate` 末尾追加：

```python
        if self.public_base_url:
            if not self.public_base_url.startswith(("http://", "https://")):
                raise ValidationError(
                    "server.public_base_url 只接受 http:// 或 https://："
                    "一个猜出来的地址在手机上点不开，不如不放"
                )
```

在 `Settings` 之前新增：

```python
@dataclass(frozen=True)
class AnnotationsCfg:
    """批注与附件的上限（spec §9）。所有上限都是**拒绝线**，不是截断线：
    悄悄截一半的正文比报错更难发现。"""

    attachments_dir: str = ""
    max_attachments: int = 5
    max_attachment_mb: int = 5
    max_user_total_mb: int = 512
    max_body_chars: int = 20000
    break_glass_minutes: int = 15
    break_glass_max_per_hour: int = 5

    def validate(self) -> None:
        for name, value in (
            ("max_attachments", self.max_attachments),
            ("max_attachment_mb", self.max_attachment_mb),
            ("max_user_total_mb", self.max_user_total_mb),
            ("max_body_chars", self.max_body_chars),
            ("break_glass_minutes", self.break_glass_minutes),
            ("break_glass_max_per_hour", self.break_glass_max_per_hour),
        ):
            if value <= 0:
                raise ValidationError(f"annotations.{name} 必须为正数")
```

`Settings` 加字段：

```python
    annotations: AnnotationsCfg = field(default_factory=AnnotationsCfg)
```

`Settings.load` 里解析 `[annotations]`：

```python
        annotations_raw = raw.get("annotations") or {}
        annotations = AnnotationsCfg(
            attachments_dir=str(annotations_raw.get("attachments_dir", "")),
            max_attachments=int(annotations_raw.get("max_attachments", 5)),
            max_attachment_mb=int(annotations_raw.get("max_attachment_mb", 5)),
            max_user_total_mb=int(annotations_raw.get("max_user_total_mb", 512)),
            max_body_chars=int(annotations_raw.get("max_body_chars", 20000)),
            break_glass_minutes=int(annotations_raw.get("break_glass_minutes", 15)),
            break_glass_max_per_hour=int(
                annotations_raw.get("break_glass_max_per_hour", 5)
            ),
        )
```

`ServerCfg(...)` 构造加：

```python
            public_base_url=str(server_raw.get("public_base_url", "")).rstrip("/"),
```

`Settings` 构造加 `annotations=annotations`，`Settings.validate` 加 `self.annotations.validate()`。

- [ ] **Step 4: 示例配置**

在 `config/settings.example.toml` 的 `[server]` 段内追加（`display_tz` 之后）：

```toml
public_base_url = ""          # 推送深链根地址；空 = 不附链接。LAN 部署填 http://192.168.x.x:8765
```

文件末尾追加：

```toml

# 批注与附件（M4-B）。所有上限都是拒绝线：超了报错，不悄悄截断。
[annotations]
attachments_dir = ""        # 空 = <db 所在目录>/attachments；备份时这个目录要一起复制
max_attachments = 5         # 每条批注最多几张截图
max_attachment_mb = 5       # 单张上限（MB）
max_user_total_mb = 512     # 每个用户的附件总配额（MB）
max_body_chars = 20000      # 批注正文上限（字符）
break_glass_minutes = 15    # admin 单条私有批注的临时查看时长
break_glass_max_per_hour = 5  # 每管理员每小时最多授权次数（治理参数，不是性能参数）
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/core/test_config.py -q`
Expected: 全绿。

- [ ] **Step 6: ruff**

Run: `uv run ruff check --no-cache src/sams/core/config.py tests/core/test_config.py`
Expected: All checks passed。

- [ ] **Step 7: 提交**

```bash
git add src/sams/core/config.py config/settings.example.toml tests/core/test_config.py
git commit -m "feat(config): [annotations] 上限与 server.public_base_url（M4-B §9）"
```

---

### Task 13: 组装接线与全量验证

**Files:**
- Modify: `src/sams/bootstrap.py`
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_bootstrap.py` 末尾追加：

```python
def test_container_wires_annotations(container):
    """批注仓库与服务必须进同一条线程上下文（D113）：Web 线程不能借用别人的连接。"""
    built, _ = container
    user = built.users.create("writer", "correct-horse-battery")
    made = built.annotations.create(
        user.id, target_type="symbol", target_ref="A:600519", body_md="先看图"
    )
    assert built.annotations_store.get(made.id) is not None
    rows = built.annotations.list_target(user.id, "symbol", "A:600519")
    assert [row.id for row in rows] == [made.id]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bootstrap.py::test_container_wires_annotations -q`
Expected: FAIL —— `AttributeError: 'Container' object has no attribute 'annotations'`。

- [ ] **Step 3: 接线**

修改 `src/sams/bootstrap.py`：

imports 加：

```python
from sams.services.annotations import AnnotationService
from sams.storage.repos.annotations import AnnotationRepo
```

`ServiceContext` 加字段（放在 `alerts_store` 之后）：

```python
    #: 批注仓库：`annotations` 那个名字留给服务（与 `theses_store`/`rules_store` 同一命名法）
    annotations_store: AnnotationRepo
    #: 批注用例：五类目标的归属校验在这里，仓库只认「这一行是不是你的」
    annotations: AnnotationService
```

`_context_on` 里，在 `rule_rechecker` 之后、`sync` 之前建服务：

```python
    annotations_store = AnnotationRepo(database, clock=clock)
    annotations = AnnotationService(
        annotations_store,
        users,
        symbols,
        theses_store,
        rules_store,
        alerts_store,
        sim_accounts,
        sim_orders,
        cfg,
    )
```

`return ServiceContext(...)` 加：

```python
        annotations_store=annotations_store,
        annotations=annotations,
```

- [ ] **Step 4: 跑接线测试与分层守卫**

Run: `uv run pytest tests/test_bootstrap.py tests/test_layering.py -q`
Expected: 全绿。若分层守卫报 `services/annotations.py` 越界，检查它 import 的只有 `core` 与 `storage`——它**不许** import `monitor`（推送上下文在计划 3 用注入接）。

- [ ] **Step 5: 全量离线闸**

Run（后台跑，别阻塞在 2 分钟内）：`uv run pytest -q`
Expected: 全部通过。重点看 `tests/test_adversarial_storage.py`、`tests/services/test_health.py`、`tests/web/test_alerts_web.py`——状态从三值变四值，任何把 `STATUS` 或 `_STATUS_WORDS` 写死成三处的地方都会在这里露出来。

Run: `uv run ruff check --no-cache src tests`
Expected: All checks passed。

- [ ] **Step 6: 提交**

```bash
git add src/sams/bootstrap.py tests/test_bootstrap.py
git commit -m "feat(bootstrap): 批注仓库与服务进入线程上下文（M4-B §3）"
```

---

## 本计划完成时的状态

- 迁移 0020 已落库；旧 `handled` 行保持原样。
- 告警四态：`new → acknowledged → handled/dismissed`，`defer` 不改状态，终态 5 分钟内可 `undo`；每次动作一行 `alert_events`。
- `no_open_alert` 与健康积压都把 `acknowledged` 算作未处理。
- 私有批注能创建/读取/编辑/置顶/软删，编辑写 revision，标签规范化，五类目标的归属校验齐全；`team` 写入尚未开放（计划 4）。
- Markdown 渲染与纯文本抽取可用且默认安全。
- `[annotations]` 与 `server.public_base_url` 可配置。
- 容器里 `annotations_store` 与 `annotations` 可用，分层守卫通过。

**下一份计划（计划 2）从这里接：** 附件服务（Pillow 重编码、配额、下载授权、prune 回收）→ 推送上下文（`NotifyService` 的 context provider、`notify_log.context_json`、通道追加）→ 闭环动作服务（`act_with_fill` 同事务成交、thesis/rule/watch 校验、同步写批注）。
