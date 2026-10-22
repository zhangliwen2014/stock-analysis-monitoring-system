# M3-D 调度器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `sams` 有一个单进程常驻的调度器：按各市场本地收盘时点跑盘后任务链（日线、公司行为、基本面三件套、规则评估、周复检、汇率、日历），每个任务的结果落 `task_runs`，系统级告警送到 admin，健康页能回答「它还在不在跑、上一轮跑成了什么」。

**Architecture:** 新层放在 `services/scheduler/`（`services` 可以 import `core/storage/datasource/analysis`，但不能 import `monitor`，所以系统告警走 core 的 `AlertSender` 端口，由 `bootstrap` 适配 `NotifyService`）。时间计算（到期、周期键）是一个只依赖 `CalendarView` 协议的无 IO 模块；job 功能只调已有的服务/仓库；`JobRunner` 负责 `task_runs` 的隔离与重试；`Scheduler` 负责 tick 顺序、心跳与单实例守卫。所有时间参数可注入，测试不 sleep、不看墙钟。

**Tech Stack:** Python 3.13、DuckDB、Polars、pytest、ruff（line-length 100，E/F/I/UP/B/SIM，`--no-cache`）、`uv run`。

---

## 0. 这一轮的范围与口径（先读这一节）

### 0.1 范围：不含盘中实时行情

用户对本轮的范围决定是 **「不含实时行情（推荐）」**。理由在库里：

- `quotes_snapshot` 表没有仓库也没有写入方，datasource 没有报价适配器（只有日线）；
- spec §6.2 与 U7 已把实时轮询与快照表记在 M4；
- M3-C 的十四种模板都是收盘确认口径（`RuleData.read` 读的是日线）。

所以 §12 的盘中那一行（每 10 秒批量快照 → `QuoteUpdated` → 增量指标 → 规则评估）**本轮不实现**，`QuoteUpdated` 仍然没有发布方。M3 的总验收「挂机运行一个真实 A 股交易日」在本轮的口径是：**收盘后一轮任务链跑通、自选股无漏报、同规则不刷屏、非交易时段无用户告警、除权股不误报、重启不重复推送**。盘中那一半（盘中止损在收盘前触发）留 M4，验收清单里逐条写明（Task 14）。

### 0.2 §12 逐条落点

| §12 条目 | 本轮落点 | 钉住它的用例 |
|---|---|---|
| 单进程常驻、任务注册表、按市场独立循环 | `Scheduler` + `JobSpec` + `services/scheduler/due.py` | `test_each_market_uses_its_own_close`、`test_a_second_daemon_is_refused` |
| 盘中每 N 秒快照 → `QuoteUpdated` | **不做**（D97，M4/U7） | — |
| 盘后：日线增量入库 | `bars` job（`SyncService.sync_daily`） | `test_bars_job_is_incremental` |
| 盘后：财务/资金流更新 | `financials` / `valuation` / `flow` job | `test_financials_job_writes_what_the_chain_answers` |
| 盘后：指标重算（收盘确认） | **不注册 job**：分析层按需计算，没有落库产物（D107） | — |
| 盘后：观点估值重算 | **不注册 job**：估值不落快照（D76）；规则评估读的是同一份按需估值 | `test_rules_job_asks_for_that_market_only` |
| 盘后：[开关]全市场扫描 | **不做**（M5 的配置开关） | — |
| 周任务：观点漂移复检 | `recheck` job（`Rechecker.run`） | `test_weekly_due_fires_on_the_last_trading_day_of_the_iso_week` |
| 宁缺勿滥 | 复用 M3-C 的 `RuleEngine`；数据缺失 → `skipped` 不触发 | M3-C 已有 |
| 采集侧相反：日历不确定按交易日轮询 | 复用 `CalendarService` 的保守日 | M2 已有 |
| 系统自监控：连续采集失败/心跳丢失/日历失败 | `SystemAlerter`（订阅 `SystemAlarm`）+ 心跳表 + `[调度]` 健康页 | `test_the_alarm_goes_to_active_admins_only`、`test_a_stale_heartbeat_is_printed_and_fails_the_exit_code`、`test_a_broken_calendar_does_not_stop_the_next_market` |
| 崩溃恢复：不重复打扰 | `task_runs.period_key` + M3-C 的 alerts 冷却（跨重启） | `test_a_successful_period_is_not_rerun`、`test_a_failure_is_retried_only_after_the_gap` |
| 任务隔离：一个失败不阻塞其他 | `JobRunner` 捕获每个 job 的异常并单独记账 | `test_one_bad_code_does_not_drop_the_rest`、`test_one_market_failure_does_not_stop_the_rest` |
| admin 健康页 | `sams health` 新增 `[调度]` 一节 + 退出码 | `test_job_lines_show_the_last_outcome_and_streak`、`test_a_stale_heartbeat_is_printed_and_fails_the_exit_code` |

### 0.3 本轮的决定

| 编号 | 决定 | 理由（一句话） |
|---|---|---|
| D97 | 本轮不含盘中实时行情 | 依赖 M4 的快照表与报价源；M3-C 的模板本来就是收盘确认口径 |
| D98 | `task_runs` 加 `period_key`，不用 `started_at` 的日期当周期 | 周任务与月任务的「跑过没有」不是一个交易日能回答的 |
| D99 | 到期判断在市场**本地**收盘时点之后，`as_of` 是市场本地交易日 | D8 的同一口径：UTC 日期在美股上恒错一天 |
| D100 | 单实例守卫用心跳表：心跳还热的第二个调度器拒绝启动 | 两个常驻进程会让同一个周期各发一次告警 |
| D101 | 心跳丢失的告警由**外部**承担（`sams health` 退出码） | 死掉的调度器发不出「我死了」；健康页是唯一的旁观者 |
| D102 | 系统告警只发 `is_admin && active` 的用户；`rule_id=NULL`、`market='SYS'`、`code=component`；同 component 在 cooldown 内只发一次 | 「与用户告警分开」落在数据形状与接收人上；去重跨重启 |
| D103 | 任务失败不重复发 `SystemAlarm`；只有连续 3 个周期失败由 runner 补一条 | registry/calendar 已经为同一个根因发过告警，一次故障两条消息是噪音 |
| D104 | D88 的归类延伸到 daily/actions/frame：全链可重试才 `TransientError`；有任何 `no_answer`/结构漂移 → `DatasourceError` | 「下轮再试」还是「出声告警」的分界只能有一份（M3-A 的 D88 只做了汇率那一条） |
| D105 | 失败任务按 `RETRY_GAP_MIN = 10` 重试；成功不重跑；`running` 超过 6 小时算崩溃留下的 | 每 tick 重拉全量不是重试，是打源 |
| D106 | 没有能力的市场不注册该 job（用 `registry.chain_for` 判） | HK/US 没有按报告期的财务源（D65/U24），每天一条失败会把真故障淹掉 |
| D107 | 不注册「指标重算」「观点估值重算」两个 job | 两者在本系统都没有落库产物；凭空造一个 job 只会造一个假执行记录 |
| D108 | fx 的到期日 = 「当天开市的市场都已收盘」的最近一天；还没有任何市场拿到到期日就不跑 | 汇率没有市场维度，但它服务于所有市场的收盘后账本 |
| D109 | 周复检在该市场 ISO 周的最后一个交易日收盘后跑；日历每月刷新当月，12 月加刷新下一年 | 周五复检、周一前到位；下一年日历在跨年前准备好 |
| D110 | 全市场扫描与 `QuoteUpdated` 生产者不进本轮 | 前者是 M5，后者依赖 M4 的报价源 |

### 0.4 计划依据的既有事实（写计划时逐条查过）

- `task_runs` 在 `0001_init.sql:360-369`：`id/job_name/market/started_at/finished_at/status/rows_affected/error`，**没有仓库、没有写入方**。
- `alerts.rule_id` 在 `0015_notify.sql` 已改成可空，注释点名「手工投递、M3-D 的系统告警」；`alerts.market/code/stage` 仍 NOT NULL，所以系统告警用 `market='SYS'`、`code=component`。
- `Settings.polling_interval_s` 已有（默认 10，`core/config.py`），但**没有**调度 tick 的配置；`MarketCfg.post_close_min` 已有（A/HK 30、US 60，测试配置里）。
- `CalendarService.post_close_run_at/next_trading_day/is_trading_day` 已有且**没有 src 调用方**（`services/calendar.py:187/246/256`）。
- `RuleEngine.check` 的 docstring 已经写着「M3-D 的调度器以后按市场时段调这里」（`services/rules/engine.py:207`），但它**没有 market 过滤**——本轮补。
- `bootstrap.attach_log_sink` 的注释已为 admin 渠道留了位置（`bootstrap.py:69-76`）。
- 三个基本面 job 的能力判据是 `AdapterRegistry.chain_for(market, method)`（`datasource/registry.py:315-323`）；HK/US 的 `financials` 链为空。
- `FxRepo.latest/freshness`、`ValuationRepo.latest_date`、`FundingFlowRepo.latest_date`、`BarRepo.latest_date` 都已存在，job 的增量窗口从它们取。
- `WatchlistRepo.list_all`（审计视图）、`ThesisRepo.list_all`、`UserRepo.list_users`、`SimAccountRepo.list_for/cash_balances`、`SimOrderRepo.positions`、`RuleRepo.list_for` 都已存在；`RuleRepo` **没有** `list_all`，全用户规则要按用户读。
- `tests/support_valuation.py` 的 `make_container` 已经能装配一个不联网的容器（`StubFundamentalsAdapter` + `support.StubAdapter`），验收用例从它取世界。
- 基线：M3-C 收尾实测 **2738 passed / 14 deselected**，分层守卫 **381 passed**。本轮收尾重新实测，不拿这两个数当承诺。

### 0.5 本轮明确不做

- `QuoteUpdated` / `IndicatorUpdated` 的发布方；盘中快照与 10 秒轮询；`quotes_snapshot`。
- 全市场扫描调度（`screen pr` 仍是手工命令）。
- admin 的 Web 页面（M4）；`[调度]` 只做 CLI 健康页。
- 任务表的历史清理（每天新增约 10 行，远没到需要 retention 的量）。
- 心跳丢失的外部告警投递（需要另一个进程/cron；本轮只给退出码与屏幕上那一句）。

---

## 1. 文件结构

```
src/sams/
  core/errors.py                      # + SchedulerBusy
  core/config.py                      # + scheduler_tick_s（默认 60）
  storage/migrations/0018_scheduler.sql
  storage/repos/task_runs.py          # TaskRunRepo + HeartbeatRepo（两个都是调度器的账）
  services/scheduler/__init__.py      # 只导出 Scheduler
  services/scheduler/types.py         # JobContext / JobOutcome / JobSpec / JobDeps
  services/scheduler/due.py           # now/日期 → Due(period_key, as_of)；无 IO
  services/scheduler/universe.py      # 自选股 + 持仓 + 规则 + 观点 → 目标与币种对
  services/scheduler/jobs.py          # 九个 job 的功能 + build_jobs（按能力注册）
  services/scheduler/runner.py        # task_runs 隔离、重试间隔、连续失败告警
  services/scheduler/alarms.py        # SystemAlarm → admin（去重跨重启）
  services/scheduler/report.py        # ScheduleData / JobHealth（健康页的读数）
  services/scheduler/service.py       # Scheduler.tick / run_forever / report
  services/rules/engine.py            # check(..., market=None)
  services/rules/recheck.py           # run(..., market=None)
  services/health.py                  # + schedule 一节
  cli/schedule.py                     # sams run start|once|jobs（模块名不叫 run：撞包级入口）
  cli/__init__.py                     # 注册 run 命令
  bootstrap.py                        # 装配 scheduler / alerter / health 的读函数
config/settings.example.toml          # + scheduler_tick_s

tests/
  support_scheduler.py                # FakeCalendar + 记录型 jobs 替身 + 世界种子
  storage/test_migration_0018.py
  storage/test_task_runs.py
  core/test_config.py                 # + scheduler_tick_s
  datasource/test_registry.py         # + 三条链的失败归类
  services/test_scheduler_due.py
  services/test_scheduler_universe.py
  services/test_scheduler_jobs_data.py
  services/test_scheduler_jobs_ops.py
  services/test_scheduler_runner.py
  services/test_scheduler_alarms.py
  services/test_scheduler_service.py
  services/test_health.py             # + [调度]
  services/test_rules_engine.py       # + market 过滤
  services/test_rules_recheck.py      # + market 过滤
  cli/test_health_cli.py              # [调度] 一节与退出码
  cli/test_run_cli.py
  test_scheduler_acceptance.py        # 一个模拟交易日的端到端
```

分工的边界：

- `due.py` 只回答「现在该跑哪个周期」，不读库、不碰服务；
- `universe.py` 只回答「跑哪些对象、换哪些币种对」，不采集；
- `jobs.py` 只把已有服务拼成一条 job，不决定自己何时跑；
- `runner.py` 只负责「这一次跑成什么、记哪一行、什么时候准重试」，不选 job；
- `service.py` 只负责 loop、顺序、心跳与把上面四块接起来；
- `report.py` 只读，供健康页使用。

---

### Task 1: 0018 迁移 + task_runs/心跳仓库

**Files:**
- Create: `src/sams/storage/migrations/0018_scheduler.sql`
- Create: `src/sams/storage/repos/task_runs.py`
- Modify: `src/sams/core/errors.py`（文件末尾加 `SchedulerBusy`）
- Test: `tests/storage/test_migration_0018.py`
- Test: `tests/storage/test_task_runs.py`

- [ ] **Step 1: 写迁移与仓库的失败用例**

`tests/storage/test_migration_0018.py`：

```python
"""0018：调度器的账。

这一枚只验形状：旧式 INSERT（不写新列）落下的行读出来 `period_key`/`note` 是 NULL，
心跳表能存能读且 `beat()` 真的推进时间。行为契约在 `test_task_runs.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support import FrozenClock

from sams.storage.repos.task_runs import HeartbeatRepo


def test_task_runs_gains_period_key_and_note_nullable(db):
    columns = db.columns("task_runs")
    assert "period_key" in columns
    assert "note" in columns
    # 老行不会被填上一个假的周期键：NULL 是「不知道」，不是「今天」
    db.execute(
        "INSERT INTO task_runs (id, job_name, market, started_at, status)"
        " VALUES ('old', 'bars', 'A', ?, 'ok')",
        [datetime(2024, 1, 2, tzinfo=UTC)],
    )
    row = db.query("SELECT period_key, note FROM task_runs WHERE id = 'old'").to_dicts()[0]
    assert row == {"period_key": None, "note": None}


def test_heartbeats_table_round_trips(db):
    clock = FrozenClock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = HeartbeatRepo(db, clock=clock)
    beat_id = repo.start(note="tick=60s")
    started = repo.latest().started_at
    clock.advance(seconds=60)
    repo.beat(beat_id)
    latest = repo.latest()
    assert latest is not None
    assert latest.id == beat_id
    assert latest.note == "tick=60s"
    assert latest.started_at == started
    assert latest.beat_at == started + timedelta(seconds=60)
```

`tests/storage/test_task_runs.py`：

```python
"""`task_runs` 的读写：周期键是「这个周期跑过没有」的唯一依据（D98）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRunRepo


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def now_utc(self):
        return self.moment

    def today(self):
        return self.moment.date()

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


def test_start_and_finish_round_trip(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    run_id = repo.start("bars", market="A", period_key="2024-10-25")
    running = repo.get(run_id)
    assert running is not None and running.status == RUNNING
    assert running.finished_at is None

    clock.advance(days=0, seconds=5)
    done = repo.finish(run_id, status=OK, rows_affected=42, note="更新 3 只")
    assert done.status == OK
    assert done.rows_affected == 42
    assert done.note == "更新 3 只"
    assert done.finished_at is not None
    assert repo.latest_for_period("bars", "A", "2024-10-25").id == run_id


def test_latest_for_period_does_not_leak_across_periods(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    old = repo.start("bars", market="A", period_key="2024-10-24")
    repo.finish(old, status=OK)
    assert repo.latest_for_period("bars", "A", "2024-10-25") is None
    assert repo.latest_for_period("bars", "A", "2024-10-24").id == old


def test_global_job_uses_null_market(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    run_id = repo.start("fx", period_key="2024-10-25")
    repo.finish(run_id, status=OK)
    market_run = repo.start("fx", market="A", period_key="2024-10-25")
    repo.finish(market_run, status=OK)
    assert repo.latest_for_period("fx", None, "2024-10-25").id == run_id
    # 市场维度不串：A 的那一行不是「全局那条」的替身
    assert repo.latest_for_period("fx", "A", "2024-10-25").id == market_run


def test_start_rejects_an_empty_job_name(db):
    repo = TaskRunRepo(db)
    with pytest.raises(ValidationError, match="job_name"):
        repo.start("", market="A")


def test_finish_rejects_unknown_status(db):
    repo = TaskRunRepo(db)
    run_id = repo.start("bars", market="A", period_key="2024-10-25")
    with pytest.raises(ValidationError, match="status"):
        repo.finish(run_id, status="done")


def test_recent_is_newest_first_and_sized(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    ids = []
    for _ in range(4):
        ids.append(repo.start("bars", market="A", period_key="2024-10-25"))
        repo.finish(ids[-1], status=FAILED, error="boom")
        clock.advance(minutes=1)
    recent = repo.recent("bars", "A", limit=3)
    assert [row.id for row in recent] == list(reversed(ids[1:]))


def test_unfinished_run_is_still_latest(db):
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    run_id = repo.start("bars", market="A", period_key="2024-10-25")
    clock.advance(hours=7)
    latest = repo.latest_for_period("bars", "A", "2024-10-25")
    assert latest.id == run_id and latest.status == RUNNING
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/storage/test_migration_0018.py tests/storage/test_task_runs.py -q
```

Expected: FAIL（`sams.storage.repos.task_runs` 不存在；`db` 里 `period_key` 也不存在）。

- [ ] **Step 3: 写迁移**

`src/sams/storage/migrations/0018_scheduler.sql`：

```sql
-- 0018：调度器的两本账（spec §12）
--
-- 1) `task_runs` 从 0001 就在库里，却一直没有写入方。调度器要回答的第一个问题不是
--    「上次跑得怎么样」而是「**这个周期**跑过没有」——`started_at` 的日期答不了：周任务的
--    两个成功行可能落在周三与周四（补跑），月任务更是跨月。所以周期键单独一列，
--    按 job 的周期写成 `YYYY-MM-DD` / `YYYY-Www` / `YYYY-MM`（D98）。
--    `note` 与 `error` 分开：`error` 是「没成」，`note` 是「成了，但只更新了 3 只」——
--    把后者写进 error，健康页会把一次正常补跑报成失败。
--
-- 2) `scheduler_heartbeats` 是「这台机器上调度器还活着吗」的唯一依据。一行代表一次
--    启动，`beat_at` 每 tick 原地更新（不增行）。单实例守卫读它（D100），健康页读它
--    （D101）。心跳不指向任何一行 `task_runs`：它记录的是进程，不是某次执行；
--    两个进程同时启动时，先写的那一行也必须留下痕迹。
CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
    id         VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    beat_at    TIMESTAMPTZ NOT NULL,
    note       VARCHAR
);

-- DuckDB 的 ADD COLUMN 不能带 NOT NULL（0015/0016/0017 的头注同一条）；
-- IF NOT EXISTS 让「迁移重放」与「新库一次建全」走同一条语句。
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS period_key VARCHAR;
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS note VARCHAR;
```

- [ ] **Step 4: 写仓库**

`src/sams/storage/repos/task_runs.py`：

```python
"""`task_runs` 与 `scheduler_heartbeats`（spec §12）。

两个表放在一个模块里，因为它们是同一个问题的两半：
「上一轮跑成了什么」（task_runs）与「现在还有没有人在跑」（heartbeats）。
分开放的话，单实例守卫与健康页要同时认识两个模块，而它们读的是同一次时钟。

状态只有三种：`running` 是「这一跑还没收尾」；同一行超过六小时仍未收尾才是崩溃留下的
证据（D105），读取侧不能把每个 `running` 都当成死进程。`ok` 与 `failed` 是两种结论。
没有 `skipped`：「这一轮不该跑」由到期计算决定，不会先写一行再跳过——那会让「没跑过」
与「跑过但没做事」在健康页上长得一样。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

RUNNING, OK, FAILED = "running", "ok", "failed"
_FINISHED = (OK, FAILED)


@dataclass(frozen=True)
class TaskRun:
    id: str
    job_name: str
    market: str | None
    period_key: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    rows_affected: int | None
    error: str | None
    note: str | None


@dataclass(frozen=True)
class Heartbeat:
    id: str
    started_at: datetime
    beat_at: datetime
    note: str | None


_COLUMNS = (
    "id, job_name, market, period_key, started_at, finished_at, status,"
    " rows_affected, error, note"
)


def _to_run(row: dict) -> TaskRun:
    return TaskRun(
        id=str(row["id"]),
        job_name=str(row["job_name"]),
        market=row["market"],
        period_key=row["period_key"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=str(row["status"]),
        rows_affected=row["rows_affected"],
        error=row["error"],
        note=row["note"],
    )


class TaskRunRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def start(
        self, job_name: str, *, market: str | None = None, period_key: str | None = None
    ) -> str:
        if not job_name:
            raise ValidationError("job_name 不能为空：一行没有名字的执行记录不是证据")
        run_id = new_id()
        self.db.execute(
            "INSERT INTO task_runs (id, job_name, market, period_key, started_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, job_name, market, period_key, self.clock.now_utc(), RUNNING],
        )
        return run_id

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        rows_affected: int = 0,
        error: str | None = None,
        note: str | None = None,
    ) -> TaskRun:
        if status not in _FINISHED:
            raise ValidationError(
                f"未知 status={status!r}：收尾只能是 {list(_FINISHED)}；{RUNNING} 是起点"
            )
        self.db.execute(
            "UPDATE task_runs SET finished_at = ?, status = ?, rows_affected = ?,"
            " error = ?, note = ? WHERE id = ?",
            [self.clock.now_utc(), status, int(rows_affected), error, note, run_id],
        )
        row = self.get(run_id)
        assert row is not None
        return row

    def get(self, run_id: str) -> TaskRun | None:
        rows = self.db.query(f"SELECT {_COLUMNS} FROM task_runs WHERE id = ?", [run_id])
        return _to_run(rows.to_dicts()[0]) if rows.height else None

    def latest_for_period(
        self, job_name: str, market: str | None, period_key: str
    ) -> TaskRun | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM task_runs"
            " WHERE job_name = ? AND market IS NOT DISTINCT FROM ? AND period_key = ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            [job_name, market, period_key],
        )
        return _to_run(rows.to_dicts()[0]) if rows.height else None

    def recent(self, job_name: str, market: str | None, *, limit: int = 3) -> list[TaskRun]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM task_runs"
            " WHERE job_name = ? AND market IS NOT DISTINCT FROM ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT ?",
            [job_name, market, int(limit)],
        )
        return [_to_run(row) for row in rows.to_dicts()]

    def count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM task_runs") or 0)


class HeartbeatRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def start(self, *, note: str | None = None) -> str:
        beat_id = new_id()
        now = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO scheduler_heartbeats (id, started_at, beat_at, note)"
            " VALUES (?, ?, ?, ?)",
            [beat_id, now, now, note],
        )
        return beat_id

    def beat(self, beat_id: str) -> None:
        self.db.execute(
            "UPDATE scheduler_heartbeats SET beat_at = ? WHERE id = ?",
            [self.clock.now_utc(), beat_id],
        )

    def latest(self) -> Heartbeat | None:
        rows = self.db.query(
            "SELECT id, started_at, beat_at, note FROM scheduler_heartbeats"
            " ORDER BY beat_at DESC, rowid DESC LIMIT 1"
        )
        if rows.is_empty():
            return None
        row = rows.to_dicts()[0]
        return Heartbeat(
            id=str(row["id"]),
            started_at=row["started_at"],
            beat_at=row["beat_at"],
            note=row["note"],
        )
```

`src/sams/core/errors.py` 末尾加：

```python
class SchedulerBusy(SamsError):
    """同一个库已经有一个心跳还热的调度器（D100）。

    单独一个类型而不是 `ValidationError`：这不是「参数敲错了」（退出 2 会让人去查命令），
    而是「另一个进程正在做这件事」——重试的动作是等它停，不是改命令行。
    """
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/storage/test_migration_0018.py tests/storage/test_task_runs.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/migrations/0018_scheduler.sql src/sams/storage/repos/task_runs.py src/sams/core/errors.py tests/storage/test_migration_0018.py tests/storage/test_task_runs.py
git commit -m "feat(storage): 0018 调度器的两本账（周期键 + 心跳）"
```

**评审收尾（2026-10-22）**：实现落在 `abbc5c6`，两路评审之后修在 `575bc26`
（代码与用例一起提交；上面五段代码已同步成修后的样子，别改回去）：

1. **排序的第二个键不能是 uuid4**。`started_at DESC, id DESC` 在时间戳相同时按随机
   十六进制挑行，`latest_for_period` 会随机选中同一周期里的哪一次；换成 DuckDB 的
   `rowid DESC`（插入顺序），心跳的 `latest` 同改。钉它的用例：
   `test_recent_is_newest_first_and_sized`。
2. **心跳用例原先测不到 `beat()`**：`beat_at >= started_at` 在 `start()` 之后本来就真，
   把 `beat()` 换成 no-op 仍然绿。改成冻时钟 + 前进 60 秒，断言 `started_at` 不变、
   `beat_at` 恰好前进；市场隔离那枚也补了一行 A 股账，否则「None 匹配所有市场」的
   实现仍会绿。
3. **D96：三处话说过了头**。`running` 不全是崩溃证据（正在跑也是 running；六小时
   未收尾才是 D105 的证据）；迁移头注里的 `beat_id` 根本不存在（表列名是 `id`）；
   `finish` 里「刚写完」描述的是一次 UPDATE。前两处改口径，第三处删掉注释。
   `assert` 保留：那是同一连接、刚 UPDATE 过的内部不变量，不为不可能的分支加域错误。
4. **删掉没有 src 读者的 API**：`STATUSES` 与 `TaskRunRepo.latest` 在实现里没有调用方；
   两枚读 `latest` 的用例改读 `latest_for_period`（Task 8 的市场隔离用例同改）。

变异电池七条全红（周期隔离、NULL 市场隔离、心跳推进、recent 顺序、note 落库、
status 校验、空 job_name）。四条闸：目标用例 **24 passed**、`ruff check` 全绿、
分层 **384 passed**、离线 **2750 passed / 14 deselected**。

---

### Task 2: `scheduler_tick_s` 配置

**Files:**
- Modify: `src/sams/core/config.py`
- Modify: `config/settings.example.toml`
- Test: `tests/core/test_config.py`

- [ ] **Step 1: 写失败用例**

在 `tests/core/test_config.py` 里追加（复用文件已有的 `BASE` 与 `write(tmp_path, text=BASE)`；`BASE` 已含 `[general]` 与 `[markets.A]`）：

```python
def test_scheduler_tick_defaults_to_60(tmp_path):
    assert Settings.load(write(tmp_path)).scheduler_tick_s == 60


def test_scheduler_tick_is_read_and_validated(tmp_path):
    read = write(tmp_path, BASE.replace("[general]", "[general]\nscheduler_tick_s = 15"))
    assert Settings.load(read).scheduler_tick_s == 15

    with pytest.raises(ValidationError, match="scheduler_tick_s"):
        Settings.load(write(tmp_path, BASE.replace("[general]", "[general]\nscheduler_tick_s = 0")))
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/core/test_config.py -q
```

Expected: FAIL（`Settings` 没有 `scheduler_tick_s`）。

- [ ] **Step 3: 实现**

`src/sams/core/config.py`：

```python
@dataclass(frozen=True)
class Settings:
    db_path: str
    secret_key: str
    report_currency: str
    rate_limit_per_s: float
    polling_interval_s: int
    cooldown_min: int
    markets: dict[str, MarketCfg]
    #: 调度器的心跳/到期检查间隔（D97）。它**不是**盘中轮询间隔：
    #: `polling_interval_s` 预留给 M4/U7 的 10 秒快照，那条链目前还没有消费者；
    #: 盘后任务按分钟级检查就够了，把一个盘中参数借给它，会让改盘中频率顺手改掉盘后
    scheduler_tick_s: int = 60
```

`load()` 里：

```python
            scheduler_tick_s=int(general.get("scheduler_tick_s", 60)),
```

`validate()` 里：

```python
        if self.scheduler_tick_s <= 0:
            raise ValidationError("scheduler_tick_s 必须为正数")
```

`config/settings.example.toml` 的 `[general]` 段加一行：

```toml
scheduler_tick_s = 60        # 调度器检查到期任务的间隔（秒）；不是盘中轮询
```

- [ ] **Step 4: 跑绿**

```bash
uv run pytest tests/core/test_config.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/core/config.py config/settings.example.toml tests/core/test_config.py
git commit -m "feat(config): 调度器 tick 与盘中轮询分开（D97）"
```

**评审收尾（2026-10-22）**：实现落在 `02b9875`，行为与计划一致；只修了三处
措辞/对齐（`a7e9160`，上面两段代码已同步）：

1. 注释把间隔归到 D100——D100 是单实例心跳守卫，**D97** 才是「调度器不进盘中」；
   决策号改对。
2. 「`polling_interval_s` 服务于 M4 的 10 秒快照」是前瞻句（那条链现在没有消费者）；
   改成「预留给 M4/U7」。
3. 示例配置那一行的 `#` 比同段其它行左一列；补一个空格。

变异电池三条全红（默认 60、读取到的值、0 的校验）。四条闸：目标用例 **12 passed**、
`ruff check` 全绿、分层 **384 passed**、离线 **2752 passed / 14 deselected**。

### Task 3: 三条链的失败归类（D88 延伸到 daily/actions/frames）

**Files:**
- Modify: `src/sams/datasource/registry.py`
- Test: `tests/datasource/test_registry.py`

- [ ] **Step 1: 写失败用例**

在 `tests/datasource/test_registry.py` 末尾追加：

```python
class AllFailAdapter:
    """每个能力都抛同一个异常：验「全链都失败」时终端错误是哪一种。"""

    markets = frozenset({"A"})

    def __init__(self, name: str, exc: Exception) -> None:
        self.name = name
        self.exc = exc

    def daily_bars(self, market, code, start, end):
        raise self.exc

    def corp_actions(self, market, code, start, end):
        raise self.exc

    def financials(self, market, code, start, end):
        raise self.exc


def _two_failing(*exc: Exception):
    return registry(
        AllFailAdapter("akshare", exc[0]),
        AllFailAdapter("eastmoney", exc[1]),
    )


def test_all_daily_sources_rate_limited_is_transient():
    """全链都是限流 ⇒ 「下轮再试」，不是「链坏了」（D104）。"""
    reg = _two_failing(RateLimited("akshare", "429"), RateLimited("eastmoney", "429"))
    with pytest.raises(TransientError, match="所有源均失败"):
        reg.fetch_daily("A", "600519", D1, D2)


def test_one_structural_failure_makes_daily_terminal():
    """掺进一个改版 ⇒ 出声告警，不带「重试就好」的意思。"""
    reg = _two_failing(RateLimited("akshare", "429"), FieldDrift("eastmoney", "少列"))
    with pytest.raises(DatasourceError) as caught:
        reg.fetch_daily("A", "600519", D1, D2)
    assert not isinstance(caught.value, TransientError)


def test_actions_keep_retryability_in_the_terminal_error():
    reg = _two_failing(RateLimited("akshare", "429"), RateLimited("eastmoney", "429"))
    with pytest.raises(TransientError, match="取不到公司行为"):
        reg.fetch_actions("A", "600519", D1, D2)


def test_frames_with_a_no_answer_are_terminal_even_if_the_rest_are_retryable():
    """「答不上来」不是「下轮再试」：它意味着这个源今天的答复不可读，必须出声。"""
    reg = _two_failing(
        RateLimited("akshare", "429"), EmptyResponse("eastmoney", "接口回空")
    )
    with pytest.raises(DatasourceError) as caught:
        reg.fetch_financials("A", "600519", D1, D2)
    assert not isinstance(caught.value, TransientError)


def test_frames_all_rate_limited_are_transient():
    reg = _two_failing(RateLimited("akshare", "429"), RateLimited("eastmoney", "429"))
    with pytest.raises(TransientError, match="取不到基本面"):
        reg.fetch_financials("A", "600519", D1, D2)
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/datasource/test_registry.py -q -k "transient or terminal or no_answer"
```

Expected: FAIL（`fetch_daily/fetch_actions/fetch_financials` 的终端错误恒为 `DatasourceError`，不是 `TransientError`）。

- [ ] **Step 3: 实现**

`src/sams/datasource/registry.py` 在 `RETRYABLE` 下方加一个函数：

```python
def _terminal_error(
    reason: str, causes: list[Exception | None], *, no_answer: bool = False
) -> DatasourceError:
    """全链都失败时终端错误该是哪一种（D88/D104）。

    判据只有一条：**每一个真失败过的源都只抖了一下**（限流/超时/网络），才值得
    「下轮再试」；链上只要有一个改版（`FieldDrift`）、或者有源连「没有」都没答清
    （`EmptyResponse`），这次失败就要人出声——重试不会让一个缺失的字段长回来。
    """
    if causes and not no_answer and all(isinstance(cause, RETRYABLE) for cause in causes):
        return TransientError("registry", reason)
    return DatasourceError("registry", reason)
```

`_Outcome` 加一列（放在 `tries` 后面）：

```python
    #: 原始异常：终端错误的归类要靠类型，字符串里活不下来（`_SourceFailed.cause` 同理）
    cause: Exception | None = None
```

`_walk` 的 failed 分支改为：

```python
            except _SourceFailed as failed:
                yield _Outcome(
                    name, "failed", error=failed.error, tries=failed.tries, cause=failed.cause
                )
                continue
```

`_call` 的两个 `raise _SourceFailed(...)` 补上 `cause`：

```python
        except DatasourceError as exc:
            self._mark_failure(name, exc)
            raise _SourceFailed(name, calls, str(exc), exc) from exc
        except Exception as exc:  # noqa: BLE001
            wrapped = DatasourceError(name, f"{type(exc).__name__}: {exc}")
            self._mark_failure(name, wrapped)
            raise _SourceFailed(name, calls, str(wrapped), wrapped) from exc
```

`fetch_daily`：循环前加 `causes: list[Exception | None] = []`，failed 分支加
`causes.append(out.cause)`，末尾的 `raise DatasourceError(...)` 改成：

```python
        raise _terminal_error(f"{market} {code} 所有源均失败 → {detail}", causes)
```

`fetch_actions` 与 `_fetch_frame`：循环前各加 `causes` 与 `no_answer = False`；
`out.kind == "failed"` 分支加 `causes.append(out.cause)`；`out.kind == "no_answer"`
分支置 `no_answer = True`；两处末尾的 `raise DatasourceError(...)` 改成：

```python
        raise _terminal_error(reason, causes, no_answer=no_answer)
```

（`fetch_actions` 与 `_fetch_frame` 现在把整段 reason 先在变量里拼好、再 raise；改名后变量名沿用
`reason`，不要留下一个不再被使用的长表达式。）

`fetch_fx` 末尾的两行分支收敛成：

```python
        raise _terminal_error(reason, [cause for _, cause in failed])
```

- [ ] **Step 4: 跑绿**

```bash
uv run pytest tests/datasource/test_registry.py -q
```

Expected: PASS（原有 30 余条一起跑，确认没有把「全空」与「全失败」的分支改坏）。

- [ ] **Step 5: 提交**

```bash
git add src/sams/datasource/registry.py tests/datasource/test_registry.py
git commit -m "feat(datasource): 全链失败的重试分界延伸到日线/公司行为/基本面（D104）"
```

**评审收尾（2026-10-22）**：实现落在 `742392b`，两路评审之后修在 `289341a`。评审发现的问题与处理：

1. 两路评审同时指出同一处 BLOCKING：`_call`/`_call_fx` 的兜底分支把包装后的
   `DatasourceError` 当作 `cause`，于是 `RETRYABLE` 里的裸 `OSError`/`TimeoutError`
   永远不命中——docstring 说的「超时/网络算抖动」（D96）在实际路径上不成立。修复是
   让 `cause` 保留原始异常；汇率报错文案改用 `_SourceFailed.error`，类型名不丢。
2. actions 的 `no_answer` 置位、以及 `causes=[]`（一个能取日线的源都没有）时
   `all([])==True` 的守卫，都没有用例钉住；补了三条用例与一条汇率用例，并把
   structural 用例参数化成两种顺序。
3. 「一个日线源都没有」原先仍报「所有源均失败 → 」，把配置缺口说成源故障；
   改为「没有能取日线的源」并列出被跳过的源。

变异电池九条全红（恒终端、恒瞬态、去掉 causes 守卫、忽略 no_answer、`any` 代
`all`、cause 回退成包装异常、actions 不置 no_answer、日线不收集 causes、汇率恒终端）。

四条闸：目标用例 **55 passed**、`ruff check` 全绿、分层 **384 passed**、离线
**2762 passed / 14 deselected**。

---

### Task 4: 到期计算（`due.py`）

**Files:**
- Create: `src/sams/services/scheduler/__init__.py`
- Create: `src/sams/services/scheduler/due.py`
- Create: `tests/support_scheduler.py`
- Test: `tests/services/test_scheduler_due.py`

- [ ] **Step 1: 写 `FakeCalendar` 与失败用例**

`tests/support_scheduler.py`（第一段；后面的任务往同一个文件追加）：

```python
"""调度器用例共用的世界：一个不看网络的日历。

日历只按「工作日开市 + 各市场本地的常规收盘时点」回答，因此到期用例不必等真实
日历源，也不会因为某个市场的真实节假日而改口径。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sams.core.errors import CalendarSourceError
from sams.core.timeutil import CalendarDay, SessionWindow

#: 与真实日历提供者的常规时段一致：A 股 15:00，港股与美股 16:00（各自本地时区）
CLOSE = {"A": time(15, 0), "HK": time(16, 0), "US": time(16, 0)}


class FakeCalendar:
    """`CalendarService` 的最小替身：到期计算只用到这三个方法。"""

    def __init__(self, settings, *, closed=None, next_unknown=()) -> None:
        self.settings = settings
        self.closed = {market: set(days) for market, days in (closed or {}).items()}
        self.next_unknown = set(next_unknown)

    def is_trading_day(self, market: str, day: date) -> bool:
        return day.weekday() < 5 and day not in self.closed.get(market, set())

    def post_close_run_at(
        self, market: str, day: date, offset_min: int, *, extended=()
    ) -> datetime:
        zone = ZoneInfo(self.settings.market_cfg(market).tz)
        close = datetime.combine(day, CLOSE[market], tzinfo=zone)
        return (close + timedelta(minutes=offset_min)).astimezone(UTC)

    def next_trading_day(self, market: str, after: date, *, horizon_days: int = 30) -> date:
        if market in self.next_unknown:
            raise CalendarSourceError(f"{market} 在 {after} 之后找不到交易日")
        cursor = after + timedelta(days=1)
        for _ in range(horizon_days):
            if self.is_trading_day(market, cursor):
                return cursor
            cursor += timedelta(days=1)
        raise CalendarSourceError(f"{market} 在 {after} 之后找不到交易日")


class WeekdayProvider:
    """`CalendarProvider` 替身：一整年铺工作日，给真 `CalendarService` 用。"""

    source = "stub"

    def __init__(self, market: str, tz: str) -> None:
        self.market = market
        self.tz = tz
        self.typical_windows = (SessionWindow("regular", "09:30", "15:00"),)

    def days_for_year(self, year: int) -> list[CalendarDay]:
        cursor = date(year, 1, 1)
        days = []
        while cursor.year == year:
            if cursor.weekday() < 5:
                days.append(
                    CalendarDay(self.market, cursor, True, False, self.tz, self.typical_windows)
                )
            cursor += timedelta(days=1)
        return days
```

`tests/services/test_scheduler_due.py`：

```python
from __future__ import annotations

from datetime import UTC, date, datetime

from support_scheduler import FakeCalendar

from sams.services.scheduler.due import (
    daily_due,
    fx_due,
    monthly_due,
    period_key,
    weekly_due,
)

#: 2024-10-25 是周五；A 股 15:00 收盘，港股 16:00，美股 16:00 纽约时（EDT）。
FRIDAY = date(2024, 10, 25)


def test_each_market_uses_its_own_close(settings):
    cal = FakeCalendar(settings)
    # 07:30 UTC = A 股 15:30（收盘 + 30 已过）、港股 15:30（其收盘 16:30 还没过）、
    # 美股 03:30（还没开）
    now = datetime(2024, 10, 25, 7, 30, tzinfo=UTC)
    assert daily_due(cal, settings, "A", now).as_of == FRIDAY
    assert daily_due(cal, settings, "HK", now).as_of == date(2024, 10, 24)
    assert daily_due(cal, settings, "US", now).as_of == date(2024, 10, 24)


def test_daily_due_waits_for_the_post_close_offset(settings):
    cal = FakeCalendar(settings)
    # A 股 15:00 收盘（07:00 UTC），配置的 30 分钟还没过 ⇒ 最新可跑的是周四
    now = datetime(2024, 10, 25, 7, 0, tzinfo=UTC)
    assert daily_due(cal, settings, "A", now).as_of == date(2024, 10, 24)
    now = datetime(2024, 10, 25, 7, 30, tzinfo=UTC)
    assert daily_due(cal, settings, "A", now).as_of == FRIDAY


def test_weekend_looks_back_to_the_last_open_day(settings):
    cal = FakeCalendar(settings)
    saturday = datetime(2024, 10, 26, 12, 0, tzinfo=UTC)
    due = daily_due(cal, settings, "A", saturday)
    assert due.period_key == "2024-10-25" and due.as_of == FRIDAY


def test_weekly_due_fires_on_the_last_trading_day_of_the_iso_week(settings):
    cal = FakeCalendar(settings)
    thursday = datetime(2024, 10, 24, 23, 0, tzinfo=UTC)
    # 周四收盘后，最近一个「本周最后交易日」还是上周五 10/18（W42）
    assert weekly_due(cal, settings, "A", thursday).period_key == "2024-W42"
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    due = weekly_due(cal, settings, "A", friday)
    assert due.period_key == "2024-W43" and due.as_of == FRIDAY


def test_weekly_due_skips_a_closed_friday(settings):
    cal = FakeCalendar(settings, closed={"A": {FRIDAY}})
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    # 周五休市 ⇒ 本周最后交易日是周四 10/24，也属于 W43
    assert weekly_due(cal, settings, "A", friday).period_key == "2024-W43"
    assert weekly_due(cal, settings, "A", friday).as_of == date(2024, 10, 24)


def test_monthly_due_is_the_daily_due_day_in_that_month(settings):
    cal = FakeCalendar(settings)
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    due = monthly_due(cal, settings, "A", friday)
    assert due.period_key == "2024-10" and due.as_of == FRIDAY


def test_weekly_does_not_conclude_when_the_next_day_is_unknown(settings):
    """下一交易日不可知 ⇒ 一周都不能宣布结束：这一轮不跑，而不是提前跑或认错周。"""
    cal = FakeCalendar(settings, next_unknown=("A",))
    friday = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    assert weekly_due(cal, settings, "A", friday) is None


def test_fx_waits_until_every_open_market_has_closed(settings):
    cal = FakeCalendar(settings)
    # 周五 08:00 UTC：A 已收盘，港股的盘后时点（16:30 本地）还没到，美股还在周五盘中
    early = datetime(2024, 10, 25, 8, 0, tzinfo=UTC)
    assert fx_due(cal, settings, early) is None
    # 周五 23:00 UTC：US 周五收盘 + 60 分钟也已过
    late = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    assert fx_due(cal, settings, late).as_of == FRIDAY


def test_fx_does_not_wait_for_a_market_that_is_closed(settings):
    cal = FakeCalendar(settings, closed={"A": {FRIDAY}, "HK": {FRIDAY}})
    late = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
    # 美股周五仍开市且已收盘；A/HK 当天休市不参与等待
    assert fx_due(cal, settings, late).as_of == FRIDAY


def test_period_key_follows_the_job_period():
    assert period_key("daily", FRIDAY) == "2024-10-25"
    assert period_key("weekly", FRIDAY) == "2024-W43"
    assert period_key("monthly", FRIDAY) == "2024-10"
    assert period_key("fx", FRIDAY) == "2024-10-25"
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_due.py -q
```

Expected: FAIL（`sams.services.scheduler.due` 不存在）。

- [ ] **Step 3: 实现**

`src/sams/services/scheduler/__init__.py` 先只写包说明与空的 `__all__`：

```python
"""调度器：「现在该跑什么」的计算部分。

到期计算在 `due.py`。
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
```

`src/sams/services/scheduler/due.py`：

```python
"""「现在该跑哪个周期」——只做日期算术，日历的回答从哪里来由 `CalendarView` 决定。

三个决定落在这里，因为它们都是**一行代码就能写错、而且错了不报错**的地方：

* 到期判断以**市场本地**的交易日与其收盘时点为准（D99）：同一瞬间，北京和纽约
  不是同一个「今天」，锚定日必须按市场换算，收盘是否已过也要按市场的时区与
  偏移去问 `CalendarView`。
* 周期键不是「今天」（D98）：周五的任务可能周六补跑，周任务横跨两天，月任务跨月。
* 汇率没有市场维度，但它的到期日要等**当天开市的市场都收盘**（D108）：
  周五晚上用周五的汇率，不能拿美股周四的收盘价去换算。

`CalendarView` 只声明本模块用到的三个方法；`CalendarService` 恰好都实现了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sams.core.config import Settings
from sams.core.errors import CalendarSourceError

logger = logging.getLogger("sams.services.scheduler.due")

DAILY, WEEKLY, MONTHLY, FX = "daily", "weekly", "monthly", "fx"
PERIODS = (DAILY, WEEKLY, MONTHLY, FX)

#: 最近到期日的回看窗口：停机十天之内重启，仍能找到一个到期日、照常触发；
#: 再长的停机不自动认领——一个月的链在一个 tick 里跑完会打满所有源，要不要补由人决定
CATCHUP_DAYS = 10
#: 周任务往前找「本周最后一个交易日」的跨度：三个整周足够覆盖长假
WEEK_SCAN_DAYS = 21


class CalendarView(Protocol):
    def is_trading_day(self, market: str, day: date) -> bool: ...

    def post_close_run_at(
        self, market: str, day: date, offset_min: int, *, extended=()
    ) -> datetime: ...

    def next_trading_day(self, market: str, after: date, *, horizon_days: int = 30) -> date: ...


@dataclass(frozen=True)
class Due:
    period_key: str
    as_of: date


def _local_today(settings: Settings, market: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(settings.market_cfg(market).tz)).date()


def _passed(view: CalendarView, settings: Settings, market: str, day: date, now: datetime) -> bool:
    offset = settings.market_cfg(market).post_close_min
    return view.post_close_run_at(market, day, offset) <= now


def daily_due(
    view: CalendarView, settings: Settings, market: str, now: datetime
) -> Due | None:
    """最近一个「已经收盘且过了盘后延迟」的交易日；窗口内没有这样的日子就返回 None。"""
    today = _local_today(settings, market, now)
    for back in range(CATCHUP_DAYS + 1):
        day = today - timedelta(days=back)
        if view.is_trading_day(market, day) and _passed(view, settings, market, day, now):
            return Due(day.isoformat(), day)
    return None


def weekly_due(
    view: CalendarView, settings: Settings, market: str, now: datetime
) -> Due | None:
    """最近一个「下一个交易日已进入下一 ISO 周」的交易日（D109）。

    判据是**下一个交易日**而不是「周五」：周五休市时，周四就是本周最后一个交易日。
    """
    today = _local_today(settings, market, now)
    for back in range(WEEK_SCAN_DAYS + 1):
        day = today - timedelta(days=back)
        if not (view.is_trading_day(market, day) and _passed(view, settings, market, day, now)):
            continue
        try:
            following = view.next_trading_day(market, day)
        except CalendarSourceError as exc:
            # 找不到下一个交易日就不敢替这一周下「已经结束」的结论：跳过这一天，
            # 继续往前找；真的一直找不到，这一轮 weekly 就不跑，而不是提前跑
            logger.warning("%s %s 的下一交易日不可知：%s", market, day, exc)
            continue
        if following.isocalendar()[:2] == day.isocalendar()[:2]:
            continue
        iso = day.isocalendar()
        return Due(f"{iso.year}-W{iso.week:02d}", day)
    return None


def monthly_due(
    view: CalendarView, settings: Settings, market: str, now: datetime
) -> Due | None:
    """日历按月刷新：第一次有到期日的交易日触发，之后同一个月由周期键去重。"""
    day = daily_due(view, settings, market, now)
    if day is None:
        return None
    return Due(day.as_of.strftime("%Y-%m"), day.as_of)


def fx_due(view: CalendarView, settings: Settings, now: datetime) -> Due | None:
    """当天开市的市场都已收盘的最近一天；一个市场都不开市时不跑（D108）。"""
    latest: dict[str, date] = {}
    for market in settings.markets:
        due = daily_due(view, settings, market, now)
        if due is not None:
            latest[market] = due.as_of
    if not latest:
        return None
    newest = max(latest.values())
    for market in settings.markets:
        if view.is_trading_day(market, newest) and latest.get(market) != newest:
            # 这个市场那天开市、但它的收盘还没过：等它
            return None
    return Due(newest.isoformat(), newest)


def period_key(period: str, as_of: date) -> str:
    """手工指定 `as_of` 时用的周期键；形状必须与到期计算写出来的一致（D98）。"""
    if period == WEEKLY:
        iso = as_of.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == MONTHLY:
        return as_of.strftime("%Y-%m")
    return as_of.isoformat()


def due_for(
    period: str,
    view: CalendarView,
    settings: Settings,
    *,
    market: str | None,
    now: datetime,
) -> Due | None:
    """一个 job 的到期日。全局 job 只有汇率一类（D108）。"""
    if period == FX:
        return fx_due(view, settings, now)
    if market is None:
        return None
    if period == WEEKLY:
        return weekly_due(view, settings, market, now)
    if period == MONTHLY:
        return monthly_due(view, settings, market, now)
    if period == DAILY:
        return daily_due(view, settings, market, now)
    raise ValueError(f"未知周期 {period!r}：只认 {list(PERIODS)}")
```

- [ ] **Step 4: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_due.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/services/scheduler/__init__.py src/sams/services/scheduler/due.py tests/support_scheduler.py tests/services/test_scheduler_due.py
git commit -m "feat(scheduler): 到期计算按市场本地收盘与周期键（D99/D108/D109）"
```

**评审收尾（2026-10-22）**：实现落在 `7dd3642`，两路评审之后修在 `a3467ce`。

1. 任务书自身有矛盾：`FakeCalendar.closed` 是无市场维度的集合，而
   `test_fx_does_not_wait_for_a_market_that_is_closed` 要求「A/HK 休市、美股开市」。
   实现者把它改成 `{market: {days}}`，这是必要偏差，测试代码段已同步。
2. 假日历原先对所有市场统一 15:00 收盘——港股/美股真实常规收盘是 16:00
   （`calendar_providers.py:135-136`），统一时点让「每个市场用自己的收盘」这条断言
   只剩美股有区分度。改成按市场（A 15:00、HK/US 16:00），测试代码段已同步。
3. D96 三处过头话：`__init__.py` 引用不存在的 `service.py` 与「Task 10」计划标签；
   `due.py` 自称「不读库、不发请求」（`CalendarService.day()` 恰恰读库）；
   `CATCHUP_DAYS` 注释声称停机三天「按各自的周期键补上」（`daily_due` 只返回最近一个
   到期日）。均已改为只说得到的事实。模块头「用 UTC 日期判断，美股每天都会错一天」
   经推演也不成立——回看窗口会吸收 UTC 与本地的日界差，改成了只陈述
   「同一瞬间北京和纽约不是同一个今天、锚定日按市场换算」这句可验证的事实。
4. `weekly_due` 的「下一交易日不可知 ⇒ 不跑」分支原先无用例钉住（删掉 try/except
   全套仍绿），补 `next_unknown` 与该用例；`FakeCalendar.windows` 无读者，删除。
   `WeekdayProvider` 保留——本计划 Task 8/13 消费它。
5. 评审对 `WEEK_SCAN_DAYS` 长假场景的推演（连续休市 >21 个自然日）在 A/HK/US
   不存在（最长春节连休约 10 天），且失败方向是「那一周不跑」而非「跑错周」，
   与 D109 的保守侧一致，不改。

变异电池：统一收盘时点、去掉不可知保护、`CATCHUP_DAYS=0` 三条全红。
四条闸：目标用例 **10 passed**、`ruff check` 全绿、分层 **390 passed**、离线
**2778 passed / 14 deselected**。

### Task 5: 对象与币种对（`universe.py`）

**Files:**
- Create: `src/sams/services/scheduler/universe.py`
- Test: `tests/services/test_scheduler_universe.py`

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_universe.py`：

```python
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sams.core.errors import ValidationError
from sams.services.rules.snapshot import Target
from sams.services.scheduler.universe import UniverseBuilder
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo


class StubData:
    """只实现 `expand` 的 `RuleData` 替身：本用例验的是并集，不是展开规则本身。"""

    def __init__(self, watchlists: WatchlistRepo) -> None:
        self.watchlists = watchlists

    def expand(self, rule) -> list[Target]:
        if rule.target_type == "watchlist":
            return [
                Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)
                for market, code in self.watchlists.codes(rule.user_id)
            ]
        market, _, code = rule.target_ref.partition("/")
        return [Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)]


def test_universe_is_the_union_of_watchlist_positions_rules_and_live_theses(db, settings):
    users = UserRepo(db)
    alice = users.create("alice", "correct horse battery")
    # bob 只有自选、没有账户/规则/论点：删掉「自选」这一路时只有他会从并集里消失
    bob = users.create("bob", "correct horse battery")
    watchlists = WatchlistRepo(db)
    watchlists.add(alice.id, "A", "600519")
    watchlists.add(bob.id, "A", "601318")

    accounts = SimAccountRepo(db)
    account = accounts.open(alice.id, "主账户", report_currency="CNY")
    for currency, amount in (("CNY", 100_000.0), ("HKD", 100_000.0), ("USD", 1_000.0)):
        accounts.adjust_cash(account.id, currency, amount, reason="入金", actor_id=alice.id)
    # 存过又清零的币种不该产生换汇对：`if amount` 判据钉在这里
    accounts.adjust_cash(account.id, "EUR", 100.0, reason="入金", actor_id=alice.id)
    accounts.adjust_cash(account.id, "EUR", -100.0, reason="出金", actor_id=alice.id)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    orders.record_fill(
        account_id=account.id,
        user_id=alice.id,
        market="HK",
        code="00700",
        side="buy",
        qty=100,
        price=300.0,
        currency="HKD",
    )
    # 买断再卖光：库里留下 qty=0 的历史行，它不该继续把这只票拉进采集对象
    orders.record_fill(
        account_id=account.id,
        user_id=alice.id,
        market="HK",
        code="00998",
        side="buy",
        qty=100,
        price=20.0,
        currency="HKD",
    )
    orders.record_fill(
        account_id=account.id,
        user_id=alice.id,
        market="HK",
        code="00998",
        side="sell",
        qty=100,
        price=21.0,
        currency="HKD",
    )

    rules = RuleRepo(db)
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="自选止损",
        target_type="watchlist",
        target_ref="*",
        params={"price": 10.0},
    )
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="美股",
        target_type="symbol",
        target_ref="US/AAPL",
        params={"price": 10.0},
    )
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="停用的",
        target_type="symbol",
        target_ref="A/600036",
        params={"price": 10.0},
        enabled=False,
    )

    theses = ThesisRepo(db)
    live = theses.create(
        user_id=alice.id,
        market="A",
        code="000001",
        style="value",
        actor_id=alice.id,
        now=datetime(2024, 10, 1, tzinfo=UTC),
    )
    opened = theses.create(
        user_id=alice.id,
        market="A",
        code="000003",
        style="value",
        actor_id=alice.id,
        now=datetime(2024, 10, 1, tzinfo=UTC),
    )
    theses.create(
        user_id=alice.id,
        market="A",
        code="000002",
        style="value",
        actor_id=alice.id,
        now=datetime(2024, 10, 1, tzinfo=UTC),
    )
    # 新建只能从 observing/proposed 开始，在跟的两态靠 UPDATE 直接落库
    db.execute("UPDATE theses SET status = 'tracking' WHERE id = ?", [live.id])
    db.execute("UPDATE theses SET status = 'opened' WHERE id = ?", [opened.id])

    universe = UniverseBuilder(
        users=users,
        watchlists=watchlists,
        accounts=accounts,
        orders=orders,
        rules=rules,
        theses=theses,
        data=StubData(watchlists),
        settings=settings,
    ).build()

    assert universe.codes("A") == ("000001", "000003", "600519", "601318")
    assert universe.codes("HK") == ("00700",)
    assert universe.codes("US") == ("AAPL",)
    assert universe.fx_pairs == (("HKD", "CNY"), ("USD", "CNY"))
    # 大小写与空格不算语义：查询侧也归一，否则 "hk" 查出来是空
    assert universe.codes("hk") == ("00700",)


def test_one_rule_that_cannot_expand_does_not_stop_the_others(db, settings, caplog):
    """展开失败的规则只跳过自己，其余对象照常进入采集。

    替身按 `RuleData.expand` 的失败契约抛 `ValidationError`（真展开器只在规则自身
    形状不成时抛，见 `snapshot.py` 的展开分支），并不断言这个形状能从 `RuleRepo`
    建出来——仓库的校验正好把它挡在外面。
    """

    users = UserRepo(db)
    alice = users.create("alice", "correct horse battery")
    rules = RuleRepo(db)
    broken = rules.create(
        alice.id,
        kind="stop.fixed",
        name="形状不成",
        target_type="symbol",
        target_ref="A/600001",
        params={"price": 10.0},
    )
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="美股",
        target_type="symbol",
        target_ref="US/AAPL",
        params={"price": 10.0},
    )

    class _Expanding:
        def expand(self, rule):
            if rule.id == broken.id:
                raise ValidationError("market 不能为空：A / HK / US")
            market, _, code = rule.target_ref.partition("/")
            return [Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)]

    accounts = SimAccountRepo(db)
    with caplog.at_level(logging.WARNING, logger="sams.services.scheduler.universe"):
        universe = UniverseBuilder(
            users=users,
            watchlists=WatchlistRepo(db),
            accounts=accounts,
            orders=SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db)),
            rules=rules,
            theses=ThesisRepo(db),
            data=_Expanding(),
            settings=settings,
        ).build()

    assert universe.codes("A") == ()
    assert universe.codes("US") == ("AAPL",)
    # 这一只票之外，配置里的其余市场一个对象都没有，仍各占一格（空元组而不是缺格）
    assert set(universe.by_market) == set(settings.markets)
    assert f"规则 {broken.id} 展开失败" in caplog.text
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_universe.py -q
```

Expected: FAIL（`sams.services.scheduler.universe` 不存在）。

- [ ] **Step 3: 实现**

`src/sams/services/scheduler/universe.py`：

```python
"""一条盘后链该覆盖哪些对象、该换哪些币种对。

并集而不是「按用户分开跑」：采集是公共资源，同一只票被两个人自选，没有理由拉两次。
对象来源有四种：自选股、持仓、启用的规则目标、在跟的（opened/tracking）观点。
停用的规则与观察中的观点不拉数据：它们不会产生任何读写，拉回来的数没人用。

**用户状态不过滤**：这是数据采集，不是投递。今天停用的用户明天启用，库里该已经有数。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sams.core.codes import canonical_code, canonical_market
from sams.core.config import Settings
from sams.core.errors import ValidationError
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.theses import OPENED, TRACKING, ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

if TYPE_CHECKING:
    from sams.services.rules.snapshot import RuleData

logger = logging.getLogger("sams.services.scheduler.universe")


@dataclass(frozen=True)
class Universe:
    #: market → 去重并排好序的代码；配置里的每个市场都有一格（可能是空元组）
    by_market: Mapping[str, tuple[str, ...]]
    #: (原币, 账户报告币)，只留两者不同的对
    fx_pairs: tuple[tuple[str, str], ...]

    def codes(self, market: str) -> tuple[str, ...]:
        return self.by_market.get(canonical_market(market), ())


class UniverseBuilder:
    def __init__(
        self,
        *,
        users: UserRepo,
        watchlists: WatchlistRepo,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        rules: RuleRepo,
        theses: ThesisRepo,
        data: RuleData,
        settings: Settings,
    ) -> None:
        self.users = users
        self.watchlists = watchlists
        self.accounts = accounts
        self.orders = orders
        self.rules = rules
        self.theses = theses
        self.data = data
        self.settings = settings

    def build(self) -> Universe:
        found: set[tuple[str, str]] = set()
        pairs: set[tuple[str, str]] = set()
        for item in self.watchlists.list_all():
            found.add(_key(item.market, item.code))

        for user in self.users.list_users():
            for account in self.accounts.list_for(user.id):
                held = [row for row in self.orders.positions(account.id) if row.qty > 0]
                found.update(_key(row.market, row.code) for row in held)
                currencies = {row.currency for row in held}
                currencies |= {
                    currency
                    for currency, amount in self.accounts.cash_balances(account.id).items()
                    if amount
                }
                for currency in currencies:
                    if currency and currency != account.report_currency:
                        pairs.add((currency, account.report_currency))

            for rule in self.rules.list_for(user.id, enabled_only=True):
                try:
                    targets = self.data.expand(rule)
                except ValidationError as exc:
                    # 跳过这一条，继续收其余对象：`RuleData.expand` 只在规则自身的形状
                    # 不成时抛 ValidationError（持仓类没绑账户、target_type 不认、
                    # target_ref 的市场段或代码段为空），坏一条不该让整晚的采集停摆
                    logger.warning("规则 %s 展开失败，跳过：%s", rule.id, exc)
                    continue
                found.update(_key(target.market, target.code) for target in targets)

            for status in (OPENED, TRACKING):
                for thesis in self.theses.list(user.id, status=status):
                    found.add(_key(thesis.market, thesis.code))

        markets = set(self.settings.markets) | {market for market, _ in found}
        by_market = {
            market: tuple(sorted(code for found_market, code in found if found_market == market))
            for market in sorted(markets)
        }
        return Universe(by_market=by_market, fx_pairs=tuple(sorted(pairs)))


def _key(market: str, code: str) -> tuple[str, str]:
    normalized = canonical_market(market)
    return normalized, canonical_code(normalized, code)
```

- [ ] **Step 4: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_universe.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/services/scheduler/universe.py tests/services/test_scheduler_universe.py
git commit -m "feat(scheduler): 盘后链的对象并集与币种对（四种来源）"
```

**评审收尾（2026-10-22）**：实现落在 `5d6b2f4`，两路评审之后修在 `e124ba9`（可证性）
与 `6ce4b97`（D96 措辞）。评审发现的问题与处理：

- **四条来源逐个可证**（`e124ba9`）。原来只有一条并集断言，删掉任何一路都不会红：
  现在 bob 只有自选（钉住「自选」这一路）、`000003` 是 opened 态论点（钉住 `OPENED`
  与 `TRACKING` 是两个状态而不是一个）、`00998` 买断再卖光留下 `qty=0` 的历史行
  （钉住 `qty > 0`）、EUR 入金 100 又出金 100 留下零余额行（钉住 `if amount`）。
  变异清单：删自选循环 / 只认 TRACKING / 去掉 `qty > 0` / 去掉 `if amount` /
  `enabled_only=True` 改成 False / 去掉 try-except / 把 warning 降成 debug——七次全红。
- **展开失败那一支补用例**。它原来只有注释，没有任何东西能证伪；现在断言「坏规则
  只跳过自己、其余对象照常进入」与那条 warning 的文本。删掉 try-except 或把 warning
  改掉都会红。
- **`size` / `empty` 删掉**（同 commit）：全仓没有读者，`Universe` 现在只有 `codes()`
  与 `fx_pairs` 两个出口。
- **D96 四处过头话**（本轮）：
  1. 模块 docstring 原来写「对象来源只有四种（spec §10 的输入侧）」——spec §10 是
     规则结构/节流/模板，没有列过对象来源；唯一的并集表述在 §7.3，而它说的是盘中
     快照的三种来源（自选 ∪ 启用规则标的 ∪ 被引用的指数），和这里的四种不是一回事。
     引用删掉，「只有」也删掉：四种是本文件可数的，不需要外部背书。
  2. 「停用的规则与观察中的观点不拉数据」原来接了一句「拉回来只会让健康页的
     「同步了几只」比「规则看了几只」大」——那两个标签此刻还不存在（健康页是 Task 11），
     改成直接陈述后果。
  3. try-except 的注释原来写「一条引用**已删对象**的规则不该让整晚的采集停摆」，
     而 `RuleData.expand`（`snapshot.py:156-195`）根本不查对象是否存在：thesis 读不到
     返回 `[]`，symbol 只是把 `target_ref` 切开。它真正抛 `ValidationError` 的三处是
     持仓类没绑账户、`target_type` 不认、市场段或代码段为空。注释与用例的 docstring
     都按这三处重写，用例里明说「不声称这个形状能从 `RuleRepo` 建出来」。
  4. `due.py` 里三处描述派发而非计算的措辞：`fx_due` 的「一个市场都不开市时不跑」
     （`if not latest` 的实际含义是「还没有任何市场拿到到期日」，周末两者并不等价）、
     `monthly_due` 的「第一次有到期日的交易日触发…去重」、`CATCHUP_DAYS` 与
     `weekly_due` 里的「照常触发」「这一轮 weekly 就不跑」。本包里还没有派发路径，
     一律改成「答不答得出到期日」。决策表 D108 行同步（「至少一个市场开市才跑」→
     「还没有任何市场拿到到期日就不跑」）。
- **补两个能红的断言**：`codes("hk")` 走归一（把 `codes()` 里的 `canonical_market` 删掉
  会红）、`set(by_market) == set(settings.markets)`（把配置市场并入 `markets` 的那半行
  删掉会红——只有第二条用例里存在「一个对象都没有的市场」，所以放在它那里）。

### Task 6: job 的形状与五个数据 job（`types.py` + `jobs.py`）

**Files:**
- Create: `src/sams/services/scheduler/types.py`
- Create: `src/sams/services/scheduler/jobs.py`
- Modify: `src/sams/core/timeutil.py`（加 `HISTORY_START`）
- Modify: `src/sams/cli/sync.py`（两个默认值改为别名）
- Test: `tests/services/test_scheduler_jobs_data.py`

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_jobs_data.py`：

```python
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars
from support_fundamentals import StubFundamentalsAdapter
from support_valuation import financial_rows, valuation

from sams.core.errors import RateLimited
from sams.datasource.registry import AdapterRegistry
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.scheduler.jobs import (
    build_jobs,
    run_actions,
    run_bars,
    run_financials,
    run_valuation,
)
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.services.sync import SyncService
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.symbols import SymbolRepo

AS_OF = date(2024, 10, 25)


class RecordingBarsAdapter:
    name = "akshare"

    def __init__(self, *, frame: pl.DataFrame | None = None, fail_on: str | None = None) -> None:
        self.frame = frame
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, date, date]] = []

    def daily_bars(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        if code == self.fail_on:
            raise RateLimited(self.name, "429")
        if self.frame is None:
            return pl.DataFrame()
        return self.frame.filter(
            (pl.col("market") == market)
            & (pl.col("code") == code)
            & pl.col("date").is_between(start, end)
        )


def deps(db, settings, *, bars=None, finance=None):
    bars = bars or RecordingBarsAdapter()
    finance = finance or StubFundamentalsAdapter(["A"])
    finance.name = "eastmoney"
    registry = AdapterRegistry(
        adapters={"akshare": bars, "eastmoney": finance},
        market_sources={"A": ("akshare", "eastmoney"), "HK": ("akshare",)},
        sleep=lambda _: None,
    )
    symbols = SymbolRepo(db)
    return JobDeps(
        settings=settings,
        registry=registry,
        universe=None,  # type: ignore[arg-type]  —— 本文件的用例自己造 Universe
        sync=SyncService(registry, BarRepo(db), symbols, settings.markets),
        corp_actions=CorpActionSyncService(
            registry, CorpActionRepo(db), CorpActionsCoverageRepo(db)
        ),
        fundamentals=FundamentalSyncService(
            registry,
            FinancialRepo(db),
            ValuationRepo(db),
            FundingFlowRepo(db),
            symbols,
        ),
        fx=None,  # type: ignore[arg-type]
        calendar=None,  # type: ignore[arg-type]
        rules=None,  # type: ignore[arg-type]
        recheck=None,  # type: ignore[arg-type]
        users=None,  # type: ignore[arg-type]
        clock=FrozenClock(datetime(2024, 10, 25, 15, tzinfo=UTC)),
    )


def ctx(*codes: str) -> JobContext:
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market="A",
        universe=Universe(by_market={"A": codes}, fx_pairs=()),
    )


def test_bars_job_is_incremental(db, settings):
    BarRepo(db).upsert(
        sample_bars(start=date(2024, 10, 24)).filter(pl.col("date") == date(2024, 10, 24))
    )
    adapter = RecordingBarsAdapter(
        frame=sample_bars(start=date(2024, 10, 25)).filter(pl.col("date") == date(2024, 10, 25))
    )
    outcome = run_bars(deps(db, settings, bars=adapter), ctx("600519"))
    assert adapter.calls == [("A", "600519", date(2024, 10, 25), AS_OF)]
    assert outcome.rows_affected == 1
    assert BarRepo(db).latest_date("A", "600519") == AS_OF


def test_bars_job_backfills_when_the_code_is_new(db, settings):
    adapter = RecordingBarsAdapter(frame=sample_bars())
    run_bars(deps(db, settings, bars=adapter), ctx("600519"))
    _, _, start, end = adapter.calls[0]
    assert end == AS_OF and start == AS_OF - timedelta(days=730)


def test_one_bad_code_does_not_drop_the_rest(db, settings):
    adapter = RecordingBarsAdapter(frame=sample_bars(), fail_on="600519")
    outcome = run_bars(deps(db, settings, bars=adapter), ctx("600519", "601088"))
    assert len(outcome.errors) == 1 and "600519" in outcome.errors[0]
    assert outcome.rows_affected == 1
    assert "1/2" in outcome.note


def test_empty_universe_is_a_successful_noop(db, settings):
    outcome = run_bars(deps(db, settings), ctx())
    assert outcome.errors == () and outcome.rows_affected == 0


def test_financials_job_writes_what_the_chain_answers(db, settings):
    finance = StubFundamentalsAdapter(["A"], financials=financial_rows())
    outcome = run_financials(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.errors == ()
    assert outcome.rows_affected == 2
    assert db.row_count("financials") == 2
    assert finance.calls == ["financials"]


def test_valuation_job_uses_the_latest_stored_day(db, settings):
    ValuationRepo(db).upsert(valuation([date(2024, 10, 24)]))
    finance = StubFundamentalsAdapter(
        ["A"], valuation=valuation([date(2024, 10, 24), AS_OF])
    )
    outcome = run_valuation(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.rows_affected == 2
    assert ValuationRepo(db).latest_date("A", "601088") == AS_OF


def test_actions_job_skips_a_fully_covered_code(db, settings):
    CorpActionsCoverageRepo(db).record(
        "A",
        "601088",
        covered_from=date(1990, 1, 1),
        covered_to=AS_OF,
        full_history=True,
        src="akshare",
    )
    adapter = RecordingBarsAdapter()
    outcome = run_actions(deps(db, settings, bars=adapter), ctx("601088"))
    assert outcome.rows_affected == 0
    assert db.row_count("corp_actions") == 0


def test_build_jobs_registers_only_markets_with_capability(db, settings):
    only = replace(
        settings, markets={k: v for k, v in settings.markets.items() if k in ("A", "HK")}
    )
    jobs = build_jobs(deps(db, only))
    registered = {(job.name, job.market) for job in jobs}
    assert ("bars", "A") in registered and ("bars", "HK") in registered
    assert ("financials", "A") in registered
    assert ("financials", "HK") not in registered
    assert not any(job.name == "actions" for job in jobs)


def test_build_jobs_skips_a_market_without_any_source(db, settings):
    """库里一个启用源都没有的市场：装配已经警告过，注册表这里不许抛。

    否则「某个市场暂时没源」会让整个常驻调度器起不来，连其余两个市场也不跑。
    """
    built = deps(db, settings)
    built.registry.market_sources = {"A": ("akshare", "eastmoney")}
    jobs = build_jobs(built)
    assert ("bars", "A") in {(job.name, job.market) for job in jobs}
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_jobs_data.py -q
```

Expected: FAIL（`sams.services.scheduler.types` / `jobs` 不存在）。

- [ ] **Step 3: 移动 `HISTORY_START`**

`src/sams/core/timeutil.py` 的 import 之后加：

```python
#: 全历史类数据（除权、财务）的起点：沪深交易所 1990/1991 年才成立，
#: 再早不存在可查的 A 股记录。job 与命令行都从这里取，不各写一个 1990
HISTORY_START = date(1990, 1, 1)
```

`src/sams/cli/sync.py` 的 import 块：删掉 `from datetime import date`（改完这个模块不再直接用
`date`，留着会被 ruff 的 F401 打红），加 `from sams.core.timeutil import HISTORY_START`。

第 22–23 行（那条注释与 `DEFAULT_ACTIONS_START = date(1990, 1, 1)`）替换为：

```python
#: 除权记录的默认起点：与调度 job 共用同一个常量，免得两处各写一个 1990
DEFAULT_ACTIONS_START = HISTORY_START
```

下面已有的 `DEFAULT_FUNDAMENTALS_START = DEFAULT_ACTIONS_START` 原样保留（不要再写一遍）。

- [ ] **Step 4: 写 `types.py`**

`src/sams/services/scheduler/types.py`：

```python
"""调度器的四种数据形状：job 吃到什么、吐出什么、怎么登记、靠什么活。

`JobOutcome.errors` 不是异常：一个 job 覆盖很多只票，其中三只取不到不该让
「另外七十只已经入库」这件事消失。runner 见到 `errors` 非空就记 `failed`，
但行数与 note 照常留下——健康页要能同时看到「写了 70 行」和「3 只失败」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sams.core.clock import Clock
from sams.core.config import Settings
from sams.datasource.registry import AdapterRegistry
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.fx import FxSyncService
from sams.services.rules.engine import RuleEngine
from sams.services.rules.recheck import Rechecker
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
from sams.services.scheduler.universe import Universe, UniverseBuilder
from sams.services.sync import SyncService
from sams.storage.repos.users import UserRepo

PERIODS = (DAILY, WEEKLY, MONTHLY, FX)


@dataclass(frozen=True)
class JobContext:
    period_key: str
    as_of: date
    #: 市场 job 一定非空；全局 job（汇率）是 None
    market: str | None
    universe: Universe


@dataclass(frozen=True)
class JobOutcome:
    rows_affected: int = 0
    note: str = ""
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class JobSpec:
    name: str
    period: str
    market: str | None
    run: Callable[[JobContext], JobOutcome]

    @property
    def title(self) -> str:
        return f"{self.name}@{self.market or '全局'}"

    @property
    def component(self) -> str:
        """系统告警里的组件名：去重按它（D102），所以它必须跨进程稳定。"""
        return f"job.{self.name}.{self.market or 'global'}"


@dataclass(frozen=True)
class JobDeps:
    settings: Settings
    registry: AdapterRegistry
    universe: UniverseBuilder
    sync: SyncService
    corp_actions: CorpActionSyncService
    fundamentals: FundamentalSyncService
    fx: FxSyncService
    calendar: CalendarService
    rules: RuleEngine
    recheck: Rechecker
    users: UserRepo
    clock: Clock
```

- [ ] **Step 5: 写 `jobs.py` 的五个数据 job 与注册**

`src/sams/services/scheduler/jobs.py`：

```python
"""九个 job 的功能本体：只把已有服务拼成一条链，不决定自己何时跑。

每个 job 覆盖一个市场的一批对象，**按只隔离**：一只票的链断了，剩下的照常入库，
断的那几只进 `JobOutcome.errors`，由 runner 记成 `failed` 并在 10 分钟后重试。
不按只隔离的后果是「一只停牌票让整个市场今晚没有数据」，而它看起来像一次正常失败。

窗口规则：

* 日线/按日估值/资金流：库里有最新一天就从下一天起，没有就回补 730 天；
* 公司行为：除非已有「全历史且覆盖到今天」的确认，否则从 1990 年起；
* 财务：每次都从 1990 年起——报告期数据是重述的，增量窗口会漏掉旧期的修正。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from functools import partial

from sams.core.errors import DatasourceError, SamsError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.types import (
    DAILY,
    JobContext,
    JobDeps,
    JobOutcome,
    JobSpec,
)

logger = logging.getLogger("sams.services.scheduler.jobs")

#: 首次见到一只票时回补多久：两年足够覆盖 PR 分位的样本地板（250 个交易日）
INITIAL_SYNC_DAYS = 730


def _window(latest: date | None, as_of: date) -> date | None:
    start = latest + timedelta(days=1) if latest else as_of - timedelta(days=INITIAL_SYNC_DAYS)
    return None if start > as_of else start


def _units(ctx: JobContext) -> list[tuple[str, str]]:
    assert ctx.market is not None  # 数据 job 只在市场 job 上登记
    return [(ctx.market, code) for code in ctx.universe.codes(ctx.market)]


def _collect(
    units: list[tuple[str, str]], action, label: str
) -> JobOutcome:
    rows = 0
    done = 0
    errors: list[str] = []
    for market, code in units:
        try:
            rows += action(market, code)
        except SamsError as exc:
            # 只接本项目的失败：一个编程错误（AttributeError 之类）必须冒到 runner，
            # 否则它会被误记成「这只票取不到」，然后在每一轮重试里重复发生
            errors.append(f"{market} {code}: {exc}")
        else:
            done += 1
    note = f"{done}/{len(units)} 只{label}"
    if errors:
        note += f"，{len(errors)} 只失败"
    return JobOutcome(rows_affected=rows, note=note, errors=tuple(errors))


def run_bars(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.sync.bars.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.sync.sync_daily(market, code, start, ctx.as_of).written

    return _collect(_units(ctx), one, "日线")


def run_actions(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        covered = deps.corp_actions.coverage.for_code(market, code)
        if any(
            row.full_history and row.covered_to is not None and row.covered_to >= ctx.as_of
            for row in covered
        ):
            # 全历史源已经确认到今天：再拉一遍只会得到同一批行
            return 0
        return deps.corp_actions.sync_actions(
            market, code, HISTORY_START, ctx.as_of
        ).written

    return _collect(_units(ctx), one, "公司行为")


def run_financials(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        return deps.fundamentals.sync_financials(
            market, code, HISTORY_START, ctx.as_of
        ).written

    return _collect(_units(ctx), one, "财务")


def run_valuation(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.fundamentals.valuation.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.fundamentals.sync_valuation(market, code, start, ctx.as_of).written

    return _collect(_units(ctx), one, "按日估值")


def run_flow(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.fundamentals.flow.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.fundamentals.sync_flow(market, code, start, ctx.as_of).written

    return _collect(_units(ctx), one, "资金流")


#: 数据 job 的登记表：能力名（= 适配器方法名）→ job 名与实现。
#: 缺能力的市场不登记（D106）：HK/US 没有按报告期的财务源，每天一条失败会把真故障淹掉
_DATA_JOBS = (
    ("daily_bars", "bars", run_bars),
    ("corp_actions", "actions", run_actions),
    ("financials", "financials", run_financials),
    ("valuation", "valuation", run_valuation),
    ("funding_flow", "flow", run_flow),
)


def _has(deps: JobDeps, market: str, method: str) -> bool:
    """这个市场有没有这个能力。

    整个市场在库里没有任何启用的源时 `sources_for` 会抛 `DatasourceError`；
    「整块没配」与「缺这一个能力」对注册表来说是同一个答案：不登记这个 job。
    启动不该因为一个市场没配源就整个失败——健康页的数据源那一节才是它该出声的地方。
    """
    try:
        return bool(deps.registry.chain_for(market, method))
    except DatasourceError:
        return False


def build_jobs(deps: JobDeps) -> tuple[JobSpec, ...]:
    jobs: list[JobSpec] = []
    for market in sorted(deps.settings.markets):
        for method, name, run in _DATA_JOBS:
            if _has(deps, market, method):
                jobs.append(JobSpec(name, DAILY, market, partial(run, deps)))
    return tuple(jobs)
```

- [ ] **Step 6: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_jobs_data.py tests/cli/test_sync_fx_cli.py -q
```

Expected: PASS（后者确认 `DEFAULT_*_START` 别名没有改掉命令行的默认区间）。

- [ ] **Step 7: 提交**

```bash
git add src/sams/services/scheduler/types.py src/sams/services/scheduler/jobs.py src/sams/core/timeutil.py src/sams/cli/sync.py tests/services/test_scheduler_jobs_data.py
git commit -m "feat(scheduler): job 形状与五个数据 job（增量窗口 + 按只隔离）"
```

### Task 7: 四个运维 job + 引擎/复检的市场过滤

**Files:**
- Modify: `src/sams/services/scheduler/jobs.py`（追加 fx/calendar/rules/recheck 与最终 `build_jobs`）
- Modify: `src/sams/services/rules/engine.py`（`check(..., market=None)`）
- Modify: `src/sams/services/rules/recheck.py`（`run(..., market=None)`）
- Modify: `src/sams/storage/repos/fx.py`（`latest_date`）
- Test: `tests/services/test_scheduler_jobs_ops.py`
- Test: `tests/services/test_rules_engine.py`
- Test: `tests/services/test_rules_recheck.py`
- Test: `tests/storage/test_fx_repo.py`

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_jobs_ops.py`：

```python
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from support import FrozenClock
from support_rules import FixedClock
from support_scheduler import WeekdayProvider

from sams.datasource.registry import AdapterRegistry
from sams.services.calendar import CalendarService
from sams.services.fx import FxSyncService
from sams.services.scheduler.jobs import (
    build_jobs,
    run_calendar,
    run_fx,
    run_recheck,
    run_rules,
)
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.users import UserRepo

AS_OF = date(2024, 10, 25)


class StubFxAdapter:
    name = "stubfx"

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, str, date, date]] = []

    def fx_rate(self, base, quote, start, end):
        self.calls.append((base, quote, start, end))
        return self.frame.filter(
            (pl.col("base") == base) & (pl.col("quote") == quote)
        )


def fx_frame(day: date = AS_OF) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [day],
            "base": ["USD"],
            "quote": ["CNY"],
            "rate": [7.1],
            "source": ["stubfx"],
        }
    )


def deps(db, settings, *, fx=None, calendar=None, rules=None, recheck=None, users=None):
    return JobDeps(
        settings=settings,
        registry=AdapterRegistry(
            adapters={}, market_sources={}, sleep=lambda _: None
        ),
        universe=None,  # type: ignore[arg-type]
        sync=None,  # type: ignore[arg-type]
        corp_actions=None,  # type: ignore[arg-type]
        fundamentals=None,  # type: ignore[arg-type]
        fx=fx,
        calendar=calendar,
        rules=rules,
        recheck=recheck,
        users=users or UserRepo(db),
        clock=FrozenClock(datetime(2024, 10, 25, 15, tzinfo=UTC)),
    )


def ctx(*, market="A", pairs=()):
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market=market,
        universe=Universe(by_market={market: ()}, fx_pairs=pairs),
    )


def fx_service(db, adapter):
    registry = AdapterRegistry(
        adapters={"stubfx": adapter}, market_sources={}, sleep=lambda _: None
    )
    return FxSyncService(registry, FxRepo(db))


def test_fx_job_starts_after_the_last_stored_day(db, settings):
    FxRepo(db).put(date(2024, 10, 24), "USD", "CNY", 7.0, source="manual")
    adapter = StubFxAdapter(fx_frame())
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    assert adapter.calls == [("USD", "CNY", date(2024, 10, 25), AS_OF)]
    assert outcome.rows_affected == 1
    assert FxRepo(db).latest_date("USD", "CNY") == AS_OF


def test_fx_job_skips_a_pair_already_at_as_of(db, settings):
    FxRepo(db).put(AS_OF, "USD", "CNY", 7.1, source="manual")
    adapter = StubFxAdapter(fx_frame())
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    assert adapter.calls == []
    assert outcome.rows_affected == 0 and outcome.errors == ()


def test_calendar_job_refreshes_december_next_year_too(db, settings):
    provider = WeekdayProvider("A", "Asia/Shanghai")
    calendar = CalendarService(
        CalendarRepo(db),
        {"A": provider},
        clock=FrozenClock(datetime(2024, 12, 20, 15, tzinfo=UTC)),
    )
    ctx_december = JobContext(
        period_key="2024-12",
        as_of=date(2024, 12, 20),
        market="A",
        universe=Universe(by_market={"A": ()}, fx_pairs=()),
    )
    outcome = run_calendar(deps(db, settings, calendar=calendar), ctx_december)
    assert outcome.errors == ()
    assert CalendarRepo(db).year_covered("A", 2024)
    assert CalendarRepo(db).year_covered("A", 2025)


class RecordingRules:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date | None, str | None]] = []

    def check(self, user_id, *, as_of=None, market=None, dry_run=False, rule_id=None):
        self.calls.append((user_id, as_of, market))
        return SimpleNamespace(raised=(1, 2), suppressed=(3,), skipped=())


class RecordingRecheck:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date | None, str | None]] = []

    def run(self, user_id, *, as_of=None, dry_run=False, thesis_id=None, market=None):
        self.calls.append((user_id, as_of, market))
        return SimpleNamespace(rows=(1, 2, 3))


def test_rules_job_asks_for_that_market_only(db, settings):
    users = UserRepo(db)
    users.create("alice", "correct horse battery")
    recorder = RecordingRules()
    outcome = run_rules(deps(db, settings, rules=recorder, users=users), ctx())
    assert recorder.calls == [(users.list_users()[0].id, AS_OF, "A")]
    assert outcome.rows_affected == 2
    assert "触发 2" in outcome.note


def test_recheck_job_asks_for_that_market_only(db, settings):
    users = UserRepo(db)
    users.create("alice", "correct horse battery")
    recorder = RecordingRecheck()
    outcome = run_recheck(deps(db, settings, recheck=recorder, users=users), ctx())
    assert recorder.calls == [(users.list_users()[0].id, AS_OF, "A")]
    assert outcome.rows_affected == 3


def test_build_jobs_puts_recheck_before_rules_and_fx_after_every_market(db, settings):
    jobs = build_jobs(
        deps(
            db,
            settings,
            fx=SimpleNamespace(),
            calendar=SimpleNamespace(),
            rules=SimpleNamespace(),
            recheck=SimpleNamespace(),
        )
    )
    titles = [job.title for job in jobs]
    assert titles.index("recheck@A") < titles.index("rules@A")
    assert titles.index("rules@US") < titles.index("fx@全局")
    assert titles.index("fx@全局") < titles.index("calendar@A")
```

`tests/services/test_rules_engine.py` 追加：

```python
def test_market_filter_leaves_other_markets_alone(h) -> None:
    h.add()
    report = h.engine().check(USER, market="HK")
    assert report.rules == 1
    assert report.raised == () and h.sender.sent == []
    assert h.engine().check(USER, market="A").raised
```

`tests/services/test_rules_recheck.py` 追加：

```python
def test_market_filter_skips_theses_from_other_markets(container) -> None:
    thesis = open_thesis(container)
    rec = build(container)
    assert rec.run(USER, as_of=AS_OF, market="HK").rows == ()
    assert len(rec.run(USER, as_of=AS_OF, market="A").rows) == 1
    with pytest.raises(ValidationError, match="不是 HK"):
        rec.run(USER, as_of=AS_OF, thesis_id=thesis.id, market="HK")
```

`tests/storage/test_fx_repo.py` 追加：

```python
def test_latest_date_ignores_staleness(db):
    repo = FxRepo(db)
    assert repo.latest_date("USD", "CNY") is None
    repo.put(date(2024, 1, 2), "USD", "CNY", 7.1, source="manual")
    assert repo.latest_date("USD", "CNY") == date(2024, 1, 2)
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_jobs_ops.py tests/services/test_rules_engine.py tests/services/test_rules_recheck.py tests/storage/test_fx_repo.py -q
```

Expected: FAIL（四个 job 不存在；`market=` 不被接受）。

- [ ] **Step 3: 实现 `FxRepo.latest_date`**

`src/sams/storage/repos/fx.py` 在 `latest` 下方加：

```python
    def latest_date(self, base: str, quote: str) -> date | None:
        """这一对已经存到哪一天，**不看陈旧度**：job 的增量窗口问的是「还缺哪段」，
        不是「这个数还能不能用」——后者是 `latest(..., max_age_days=)` 的问题。"""
        value = self.db.scalar(
            "SELECT max(date) FROM fx_rates WHERE base_ccy = ? AND quote_ccy = ?",
            [base, quote],
        )
        if value is None:
            return None
        return value.date() if isinstance(value, datetime) else value
```

- [ ] **Step 4: 实现四个运维 job 与最终注册表**

`src/sams/services/scheduler/jobs.py` 在 `run_flow` 之后追加：

```python
def run_fx(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """汇率没有市场维度：对象来自账户的币种对，窗口按每对各自的库内最新日。"""
    pairs = ctx.universe.fx_pairs
    rows = 0
    done = 0
    errors: list[str] = []
    for base, quote in pairs:
        try:
            start = _window(deps.fx.fx.latest_date(base, quote), ctx.as_of)
            if start is not None:
                rows += deps.fx.sync(base, quote, start, ctx.as_of).written
        except SamsError as exc:
            errors.append(f"{base}→{quote}: {exc}")
        else:
            done += 1
    note = f"{done}/{len(pairs)} 对汇率"
    if errors:
        note += f"，{len(errors)} 对失败"
    return JobOutcome(rows, note, tuple(errors))


def run_calendar(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """当月刷新一次；12 月顺手把下一年铺好（D109）。"""
    assert ctx.market is not None
    years = [ctx.as_of.year]
    if ctx.as_of.month == 12:
        years.append(ctx.as_of.year + 1)
    rows = 0
    try:
        for year in years:
            rows += deps.calendar.refresh(ctx.market, year)
    except SamsError as exc:
        return JobOutcome(rows, f"刷新 {'/'.join(map(str, years))} 年失败", (str(exc),))
    return JobOutcome(rows, f"刷新 {'/'.join(map(str, years))} 年日历")


def run_rules(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """只评估这个市场的对象；每用户一趟，每用户失败互不影响。"""
    users = deps.users.list_users()
    raised = suppressed = skipped = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.rules.check(user.id, as_of=ctx.as_of, market=ctx.market)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        raised += len(report.raised)
        suppressed += len(report.suppressed)
        skipped += len(report.skipped)
    note = f"{len(users)} 个用户：触发 {raised}、冷却 {suppressed}、跳过 {skipped}"
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(raised, note, tuple(errors))


def run_recheck(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    users = deps.users.list_users()
    rows = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.recheck.run(user.id, as_of=ctx.as_of, market=ctx.market)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        rows += len(report.rows)
    note = f"{len(users)} 个用户复检 {rows} 条观点"
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(rows, note, tuple(errors))
```

`build_jobs` 整体替换为：

```python
def build_jobs(deps: JobDeps) -> tuple[JobSpec, ...]:
    """注册顺序就是 tick 的执行顺序：
    每个市场：数据链 → 周复检 → 规则；然后汇率；最后日历。

    周复检排在规则之前：同一晚观点漂移了，当晚的规则评估就该读到新的基线；
    反过来则要等到下一周，而「周复检之后价格又跌破止损」这一晚没有任何人被告知。
    """
    jobs: list[JobSpec] = []
    for market in sorted(deps.settings.markets):
        for method, name, run in _DATA_JOBS:
            if deps.registry.chain_for(market, method):
                jobs.append(JobSpec(name, DAILY, market, partial(run, deps)))
        jobs.append(JobSpec("recheck", WEEKLY, market, partial(run_recheck, deps)))
        jobs.append(JobSpec("rules", DAILY, market, partial(run_rules, deps)))
    jobs.append(JobSpec("fx", FX, None, partial(run_fx, deps)))
    for market in sorted(deps.settings.markets):
        jobs.append(JobSpec("calendar", MONTHLY, market, partial(run_calendar, deps)))
    return tuple(jobs)
```

`types.py` 的 import 行相应改为：

```python
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
```

- [ ] **Step 5: 市场过滤（引擎与复检）**

`src/sams/services/rules/engine.py` 的 `check` 签名加 `market: str | None = None`，docstring 补一句
「按市场过滤的对象级子集：调度器每个市场单独跑一趟，过滤在展开之后做」；
在 `targets = self.data.expand(rule)` 的 `except` 块之后插：

```python
            if market is not None:
                targets = [target for target in targets if target.market == market]
                if not targets:
                    continue
```

`rules=len(selected)` 保持不变：这个数回答的是「这一趟看了几条规则」，不是「看了几个对象」。

`src/sams/services/rules/recheck.py` 的 `run` 签名加 `market: str | None = None`，
`_select(user_id, thesis_id, market)`：

```python
    def _select(self, user_id: str, thesis_id: str | None, market: str | None) -> list[Thesis]:
        if thesis_id is not None:
            thesis = self.theses.require(user_id, thesis_id)
            if market is not None and thesis.market != market:
                raise ValidationError(
                    f"{thesis.id} 属于 {thesis.market}，不是 {market}："
                    "点名一条观点又限定另一个市场，是把两次请求拼成了一次"
                )
            if thesis.status not in RECHECK_STATUSES:
                allowed = "/".join(RECHECK_STATUSES)
                raise ValidationError(
                    f"{thesis.id} 现在是 {thesis.status}，不在复检范围内（只比 {allowed}）："
                    "没建过仓的论没有「建仓时那一眼」可比，静默返回零行会被读成复检过了"
                )
            return [thesis]
        found = list(self.theses.list(user_id, status=OPENED))
        found += list(self.theses.list(user_id, status=TRACKING))
        if market is not None:
            found = [thesis for thesis in found if thesis.market == market]
        return found
```

- [ ] **Step 6: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_jobs_ops.py tests/services/test_rules_engine.py tests/services/test_rules_recheck.py tests/storage/test_fx_repo.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/sams/services/scheduler/jobs.py src/sams/services/rules/engine.py src/sams/services/rules/recheck.py src/sams/storage/repos/fx.py tests/services/test_scheduler_jobs_ops.py tests/services/test_rules_engine.py tests/services/test_rules_recheck.py tests/storage/test_fx_repo.py
git commit -m "feat(scheduler): 汇率/日历/规则/复检四个 job 与市场过滤"
```

---

### Task 8: `JobRunner`——一个 job 的一次执行怎么记账

**Files:**
- Create: `src/sams/services/scheduler/runner.py`
- Test: `tests/services/test_scheduler_runner.py`

三件事只能有一个地方决定（D105）：本周期跑过没有、上一轮留下的行还算不算数、
失败之后多久准重来。写成 runner 而不是写进九个 job，是因为这九个 job 的失败形状
本该一样；写进 job 的话，某一个 job 忘了看 `period_key`，它就会每 tick 跑一遍。

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_runner.py`：

```python
"""`JobRunner`：记账、重试间隔、连续失败的告警（D103/D105）。

时钟全部由 `FrozenClock` 推进：这些规则说的是「多久之后」，不是「过一会儿」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sams.core.errors import TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.services.scheduler.due import Due
from sams.services.scheduler.runner import (
    RETRY_GAP_MIN,
    SKIPPED,
    STALE_RUNNING_H,
    JobRunner,
)
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRunRepo
from support import FrozenClock

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
UNIVERSE = Universe(by_market={}, fx_pairs=())
DUE = Due(period_key="2024-10-25", as_of=date(2024, 10, 25))


def harness(db, run, *, market="A"):
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    bus = EventBus()
    alarms: list[SystemAlarm] = []
    bus.subscribe(SystemAlarm, alarms.append)
    runner = JobRunner(runs=runs, bus=bus, clock=clock)
    spec = JobSpec(name="bars", period="daily", market=market, run=run)
    return runner, runs, clock, alarms, spec


def test_success_writes_ok_with_rows_and_note(db):
    seen = []

    def job(ctx):
        seen.append(ctx)
        return JobOutcome(rows_affected=3, note="3 只")

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.status == OK
    assert result.rows_affected == 3
    row = runs.latest_for_period("bars", "A", "2024-10-25")
    assert (row.status, row.rows_affected, row.note) == (OK, 3, "3 只")
    assert seen[0].period_key == "2024-10-25"
    assert seen[0].as_of == date(2024, 10, 25)
    assert seen[0].market == "A"


def test_a_successful_period_is_not_rerun(db):
    calls = []

    def job(ctx):
        calls.append(ctx.period_key)
        return JobOutcome()

    runner, runs, _, _, spec = harness(db, job)
    first = runner.execute(spec, DUE, UNIVERSE)
    second = runner.execute(spec, DUE, UNIVERSE)

    assert first.status == OK
    assert second.status == SKIPPED
    assert "已经成功" in second.skip_reason
    assert second.run_id == first.run_id
    assert calls == ["2024-10-25"]
    assert runs.count() == 1


def test_a_failure_is_retried_only_after_the_gap(db):
    calls = []

    def job(ctx):
        calls.append(1)
        raise TransientError("akshare", "boom")

    runner, runs, clock, _, spec = harness(db, job)
    first = runner.execute(spec, DUE, UNIVERSE)
    assert first.status == FAILED
    assert "boom" in first.error
    assert runs.latest_for_period("bars", "A", "2024-10-25").status == FAILED

    waiting = runner.execute(spec, DUE, UNIVERSE)
    assert waiting.status == SKIPPED
    assert f"不足 {RETRY_GAP_MIN} 分钟" in waiting.skip_reason
    assert len(calls) == 1

    clock.advance(minutes=RETRY_GAP_MIN)
    again = runner.execute(spec, DUE, UNIVERSE)
    assert again.status == FAILED
    assert len(calls) == 2


def test_a_crashed_running_row_is_replaced_only_after_the_limit(db):
    def job(ctx):
        return JobOutcome()

    runner, runs, clock, _, spec = harness(db, job)
    runs.start("bars", market="A", period_key="2024-10-25")

    clock.advance(hours=STALE_RUNNING_H - 1)
    blocked = runner.execute(spec, DUE, UNIVERSE)
    assert blocked.status == SKIPPED
    assert "还在跑" in blocked.skip_reason

    clock.advance(hours=1)
    fresh = runner.execute(spec, DUE, UNIVERSE)
    assert fresh.status == OK
    assert runs.count() == 2


def test_the_third_failed_period_alarms_once(db):
    def job(ctx):
        raise TransientError("akshare", "boom")

    runner, _, _, alarms, spec = harness(db, job)
    for period in ("2024-10-24", "2024-10-25", "2024-10-26"):
        result = runner.execute(
            spec, Due(period_key=period, as_of=date.fromisoformat(period)), UNIVERSE
        )
        assert result.status == FAILED

    assert len(alarms) == 1
    assert alarms[0].component == "job.bars.A"
    assert "连续 3 个周期失败" in alarms[0].message
    assert "boom" in alarms[0].message

    runner.execute(spec, Due(period_key="2024-10-27", as_of=date(2024, 10, 27)), UNIVERSE)
    assert len(alarms) == 1  # 第 4 个失败周期不再加一条（D103）


def test_retries_inside_one_period_do_not_count_as_periods(db):
    def job(ctx):
        raise TransientError("akshare", "boom")

    runner, _, clock, alarms, spec = harness(db, job)
    for _ in range(5):
        runner.execute(spec, DUE, UNIVERSE)
        clock.advance(minutes=RETRY_GAP_MIN)
    assert alarms == []  # 同一个周期的五次重试只算一个失败周期

    runner.execute(spec, Due(period_key="2024-10-26", as_of=date(2024, 10, 26)), UNIVERSE)
    assert alarms == []  # 两个周期

    runner.execute(spec, Due(period_key="2024-10-27", as_of=date(2024, 10, 27)), UNIVERSE)
    assert len(alarms) == 1  # 三个周期


def test_outcome_errors_mark_failed_and_keep_the_note(db):
    def job(ctx):
        return JobOutcome(rows_affected=1, note="1/2 只日线", errors=("600519: boom",))

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.status == FAILED
    assert "600519: boom" in result.error
    row = runs.latest_for_period("bars", "A", "2024-10-25")
    assert (row.status, row.rows_affected, row.note) == (FAILED, 1, "1/2 只日线")


def test_a_programming_error_is_recorded_not_raised(db):
    def job(ctx):
        raise TypeError("bad code")

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.status == FAILED
    assert "TypeError" in result.error
    assert runs.latest_for_period("bars", "A", "2024-10-25").status == FAILED


def test_global_jobs_and_market_jobs_keep_separate_ledgers(db):
    def job(ctx):
        return JobOutcome()

    runner, runs, _, _, spec = harness(db, job, market=None)
    runner.execute(spec, DUE, UNIVERSE)
    # `job_name` 相同、market 不同：八小时后的 A 股那一行不是「全局那条」的替身
    assert runs.latest_for_period("bars", None, DUE.period_key) is not None
    assert runs.latest_for_period("bars", "A", DUE.period_key) is None
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_runner.py -q
```

Expected: FAIL（`sams.services.scheduler.runner` 不存在）。

- [ ] **Step 3: 写实现**

`src/sams/services/scheduler/runner.py`：

```python
"""`task_runs` 的唯一写入方：一次 job 执行怎么开始、怎么收尾、什么时候准重来。

三条规则（D105）写在这里而不是 job 里：九个 job 的失败形状本该一样，
写进 job 的话，某一个忘了看 `period_key` 就会每 tick 跑一遍。
- 本周期成功过 → 不再跑（`period_key` 是「跑过没有」的唯一依据，D98）；
- 上一轮还在跑且还算新鲜 → 等它（过期的 `running` 是崩溃留下的行，可以重跑）；
- 上一次失败且还在重试间隔内 → 等一等（每 tick 都重拉不是重试，是打源）。
连续失败满三个**周期**才发一条 `SystemAlarm`（D103）：同一个周期里的重试
不是三个周期，否则一次网络抖动很快就能凑满。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.events import EventBus, SystemAlarm
from sams.services.scheduler.due import Due
from sams.services.scheduler.types import JobContext, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRun, TaskRunRepo

RETRY_GAP_MIN = 10
STALE_RUNNING_H = 6
FAILURES_FOR_ALARM = 3
#: `RunResult` 的第三种结论：这一轮没跑，而且**没有**写 `task_runs`
#: （「该不该跑」由到期计算与上面三条规则回答，不是一次执行）
SKIPPED = "skipped"

STREAK_WINDOW = 30


@dataclass(frozen=True)
class RunResult:
    job: str
    market: str | None
    period_key: str
    status: str
    rows_affected: int = 0
    note: str = ""
    error: str = ""
    skip_reason: str = ""
    run_id: str = ""


def failure_streak(runs: Sequence[TaskRun]) -> int:
    """从最近一行往回数：连续多少个**周期**失败。

    `runs` 必须是新到旧。同一个周期里的多次重试只算一次（D105 允许隔一段时间
    再试），否则一次网络抖动就能凑满三次告警。0018 之前写的老行没有周期键，
    一行就是一个周期——它们的时刻本来也没有别的用途。
    """
    seen: set[str] = set()
    streak = 0
    for run in runs:
        key = run.period_key or run.id
        if key in seen:
            continue
        seen.add(key)
        if run.status != FAILED:
            break
        streak += 1
    return streak


@dataclass(frozen=True)
class JobRunner:
    runs: TaskRunRepo
    bus: EventBus
    clock: Clock = SYSTEM_CLOCK
    retry_gap: timedelta = timedelta(minutes=RETRY_GAP_MIN)
    stale_after: timedelta = timedelta(hours=STALE_RUNNING_H)
    failures_for_alarm: int = FAILURES_FOR_ALARM

    def execute(self, spec: JobSpec, due: Due, universe: Universe) -> RunResult:
        now = self.clock.now_utc()
        previous = self.runs.latest_for_period(spec.name, spec.market, due.period_key)
        reason = self._skip_reason(previous, due, now) if previous is not None else ""
        if reason:
            return RunResult(
                job=spec.name,
                market=spec.market,
                period_key=due.period_key,
                status=SKIPPED,
                skip_reason=reason,
                run_id=previous.id if previous is not None else "",
            )
        run_id = self.runs.start(spec.name, market=spec.market, period_key=due.period_key)
        context = JobContext(
            period_key=due.period_key,
            as_of=due.as_of,
            market=spec.market,
            universe=universe,
        )
        try:
            outcome = spec.run(context)
        except Exception as exc:  # noqa: BLE001 — 隔离是这一层的职责：一次跑挂不能带走循环
            return self._failed(spec, due, run_id, f"{type(exc).__name__}: {exc}")
        if outcome.ok:
            self.runs.finish(
                run_id,
                status=OK,
                rows_affected=outcome.rows_affected,
                note=outcome.note or None,
            )
            return RunResult(
                job=spec.name,
                market=spec.market,
                period_key=due.period_key,
                status=OK,
                rows_affected=outcome.rows_affected,
                note=outcome.note,
                run_id=run_id,
            )
        return self._failed(
            spec,
            due,
            run_id,
            "；".join(outcome.errors),
            rows_affected=outcome.rows_affected,
            note=outcome.note,
        )

    def _skip_reason(self, previous: TaskRun, due: Due, now: datetime) -> str:
        if previous.status == OK:
            return f"周期 {due.period_key} 已经成功"
        if previous.status == RUNNING:
            age = now - previous.started_at
            if age < self.stale_after:
                return f"上一轮还在跑（已 {int(age.total_seconds() // 60)} 分钟）"
            return ""
        moment = previous.finished_at or previous.started_at
        waited = now - moment
        if waited < self.retry_gap:
            return (
                f"距上次失败 {int(waited.total_seconds() // 60)} 分钟，"
                f"不足 {int(self.retry_gap.total_seconds() // 60)} 分钟"
            )
        return ""

    def _failed(
        self,
        spec: JobSpec,
        due: Due,
        run_id: str,
        error: str,
        *,
        rows_affected: int = 0,
        note: str = "",
    ) -> RunResult:
        self.runs.finish(
            run_id,
            status=FAILED,
            rows_affected=rows_affected,
            error=error,
            note=note or None,
        )
        streak = failure_streak(
            self.runs.recent(spec.name, spec.market, limit=STREAK_WINDOW)
        )
        if streak == self.failures_for_alarm:
            self.bus.publish(
                SystemAlarm(
                    component=spec.component,
                    message=(
                        f"{spec.title} 连续 {streak} 个周期失败；"
                        f"最近一次（{due.period_key}）：{error}"
                    ),
                )
            )
        return RunResult(
            job=spec.name,
            market=spec.market,
            period_key=due.period_key,
            status=FAILED,
            rows_affected=rows_affected,
            note=note,
            error=error,
            run_id=run_id,
        )
```

- [ ] **Step 4: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_runner.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/sams/services/scheduler/runner.py tests/services/test_scheduler_runner.py
git commit -m "feat(scheduler): JobRunner 的周期记账、重试间隔与连续失败告警"
```

---

### Task 9: `SystemAlerter`——系统告警只到管理员，冷却跨重启

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`（`latest_open` 之后加 `since_system`）
- Create: `src/sams/services/scheduler/alarms.py`
- Test: `tests/services/test_scheduler_alarms.py`
- Test: `tests/storage/test_alerts_repo.py`（追加两条）

`SystemAlarm` 现在有三个发布方（`datasource/registry.py`、`services/calendar.py`、
Task 8 的 runner），订阅方只有 `attach_log_sink` 的日志。D102 要回答的是：
**发给谁**（`is_admin && active`）、**写什么形状**（`rule_id=NULL`、`market='SYS'`、
`code=component`）、**多久之内算同一次故障**（同组件同人一个 `cooldown_min`）。
去重读 `alerts` 的行，所以调度器重启之后计数不会从零开始。

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_alarms.py`：

```python
"""`SystemAlarm` → 管理员（D102）。

四条规则：只有 `is_admin && active` 收；形状是 `market='SYS'`、`code=component`、
`rule_id=NULL`；同组件同人一个冷却窗口只发一次；去重读库，重启之后仍是同一次故障。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import STRONG, AlertMessage
from sams.services.scheduler.alarms import SYS_LABEL, SYS_MARKET, SystemAlerter
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo
from support import FrozenClock

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
PASSWORD = "correct-horse-battery"


class PersistingSender:
    """`AlertSender` 的替身：像 `NotifyService.dispatch` 一样先落一行，再返回 id。

    去重读的是 `alerts` 的行；只记调用的假发送器会让冷却永远不成立，
    测试就退化成「两次调用只记一次」的同义反复。
    """

    def __init__(self, alerts: AlertRepo, *, fail_for: set[str] | None = None) -> None:
        self.alerts = alerts
        self.fail_for = fail_for or set()
        self.calls: list[tuple[str, AlertMessage, str | None]] = []

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id: str | None = None,
        account_id: str | None = None,
    ) -> str:
        if user_id in self.fail_for:
            raise RuntimeError("通道挂了")
        alert = self.alerts.create(
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
        )
        self.calls.append((user_id, message, rule_id))
        return alert.id


def harness(db, settings, *, fail_for=None):
    clock = FrozenClock(MOMENT)
    users = UserRepo(db)
    alerts = AlertRepo(db, clock=clock)
    sender = PersistingSender(alerts, fail_for=fail_for)
    alerter = SystemAlerter(
        alerts=alerts, users=users, sender=sender, settings=settings, clock=clock
    )
    bus = EventBus()
    alerter.attach(bus)
    return bus, sender, users, alerts, clock


def test_the_alarm_goes_to_active_admins_only(db, settings):
    bus, sender, users, _, _ = harness(db, settings)
    admin = users.create("admin", PASSWORD, is_admin=True)
    gone = users.create("admin-old", PASSWORD, is_admin=True)
    users.disable(gone.id)
    users.create("alice", PASSWORD)

    bus.publish(SystemAlarm(component="job.bars.A", message="boom"))

    assert [call[0] for call in sender.calls] == [admin.id]
    _, message, rule_id = sender.calls[0]
    assert message.market == SYS_MARKET
    assert message.code == "job.bars.A"
    assert message.severity == STRONG
    assert message.body == "boom"
    assert message.triggered_at == MOMENT
    assert rule_id is None


def test_the_alert_row_is_a_system_alert(db, settings):
    bus, _, users, alerts, _ = harness(db, settings)
    admin = users.create("admin", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="日历源全挂"))

    row = alerts.list_for(admin.id)[0]
    assert row.rule_id is None
    assert (row.market, row.code, row.stage) == (SYS_MARKET, "calendar", SYS_LABEL)
    assert row.severity == STRONG
    assert row.rule_name == "系统告警"
    assert row.payload["extra"]["body"] == "日历源全挂"


def test_the_same_component_is_silent_inside_the_cooldown(db, settings):
    bus, sender, users, _, clock = harness(db, settings)
    users.create("admin", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="第一次"))
    bus.publish(SystemAlarm(component="calendar", message="第二次"))
    assert len(sender.calls) == 1

    clock.advance(minutes=settings.cooldown_min + 1)
    bus.publish(SystemAlarm(component="calendar", message="第三次"))
    assert [call[1].body for call in sender.calls] == ["第一次", "第三次"]


def test_different_components_do_not_share_a_cooldown(db, settings):
    bus, sender, users, _, _ = harness(db, settings)
    users.create("admin", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="a"))
    bus.publish(SystemAlarm(component="datasource:akshare", message="b"))

    assert len(sender.calls) == 2


def test_dedupe_reads_the_database_not_memory(db, settings):
    bus, _, users, alerts, clock = harness(db, settings)
    admin = users.create("admin", PASSWORD, is_admin=True)
    bus.publish(SystemAlarm(component="calendar", message="第一次"))

    # 「重启」：同一份库上换一个 alerter 和一个干净的 sender
    second_sender = PersistingSender(alerts)
    restarted = SystemAlerter(
        alerts=alerts, users=users, sender=second_sender, settings=settings, clock=clock
    )
    restarted.handle(SystemAlarm(component="calendar", message="重启后又来一次"))

    assert second_sender.calls == []
    assert len(alerts.list_for(admin.id)) == 1


def test_a_failing_recipient_does_not_block_the_next(db, settings):
    users = UserRepo(db)
    blocked = users.create("admin-a", PASSWORD, is_admin=True)
    bus, sender, _, _, _ = harness(db, settings, fail_for={blocked.id})
    other = users.create("admin-b", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="boom"))

    assert [call[0] for call in sender.calls] == [other.id]
```

`tests/storage/test_alerts_repo.py` 末尾追加：

```python
def test_since_system_matches_one_admin_one_component(repo, seeded_user, other_user):
    system = {
        **CREATE_KW,
        "rule_id": None,
        "account_id": None,
        "market": "SYS",
        "code": "calendar",
    }
    repo.create(seeded_user, **system)

    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert repo.since_system(seeded_user, "calendar", since=since) is not None
    assert repo.since_system(other_user, "calendar", since=since) is None
    assert repo.since_system(seeded_user, "datasource:akshare", since=since) is None
    assert (
        repo.since_system(
            seeded_user, "calendar", since=datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
        )
        is None
    )


def test_since_system_does_not_mistake_a_stock_alert_for_a_component(repo, seeded_user):
    repo.create(seeded_user, **{**CREATE_KW, "code": "calendar"})

    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert repo.since_system(seeded_user, "calendar", since=since) is None
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_alarms.py tests/storage/test_alerts_repo.py -q
```

Expected: FAIL（`sams.services.scheduler.alarms` 不存在；`AlertRepo` 没有 `since_system`）。

- [ ] **Step 3: 写 `since_system`**

`src/sams/storage/repos/alerts.py` 的 `latest_open` 之后：

```python
    def since_system(
        self, user_id: str, component: str, *, since: datetime
    ) -> Alert | None:
        """这个人从 `since` 起有没有收到过关于这个组件的系统告警（D102 的去重键）。

        `code` 与股票代码共用一列，靠 `market = 'SYS'` 与 `rule_id IS NULL` 分开：
        一条恰好把 `code` 写成组件名的规则告警不能被当成冷却的起点。
        `triggered_at` 与 `since` 必须在同一条时间轴上：调用方传「现在 - 冷却」，
        这里不读 `clock`，否则窗口的宽度会变成两个时钟之差。
        """
        rows = self.db.query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts"
            " WHERE user_id = ? AND market = 'SYS' AND code = ? AND rule_id IS NULL"
            " AND triggered_at >= ? ORDER BY triggered_at DESC, id DESC LIMIT 1",
            [user_id, component, _moment(since)],
        )
        return self._to_alert(rows.to_dicts()[0]) if rows.height else None
```

- [ ] **Step 4: 写 `alarms.py`**

`src/sams/services/scheduler/alarms.py`：

```python
"""`SystemAlarm` → 管理员（D102）。

与用户告警共用 `alerts`/`notify_log` 两张表和同一条投递路径，
不一样的三件事都在这里：接收人（`is_admin && active`）、数据形状
（`rule_id=NULL`、`market='SYS'`、`code=component`）、去重（同组件同人一个冷却窗口）。
去重读的是库里的行而不是进程内的一份集合：调度器重启之后，同一次故障不应该再发一遍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import STRONG, AlertMessage, AlertSender
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import ACTIVE, UserRepo

logger = logging.getLogger("sams.scheduler.alarms")

SYS_MARKET = "SYS"
SYS_LABEL = "系统"
SYS_RULE_NAME = "系统告警"


@dataclass(frozen=True)
class SystemAlerter:
    alerts: AlertRepo
    users: UserRepo
    sender: AlertSender
    settings: Settings
    clock: Clock = SYSTEM_CLOCK

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(SystemAlarm, self.handle)

    def handle(self, alarm: SystemAlarm) -> None:
        now = self.clock.now_utc()
        since = now - timedelta(minutes=self.settings.cooldown_min)
        for user in self.users.list_users():
            if not user.is_admin or user.status != ACTIVE:
                continue
            if self.alerts.since_system(user.id, alarm.component, since=since) is not None:
                continue
            message = AlertMessage(
                market=SYS_MARKET,
                code=alarm.component,
                severity=STRONG,
                triggered_at=now,
                name=SYS_LABEL,
                account_name=SYS_LABEL,
                rule_name=SYS_RULE_NAME,
                value_text=alarm.component,
                threshold_text=f"冷却 {self.settings.cooldown_min} 分钟",
                stage=SYS_LABEL,
                body=alarm.message,
            )
            try:
                self.sender.dispatch(message, user_id=user.id, rule_id=None)
            except Exception as exc:  # noqa: BLE001 — 一个收件人失败不能挡住其余收件人
                logger.warning("系统告警发给 %s 失败：%s", user.id, exc)
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_alarms.py tests/storage/test_alerts_repo.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/alerts.py src/sams/services/scheduler/alarms.py tests/services/test_scheduler_alarms.py tests/storage/test_alerts_repo.py
git commit -m "feat(scheduler): 系统告警投递给在线管理员（冷却跨重启）"
```

---

### Task 10: `Scheduler`——tick 的顺序、心跳与常驻循环

**Files:**
- Create: `src/sams/services/scheduler/report.py`
- Create: `src/sams/services/scheduler/service.py`
- Test: `tests/services/test_scheduler_service.py`

一轮 tick 做四件事，顺序就是失败时的止损顺序：构建对象全集（一次）→ 给心跳打一拍
（只有 `run_forever` 认领过才打）→ 按注册顺序判断到期并交给 runner → 把结果原样交回。
单个 job 的异常在 runner 里已经落账；这里只隔离**到期计算**的异常——日历源坏了不能让
排在后面的市场也不跑。

心跳只由常驻循环认领（D100）：`sams run once` 是一次手工执行，不该留下一个让下一个
常驻进程白等五分钟的行。认领读的是心跳表，所以第二个常驻进程会在这里被拒。

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_service.py`：

```python
"""`Scheduler`：注册顺序、市场过滤、`as_of` 覆盖、心跳与常驻循环。

runner 用记录型替身：这一层要钉的是「谁在什么时候被交给 runner」，
执行本身的三条时间规则已经在 `test_scheduler_runner.py` 钉过。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sams.core.errors import CalendarSourceError, SchedulerBusy, StorageError, ValidationError
from sams.services.scheduler.report import HEARTBEAT_STALE_S, collect
from sams.services.scheduler.runner import RunResult
from sams.services.scheduler.service import Scheduler
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo
from support import FrozenClock
from support_scheduler import FakeCalendar

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
FRIDAY = date(2024, 10, 25)
UNIVERSE = Universe(by_market={}, fx_pairs=())
PASSWORD = "correct-horse-battery"


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str, date]] = []

    def execute(self, spec, due, universe):
        self.calls.append((spec.name, spec.market, due.period_key, due.as_of))
        return RunResult(
            job=spec.name,
            market=spec.market,
            period_key=due.period_key,
            status=OK,
        )


class StubUniverse:
    def __init__(self) -> None:
        self.builds = 0

    def build(self) -> Universe:
        self.builds += 1
        return UNIVERSE


class BrokenUniverse:
    def build(self) -> Universe:
        raise StorageError("db 挂了")


def _noop(ctx):
    return JobOutcome()


def job_spec(name, market, period="daily"):
    return JobSpec(name=name, period=period, market=market, run=_noop)


def make(
    db,
    settings,
    specs,
    runner,
    *,
    clock=None,
    sleep=None,
    universe=None,
    calendar=None,
):
    clock = clock or FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    runs = TaskRunRepo(db, clock=clock)
    scheduler = Scheduler(
        settings,
        specs,
        runner,
        runs,
        heartbeats,
        universe or StubUniverse(),
        UserRepo(db),
        calendar or FakeCalendar(settings),
        clock=clock,
        sleep=sleep or (lambda seconds: None),
    )
    return scheduler, heartbeats, runs


def test_jobs_run_in_registration_order(db, settings):
    runner = RecordingRunner()
    specs = (
        job_spec("bars", "A"),
        job_spec("recheck", "A", "weekly"),
        job_spec("rules", "A"),
        job_spec("fx", None, "fx"),
        job_spec("calendar", "A", "monthly"),
    )
    scheduler, _, _ = make(db, settings, specs, runner)

    report = scheduler.tick(as_of=FRIDAY)

    assert [call[0] for call in runner.calls] == ["bars", "recheck", "rules", "fx", "calendar"]
    assert len(report.results) == 5
    assert report.errors == () and report.waiting == ()
    assert report.failed == ()


def test_as_of_uses_the_period_key_of_its_week(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("recheck", "A", "weekly"),), runner)

    scheduler.tick(as_of=FRIDAY, only_job="recheck", only_market="A")

    assert runner.calls == [("recheck", "A", "2024-W43", FRIDAY)]


def test_the_market_filter_narrows_the_selection(db, settings):
    runner = RecordingRunner()
    specs = (job_spec("bars", "A"), job_spec("bars", "HK"), job_spec("calendar", "A", "monthly"))
    scheduler, _, _ = make(db, settings, specs, runner)

    scheduler.tick(as_of=FRIDAY, only_market="HK")

    assert runner.calls == [("bars", "HK", "2024-10-25", FRIDAY)]


def test_an_unmatched_filter_is_refused(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    with pytest.raises(ValidationError, match="没有匹配"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")
    with pytest.raises(ValidationError, match="没有匹配"):
        scheduler.tick(as_of=FRIDAY, only_market="US")


def test_a_broken_calendar_does_not_stop_the_next_market(db, settings):
    class BrokenCalendar(FakeCalendar):
        def is_trading_day(self, market, day):
            if market == "A":
                raise CalendarSourceError("日历源挂了")
            return super().is_trading_day(market, day)

    runner = RecordingRunner()
    specs = (job_spec("bars", "A"), job_spec("bars", "HK"))
    scheduler, _, _ = make(db, settings, specs, runner, calendar=BrokenCalendar(settings))

    report = scheduler.tick(now=MOMENT)

    assert len(report.errors) == 1
    assert "bars@A" in report.errors[0] and "日历源挂了" in report.errors[0]
    assert [call[1] for call in runner.calls] == ["HK"]


def test_fx_waits_until_every_market_has_closed(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("fx", None, "fx"),), runner)

    early = scheduler.tick(now=datetime(2024, 10, 25, 8, 0, tzinfo=UTC))
    assert early.results == ()
    assert early.waiting == ("fx@全局",)

    late = scheduler.tick(now=datetime(2024, 10, 25, 23, 0, tzinfo=UTC))
    assert late.waiting == ()
    assert runner.calls == [("fx", None, "2024-10-25", FRIDAY)]


def test_the_universe_is_built_once_per_tick(db, settings):
    runner = RecordingRunner()
    universe = StubUniverse()
    specs = (job_spec("bars", "A"), job_spec("rules", "A"))
    scheduler, _, _ = make(db, settings, specs, runner, universe=universe)

    scheduler.tick(as_of=FRIDAY)

    assert universe.builds == 1
    assert len(runner.calls) == 2


def test_a_manual_tick_does_not_leave_a_heartbeat(db, settings):
    runner = RecordingRunner()
    scheduler, heartbeats, _ = make(db, settings, (job_spec("bars", "A"),), runner)

    scheduler.tick(as_of=FRIDAY)

    assert heartbeats.latest() is None


def test_run_forever_beats_on_every_tick(db, settings):
    runner = RecordingRunner()
    clock = FrozenClock(MOMENT)
    reports = []

    def sleep(seconds):
        clock.advance(seconds=seconds)

    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), runner, clock=clock, sleep=sleep
    )

    ticks = scheduler.run_forever(max_ticks=2, on_tick=reports.append)

    assert ticks == 2
    assert len(reports) == 2
    beat = heartbeats.latest()
    assert beat.note == f"tick={settings.scheduler_tick_s}s"
    assert beat.beat_at == MOMENT + timedelta(seconds=settings.scheduler_tick_s)


def test_a_second_daemon_is_refused(db, settings):
    first, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    second, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    first.run_forever(max_ticks=1)

    with pytest.raises(SchedulerBusy, match="心跳"):
        second.run_forever(max_ticks=1)


def test_a_dead_heartbeat_does_not_block_a_new_daemon(db, settings):
    old_clock = FrozenClock(MOMENT - timedelta(minutes=6))
    old = HeartbeatRepo(db, clock=old_clock).start(note="tick=60s")
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner()
    )

    scheduler.run_forever(max_ticks=1)

    assert heartbeats.latest().id != old


def test_a_tick_level_failure_is_reported_and_the_loop_survives(db, settings):
    reports = []
    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        universe=BrokenUniverse(),
    )

    ticks = scheduler.run_forever(max_ticks=2, on_tick=reports.append)

    assert ticks == 2
    assert all("db 挂了" in report.errors[0] for report in reports)


def test_ctrl_c_stops_the_loop_cleanly(db, settings):
    def interrupt(seconds):
        raise KeyboardInterrupt

    scheduler, _, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), sleep=interrupt
    )
    reports = []

    ticks = scheduler.run_forever(on_tick=reports.append)

    assert ticks == 1
    assert len(reports) == 1


def test_schedule_data_counts_admins_and_reads_the_heartbeat(db, settings):
    clock = FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    runs = TaskRunRepo(db, clock=clock)
    users = UserRepo(db)
    users.create("admin", PASSWORD, is_admin=True)
    gone = users.create("admin-old", PASSWORD, is_admin=True)
    users.disable(gone.id)
    users.create("alice", PASSWORD)
    beat_id = heartbeats.start(note="tick=60s")
    clock.advance(seconds=120)

    data = collect(
        runs=runs,
        heartbeats=heartbeats,
        users=users,
        specs=(job_spec("bars", "A"),),
        clock=clock,
    )

    assert data.heartbeat.id == beat_id
    assert data.beat_age_s == 120
    assert data.admins == 1
    assert data.stale is False and data.never_started is False
    assert data.jobs[0].latest is None and data.jobs[0].streak == 0


def test_schedule_data_turns_stale_after_the_threshold(db, settings):
    clock = FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    heartbeats.start(note="tick=60s")
    clock.advance(seconds=HEARTBEAT_STALE_S + 1)

    data = collect(
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=heartbeats,
        users=UserRepo(db),
        specs=(),
        clock=clock,
    )

    assert data.stale is True


def test_schedule_data_without_a_heartbeat_is_not_stale(db):
    clock = FrozenClock(MOMENT)

    data = collect(
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(),
        clock=clock,
    )

    assert data.never_started is True
    assert data.stale is False
    assert data.beat_age_s is None


def test_schedule_data_reports_the_latest_run_and_streak(db):
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    for period in ("2024-10-24", "2024-10-25"):
        run_id = runs.start("bars", market="A", period_key=period)
        runs.finish(run_id, status=FAILED, error="boom")
        clock.advance(minutes=1)

    data = collect(
        runs=runs,
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(job_spec("bars", "A"), job_spec("fx", None, "fx")),
        clock=clock,
    )

    assert data.jobs[0].latest.period_key == "2024-10-25"
    assert data.jobs[0].streak == 2
    assert data.jobs[1].latest is None and data.jobs[1].streak == 0


def test_the_scheduler_reports_through_its_own_clock(db, settings):
    clock = FrozenClock(MOMENT)
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), clock=clock
    )
    heartbeats.start(note="tick=60s")

    data = scheduler.schedule_data()

    assert data.heartbeat is not None
    assert data.beat_age_s == 0
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_service.py -q
```

Expected: FAIL（`sams.services.scheduler.service` 不存在）。

- [ ] **Step 3: 写 `report.py`**

`src/sams/services/scheduler/report.py`：

```python
"""`sams health` 的 `[调度]` 一节：它还在不在跑、每个 job 上一轮跑成什么。

只读：这一层不写 `task_runs`、不碰心跳，也不替调度器判断「该不该跑」。
`streak` 用的是 runner 同一个函数——页面上数出来的连续失败必须与发不发
`SystemAlarm` 的口径是同一条，否则「页面显示三次失败但没告警」会查不出原因。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.services.scheduler.runner import STREAK_WINDOW, failure_streak
from sams.services.scheduler.types import JobSpec
from sams.storage.repos.task_runs import Heartbeat, HeartbeatRepo, TaskRun, TaskRunRepo
from sams.storage.repos.users import ACTIVE, UserRepo

#: 心跳超过这个秒数算「可能停了」。它同时是单实例守卫的阈值（D100/D101）：
#: 比一个 tick 长得多，又短到一次崩溃重启不用等太久。
HEARTBEAT_STALE_S = 300


@dataclass(frozen=True)
class JobHealth:
    name: str
    market: str | None
    period: str
    latest: TaskRun | None
    streak: int


@dataclass(frozen=True)
class ScheduleData:
    heartbeat: Heartbeat | None
    beat_age_s: int | None
    admins: int
    jobs: tuple[JobHealth, ...]

    @property
    def stale(self) -> bool:
        """有过心跳、但已经过期：D101 的「它可能死了」只有这一条判据。"""
        return self.beat_age_s is not None and self.beat_age_s > HEARTBEAT_STALE_S

    @property
    def never_started(self) -> bool:
        return self.heartbeat is None


def collect(
    *,
    runs: TaskRunRepo,
    heartbeats: HeartbeatRepo,
    users: UserRepo,
    specs: Sequence[JobSpec],
    clock: Clock = SYSTEM_CLOCK,
) -> ScheduleData:
    moment = clock.now_utc()
    beat = heartbeats.latest()
    age = None
    if beat is not None:
        age = max(0, int((moment - beat.beat_at).total_seconds()))
    admins = sum(1 for user in users.list_users() if user.is_admin and user.status == ACTIVE)
    jobs = []
    for spec in specs:
        recent = runs.recent(spec.name, spec.market, limit=STREAK_WINDOW)
        jobs.append(
            JobHealth(
                name=spec.name,
                market=spec.market,
                period=spec.period,
                latest=recent[0] if recent else None,
                streak=failure_streak(recent),
            )
        )
    return ScheduleData(heartbeat=beat, beat_age_s=age, admins=admins, jobs=tuple(jobs))
```

- [ ] **Step 4: 写 `service.py`**

`src/sams/services/scheduler/service.py`：

```python
"""`Scheduler`：tick 的顺序、心跳与常驻循环。

一轮 tick 的顺序就是失败时的止损顺序：
1. 构建这一轮的对象全集（一次 tick 只构建一次）；
2. 给心跳打一拍（只有 `run_forever` 认领过才打）；
3. 按注册顺序逐个 job 判断到期并交给 `JobRunner`；
4. 把结果原样交回——打印与退出码是 CLI 的事。

单个 job 的异常在 runner 里已经落账；这里只隔离**到期计算**的异常：
日历源坏了不能让排在后面的市场也不跑。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.errors import SamsError, SchedulerBusy, ValidationError
from sams.services.scheduler.due import CalendarView, Due, due_for, period_key
from sams.services.scheduler.report import HEARTBEAT_STALE_S, ScheduleData, collect
from sams.services.scheduler.runner import JobRunner, RunResult
from sams.services.scheduler.types import JobSpec
from sams.services.scheduler.universe import UniverseBuilder
from sams.storage.repos.task_runs import FAILED, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.scheduler")


@dataclass(frozen=True)
class TickReport:
    now: datetime
    results: tuple[RunResult, ...] = ()
    waiting: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[RunResult, ...]:
        return tuple(result for result in self.results if result.status == FAILED)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        jobs: Sequence[JobSpec],
        runner: JobRunner,
        runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        universe: UniverseBuilder,
        users: UserRepo,
        calendar: CalendarView,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.jobs = tuple(jobs)
        self.runner = runner
        self.runs = runs
        self.heartbeats = heartbeats
        self.universe = universe
        self.users = users
        self.calendar = calendar
        self.clock = clock
        self.sleep = sleep
        self._beat_id: str | None = None

    def tick(
        self,
        now: datetime | None = None,
        *,
        as_of: date | None = None,
        only_job: str | None = None,
        only_market: str | None = None,
    ) -> TickReport:
        moment = now or self.clock.now_utc()
        if self._beat_id is not None:
            self.heartbeats.beat(self._beat_id)
        selected = self._selected(only_job, only_market)
        universe = self.universe.build()
        results: list[RunResult] = []
        waiting: list[str] = []
        errors: list[str] = []
        for spec in selected:
            if as_of is not None:
                due = Due(period_key=period_key(spec.period, as_of), as_of=as_of)
            else:
                try:
                    due = due_for(
                        spec.period,
                        self.calendar,
                        self.settings,
                        market=spec.market,
                        now=moment,
                    )
                except SamsError as exc:
                    errors.append(f"{spec.title}: {exc}")
                    continue
                if due is None:
                    waiting.append(spec.title)
                    continue
            results.append(self.runner.execute(spec, due, universe))
        return TickReport(
            now=moment,
            results=tuple(results),
            waiting=tuple(waiting),
            errors=tuple(errors),
        )

    def run_forever(
        self,
        *,
        on_tick: Callable[[TickReport], None] | None = None,
        max_ticks: int | None = None,
    ) -> int:
        """认领心跳，然后循环 tick。返回实际跑过的 tick 数（测试与 `--once` 需要）。"""
        self._claim()
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                try:
                    report = self.tick()
                except SamsError as exc:
                    logger.warning("这一轮 tick 失败：%s", exc)
                    report = TickReport(now=self.clock.now_utc(), errors=(str(exc),))
                if on_tick is not None:
                    on_tick(report)
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                self.sleep(self.settings.scheduler_tick_s)
        except KeyboardInterrupt:
            logger.warning("收到中断，调度器退出")
        return ticks

    def schedule_data(self) -> ScheduleData:
        return collect(
            runs=self.runs,
            heartbeats=self.heartbeats,
            users=self.users,
            specs=self.jobs,
            clock=self.clock,
        )

    def _claim(self) -> str:
        """认领心跳；已经有别的调度器在跳就拒绝启动（D100）。

        只有 `run_forever` 调它：`tick` 是一次手工执行，不该留下一个让下一个
        常驻进程白等五分钟的心跳。
        """
        if self._beat_id is not None:
            return self._beat_id
        moment = self.clock.now_utc()
        latest = self.heartbeats.latest()
        if latest is not None:
            age = (moment - latest.beat_at).total_seconds()
            if age < HEARTBEAT_STALE_S:
                raise SchedulerBusy(
                    f"另一个调度器（{latest.id}）{int(age)} 秒前还有心跳："
                    "两个常驻进程会让同一个周期各发一次告警"
                )
        self._beat_id = self.heartbeats.start(
            note=f"tick={self.settings.scheduler_tick_s}s"
        )
        return self._beat_id

    def _selected(
        self, only_job: str | None, only_market: str | None
    ) -> tuple[JobSpec, ...]:
        chosen = tuple(
            spec
            for spec in self.jobs
            if (only_job is None or spec.name == only_job)
            and (only_market is None or spec.market == only_market)
        )
        if chosen:
            return chosen
        wanted = []
        if only_job is not None:
            wanted.append(f"job={only_job}")
        if only_market is not None:
            wanted.append(f"market={only_market}")
        names = sorted({spec.name for spec in self.jobs})
        raise ValidationError(
            f"没有匹配的 job（{'、'.join(wanted) or '全部'}）：现有 {names}"
        )
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_service.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/services/scheduler/report.py src/sams/services/scheduler/service.py tests/services/test_scheduler_service.py
git commit -m "feat(scheduler): 常驻循环、心跳单实例守卫与健康读数"
```

---

### Task 11: 健康页的 `[调度]` 与退出码

**Files:**
- Modify: `src/sams/services/health.py`（`KEY_TABLES` 两行、`HealthReport.schedule`、构造参数）
- Modify: `src/sams/cli/health.py`（`_print_schedule` + 退出码）
- Test: `tests/services/test_health.py`（追加三枚）
- Test: `tests/cli/test_health_cli.py`（新建）

D101 说「心跳丢失的告警由外部承担」：死掉的调度器发不出「我死了」，所以
`sams health` 的退出码是这条决定唯一的执行点。屏幕上那一句给人看，退出码给 cron 看。
`schedule` 是一个**函数**而不是 `Scheduler` 对象：`services/health.py` 不需要认识
调度器的循环，只需要一份 `ScheduleData`；不传时这一节写「未接入」而不是消失。

- [ ] **Step 1: 写失败用例**

`tests/services/test_health.py`：把 `service` 辅助函数改成能接 `schedule`，并追加三枚。

```python
def service(db, settings, *adapters, local_status=None, schedule=None) -> HealthService:
    registry = AdapterRegistry(
        adapters={a.name: a for a in adapters},
        market_sources={"A": tuple(a.name for a in adapters)},
        sleep=lambda seconds: None,
    )
    return HealthService(
        db,
        registry,
        CalendarRepo(db),
        settings,
        local_status=local_status,
        schedule=schedule,
    )
```

```python
def test_key_tables_include_the_scheduler_ledgers(db, settings):
    report = service(db, settings).report()
    assert {"task_runs", "scheduler_heartbeats"} <= set(report.tables)
    assert report.tables["task_runs"] == 0


def test_report_carries_the_schedule_reading(db, settings):
    data = ScheduleData(heartbeat=None, beat_age_s=None, admins=0, jobs=())
    report = service(db, settings, schedule=lambda: data).report()
    assert report.schedule is data
    assert report.schedule_stale is False


def test_without_a_schedule_reader_the_section_is_absent(db, settings):
    report = service(db, settings).report()
    assert report.schedule is None
    assert report.schedule_stale is False
```

需要追加的 import：

```python
from sams.services.scheduler.report import ScheduleData
```

`tests/cli/test_health_cli.py`（新建）：

```python
"""`sams health` 的 `[调度]` 一节（D101）。

心跳丢失只能被**外部**发现：这一屏的那一句与退出码是调度器死后唯一会响的东西。
用一个只带四样东西的假容器：这一枚钉的是屏幕与退出码，不是装配。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sams.cli.health import show
from sams.services.health import HealthReport
from sams.services.scheduler.report import JobHealth, ScheduleData
from sams.storage.repos.task_runs import Heartbeat, TaskRun

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)


def beat(age_s: int) -> Heartbeat:
    return Heartbeat(
        id="beat-1",
        started_at=MOMENT - timedelta(days=1),
        beat_at=MOMENT - timedelta(seconds=age_s),
        note="tick=60s",
    )


class FakeContainer:
    def __init__(self, report, settings) -> None:
        self.health = SimpleNamespace(report=lambda: report)
        self.settings = settings
        self.source_admin = SimpleNamespace(list=lambda: ())
        self.registry = SimpleNamespace(
            market_sources={}, chain_for=lambda market, method: ()
        )


def report_with(data, *, pending=()) -> HealthReport:
    return HealthReport(db_path=":memory:", schedule=data, pending_migrations=list(pending))


def test_a_stale_heartbeat_is_printed_and_fails_the_exit_code(settings, capsys):
    data = ScheduleData(heartbeat=beat(601), beat_age_s=601, admins=1, jobs=())

    assert show(FakeContainer(report_with(data), settings), None) == 1

    out = capsys.readouterr().out
    assert "[调度]" in out
    assert "心跳" in out and "可能已经停了" in out


def test_a_fresh_heartbeat_passes(settings, capsys):
    data = ScheduleData(heartbeat=beat(30), beat_age_s=30, admins=2, jobs=())

    assert show(FakeContainer(report_with(data), settings), None) == 0

    out = capsys.readouterr().out
    assert "管理员 2 名" in out


def test_never_started_is_honest_but_does_not_fail(settings, capsys):
    data = ScheduleData(heartbeat=None, beat_age_s=None, admins=0, jobs=())

    assert show(FakeContainer(report_with(data), settings), None) == 0

    out = capsys.readouterr().out
    assert "从未启动" in out
    assert "没有在用的管理员" in out


def test_job_lines_show_the_last_outcome_and_streak(settings, capsys):
    run = TaskRun(
        id="r1",
        job_name="bars",
        market="A",
        period_key="2024-10-25",
        started_at=MOMENT,
        finished_at=MOMENT,
        status="failed",
        rows_affected=0,
        error="[akshare] boom",
        note=None,
    )
    data = ScheduleData(
        heartbeat=beat(30),
        beat_age_s=30,
        admins=1,
        jobs=(JobHealth(name="bars", market="A", period="daily", latest=run, streak=2),),
    )

    show(FakeContainer(report_with(data), settings), None)

    out = capsys.readouterr().out
    assert "bars" in out and "failed" in out
    assert "周期 2024-10-25" in out and "连续失败 2 个周期" in out and "boom" in out


def test_without_a_schedule_reader_the_section_says_so(settings, capsys):
    report = HealthReport(db_path=":memory:")

    assert show(FakeContainer(report, settings), None) == 0

    assert "未接入调度器读数" in capsys.readouterr().out
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_health.py tests/cli/test_health_cli.py -q
```

Expected: FAIL（`HealthService` 没有 `schedule` 参数；`HealthReport` 没有 `schedule` 字段；`sams.cli.health.show` 还没有 `[调度]`）。

- [ ] **Step 3: 改 `services/health.py`**

顶部 import 加一行（`TYPE_CHECKING` 与 `ScheduleData`）：

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sams.services.scheduler.report import ScheduleData
```

`KEY_TABLES` 末尾（`"notify_log"` 之后）：

```python
    # 调度器的两本账：task_runs 是每个 job 的执行记录，scheduler_heartbeats 是「还在跑吗」
    "task_runs",
    "scheduler_heartbeats",
```

`HealthReport` 末尾加：

```python
    #: 调度器的读数；`None` 是「这个进程没接调度器」（不是「从没启动」，那是心跳为空）。
    #: 用 `TYPE_CHECKING` 的字符串注解而不是 import：这一层只需要那份数据的形状，
    #: 不需要在 import 图上连到调度器的循环
    schedule: ScheduleData | None = None

    @property
    def schedule_stale(self) -> bool:
        return self.schedule is not None and self.schedule.stale
```

构造参数末尾加：

```python
        schedule: Callable[[], ScheduleData] | None = None,
```

赋值与 `report()`：

```python
        self.schedule = schedule
```

```python
            notify=self._notify_data(),
            schedule=self.schedule() if self.schedule is not None else None,
```

- [ ] **Step 4: 改 `cli/health.py`**

在 `_print_notify` 之后加：

```python
def _age(seconds: int | None) -> str:
    if seconds is None:
        return "未知"
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    hours, rest = divmod(seconds, 3600)
    return f"{hours} 小时 {rest // 60} 分钟"


def _print_schedule(report) -> None:
    """`[调度]`：它还在不在跑、每个 job 上一轮跑成什么（D101）。

    心跳丢失的告警只能由**外部**发出；这一节的退出码是那条决定唯一的执行点——
    屏幕可以没人看，cron 看的是 `sams health` 的退出码。
    """
    print("[调度]")
    data = report.schedule
    if data is None:
        print("  未接入调度器读数")
        return
    if data.never_started:
        print("  从未启动（`sams run start` 启动常驻调度器）")
    elif data.stale:
        print(f"  ⚠ 心跳已经 {_age(data.beat_age_s)} 没更新：常驻调度器可能已经停了")
    else:
        print(f"  心跳 {_age(data.beat_age_s)}前 · 管理员 {data.admins} 名")
    if data.admins == 0:
        print("  ⚠ 没有在用的管理员：系统告警一个收件人都没有（`sams user set-admin …`）")
    for job in data.jobs:
        latest = job.latest
        line = f"  {job.name:<9} {(job.market or '全局'):<4}"
        if latest is None:
            print(line + " 从未运行")
            continue
        line += f" {latest.status:<7} 周期 {latest.period_key or '—'}"
        if latest.finished_at is not None:
            line += f" · {stamp(latest.finished_at)}"
        if job.streak:
            line += f" · 连续失败 {job.streak} 个周期"
        if latest.error:
            line += f" · {latest.error[:60]}"
        if latest.note:
            line += f" · {latest.note}"
        print(line)
```

`show` 里 `_print_notify(report)` 之后加 `_print_schedule(report)`，并把最后一行改成：

```python
    degraded = (
        report.degraded_sources or report.pending_migrations or report.schedule_stale
    )
    return 1 if degraded else 0
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/services/test_health.py tests/cli/test_health_cli.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/sams/services/health.py src/sams/cli/health.py tests/services/test_health.py tests/cli/test_health_cli.py
git commit -m "feat(health): [调度] 一节与心跳丢失的退出码"
```

### Task 12: `sams run start|once|jobs` 与组装根接线

**Files:**
- Create: `src/sams/cli/schedule.py`
- Modify: `src/sams/cli/__init__.py`（`COMMAND_MODULES` 加 `schedule`）
- Modify: `src/sams/bootstrap.py`
- Test: `tests/cli/test_run_cli.py`
- Test: `tests/test_bootstrap.py`（追加三条）

到这一步为止，调度器在代码里已经能跑，但没有任何进程会造它：`bootstrap` 不装配，
命令行也够不着。这个 task 把两头接上，同时回答三个使用问题：

* `run start` —— 常驻。屏幕上必须先是「几个 job、多久一轮」，否则运维不知道
  自己启动的是不是刚改完的那份配置；
* `run once` —— 手工补一轮，也是排障入口。它打印每条结果、等待中的 job 与到期
  计算的错误，失败/出错时退出码 1；
* `run jobs` —— 按执行顺序列出注册结果，回答「为什么今晚没有港股规则」。

两个容易踩的接线点，先写清楚：

1. **模块名不能叫 `run.py`**。`sams/cli/__init__.py` 有一个包级的 `run()`，
   `from sams.cli import run` 会先解析到同名子模块，`cli.run([...])` 变成
   `Module` 不可调用——症状是所有 CLI 测试一起红，而报错停在测试的调用处。
2. **`iso_date` 必须在 handler 里调用，不能当 `type=`**。argparse 不认
   `ValidationError`，`--as-of 2024-13-40` 会吐一段栈而不是「这个日期该怎么写」；
   handler 里调用才能经 `exit_for` 变成退出码 2（D77）。

- [ ] **Step 1: 写失败的 CLI 用例**

`tests/cli/test_run_cli.py`：

```python
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from sams.cli import build_parser
from sams.core.errors import SchedulerBusy, ValidationError
from sams.services.scheduler.runner import SKIPPED, RunResult
from sams.services.scheduler.service import TickReport
from sams.services.scheduler.types import DAILY, FX, WEEKLY, JobOutcome, JobSpec

NOW = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
DAY = date(2024, 10, 25)


def result(**over) -> RunResult:
    fields = {
        "job": "bars",
        "market": "A",
        "period_key": "2024-10-25",
        "status": "ok",
        "rows_affected": 3,
        "note": "入库 3 根",
    }
    fields.update(over)
    return RunResult(**fields)


class FakeScheduler:
    """只回答三个问题：注册了什么、tick 收到了哪些参数、循环推了几轮。"""

    def __init__(self, *, report: TickReport | None = None, reports=(), error=None):
        self.jobs = (
            JobSpec("bars", DAILY, "A", lambda context: JobOutcome()),
            JobSpec("recheck", WEEKLY, "A", lambda context: JobOutcome()),
            JobSpec("fx", FX, None, lambda context: JobOutcome()),
        )
        self.report = report if report is not None else TickReport(now=NOW)
        self.reports = tuple(reports)
        self.error = error
        self.calls: list[dict] = []

    def tick(self, *, as_of=None, only_job=None, only_market=None) -> TickReport:
        self.calls.append(
            {"as_of": as_of, "only_job": only_job, "only_market": only_market}
        )
        if self.error is not None:
            raise self.error
        return self.report

    def run_forever(self, *, on_tick=None, max_ticks=None) -> int:
        if self.error is not None:
            raise self.error
        for report in self.reports:
            on_tick(report)
        return len(self.reports)


def invoke(scheduler, argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    container = SimpleNamespace(
        scheduler=scheduler,
        settings=SimpleNamespace(scheduler_tick_s=60),
    )
    return args.handler(container, args)


def test_once_prints_each_result_and_counts_failures(capsys):
    scheduler = FakeScheduler(
        report=TickReport(
            now=NOW,
            results=(
                result(),
                result(
                    job="actions",
                    market="HK",
                    status="failed",
                    note="",
                    error="RateLimited: 429",
                ),
                result(
                    job="fx",
                    market=None,
                    status=SKIPPED,
                    note="",
                    skip_reason="10 分钟内重试过",
                ),
            ),
            waiting=("calendar@A",),
        )
    )
    assert invoke(scheduler, ["run", "once"]) == 1
    out = capsys.readouterr().out
    assert "ok     bars@A" in out and "入库 3 根" in out
    assert "failed actions@HK" in out and "429" in out
    assert "skipped fx@全局" in out and "10 分钟内重试过" in out
    assert "等待  calendar@A" in out


def test_once_exits_zero_when_nothing_failed(capsys):
    scheduler = FakeScheduler(
        report=TickReport(now=NOW, results=(result(),), waiting=("fx@全局",))
    )
    assert invoke(scheduler, ["run", "once"]) == 0


def test_once_normalizes_market_and_parses_as_of(capsys):
    scheduler = FakeScheduler()
    argv = ["run", "once", "--market", "hk", "--as-of", "2024-10-25", "--job", "bars"]
    assert invoke(scheduler, argv) == 0
    assert scheduler.calls == [
        {"as_of": DAY, "only_job": "bars", "only_market": "HK"}
    ]


def test_a_malformed_date_is_a_usage_error(capsys):
    scheduler = FakeScheduler()
    assert invoke(scheduler, ["run", "once", "--as-of", "2024-13-40"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert scheduler.calls == [], "日期都没看懂，就不该碰调度器"


def test_an_unknown_job_is_a_usage_error(capsys):
    scheduler = FakeScheduler(error=ValidationError("没有匹配的 job（name=ghost）"))
    assert invoke(scheduler, ["run", "once", "--job", "ghost"]) == 2
    assert "没有匹配的 job" in capsys.readouterr().err


def test_start_reports_the_job_count_and_suppresses_skipped_only_ticks(capsys):
    skipped = TickReport(
        now=NOW,
        results=(result(status=SKIPPED, note="", skip_reason="成功过的周期不重跑"),),
    )
    alive = TickReport(now=NOW, results=(result(),))
    scheduler = FakeScheduler(reports=(skipped, alive))
    assert invoke(scheduler, ["run", "start"]) == 0
    out = capsys.readouterr().out
    assert "3 个 job，每 60 秒一轮" in out
    assert "已停止（2 个 tick）" in out
    # 常驻循环每 60 秒一轮；全是「已跑过」的 tick 每秒刷一行会把真正的失败冲走
    assert "成功过的周期不重跑" not in out
    assert out.count("bars@A") == 1


def test_start_reports_a_second_daemon_as_a_message(capsys):
    scheduler = FakeScheduler(
        error=SchedulerBusy("已经有一个调度器在运行（心跳 30 秒前）")
    )
    assert invoke(scheduler, ["run", "start"]) == 1
    captured = capsys.readouterr()
    assert "已经有一个调度器" in captured.err
    assert "SchedulerBusy" not in captured.err
    assert captured.out.startswith("调度器启动")


def test_jobs_lists_the_registration_order(capsys):
    assert invoke(FakeScheduler(), ["run", "jobs"]) == 0
    out = capsys.readouterr().out
    assert "注册 3 个 job" in out
    assert out.index("bars") < out.index("recheck") < out.index("fx")
    assert "全局" in out
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/cli/test_run_cli.py -q
```

Expected: FAIL（`run` 不是已注册的命令）。

- [ ] **Step 3: 写 `cli/schedule.py` 并注册**

`src/sams/cli/schedule.py`：

```python
"""`sams run`：常驻调度、手工跑一轮、列出注册的 job。

模块名用 schedule 而不是 run：`sams/cli/__init__.py` 里已经有一个包级的 `run()`，
同名模块会让 `from sams.cli import run` 解析成子命令模块，而不是那个入口。
"""

from __future__ import annotations

import argparse
import sys

from sams.bootstrap import Container
from sams.cli.common import exit_for, group, iso_date, stamp
from sams.core.codes import canonical_market
from sams.core.errors import SamsError
from sams.services.scheduler.runner import SKIPPED
from sams.services.scheduler.service import TickReport


def add_to(subparsers) -> None:
    actions = group(subparsers, "run", "调度器：常驻运行、手工跑一轮、列出注册的 job")

    start = actions.add_parser("start", help="常驻运行（Ctrl-C 停止；同一时刻只允许一个）")
    start.set_defaults(handler=_start)

    once = actions.add_parser("once", help="按当下的到期状态手工跑一轮")
    once.add_argument(
        "--as-of", help="把这一轮当成哪一天（YYYY-MM-DD；默认按各市场当地日期）"
    )
    once.add_argument(
        "--job",
        help="只跑这个 job（bars/actions/financials/valuation/flow/recheck/rules/fx/calendar）",
    )
    once.add_argument("--market", help="只跑这个市场（A/HK/US）")
    once.set_defaults(handler=_once)

    actions.add_parser("jobs", help="按执行顺序列出注册的 job").set_defaults(
        handler=_jobs
    )


def _print_result(result, indent: str = "") -> None:
    line = (
        f"{indent}{result.status:<6} {result.job}@{result.market or '全局'}"
        f"  周期 {result.period_key}"
    )
    if result.note:
        line += f" · {result.note}"
    if result.skip_reason:
        line += f" · {result.skip_reason}"
    if result.error:
        line += f" · {result.error}"
    print(line)


def _print_tick(report: TickReport) -> None:
    """常驻模式的一行Tick：全是 skipped 且没有错误时整轮不出声。"""
    shown = [result for result in report.results if result.status != SKIPPED]
    if not shown and not report.errors:
        return
    print(f"[{stamp(report.now)}]")
    for result in shown:
        _print_result(result, "  ")
    for error in report.errors:
        print(f"  错误  {error}")


def _start(container: Container, args: argparse.Namespace) -> int:
    scheduler = container.scheduler
    print(
        f"调度器启动：{len(scheduler.jobs)} 个 job，"
        f"每 {container.settings.scheduler_tick_s} 秒一轮（Ctrl-C 停止）"
    )
    try:
        ticks = scheduler.run_forever(on_tick=_print_tick)
    except SamsError as exc:
        # 认领心跳失败（另一个调度器在跑）走这里：顶层 run() 只会给退出码 1，
        # 而「命令敲错了」与「已经有进程在跑」对脚本是两件事（D77）
        print(f"错误: {exc}", file=sys.stderr)
        return exit_for(exc)
    print(f"已停止（{ticks} 个 tick）")
    return 0


def _once(container: Container, args: argparse.Namespace) -> int:
    try:
        market = canonical_market(args.market) if args.market else None
        as_of = iso_date(args.as_of) if args.as_of else None
        report = container.scheduler.tick(
            as_of=as_of, only_job=args.job, only_market=market
        )
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return exit_for(exc)
    for result in report.results:
        _print_result(result)
    for title in report.waiting:
        print(f"等待  {title}  还没到点")
    for error in report.errors:
        print(f"错误  {error}")
    return 1 if report.failed or report.errors else 0


def _jobs(container: Container, args: argparse.Namespace) -> int:
    print(f"注册 {len(container.scheduler.jobs)} 个 job（执行顺序即下列顺序）：")
    for spec in container.scheduler.jobs:
        print(f"  {spec.name:<10} {spec.market or '全局':<4} {spec.period}")
    return 0
```

`src/sams/cli/__init__.py` 两处都按字母序插入 `schedule`（`rule` 之后、`screen` 之前）：

```python
from sams.cli import (
    alert,
    analysis,
    calendar,
    channel,
    db,
    health,
    ledger,
    rule,
    schedule,
    screen,
    source,
    sync,
    thesis,
    user,
)
```

```python
COMMAND_MODULES = (
    db,
    user,
    sync,
    calendar,
    health,
    source,
    analysis,
    screen,
    thesis,
    ledger,
    channel,
    rule,
    schedule,
    alert,
)
```

- [ ] **Step 4: 跑绿**

```bash
uv run pytest tests/cli/test_run_cli.py tests/cli/test_cli.py -q
```

Expected: PASS。`tests/cli/test_cli.py` 里的 `test_every_subcommand_help_page_formats`
会自动走到新命令的三个 help 页。

- [ ] **Step 5: 写失败的组装根用例**

在 `tests/test_bootstrap.py` 末尾追加（`SystemAlarm` 已在文件顶部 import）：

```python
def test_the_container_registers_jobs_in_execution_order(container):
    """执行顺序是语义：周复检要在当晚规则之前，汇率要排在所有市场之后。

    这个用例只钉顺序与「按能力注册」，不钉 job 数量——`stub_adapters` 只给日线，
    所以港股没有基本面 job，而这是 D106 想要的结果，不是缺数据。
    """
    built, _ = container
    titles = [job.title for job in built.scheduler.jobs]
    assert titles.count("bars@A") == 1
    assert titles.index("recheck@US") < titles.index("rules@US")
    assert titles.index("rules@US") < titles.index("fx@全局")
    assert titles.index("fx@全局") < titles.index("calendar@A")


def test_a_job_alarm_becomes_a_system_alert_for_admins(container):
    """D102：同一条 `SystemAlarm` 在总线有两类订阅者——日志与管理员。

    路由的分支在 `test_scheduler_alarms.py` 逐条钉过，这里只验接线：
    告警真的从总线走到了 `alerts` 那张表，且带上了系统告警的三件标记。
    """
    built, _ = container
    built.users.create("root", "correct-horse-battery", is_admin=True)
    admin = built.users.get_by_username("root")

    built.bus.publish(SystemAlarm(component="job.bars.A", message="连续 3 个周期失败"))

    rows = built.alerts_store.list_for(admin.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.market == "SYS" and row.code == "job.bars.A"
    assert row.rule_id is None
    assert "连续 3 个周期失败" in row.payload["extra"]["body"]


def test_health_carries_the_schedule_reading(container):
    built, _ = container
    report = built.health.report()
    assert report.schedule is not None
    assert {"bars", "rules", "fx"} <= {job.name for job in report.schedule.jobs}
    assert report.schedule.admins == 0
```

- [ ] **Step 6: 跑红**

```bash
uv run pytest tests/test_bootstrap.py -q
```

Expected: FAIL（`Container` 没有 `scheduler`；`HealthReport.schedule` 仍为 `None`；
发布告警后 `alerts` 为空）。

- [ ] **Step 7: 接线 `bootstrap.py`**

import 块按字母序补六行服务与两行仓库：

```python
from sams.services.scheduler.alarms import SystemAlerter
from sams.services.scheduler.jobs import build_jobs
from sams.services.scheduler.runner import JobRunner
from sams.services.scheduler.service import Scheduler
from sams.services.scheduler.types import JobDeps
from sams.services.scheduler.universe import UniverseBuilder
```

```python
from sams.storage.repos.task_runs import HeartbeatRepo, TaskRunRepo
```

`Container` 的 `rule_rechecker` 之后加：

```python
    #: 常驻调度器（spec §12）：`run start/once/jobs` 与健康页的 `[调度]` 都读它
    scheduler: Scheduler
```

把 sender 提成变量（`RuleEngine` 与 `SystemAlerter` 共用同一条发送路径——
两套 sender 意味着 DND 与通道选择有两份实现，admin 收不收得到系统告警
会取决于它走的是哪一条）：

```python
    # 规则告警与系统告警走同一个 sender：不是复用技巧，是「只有一条发送路径」
    sender = _NotifyAlertSender(notify)
    rule_engine = RuleEngine(
        rules=rules_store,
        alerts=alerts_store,
        data=rule_data,
        calendar=calendar,
        fx=fx_store,
        bus=event_bus,
        sender=sender,
        settings=cfg,
        clock=clock,
    )
```

`rule_rechecker = Rechecker(...)` 之后、`return Container(` 之前，加：

```python
    SystemAlerter(
        alerts=alerts_store, users=users, sender=sender, settings=cfg, clock=clock
    ).attach(event_bus)

    # 下面四个服务原本就地造在 `return Container(...)` 里：调度器也要吃它们，
    # 而同一个服务被造两份时，「job 里跑的」与「命令行跑的」不再是同一条链
    sync = SyncService(registry, bars, symbols, cfg.markets)
    corp_action_sync = CorpActionSyncService(
        registry, corp_actions, corp_actions_coverage, clock=clock
    )
    fundamental_sync = FundamentalSyncService(
        registry, financials_store, valuation_store, flow_store, symbols
    )
    fx_sync = FxSyncService(registry, fx_store)
    universe_builder = UniverseBuilder(
        users=users,
        watchlists=watchlists,
        accounts=sim_accounts,
        orders=sim_orders,
        rules=rules_store,
        theses=theses_store,
        data=rule_data,
        settings=cfg,
    )
    task_runs = TaskRunRepo(database, clock=clock)
    heartbeats = HeartbeatRepo(database, clock=clock)
    deps = JobDeps(
        settings=cfg,
        registry=registry,
        universe=universe_builder,
        sync=sync,
        corp_actions=corp_action_sync,
        fundamentals=fundamental_sync,
        fx=fx_sync,
        calendar=calendar,
        rules=rule_engine,
        recheck=rule_rechecker,
        users=users,
        clock=clock,
    )
    scheduler = Scheduler(
        cfg,
        build_jobs(deps),
        JobRunner(runs=task_runs, bus=event_bus, clock=clock),
        task_runs,
        heartbeats,
        universe_builder,
        users,
        calendar,
        clock=clock,
    )
```

`return Container(...)` 里四处改成上面造好的对象，并补 `scheduler`：

```python
        sync=sync,
        corp_action_sync=corp_action_sync,
        fundamental_sync=fundamental_sync,
        fx_sync=fx_sync,
```

```python
        rule_rechecker=rule_rechecker,
        scheduler=scheduler,
```

`HealthService(...)` 的构造参数补一行：

```python
            schedule=scheduler.schedule_data,
```

- [ ] **Step 8: 跑绿与静态检查**

```bash
uv run pytest tests/cli/test_run_cli.py tests/test_bootstrap.py tests/services/test_health.py -q
uv run ruff check src tests --no-cache
```

Expected: PASS；ruff 无输出。

- [ ] **Step 9: 提交**

```bash
git add src/sams/cli/schedule.py src/sams/cli/__init__.py src/sams/bootstrap.py tests/cli/test_run_cli.py tests/test_bootstrap.py
git commit -m "feat(scheduler): sams run start|once|jobs 与组装根接线"
```

### Task 13: 一个模拟交易日的端到端验收

**Files:**
- Modify: `tests/support_valuation.py`（`make_container` 接受 `moment`/`adapters`/`providers`）
- Create: `tests/test_scheduler_acceptance.py`
- Test: `tests/test_scheduler_acceptance.py`

这一枚验的不是「每个零件都对」（那由前十二枚各自的用例回答），而是**它们排成一晚之后
仍然对**：数据 job 写进去的行被规则读到、失败的 job 不挡住后面的市场、常驻循环认领了
心跳、第二轮不重复写账也不重复打扰。把这条链拆成单个用例全绿、拼起来却对不上的
事故，只会在这个粒度上露头。

时钟冻在 **2024-10-25 23:00 UTC**：A/HK 本地 15:00 收盘 + 30 分钟、美股 EDT 15:00
收盘 + 60 分钟都已过去，三个市场的 `as_of` 都是 10-25，且 10-25 是那一 ISO 周的
最后一个交易日（下一个交易日 10-28 已是第 44 周）。库里只铺到 **10-24**：这一晚每条
job 都有真正要做的事，不是「全都已跑过」的空转。

数据源替身必须**五个方法都在**。`chain_for` 按方法存在与否路由（D38）：只有日线的桩
会让 `financials`/`valuation`/`flow` 三个 job 根本不登记，验收就悄悄退化成「只验了日线」。

- [ ] **Step 1: 让 `make_container` 能接住这三个口子**

`tests/support_valuation.py` 的 `make_container` 整体替换为：

```python
def make_container(
    db,
    settings,
    *,
    today: date = TODAY,
    moment: datetime | None = None,
    adapters: dict | None = None,
    providers: dict | None = None,
    notifier_factory=None,
    **seeded,
):
    """一个铺好样本库、时钟冻在 `today`（或指定时刻）的容器。

    `adapters`/`providers` 是调度器验收的后门：那一晚要五个能力的源与非网络的日历；
    默认的 `StubFundamentalsAdapter` 只有基本面三件、没有日线，日历也要走真 provider。
    `moment` 与 `today` 并存：前者到分钟（收盘后延迟要它），后者是「铺到哪一天」。
    """
    adapter = StubFundamentalsAdapter(["A"])
    container = build_container(
        settings,
        db=db,
        adapters=adapters
        if adapters is not None
        else {
            "akshare": adapter,
            "eastmoney": adapter,
            "yfinance": support.StubAdapter(
                "yfinance", ["HK", "US"], support.sample_bars(source="yfinance")
            ),
        },
        providers=providers,
        clock=support.FrozenClock(
            moment
            if moment is not None
            else datetime(today.year, today.month, today.day, 15, tzinfo=UTC)
        ),
        notifier_factory=notifier_factory,
    )
    container.registry.sleep = lambda _seconds: None
    seed(db, **seeded)
    return container
```

- [ ] **Step 2: 写验收用例**

`tests/test_scheduler_acceptance.py`：

```python
"""M3-D 的验收：一个模拟交易日，从日线到告警、从心跳到健康页走一遍。

时钟冻在 2024-10-25 23:00 UTC：三个市场的收盘与盘后延迟都已过去；库里铺到 10-24，
所以每条 job 都有真正要做的事。数据源是五能力替身——只有日线的桩会让三个基本面 job
根本不登记，验收就退化成「只验了日线」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import support_valuation as sample
from support import StubAdapter, sample_bars
from support_scheduler import WeekdayProvider

from sams.core.errors import RateLimited
from sams.core.notify import NotifyResult
from sams.services.scheduler.runner import SKIPPED
from sams.storage.repos.bars import BarRepo

MOMENT = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
AS_OF = date(2024, 10, 25)
USER = "trader"
PWD = "correct-horse-battery"
HISTORY = [day for day in sample.DAYS if day <= date(2024, 10, 24)]
HISTORY_CLOSES = sample.CLOSES[: len(HISTORY)]


def weekdays(start: date, end: date) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


class WorldAdapter:
    """A 股的五能力源：每个方法都要在，`chain_for` 才登记对应的 job。"""

    name = "world"
    corp_actions_full_history = True

    def __init__(self, markets=("A",)) -> None:
        self.markets = frozenset(markets)

    def daily_bars(self, market, code, start, end):
        days = weekdays(start, end)
        return sample.bars(days, [sample.PRICE] * len(days), market=market, code=code)

    def corp_actions(self, market, code, start, end):
        return sample.dividend(AS_OF, market=market, code=code)

    def financials(self, market, code, start, end):
        return sample.financial_rows(market=market, code=code)

    def valuation(self, market, code, start, end):
        return sample.valuation(weekdays(start, end), market=market, code=code)

    def funding_flow(self, market, code, start, end):
        return sample.flow_rows(weekdays(start, end), net=1.0e8, market=market, code=code)


def providers_for(settings) -> dict:
    return {
        market: WeekdayProvider(market, cfg.tz)
        for market, cfg in settings.markets.items()
    }


def recording_factory(kind, config):
    class Node:
        name = kind

        def available(self):
            return True

        def unavailable_reason(self):
            return ""

        def send(self, message):
            return NotifyResult(ok=True)

    return Node()


def build_world(db, settings, *, yfinance=None, notifier_factory=None):
    world = WorldAdapter()
    return sample.make_container(
        db,
        settings,
        moment=MOMENT,
        adapters={
            "akshare": world,
            "eastmoney": world,
            "yfinance": yfinance
            or StubAdapter("yfinance", ["HK", "US"], sample_bars("HK", "00700", source="yfinance")),
        },
        providers=providers_for(settings),
        notifier_factory=notifier_factory,
        days=HISTORY,
        closes=HISTORY_CLOSES,
    )


def test_one_trading_day_converges_to_one_alert_and_a_fresh_heartbeat(db, settings):
    container = build_world(db, settings, notifier_factory=recording_factory)
    user = container.users.create(USER, PWD)
    container.watchlists.add(user.id, "A", sample.CODE)
    node = container.notify.add(user.id, "wecom", "主群", {"url": "https://x/key=k"})
    container.rule_service.add(
        user.id,
        kind="stop.fixed",
        name="跌破十万",
        target_type="symbol",
        target_ref=f"A/{sample.CODE}",
        params={"price": 100000},
        channel_ids=[node.id],
    )

    reports = []
    assert container.scheduler.run_forever(on_tick=reports.append, max_ticks=1) == 1
    report = reports[0]

    assert report.waiting == () and report.errors == ()
    assert [result.status for result in report.results] == ["ok"] * len(report.results)
    assert [f"{r.job}@{r.market or '全局'}" for r in report.results] == [
        job.title for job in container.scheduler.jobs
    ]

    # 规则读的是今晚刚写进去的那一根：bars job 必须排在 rules job 之前
    assert BarRepo(db).latest_date("A", sample.CODE) == AS_OF

    alerts = container.alerts_store.list_for(user.id)
    assert len(alerts) == 1
    assert alerts[0].code == sample.CODE
    assert container.alerts_store.deliveries(alerts[0].id)[0].outcome == "sent"

    health = container.health.report()
    assert health.schedule is not None
    assert health.schedule.beat_age_s == 0
    assert not health.schedule.stale
    assert len(health.schedule.jobs) == len(container.scheduler.jobs)
    assert all(
        job.latest is not None and job.latest.status == "ok"
        for job in health.schedule.jobs
    )

    # 同一晚再一拍：成功的周期不重跑，不写新账，也不重复打扰
    run_count = container.scheduler.runs.count()
    again = container.scheduler.tick()
    assert [result.status for result in again.results] == [SKIPPED] * len(again.results)
    assert container.scheduler.runs.count() == run_count
    assert len(container.alerts_store.list_for(user.id)) == 1


def test_one_market_failure_does_not_stop_the_rest(db, settings):
    broken = StubAdapter(
        "yfinance", ["HK", "US"], RateLimited("yfinance", "429 too many requests")
    )
    container = build_world(db, settings, yfinance=broken)
    user = container.users.create(USER, PWD)
    container.watchlists.add(user.id, "A", sample.CODE)
    container.watchlists.add(user.id, "HK", "00700")

    report = container.scheduler.tick()

    by_key = {(result.job, result.market): result for result in report.results}
    assert report.errors == (), "到期计算本身不该坏；坏的是港股的源"
    assert by_key[("bars", "A")].status == "ok"
    assert by_key[("bars", "HK")].status == "failed"
    assert "429" in by_key[("bars", "HK")].error
    assert by_key[("rules", "A")].status == "ok"
    assert by_key[("fx", None)].status == "ok"
    assert BarRepo(db).latest_date("A", sample.CODE) == AS_OF
```

- [ ] **Step 3: 跑它**

```bash
uv run pytest tests/test_scheduler_acceptance.py -q
```

Expected: PASS。红了先别改用例：这一枚红的地方就是前十二枚之间的集成缺口
（最常见的是 job 注册顺序、日历 provider 没接到 `make_container`、或规则读到的
K 线仍是 10-24）。按失败信息回到对应的那一枚修，修完再跑这一枚。

- [ ] **Step 4: 回归这一层最容易被碰到的邻居**

```bash
uv run pytest tests/services/test_scheduler_service.py tests/services/test_scheduler_runner.py tests/cli/test_rule_cli.py tests/test_bootstrap.py -q
```

Expected: PASS。`make_container` 多了三个可选参数，所有老调用点都必须原样可用。

- [ ] **Step 5: 提交**

```bash
git add tests/support_valuation.py tests/test_scheduler_acceptance.py
git commit -m "test(scheduler): 一个模拟交易日的端到端验收（告警、心跳、幂等）"
```

### Task 14: spec 收尾——状态行、§14 两段、U7

**Files:**
- Modify: `docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md`

这一枚不改代码：把「M3 做完了」写成可复核的几句话。先记两件容易糊过去的事：

1. **§14 缺 M3-C 那一段**。M3-C 的计划收了尾（它自己的 §4/§5），spec 的状态行却还写着
   「M3-C 待实施计划」。不改这一句，下一份计划读到的现状就是假的。
2. **数字只能来自实测**。M3-C 的 2738/381 是它收尾时的读数，本轮收尾必须重新跑；
   拿旧数当承诺与不写数一样糟。

- [ ] **Step 1: 先把全量跑一遍，把数字记下来**

```bash
uv run ruff check src tests --no-cache
uv run pytest tests/test_layering.py -q
uv run pytest -m "not live" -q
uv run pytest -m "not live" -q --collect-only | tail -2
```

Expected: 前三行全绿；最后一行给出收集数，与 `-q` 最后那句 `NNNN passed` 加上
`14 deselected` 对得上。把三个数记下来：**离线 passed**、**收集数**、**分层 passed**。
对不上就别往下写——下面的段落要引用它们。

- [ ] **Step 2: 改状态行**

文件第 4 行整体替换为（`<离线 passed>` 换成 Step 1 的读数，其余原样）：

```markdown
状态：**M1 + M2（A–E）+ M3-A 账本 + M3-B 通知层 + M3-C 规则引擎 + M3-D 调度器已实现并离线自测通过**（<离线 passed> 用例绿 + 真实屏），本文档已同步这些里程碑落地的口径决定（D1–D110，§14 按里程碑分段记录）；M4–M5 只有范围
```

- [ ] **Step 3: 在 §14 补 M3-C 那一段**

M3-B 段之后、`**M3 还剩**` 之前插入：

```markdown
**M3-C 规则引擎已完成（2026-10-22，新增 D94–D96）**：§9.2/§9.3/§10 落成——迁移 0016 给复检基线一块住的地方、0017 给 `rules.params_on` 一个价空间锚；`core/rules.py` 把十四种模板的参数契约收成名单的唯一出处；`services/rules/snapshot.py` 的 `expand`/`read` 是取数的唯一入口，`templates/` 的止损止盈与观点类模板共用同一份 `stop_stage`/`target_stage`；`engine.py` 一趟求值的次序是判定 → 过滤 → 冷却 → 投递 → 事件（D94/D95），命中后走 `AlertSender` 端口（`bootstrap` 把它接到 `NotifyService.dispatch`）；`derive.py` 六条建仓派生幂等、改参数回写论上那一列；`recheck.py` 周复检建基线、比基线；`maintenance.py` 补上四个仓库层看不见的检查；`sams rule {kinds,add,list,show,edit,enable,disable,remove,derive,check,recheck}` 与 `sams alert {list,show,handle,dismiss}` 两条命令面；`sams health` 能读出告警积压。除权日误报由 0017 的锚 + `move_prices` 把参数与论上的线搬进今天的价空间关掉（对应的强制验收在真容器上红过再绿）。三道闸：离线 **2738 passed / 14 deselected**（收集 2752），分层守卫 **381 passed**，`ruff check` 全绿。那一轮记下的债（`Relocation.moves` 没有 src 读者、`params_on` 不在 `sams rule list`、`QuoteUpdated` 无发布方）原样留给后续。
```

- [ ] **Step 4: 用 M3-D 那一段替换「M3 还剩」**

`**M3 还剩**：…` 那整段删除，替换为（三层数字来自 Step 1；「本层新增」= 离线 passed − 2738）：

```markdown
**M3-D 调度器已完成（2026-10-22，新增 D97–D110）**：§12 收盘后那一半落成——迁移 0018 给 `task_runs` 加 `period_key` 与 `scheduler_heartbeats` 表；`services/scheduler/` 按「到期计算无 IO（`due.py`）→ 对象并集（`universe.py`）→ job 只拼已有服务（`jobs.py`）→ runner 只记这一跑（`runner.py`）→ service 只排顺序与心跳（`service.py`）→ report 只读（`report.py`）」切开；`sams run {start,once,jobs}` 是它的三个入口；`sams health` 多一节 `[调度]`，心跳丢失用退出码说。job 的注册顺序即执行顺序：每个市场数据链 → 周复检 → 规则，然后汇率，最后日历；没有能力的市场不登记该 job（D106）。这一层防的三件事：**同一晚不重复跑**（`period_key` + 成功不重跑 + 10 分钟重试，D98/D105）、**不重复打扰**（`SystemAlarm` 只在连续 3 个周期失败时补一条，D103；admin 去重跨重启，D102）、**一个失败不挡住别人**（runner 按 job 隔离、tick 按到期计算隔离；模拟交易日里港股 429 时 A 的整条链与后面的 job 照常）。M3 总验收的「挂机运行一个真实 A 股交易日」按 D97 的口径落在 `tests/test_scheduler_acceptance.py` 的模拟交易日上：收盘后整链 ok、规则读到当晚 K 线并只发一条告警、健康页心跳为 0 秒、同一晚第二拍全部 skipped 且不新增账与告警。盘中那一半（每 10 秒快照 → `QuoteUpdated` → 盘中规则）仍归 M4/U7，`quotes_snapshot` 仍没有写入方。三道闸：`ruff check` 全绿、分层 **<分层 passed> passed**、离线 **<离线 passed> passed / 14 deselected**（M3-C 收尾时 2738，本层新增 <离线 passed − 2738> 枚）。

**M3 到此收口**：规则引擎、通知、账本、调度器四层都已接上，M3 不再有「待实施计划」的条目。留给 M4 的两件是看板与盘中实时行情（`quotes_snapshot`/`QuoteUpdated` 的发布方与每 10 秒轮询）；此外，上面那个模拟交易日验的是**离线**的整链，§14 原验收里「真实 A 股交易日挂机、自选股无漏报」仍需要一次真实网络、真实交易日的运行来复核——它是一次人工动作，不是自动化闸能替代的。
```

- [ ] **Step 5: 改 §18 的 U7**

U7 那一行整体替换为：

```markdown
| U7 | `quotes_snapshot.is_stale` 有表无写入方（`task_runs` 已在 M3-D 接上写入方） | §5.3 的陈旧标记服务盘中轮询，M1 只有盘后日线；M3-D 的调度器只写 `task_runs` 与 `heartbeats`，快照采集器仍不存在 | M4 的快照采集器落地时（届时一并接 `QuoteUpdated`） |
```

- [ ] **Step 6: 自检，别让文档说假话**

```bash
grep -n "M3-C 规则引擎、M3-D 调度器" docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md
grep -n "M3 还剩\|M3 到此收口\|M3-D 调度器已完成\|M3-C 规则引擎已完成" docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md
grep -n "^| U7 |" docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md
```

Expected: 第一条**没有输出**（旧状态已消失）；第二条恰好三行（`M3-C`、`M3-D`、`M3 到此收口`，
且 `M3 还剩` 一个都没有）；第三条是新的 U7。再把 Step 2/4 里的数字与 Step 1 的读数对一遍——
文档里的每一个数都必须能在刚才那次输出里找到，找不到就改回实测值。

- [ ] **Step 7: 提交**

```bash
git add docs/superpowers/specs/2026-10-17-stock-analysis-monitoring-system-design.md
git commit -m "docs(spec): M3-C / M3-D 收尾——D94–D110 与模拟交易日验收"
```
