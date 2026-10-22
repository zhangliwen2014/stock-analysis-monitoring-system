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
"""五个数据 job 的两条规矩：窗口从库里已有的日子往前，失败按只隔离。

窗口断言看的是**问出去的区间**（`adapter.calls`），不是写回来的行数：一个把 730 天
写死的 job 也能把今天的 K 线放进库，只是每晚都把所有源重拉一遍。
按只隔离看的是**其余那一只**：一只票的链断了，剩下那只照常入库，断的那只进 `errors`。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import polars as pl
import pytest
from support import sample_actions, sample_bars
from support_fundamentals import StubFundamentalsAdapter
from support_valuation import financial_rows, flow_rows, valuation

from sams.core.errors import RateLimited
from sams.datasource.registry import AdapterRegistry
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.scheduler.jobs import (
    build_jobs,
    run_actions,
    run_bars,
    run_financials,
    run_flow,
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
    """日线 + 公司行为的记录替身：记下每一次问出去的区间。

    `name`/`markets` 按实例传入，以对齐真实源：`akshare` 只覆盖 A 股（`akshare_cn.py`），
    港股日线另由 `yfinance` 供（`yfinance_us.py`）——注册表在构造时就校验「路由里的源
    支持该市场」，把一个只吃 A 的名字排进 HK 的链上是这里要避开的谎。两个名字都只提供
    真实源确实提供的那几种能力（日线、公司行为）。
    """

    def __init__(
        self,
        *,
        name: str = "akshare",
        markets: tuple[str, ...] = ("A",),
        frame: pl.DataFrame | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.name = name
        self.markets = frozenset(markets)
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

    def corp_actions(self, market, code, start, end):
        return sample_actions(market=market, code=code, source=self.name)


def deps(db, settings, *, bars=None, finance=None, hk=None):
    """装配这条链上真正跑到的那几个服务；汇率/日历/规则那几个槽位这里是 None，本文件不跑它们。"""
    bars = bars or RecordingBarsAdapter()
    hk = hk or RecordingBarsAdapter(name="yfinance", markets=("HK",))
    finance = finance or StubFundamentalsAdapter(["A"])
    finance.name = "eastmoney"
    registry = AdapterRegistry(
        adapters={"akshare": bars, "yfinance": hk, "eastmoney": finance},
        market_sources={"A": ("akshare", "eastmoney"), "HK": ("yfinance",)},
        sleep=lambda _: None,
    )
    symbols = SymbolRepo(db)
    return JobDeps(
        settings=settings,
        registry=registry,
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
    frame = pl.concat([sample_bars(code="600519"), sample_bars(code="601088")]).filter(
        pl.col("date") == date(2024, 1, 2)
    )
    adapter = RecordingBarsAdapter(frame=frame, fail_on="600519")
    outcome = run_bars(deps(db, settings, bars=adapter), ctx("600519", "601088"))
    assert len(outcome.errors) == 1 and "600519" in outcome.errors[0]
    assert outcome.rows_affected == 1
    assert "1/2" in outcome.note


def test_empty_universe_is_a_successful_noop(db, settings):
    outcome = run_bars(deps(db, settings), ctx())
    assert outcome.errors == () and outcome.rows_affected == 0


def test_a_programming_error_is_not_collected_as_a_failed_code(db, settings, monkeypatch):
    """`_collect` 只接本项目的失败：一个编程错误必须原样冒出去，而不是变成一条 `errors`。

    被接住的编程错误出门时说的是「这只票取不到」，于是要修的那行代码没人会去修。
    """

    def boom(self, market, code):
        raise TypeError("latest_date 收到了没有键的一行")

    monkeypatch.setattr(BarRepo, "latest_date", boom)
    with pytest.raises(TypeError):
        run_bars(deps(db, settings), ctx("600519", "601088"))


def test_financials_job_writes_what_the_chain_answers(db, settings):
    finance = StubFundamentalsAdapter(["A"], financials=financial_rows())
    outcome = run_financials(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.errors == ()
    assert outcome.rows_affected == 2
    assert db.row_count("financials") == 2
    assert finance.calls == ["financials"]


def test_valuation_job_uses_the_latest_stored_day(db, settings):
    ValuationRepo(db).upsert(valuation([date(2024, 10, 24)]))
    finance = StubFundamentalsAdapter(["A"], valuation=valuation([date(2024, 10, 24), AS_OF]))
    outcome = run_valuation(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.rows_affected == 2
    assert ValuationRepo(db).latest_date("A", "601088") == AS_OF


def test_flow_job_uses_the_latest_stored_day(db, settings):
    # 库里资金流停在 24 号 ⇒ 窗口从 25 号起（读的是 flow.latest_date）；桩无视传入区间，
    # 所以把按日估值那一列也铺到 AS_OF：若 run_flow 误读 valuation.latest_date，窗口会算成
    # 「已追平」而写 0 行——rows_affected 与 flow 最新日两处都会红。
    FundingFlowRepo(db).upsert(flow_rows([date(2024, 10, 24)], net=1.0e8))
    ValuationRepo(db).upsert(valuation([AS_OF]))
    finance = StubFundamentalsAdapter(
        ["A"], flow=flow_rows([date(2024, 10, 24), AS_OF], net=1.0e8)
    )
    outcome = run_flow(deps(db, settings, finance=finance), ctx("601088"))
    assert outcome.rows_affected == 2
    assert FundingFlowRepo(db).latest_date("A", "601088") == AS_OF


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
    # 覆盖确认已经说到「全历史且到今天」，run_actions 直接跳过、不再问链：
    # 链上现在有能给公司行为的源，若不跳过就会真取回一行并写进库——row_count 为 0 才算跳过
    assert outcome.errors == ()
    assert outcome.rows_affected == 0
    assert db.row_count("corp_actions") == 0


def test_build_jobs_registers_only_markets_with_capability(db, settings):
    only = replace(
        settings, markets={k: v for k, v in settings.markets.items() if k in ("A", "HK")}
    )
    jobs = build_jobs(deps(db, only))
    registered = {(job.name, job.market) for job in jobs}
    # A：akshare 给日线与公司行为，eastmoney 给基本面/估值/资金流——五种全登记
    assert ("bars", "A") in registered
    assert ("actions", "A") in registered
    assert ("financials", "A") in registered
    assert ("valuation", "A") in registered
    assert ("flow", "A") in registered
    # HK：yfinance 给日线与公司行为，没有任何基本面源 ⇒ 后三种不登记
    assert ("bars", "HK") in registered
    assert ("actions", "HK") in registered
    assert ("financials", "HK") not in registered
    assert ("valuation", "HK") not in registered
    assert ("flow", "HK") not in registered


def test_build_jobs_skips_a_market_without_any_source(db, settings):
    """库里一个启用源都没有的市场：注册表这里不许抛。

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
「另外七十只已经入库」这件事消失。所以失败与行数并存于同一个结果里——
读它的一侧（`task_runs` 的一行）要能同时看到「写了 70 行」和「3 只失败」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sams.core.config import Settings
from sams.services.calendar import CalendarService
from sams.services.corp_actions import CorpActionSyncService
from sams.services.fundamentals import FundamentalSyncService
from sams.services.fx import FxSyncService
from sams.services.rules.engine import RuleEngine
from sams.services.rules.recheck import Rechecker
from sams.services.scheduler.universe import Universe
from sams.services.sync import SyncService
from sams.storage.repos.users import UserRepo


class RegistryView(Protocol):
    """调度器对数据源层只问一句话：「这个市场这条能力链上有谁」（`chain_for`）。

    与 `due.py` 的 `CalendarView` 同一做法——`AdapterRegistry` 恰好实现了这一个方法，
    这里不写死那个类。Protocol 真正买到的只有两件事：types/jobs 不 import 数据源层，
    以及它们向数据源层问的只有 `chain_for` 这一句。它不构成取数禁令——每个服务都把
    `registry` 存成了公开属性，`deps.sync.registry.fetch_*` 仍然够得着；拦在取数之外靠的是
    「job 一律走 services」这条约定，不是这个 Protocol。
    """

    def chain_for(self, market: str, method: str) -> tuple[str, ...]: ...


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
    registry: RegistryView
    sync: SyncService
    corp_actions: CorpActionSyncService
    fundamentals: FundamentalSyncService
    fx: FxSyncService
    calendar: CalendarService
    rules: RuleEngine
    recheck: Rechecker
    users: UserRepo
```

- [ ] **Step 5: 写 `jobs.py` 的五个数据 job 与注册**

`src/sams/services/scheduler/jobs.py`：

```python
"""数据 job 的功能本体：只把已有服务拼成一条链，不决定自己何时跑。

每个 job 覆盖一个市场的一批对象，**按只隔离**：一只票的链断了，剩下的照常入库，
断的那几只进 `JobOutcome.errors`，与「已经写进行数」并存于同一个结果里。
不按只隔离的后果是「一只停牌票让整个市场今晚没有数据」，而它看起来像一次正常失败。

窗口规则：

* 日线/按日估值/资金流：库里有最新一天就从下一天起，没有就回补 730 天；
* 公司行为：除非已有「全历史且覆盖到今天」的确认，否则从 1990 年起；
* 财务：每次都从 1990 年起——报告期数据是重述的，增量窗口会漏掉旧期的修正。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from functools import partial

from sams.core.errors import DatasourceError, SamsError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.due import DAILY
from sams.services.scheduler.types import (
    JobContext,
    JobDeps,
    JobOutcome,
    JobSpec,
)

#: 首次见到一只票时回补多久：对齐 PR 分位的样本地板 `MIN_SAMPLES = 250`
#: （`analysis/valuation/percentile.py`：少于它，「分位说的是抽样噪声而不是估值位置」）。
#: 730 个日历日是一个交易日年的约两倍，给足余量；但不保证一定凑够 250 个交易日——
#: 长期停牌的票回补下来仍可能不足，那种票本就不该有分位。
INITIAL_SYNC_DAYS = 730


def _window(latest: date | None, as_of: date) -> date | None:
    """增量窗口的起点；返回 None 表示已经追平到今天，没什么可问。"""
    start = latest + timedelta(days=1) if latest else as_of - timedelta(days=INITIAL_SYNC_DAYS)
    return None if start > as_of else start


def _units(ctx: JobContext) -> list[tuple[str, str]]:
    assert ctx.market is not None  # 数据 job 只在市场 job 上登记
    return [(ctx.market, code) for code in ctx.universe.codes(ctx.market)]


def _collect(
    units: list[tuple[str, str]], action: Callable[[str, str], int], label: str
) -> JobOutcome:
    rows = 0
    done = 0
    errors: list[str] = []
    for market, code in units:
        try:
            rows += action(market, code)
        except SamsError as exc:
            # 只接本项目的失败：一个编程错误（AttributeError 之类）不许在这里被接住，
            # 否则它出门的那一句就是「这只票取不到」，而该修的那行代码永远没人去修
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
        return deps.corp_actions.sync_actions(market, code, HISTORY_START, ctx.as_of).written

    return _collect(_units(ctx), one, "公司行为")


def run_financials(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        return deps.fundamentals.sync_financials(market, code, HISTORY_START, ctx.as_of).written

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

    整个市场一个源都没配上时 `sources_for` 会抛 `DatasourceError`；
    「整块没配」与「缺这一个能力」对注册表来说是同一个答案：不登记这个 job。
    启动不该因为一个市场没配源就整个失败——健康页的降级链那一节才是它该出声的地方。
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

**评审收尾（2026-10-22）**：实现落在 `67362ef`，两路评审之后修在 `817352c`。评审发现的问题与处理：

- **计划里的 `types.py` 会造出第二个组装根**（`67362ef` 的偏差，已确认是对的解法）。
  `tests/test_layering.py::test_bootstrap_is_the_only_assembly_root` 禁止任何非 bootstrap
  模块同时 import storage + datasource + services，而计划的 `JobDeps` 三者齐全（`Clock`/
  `Settings` 之外还有 `AdapterRegistry`、`UserRepo` 与五个服务）。落地的解法是本地
  `RegistryView` Protocol 只声明 `chain_for`，与 `due.py` 的 `CalendarView` 同一做法；
  计划片段已按落地形状替换。往后 Task 7/8/10 若也要在同一个类型里握数据源层，走的仍是这条路。
- **删掉三个没人读的东西**（`817352c`）：`types.PERIODS`（`due.py:29` 那份才是有人读的，
  `due.py:157` 的报错引用它）、`JobDeps.universe`（`Scheduler` 自带 builder，见本计划
  Step「Scheduler」里的 `self.universe.build()`）、`JobDeps.clock`（runner 与 scheduler
  各自持有时钟）。**Task 7 要注意**：计划的运维 job 用例仍写着 `clock=FrozenClock(...)`
  与 `universe=None`，那两个槽位已经不在了。
- **`run_flow` 一条用例都没有**，登记表也只从正向没认过 `actions`：原来那句
  `assert not any(job.name == "actions")` 抓不到「把 `_DATA_JOBS` 里那一行删掉」——这条链
  本来就没有能给公司行为的源，删了那行照样全绿。现在 `RecordingBarsAdapter` 带
  `corp_actions`，A 的五种能力逐条正向断言，HK 断言「bars/actions 在、financials/valuation/
  flow 不在」；把 `run_flow` 里的 `flow` 换成 `valuation`、`sync_flow` 换成 `sync_valuation`
  都会红。
- **测试桩不再冒充真实源没有的能力**：`akshare` 在真实代码里只覆盖 A 股
  （`akshare_cn.py:363-364`），港股日线来自 `yfinance`（`yfinance_us.py:256-257`，它确实
  同时提供 `daily_bars` 与 `corp_actions`）。原来那个名叫 `akshare`、`markets={"A","HK"}`
  的桩把假能力排进了 HK 的链。现在桩按实例接 `name`/`markets`，HK 走 `yfinance`。
- **D96 四处过头话**：`_has` 指向的「健康页的数据源那一节」是错的，「没有任何可用源」
  印在 `[降级链]` 下（`cli/health.py:181-190`），`[数据源]` 那节只报每个源的健康；
  `RegistryView` 原来写「调度器拿不到 `fetch_*`」——每个服务都把 `registry` 存成公开属性
  （`services/sync.py:48`、`fundamentals.py:76`、`corp_actions.py:60`、`fx.py:50`），
  改成只说 Protocol 真正买到的两件事；`INITIAL_SYNC_DAYS` 原来断言「两年足够覆盖
  250 个交易日」，长期停牌的票回补下来可能不足，改成点名 `MIN_SAMPLES = 250`
  （`analysis/valuation/percentile.py:38`）并说明不保证；模块 docstring 的「九个 job」
  与「runner 记 failed / 10 分钟后重试」「装配已经警告过」都是本轮还没有的东西。
- **没采纳的建议与理由**：把 `JobDeps` 拆成 Data/Ops 两半——删完上面三个字段之后剩下的
  每个字段在 Task 7 都有读者，拆开只是把 `# type: ignore` 换个地方放；给 `_collect` 加
  `word=`/`sep=` 好让 `run_fx` 复用——第二个调用方还不存在，等 Task 7 真写出来再看要不要抽；
  把 `AS_OF`/`ctx(...)` 挪进 `tests/support_scheduler.py`——Task 7 的用例用的是
  `SimpleNamespace` 替身，现在共享只会共享一半。
- **留下的两处不干净**：`StubFundamentalsAdapter` 不记问出去的区间，所以 flow/valuation
  两条用例只能行为上钉窗口（把按日估值也铺到 `AS_OF`，读错列就写 0 行）；`deps(...)` 里
  `fx/calendar/rules/recheck/users` 仍传 `None` + `# type: ignore[arg-type]`，等 Task 7
  的运维 job 用例真把它们装上才消得掉。

### Task 7: 四个运维 job + 引擎/复检的市场过滤

**Files:**
- Modify: `src/sams/services/scheduler/jobs.py`（追加 fx/calendar/rules/recheck 与最终 `build_jobs`）
- Modify: `src/sams/services/rules/engine.py`（`check(..., market=None)`，过滤之前先归一市场键）
- Modify: `src/sams/services/rules/recheck.py`（`run(..., market=None)`，同上）
- Modify: `src/sams/services/actors.py`（`active_users`：按名单跑的那两头只认启用的账号）
- Modify: `src/sams/storage/repos/fx.py`（`latest_date`）
- Test: `tests/services/test_scheduler_jobs_ops.py`
- Test: `tests/services/test_rules_engine.py`
- Test: `tests/services/test_rules_recheck.py`
- Test: `tests/storage/test_fx_repo.py`

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_jobs_ops.py`（整份，落地形状）：

```python
"""四个运维 job 各自的那句话：汇率按对、日历按年、规则与复检按用户。

这里的断言看的是**问出去的对象与窗口**，不是写回来的行数：一个不问库里已有哪一天的
汇率 job 也能把今天的报价放进库，只是每晚把所有币种对重拉一遍；一个不传市场的规则 job
也能触发告警，只是 A 股那一趟顺手把美股也判了——两个市场各自的条件本该互不牵连。
「互不牵连」在四个 job 上是同一件事：坏一个（一对/一个用户）时，其余那一个照常做完，
而坏的那一个进 `errors` 而不是把整趟带走，所以每一类都钉正反两头。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from support import FrozenClock
from support_scheduler import WeekdayProvider

from sams.core.errors import CalendarSourceError, DatasourceError, ValidationError
from sams.datasource.registry import AdapterRegistry
from sams.services.calendar import CalendarService
from sams.services.fx import FxSyncService
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
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
    """按币种对回答的汇率源：给自己那几天报价，`fail_on` 那一对挂掉。

    它对「问哪一对」都答得上来（`fail_on` 之外），所以按对隔离那一枚要的效果是真的：
    坏掉的那一对之外，其余那一对仍然被问过、仍然写了行。
    `markets` 给空元组不是「这个源哪个市场都不吃」：`fetch_fx` 不走市场路由，
    它按能力遍历注册表里的每一个源——汇率这一条链没有市场维度（`services/fx.py` 的文件头）。
    """

    name = "stubfx"
    markets: tuple[str, ...] = ()

    def __init__(
        self, *, days: tuple[date, ...] = (AS_OF,), fail_on: tuple[str, str] | None = None
    ) -> None:
        self.days = days
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, date, date]] = []

    def fx_rate(self, base, quote, start, end):
        self.calls.append((base, quote, start, end))
        if (base, quote) == self.fail_on:
            raise DatasourceError(self.name, "503 服务不可用")
        # 只回被问到的那一段：源给宽区间是常态，`_call_fx` 那一头会裁掉问之外的日子
        days = [day for day in self.days if start <= day <= end]
        return pl.DataFrame(
            {
                "date": days,
                "base": [base] * len(days),
                "quote": [quote] * len(days),
                "rate": [7.1] * len(days),
                "source": [self.name] * len(days),
            }
        )


def deps(db, settings, *, fx=None, calendar=None, rules=None, recheck=None, users=None):
    """三个数据服务的槽位这里是 None——本文件不跑数据 job。

    注册表给空的那一套：`build_jobs` 因此一个数据 job 都不登记，注册表那一枚用例
    断的就只有这四个运维 job。
    """
    return JobDeps(
        settings=settings,
        registry=AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _: None),
        sync=None,  # type: ignore[arg-type]
        corp_actions=None,  # type: ignore[arg-type]
        fundamentals=None,  # type: ignore[arg-type]
        fx=fx,
        calendar=calendar,
        rules=rules,
        recheck=recheck,
        users=users or UserRepo(db),
    )


def ctx(*, market="A", pairs=()):
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market=market,
        universe=Universe(by_market={market: ()} if market else {}, fx_pairs=pairs),
    )


def fx_service(db, adapter):
    registry = AdapterRegistry(
        adapters={"stubfx": adapter}, market_sources={}, sleep=lambda _: None
    )
    return FxSyncService(registry, FxRepo(db))


def _boom(*_args, **_kwargs):
    """三个槽位共用的一种替身：它抛的不是本项目的失败。

    「只接 `SamsError`」这句在四段各写一遍的循环里，所以每一段都要在自己那一头钉一枚：
    把其中一处放宽成 `except Exception`，其余三处不该跟着红，而那一处也不该绿着过。
    """
    raise TypeError("这一层收到了没有键的一行")


# --- 汇率：窗口按每对各自问，失败按对隔离 --------------------------------------------------


def test_fx_job_starts_after_the_last_stored_day(db, settings):
    FxRepo(db).put(date(2024, 10, 24), "USD", "CNY", 7.0, source="manual")
    adapter = StubFxAdapter()
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    assert adapter.calls == [("USD", "CNY", date(2024, 10, 25), AS_OF)]
    assert outcome.rows_affected == 1
    assert FxRepo(db).latest_date("USD", "CNY") == AS_OF


def test_fx_job_backfills_the_same_window_as_the_daily_jobs_for_a_new_pair(db, settings):
    """没存过的一对走的是与日线同一条回补规则（`_window`），不是「从今天开始同步」。

    断的是问出去的区间：只写今天那一行的 job 也能把今天的报价放进库，
    而这一对从此永远缺着前面那两年。
    """
    adapter = StubFxAdapter(days=(AS_OF - timedelta(days=730), AS_OF))
    run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    _, _, start, end = adapter.calls[0]
    assert (start, end) == (AS_OF - timedelta(days=730), AS_OF)


def test_fx_job_skips_a_pair_already_at_as_of(db, settings):
    FxRepo(db).put(AS_OF, "USD", "CNY", 7.1, source="manual")
    adapter = StubFxAdapter()
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    # 追平到今天就不该问链：每晚重问一次同一个数，症状是「一切正常」
    assert adapter.calls == []
    assert outcome.rows_affected == 0 and outcome.errors == ()


def test_a_pair_list_that_is_empty_is_a_successful_noop(db, settings):
    """没人持有外币的那一晚：0 对既不是失败，也不该被读成「汇率链坏了」。

    数据那一头已经钉过同一条（`test_empty_universe_is_a_successful_noop`），
    但那是另一段循环，所以这里也要一枚。
    """
    adapter = StubFxAdapter()
    outcome = run_fx(deps(db, settings, fx=fx_service(db, adapter)), ctx(market=None))
    assert adapter.calls == []
    assert outcome.errors == () and outcome.rows_affected == 0
    assert outcome.note == "0/0 对汇率"


def test_one_bad_pair_does_not_drop_the_other(db, settings):
    FxRepo(db).put(date(2024, 10, 24), "HKD", "CNY", 0.91, source="manual")
    adapter = StubFxAdapter(fail_on=("USD", "CNY"))
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"), ("HKD", "CNY"))),
    )
    assert [call[:2] for call in adapter.calls] == [("USD", "CNY"), ("HKD", "CNY")]
    assert len(outcome.errors) == 1 and "USD→CNY" in outcome.errors[0]
    assert outcome.rows_affected == 1  # 坏那对之外的 HKD 照常被问到并落了库
    assert FxRepo(db).latest_date("HKD", "CNY") == AS_OF
    assert outcome.note == "1/2 对汇率，1 对失败"


def test_a_programming_error_is_not_collected_as_a_failed_pair(db, settings, monkeypatch):
    """和五个数据 job 同一条规矩：只接本项目的失败。

    被接住的编程错误出门时说的是「这一对取不到」，而该修的那行代码永远没人去修。
    汇率这一头是自己的那套循环，所以这条也得在这里单独钉一次：分叉是两次改动的事。
    """

    def boom(self, base, quote):
        raise TypeError("latest_date 收到了没有键的一行")

    monkeypatch.setattr(FxRepo, "latest_date", boom)
    with pytest.raises(TypeError):
        run_fx(
            deps(db, settings, fx=fx_service(db, StubFxAdapter())),
            ctx(market=None, pairs=(("USD", "CNY"), ("HKD", "CNY"))),
        )


# --- 日历：12 月顺手把下一年铺好 -----------------------------------------------------------


def calendar_service(db, *, day: datetime) -> CalendarService:
    return CalendarService(
        CalendarRepo(db),
        {"A": WeekdayProvider("A", "Asia/Shanghai")},
        clock=FrozenClock(day),
    )


def december_ctx(market="A") -> JobContext:
    return JobContext(
        period_key="2024-12",
        as_of=date(2024, 12, 20),
        market=market,
        universe=Universe(by_market={market: ()}, fx_pairs=()),
    )


def test_calendar_job_refreshes_december_next_year_too(db, settings):
    calendar = calendar_service(db, day=datetime(2024, 12, 20, 15, tzinfo=UTC))
    outcome = run_calendar(deps(db, settings, calendar=calendar), december_ctx())
    assert outcome.errors == ()
    assert CalendarRepo(db).year_covered("A", 2024)
    assert CalendarRepo(db).year_covered("A", 2025)


def test_calendar_job_outside_december_leaves_next_year_alone(db, settings):
    """只有 12 月那一趟才顺手铺下一年：十月铺它，一年十二次里十一次都是白问来源。"""
    calendar = calendar_service(db, day=datetime(2024, 10, 25, 15, tzinfo=UTC))
    outcome = run_calendar(deps(db, settings, calendar=calendar), ctx())
    assert outcome.errors == ()
    assert CalendarRepo(db).year_covered("A", 2024)
    assert not CalendarRepo(db).year_covered("A", 2025)


class NextYearBlind(WeekdayProvider):
    """下一年那一问挂掉的日历源：当月这一次刷新不该被它带走。"""

    def days_for_year(self, year: int):
        if year > 2024:
            raise CalendarSourceError(f"{self.market} {year} 年没有日历来源")
        return super().days_for_year(year)


def test_calendar_job_next_year_failure_keeps_the_current_year(db, settings):
    calendar = CalendarService(
        CalendarRepo(db),
        {"A": NextYearBlind("A", "Asia/Shanghai")},
        clock=FrozenClock(datetime(2024, 12, 20, 15, tzinfo=UTC)),
    )
    outcome = run_calendar(deps(db, settings, calendar=calendar), december_ctx())
    assert CalendarRepo(db).year_covered("A", 2024)
    assert not CalendarRepo(db).year_covered("A", 2025)
    assert outcome.rows_affected > 0  # 当年那一整年的行还在账上
    assert len(outcome.errors) == 1 and "2025" in outcome.errors[0]


def test_a_programming_error_is_not_collected_as_a_failed_year(db, settings):
    with pytest.raises(TypeError):
        run_calendar(deps(db, settings, calendar=SimpleNamespace(refresh=_boom)), ctx())


# --- 规则与复检：市场传下去，用户互不牵连 --------------------------------------------------


class RecordingRules:
    """`RuleEngine.check` 的替身：记下问出去的 `(user_id, as_of, market)`。

    报告用 `SimpleNamespace` 而不是真 `RunReport`：这一层只读报告里那几摞的长度，
    真报告的形状由引擎自己的用例钉，装在这里只会让引擎加一摞时两边一起改。
    """

    def __init__(self, *, fail_for: str | None = None) -> None:
        self.calls: list[tuple[str, date | None, str | None]] = []
        self.fail_for = fail_for

    def check(self, user_id, *, as_of=None, market=None, dry_run=False, rule_id=None):
        self.calls.append((user_id, as_of, market))
        if user_id == self.fail_for:
            raise ValidationError(f"{user_id} 这一趟取不到")
        return SimpleNamespace(raised=(1, 2), suppressed=(3, 4, 5), skipped=(6,), blocked=(7,))


class RecordingRecheck:
    def __init__(self, *, fail_for: str | None = None) -> None:
        self.calls: list[tuple[str, date | None, str | None]] = []
        self.fail_for = fail_for

    def run(self, user_id, *, as_of=None, dry_run=False, thesis_id=None, market=None):
        self.calls.append((user_id, as_of, market))
        if user_id == self.fail_for:
            raise ValidationError(f"{user_id} 这一趟读不到")
        return SimpleNamespace(rows=(1, 2, 3))


def one_user(db):
    users = UserRepo(db)
    return users, users.create("alice", "correct horse battery")


def two_users(db):
    users, alice = one_user(db)
    return users, alice, users.create("bob", "correct horse battery")


def test_rules_job_asks_for_that_market_only(db, settings):
    users, alice = one_user(db)
    recorder = RecordingRules()
    outcome = run_rules(deps(db, settings, rules=recorder, users=users), ctx())
    assert recorder.calls == [(alice.id, AS_OF, "A")]
    assert outcome.rows_affected == 2
    # 四摞各是一个数：合成一个「异常数」就分不清「压住」「判不了」「展开失败」
    # （`suppressed` 那一摞是过滤器与冷却合起来的，所以那句不写「冷却」）
    assert outcome.note == "1/1 个用户：触发 2、压住 3、判不了 1、阻塞 1"


def test_one_bad_user_does_not_drop_the_others_in_rules(db, settings):
    users, alice, bob = two_users(db)
    recorder = RecordingRules(fail_for=bob.id)
    outcome = run_rules(deps(db, settings, rules=recorder, users=users), ctx())
    # 挂掉那一个也真被问过（失败来自这一趟的取数，不是没排上队）
    assert [call[0] for call in recorder.calls] == [alice.id, bob.id]
    assert len(outcome.errors) == 1 and "bob" in outcome.errors[0]
    assert outcome.rows_affected == 2  # alice 那两条触发还在
    assert outcome.note == "1/2 个用户：触发 2、压住 3、判不了 1、阻塞 1，1 个用户失败"


def test_a_disabled_user_is_not_evaluated_or_delivered_to(db, settings):
    """`sams user disable` 说的是「停止一切动作」，而夜里跑的是这一条链。

    投递那一头（`router.dispatch`）只查「有没有这个用户」，不查状态：不在这里挡，
    禁用就只挡住了手敲命令的那一路。采集那一头相反（`universe.py` 的文件头），
    所以这一枚钉的是「按用户做事」的那两头之一，另一头是复检。
    """
    users, alice = one_user(db)
    disabled = users.create("bob", "correct horse battery")
    users.disable(disabled.id)
    recorder = RecordingRules()
    outcome = run_rules(deps(db, settings, rules=recorder, users=users), ctx())
    assert recorder.calls == [(alice.id, AS_OF, "A")]
    assert outcome.note == "1/1 个用户：触发 2、压住 3、判不了 1、阻塞 1"


def test_recheck_job_asks_for_that_market_only(db, settings):
    users, alice = one_user(db)
    recorder = RecordingRecheck()
    outcome = run_recheck(deps(db, settings, recheck=recorder, users=users), ctx())
    assert recorder.calls == [(alice.id, AS_OF, "A")]
    assert outcome.rows_affected == 3
    assert outcome.note == "1/1 个用户复检 3 条观点"


def test_one_bad_user_does_not_drop_the_others_in_recheck(db, settings):
    users, alice, bob = two_users(db)
    recorder = RecordingRecheck(fail_for=bob.id)
    outcome = run_recheck(deps(db, settings, recheck=recorder, users=users), ctx())
    assert [call[0] for call in recorder.calls] == [alice.id, bob.id]
    assert len(outcome.errors) == 1 and "bob" in outcome.errors[0]
    assert outcome.rows_affected == 3
    assert outcome.note == "1/2 个用户复检 3 条观点，1 个用户失败"


def test_recheck_job_writes_no_baseline_for_a_disabled_user(db, settings):
    """复检那一头不是投递，是写库：同一条规矩要有第二枚，因为它坏的是另一种东西。"""
    users, alice = one_user(db)
    disabled = users.create("bob", "correct horse battery")
    users.disable(disabled.id)
    recorder = RecordingRecheck()
    outcome = run_recheck(deps(db, settings, recheck=recorder, users=users), ctx())
    assert recorder.calls == [(alice.id, AS_OF, "A")]
    assert outcome.note == "1/1 个用户复检 3 条观点"


def test_a_programming_error_is_not_collected_as_a_failed_user_in_rules(db, settings):
    users, _ = one_user(db)
    with pytest.raises(TypeError):
        run_rules(deps(db, settings, rules=SimpleNamespace(check=_boom), users=users), ctx())


def test_a_programming_error_is_not_collected_as_a_failed_user_in_recheck(db, settings):
    users, _ = one_user(db)
    with pytest.raises(TypeError):
        run_recheck(deps(db, settings, recheck=SimpleNamespace(run=_boom), users=users), ctx())


# --- 注册表：顺序就是执行顺序 --------------------------------------------------------------


def built(db, settings):
    return build_jobs(
        deps(
            db,
            settings,
            fx=SimpleNamespace(),
            calendar=SimpleNamespace(),
            rules=SimpleNamespace(),
            recheck=SimpleNamespace(),
        )
    )


def test_build_jobs_puts_recheck_before_rules_and_fx_after_every_market(db, settings):
    titles = [job.title for job in built(db, settings)]
    assert titles.index("recheck@A") < titles.index("rules@A")
    assert titles.index("rules@US") < titles.index("fx@全局")
    assert titles.index("fx@全局") < titles.index("calendar@A")


def test_build_jobs_registers_the_ops_jobs_on_every_market_with_their_period(db, settings):
    """四个运维 job 每一个都要有人登记，而且按各自的周期：少一行、或周期写错一格，这一枚就该红。

    写错一格不是笔误那么轻：`fx` 挂到 DAILY 上，`due_for` 对「全局 job（market=None）」直接答
    None，于是它一个晚上都不会跑——而注册表看起来是完整的。
    """
    by_title = {job.title: job for job in built(db, settings)}
    for market in sorted(settings.markets):
        assert by_title[f"recheck@{market}"].period == WEEKLY
        assert by_title[f"recheck@{market}"].market == market
        assert by_title[f"rules@{market}"].period == DAILY
        assert by_title[f"calendar@{market}"].period == MONTHLY
    assert by_title["fx@全局"].period == FX
    assert by_title["fx@全局"].market is None
```

`tests/services/test_rules_engine.py` 追加：

```python
def test_market_filter_leaves_other_markets_alone(h) -> None:
    """调度器每个市场单独跑一趟：不是这个市场的对象一个都不许判。

    两头都钉：`market="HK"` 时那条 A 股规则既不响也不投递，`market="A"` 时照旧响。
    没有这层过滤的代价是每一趟都把全部对象重读一遍，而第二趟响不响只剩冷却一道闸——
    冷却的键是 `(rule, market, code)`，那个 `market` 是**对象自己的**市场，同一只票
    在哪一趟都是同一个键：挡不挡得住取决于窗口（`cooldown_min=0` 就是重复投递），
    不取决于这一趟跑的是哪个市场。
    `rules` 仍是 1——那个数问的是「这一趟看了几条规则」，不是「看了几个对象」，
    跟着对象数缩会让屏幕读不出「规则被过滤了」还是「压根没规则」。
    """
    h.add()
    report = h.engine().check(USER, market="HK")
    assert report.rules == 1
    assert report.raised == () and h.sender.sent == []
    # 五摞出口全空，而且「一个都没读」是断得出来的：判定要问日历，连一次都没问就是真没读
    assert (report.suppressed, report.skipped, report.blocked, report.quiet) == ((), (), (), 0)
    assert report.judged == 0 and h.calendar.day_calls == []
    assert h.engine().check(USER, market="A").raised
    # 大小写在这里不是排版：配置里那个市场键原样留着，而库里存的是大写。
    # 不归一的话 `"a"` 那一趟会挑出零个对象，出门说的还是「触发 0」
    assert h.engine().check(USER, market="a").raised
```

`tests/services/test_rules_recheck.py` 追加：

```python
def test_market_filter_skips_theses_from_other_markets(container) -> None:
    """调度器每个市场单独跑一趟复检：不属于这个市场的论一条都不许读。

    点名一条 A 股的论又限定 HK，两句拼不成一句——这里必须出声（静默返回零行会被读成
    「HK 那一趟复检过了」），而 `market=None` 那一头其余用例照旧全比，CLI 不必改。
    """
    thesis = open_thesis(container)
    rec = build(container)
    assert rec.run(USER, as_of=AS_OF, market="HK").rows == ()
    assert len(rec.run(USER, as_of=AS_OF, market="A").rows) == 1
    # 与引擎同一件事：库里存大写，配置里的键原样留着，`"a"` 不归一就是「0 条观点」
    assert len(rec.run(USER, as_of=AS_OF, market="a").rows) == 1
    with pytest.raises(ValidationError, match="不是 HK"):
        rec.run(USER, as_of=AS_OF, thesis_id=thesis.id, market="HK")
```

`tests/storage/test_fx_repo.py` 追加：

```python
def test_latest_date_answers_even_when_that_rate_is_stale(fx: FxRepo) -> None:
    """增量窗口问的是「这一对还缺哪段」，不是「那个数还能不能用」——后者是 `latest` 的问题。

    拿 `latest` 的定窗口，一行三周没动的汇率会被答成「库里什么都没有」，于是每一趟都
    从回补起点把那段重拉一遍；窗口那条线存在的理由正是别再重拉，所以这里不看陈旧度。
    """
    old = DAY - timedelta(days=30)
    assert fx.latest_date("USD", "CNY") is None
    fx.put(old, "USD", "CNY", 7.1, source="manual")
    assert fx.latest_date("USD", "CNY") == old
    assert fx.latest(DAY, "USD", "CNY") is None  # 同一个数在 `latest` 那一头是不可用
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_jobs_ops.py tests/services/test_rules_engine.py tests/services/test_rules_recheck.py tests/storage/test_fx_repo.py -q
```

Expected: FAIL（四个 job 不存在；`market=` 不被接受；`FxRepo.latest_date` 不存在）。

- [ ] **Step 3: 实现 `FxRepo.latest_date`**

`src/sams/storage/repos/fx.py` 在 `latest` 下方加：

```python
    def latest_date(self, base: str, quote: str) -> date | None:
        """这一对已经存到哪一天，**不看陈旧度**：job 的增量窗口问的是「还缺哪段」，
        不是「这个数还能不能用」——后者是 `latest(..., max_age_days=)` 的问题。
        拿 `latest` 定窗口，一行三周没动的汇率会被当成「库里什么都没有」。
        """
        value = self.db.scalar(
            "SELECT max(date) FROM fx_rates WHERE base_ccy = ? AND quote_ccy = ?",
            [base, quote],
        )
        if value is None:
            return None
        return value.date() if isinstance(value, datetime) else value
```

- [ ] **Step 4: 实现四个运维 job 与最终注册表**

`src/sams/services/actors.py` 加这一头（按名单跑的循环只认启用的账号；`sams user disable`
挡住的是手敲命令那一路，夜里跑的是这一条链）：

```python
def active_users(users: UserRepo) -> list[User]:
    """替一批用户做事的那一趟，只认启用的账号。

    `resolve_actor` 挡的是屏幕上传进来的那一个名字，而常驻的盘后链按名单跑，
    那一条路上没有任何入口经过 `resolve_actor`：不在这里挡，`sams user disable`
    就只挡住了手敲命令的那一头。
    采集那一头相反（见 `services/scheduler/universe.py` 的文件头）：停用的用户照拉对象，
    数据是公共资源，明天启用时库里该已经有数。
    """
    return [user for user in users.list_users() if user.status == ACTIVE]
```

`src/sams/services/scheduler/jobs.py` 在 `run_flow` 之后追加：

```python
def run_fx(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """汇率没有市场维度：对象来自账户的币种对，窗口按每对各自的库内最新日。

    不并进 `_collect` 是因为那一头的单位是 `(market, code)` 那一只票：量词「只」和失败
    那句的前缀都是按它写的，而这里的单位是「一对」。接住的仍然只有 `SamsError`——
    按对隔离与按只隔离是同一条规矩，但那份规矩是两处各写一遍的，所以四个运维 job
    每一个都在自己那一头单独钉一枚：放宽一处不该让其余三处跟着红。
    """
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
    """每月一次，每次把 `as_of` 那一年整年重铺；12 月顺手把下一年也铺上
    （D109：跨年那天之前要有一整年的日历）。

    按年隔离：下一年那条链挂了不该把当年已经刷到的行一起报没了——行数照收，
    坏的那一年单独进 `errors`。
    """
    assert ctx.market is not None  # 日历 job 只在市场 job 上登记
    years = [ctx.as_of.year]
    if ctx.as_of.month == 12:
        years.append(ctx.as_of.year + 1)
    rows = 0
    errors: list[str] = []
    for year in years:
        try:
            rows += deps.calendar.refresh(ctx.market, year)
        except SamsError as exc:
            errors.append(f"{ctx.market} {year} 年日历: {exc}")
    note = f"刷新 {'/'.join(map(str, years))} 年日历"
    if errors:
        note += f"，{len(errors)} 年失败"
    return JobOutcome(rows, note, tuple(errors))


def run_rules(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """只评估这个市场的对象；每用户一趟，一个用户失败不带走其余（`errors` 与行数并存）。

    这一趟的出口是投递（`engine._dispatch` → 通知通道），所以只跑启用的账号：
    `router.dispatch` 只查「有没有这个用户」，不查状态。采集那一头不过滤，理由在
    `universe.py` 的文件头。

    `suppressed`（过滤器与冷却合起来拦下的，不是一个「冷却」数）、`skipped`（判不了）、
    `blocked`（展开失败）各留一个数：合成一个「异常数」就分不清那三件事，处置的话也不同。
    判过了没到的 `quiet` 不进 note：那一摞说的是「没到」，而这一屏问的是
    「到了什么、有什么拦着」。
    """
    users = active_users(deps.users)
    raised = suppressed = skipped = blocked = done = 0
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
        blocked += len(report.blocked)
        done += 1
    note = (
        f"{done}/{len(users)} 个用户：触发 {raised}、压住 {suppressed}、"
        f"判不了 {skipped}、阻塞 {blocked}"
    )
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(raised, note, tuple(errors))


def run_recheck(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """周复检按用户隔离，与 `run_rules` 同一条规矩，也只跑启用的账号——它会写基线。

    行数就是这一趟挑出来的论数，里头含「刚建立基线」与「读不到分」那两类：
    它们没有可比的东西，所以这一句不说「比过」。
    """
    users = active_users(deps.users)
    rows = done = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.recheck.run(user.id, as_of=ctx.as_of, market=ctx.market)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        rows += len(report.rows)
        done += 1
    note = f"{done}/{len(users)} 个用户复检 {rows} 条观点"
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(rows, note, tuple(errors))


def build_jobs(deps: JobDeps) -> tuple[JobSpec, ...]:
    """注册顺序就是同一个 tick 里的执行顺序：
    每个市场：数据链 → 周复检 → 规则；然后汇率；最后日历。

    周复检排在规则之前说的是**同一晚之内**：`thesis.drift` 读的是复检写下的
    `baseline_items`，没有它就只拿到「这条论还没有分项基线」那一句。先复检，那条论当晚
    就有基线可读——但当晚比不出掉档：那份基线正是同一趟同一次读取写下的，
    自己和自己不会掉一档。要真比出漂移，得等下一晚的快照与这一晚的基线对上。
    已经有基线的论不受这个顺序影响。
    """
    jobs: list[JobSpec] = []
    for market in sorted(deps.settings.markets):
        for method, name, run in _DATA_JOBS:
            if _has(deps, market, method):
                jobs.append(JobSpec(name, DAILY, market, partial(run, deps)))
        jobs.append(JobSpec("recheck", WEEKLY, market, partial(run_recheck, deps)))
        jobs.append(JobSpec("rules", DAILY, market, partial(run_rules, deps)))
    jobs.append(JobSpec("fx", FX, None, partial(run_fx, deps)))
    for market in sorted(deps.settings.markets):
        jobs.append(JobSpec("calendar", MONTHLY, market, partial(run_calendar, deps)))
    return tuple(jobs)
```

（上面这段以 `def run_fx` 起、到文件末尾为止，含 `build_jobs` 的整份替换。）

- [ ] **Step 5: 市场过滤（引擎与复检）**

`src/sams/services/rules/engine.py` 的 `check` 签名加 `market: str | None = None`。
`rules=len(selected)` 保持不变：这个数回答的是「这一趟看了几条规则」，不是「看了几个对象」。
下面这段截到过滤那块为止，`_one` 那一半没动。

```python
    def check(
        self,
        user_id: str,
        *,
        as_of: date | None = None,
        market: str | None = None,
        dry_run: bool = False,
        rule_id: str | None = None,
    ) -> RunReport:
        """一趟同步求值。调度器的 `rules` job 按市场跑（传 `market`），CLI 那一趟不传，
        而本函数不知道自己被调了几次——一次还是三个市场，是调用方的事。

        `market` 是对象级子集：过滤在展开之后做——规则的目标可以是 `*` 那种
        「我的全部持仓」，展开之前问不出「这里面哪些属于 HK」。大小写先归一（D46）：
        配置里那个键是原样留下的，`"hk"` 不归一就会让每一张规则都「对象全在别的市场」，
        而那一趟出门说的是「触发 0」，与一晚平安长得一模一样。
        """
        now = self.clock.now_utc()
        day = as_of if as_of is not None else self.clock.today()
        if market is not None:
            market = canonical_market(market)
        raised: list[Raised] = []
        suppressed: list[Suppressed] = []
        skipped: list[Skipped] = []
        blocked: list[Blocked] = []
        quiet = 0
        cache = _Cache()
        selected = self._select(user_id, rule_id)
        for rule in selected:
            try:
                targets = self.data.expand(rule)
            except ValidationError as exc:
                blocked.append(Blocked(rule_id=rule.id, rule_name=rule.name, reason=str(exc)))
                continue
            if market is not None:
                targets = [target for target in targets if target.market == market]
                if not targets:
                    # 这条规则的对象全在别的市场：这一趟一个都没读，所以五摞出口
                    # （触发/压制/跳过/阻塞/没到）都不因此动一行——只有 `rules` 那个数
                    # 照旧算它一眼，它问的是「这一趟看了几条规则」而不是「看了几个对象」
                    continue
```

`src/sams/services/rules/recheck.py` 的 `run` 签名加 `market: str | None = None`，
`_select(user_id, thesis_id, market)`：

```python
    def run(
        self,
        user_id: str,
        *,
        as_of: date | None = None,
        dry_run: bool = False,
        thesis_id: str | None = None,
        market: str | None = None,
    ) -> RecheckReport:
        """一趟复检。`market` 是调度器那一头给的：每个市场各跑一趟，别市场的论不进 `_one`
        ——那里头既读行情也可能写基线，一趟 HK 的复检不该碰 A 股的论。
        给定的一头先归一大小写（D46）：`"hk"` 与库里大写的 `HK` 比不出相等，
        那个市场的论就会一条都进不了 `_one`，而屏幕上说的是「复检 0 条观点」。
        """
        day = as_of if as_of is not None else self.clock.today()
        if market is not None:
            market = canonical_market(market)
        targets = self._select(user_id, thesis_id, market)
        rows = tuple(self._one(thesis, day, dry_run) for thesis in targets)
        return RecheckReport(user_id=user_id, as_of=day, dry_run=dry_run, rows=rows)

    def _select(
        self, user_id: str, thesis_id: str | None, market: str | None
    ) -> list[Thesis]:
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
git add src/sams/services/scheduler/jobs.py src/sams/services/actors.py src/sams/services/rules/engine.py src/sams/services/rules/recheck.py src/sams/storage/repos/fx.py tests/services/test_scheduler_jobs_ops.py tests/services/test_rules_engine.py tests/services/test_rules_recheck.py tests/storage/test_fx_repo.py
git commit -m "feat(scheduler): 汇率/日历/规则/复检四个 job 与市场过滤"
```

**评审收尾（2026-10-22）**：实现落在 `6d342c5`，两路评审之后修在 `20bca30`。评审发现的问题与处理：

- **按计划写完会漏掉「禁用」那一半**（BLOCKING，`20bca30`）。计划的 `run_rules`/`run_recheck`
  直接吃 `deps.users.list_users()`，而那个仓库不带状态过滤（`storage/repos/users.py:180`）。
  规则这一头的出口是投递：`engine._dispatch` → `monitor/router.py:118` 只查「有没有这个用户」，
  不查状态。于是 `sams user disable` 只挡住了手敲命令那一路，而夜里跑的正是这条链——
  `actors.py` 的文件头把症状写得很直白（「被停用的账号还在往库里写」）。落地的解法是把这条
  规矩收进 `services/actors.py::active_users`（那里已经有「一个用户」的形状 `resolve_actor`），
  两个 job 都调它；采集那一头仍然不过滤，理由在 `universe.py` 的文件头。
  **往后的任务要注意**：Task 9 的系统告警、Task 11/12 里任何「按名单跑」的都欠这一枚，
  别再去 `list_users()`。
- **市场键不归一会静默判零个对象**（IMPORTANT，`20bca30`）。`Settings.load` 把 `[markets.*]`
  的键原样留下（`core/config.py:74-88`，`validate()` 不查大小写），而 `target.market` 与
  `thesis.market` 是库里的大写键。计划的过滤是 `target.market == market` 直比：`"hk"` 会让
  每一条规则都走进「对象全在别的市场」那一支，出门说的还是「触发 0」。两头都改成先
  `canonical_market(market)`（`services/calendar.py:99` 的 `refresh` 早就自己归一，
  所以日历那一头没这问题）。引擎与复检的用例各加一句 `market="a"`。
- **`suppressed` 被 note 叫成「冷却」是错的**：那一摞是过滤器与冷却合起来拦下的
  （`engine.py:333-336` 那两段 `return self._suppressed(...)`），job 自己的 docstring 也写着
  两件事。按 CLI 那一屏的词改成「压住」，`skipped` 改成「判不了」；分母也统一到 `done/total`
  ——`run_fx` 一直是，规则与复检原本把失败的那一个也算进「N 个用户」。
- **D96 五处过头话**（`20bca30`）：① 引擎用例里那句「响了就等于替美股那一趟把 A 股的告警
  又推一遍，而冷却是按 `(rule, market, code)` 去的，两个市场各推一遍谁也拦不住谁」把因果说反了
  ——`_cooldown` 取的是 `snapshot.market`（`engine.py:444-446`），那是**对象自己的**市场，
  同一只票在哪个市场的趟里都是同一个键：窗口内挡得住，`cooldown_min=0` 才挡不住
  （`core/config.py:109` 只拒负数）。现在改成说真实代价：每一趟都把全部对象重读一遍，
  响不响只剩冷却一道闸。② `build_jobs` 那句「先复检，当晚就比得出漂移」不成立：
  `_lay_baseline` 写的 `baseline_items` 正是同一趟同一次读取的快照，而 `thesis_drift` 要的是
  今天严格低于基线（`templates.py:569`），自己和自己不会掉档。先复检买到的是「那条论当晚
  有基线可读，规则那一头不再只拿到『还没有分项基线』那句」——计划原本的措辞
  「就该读到新的基线」是对的。③ `run_recheck` 的「行数就是这一趟比过的论数」：
  `RecheckReport.rows` 含 `BASELINE`（没比）与 `NO_SCORE`（读不到分）。④ `run_rules` 的
  「一个正常的夜里 `quiet` 占绝大多数」是一句没有任何东西支撑的频率断言，删了。
  ⑤ `run_calendar` 的「当月刷新一次」：`refresh(market, year)` 铺的是整年
  （`services/calendar.py:101-105`），改成「每月一次，每次把那一年整年重铺」。
- **「只接 `SamsError`」原来只在汇率那一头钉过**：规则、复检、日历那三段各是各写的循环，
  把任何一处的 `except SamsError` 放宽成 `except Exception` 都能全绿。现在四段各一枚。
  变异清单（`20bca30`，九次全红）：引擎去归一 / 复检去归一 / `run_rules` 不挡禁用 /
  `run_recheck` 不挡禁用 / 四处 `except SamsError` 各放宽一次 / `run_fx` 空表那句 note 改掉。
- **计划与落地的两处形状差**：计划的 `deps(...)` 还带 `universe=None` 与 `clock=FrozenClock(...)`，
  而 `JobDeps` 那两个字段的槽位在 `817352c` 已经删了；计划的 `build_jobs` 直接写
  `deps.registry.chain_for(market, method)`，Task 6 修的 `_has()`（整个市场没配源时
  `DatasourceError` 不许把注册表炸掉）留着，计划片段已按落地形状替换。`_collect` 没并进这四个
  job：单位不是 `(market, code)`，那份规矩改由四段各自钉。
- **没采纳的建议与理由**：把 `run_fx` 那段循环塞回 `_collect`——要给 `_collect` 加「量词」与
  「错误前缀形状」两个旋钮，而第二个调用方仍然只有它自己；`deps.fx.fx.latest_date` 读起来像
  笔误——那是 `FxSyncService` 本来就公开的属性，与 `deps.sync.bars` 同一个形状，改它属于无关重构。
- **留下的两处不干净**：`run_fx` 里「已经追平到今天」的那一对算进 `done`（那句 note 说的是
  「问过了几对」），没单独钉；四个运维 job 的用例仍给 `sync/corp_actions/fundamentals` 传
  `None` + `# type: ignore[arg-type]`。

---

### Task 8: `JobRunner`——一个 job 的一次执行怎么记账

**Files:**
- Create: `src/sams/services/scheduler/runner.py`
- Modify: `src/sams/storage/repos/task_runs.py`（新增 `latest_per_period`）
- Test: `tests/services/test_scheduler_runner.py`
- Test: `tests/storage/test_task_runs.py`（追加四枚）

三件事只能有一个地方决定（D105）：本周期跑过没有、上一轮留下的行还算不算数、
失败之后多久准重来。写成 runner 而不是写进那九类 job（`_DATA_JOBS` 五类 + 复检/规则/汇率/日历），
是因为它们的失败形状本该一样；写进 job 的话，某一个 job 忘了看 `period_key`，
它就会每 tick 跑一遍。

**这一层的账要数「周期」而不是数「行」**：一个一直失败的周期每 `RETRY_GAP_MIN` 分钟就多写一行，
而 `daily_due` 会把同一个 `period_key` 一路到期到下一个交易日收盘之后。所以按行开的窗口
无论多宽都会被这一晚自己的重试占满，D103 的「连续三个周期」永远凑不满；
分组这件事归仓库（`latest_per_period`），runner 只数它拿到的那几个周期。

- [ ] **Step 1: 写失败用例（runner 那一半）**

`tests/services/test_scheduler_runner.py`：

```python
"""`JobRunner`：记账、重试间隔、连续失败的告警（D103/D105）。

时钟全部由 `FrozenClock` 推进：这些规则说的是「多久之后」，不是「过一会儿」。
每一枚都钉两头——该等的那一轮没跑、等到了的那一轮真跑了，
所以「把间隔写死成零」与「把间隔写成无穷」都能在这里红掉。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from support import FrozenClock

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
    # 行上的市场就是 spec 的市场：漏写 market 的话这一行会落进「全局」那本账
    assert row.market == "A"
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
    assert runs.count() == 1  # 被拦下的那一轮不留行


def test_a_blank_note_is_stored_as_null(db):
    """job 没说话与 job 说了一个空串在库里必须是两种答案。

    `TaskRun.note` 是 `str | None`：写 `""` 进去，读的一侧就分不出「没写」与「写了个空」。
    """

    def job(ctx):
        return JobOutcome(rows_affected=2)

    runner, runs, _, _, spec = harness(db, job)
    result = runner.execute(spec, DUE, UNIVERSE)

    assert result.note == ""
    row = runs.latest_for_period("bars", "A", "2024-10-25")
    assert row.note is None
    assert row.rows_affected == 2


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
    assert runs.count() == 2  # 中间那轮等待同样不留行


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
    # 崩溃那一行原样留着：改写它等于抹掉「那一晚死过一次」的证据
    assert [row.status for row in runs.recent("bars", "A", limit=5)] == [OK, RUNNING]


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


def test_the_alarm_survives_a_retry_storm_inside_the_middle_period(db):
    """窗口按行数时，一个周期的重试能把窗口全部占满。

    第 2 个周期失败 29 次（每次隔 `RETRY_GAP_MIN`），最近 30 行就只剩
    第 3 个周期的一行加第 2 个周期的 29 行：连续三个失败周期永远凑不满，
    一个每晚都失败的 job 一条告警都不发。读取按周期分组后应当恰好告一条，
    且是第 3 个周期出结论时告（D103）。
    """

    def job(ctx):
        raise TransientError("akshare", "boom")

    runner, _, clock, alarms, spec = harness(db, job)
    runner.execute(spec, Due(period_key="2024-10-24", as_of=date(2024, 10, 24)), UNIVERSE)
    for attempt in range(29):
        if attempt:
            clock.advance(minutes=RETRY_GAP_MIN)
        runner.execute(spec, DUE, UNIVERSE)
    clock.advance(minutes=RETRY_GAP_MIN)
    runner.execute(spec, Due(period_key="2024-10-26", as_of=date(2024, 10, 26)), UNIVERSE)

    assert len(alarms) == 1
    assert "2024-10-26" in alarms[0].message


def test_the_alarming_period_alarms_once_across_its_retries(db):
    """告警的周期自己继续重试，不能每重试一次就再告一条。

    第 3 个周期失败 5 次：每一次的连续失败周期数都恰好是 3，
    若不看「这一行是不是这个周期的第一个结论」，这里会收到 5 条而不是 1 条（D103）。
    """

    def job(ctx):
        raise TransientError("akshare", "boom")

    runner, _, clock, alarms, spec = harness(db, job)
    runner.execute(spec, Due(period_key="2024-10-24", as_of=date(2024, 10, 24)), UNIVERSE)
    runner.execute(spec, Due(period_key="2024-10-25", as_of=date(2024, 10, 25)), UNIVERSE)
    for attempt in range(5):
        if attempt:
            clock.advance(minutes=RETRY_GAP_MIN)
        runner.execute(spec, Due(period_key="2024-10-26", as_of=date(2024, 10, 26)), UNIVERSE)

    assert len(alarms) == 1


def test_a_successful_period_resets_the_streak(db):
    """连续失败必须是**连续**：中间成功过一个周期，计数就要从头来。

    把 `failure_streak` 里的 `break` 换成 `continue`，第 4 个失败周期会数到
    第 1、2 个（跳过第 3 个成功的），抖动一场也会发出告警。
    """

    def job(ctx):
        if ctx.period_key == "2024-10-26":
            return JobOutcome()
        raise TransientError("akshare", "boom")

    runner, _, _, alarms, spec = harness(db, job)
    for period in ("2024-10-24", "2024-10-25", "2024-10-26", "2024-10-27"):
        result = runner.execute(
            spec, Due(period_key=period, as_of=date.fromisoformat(period)), UNIVERSE
        )
        assert result.status == (OK if period == "2024-10-26" else FAILED)

    assert alarms == []


def test_the_gap_is_measured_from_the_end_of_the_last_attempt(db):
    """重试间隔从上一次**结束**起算，不是从它开始起算。

    一个跑了 25 分钟才失败的 job，失败后立刻重来并没有「等满 10 分钟」：
    从 `started_at` 起算的话，慢 job 的每次失败都像是早就过完了间隔，
    打源的就不是重试，是循环。
    """

    def job(ctx):
        clock.advance(minutes=25)
        raise TransientError("akshare", "boom")

    runner, _, clock, _, spec = harness(db, job)
    first = runner.execute(spec, DUE, UNIVERSE)
    assert first.status == FAILED

    waiting = runner.execute(spec, DUE, UNIVERSE)
    assert waiting.status == SKIPPED
    assert "不足" in waiting.skip_reason

    clock.advance(minutes=RETRY_GAP_MIN)
    again = runner.execute(spec, DUE, UNIVERSE)
    assert again.status == FAILED


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


def test_a_global_row_does_not_stand_in_for_the_market_row(db):
    """上面那一枚只钉了「全局看不见 A 股那一行」，这一枚钉反方向。

    查账时漏掉 `market` 的话，A 股那一晚会被全局那条的成功行说成「已经成功」，
    而一个市场的数据都不会拉——症状与「今天不是交易日」一模一样。
    """
    calls = []

    def job(ctx):
        calls.append(ctx.market)
        return JobOutcome()

    runner, _, _, _, global_spec = harness(db, job, market=None)
    runner.execute(global_spec, DUE, UNIVERSE)
    market_spec = JobSpec(name="bars", period="daily", market="A", run=job)

    assert runner.execute(market_spec, DUE, UNIVERSE).status == OK
    assert calls == [None, "A"]


def test_the_running_row_written_by_this_round_is_not_its_own_block(db):
    """查账读的是这一轮之前的行：自己刚写的那一行不能把自己拦下。

    把 `latest_for_period` 挪到 `start` 之后，每一轮都会得到「上一轮还在跑（已 0 分钟）」，
    而 job 一次也不会被执行——注册表、心跳、退出码全都正常。
    """

    def job(ctx):
        return JobOutcome()

    runner, runs, _, _, spec = harness(db, job)
    assert runner.execute(spec, DUE, UNIVERSE).status == OK
    assert runs.latest_for_period("bars", "A", DUE.period_key).status == OK
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_runner.py -q
```

Expected: FAIL（`sams.services.scheduler.runner` 不存在）。

- [ ] **Step 3: 实现 `TaskRunRepo.latest_per_period`**

`src/sams/storage/repos/task_runs.py` 在 `recent` 下方加：

```python
    def latest_per_period(
        self, job_name: str, market: str | None, *, limit: int
    ) -> list[TaskRun]:
        """每个周期只报它最近的那一行，新到旧，最多 `limit` 个**周期**。

        `recent` 数的是行，而一个一直失败的周期每 `RETRY_GAP_MIN` 分钟就多写一行：
        按行数窗口，多大的数都会被同一个周期自己的重试占满。
        一个周期的结论就是它的最后一行：成功之后这个周期不再被重跑，
        所以最后一行不会被更早的行盖住。
        """
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM ("
            " SELECT *, rowid,"
            " row_number() OVER ("
            "   PARTITION BY period_key ORDER BY started_at DESC, rowid DESC"
            " ) AS rnk"
            " FROM task_runs WHERE job_name = ? AND market IS NOT DISTINCT FROM ?)"
            " WHERE rnk = 1 ORDER BY started_at DESC, rowid DESC LIMIT ?",
            [job_name, market, int(limit)],
        )
        return [_to_run(row) for row in rows.to_dicts()]
```

`tests/storage/test_task_runs.py` 追加（沿用该文件自己的 `Clock` 桩；推进时钟的那些枚
故意让两行的 `started_at` 不同，好把「按周期取最新」与「同一瞬间靠 `rowid` 决胜」分开钉）：

```python
def test_latest_per_period_reports_only_the_last_row_of_a_period(db):
    """一个周期的结论就是它的最后一行：更早的重试行不能替这个周期说话。

    两行之间推进了时钟，所以这一枚钉的是「按周期取最新一行」本身，
    不依赖同一时刻多行之间的 `rowid` 决胜。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    failed = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(failed, status=FAILED, error="boom")
    clock.advance(minutes=1)
    ok = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(ok, status=OK)
    latest = repo.latest_per_period("bars", "A", limit=5)
    assert [row.id for row in latest] == [ok]
    assert latest[0].status == OK


def test_two_rows_at_the_same_instant_report_the_last_written(db):
    """同一瞬间写下的两行，报后写的那一行：决胜靠 `rowid`，不靠运气。

    去掉分组排序里的 `rowid DESC`，DuckDB 会把**先**写的那一行当成这个周期的结论：
    一个先失败、后跑成的周期就被数成一个失败周期，连续三个周期的告警因此能凑满，
    而这一晚其实一次都没打挂。「最近的一行」如果由存储引擎的心情决定，就不是最近的一行。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    first = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(first, status=FAILED, error="第一次")
    second = repo.start("bars", market="A", period_key="2024-10-25")
    repo.finish(second, status=OK)
    latest = repo.latest_per_period("bars", "A", limit=5)
    assert [row.id for row in latest] == [second]


def test_latest_per_period_limit_counts_periods_not_rows(db):
    """`limit` 数的是**周期**：一个周期无论写了多少行，都只占一格。"""
    clock = Clock(datetime(2024, 10, 24, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    last_ids = []
    for period in ("2024-10-24", "2024-10-25", "2024-10-26"):
        for _ in range(3):
            run_id = repo.start("bars", market="A", period_key=period)
            repo.finish(run_id, status=FAILED, error="boom")
            clock.advance(minutes=1)
        last_ids.append(run_id)
    latest = repo.latest_per_period("bars", "A", limit=2)
    assert [row.id for row in latest] == list(reversed(last_ids))[:2]
    assert [row.period_key for row in latest] == ["2024-10-26", "2024-10-25"]


def test_latest_per_period_does_not_leak_across_markets(db):
    """两个方向都要钉住：全局账本看不见 A 股的行，A 股的账本也看不见全局的行。

    漏了 `market` 的话，一个账本的失败周期会去数进另一个账本的连续失败里。
    """
    clock = Clock(datetime(2024, 10, 25, 8, 30, tzinfo=UTC))
    repo = TaskRunRepo(db, clock=clock)
    for period in ("2024-10-25", "2024-10-26"):
        global_run = repo.start("fx", period_key=period)
        repo.finish(global_run, status=OK)
        market_run = repo.start("fx", market="A", period_key=period)
        repo.finish(market_run, status=OK)
        clock.advance(minutes=1)
    global_rows = repo.latest_per_period("fx", None, limit=5)
    assert [row.market for row in global_rows] == [None, None]
    market_rows = repo.latest_per_period("fx", "A", limit=5)
    assert [row.market for row in market_rows] == ["A", "A"]
```

- [ ] **Step 4: 实现 runner**

`src/sams/services/scheduler/runner.py`：

```python
"""`task_runs` 的唯一写入方：一个 job 的一次执行怎么开始、怎么收尾、什么时候准重来。

三条规则只写在这一处（D105），不写进 job 里：每个 job 的失败形状本该一样，
写进 job 的话，某一个忘了看 `period_key`，它就会每 tick 跑一遍。
- 本周期成功过 → 不再跑（`period_key` 是「跑过没有」的唯一依据，D98）；
- 上一轮还在跑且还算新鲜 → 等它（超过 `STALE_RUNNING_H` 的 `running` 是崩溃留下的行，
  读取侧不能把它当活进程，但也不能每个 `running` 都当成死进程）；
- 上一次失败且还在重试间隔内 → 等一等（每 tick 都重拉不是重试，是打源）。
连续失败满 `FAILURES_FOR_ALARM` 个**周期**才发一条 `SystemAlarm`，且一个周期至多告一次——
只在写出它**第一个结论**的那一次（D103）：账本读取按周期分组，因为一个一直失败的周期
每轮重试都多写一行；同一个周期里的重试不是三个周期，否则一次网络抖动很快就能凑满。

`SKIPPED` 不是 `task_runs` 的第四种状态：被这三条拦下的那一轮**不留行**——
「该不该跑」由到期计算与这三条回答，不是一次执行（`task_runs.py` 的文件头）。

异常在这里接住的是 `Exception`，而 `jobs.py` 的每一段循环只接 `SamsError`：这不是矛盾，
是两层各管各的隔离范围。job 那一层的单位是一只票/一对币种/一个用户，接错了就把编程错误
说成「这一只取不到」，而该修的那行代码永远没人去修；这一层的单位是「一个 job 的一晚」，
它冒出来的任何东西都不该带走同一个 tick 里排在后面的那些 job，所以 job 本体的异常
全部在这里接住并记成一行 `failed`（`TypeError: ...`），让健康页与告警去读那一行。
账本写入（`start`/`finish`）不在这个 `try` 里：那一行连自己都写不进去时，这里没有
别的载体能替它说话。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.events import EventBus, SystemAlarm
from sams.services.scheduler.due import Due
from sams.services.scheduler.types import JobContext, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, RUNNING, TaskRun, TaskRunRepo

logger = logging.getLogger("sams.services.scheduler.runner")

#: 失败后多久准重来（D105）。
RETRY_GAP_MIN = 10
#: `running` 超过这个小时数就当它是崩溃留下的行，可以重跑（D105）。
STALE_RUNNING_H = 6
#: 连续多少个**周期**失败才发一条 `SystemAlarm`（D103）。
FAILURES_FOR_ALARM = 3
#: `RunResult` 的第三种结论：这一轮没跑，而且**没有**写 `task_runs`
#: （「该不该跑」由到期计算与上面三条规则回答，不是一次执行）
SKIPPED = "skipped"

#: 数连续失败周期时读几个**周期**：多读一格，第 4 个失败周期才不会被数成第 3 个（D103）。
STREAK_PERIODS = FAILURES_FOR_ALARM + 1


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
    """从最近的周期往回数：连续多少个**周期**失败。

    `runs` 必须是每个周期一行（那一行的最近状态）、新到旧——`latest_per_period`
    给的就是这个形状：一个周期的结论就是它的最后一行，成功之后不再被重跑，
    所以这里不需要再按 `period_key` 去重。
    """
    streak = 0
    for run in runs:
        if run.status != FAILED:
            break
        streak += 1
    return streak


@dataclass(frozen=True)
class JobRunner:
    runs: TaskRunRepo
    bus: EventBus
    clock: Clock = SYSTEM_CLOCK

    def execute(self, spec: JobSpec, due: Due, universe: Universe) -> RunResult:
        now = self.clock.now_utc()
        # 查账在 `start` 之前：这一轮自己写的那一行不能成为拦下自己的那一行
        previous = self.runs.latest_for_period(spec.name, spec.market, due.period_key)
        if previous is not None:
            reason = self._skip_reason(previous, due, now)
            if reason:
                return RunResult(
                    job=spec.name,
                    market=spec.market,
                    period_key=due.period_key,
                    status=SKIPPED,
                    skip_reason=reason,
                    run_id=previous.id,
                )
        # 崩溃会留下那行 `running`：它没有结论，所以替它重跑的这一轮就是这个周期的第一个结论
        first_verdict = previous is None or previous.status == RUNNING
        run_id = self.runs.start(spec.name, market=spec.market, period_key=due.period_key)
        context = JobContext(
            period_key=due.period_key,
            as_of=due.as_of,
            market=spec.market,
            universe=universe,
        )
        try:
            outcome = spec.run(context)
        except Exception as exc:
            # 隔离是这一层的职责：一次跑挂不能带走同一个 tick 里排在后面的那些 job
            # （`jobs.py` 那一层只接 `SamsError`，理由写在它的文件头）
            # 行里只留得下 `类型名: 消息`，栈只能进日志，否则编程错误没法读
            logger.exception("job %s 周期 %s 执行异常", spec.name, due.period_key)
            return self._failed(
                spec, due, run_id, f"{type(exc).__name__}: {exc}", first_verdict=first_verdict
            )
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
            first_verdict=first_verdict,
            rows_affected=outcome.rows_affected,
            note=outcome.note,
        )

    def _skip_reason(self, previous: TaskRun, due: Due, now: datetime) -> str:
        if previous.status == OK:
            return f"周期 {due.period_key} 已经成功"
        if previous.status == RUNNING:
            age = now - previous.started_at
            if age < timedelta(hours=STALE_RUNNING_H):
                return f"上一轮还在跑（已 {int(age.total_seconds() // 60)} 分钟）"
            return ""
        # `failed` 只能由 `finish()` 写出，而它同一句就写了 `finished_at`
        waited = now - previous.finished_at
        if waited < timedelta(minutes=RETRY_GAP_MIN):
            return (
                f"距上次失败 {int(waited.total_seconds() // 60)} 分钟，"
                f"不足 {RETRY_GAP_MIN} 分钟"
            )
        return ""

    def _failed(
        self,
        spec: JobSpec,
        due: Due,
        run_id: str,
        error: str,
        *,
        first_verdict: bool,
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
            self.runs.latest_per_period(spec.name, spec.market, limit=STREAK_PERIODS)
        )
        # 只在第一个结论上告（D103）：第三个周期重试到第 144 次时连续数还是 3，
        # 没有 `first_verdict` 这一格，告警会从「永不发」翻成「每天发 144 条」
        if first_verdict and streak == FAILURES_FOR_ALARM:
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

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/storage/test_task_runs.py tests/services/test_scheduler_runner.py -q
```

Expected: PASS（11 + 16 = 27 枚）。

- [ ] **Step 6: 提交**

```bash
git add src/sams/services/scheduler/runner.py src/sams/storage/repos/task_runs.py tests/services/test_scheduler_runner.py tests/storage/test_task_runs.py
git commit -m "feat(scheduler): JobRunner 的周期记账、重试间隔与连续失败告警"
```

---


**评审收尾（2026-10-22）**：实现落在 `d99efae`，两路评审之后修在 `2e30aa4`，
`rowid` 决胜那一枚补在 `6d4b09e`。评审发现的问题与处理：

- **D103 的告警在生产形状下永远不响**（BLOCKING，`2e30aa4`）。计划的窗口是
  `recent(..., limit=STREAK_WINDOW=30)`，数的是**行**；一个一直失败的周期每 10 分钟多写一行，
  而 `daily_due`（`due.py:63`）把同一个 `period_key` 一路到期到下一个交易日收盘之后，
  约 144 行/天。最近 30 行于是全是这一个周期的重试，`failure_streak` 只见到一个周期键，
  永远数到 1，健康页写着「连续失败」而管理员一条都收不到。落地把分组下推到仓库
  （`latest_per_period`，窗口函数按 `period_key` 分组、`limit` 数周期），
  runner 只留 `STREAK_PERIODS = FAILURES_FOR_ALARM + 1`。
- **只把窗口改对会反过来告上百条**（同一次修完）。计数一变成按周期的，第三个周期的
  每一次重试看到的连续数都恰好是 3——从每天 0 条变成每天上百条。补的第二格是
  `first_verdict`：只有写出本周期**第一个结论**的那一轮才配告警；上一行是崩溃留下的
  `running` 时它没有结论，替它重跑的那一轮就算第一个结论。用例
  `test_the_alarming_period_alarms_once_across_its_retries` 单独钉这一格（它对
  「只改窗口」的那版实现是红的）。
- **计划片段自身有两处死代码**：`if previous is not None else ""` 那个三元（`previous`
  在 `if reason` 那支里必然不是 None）、`finished_at or started_at` 的回退
  （`failed` 只能由 `finish()` 写出，而它同一句就写了 `finished_at`，
  DDL 只在收尾前允许 NULL）。落地都删了；`RETRY_GAP_MIN` 取 `started_at` 的写法
  由 `test_the_gap_is_measured_from_the_end_of_the_last_attempt` 钉住——
  那枚用例改的是 job 内部推进 25 分钟，旧实现下是绿的，因为没有任何用例在
  `start` 与 `finish` 之间推过时钟。
- **「0018 之前写的老行没有周期键」是假的**：`0018_scheduler.sql` 的文件头写着这张表
  从来没有写入方，所以计划那句为老行准备的 `run.period_key or run.id` 没有对象，
  落地按「不写没发生的兼容分支」删掉。
- **D96 两处过头话**：① 文件头说这一层「兜住全部」，而 `try` 只包住 `spec.run`——
  `runs.start`/`runs.finish` 在外面，账本自己都写不进去时这里没有别的载体。
  现在照实写范围。② 那个窗口的注释说「窗口必须宽于那个数」，没有任何行数够宽
  （每周期重试次数没有上界），换成按周期分组之后这条才成立。
- **补齐的单向钉**（四枚新用例）：`failure_streak` 的 `break`（`break`→`continue`
  在旧用例下全绿，症状是一场抖动也能告警）、重试间隔从 `finished_at` 起算（换成
  `started_at` 全绿，症状是慢 job 每 tick 打一次源）、连续失败读的那一趟也要按市场分账
  （漏掉 `market` 全绿，症状是全局那条链的三次失败替 A 股告警）、
  同一瞬间两行的决胜（去掉 `rowid DESC`，DuckDB 会把**先**写的那一行当成本周期的结论，
  于是「先失败后跑成」的周期被数成失败周期）。
- **裸 `Exception` 加了一条 `logger.exception`**：行里只放得下「类型名: 消息」，
  没有栈的 `TypeError` 读不出是哪一行。`outcome.errors` 那一支不加——失败文本本来就是数据。
- **没采纳的建议与理由**：把 `RunResult.run_id` 在 `SKIPPED` 时清空或改名
  `previous_run_id`——那个字段在这里回答的是「拦下这一轮的是哪一行」，
  计划与用例都要它，改名属于无关重构；把 `skip_reason` 与 `error` 并成一个 `detail`——
  Task 11 那一屏要分开读两种颜色；把「一个周期只告一次」的去重推给 Task 9 的 `SystemAlerter`
  （D102 的 cooldown 是按 component 的投递去重，默认 30 分钟，而重试是 10 分钟一次，
  推过去等于每小时告两条）。
- **往后的任务要注意**：Task 11 的 `report.py` 计划片段还在 `from ...runner import
  STREAK_WINDOW, failure_streak` 并用 `runs.recent(..., limit=STREAK_WINDOW)` 数连续失败——
  `STREAK_WINDOW` 已经不存在了。那一屏要的是「连续几个周期」，同样必须走
  `latest_per_period`，并且自己带一个显示用的周期上限（要说清那个数会饱和），
  `failure_streak` 可以接着用——它现在吃的形状是「每周期一行」。

### Task 9: `SystemAlerter`——系统告警只到管理员，冷却跨重启

**Files:**
- Modify: `src/sams/storage/repos/alerts.py`（`latest_open` 之后加 `has_system_alert`）
- Create: `src/sams/services/scheduler/alarms.py`
- Test: `tests/services/test_scheduler_alarms.py`（自带 `PersistingSender` 替身）
- Test: `tests/storage/test_alerts_repo.py`（追加四枚）

`SystemAlarm` 现在有三个发布方（`datasource/registry.py`、`services/calendar.py`、
Task 8 的 runner），订阅方只有 `attach_log_sink` 的日志。D102 要回答的是：
**发给谁**（`is_admin && active`）、**写什么形状**（`rule_id=NULL`、`market='SYS'`、
`code=component`）、**多久之内算同一次故障**（同组件同人一个 `cooldown_min`）。
去重读 `alerts` 的行，所以调度器重启之后计数不会从零开始。

收件人那一关复用 `services/actors.active_users`（「替一批用户做事只认启用账号」那条
不变量归它），`is_admin` 才是本模块自己加的那一关。

- [ ] **Step 1: 写失败用例（服务层）**

`tests/services/test_scheduler_alarms.py`：

```python
"""`SystemAlarm` → 管理员（D102）。

四条规则：只有 `is_admin && active` 收；形状是 `market='SYS'`、`code=component`、
`rule_id=NULL`；同组件同人一个冷却窗口只发一次；去重读库，重启之后仍是同一次故障。
这四条各钉两头——该沉默的那一次没出声、该出声的那一次确实出声，否则「把冷却写成零」
与「写成无穷」都能全绿；两头不一定落在同一个用例里（形状那一枚只管库里那一行长什么样，
「只缺半边的行不能当冷却起点」那一头住在 `tests/storage/test_alerts_repo.py`）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime

from support import FrozenClock

from sams.core.errors import TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import STRONG, AlertMessage
from sams.services.scheduler.alarms import SYS_LABEL, SYS_MARKET, SystemAlerter
from sams.services.scheduler.due import Due
from sams.services.scheduler.runner import JobRunner
from sams.services.scheduler.types import JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.task_runs import TaskRunRepo
from sams.storage.repos.users import UserRepo

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
PASSWORD = "correct-horse-battery"
UNIVERSE = Universe(by_market={}, fx_pairs=())


class PersistingSender:
    """`AlertSender` 的替身：像 `NotifyService.dispatch` 一样先在 `alerts` 落一行，再尝试投递。

    去重读的是库里那一行，所以替身必须真写库：只记调用的替身攒不出冷却，
    「两次告警只发一次」就会退化成「两次调用只断言一次调用」。
    写的形状照 `support_rules.StoringSender`（同一套列，同一份 payload）。
    `fail_for` 那一个人是**写完行之后**才抛的：生产上那一行先落库、投递在后面
    （`monitor/router.py` 先 `alerts.create` 再发），所以「发不出去」改不了去重的答案。
    """

    def __init__(self, alerts: AlertRepo, *, fail_for: Iterable[str] | None = None) -> None:
        self.alerts = alerts
        #: 这些用户的 `dispatch` 抛异常：一个收件人失败不该带走其余收件人
        self.fail_for = set(fail_for or ())
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
        if user_id in self.fail_for:
            raise RuntimeError(f"通道挂了：{user_id}")
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

    # 三个人都在库里：只有一个收件人不是因为另外两个没建成功
    assert len(users.list_users()) == 3
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


def test_the_boundary_moment_still_counts_as_inside_the_cooldown(db, settings):
    """窗口是闭的：`triggered_at == since` 的那一行仍然算「刚告过」。

    写成 `>` 的话，恰好隔 `cooldown_min` 分钟的那一次会再发一遍——而库里躺着的那一行
    说的正是「这一次已经发过了」，屏幕上的管理员会看到同一场故障的两条告警。
    """
    bus, sender, users, _, clock = harness(db, settings)
    users.create("admin", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="第一次"))
    clock.advance(minutes=settings.cooldown_min)
    bus.publish(SystemAlarm(component="calendar", message="恰好到点，仍然沉默"))
    assert [call[1].body for call in sender.calls] == ["第一次"]

    clock.advance(minutes=1)
    bus.publish(SystemAlarm(component="calendar", message="过了一点就该出声"))
    assert [call[1].body for call in sender.calls] == ["第一次", "过了一点就该出声"]


def test_different_components_do_not_share_a_cooldown(db, settings):
    bus, sender, users, _, _ = harness(db, settings)
    users.create("admin", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="a"))
    bus.publish(SystemAlarm(component="datasource:akshare", message="b"))

    assert len(sender.calls) == 2


def test_the_cooldown_is_per_recipient(db, settings):
    """去重键里有收件人：两个人共用一个窗口时，后建的那一个永远收不到第一条。

    按组件独占去重的话，第一个管理员那一行就把第二个挡住了——而「发给在线管理员」
    说的是每一个在线管理员。
    """
    bus, sender, users, _, _ = harness(db, settings)
    first = users.create("admin-a", PASSWORD, is_admin=True)
    second = users.create("admin-b", PASSWORD, is_admin=True)

    bus.publish(SystemAlarm(component="calendar", message="日历源全挂"))

    assert [call[0] for call in sender.calls] == [first.id, second.id]

    # 第二趟：两个人各自还在窗口内，一个都不该再加
    bus.publish(SystemAlarm(component="calendar", message="还是那场故障"))
    assert [call[0] for call in sender.calls] == [first.id, second.id]


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


def test_a_failing_recipient_leaves_a_traceback_in_the_log(db, settings, caplog):
    """按收件人包起来的 `try` 不许把栈吃掉。

    `dispatch` 在收件人循环里被逐个人包住，异常因此到不了 `EventBus.publish`（那里是
    另一处会带栈的日志）：写成 `logger.warning` 的话，一次投递失败在屏幕上只剩一行
    没有模块、没有帧的消息——没人能从那一句话找到抛出来的那一段代码。
    """
    users = UserRepo(db)
    broken = users.create("admin-a", PASSWORD, is_admin=True)
    bus, _, _, _, _ = harness(db, settings, fail_for={broken.id})

    with caplog.at_level(logging.WARNING):
        bus.publish(SystemAlarm(component="calendar", message="boom"))

    logged = [rec for rec in caplog.records if "系统告警发给" in rec.getMessage()]
    assert len(logged) == 1
    assert logged[0].exc_info is not None
    assert logged[0].exc_info[0] is RuntimeError
    # 栈里要有抛出那一站：只有消息文本的话，`warning` 与 `exception` 断言不出差别
    assert "in dispatch" in caplog.text


def test_a_recipient_whose_delivery_raised_is_not_retried_inside_the_window(db, settings):
    """一次发不出去 = 这一窗口内不再替他重试；读的一侧看到的那一行沉默没有解释，
    只有库里那一行解释了它。

    生产上 `dispatch` 先写 `alerts` 那一行、再尝试投递（`monitor/router.py`），所以失败的
    那一个人也在库里留下一行，而去重读的就是这一行：同一场故障的下一次发布对他仍然沉默。
    """
    users = UserRepo(db)
    broken = users.create("admin-a", PASSWORD, is_admin=True)
    bus, sender, _, alerts, _ = harness(db, settings, fail_for={broken.id})

    bus.publish(SystemAlarm(component="calendar", message="第一次"))
    bus.publish(SystemAlarm(component="calendar", message="第二次"))

    # 那一行确实落了，而它压住了第二次的投递
    assert len(alerts.list_for(broken.id)) == 1
    assert sender.calls == []


def test_the_runner_alarm_is_delivered_by_the_attached_alerter(db, settings):
    """D103 → D102 的那一棒：runner 发的那一条由这里送到管理员眼前。

    去重键用的是 `JobSpec.component` 那一侧的 `job.<name>.<market>`：两边各写各的串时，
    同一个 job 的每一晚都会重新数一遍冷却，而屏幕上是每天一条重复告警。
    """
    bus, sender, users, alerts, clock = harness(db, settings)
    admin = users.create("admin", PASSWORD, is_admin=True)
    runner = JobRunner(runs=TaskRunRepo(db, clock=clock), bus=bus, clock=clock)

    def failing(ctx):
        raise TransientError("akshare", "boom")

    spec = JobSpec(name="bars", period="daily", market="A", run=failing)
    for period in ("2024-10-24", "2024-10-25", "2024-10-26"):
        runner.execute(
            spec, Due(period_key=period, as_of=date.fromisoformat(period)), UNIVERSE
        )

    assert len(sender.calls) == 1
    row = alerts.list_for(admin.id)[0]
    assert (row.market, row.code, row.rule_id) == (SYS_MARKET, "job.bars.A", None)

    # 同一个组件再告一次（换市场才算换组件）：窗口内沉默
    bus.publish(SystemAlarm(component="job.bars.A", message="再来一次"))
    assert len(sender.calls) == 1
```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_scheduler_alarms.py -q
```

Expected: FAIL（`sams.services.scheduler.alarms` 不存在，`AlertRepo` 没有 `has_system_alert`）。

- [ ] **Step 3: 写 `has_system_alert`**

`src/sams/storage/repos/alerts.py` 在 `latest_open` 之后加：

```python
    def has_system_alert(self, user_id: str, component: str, *, since: datetime) -> bool:
        """这个人名下从 `since` 起有没有过关于这个组件的系统告警（D102 的去重键）。

        问的是**库里有没有那一行**，不是那个人有没有真的收到一条通知：
        压制与 `no_channel` 照样留下这一行，而这一行正是压住后续告警的那个东西。
        `code` 与股票代码共用一列，靠 `market = 'SYS'` 与 `rule_id IS NULL` 分开：
        一条恰好把 `code` 写成组件名的规则告警不能被当成冷却的起点。
        `triggered_at` 与 `since` 必须在同一条时间轴上：调用方传「现在 - 冷却」，
        这里不读 `clock`，否则窗口的宽度会变成两个时钟之差。
        答案是一个 `bool` 而不是那一行：调用方拿到行也只为问同一个问题，
        为它解出整份 `payload_json` 是那一次白花的解码。
        """
        return bool(
            self.db.scalar(
                "SELECT EXISTS ("
                " SELECT 1 FROM alerts"
                " WHERE user_id = ? AND market = 'SYS' AND code = ? AND rule_id IS NULL"
                " AND triggered_at >= ?)",
                [user_id, component, _moment(since)],
            )
        )

    def record(
        self,
        alert_id: str,
        channel_id: str | None,
        outcome: str,
        *,
        error: str = "",
        attempts: int = 1,
    ) -> None:
        """一次投递尝试的结果。

        `channel_id=None` 只允许配 `suppressed`/`no_channel`。这条约束写在 Python 而不是
        DDL：DuckDB 的 CHECK 报错到屏幕上只剩一个约束名，而这一句要能说清
        「为什么压制没有通道」——压制的意思是「还没有选到通道就停了」。
        """
        if outcome not in OUTCOMES:
            raise ValidationError(f"未知投递结果 outcome={outcome!r}：只有 {list(OUTCOMES)}")
        if self.get(alert_id) is None:
            raise StorageError(f"告警 {alert_id} 不存在（投递记录必须挂在一条告警上）")
        if outcome == NO_CHANNEL and channel_id is not None:
            raise ValidationError(
                "no_channel 不能带 channel_id：那一句话的意思是「一个通道都没选到」，"
                "配一个 id 就自相矛盾了"
            )
        if outcome in (SENT, FAILED) and not channel_id:
            raise ValidationError(f"{outcome} 必须带 channel_id：一次真发过的事件总得发到哪")
        # `suppressed` 两种形状都合法：免打扰压制发生在选路之前（channel_id 为空），
        # 而「这台机器没有桌面」是关于某一条具体通道的事实（id 要留着才认得出是哪条）
        if attempts < 0:
            raise ValidationError(f"attempts 不能为负（收到 {attempts}）")
        self.db.execute(
            "INSERT INTO notify_log (id, alert_id, channel_id, ts, attempt, outcome, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                new_id(),
                alert_id,
                channel_id,
                self.clock.now_utc(),
                int(attempts),
                outcome,
                (error or "").strip()[:_ERROR_MAX_CHARS],
            ],
        )

    def deliveries(self, alert_id: str) -> list[Delivery]:
        rows = self.db.query(
            "SELECT id, channel_id, coalesce(outcome, 'failed') AS outcome,"
            " coalesce(error, '') AS error, attempt, ts FROM notify_log"
            " WHERE alert_id = ? ORDER BY ts, id",
            [alert_id],
        )
        return [
            Delivery(
                id=str(row["id"]),
                channel_id=row["channel_id"],
                outcome=str(row["outcome"]),
                error=str(row["error"] or ""),
                attempts=int(row["attempt"] or 1),
```

`src/sams/services/actors.py` 里已有、这里直接复用的那一个筛选：

```python
def active_users(users: UserRepo) -> list[User]:
    """替一批用户做事的那一趟，只认启用的账号。

    `resolve_actor` 挡的是屏幕上传进来的那一个名字，而常驻的盘后链按名单跑，
    那一条路上没有任何入口经过 `resolve_actor`：不在这里挡，`sams user disable`
    就只挡住了手敲命令的那一头。
    采集那一头相反（见 `services/scheduler/universe.py` 的文件头）：停用的用户照拉对象，
    数据是公共资源，明天启用时库里该已经有数。
    """
    return [user for user in users.list_users() if user.status == ACTIVE]
```

仓库层的四枚用例（`tests/storage/test_alerts_repo.py` 追加）：

```python
# ---------- 系统告警的冷却（D102：同组件同人一个窗口）----------


def test_has_system_alert_matches_one_admin_one_component(repo, seeded_user, other_user):
    system = {
        **CREATE_KW,
        "rule_id": None,
        "account_id": None,
        "market": "SYS",
        "code": "calendar",
    }
    repo.create(seeded_user, **system)

    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is True
    assert repo.has_system_alert(other_user, "calendar", since=since) is False
    assert repo.has_system_alert(seeded_user, "datasource:akshare", since=since) is False
    assert (
        repo.has_system_alert(
            seeded_user, "calendar", since=datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
        )
        is False
    )


def test_has_system_alert_counts_the_boundary_moment_as_inside(repo, seeded_user):
    """窗口在仓库层也是闭的：`triggered_at == since` 的那一行算「刚告过」。

    只在服务层钉这一条不够——那里隔着收件人筛选与消息构造，把 `>=` 写成 `>` 时
    屏幕上留下的是一条没有解释的沉默，而这一层的一问一答必须自己说清边界归谁。
    """
    moment = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)
    repo.create(
        seeded_user,
        **{
            **CREATE_KW,
            "rule_id": None,
            "account_id": None,
            "market": "SYS",
            "code": "calendar",
            "triggered_at": moment,
        },
    )

    assert repo.has_system_alert(seeded_user, "calendar", since=moment) is True
    # 窗口右端挪一秒，那一行就落在外面：闭区间不是「全都算」
    assert (
        repo.has_system_alert(seeded_user, "calendar", since=moment + timedelta(seconds=1))
        is False
    )


def test_has_system_alert_does_not_mistake_a_stock_alert_for_a_component(repo, seeded_user):
    repo.create(seeded_user, **{**CREATE_KW, "code": "calendar"})

    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is False


def test_has_system_alert_needs_both_halves_of_the_system_shape(repo, seeded_user):
    """`market = 'SYS'` 与 `rule_id IS NULL` 是两个条件，所以要一行只缺半边的证据。

    只种形状完整的那一行时，删掉任一条件用例都还是绿的；缺一边的那两种行都会被当成
    冷却的起点，于是本该出声的那一次被按下——而屏幕上留下的是一行看不出来的沉默。
    """
    since = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    # SYS 市场、组件名对，但挂着 `rule_id`：那是规则告警，不是系统告警
    repo.create(seeded_user, **{**CREATE_KW, "market": "SYS", "code": "calendar"})
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is False

    # 反过来：没有 `rule_id`，但市场是普通的 A（一条恰好把 `code` 写成组件名的规则告警）
    repo.create(seeded_user, **{**CREATE_KW, "rule_id": None, "code": "calendar"})
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is False

    # 两半都齐才算：同一句查询的正例
    system = {"rule_id": None, "account_id": None, "market": "SYS", "code": "calendar"}
    repo.create(seeded_user, **{**CREATE_KW, **system})
    assert repo.has_system_alert(seeded_user, "calendar", since=since) is True
```

- [ ] **Step 4: 写 `alarms.py`**

`src/sams/services/scheduler/alarms.py`：

```python
"""`SystemAlarm` → 管理员（D102）。

与用户告警共用 `alerts` 那张表，投递也只对着同一个端口 `AlertSender`（D95）：那是规则
引擎唯一的发送出口，生产上由 `bootstrap` 把 `NotifyService.dispatch` 折成这个形状。
本模块只说「我对着这个端口发」——它接到哪条线路上由装配那一步决定。
不一样的三件事都在这里：收件人（启用且 `is_admin`）、数据形状（`rule_id=NULL`、
`market='SYS'`、`code=component`）、去重（同组件同人一个冷却窗口）。
去重读的是库里的行而不是进程内的一份集合：调度器重启之后，同一个窗口内的同一次故障
不会再发一遍。

这里说的「发给管理员」是这条告警写给谁、以及库里那一行会压住哪些后续告警：
投递那一步由 `NotifyService.dispatch` 决定，它可能以 `NO_CHANNEL` 或免打扰压制收场，
那两种结果里都没有人真的收到一条通知——所以本模块不声称任何人收到了什么。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.config import Settings
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import STRONG, AlertMessage, AlertSender
from sams.services.actors import active_users
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.services.scheduler.alarms")

#: `alerts.market` 里代表「这不是一只票」的那一个值：去重查询靠它与规则告警分开
SYS_MARKET = "SYS"
#: 读的一侧看到的「系统」：占的是标的位、账户位与阶段位
#: （用户告警在这三段上写的是股票名、账户名与阶段，而这里没有一只票）
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
        for user in active_users(self.users):
            if not user.is_admin:
                continue
            if self.alerts.has_system_alert(user.id, alarm.component, since=since):
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
                # 栈只能在这里留：异常被逐个人包住之后到不了 `EventBus.publish`（那里是另一处
                # 带栈的日志），少一个栈就没人能从那一句话找到抛出的是哪一段代码
                # （`runner.py` 答同一个问题时用的也是 `logger.exception`）
                logger.exception("系统告警发给 %s 失败：%s", user.id, exc)
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_alarms.py tests/storage/test_alerts_repo.py -q
```

Expected: PASS（11 + 36 = 47 枚）。

- [ ] **Step 6: 提交**

```bash
git add src/sams/storage/repos/alerts.py src/sams/services/scheduler/alarms.py tests/services/test_scheduler_alarms.py tests/storage/test_alerts_repo.py
git commit -m "feat(scheduler): 系统告警投递给在线管理员（冷却跨重启）"
```


**评审收尾（2026-10-22）**：实现落在 `bff0e38`，两路评审之后修在 `e5d63df`。
评审发现的问题与处理：

- **仓库层那一问一答原本返回 `Alert | None`，而调用方只问「有没有」**（`e5d63df`）。
  解一份 `payload_json` 只为说一个「是」，而且 `ORDER BY triggered_at DESC, id DESC`
  里的 `id` 是 `uuid4().hex`、不按时间排，那段排序是死的（D96：不许留没有主人的排序）。
  改成 `has_system_alert(...) -> bool`（`SELECT EXISTS` + `db.scalar`），WHERE 条件与
  `>=` 闭区间原样保留。**没**把 `>=` 翻成 `>`：那条建议要的是「和
  `services/rules/engine.py:461` 统一」，而那句写的是 `gap >= window` 就**放行**，
  两层的取向本来就相反（那边「恰好到点」算出窗、这边算在窗内）。这一秒之差留在两个层
  各答一次，是因为 `triggered_at` 精确到微秒、生产上落不到那个瞬时，而把它对齐要给
  一条已经在跑的规则链引入一次同步改动——收益是一个测不到的时刻，成本是两条链同时动。
  闭区间归「有」的一侧由两枚用例分别钉住（服务层
  `test_the_boundary_moment_still_counts_as_inside_the_cooldown`、仓库层
  `_counts_the_boundary_moment_as_inside`）。
- **测试替身与生产顺序相反，恰好表达不出一次真发生过的后果**（`e5d63df`）。
  `dispatch` 在生产上是先写 `alerts` 那一行、再尝试投递（`monitor/router.py` 的
  `alerts.create` 在发送之前），所以「发不出去」的那个人也在库里留下一行，而去重读的
  就是这一行——替他重试也没有用。原替身在写行之前就 `raise`，那条后果在测试里不存在。
  现在按生产顺序抛，并补 `test_a_recipient_whose_delivery_raised_is_not_retried_inside_the_window`。
- **按收件人包起来的 `try` 把栈吃掉了**（`e5d63df`）。`dispatch` 在收件人循环里被逐个
  包住，异常因此到不了 `EventBus.publish`（那里才是另一处带栈的日志）；写成
  `logger.warning` 时屏幕上只剩一行没有模块、没有帧的消息，没人能从那句话找到抛出的
  是哪一段。改成 `logger.exception`（Task 8 的 runner 答同一个问题用的也是它），并补
  `test_a_failing_recipient_leaves_a_traceback_in_the_log`——它断言 `exc_info` 与栈里的
  `in dispatch`，只断言消息文本时 `warning` 与 `exception` 分不出红绿。
- **收件人筛选复用了 `services/actors.active_users`**（原计划把 `list_users()` 与
  `status == ACTIVE` 就地写在 `handle` 里）。「替一批用户做事只认启用账号」这条不变量
  已经归那一层管（`sams user disable` 挡住的那一头），在这里再抄一遍就是第二处
  会漂移的地方。
- **`PersistingSender` 建在 `tests/support_scheduler.py`，随后搬进
  `tests/services/test_scheduler_alarms.py`，那个共用文件恢复原样**。要说清理由的边界：
  成立的根据是「只有一个用户的替身不该占共用文件这一格」，不是「共用文件只放多用户的东西」
  这条约定——同一个文件里的 `FakeCalendar` 与 `WeekdayProvider` 也各只有一个 importer，
  所以拿后者当先例是站不住的。搬运的同时删掉了 `support_scheduler.py` 文件头那句
  为它写的介绍。
- **D96 两处口径**：① `alarms.py` 文件头原先写「生产上是 bootstrap 折给 `NotifyService.dispatch`
  的那一份」，而接线发生在还没写的装配那一步（Task 12），现在只说它对着 `AlertSender`
  这个端口发、接到哪条线路上由装配决定。② 它原先还说「管理员收到了」，而
  `dispatch` 可能以 `NO_CHANNEL` 或免打扰压制收场，那两种结果里没有人真的收到——
  现在明写本模块不声称任何人收到了什么，并把「库里那一行才是压住后续告警的东西」留在
  `has_system_alert` 的文档里。
- **D96 一处过头话被用例反过来纠正**：测试文件头原写「每一枚都钉两头」，而形状那一枚
  （`test_the_alert_row_is_a_system_alert`）只管库里那一行长什么样，它的另一头
  （「只缺半边的行不能当冷却起点」）住在仓库层。现在写成「这四条各钉两头，两头不一定
  落在同一个用例里」。
- **仓库层从计划的两枚变成四枚**：计划里那枚正例把「同一时间窗、不同人、不同组件」
  三种负例挤在一行断言里，边界（`>=`）与形状（`market='SYS'` / `rule_id IS NULL`
  两个条件各缺一边）都没有对手。补的两枚：`_counts_the_boundary_moment_as_inside`
  （恰好等于 `since` 在内、晚一秒在外）、`_needs_both_halves_of_the_system_shape`
  （只删任一条件都还是绿的，所以必须种一行只缺半边的证据）。
- **logger 名**：计划写 `sams.scheduler.alarms`，兄弟模块用的是完整点路径
  （`sams.services.scheduler.runner`），落地按点路径。日志级别、`SYS_LABEL` 占三段位、
  `value_text` 写组件名（`threshold_text` 已经写了冷却多久，两处都在屏上渲染）都按计划。
- **实现阶段的子代理做了一次 `git reset --soft HEAD~1`** 来改提交标题（`ce743d3` →
  `bff0e38`）。核对过 reflog 与两个提交的 tree 都是 `cf28959`（没丢工作），但「永不
  `--amend`、永远新提交」这条同样管住 soft reset，已在下一份 brief 里明写禁止。
- **没采纳的建议与理由**：把 `severity == STRONG` 的两处断言并成一处（消息与库里那一行
  是两个受害者，`AlertMessage` 传错与 `alerts.create` 写错不是同一个 bug）；把
  `StoringSender` / `PersistingSender` 那两段 17 参数的 `alerts.create` 抽成一个共用
  helper（写错参数名会在调用时就 `TypeError`，不是静默漂移，抽走反而把两处形状藏在
  一个签名里）；把 `RunResult.skip_reason` 与 `error` 并成一个字段（Task 11 要分开读两种颜色，
  这条在 Task 8 已记）；`canonical_market` 认 `"SYS"`（没有调用方会走到那儿的形状）。
- **往后的任务要注意**：
  ① **Task 12 必须真的 `alerter.attach(event_bus)`**——`SystemAlerter` 的构造与 attach
  是分开的两步，只 new 不 attach 时这条链在屏上完全看不出来（库里没有行，健康页也不会
  报「没人订阅」）。Task 12 的验收里要有一枚「发布一条 `SystemAlarm` → `alerts` 里出现
  一行 `market='SYS'`」的端到端用例。
  ② **Task 11 的 `services/health.py:290` 数的是全部 `status='new'` 的 alert**
  （`count(*)` 与 `min(triggered_at)` 两问都只按 `status` 筛），
  `market='SYS'` 的行会被算进「未处理 N 条」；系统告警不该和用户看到的待处理混在一个
  数里，考虑按 `FILTER (WHERE market = 'SYS')` 拆成两个数。
  ③ 去重靠 `alerts` 那一行，而那一行只在 `dispatch` 走到 `alerts.create` 时才存在：
  如果 Task 12 的装配把 `AlertSender` 换成一个不落库的替身（例如纯日志的 sender），
  冷却会退化成「每 tick 一条」。本任务的替身之所以必须真写库，就是因为这一条。

### Task 10: `Scheduler`——tick 的顺序、心跳与常驻循环

**Files:**
- Create: `src/sams/services/scheduler/heartbeat.py`
- Create: `src/sams/services/scheduler/report.py`
- Create: `src/sams/services/scheduler/service.py`
- Modify: `src/sams/services/actors.py`（补 `admin_users`）
- Modify: `src/sams/services/scheduler/alarms.py`（收件人改读 `admin_users`）
- Modify: `src/sams/core/errors.py`（`SchedulerBusy` 的文档头改口径）
- Test: `tests/services/test_scheduler_service.py`

一轮 tick 做四件事，顺序就是代码里的顺序：打过心跳就再拍一下（手工 `tick` 没认领过
就什么也不留）→ 按 `only_job`/`only_market` 选出这一轮的 job（一个都不匹配就拒绝）→
构建这一轮的对象全集（一次 tick 只构建一份）→ 按注册顺序逐个判断到期并交给
`JobRunner`。单个 job 的异常在 runner 里已经落账；这里只隔离**到期计算**的
`SamsError`：日历源坏了不能让排在后面的市场也不跑，而日历实现里的编程错误不能被
收成一句「这一轮算不出」——那会被读成一种等。

拍放在做活之前：那一拍证明的是「循环还在转」，不是「这一轮做完了事」。两种放法的
最坏年龄都是「一轮做活的时长 + 等待间隔」，挪到收尾换不来更准的读数；换来的只是
一轮在打拍之前就失败时一拍都不留，于是「这一轮做不成」被读成「循环停了」。

心跳只由常驻循环认领（D100）：`sams run once` 是一次手工执行，不该留下一行让下一个
循环白等一个阈值。守卫读的是心跳表而不是进程内的标记，所以它挡得住「同一个进程里
再造一个 `Scheduler` 去跑常驻循环」；它挡不住另一个操作系统进程——库文件上的锁先就
让第二个进程连不上（`storage/db.py` 的 `Database.open` 把那个 `IO Error` 包成
`StorageError`），那一类重复启动在走到这句守卫之前就炸在连接上了。这条事实同时决定
Task 11/12 的形状，见下面的「往后的任务要注意」。

- [ ] **Step 1: 写失败用例**

`tests/services/test_scheduler_service.py`：

```python
"""`Scheduler`：注册顺序、市场过滤、`as_of` 覆盖、心跳与常驻循环。

runner 用记录型替身：这一层要钉的是「谁在什么时候被交给 runner」，
执行本身的三条时间规则已经在 `test_scheduler_runner.py` 钉过。
心跳那一半也两头钉：手工 `tick` 一行都不留、常驻循环每一拍打一拍，
而过期那条判据只写在一处（`heartbeat.stale_at`），读数与单实例守卫必须同时翻。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from support import FrozenClock
from support_scheduler import FakeCalendar

from sams.core.errors import CalendarSourceError, SchedulerBusy, StorageError, ValidationError
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S
from sams.services.scheduler.report import STREAK_DISPLAY_PERIODS, collect
from sams.services.scheduler.runner import RunResult
from sams.services.scheduler.service import Scheduler
from sams.services.scheduler.types import JobOutcome, JobSpec
from sams.services.scheduler.universe import Universe
from sams.storage.repos.task_runs import FAILED, OK, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

MOMENT = datetime(2024, 10, 25, 8, 30, tzinfo=UTC)
FRIDAY = date(2024, 10, 25)
UNIVERSE = Universe(by_market={}, fx_pairs=())
PASSWORD = "correct-horse-battery"


class RecordingRunner:
    def __init__(self, *, fail_for=()) -> None:
        self.calls: list[tuple[str, str | None, str, date]] = []
        self.fail_for = set(fail_for)

    def execute(self, spec, due, universe):
        self.calls.append((spec.name, spec.market, due.period_key, due.as_of))
        if spec.name in self.fail_for:
            return RunResult(
                job=spec.name,
                market=spec.market,
                period_key=due.period_key,
                status=FAILED,
                error="boom",
            )
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
    """账本读不到：那是 `SamsError`，一轮 tick 的失败不该带走常驻循环。"""

    def build(self) -> Universe:
        raise StorageError("db 挂了")


class ExplodingUniverse:
    """不是 `SamsError`：这一层没有可信的下一步，只能让它炸出去。"""

    def build(self) -> Universe:
        raise RuntimeError("编程错误")


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
        runner=runner,
        runs=runs,
        heartbeats=heartbeats,
        universe=universe or StubUniverse(),
        users=UserRepo(db),
        calendar=calendar or FakeCalendar(settings),
        clock=clock,
        sleep=sleep or (lambda seconds: None),
    )
    return scheduler, heartbeats, runs


def reading(db, specs=(), *, age_seconds=None, clock=None):
    """一份只带心跳读数的 `ScheduleData`：`age_seconds` 是那一拍距今多久。"""
    clock = clock or FrozenClock(MOMENT)
    heartbeats = HeartbeatRepo(db, clock=clock)
    if age_seconds is not None:
        # 写的那一侧用的是自己的时钟：与 `collect` 读的那个相差 `age_seconds`
        wrote_at = clock.now_utc() - timedelta(seconds=age_seconds)
        HeartbeatRepo(db, clock=FrozenClock(wrote_at)).start(note="tick=60s")
    return collect(
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=heartbeats,
        users=UserRepo(db),
        specs=specs,
        clock=clock,
    )


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
    # 不传 `now` 时读的是注入的那个时钟，不是墙钟
    assert report.now == MOMENT
    assert len(report.results) == 5
    assert report.errors == () and report.waiting == ()
    assert report.failed == ()


def test_failed_results_are_picked_out_of_the_tick(db, settings):
    runner = RecordingRunner(fail_for={"rules"})
    specs = (job_spec("bars", "A"), job_spec("rules", "A"))
    scheduler, _, _ = make(db, settings, specs, runner)

    report = scheduler.tick(as_of=FRIDAY)

    assert [result.job for result in report.results] == ["bars", "rules"]
    # 只有 `failed` 那一支是 FAILED：跑成的那条不能跟着一起被判成失败
    assert [result.job for result in report.failed] == ["rules"]


def test_as_of_uses_the_period_key_of_its_week(db, settings):
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("recheck", "A", "weekly"),), runner)

    scheduler.tick(as_of=FRIDAY, only_job="recheck", only_market="A")

    assert runner.calls == [("recheck", "A", "2024-W43", FRIDAY)]


def test_the_manual_anchor_is_not_the_clock_of_the_tick(db, settings):
    """补跑的那一天是 `as_of`，`now` 只是这一轮的时间戳。
    把锚点换成从时钟读出来的那一天，补跑会静默地写到另一个周期上（D98）。"""
    runner = RecordingRunner()
    scheduler, _, _ = make(db, settings, (job_spec("recheck", "A", "weekly"),), runner)
    december = datetime(2024, 12, 3, 8, 30, tzinfo=UTC)

    report = scheduler.tick(now=december, as_of=FRIDAY, only_job="recheck")

    assert runner.calls == [("recheck", "A", "2024-W43", FRIDAY)]
    assert report.now == december


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


def test_the_refusal_names_the_filter_and_the_registry(db, settings):
    """一句话要能看出两件事：哪一个词打错了（`job=`/`market=`），以及这里有什么。"""
    specs = (job_spec("bars", "A"), job_spec("fx", None, "fx"))
    scheduler, _, _ = make(db, settings, specs, RecordingRunner())

    with pytest.raises(ValidationError, match=r"job=nope"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")
    with pytest.raises(ValidationError, match=r"market=US"):
        scheduler.tick(as_of=FRIDAY, only_market="US")
    # 两个条件都敲上时两个都要说：只说一个会让人去改那个没敲错的地方
    with pytest.raises(ValidationError, match=r"job=bars、market=US"):
        scheduler.tick(as_of=FRIDAY, only_job="bars", only_market="US")
    with pytest.raises(ValidationError, match=r"现有 \['bars', 'fx'\]"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")


def test_an_empty_registry_is_refused_too(db, settings):
    """没登记 job 与「筛选条件敲错了」是两个原因：少了「全部」那一个字，
    前者会被读成后者的一个特例，于是人去查命令，而该查的是这里登记了什么。"""
    scheduler, _, _ = make(db, settings, (), RecordingRunner())

    with pytest.raises(ValidationError, match="没有匹配的 job（全部）"):
        scheduler.tick(as_of=FRIDAY)


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


def test_an_unexpected_calendar_error_is_not_collected_as_a_tick_error(db, settings):
    """这里隔离的只有 `SamsError`：日历实现里的编程错误不能被收成一句「这一轮算不出」，
    那样一种坏会被读成一种等。"""

    class BoomingCalendar(FakeCalendar):
        def is_trading_day(self, market, day):
            raise RuntimeError("日历在读一个不存在的键")

    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        calendar=BoomingCalendar(settings),
    )

    with pytest.raises(RuntimeError, match="不存在的键"):
        scheduler.tick(now=MOMENT)


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


def test_the_beat_goes_out_before_the_selection(db, settings):
    """一个 job 都没选出的那一轮，那一拍也已经打出去了：选 job 失败同样是
    「循环还在转」的一轮。挪到选取之后，读的那一侧就把它与「进程停了」混成一个。"""
    clock = FrozenClock(MOMENT)
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), clock=clock
    )
    scheduler.run_forever(max_ticks=1)
    clock.advance(seconds=30)

    with pytest.raises(ValidationError, match="没有匹配"):
        scheduler.tick(as_of=FRIDAY, only_job="nope")

    assert heartbeats.latest().beat_at == MOMENT + timedelta(seconds=30)


def test_run_forever_beats_on_every_tick(db, settings):
    runner = RecordingRunner()
    clock = FrozenClock(MOMENT)
    reports = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds=seconds)

    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), runner, clock=clock, sleep=sleep
    )

    ticks = scheduler.run_forever(max_ticks=2, on_tick=reports.append)

    assert ticks == 2
    assert len(reports) == 2
    beat = heartbeats.latest()
    assert beat.note == f"tick={settings.scheduler_tick_s}s"
    # 第二拍是第二轮 tick 打的，不是认领时那一次：时钟只被 `sleep` 推过一格
    assert beat.beat_at == MOMENT + timedelta(seconds=settings.scheduler_tick_s)
    # 等格数比拍数少一个：最后一拍之后不再等
    assert sleeps == [settings.scheduler_tick_s]


def test_zero_rounds_leave_no_tick_and_no_claim(db, settings):
    """`max_ticks=0`（或负数）一轮都不跑，也不认领心跳：留下一行刚打的拍会把下一个常驻循环
    挡在阈值外面，而这一次连活都还没开始。"""
    for max_ticks in (0, -1):
        runner = RecordingRunner()
        scheduler, heartbeats, _ = make(db, settings, (job_spec("bars", "A"),), runner)
        reports = []

        ticks = scheduler.run_forever(max_ticks=max_ticks, on_tick=reports.append)

        assert ticks == 0
        assert reports == []
        assert runner.calls == []
        assert heartbeats.latest() is None


def test_the_dependencies_are_passed_by_name(db, settings):
    """`jobs` 之后六个依赖里有三个同为仓库对象、两个同为注入的替身：写错顺序的两份
    都构造得出对象，症状要到第一次真跑才看得见。所以这一句 `TypeError` 是那道墙。"""
    args = (
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        TaskRunRepo(db),
        HeartbeatRepo(db),
        StubUniverse(),
        UserRepo(db),
        FakeCalendar(settings),
    )

    with pytest.raises(TypeError, match="positional"):
        Scheduler(*args)


def test_the_same_scheduler_can_start_its_loop_again(db, settings):
    scheduler, heartbeats, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    scheduler.run_forever(max_ticks=1)
    claimed = heartbeats.latest().id
    scheduler.run_forever(max_ticks=1)

    # 再次进循环不与自己抢心跳，也不另起一行
    assert heartbeats.latest().id == claimed


def test_a_second_daemon_is_refused(db, settings):
    first, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    second, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    first.run_forever(max_ticks=1)

    with pytest.raises(SchedulerBusy, match="心跳"):
        second.run_forever(max_ticks=1)


def test_the_guard_reads_the_last_beat_not_the_row_being_created(db, settings):
    """`started_at` 是那一行被创建的时刻，「还活着」看的是最后一次 `beat_at`。
    读错那一列，一个刚打过拍的循环会被看成六分钟前起、于是被接管。"""
    beat_id = HeartbeatRepo(db, clock=FrozenClock(MOMENT - timedelta(minutes=6))).start(
        note="tick=60s"
    )
    HeartbeatRepo(db, clock=FrozenClock(MOMENT)).beat(beat_id)
    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())

    with pytest.raises(SchedulerBusy, match="心跳"):
        scheduler.run_forever(max_ticks=1)


def test_a_dead_heartbeat_does_not_block_a_new_daemon(db, settings):
    old_clock = FrozenClock(MOMENT - timedelta(minutes=6))
    old = HeartbeatRepo(db, clock=old_clock).start(note="tick=60s")
    scheduler, heartbeats, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner()
    )

    scheduler.run_forever(max_ticks=1)

    assert heartbeats.latest().id != old


def test_exactly_at_the_threshold_is_stale_and_open_to_takeover(db, settings):
    data = reading(db, age_seconds=HEARTBEAT_STALE_S)
    assert data.beat_age_s == HEARTBEAT_STALE_S
    assert data.stale is True

    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    assert scheduler.run_forever(max_ticks=1) == 1


def test_one_second_under_the_threshold_is_fresh_and_refuses_takeover(db, settings):
    data = reading(db, age_seconds=HEARTBEAT_STALE_S - 1)
    assert data.stale is False

    scheduler, _, _ = make(db, settings, (job_spec("bars", "A"),), RecordingRunner())
    with pytest.raises(SchedulerBusy, match="心跳"):
        scheduler.run_forever(max_ticks=1)


def test_a_tick_not_shorter_than_the_heartbeat_threshold_is_refused(db, settings):
    """等待间隔不比阈值短，形状就是「一个空转的循环也会把自己读成停了」：
    读数与守卫共用同一个比较，两边一起翻错，所以在构造时就挡住，而不是等第一轮。"""
    for tick_s in (HEARTBEAT_STALE_S - 1, 1):
        make(
            db,
            replace(settings, scheduler_tick_s=tick_s),
            (job_spec("bars", "A"),),
            RecordingRunner(),
        )
    for tick_s in (HEARTBEAT_STALE_S, HEARTBEAT_STALE_S + 1):
        with pytest.raises(ValidationError, match="scheduler_tick_s"):
            make(
                db,
                replace(settings, scheduler_tick_s=tick_s),
                (job_spec("bars", "A"),),
                RecordingRunner(),
            )


def test_a_tick_level_failure_is_reported_and_the_loop_survives(db, settings, caplog):
    reports = []
    clock = FrozenClock(MOMENT)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds=seconds)

    scheduler, heartbeats, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        clock=clock,
        sleep=sleep,
        universe=BrokenUniverse(),
    )

    with caplog.at_level(logging.WARNING):
        ticks = scheduler.run_forever(max_ticks=2, on_tick=reports.append)

    assert ticks == 2
    assert all("db 挂了" in report.errors[0] for report in reports)
    # 那一拍在做活之前就打出去了：这一轮没做成事，不等于循环不转了
    assert heartbeats.latest().beat_at == MOMENT + timedelta(seconds=settings.scheduler_tick_s)
    # 每一轮失败都要留下一句：只留 `TickReport` 的话，日志里读不到「循环还在转但在失败」
    logged = [rec for rec in caplog.records if "这一轮 tick 失败" in rec.getMessage()]
    assert len(logged) == 2
    # 栈只能留在这里：这一句把异常吞了，往上再没有人能打印它
    assert "in build" in caplog.text


def test_an_interrupt_leaves_a_line_in_the_log(db, settings, caplog):
    """退出要留下一句：`run_forever` 正常返回，返回值看不出它是被打断的还是跑满了轮数——
    那一行是日志里唯一的区别。"""

    def interrupt(seconds):
        raise KeyboardInterrupt

    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        sleep=interrupt,
    )
    with caplog.at_level(logging.WARNING):
        ticks = scheduler.run_forever()

    assert ticks == 1
    assert "收到中断，调度器退出" in caplog.text


def test_an_unexpected_error_is_not_swallowed_by_the_loop(db, settings):
    scheduler, _, _ = make(
        db,
        settings,
        (job_spec("bars", "A"),),
        RecordingRunner(),
        universe=ExplodingUniverse(),
    )

    with pytest.raises(RuntimeError, match="编程错误"):
        scheduler.run_forever(max_ticks=3)


def test_ctrl_c_stops_the_loop_cleanly(db, settings):
    def interrupt(seconds):
        raise KeyboardInterrupt

    scheduler, _, _ = make(
        db, settings, (job_spec("bars", "A"),), RecordingRunner(), sleep=interrupt
    )
    reports = []

    try:
        ticks = scheduler.run_forever(on_tick=reports.append)
    except KeyboardInterrupt as exc:  # 中断是常驻循环的正常退出方式，不是要往上抛的错误
        pytest.fail(f"中断没有被接住：{exc}")

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
    # 只认「启用且 is_admin」：被禁用的管理员与不是管理员的普通账号各挡住一半
    assert data.admins == 1
    assert data.stale is False and data.never_started is False
    # 读数带着登记的那个周期：这一行读的是 `spec.period`，不是账本上那一行
    assert data.jobs[0].period == "daily"
    assert data.jobs[0].latest is None and data.jobs[0].streak == 0


def test_schedule_data_turns_stale_after_the_threshold(db, settings):
    data = reading(db, age_seconds=HEARTBEAT_STALE_S + 1)

    assert data.stale is True


def test_schedule_data_without_a_heartbeat_is_not_stale(db):
    data = reading(db)

    assert data.never_started is True
    assert data.stale is False
    assert data.beat_age_s is None


def test_the_reading_ages_the_last_beat_not_the_row_being_created(db):
    """与守卫读同一列：年龄从 `beat_at` 算。读成 `started_at` 时，一个一直在打的循环
    会被显示成「六分钟前起的、之后再没动过」。"""
    beat_id = HeartbeatRepo(db, clock=FrozenClock(MOMENT - timedelta(minutes=6))).start(
        note="tick=60s"
    )
    HeartbeatRepo(db, clock=FrozenClock(MOMENT - timedelta(seconds=30))).beat(beat_id)
    clock = FrozenClock(MOMENT)

    data = collect(
        runs=TaskRunRepo(db, clock=clock),
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(),
        clock=clock,
    )

    assert data.beat_age_s == 30
    assert data.stale is False


def test_a_beat_written_ahead_of_the_reading_clock_reads_as_zero(db):
    """写那一侧的时钟跑在读取侧前面（两个各自的 `Clock`、或系统时钟被校正过）时，
    年龄不能是负数——负数打印出来是一个看不出原因的数。"""
    heartbeats = HeartbeatRepo(db, clock=FrozenClock(MOMENT + timedelta(seconds=5)))
    heartbeats.start(note="tick=60s")

    data = collect(
        runs=TaskRunRepo(db, clock=FrozenClock(MOMENT)),
        heartbeats=heartbeats,
        users=UserRepo(db),
        specs=(),
        clock=FrozenClock(MOMENT),
    )

    assert data.beat_age_s == 0
    assert data.stale is False


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


def test_the_streak_counts_periods_not_rows(db):
    """同一个周期重试五次只有一个结论：按**行**数窗口时它会被数成五个失败周期。"""
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    for _ in range(5):
        run_id = runs.start("bars", market="A", period_key="2024-10-25")
        runs.finish(run_id, status=FAILED, error="boom")
        clock.advance(minutes=1)

    data = collect(
        runs=runs,
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(job_spec("bars", "A"),),
        clock=clock,
    )

    assert data.jobs[0].latest.period_key == "2024-10-25"
    assert data.jobs[0].streak == 1


def test_the_streak_saturates_at_the_display_cap(db):
    """比 `STREAK_DISPLAY_PERIODS` 更长的连败显示那个上限本身，不显示真实周期数。"""
    clock = FrozenClock(MOMENT)
    runs = TaskRunRepo(db, clock=clock)
    last_written = None
    for index in range(STREAK_DISPLAY_PERIODS + 1):
        day = FRIDAY - timedelta(days=index)
        last_written = day.isoformat()
        run_id = runs.start("bars", market="A", period_key=last_written)
        runs.finish(run_id, status=FAILED, error="boom")
        clock.advance(minutes=1)

    data = collect(
        runs=runs,
        heartbeats=HeartbeatRepo(db, clock=clock),
        users=UserRepo(db),
        specs=(job_spec("bars", "A"),),
        clock=clock,
    )

    assert data.jobs[0].streak == STREAK_DISPLAY_PERIODS
    assert data.jobs[0].latest.period_key == last_written


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

Expected: FAIL，`ModuleNotFoundError: No module named 'sams.services.scheduler.heartbeat'`
——收集阶段就在第一个 import 上炸，`report`/`service` 那两行要等这一行有了才会被读到，
所以这里只可能报这一个名字。

- [ ] **Step 3: 写 `heartbeat.py` 与 `report.py`**

`src/sams/services/scheduler/heartbeat.py`：

```python
"""心跳的过期阈值与「那一拍还算不算活着」这条判据。

判据只写在这一处，因为两头要同时翻：健康读数里的那个布尔（它可能死了）与
`Scheduler._claim`（放不放第二个常驻循环进来）。各写一遍的话，恰好踩在阈值上的
那一瞬会一个说「没停」、另一个已经放行。
放在独立的一个模块而不是 `report.py`：读的那一层只把数取回来，不该拥有「该不该
放一个常驻循环进来」这条策略。
"""

from __future__ import annotations

#: 心跳的年龄到这一步就算「可能已经停了」（D100 的守卫等的也是这个数）。
#: 它必须**大于**一轮的等待间隔（`scheduler_tick_s`）：否则一个什么都没做的循环
#: 也会在两次拍之间把自己读成停了。`Scheduler.__init__` 挡住这一条。
#: 一轮**做活**的时长不在这条约束里：那一拍只在每轮开头打，所以一轮如果做得比阈值
#: 久，还在做活的进程会被读成停了；要修它得在做活中间补拍，这里没有那些拍。
HEARTBEAT_STALE_S = 300


def stale_at(age_seconds: float) -> bool:
    """那一拍是不是已经过期。取 `>=`：恰好等于阈值算过期，两头共用这一个比较。"""
    return age_seconds >= HEARTBEAT_STALE_S
```

`src/sams/services/scheduler/report.py`：

```python
"""调度器的健康读数：它还在不在跑、每个 job 上一轮跑成什么。

只读：这一层不写 `task_runs`、不碰心跳，也不决定「该不该放一个常驻循环进来」——
「过期」那条判据归 `heartbeat.py`，这里只是拿它算一个布尔。
`streak` 与 runner 用的是同一个函数、同一个「每周期一行」的形状：读数里数出来的
连续失败必须与发不发 `SystemAlarm` 的口径是同一条（D103），否则「显示连败而管理员
一条都没收到」会查不出原因。
谁把这些数打印出来、退出码怎么定，由读它的那一侧决定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.services.actors import admin_users
from sams.services.scheduler.heartbeat import stale_at
from sams.services.scheduler.runner import failure_streak
from sams.services.scheduler.types import JobSpec
from sams.storage.repos.task_runs import Heartbeat, HeartbeatRepo, TaskRun, TaskRunRepo
from sams.storage.repos.users import UserRepo

#: 连续失败最多数出多少个**周期**。它会**饱和**：比这个上限还长的连败显示的是上限本身，
#: 不是真实的周期数——一屏要给的是「停了多久」的量级，而告警在第三个周期就响了（D103），
#: 再往上数不改变任何人该做的事。
STREAK_DISPLAY_PERIODS = 30


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
        """有过心跳、且已经过期：「它可能死了」只有这一条判据。
        发现它停了归外部（D101），所以这里只给读数，不发任何东西。
        """
        return self.beat_age_s is not None and stale_at(self.beat_age_s)

    @property
    def never_started(self) -> bool:
        """没有那一行：与「有过但停了」是两件事，前者不该被读成一次事故。"""
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
        # 下限 0：那一拍可能由一个跑在前面的时钟写下（两个各自的 `Clock`、或系统时钟被
        # 校正过），负年龄是一个看不出原因的数
        age = max(0, int((moment - beat.beat_at).total_seconds()))
    admins = len(admin_users(users))
    jobs = []
    for spec in specs:
        # 按周期分组：一个一直失败的周期每轮重试都多写一行，按行数窗口会被它自己占满
        periods = runs.latest_per_period(spec.name, spec.market, limit=STREAK_DISPLAY_PERIODS)
        jobs.append(
            JobHealth(
                name=spec.name,
                market=spec.market,
                period=spec.period,
                latest=periods[0] if periods else None,
                streak=failure_streak(periods),
            )
        )
    return ScheduleData(heartbeat=beat, beat_age_s=age, admins=admins, jobs=tuple(jobs))
```

`src/sams/services/actors.py` 末尾补上两个消费者共用的那一个筛选：

```python
def admin_users(users: UserRepo) -> list[User]:
    """要替「管理员」做的那一批人：启用且 `is_admin`。

    同一个判据抄两份，迟早一份漏一个条件（`active_users` 的文档头说的就是这件事）。
    这里两个消费者读的正是同一头：健康读数里那个「有几个管理员」与系统告警的收件人
    名单——数出来的个数与真发出去的人数对不上，就是「显示有管理员而谁都没收到」
    这种查不出原因的症状。
    """
    return [user for user in active_users(users) if user.is_admin]
```

`src/sams/services/scheduler/alarms.py` 的收件人循环改成读它：

```python
    def handle(self, alarm: SystemAlarm) -> None:
        now = self.clock.now_utc()
        since = now - timedelta(minutes=self.settings.cooldown_min)
        for user in admin_users(self.users):
            if self.alerts.has_system_alert(user.id, alarm.component, since=since):
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
                # 栈只能在这里留：异常被逐个人包住之后到不了 `EventBus.publish`（那里是另一处
                # 带栈的日志），少一个栈就没人能从那一句话找到抛出的是哪一段代码
                # （`runner.py` 答同一个问题时用的也是 `logger.exception`）
                logger.exception("系统告警发给 %s 失败：%s", user.id, exc)
```

- [ ] **Step 4: 写 `service.py`**

`src/sams/services/scheduler/service.py`：

```python
"""`Scheduler`：tick 的顺序、心跳与常驻循环。

一轮 tick 做四件事，顺序就是代码里的顺序：

1. 认领过心跳就打一拍（手工 `tick` 没认领过，什么也不留）；
2. 按 `only_job`/`only_market` 选出这一轮要跑的 job，一个都不匹配就拒绝；
3. 构建这一轮的对象全集——一次 tick 只构建一次，交给每个 job 的是同一份；
4. 按注册顺序逐个 job 判断到期并交给 `JobRunner`，把结果原样交回。

打拍放在做活之前：那一拍证明的是「循环还在转」，不是「这一轮做完了事」。挪到收尾
之后，一轮在打拍之前就失败（账本读不到）会一拍都不留，读的那一侧把「这一轮做不成」
看成「循环停了」——那是两件事：前者是这一轮的结论（`TickReport.errors`），后者才是
那一拍要答的。
两种放法的最坏年龄都是「一轮做活的时长 + 等待间隔」，挪到收尾换不来更准的读数。

等待间隔不比心跳阈值短的话，一个空转的循环也会在两次拍之间把自己读成停了，
所以构造时挡住（见 `heartbeat.py`）。

单个 job 的异常在 runner 里已经落账；这里只隔离**到期计算**的异常：
日历源坏了不能让排在后面的市场也不跑。

打印与退出码都不在这里：谁造这个循环、结果怎么给人看，由装配与入口那一侧决定。
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
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S, stale_at
from sams.services.scheduler.report import ScheduleData, collect
from sams.services.scheduler.runner import JobRunner, RunResult
from sams.services.scheduler.types import JobSpec
from sams.services.scheduler.universe import UniverseBuilder
from sams.storage.repos.task_runs import FAILED, HeartbeatRepo, TaskRunRepo
from sams.storage.repos.users import UserRepo

logger = logging.getLogger("sams.services.scheduler.service")


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
        *,
        runner: JobRunner,
        runs: TaskRunRepo,
        heartbeats: HeartbeatRepo,
        universe: UniverseBuilder,
        users: UserRepo,
        calendar: CalendarView,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.scheduler_tick_s >= HEARTBEAT_STALE_S:
            raise ValidationError(
                f"scheduler_tick_s={settings.scheduler_tick_s} 不小于心跳阈值 "
                f"{HEARTBEAT_STALE_S}：一个空转的循环也会在两次拍之间把自己读成停了，"
                "而读数与守卫用的是同一个比较，两边一起翻错"
            )
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
        moment = now if now is not None else self.clock.now_utc()
        if self._beat_id is not None:
            self.heartbeats.beat(self._beat_id)
        selected = self._selected(only_job, only_market)
        universe = self.universe.build()
        results: list[RunResult] = []
        waiting: list[str] = []
        errors: list[str] = []
        for spec in selected:
            if as_of is not None:
                # 手工补跑：指定的那一天就是这一轮的锚点，周期键仍按 job 自己的周期算，
                # 于是它与到期计算写出来的那一串形状一致（D98），runner 不必认识两种输入
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
                    # 只说这一个 job 这一轮算不出到期日：其余市场照常跑
                    errors.append(f"{spec.title}: {exc}")
                    continue
                if due is None:
                    # 没到期与算不出是两种结论：前者下一轮可能就到点了，后者是要人去修的，
                    # 混进同一个列表里就分不出该等还是该查
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
        """认领心跳，然后一轮一轮跑下去。返回实际跑过的 tick 数。

        `max_ticks=0`（或负数）一轮都不跑，也不认领心跳：留下一个刚打过的拍会把
        下一个常驻循环挡在阈值外面，而这一次连活都没开始。

        这里只接 `SamsError`：那一类是「这一轮做不成」（账本读不到、一个 job 都没选出），
        循环本身没坏，下一轮还得试。别的东西（编程错误）在这里没有可信的下一步，
        炸出去比原地重入更值钱——循环接住它之后，每一拍照打，读的那一侧只会把一个
        坏掉的实现看成「还在跑」。
        """
        if max_ticks is not None and max_ticks <= 0:
            return 0
        self._claim()
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                try:
                    report = self.tick()
                except SamsError as exc:
                    # 栈只能留在这里：这一句把异常吞了，`tick` 的那一层不再有人能打印它
                    logger.exception("这一轮 tick 失败：%s", exc)
                    report = TickReport(now=self.clock.now_utc(), errors=(str(exc),))
                if on_tick is not None:
                    on_tick(report)
                ticks += 1
                if max_ticks is None or ticks < max_ticks:
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

    def _claim(self) -> None:
        """认领心跳；已经有一个循环在跳就拒绝启动（D100 的单实例守卫）。

        只有 `run_forever` 调它：一次手工执行不该留下一行，让下一个常驻循环白等
        一个阈值（`HEARTBEAT_STALE_S` 说的那个数）。

        守卫读的是心跳表而不是进程内的一个标记，所以它挡得住「同一个进程里再造一个
        `Scheduler` 去跑常驻循环」。它挡不住另一个操作系统进程：库文件上的锁先就让
        第二个进程连不上（见 `storage/db.py` 里 `Database.open` 包 `duckdb.Error` 的
        那一处），那一类重复启动在走到这句守卫之前就已经炸在连接上。
        """
        if self._beat_id is not None:
            # 同一个实例再次进循环：上一轮自己认领的那一行不能把自己拒在外面
            return
        latest = self.heartbeats.latest()
        if latest is not None:
            age = (self.clock.now_utc() - latest.beat_at).total_seconds()
            if not stale_at(age):
                raise SchedulerBusy(
                    f"另一个调度器（{latest.id}）{int(age)} 秒前还有心跳："
                    "同一份库上跳两个循环会把同一个周期各跑一遍，而 `task_runs` 里"
                    "那一行是「这个周期跑过没有」的唯一依据（D98）"
                )
        self._beat_id = self.heartbeats.start(note=f"tick={self.settings.scheduler_tick_s}s")

    def _selected(self, only_job: str | None, only_market: str | None) -> tuple[JobSpec, ...]:
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
        # 一个都没匹配上是命令敲错了（`wanted`），还是这里本来就没登记 job（「全部」）：
        # 后者不说清就会被读成「这一轮跑完了，只是什么都没做」
        raise ValidationError(
            f"没有匹配的 job（{'、'.join(wanted) or '全部'}）：现有 {names}"
        )
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/services/test_scheduler_service.py tests/services/test_scheduler_alarms.py -q
```

Expected: PASS（37 + 11 = 48 枚）。

- [ ] **Step 6: 提交**

实现与评审收尾各一个提交：不改历史（`--amend` 与 `git reset --soft` 都不用），
所以评审发现的那几处口径由第二个提交说明。

```bash
# 009e65e —— 实现
git add src/sams/services/scheduler/report.py \
    src/sams/services/scheduler/service.py \
    tests/services/test_scheduler_service.py
git commit -m "feat(scheduler): tick 的顺序、心跳单实例守卫与健康读数"

# 1a25b36 —— 评审收尾
git add src/sams/services/scheduler/heartbeat.py src/sams/services/scheduler/report.py \
    src/sams/services/scheduler/service.py src/sams/services/scheduler/alarms.py \
    src/sams/services/actors.py src/sams/core/errors.py \
    tests/services/test_scheduler_service.py
git commit -m "fix(scheduler): 评审收尾——守卫不再谎称跨进程，判据与收件人各收成一处"
```

**评审收尾（2026-10-23）**：实现落在 `009e65e`，两路评审之后修在 `1a25b36`。

- **「两个常驻进程会替同一个周期各下一次结论」这条理由被实测推翻**（`1a25b36`）。
  DuckDB 对库文件持**独占锁，只读连接也要**：第二个进程连 `duckdb.connect(path)` 都过不去，
  `IO Error: Could not set lock on file` 在 `storage/db.py` 的 `Database.open` 被包成
  `StorageError`——它到不了 `_claim` 那一句。我复现过两次（两个 `python -c` 子进程，前一个
  握着连接睡 5 秒）。于是守卫真正挡的是**同一个进程里再造一个 `Scheduler` 去跑常驻循环**，
  `SchedulerBusy` 的消息、`_claim` 与 `stale_at` 的文档头、`core/errors.py` 里那句
  「另一个进程正在做这件事」、`collect` 里那句「由另一个进程写下」全部按这条事实改写；
  `report.py` 的「页面上数出来」与文件头里那节还不存在的 `[调度]` 也一并去掉
  （D96：不许对没接线的生产作现在时的陈述）。用例只按 `match="心跳"` 认这条消息，
  改文案不动断言形状。
- **这条事实的真正后果在下一个任务**：`sams health` 作为旁观者**在常驻进程活着的时候读不到那个库**，
  所以 D101 的「发现它停了归外部」不能实现成「另一个进程读心跳表」。Task 11/12 要重新决定
  那一读从哪儿来（要么 health 只读自己那一份库、承认读不到正在跑的进程，要么把那一拍
  写到一个不锁文件的载体上）。本轮没有替它做决定，只把话说清楚。
- **`scheduler_tick_s` 与阈值的关系从注释改成构造期守卫**（`Scheduler.__init__`）。
  原注释写「它必须长于一轮 tick，否则还在做活的进程会被读成停了」——那句把因果说反了：
  还在做活的那一轮不打拍，被读成停的原因是**做活时长**，而不是等待间隔；等待间隔太长的
  症状是**一个空转的循环**把自己读成停。现在 `tick_s >= HEARTBEAT_STALE_S` 直接
  `ValidationError`，用例两头各钉一次（阈值减一与 `1` 放行、阈值与阈值加一拒绝）。
  判据没搬进 `core/config.py`：`core` 不能 import `services`（分层测试就在那儿看着）。
- **打拍位置的换法只换来一件事**：那一拍在每轮开头打，两种放法的最坏年龄都是
  「一轮做活的时长 + 等待间隔」，所以「挪到收尾会更准」是假的；真的差别是一轮在打拍之前
  就失败（账本读不到）时，挪到收尾会一拍都不留，于是「这一轮做不成」被读成「循环停了」。
  钉它的是本来就有的 `test_a_tick_level_failure_is_reported_and_the_loop_survives`
  （断言那一轮的拍已落库）与新加的 `test_the_beat_goes_out_before_the_selection`
  （一个 job 都没选出的那一轮也已经打过拍）。
- **过期判据搬出读数层**（F1）：`HEARTBEAT_STALE_S` 与 `stale_at` 原来住在 `report.py`，
  而 `report.py` 的文件头写着「不替调度器判断该不该跑」——它却拥有「能不能放第二个常驻
  循环进来」这条策略。现在判据在 `heartbeat.py`，读数与守卫都从那儿 import，
  两头同时翻这条仍由 `test_exactly_at_the_threshold_is_stale_and_open_to_takeover`
  与 `_one_second_under_the_threshold…` 两枚用例钉住（同一个年龄，一次问读数、一次问守卫）。
- **「管理员」那半个谓词从两处收成一处**：`alarms.handle` 原来是 `active_users(...)` 里
  `if not user.is_admin: continue`，`report.collect` 原来自己 `sum(1 for ... if is_admin)`。
  两处读的是同一个谓词，收成 `services/actors.admin_users`（`active_users` 之上再加
  `is_admin`），actors 的文件头从「两个形状」改成「三个形状」。删掉 `is_admin` 那半会同时
  打红服务层与告警层（`2 failed, 10 passed`），删掉 `ACTIVE` 那半会打红
  `test_schedule_data_counts_admins_and_reads_the_heartbeat`（被禁用的管理员也被数进去）。
- **七枚原先站不住的钉补齐**（评审用变异找出来的洞，逐条对应一个变异）：`as_of` 与
  `now` 混同（把锚点换成时钟读出来的那一天，原用例全绿，因为 `MOMENT` 与 `FRIDAY` 是同一天）；
  `_claim` 与 `collect` 读错列（`beat_at` → `started_at`：一个六分钟前起、一直打到现在的循环
  会被看成早就停了）；`_selected` 的三种文案（`job=`、`market=`、两个都敲、以及空注册表
  要说「全部」——不说清就会被读成「这一轮跑完了，只是什么都没做」）；日历里的非
  `SamsError` 必须炸出 `tick()`（`except SamsError` 写成 `except Exception` 原先全绿）；
  「这一轮 tick 失败」那一句日志与它的栈（删掉 `logger.warning` 原先全绿，所以改成
  `logger.exception` 并断言 `in build`）；中断那句要留在日志里（返回值看不出它是被打断的
  还是跑满了轮数）。
- **`max_ticks=0` 从「跑一轮」改成「一轮都不跑，也不认领」**。第一版把循环条件写成
  `max_ticks is None or ticks < max_ticks`，与提前返回重复——变异「条件退回 `while True`」
  实测仍全绿，说明有一处在说谎。现在轮数只由循环条件答，等待只由「还有下一轮吗」答，
  提前返回只管认领；三个位置各有一枚变异打得红（`while True` 会挂死、去掉守卫的 sleep 会让
  `sleeps == [tick_s]` 变红、`<= 0` 写成 `< 0` 会让零轮那枚变红）。
- `Scheduler.__init__` 里 `jobs` 之后的六个依赖改成关键字参数：九个位置参数在装配那一步
  （Task 12）会写错顺序，而写错的两种顺序都构造得出对象。
- **一轮 21 枚变异**：16 枚（分层、阈值、`beat_at`、文案、`except`、两处日志、打拍顺序、
  提前返回、构造守卫、`admin_users` 两半、`stale_at`）+ 5 枚 `run_forever`，全部 RED；
  中途那一枚 GREEN 的（循环条件与提前返回重复）已按上一条修掉。变异脚本跑完逐字节还原，
  `git status` 干净。
- **修复轮的一次事故要说清**：接手时上一个后台子代理仍在写文件（它被派出去后我没收到完成
  通知），于是 `services/actors.py` 里出现了两个同名 `admin_users`——后定义的那个会静默
  盖掉前一个，ruff 与用例都不会叫。已停掉那个代理、去重、并把这一条写进后续 brief：
  修复轮要么我自己写，要么先确认代理已经结束。
- **第二轮评审（`1a25b36`）补的两枚**：① `max_ticks` 的负数——文档头写着「0（或负数）都不认领」，
  而 `<= 0` 写成 `== 0` 时 36 枚用例全绿（负数照样落进 `_claim`，只是循环条件替它挡住了
  跑轮，那一行心跳还是留下了：下一个常驻循环被挡 300 秒）。零轮那枚用例改成 `(0, -1)` 两轮
  都问，`== 0` 的变异现在打红。② 关键字边界——把 `__init__` 里那个 `*` 删掉，用例全绿：
  六个依赖里 `runs`/`heartbeats` 同为仓库对象、`runner`/`universe` 同为注入的替身，
  写错顺序的两种形状都构造得出对象。补 `test_the_dependencies_are_passed_by_name`
  （按位置构造必须 `TypeError`，与 `tests/core/test_retry.py` 那枚「必需关键字」同形）。
- **没采纳的建议与理由**：把两处的「那一拍多久以前」并成一个共用函数（`_claim` 要的是一个
  浮点年龄去比阈值，`collect` 要的是一个非负整数给人读；共用的只有判据 `stale_at`，
  两边的精度本来就该不同——并成一个会逼着守卫也吃一次 clamp，而 clamp 在守卫那一侧
  没有自己的用例可钉）。`report.py:34` 的 `JobHealth.period` 与 `service.py` 的
  `schedule_data()` 这一轮都还没有消费者：前者留给 Task 11（要么打印它，要么删字段），
  后者正是 Task 11 那条只读命令该走的入口，本轮不提前替它决定形状。
- **往后的任务要注意**：
  ① **DuckDB 的锁决定了 `sams health` 能读到什么**（见上），Task 11 不能假设旁观进程读得到
  心跳表；`services/health.py:290` 那个「未处理 N 条」把 `market='SYS'` 的行也算进去了，
  要按 `FILTER (WHERE market = 'SYS')` 拆开。
  ② `JobHealth.period` 现在没有任何读者：`sams health` 计划里那枚 `_print_schedule` 打的是
  `latest.period_key`，从不读 `job.period`。Task 11 要么把它打出来（一个 job 的周期是
  「连败三个周期」那句话的分母），要么删掉这个字段——留着没有主人的字段是 D96 说的死 API。
  ③ 计划后段的片段仍写着 `from sams.services.scheduler.report import HEARTBEAT_STALE_S`
  与 `STREAK_WINDOW`：判据在 `heartbeat.py`，窗口叫 `STREAK_DISPLAY_PERIODS`（30 个**周期**，
  会饱和）。
  ④ Task 12 仍必须真的 `alerter.attach(event_bus)`，`run_forever` 的 `max_ticks=0` 已定义成
  「不跑也不认领」，命令行映射时别把「零轮」当成「无限」。
  ⑤ Task 12 那段装配里的 `Scheduler(settings, jobs, runner, runs, heartbeats, universe, users,
  calendar, ...)` 是八个位置参数，现在 `jobs` 之后一律关键字——照抄那段会 `TypeError`
  （这是要的：九个位置参数写错顺序时，两种顺序都构造得出对象）。

### Task 11: 健康页的 `[调度]` 与退出码

**Files:**
- Modify: `src/sams/services/health.py`（`KEY_TABLES` 两张台账、`NotifyData` 的两个系统数、
  `HealthReport.schedule` 与 `schedule_stale`、构造参数 `schedule`）
- Modify: `src/sams/cli/health.py`（`_age`、`_print_schedule`、`_print_backlog` 的系统那一行、退出码）
- Test: `tests/services/test_health.py`（追加 5 枚 + `service` 辅助函数接 `schedule`）
- Test: `tests/cli/test_health_cli.py`（新建，19 枚）

D101 说「心跳丢失的发现归外部」：死掉的调度器发不出「我死了」，所以这一屏的那一句与
`sams health` 的退出码是那条决定唯一的执行点。屏幕上那句给人读，那个码给不盯屏幕的人读。
`schedule` 是一个**函数**而不是 `Scheduler` 对象：`services/health.py` 只要那份
`ScheduleData` 的形状，不需要在 import 图上连到调度器的循环；不传时这一节写「未接入」
而不是从屏幕上消失。

**这一节读的是库，不是那个进程**（实测过两次）：DuckDB 1.5.6 对库文件持独占锁，
常驻调度器活着的时候，另一个 `sams` 命令连 `read_only=True` 都开不了这个文件。所以
Task 12 接线之后 `[调度]` 说的也是「上一次它留下的记录」，而「它此刻还活着吗」要由那一任务
决定载体：要么调度器把那一拍写到一个不锁文件的旁路上，要么承认这个旁观者读不到活的进程。

- [ ] **Step 1: 写失败用例**

`tests/services/test_health.py`：辅助函数接住 `schedule`，末尾追加五枚。

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

顶部这一段 import（这一枚新增的是 `ScheduleData` 与 `Alert`/`AlertRepo`）：

```python
from sams.services.health import KEY_TABLES, HealthService
from sams.services.scheduler.report import ScheduleData
from sams.storage.db import Database
from sams.storage.migrate import apply_migrations, discover
from sams.storage.repos.alerts import Alert, AlertRepo
from sams.storage.repos.bars import BarRepo
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.financials import FinancialRepo, FundingFlowRepo, ValuationRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo
```

```python
# ---------- 调度器读数：随报告带上，系统积压单独数 ----------


def test_key_tables_include_the_scheduler_ledgers(db, settings) -> None:
    """两张表各答一个问题，所以都要在这一屏上：`task_runs` 是「每个 job 跑成什么」，
    `scheduler_heartbeats` 是「还在跑吗」（D101：判「它还在不在跑」的读者必须在循环之外）。"""
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.tables["task_runs"] == 0
    assert report.tables["scheduler_heartbeats"] == 0


def test_report_carries_the_schedule_reading(db, settings) -> None:
    """`schedule` 是函数不是调度器对象：这一层调用它、存下结果，不认识循环。

    这里给的是 `collect` 真正会产出的形状（没行就没有年龄）；「从未启动不是过期」
    这一条在 `tests/services/test_scheduler_service.py` 里从 `collect` 那一层钉住。
    """
    data = ScheduleData(heartbeat=None, beat_age_s=None, admins=1, jobs=())
    report = service(
        db, settings, StubAdapter("akshare", ["A"], sample_bars()), schedule=lambda: data
    ).report()
    assert report.schedule is data
    assert report.schedule_stale is False


def test_an_unwired_reader_gives_no_reading_rather_than_a_blank_one(db, settings) -> None:
    """「没接入」是 None，与「接入了但从未启动」（`ScheduleData(heartbeat=None)`）
    是两句话，必须在类型上分得开：把前者显示成后者，运维会去查一个没跑过的调度器。"""
    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.schedule is None
    assert report.schedule_stale is False


def make_sys_alert(alerts: AlertRepo, user_id: str, component: str, moment: datetime) -> Alert:
    """一条系统告警的管理员副本：形状照 `has_system_alert` 的 WHERE（`market='SYS'`、
    `rule_id` 为 NULL、`code` 是组件名）。写回来的那一行给用例比时刻用。"""
    return alerts.create(
        user_id,
        rule_id=None,
        rule_name="系统",
        account_id=None,
        account_name="",
        market="SYS",
        code=component,
        symbol_name="",
        severity="mid",
        stage="调度",
        value_text="",
        threshold_text="",
        action_note="",
        triggered_at=moment,
    )


def test_system_alerts_are_not_counted_as_user_backlog(db, settings) -> None:
    """一条系统告警在每个管理员名下各落一行（D102）：把它们数进用户积压，
    「2 个组件在反复告警、2 个管理员」会显示成「未处理 4 条」，而那两句修法是两件事。

    组件数用 `count(DISTINCT code)` 而不是数行：一行管理员副本不是一个新问题。
    四个数都钉（两批的条数与两批各自最早那条）：只钉条数的话，把用户的最早时刻
    填进系统那一格是绿的，而屏幕上那一句会把「一小时前开始的故障」说成「刚发生的」。
    """
    alerts = AlertRepo(db)
    trader = UserRepo(db).create("trader", PASSWORD).id
    keeper = UserRepo(db).create("keeper", PASSWORD).id
    moment = datetime.now(UTC)
    # 系统那一批更早：两批的最早时刻必须是两个不同的数，才有东西可换错
    hour_earlier = moment - timedelta(hours=1)
    user_rows = [
        alerts.create(
            trader,
            rule_id="rule-1",
            rule_name="PR 低",
            account_id=None,
            account_name="主账户",
            market="A",
            code=code,
            symbol_name="某票",
            severity="mid",
            stage="入池",
            value_text="PR=0.31",
            threshold_text="PR≤0.4",
            action_note="",
            triggered_at=moment,
        )
        for code in ("600519", "000001")
    ]
    sys_rows = [
        make_sys_alert(alerts, trader, "sync", hour_earlier),
        make_sys_alert(alerts, keeper, "sync", hour_earlier),
    ]

    report = service(db, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
    assert report.notify.unhandled == 2, "用户积压不混系统副本"
    assert report.notify.unhandled_system == 1, "2 行同组件 = 1 个问题"
    assert report.notify.unhandled_oldest == min(r.triggered_at for r in user_rows)
    assert report.notify.unhandled_system_oldest == min(r.triggered_at for r in sys_rows)


def test_the_system_backlog_uses_the_same_maybe_line_as_the_rest(tmp_path, settings) -> None:
    """未迁移的库里那两个新数也是 None：沿用 `NotifyData` 那一节其余那几个数的同一条线（D93），
    而不是给系统积压另立第二条「该跑迁移」的判据。库的形状照旧用例的办法造：只到 0014。"""
    partial = tmp_path / "migrations"
    partial.mkdir()
    for path in discover():
        if not path.name.startswith("0015"):
            (partial / path.name).symlink_to(path)
    fresh = Database.open(tmp_path / "pre-0015.duckdb")
    try:
        apply_migrations(fresh, partial)
        report = service(fresh, settings, StubAdapter("akshare", ["A"], sample_bars())).report()
        assert report.notify.unhandled_system is None
        assert report.notify.unhandled_system_oldest is None
    finally:
        fresh.close()
```

`tests/cli/test_health_cli.py`（新建，整份就是这些）：

```python
"""`sams health` 的 `[调度]` 一节与退出码：钉屏幕和退出码，不钉装配。

容器是手搭的 `SimpleNamespace`：装配归后面的任务，这些用例只回答
「给定一份读数，这一屏该说什么、该以什么码退出」。过期与新鲜那两枚用
`HEARTBEAT_STALE_S` 取数而不是写死秒数——阈值改了这两枚要跟着翻，
不许出现「阈值改了而测试还绿」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sams.cli.health import show
from sams.services.health import HealthReport, NotifyData
from sams.services.scheduler.heartbeat import HEARTBEAT_STALE_S
from sams.services.scheduler.report import JobHealth, ScheduleData
from sams.storage.repos.task_runs import Heartbeat, TaskRun

MOMENT = datetime(2024, 10, 25, 15, 30, tzinfo=UTC)


def beat(age_seconds: int) -> Heartbeat:
    """一行心跳，`age_seconds` 前打过最后一拍。

    这一层的判据读的是 `ScheduleData.beat_age_s`（`collect` 由那一拍算出来），
    时间戳只负责让形状像「有过那一拍」——与 `heartbeat=None` 的那一格分开。
    """
    return Heartbeat(
        id="hb-1",
        started_at=MOMENT - timedelta(seconds=age_seconds),
        beat_at=MOMENT - timedelta(seconds=age_seconds),
        note="tick=60s",
    )


def run_for(
    name: str,
    *,
    market: str | None = "A",
    period: str = "daily",
    period_key: str | None = "2024-10-25",
    status: str = "ok",
    finished_at: datetime | None = MOMENT,
    error: str | None = None,
    note: str | None = None,
    streak: int = 0,
) -> JobHealth:
    """一行台账加上它的连败计数：`[调度]` 里 job 那一行的全部输入。

    默认是「一次干净的成功」——无错误、无说明、无连败，用例只写自己要改的那一格。
    """
    run = TaskRun(
        id=f"r-{name}",
        job_name=name,
        market=market,
        period_key=period_key,
        started_at=MOMENT - timedelta(minutes=5),
        finished_at=finished_at,
        status=status,
        rows_affected=1,
        error=error,
        note=note,
    )
    return JobHealth(name=name, market=market, period=period, latest=run, streak=streak)


def reading(*, age: int | None, admins: int = 2, jobs: tuple[JobHealth, ...] = ()):
    """一份读数。`age=None` 就是「还没有那一拍」那一格（`never_started`）。"""
    return ScheduleData(
        heartbeat=None if age is None else beat(age),
        beat_age_s=age,
        admins=admins,
        jobs=jobs,
    )


def report_with(schedule: ScheduleData | None = None, notify: NotifyData | None = None):
    return HealthReport(db_path=":memory:", schedule=schedule, notify=notify or NotifyData())


@pytest.fixture
def invoke(settings, capsys):
    """跑一遍 `sams health` 的 `show`，拿 (退出码, 屏幕)。只带这一屏真正读的四样。"""

    def call(report: HealthReport) -> tuple[int, str]:
        container = SimpleNamespace(
            health=SimpleNamespace(report=lambda: report),
            settings=settings,
            source_admin=SimpleNamespace(list=lambda: []),
            registry=SimpleNamespace(market_sources={}, chain_for=lambda market, cap: ()),
        )
        capsys.readouterr()
        code = show(container, SimpleNamespace())
        return code, capsys.readouterr().out

    return call


def test_beat_exactly_at_the_threshold_is_stale_and_exit_1(invoke) -> None:
    """恰好等于阈值算过期：`stale_at` 取 `>=`，这一枚钉的就是那个等号。

    退出码这一头是 D101 唯一的执行点：屏幕那句给人读，这个码给不盯屏幕的读者读。
    """
    code, text = invoke(report_with(schedule=reading(age=HEARTBEAT_STALE_S)))
    assert code == 1, "「它可能已经停了」必须让读退出码的人拿到非零"
    assert "常驻调度器可能已经停了" in text


def test_one_second_under_the_threshold_is_fresh_and_exit_0(invoke) -> None:
    """同一阈值的另一头：少一秒就不算「可能停了」，码回到 0。

    与上一枚差一秒、差一个码，才是那个 `>=` 的两边而不是一个写死的秒数。
    """
    code, text = invoke(report_with(schedule=reading(age=HEARTBEAT_STALE_S - 1)))
    assert code == 0
    assert f"心跳 {(HEARTBEAT_STALE_S - 1) // 60} 分钟前 · 管理员 2 名" in text


def test_never_started_prints_a_line_but_exits_0(invoke) -> None:
    """一次都没跑过的库不是事故：`never_started` 不进退出码，那一格自己会说。"""
    code, text = invoke(report_with(schedule=reading(age=None)))
    assert code == 0
    assert "从未启动" in text and "sams run start" in text


def test_zero_admins_is_said_loudly_without_touching_the_exit_code(invoke) -> None:
    """「有 0 个管理员但循环还在跑」是合法的一格：两句话都要打，码还是 0。"""
    code, text = invoke(report_with(schedule=reading(age=60, admins=0)))
    assert "没有在用的管理员" in text
    assert "管理员 0 名" in text, "新鲜那一行的「管理员 N 名」照打，两句不冲突"
    assert code == 0


def test_a_stale_beat_over_an_hour_shows_hours_and_minutes(invoke) -> None:
    """时长串走「小时 + 分钟」那一支：7500 秒在屏上该是「2 小时 5 分钟」。"""
    _, text = invoke(report_with(schedule=reading(age=2 * 3600 + 5 * 60)))
    assert "已经 2 小时 5 分钟没更新" in text


def test_an_exact_hour_does_not_get_a_zero_minute_tail(invoke) -> None:
    """整小时不补「0 分钟」：7200 秒那一格的信息量是「两个小时没拍了」，
    「2 小时 0 分钟」读起来像秒表，会让那一行显得还差一分钟就到下一个数。"""
    _, text = invoke(report_with(schedule=reading(age=2 * 3600)))
    assert "已经 2 小时没更新" in text
    assert "0 分钟" not in text


def test_a_stale_beat_of_whole_minutes_shows_minutes(invoke) -> None:
    """整分钟的读数不该被写成秒：600 秒那一格打「10 分钟」。"""
    _, text = invoke(report_with(schedule=reading(age=10 * 60)))
    assert "已经 10 分钟没更新" in text


def test_minutes_drop_the_leftover_seconds(invoke) -> None:
    """不足一分钟的零头被折掉：1840 秒打「30 分钟」而不是「30 分 40 秒」。"""
    _, text = invoke(report_with(schedule=reading(age=30 * 60 + 40)))
    assert "已经 30 分钟没更新" in text
    assert "40 秒" not in text


def test_a_fresh_beat_under_a_minute_shows_seconds(invoke) -> None:
    """不足一分钟时给秒：那一支被折成「0 分钟前」，屏上就分不出刚打的与半分钟前的。"""
    _, text = invoke(report_with(schedule=reading(age=30, admins=1)))
    assert "心跳 30 秒前" in text


def test_a_failed_run_shows_period_status_streak_and_error(invoke) -> None:
    """job 那一行：周期是「连续失败 N 个周期」的分母，没有这一列就说不清连败多久。

    错误串折到 60 个字符：一条带栈的源错误能有几百字，而这一屏是给人扫的，
    「哪个 job 坏了」由前面那三列说，不由栈说。
    """
    long_error = "connection reset by peer after 3 retries: " + "X" * 80
    job = run_for("bars", status="failed", error=long_error, streak=2)
    _, text = invoke(report_with(schedule=reading(age=60, jobs=(job,))))
    assert "bars" in text
    assert "failed" in text
    # 「daily」是 `job.period` 的读者：没有这一断言，健康页上的周期字段没有主人
    assert "daily" in text
    assert "周期 2024-10-25" in text
    assert "连续失败 2 个周期" in text
    assert long_error[:60] in text
    assert long_error[:61] not in text, "折痕在 60 个字符：多一个字符就是这一枚的另一边"
    assert "X" * 60 not in text, "整条栈不该把这一行撑开"
    # 收尾时刻也在这一行：`周期 2024-10-25` 说的是哪个周期，时刻说的是它什么时候结束的
    assert "2024-10-25 15:30:00 UTC" in text


def test_a_still_running_row_shows_no_completion_time(invoke) -> None:
    """崩溃留下的那一行是 `running`、没有收尾时刻（D105）：没有时刻就什么都不打，
    而不是落一个「–」——那个破折号会被读成「跑完了，只是库里没记时间」。"""
    job = run_for("bars", status="running", finished_at=None)
    _, text = invoke(report_with(schedule=reading(age=60, jobs=(job,))))
    line = next(ln for ln in text.splitlines() if "bars" in ln)
    assert "running" in line
    assert "UTC" not in line and "–" not in line


def test_the_note_on_a_run_is_printed(invoke) -> None:
    """`note` 说的是「成了，但要补一句」：`error` 是没成，`note` 是成了却只更新了 3 只
    （0018 的头注），runner 写的「周期 … 已经成功」也落在这一列。

    两行一起给：只钉带说明的那一行，会放过「干净的那行也被打上一段」——那时运维看到的
    两个 `ok` 长得一样，而其中一个其实只更新了 3 只。
    """
    jobs = (run_for("fx", market=None, note="等 A 股收盘后才换汇"), run_for("bars"))
    _, text = invoke(report_with(schedule=reading(age=60, jobs=jobs)))
    noted = next(ln for ln in text.splitlines() if "等 A 股" in ln)
    clean = next(ln for ln in text.splitlines() if ln.lstrip().startswith("bars"))
    assert "ok" in noted and "全局" in noted, "没有市场的 job 报「全局」而不是空白"
    assert clean.endswith("2024-10-25 15:30:00 UTC"), text
    assert "None" not in clean and "连续失败" not in clean


def test_a_zero_streak_is_not_printed_as_zero_failures(invoke) -> None:
    """`streak=0` 那一格什么都不说：「连续失败 0 个周期」会把一次正常的运行读成
    「刚停下来」，而这一屏上「没坏」的正确表达是少一段字。"""
    _, text = invoke(report_with(schedule=reading(age=60, jobs=(run_for("bars", streak=0),))))
    assert "连续失败" not in text


def test_a_run_row_without_a_period_key_shows_a_dash(invoke) -> None:
    """`period_key` 是 0018 用 `ALTER TABLE … ADD COLUMN` 加上的，DuckDB 那一句带不了
    NOT NULL，所以库里这一列可以是 NULL：这里落一个「–」而不是把 `None` 打上屏幕。"""
    _, text = invoke(
        report_with(schedule=reading(age=60, jobs=(run_for("bars", period_key=None),)))
    )
    line = next(ln for ln in text.splitlines() if ln.lstrip().startswith("bars"))
    assert "周期 –" in line and "None" not in line


def test_the_columns_line_up_across_the_widest_inputs(invoke) -> None:
    """名字与周期两列的宽度由注册表算得出（`financials` 10 个字符 → 留 11、
    `monthly` 7 个 → 正好 7）；市场那一列的 4 不是：市场码是 `canonical_market`
    大写后的自由字符串，现在用到的 `A`/`HK` 都只有 1~2 个字符，「全局」占 2 个。

    `:<` 只补不截，哪一列留窄了后面的状态列就跟着左移，一屏里两行不齐就读不出
    「谁在反复失败」——对齐这一枚钉的是三列**当下**齐不齐，不是「这三格永远够宽」。
    """
    jobs = (
        run_for("financials", period="daily"),
        run_for("calendar", period="monthly"),
        run_for("bars", market="HK", period="daily"),
    )
    _, text = invoke(report_with(schedule=reading(age=60, jobs=jobs)))
    lines = [
        ln
        for name in ("financials", "calendar", "bars")
        for ln in text.splitlines()
        if ln.lstrip().startswith(name)
    ]
    assert len(lines) == 3, text
    offsets = {ln.index("ok") for ln in lines}
    assert len(offsets) == 1, f"三行的状态列没对齐：{lines}"


def test_a_job_that_never_ran_says_so(invoke) -> None:
    """登记了但一行台账都没有：与「跑过且失败」是两件事，前者是还没轮到。"""
    job = JobHealth(name="bars", market="A", period="daily", latest=None, streak=0)
    _, text = invoke(report_with(schedule=reading(age=60, jobs=(job,))))
    assert "从未运行" in text
    assert "daily" in text, "从未运行的那一行也报周期：它是「该不该轮到」的分母"


def test_unwired_reading_keeps_the_section_and_exits_0(invoke) -> None:
    """这一节不因「没接入」而消失：缺席会被读成「调度器好好的」，
    而那一句说的才是事实——还没有读数接进来。"""
    code, text = invoke(HealthReport(db_path=":memory:"))
    assert "[调度]" in text
    assert "未接入调度器读数" in text
    assert code == 0


def test_an_unmigrated_db_says_the_version_and_no_backlog_numbers(invoke) -> None:
    """未迁移的那一屏只说「该跑迁移」，不接着报积压：`NotifyData()` 里那八个数是 `None`，
    而 `None` 是「没查过」不是「查出来是 0」。把两者印在同一屏上，读的人会在一个
    还没有 `alerts` 表形状的库里去找「谁没看告警」。

    这一枚钉的是 `_print_notify` 里那句 `return`：它是「这条线只判一次」的唯一主人。
    """
    _, text = invoke(report_with(notify=NotifyData()))
    assert "库里还没有 0015 的形状" in text
    assert "未处理告警" not in text, "未迁移的库不报用户积压，连 0 都不报"
    assert "个组件未处理" not in text


def test_system_backlog_gets_its_own_line_not_the_user_one(invoke) -> None:
    """系统与用户是两个受害者，分两行：给用户的那句修法（`sams alert list`）
    答不了「哪个 job 在反复失败」，而这一屏下面的 `[调度]` 那一节能答。"""
    notify = NotifyData(
        channels=1,
        enabled=1,
        sent=0,
        failed=0,
        suppressed=0,
        no_channel=0,
        unhandled=0,
        unhandled_system=2,
        unhandled_system_oldest=MOMENT,
    )
    code, text = invoke(report_with(notify=notify))
    assert "系统告警 2 个组件未处理" in text
    assert "`[调度]` 那一节按 job 列了最近一次的结论" in text
    assert "未处理告警 2 条" not in text, "用户那句说的还是用户的 0 条"
    assert "未处理告警 0 条" in text
    assert code == 0

```

- [ ] **Step 2: 跑红**

```bash
uv run pytest tests/services/test_health.py tests/cli/test_health_cli.py -q
```

Expected: FAIL（`HealthService` 没有 `schedule` 参数；`HealthReport` 没有 `schedule` 字段；
`NotifyData` 没有那两个系统数；`sams.cli.health.show` 还没有 `[调度]`）。

- [ ] **Step 3: 改 `services/health.py`**

注解只 import 类型，运行时不连调度器：

```python
if TYPE_CHECKING:
    # 只在注解里出现：健康这一层要的是 `ScheduleData` 的**形状**，不是调度器的循环
    # （运行时 import 会把 services.health 连到调度器的依赖图上，见 `HealthService.__init__`）
    from sams.services.scheduler.report import ScheduleData
```

`KEY_TABLES` 末尾（`"notify_log"` 之后）：

```python
    # 调度器的两张台账各答一个问题：`task_runs` 是「每个 job 上一轮跑成什么」，
    # `scheduler_heartbeats` 是「还在跑吗」（D101：判「它还在不在跑」的读者必须在循环之外）
    "task_runs",
    "scheduler_heartbeats",
```

`NotifyData` 末尾加两个数（`None` 沿用 D93 那条「库里还没有 0015 的形状」的线）：

```python
    #: 系统积压：**未处理的组件个数**（`code` 存的是组件名，D102）而不是行数——
    #: 一行管理员副本不是一个新问题，数行数会把同一个故障按管理员数放大。
    #: 最早一条的时刻与用户那两个数走同一条可空线：未迁移时保持 `None`
    unhandled_system: int | None = None
    unhandled_system_oldest: datetime | None = None
```

`HealthReport` 末尾加读数：

```python
    #: 调度器的读数。**None 是「没接入」**，与接入了但从未启动
    #: （`ScheduleData(heartbeat=None, ...)`）是两件事，必须在类型上分得开：
    #: 把前者显示成后者，运维会去查一个从没跑过的调度器
    schedule: ScheduleData | None = None
```

以及退出码唯一读的那条判据（它排在 `degraded_sources` 之后）：

```python
    @property
    def schedule_stale(self) -> bool:
        """有过心跳且已经过期：退出码里只有这一条算「它可能死了」（D101）。
        从未启动不是事故、没接入更不是，所以这里不读 `never_started`。"""
        return self.schedule is not None and self.schedule.stale
```

构造参数与赋值：

```python
        schedule: Callable[[], ScheduleData] | None = None
```

```python
        #: 调度器读数同理，而且**函数比调度器对象更对**：健康这一层不认识循环，
        #: 只要一份 `ScheduleData` 的形状；传对象会把 `services.health` 连到调度器的依赖图上
        self.schedule = schedule
```

```python
            schedule=self.schedule() if self.schedule is not None else None,
```

`_notify_data` 里用户那两问排除 `market='SYS'`，系统那两问只认 `SYS` 且**数组件**：

```python
        # 用户积压排除 `market='SYS'`：系统告警在每个管理员名下各有一行副本，
        # 混进来的话「2 个组件、2 个管理员」会显示成「未处理 4 条」
        unhandled = int(
            self.db.scalar(
                "SELECT count(*) FROM alerts WHERE status = 'new' AND market <> 'SYS'"
            ) or 0
        )
        oldest = (
            self.db.scalar(
                "SELECT min(triggered_at) FROM alerts"
                " WHERE status = 'new' AND market <> 'SYS'"
            )
            if unhandled
            else None
        )
        # 数**组件**而不是数行：`code` 存的是组件名（D102），一行管理员副本不是一个新问题
        system_unhandled = int(
            self.db.scalar(
                "SELECT count(DISTINCT code) FROM alerts"
                " WHERE status = 'new' AND market = 'SYS'"
            ) or 0
        )
        system_oldest = (
            self.db.scalar(
                "SELECT min(triggered_at) FROM alerts WHERE status = 'new' AND market = 'SYS'"
            )
            if system_unhandled
            else None
        )
```

```python
            unhandled=unhandled,
            unhandled_oldest=oldest,
            unhandled_system=system_unhandled,
            unhandled_system_oldest=system_oldest,
```

- [ ] **Step 4: 改 `cli/health.py`**

积压那一节分成两行（两个受害者、两句修法）：

```python
def _print_backlog(notify) -> None:
    """积压那两行，只在「库已经是 0015 的形状」时被调用：那一句由 `_print_notify` 判、
    判完就 return，这条线只在那一处判（在这里再判一次就是第二个主人）。

    用户与系统各一行：两个受害者、两句修法——`sams alert list` 答不了「哪个 job 在反复失败」。
    """
    if notify.unhandled:
        oldest = (
            f"（最早 {stamp(notify.unhandled_oldest)}）" if notify.unhandled_oldest else ""
        )
        print(
            f"  ⚠ 未处理告警 {notify.unhandled} 条{oldest}"
            "：`sams alert list --user … --status new`"
        )
    else:
        print("  未处理告警 0 条")
    if notify.unhandled_system:
        oldest = (
            f"（最早 {stamp(notify.unhandled_system_oldest)}）"
            if notify.unhandled_system_oldest
            else ""
        )
        print(
            f"  ⚠ 系统告警 {notify.unhandled_system} 个组件未处理{oldest}"
            "：下面 `[调度]` 那一节按 job 列了最近一次的结论"
        )
```

`_print_notify` 之后是这一节的两函数：`_age` 把秒折成能扫一眼的时长，`_print_schedule`
把读数打成屏幕。

```python
def _age(seconds: int) -> str:
    """把「距今多少秒」折成人能扫一眼的时长：一个四位数秒值在屏幕上没有量感。

    整小时不补一个「0 分钟」：「1 小时 0 分钟没更新」读起来像秒表，而那一格的
    信息量就是「一个多小时没拍了」。
    """
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"


def _print_schedule(report) -> None:
    """`[调度]`：它还在不在跑，加上每个 job 上一轮跑成什么。

    死掉的调度器发不出「我死了」（D101），所以「还在不在跑」这句话只能由外面的人说。
    「未接入」也要占一行而不是让这一节消失——缺席会被读成「调度器好好的」。
    过期那一句之外，同一件事还进 `show` 的最后一行：屏幕上那句给人读，那个码给
    不盯屏幕的人读。
    """
    print("[调度]")
    if report.schedule is None:
        print("  未接入调度器读数")
        return
    data = report.schedule
    # 到下面两支时年龄已经折得出来：`collect` 把那一拍与年龄绑在同一条腿上（有行才算得出数）。
    # 那个「有行没有数」的形状不在这里兜住：炸出去的那一句正指着写错的调用方，
    # 而屏上一个不动声色的「未知」只会让读的人以为一切都好
    if data.never_started:
        print("  从未启动（`sams run start` 启动常驻调度器）")
    elif data.stale:
        print(f"  ⚠ 心跳已经 {_age(data.beat_age_s)}没更新：常驻调度器可能已经停了")
    else:
        print(f"  心跳 {_age(data.beat_age_s)}前 · 管理员 {data.admins} 名")
    if data.admins == 0:
        # 与上一行的「管理员 0 名」不冲突：那一行报事实，这一行报后果——
        # 「循环还在跑」与「告警没有一个收件人」恰好可以同时成立
        print(
            "  ⚠ 没有在用的管理员：系统告警一个收件人都没有"
            "（`sams user promote --username …`）"
        )
    for job in data.jobs:
        # 周期紧跟着名字：它是「连续失败 N 个周期」的分母——
        # 日频 job 连败 2 个周期与月频 job 连败 2 个周期是两个不同的事
        # 名字那一列留宽到注册表里最长的那个名字（现在是 `financials`，10 个字符）：
        # `:<` 只补不截，留窄一格后面的列就跟着左移，两行不齐时读不出「谁在反复失败」。
        # 注册表里长出更长的名字时这一格要跟着改——用例钉的是当下这三列齐不齐，
        # 不是「这一格永远够宽」
        line = f"  {job.name:<11} {(job.market or '全局'):<4} {job.period:<7}"
        latest = job.latest
        if latest is None:
            print(line + " 从未运行")
            continue
        line += f" {latest.status:<7} 周期 {latest.period_key or '–'}"
        if latest.finished_at:
            line += f" · {stamp(latest.finished_at)}"
        if job.streak:
            line += f" · 连续失败 {job.streak} 个周期"
        if latest.error:
            line += f" · {latest.error[:60]}"
        if latest.note:
            line += f" · {latest.note}"
        print(line)
```

`show` 里 `_print_notify(report)` 之后加 `_print_schedule(report)`，最后一行按这条退出：

```python
    # 退出码说的是同一件事（D101：死掉的调度器发不出「我死了」，判它的读者在循环之外）。
    # 用 `schedule_stale` 而不用「从未启动」：一次都没跑过的库不是事故，
    # 那一格由 `[调度]` 自己的文案说
    return (
        1
        if report.degraded_sources or report.pending_migrations or report.schedule_stale
        else 0
    )
```

- [ ] **Step 5: 跑绿**

```bash
uv run pytest tests/services/test_health.py tests/cli/test_health_cli.py -q
```

Expected: PASS（34 + 19 = 53 枚）。

- [ ] **Step 6: 提交**

实现与评审收尾各一个提交：不改历史（`--amend` 与 `git reset --soft` 都不用），
所以评审发现的那几处口径由后面那两个提交说明。

```bash
# fc812df —— 实现
git add src/sams/services/health.py src/sams/cli/health.py \
    tests/services/test_health.py tests/cli/test_health_cli.py
git commit -m "feat(health): [调度] 一节与心跳丢失的退出码"

# 695ab55 —— 评审收尾（第一轮）
git add src/sams/cli/health.py tests/services/test_health.py tests/cli/test_health_cli.py
git commit -m "fix(health): 评审收尾——不存在的命令与走不到的形状都从这一屏去掉"

# 1b853de —— 评审收尾（第二轮）
git add src/sams/cli/health.py tests/cli/test_health_cli.py
git commit -m "fix(health): 评审第二轮——未迁移那条线只留一个主人"
```

**评审收尾（2026-10-23）**：实现落在 `fc812df`，修在 `695ab55`（第一轮）与 `1b853de`（第二轮）。

- **「有过心跳但算不出年龄」是一个代码走不到的形状**：`collect` 只有在那一行心跳存在时
  才写 `beat_age_s`，所以用例原来手工造的 `ScheduleData(heartbeat=有, beat_age_s=None)`
  不存在，而 `_age` 里那条 `None` 分支（返回「未知」）也就是死代码。两处一起删：
  `_age(seconds: int)` 不再接 `None`，用例改回 `collect` 真正会产出的那份
  （`heartbeat=None, beat_age_s=None`）。「没有年龄不等于过期」那一半仍然有主人，
  在 `tests/services/test_scheduler_service.py` 里从 `collect` 那一层钉着。
- **`_age` 整小时不再补「0 分钟」**：「1 小时 0 分钟没更新」读起来像秒表。钉它的用例两头
  都给（`2 小时 5 分钟` 与 `2 小时` 且屏上没有「0 分钟」）。
- **修法提示改指真实存在的命令**：原来那句 `sams user set-admin …` 在 CLI 里没有
  （`cli/user.py` 的动作是 add/list/passwd/enable/disable/**promote**），会引导运维去敲一条
  不存在的命令。现在是 `sams user promote --username …`。
- **对没接线的生产作现在时陈述的注释去掉**：「退出码让 cron 也能听见」——`bootstrap.py`
  还没把 `schedule` 接进来（那是 Task 12），改成「同一件事还进 `show` 的最后一线：
  屏幕上那句给人读，那个码给不盯屏幕的人读」。
- **列宽按注册表里的真实取值定，并补上对齐这一枚**：名字列原来留 9 个字符，而
  `financials` 有 10 个（`_DATA_JOBS` 与 `build_jobs` 里最长就是它），真数据里那一行会把
  后面每一列左移一格，「谁在反复失败」在两行之间对不齐。现在是 11；周期列 7 正好是
  `monthly` 的长度。用例把三列一起钉（`financials`/`calendar`/`bars`，市场码取 `A` 与 `HK`
  两种长度），变异 `{job.name:<11}`→`<9`、`{job.period:<7}`→`<6`、去掉市场列宽度都打红。
- **两枚单向钉补齐**（评审用变异找出来）：`if job.streak:` 改成恒真是绿的（没有用例说
  「连败 0 就不该提连败」），`latest.error[:60]` 改成 `[:61]` 也是绿的。现在各有一边：
  `test_a_zero_streak_is_not_printed_as_zero_failures` 与
  `long_error[:61] not in text`。同一条思路补上 `period_key` 为空时落「–」而不是 `None`
  （这一列是 0018 用 `ALTER TABLE … ADD COLUMN` 加的，DuckDB 那句带不了 NOT NULL）。
- **修法提示不指还没存在的命令**：系统积压那一行原来写「`sams run jobs` 看哪个 job 在
  反复失败」，而 `run` 这个命令是 Task 12 才有的——今天敲它会得到「未知子命令」。改成指向
  本屏下面那一节（`[调度]` 按 job 列最近一次的结论），Task 12 之后那句话仍然是真的。
- **代码不固定的计数从文档里去掉**：用例原来写「沿用『六个数一起可空』的同一条线」，
  而 `NotifyData` 里可空的数有八个、这个数字没有任何代码在守。改成指向那条线本身
  （「`NotifyData` 那一节其余那几个数的同一条线（D93）」）。
- **注释里的周期举例换成真存在的周期**：原来那句「日频 job 连败 2 个周期与季频 job 连败
  2 个周期是两个不同的事」举了「季频」，而 `due.py` 的 `PERIODS` 只有
  `daily`/`weekly`/`monthly`/`fx` 四个取值——注释举了一个代码里不存在的类别。改成月频。
- **第一轮驳回的一条，第二轮按仓库事实接受**：「`_print_backlog` 里 `unhandled is None`
  那一支走不到」。第一次的回答是「调用方今天没有那个检查」——这句话是错的：
  `_print_notify` 在 `notify.channels is None` 时打完「库里还没有 0015 的形状」就 `return`，
  那条线本来就只判一次。于是那一支既走不到、又没有用例钉着，两处一起删，
  `_print_backlog` 的文档串改成点名那个唯一主人。缺的那枚屏幕用例补上了：
  `test_an_unmigrated_db_says_the_version_and_no_backlog_numbers`——未迁移的那一屏有
  「库里还没有 0015 的形状」而没有「未处理告警」（`None` 是「没查过」，印成 0 是谎话）。
  删掉那句 `return` 现在会打红（变异「未迁移的库跟着报积压数字」）。
- **列宽的注释不替市场那一列打包票，方向也不说反**：原来那句「三列各自按注册表里最长的
  取值留宽」对名字（`financials` 10 个字符 → 留 11）与周期（`monthly` 7 个 → 正好 7）成立，
  对市场不成立——市场码不是封闭集合（`core/codes.py` 的 `canonical_market` 只做大小写与
  空格归一，没有取值表），4 那一格是留的余量而不是算出来的。注释与用例文档串一起改成
  「钉的是这三列**当下**齐不齐」。另外原来写「整列就右移」，方向说反了：留窄一格，
  后面的列往**左**走。
- **三条评审意见按仓库事实驳回**：①「系统积压 `count(DISTINCT code)` 会把一个组件的两个
  问题合成一个」——屏幕上那一格明说单位是「个组件」，与 D102 写行的单位一致，不是「问题数」；
  ②「健康页那两个数该带上 D102 的三件标记（再加 `rule_id IS NULL`）」——`market = 'SYS'`
  与 `market <> 'SYS'` 是 `alerts` 的一对**补集**，加第三个条件会让一个假想的
  「SYS 而带 rule_id」的行两个数都不进；写侧那条三条件查询认的是「自己写下的那批行」，
  读侧这两问切的是全表，语义不同。今天只有 `scheduler/alarms.py` 写 `market='SYS'`；
  ③「`[调度]` 里 `sams run start` 与积压那行指向 `[调度]` 都是还没存在的命令」——两支今天
  都走不到：`bootstrap.py` 没传 `schedule=`，那一节只会说「未接入调度器读数」，而系统积压
  那一行要 `unhandled_system > 0`，那只能由还没接线的调度器写出来。Task 12 接上这两支的
  同一晚 `run start` 就存在了。与第一轮删掉的 `sams run jobs` 不同：那一次本屏有一句真替换，
  这一次没有同屏的可指。
- **变异电池 37 枚**（`/tmp/mut11.py`，两份文件跑完逐字节还原）：服务层 10 枚（两条 SQL 的
  排除与只认、`DISTINCT`、两批时刻互换、`schedule_stale` 的两半、读数不被取、`KEY_TABLES`
  两张表）、屏幕 27 枚（退出码的两头、三列宽度与「周期那一列没了」、连败与 note 两支的
  正反、收尾时刻/零管理员/过期那一支/「从未启动」并入的单向、错误折痕的两边、`_age` 四支
  与整小时的尾巴、量词、「未接入」那一行、积压那两行不打、未迁移的库跟着报积压数字）。全红。

### Task 12: `sams run start|once|jobs` 与组装根接线

**Files:**
- Create: `src/sams/cli/schedule.py`
- Modify: `src/sams/cli/__init__.py`（import 块与 `COMMAND_MODULES` 各加 `schedule`）
- Modify: `src/sams/bootstrap.py`（装配 `Scheduler`、`Container.scheduler`、`SystemAlerter` 接线、
  `HealthService(schedule=…)`）
- Modify: `src/sams/services/health.py` + `src/sams/cli/health.py`（未迁移容器的护栏，见 Step 11）
- Test: `tests/cli/test_run_cli.py`（新建 10 枚）
- Test: `tests/test_bootstrap.py`（追加 3 枚）
- Test: `tests/services/test_health.py` + `tests/cli/test_health_cli.py`（护栏那 4 处）

到这一步为止，调度器在代码里已经能跑，但没有任何进程会造它：`bootstrap` 不装配，
命令行也够不着。这个 task 把两头接上，同时回答三个使用问题：

* `run start` —— 常驻。屏幕上必须先是「几个 job、多久一轮」，否则运维不知道
  自己启动的是不是刚改完的那份配置；
* `run once` —— 手工补一轮，也是排障入口。它打印每条结果、等待中的 job 与到期
  计算的错误，失败/出错时退出码 1；
* `run jobs` —— 按执行顺序列出注册结果，回答「为什么今晚没有港股规则」。

两个容易踩的接线点，先写清楚：

1. **模块名不能叫 `run.py`**。`sams/cli/__init__.py` 有一个包级的 `run()`。今天
   `COMMAND_MODULES` 写在 `def run()` 之前，元组拿到的是子模块，所以不会坏；把两行的
   顺序换一下，元组里装的就是那个函数，`module.add_to(...)` 当场 `AttributeError`。
   换一个名字，这件事就不再依赖那两行的先后。
2. **`iso_date` 必须在 handler 里调用，不能当 `type=`**。argparse 不认
   `ValidationError`，`--as-of 2024-13-40` 会吐一段栈而不是「这个日期该怎么写」；
   handler 里调用才能经 `exit_for` 变成退出码 2（D77）。

还有第三个，是接线之后才显形的：**读数接进生产，就把「库里还没有那两张表」这一形状
带进了每一次 `sams health`**（Step 11）。

- [ ] **Step 1: 写失败的 CLI 用例**

`tests/cli/test_run_cli.py`（整份就是这些）：

```python
"""`sams run start|once|jobs`：钉的是「给定一份读数，屏幕上该有什么、以什么码退出」。

替身只回答三个问题——注册了什么、`tick` 收到哪些参数、循环推了几轮。「该不该跑这一轮」
「连败几个周期才告警」那些判断在 `tests/services/test_scheduler_service.py` 里钉过；
这一层去重演它们，同一句话就会有两份各自失效的主人。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from sams.cli import build_parser
from sams.core.errors import SchedulerBusy, ValidationError
from sams.services.scheduler.due import DAILY, FX, WEEKLY
from sams.services.scheduler.runner import SKIPPED, RunResult
from sams.services.scheduler.service import TickReport
from sams.services.scheduler.types import JobOutcome, JobSpec

NOW = datetime(2024, 10, 25, 23, 0, tzinfo=UTC)
DAY = date(2024, 10, 25)


def result(**over) -> RunResult:
    """一行执行结果，默认是「跑成了，并且有一句可说的话」。

    用例要「没有尾巴」的那几格自己写 `note=""`：屏幕上那些尾巴判的就是空串，
    默认值替它们说话会把「没话说」这一支钉不住。
    """
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
        # 四个而不是三个：名字列的宽度要靠一个 10 个字符的名字才钉得住（`financials`），
        # 而注册顺序的语义（数据 → 周检 → 规则 → 汇率 → 日历）由 period 那三样代表
        self.jobs = (
            JobSpec("bars", DAILY, "A", lambda context: JobOutcome()),
            JobSpec("financials", DAILY, "A", lambda context: JobOutcome()),
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
    """走一遍真的 argparse：命令名、参数、`--help` 那一套都由 `build_parser` 负责。

    容器是手搭的：装配归 `tests/test_bootstrap.py`，这里只给这两个 handler 读的那两样。
    `scheduler_tick_s` 特意取一个不是默认值的数：屏幕上那一格读的是设置，
    写死成 60 的用例分不出「读了设置」和「抄了默认值」。
    """
    args = build_parser().parse_args(argv)
    container = SimpleNamespace(
        scheduler=scheduler,
        settings=SimpleNamespace(scheduler_tick_s=45),
    )
    return args.handler(container, args)


def test_once_prints_each_result_and_counts_failures(capsys):
    """三种结论各占一行，其中失败让退出码变 1：`once` 是排障入口，
    敲它的人要知道「这一轮有没有需要人看的东西」。"""
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
    lines = out.splitlines()
    # 三行都钉整行：note / skip_reason / error 三格各自在一行上有值、在另两行上是空串，
    # 「条件改成无条件」多出来的是一个 ` · ` 空尾巴，只查子串的断言拦不住
    assert "ok     bars@A  周期 2024-10-25 · 入库 3 根" in lines
    assert "failed actions@HK  周期 2024-10-25 · RateLimited: 429" in lines
    assert "skipped fx@全局  周期 2024-10-25 · 10 分钟内重试过" in lines
    assert "等待  calendar@A  还没到点" in out


def test_once_exits_zero_when_nothing_failed(capsys):
    """全成功 + 等待中也还是 0：「还没到点」不是故障，把两者合成一个码，
    cron 就会每天为一个正常的下午报警。

    三个旗标都不给时也钉：`None` 传下去才是「按各市场当地日期、全部 job」，替一个空旗标
    填上「今天」会让三个市场共用同一个日期——屏幕上那一轮说的就不是「今晚」了。
    """
    scheduler = FakeScheduler(
        report=TickReport(now=NOW, results=(result(),), waiting=("fx@全局",))
    )
    assert invoke(scheduler, ["run", "once"]) == 0
    assert "等待  fx@全局  还没到点" in capsys.readouterr().out
    assert scheduler.calls == [{"as_of": None, "only_job": None, "only_market": None}]


def test_once_normalizes_market_and_parses_as_of():
    """钉的是「进调度器之前已经归一」：`Scheduler._selected` 按 `spec.market == only_market`
    过滤，而注册表里的键是大写，所以原样传下去的 `hk` 会选出零个 job，屏幕上写着「跑了
    一轮」而什么都没做。替身只记录自己收到了什么，真实的那道过滤在 `service.py`。"""
    scheduler = FakeScheduler()
    argv = ["run", "once", "--market", "hk", "--as-of", "2024-10-25", "--job", "bars"]
    assert invoke(scheduler, argv) == 0
    assert scheduler.calls == [
        {"as_of": DAY, "only_job": "bars", "only_market": "HK"}
    ]


def test_a_malformed_date_is_a_usage_error(capsys):
    """2 与 1 的分界（D77）：`--as-of 2024-13-40` 是敲错了，改参数就能重跑；
    一段栈会让运维先去查那个抛错的文件。"""
    scheduler = FakeScheduler()
    assert invoke(scheduler, ["run", "once", "--as-of", "2024-13-40"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
    assert scheduler.calls == [], "日期都没看懂，就不该碰调度器"


def test_an_unknown_job_is_a_usage_error(capsys):
    """job 名敲错同样是 2：`_selected` 抛的是 `ValidationError`，
    而「这一轮没选出 job」与「这一轮跑成了」在屏幕上必须分得开。"""
    scheduler = FakeScheduler(error=ValidationError("没有匹配的 job（name=ghost）"))
    assert invoke(scheduler, ["run", "once", "--job", "ghost"]) == 2
    assert "没有匹配的 job" in capsys.readouterr().err


def test_once_prints_an_error_line_and_exits_1(capsys):
    """到期计算算不出来的那些 job 不进 `results`，进 `errors`：只打结果的那一屏
    会把「三个市场的日历读不到」说成「这一轮无事可做」。"""
    scheduler = FakeScheduler(
        report=TickReport(
            now=NOW,
            results=(),
            waiting=("calendar@A",),
            errors=("rules@A: 日历里没有 2024-10-25",),
        )
    )
    assert invoke(scheduler, ["run", "once"]) == 1, "只有错误、没有失败也要非零"
    out = capsys.readouterr().out
    assert "错误  rules@A: 日历里没有 2024-10-25" in out
    assert "等待  calendar@A  还没到点" in out


def test_start_reports_the_job_count_and_suppresses_skipped_only_ticks(capsys):
    """启动那一句先报「几个 job、多久一轮」：敲的人要知道自己启动的是不是刚改完的配置。
    一轮里全是「这个周期已经跑过」时整轮不出声——几十秒一轮刷屏会把真正的失败冲走。"""
    skipped = TickReport(
        now=NOW,
        results=(result(status=SKIPPED, note="", skip_reason="成功过的周期不重跑"),),
    )
    alive = TickReport(now=NOW, results=(result(),))
    scheduler = FakeScheduler(reports=(skipped, alive))
    assert invoke(scheduler, ["run", "start"]) == 0
    out = capsys.readouterr().out
    assert "4 个 job，每 45 秒一轮" in out
    assert "已停止（2 个 tick）" in out
    assert "成功过的周期不重跑" not in out
    # 整行连缩进一起钉：少掉那一格缩进，这一行就和「出声的那一轮」的头一行分不出层级
    assert "  ok     bars@A  周期 2024-10-25 · 入库 3 根" in out.splitlines()
    assert out.count("bars@A") == 1
    # 静音连头一行一起静音：只挡结果行，一轮全 skipped 时仍然留一行光秃秃的时刻，
    # 那一行读起来像「这一轮做了点什么」
    assert out.count("2024-10-25 23:00:00 UTC") == 1


def test_a_tick_with_an_error_speaks_even_when_every_job_was_skipped(capsys):
    """「全 skipped」与「全 skipped 且这一轮有算不出来的东西」是两屏：
    前一种该安静，后一种必须出声，否则静音规则会顺手吃掉真正的故障。"""
    noisy = TickReport(
        now=NOW,
        results=(result(status=SKIPPED, note="", skip_reason="成功过的周期不重跑"),),
        errors=("bars@A: 读不到账本",),
    )
    scheduler = FakeScheduler(reports=(noisy,))
    assert invoke(scheduler, ["run", "start"]) == 0
    lines = capsys.readouterr().out.splitlines()
    # 连那一格缩进一起钉：错误行是这一轮头一行的下属，顶格打就看不出它属于哪一轮
    assert "  错误  bars@A: 读不到账本" in lines
    assert "[2024-10-25 23:00:00 UTC]" in lines, "出声的那一轮要带上它是哪一轮"
    assert "成功过的周期不重跑" not in "\n".join(lines), "出声不等于把 skipped 也倒出来"


def test_start_reports_a_second_daemon_as_a_message(capsys):
    """认领心跳失败（已经有一个循环在跑）是一句话，不是一段栈；
    也不是 2——命令行没错，是那个进程已经存在（D100）。"""
    scheduler = FakeScheduler(
        error=SchedulerBusy("已经有一个调度器在运行（心跳 30 秒前）")
    )
    assert invoke(scheduler, ["run", "start"]) == 1
    captured = capsys.readouterr()
    assert "已经有一个调度器" in captured.err
    assert "SchedulerBusy" not in captured.err
    assert captured.out.startswith("调度器启动")


def test_jobs_lists_the_registration_order(capsys):
    """「为什么今晚没有港股规则」只有两个答案来源：注册顺序，以及每条各自的市场与周期。

    钉整行而不是列的位置：名字那一格少留一格时四行**仍然彼此对齐**（一起左移），
    只查「齐不齐」拦不住改宽度。而 11 与 4 这两格的出处是「注册表里最长的名字
    （`financials`）之后还留两格」，与健康页 `[调度]` 那一节同一条线——所以这里要红的是
    那两格的具体间距，市场那一格同时给两种长度（`A` 与「全局」）。
    """
    assert invoke(FakeScheduler(), ["run", "jobs"]) == 0
    out = capsys.readouterr().out
    assert "注册 4 个 job（执行顺序即下列顺序）：" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("  ")]
    assert rows == [
        "  bars        A    daily",
        "  financials  A    daily",
        "  recheck     A    weekly",
        "  fx          全局   fx",
    ]

```

三处与「拿替身演一遍」有关的口径，都在文件里钉着，这里只点名为什么这么造：

- **替身设置的 `scheduler_tick_s` 是 45，不是生产默认值 60**。屏幕上那一格填的必须是
  设置里的数：把两处都写成 60，`每 60 秒一轮` 这个字面量就是绿的（变异「一轮多长写死在
  屏幕上」正是这么打的）。
- **注册表里放四个 job，其中一个是 `financials`**。名字列宽按注册表里最长的取值留，
  三个短名字（`bars`/`recheck`/`fx`）钉不出这一格至少要 11 个字符。
- **断言取整行、不取子串**。`"ok     bars@A  周期 2024-10-25 · 入库 3 根"` 这种整行断言
  同时钉住状态列宽、`@` 后面的市场、分隔符那一个 `·` 与缩进；子串断言只钉得住文字，
  把两格合成一格、或把缩进吞掉都是绿的。

- [ ] **Step 2: 跑红**

```bash
.venv/bin/python -m pytest tests/cli/test_run_cli.py -q
```

Expected: FAIL（`run` 不是已注册的子命令，`build_parser().parse_args(["run", …])` 在
argparse 处就 `SystemExit(2)`）。

- [ ] **Step 3: 写 `cli/schedule.py`**

```python
"""`sams run`：常驻调度、手工补一轮、按执行顺序列出注册的 job。

模块名用 `schedule` 而不是 `run`：`sams/cli/__init__.py` 里那个包级入口函数也叫 `run`。
叫同一个名字今天不会坏，只因为 `COMMAND_MODULES` 写在 `def run()` 之前——元组拿到的是
子模块，函数随后把包上那个名字改成自己。把两行的顺序换一下，元组里装的就是函数，
`module.add_to(...)` 当场 `AttributeError`。换一个名字，这件事就不依赖那两行的先后了。

这一层不认识「该不该跑这一轮」：到期计算、连败计数、心跳守卫都在 `services.scheduler`，
这里只做三件事——把参数归一化、把一轮的结论落到屏幕上、把退出码分给脚本。
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
    """常驻模式的一轮：全是「这个周期已经跑过」且没有错误时，整轮不出声。

    常驻循环按 `scheduler_tick_s` 一轮接一轮，出声的那一屏会把真正的失败冲走。
    但「有错误」必须能穿透这条静音——静音的理由是「无事可做」，
    一个算不出到期日的 job 不是无事可做。
    """
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
    # 启动那一句先报「几个 job、多久一轮」：敲的人要能确认自己启动的是刚改完的那份配置，
    # 而不是一个还在跑旧注册表的进程
    print(
        f"调度器启动：{len(scheduler.jobs)} 个 job，"
        f"每 {container.settings.scheduler_tick_s} 秒一轮（Ctrl-C 停止）"
    )
    try:
        ticks = scheduler.run_forever(on_tick=_print_tick)
    except SamsError as exc:
        # 走到这里的是「进不了循环」那一类：`run_forever` 先认领心跳，才进那个自己兜
        # `SamsError` 的 try，所以「已经有一个循环在跑」与「心跳那一行写不进去」
        # （库在那一刻读不了、迁移只应用了一半）是同一个出口。顶层 `run()` 也会兜住、
        # 说同一句话、给同一个码（1，不是 2：命令行没错）；这里先兜一层，是为了这一层的
        # 两个出口由这一层自己的用例钉住——顶层那一句目前没有用例
        print(f"错误: {exc}", file=sys.stderr)
        return exit_for(exc)
    print(f"已停止（{ticks} 个 tick）")
    return 0


def _once(container: Container, args: argparse.Namespace) -> int:
    try:
        # 归一化放在这里而不是交给 argparse 的 `type=`：`iso_date` 抛的是
        # `ValidationError`，argparse 不认它，`--as-of 2024-13-40` 会吐一段栈（D77）
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
    # 错误也非零：那几条是「这一轮连该不该跑都算不出来」，与「跑过了、跳过」不同。
    # 能走到这一行的 `errors` 只有到期计算那一条路：账本读不了那类是从上面那个
    # `except SamsError` 出 screen 的（一句 stderr、同一个码 1），不留到屏幕上
    return 1 if report.failed or report.errors else 0


def _jobs(container: Container, args: argparse.Namespace) -> int:
    print(f"注册 {len(container.scheduler.jobs)} 个 job（执行顺序即下列顺序）：")
    for spec in container.scheduler.jobs:
        # 周期紧跟着名字：它是「今晚该不该跑」的分母。两格宽度与健康页 `[调度]`
        # 那一节一致（名字按注册表里最长的 `financials` 留宽），`:<` 只补不截，
        # 留窄一格后面的列就跟着左移
        print(f"  {spec.name:<11} {(spec.market or '全局'):<4} {spec.period}")
    return 0

```

- [ ] **Step 4: 注册进包入口**

`src/sams/cli/__init__.py` 两处（import 块按字母序，`COMMAND_MODULES` 按命令分组顺序，
`schedule` 落在 `rule` 之后）：

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

- [ ] **Step 5: 跑绿**

```bash
.venv/bin/python -m pytest tests/cli/test_run_cli.py tests/cli/test_cli.py -q
```

Expected: PASS。`tests/cli/test_cli.py` 里的 `test_every_subcommand_help_page_formats`
会自动走到新命令的三个 help 页。

- [ ] **Step 6: 写失败的组装根用例**

在 `tests/test_bootstrap.py` 末尾追加（`SystemAlarm` 已在文件顶部 import）：

```python
def test_the_container_registers_jobs_in_execution_order(container):
    """执行顺序是语义：周复检排在当晚规则之前，汇率排在规则之后、日历之前。

    这里不钉 job 的个数：`stub_adapters()` 只给日线，港美股因此没有基本面 job，
    而那是 D106「按能力注册」要的结果，不是缺数据。
    """
    built, _ = container
    titles = [job.title for job in built.scheduler.jobs]
    assert titles.count("bars@A") == 1
    assert titles.index("recheck@US") < titles.index("rules@US")
    assert titles.index("rules@US") < titles.index("fx@全局")
    assert titles.index("fx@全局") < titles.index("calendar@A")


def test_a_job_alarm_becomes_a_system_alert_for_admins(container):
    """D102：同一条 `SystemAlarm` 在总线有两类订阅者——日志（上面那枚钉的）与管理员。

    路由的分支在 `test_scheduler_alarms.py` 逐条钉过，这里只验接线：告警真的从总线
    走到了 `alerts` 那张表，并带上系统告警那三件标记（`rule_id` 为空、市场是 `SYS`、
    `code` 存组件名）。接线断了的形状是「日志里天天有、管理员一条都没收到」。
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
    """健康页那一节读的就是这里装的那台调度器：两处各造一份时，
    屏幕上「最近一次的结论」说的不是真在跑的那一个。"""
    built, _ = container
    report = built.health.report()
    assert report.schedule is not None
    assert {"bars", "rules", "fx"} <= {job.name for job in report.schedule.jobs}
    assert report.schedule.admins == 0
```

- [ ] **Step 7: 跑红**

```bash
.venv/bin/python -m pytest tests/test_bootstrap.py -q
```

Expected: FAIL（`Container` 没有 `scheduler`；`HealthReport.schedule` 仍为 `None`；
发布告警后 `alerts` 为空）。

- [ ] **Step 8: 接线 `bootstrap.py`**

六行服务 import（排在 `rules.snapshot` 之后、`screen` 之前，字母序）与一行仓库 import：

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

`Container` 的 `rule_rechecker` 之后：

```python
    #: 常驻调度器（spec §12）：`sams run start|once|jobs` 与健康页的 `[调度]` 读的是同一台
    scheduler: Scheduler
```

sender 提成变量——`RuleEngine` 与 `SystemAlerter` 共用同一条发送路径，两套 sender 意味着
DND 与通道选择有两份实现，admin 收不收得到系统告警会取决于它走的是哪一条：

```python
    # 规则告警与系统告警共用这一个 sender：DND、通道选择、`notify_log` 那一行都住在
    # `NotifyService` 那一头，一个组装根里留两条发送路径，迟早一条带上这些而另一条不带
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

`rule_rechecker = Rechecker(...)` 之后、`return Container(` 之前。四个同步服务与
`UniverseBuilder` 一起提出来：同一个服务被造两份时，容器里那个与 job 里那个不是同一个
对象，症状不是报错、是「改了没生效」。

```python
    # D102：系统告警与规则告警共用上面那个 sender，订阅的是同一条总线
    SystemAlerter(
        alerts=alerts_store, users=users, sender=sender, settings=cfg, clock=clock
    ).attach(event_bus)

    # 下面四个同步服务与 `UniverseBuilder` 原本就地造在 `return Container(...)` 的参数里。
    # 调度器也要吃它们，而同一个服务被造两份时，容器里那个与 job 里那个不是同一个对象：
    # 测试替换或事后调一处参数，另一处照旧跑——症状不是报错，是「改了没生效」
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
        sync=sync,
        corp_actions=corp_action_sync,
        fundamentals=fundamental_sync,
        fx=fx_sync,
        calendar=calendar,
        rules=rule_engine,
        recheck=rule_rechecker,
        users=users,
    )
    scheduler = Scheduler(
        cfg,
        build_jobs(deps),
        runner=JobRunner(runs=task_runs, bus=event_bus, clock=clock),
        runs=task_runs,
        heartbeats=heartbeats,
        universe=universe_builder,
        users=users,
        calendar=calendar,
        clock=clock,
    )
```

`return Container(...)` 里四处改成上面造好的对象，并补 `scheduler`：

```python
        calendar=calendar,
        sync=sync,
        corp_action_sync=corp_action_sync,
        fundamental_sync=fundamental_sync,
        fx_sync=fx_sync,
```

```python
        rule_rechecker=rule_rechecker,
        scheduler=scheduler,
```

`HealthService(...)` 的构造参数补一行（那三行注释记的是「这一屏读的不是那个进程」，
Task 11 的实测结论在接线之后仍然成立）：

```python
            # 本机通道的可用性由 monitor 探测，health 只接这一个函数：多一条 import 就红了
            local_status=notify.local_status,
            # 健康页读的就是上面这台调度器留下的那两张台账：不是「它此刻活着吗」——
            # 调度器持有库文件时，第二个进程连不上（DuckDB 的独占锁），这一屏说的是
            # 「它上一次写下结论是什么时候」
            schedule=scheduler.schedule_data,
```

- [ ] **Step 9: 跑绿与静态检查**

```bash
.venv/bin/python -m pytest tests/cli/test_run_cli.py tests/test_bootstrap.py \
    tests/services/test_health.py -q
.venv/bin/ruff check src tests --no-cache
```

Expected: PASS；ruff `All checks passed!`。

- [ ] **Step 10: 真进程冒烟（不是用例，是打开眼睛看）**

```bash
SAMS_CONFIG=tmp/task12-smoke.toml SAMS_SECRET_KEY=… .venv/bin/sams run jobs
SAMS_CONFIG=tmp/task12-smoke.toml SAMS_DB_PATH=/tmp/t12c.duckdb SAMS_SECRET_KEY=… \
    .venv/bin/sams health
```

一份临时配置 + 一个空的 DuckDB 文件，跑的是真注册表：

- `run jobs` 报 21 个 job（`A` 七个 / `HK` 五个 / `US` 五个 / `fx` 一个 / `calendar` 三个），
  顺序与 `build_jobs` 一致，且**这个数量本身就是一次验收**——`stub_adapters` 造不出来
  这个形状（D106 的能力门在真配置上是全开的）。
- 那个空库被 `sams health` 顺手迁移之后，`[调度]` 说「从未启动」、21 行 job 全打
  「从未运行」，`⚠ 没有在用的管理员` 露头（新库里确实一个用户都没有），退出码 0。
- 两屏的列宽逐字符对齐（`:{11}` / `:{4}`）。**CJK 那一格在终端里仍然偏 2 个格**：
  `f"{…:<4}"` 数的是字符数，`全局` 两个字符占四个显示格，所以只有 `fx` 那一行的第三列
  比别行晚两列。今天按「接受」处理——它不是数据错、也不是行缺失，为它引入一套
  显示宽度计算（`east_asian_width`）会把两屏的断言全换成宽度表；如果 Task 13 之后这一屏
  真的多了全局 job，再回来算这笔。

- [ ] **Step 11: 未迁移的容器不能炸掉整屏（接线带出来的回归）**

接线前跑全量，`tests/test_adversarial_services.py::test_health_show_exit_codes` 直接红了：
那枚用例造的是**没跑过迁移**的容器，而 `HealthService.report()` 现在无条件调
`self.schedule()` → 对着不存在的 `scheduler_heartbeats` 发 SQL → `StorageError`。
整屏出不来，与它想报的「哪个迁移还没应用」是同一个症状的两半。

修法是发 SQL 之前问一次表在不在，把「还没迁移」与「没接入」分成两个字段：

```python
#: 调度器读数要查的两张表。`[表]` 那一节报的也是这两张：一处定义、另一处只引用，
#: 免得「能不能查」与「报不报行数」两边各写一遍、然后有一边先失效。
#: 两张表各答一个问题：`task_runs` 是「每个 job 上一轮跑成什么」，
#: `scheduler_heartbeats` 是「还在跑吗」（D101：判「它还在不在跑」的读者必须在循环之外）
SCHEDULE_TABLES = ("task_runs", "scheduler_heartbeats")
```

`HealthReport` 的两个字段与那道护栏：

```python
    #: 调度器的读数。「没有读数」在这一格上有两种，别读成同一种：`None` 是「没接入」
    #: 或「库里还没有那两张表」（后者由下面那一格说清楚），而
    #: `ScheduleData(heartbeat=None, ...)` 是「接入了、只是那一拍还没写下」。
    #: 三种混成一屏，运维会去查一个从没跑过的调度器
    schedule: ScheduleData | None = None
    #: 读数接的是生产那台调度器，但库里还没应用 0018，于是这一问压根没发出去。
    #: 单独一格是为了把「还没迁移」从 `None` 里分出来：两句的修法相反，
    #: 一句跑 `sams db migrate`、一句查装配
    schedule_unmigrated: bool = False
```

```python
        # 调度器那一问要查 0018 那两张表，护栏放在发 SQL 之前：这一屏的合同是
        # 「拿到一个没迁移的容器也要出得来」（`tests/test_adversarial_services.py`
        # 直接喂 `show` 钉着它）。这里只问表在不在、不问列，靠的是 0018 一次做两件事：
        # 建 `scheduler_heartbeats`、给 0001 那张 `task_runs` 补上 `period_key`/`note`。
        # `_notify_data` 那边没有这个巧合（`channels`/`notify_log` 是 0001 的表，
        # `outcome` 那一列要到 0015 才有），所以它多问了一句列在不在
        schedule = None
        schedule_unmigrated = False
        if self.schedule is not None:
            if set(SCHEDULE_TABLES) <= have:
                schedule = self.schedule()
            else:
                schedule_unmigrated = True
```

屏幕上那一支排在「未接入」之前（两句的修法相反）：

```python
    if report.schedule_unmigrated:
        # 排在「未接入」之前，两句的修法相反：一句跑迁移、一句查装配。
        # 版本号写在这里是给个人去跑哪条命令，真要问「还差哪几个迁移」看这一屏最上面
        # 那行的「待应用迁移」——那一句才是从库里读出来的
        print("  库里还没有 0018 的形状：sams db migrate")
        return
```

用例补 4 处（1 枚新建 + 3 枚追加断言）：`tests/services/test_health.py` 里那枚未迁移用例
现在数着 `schedule` 被调了几次并钉 `asked == []`（护栏要在发 SQL 之前，不是发完再吞异常），
`tests/cli/test_health_cli.py::test_an_unmigrated_db_says_the_schedule_shape_not_the_unwired_one`
钉整行——子串断言在这一屏会被 `[通知]` 那节的「库里还没有 0015 的形状」顶掉。

- [ ] **Step 12: 提交**

不改历史（不 `--amend`、不 `reset --soft`），所以评审那两处由后面的提交说明。

```bash
# 02711a3 —— 实现
git add src/sams/cli/schedule.py src/sams/cli/__init__.py src/sams/bootstrap.py \
    tests/cli/test_run_cli.py tests/test_bootstrap.py
git commit -m "feat(scheduler): sams run start|once|jobs 与组装根接线"

# d50fc19 —— Step 11（回归 + 修法 + 4 处用例）
git add src/sams/services/health.py src/sams/cli/health.py \
    tests/services/test_health.py tests/cli/test_health_cli.py
git commit -m "fix(health): 读数接进生产之后，未迁移的容器不能再炸掉整屏"

# 0e2ca4e —— 评审收尾（只改注释与文档串，逻辑一行没动）
git commit -m "docs(scheduler): 评审收尾——注释不再替走不到的路径打包票"
```

**评审与实测（2026-10-23）**

- **计划草稿里有三处与仓库对不上，落地时按仓库改**：
  ① `Scheduler(cfg, build_jobs(deps), JobRunner(…), task_runs, heartbeats, universe_builder,
  users, calendar, clock=clock)`——真实签名里 `runner` 之后全是**仅关键字**参数
  （`service.py` 的 `__init__`），八 positional 直接 `TypeError`；现在是九个关键字。
  ② `JobDeps` 草稿带 `universe=universe_builder` 与 `clock=clock`——真实 `JobDeps`
  是十个字段，没有这两个：`UniverseBuilder` 与时钟由 `Scheduler` 自己拿（`Scheduler`
  要在 tick 之外做心跳与到期，job 只在 `JobContext` 里拿到需要的东西）。
  ③ 用例草稿 `from sams.services.scheduler.types import DAILY, FX, WEEKLY`——这四个周期
  常量住在 `due.py`（`types.py` 只有 `JobSpec`/`JobOutcome`/`JobDeps`）。
- **`_jobs` 的名字列宽从草稿的 `:<10` 改成 `:<11`**：`financials` 有 10 个字符，
  留 10 格时它的周期紧跟一格、其余各行晚一格，与 `[调度]` 那节（Task 11 定的 11）不再一致。
- **五枚变异第一轮是绿的，每枚的根因都是一处弱断言**：替身设置与屏幕字面量同为 60（改成
  45）；子串断言看不出列宽整体变化与缩进（改整行）；`out.count(时间戳)` 缺位导致
  「这一轮不打标题」也是绿的（补 `== 1`）。补强后 50 枚全红。
- **变异电池两枚共 62**：`/tmp/mut12.py` 50 枚（`cli/schedule.py` 的三屏与退出码、
  `cli/__init__.py` 的两处注册、`bootstrap.py` 的容器字段/健康读数来源/`SystemAlerter`
  订阅/调度器四个依赖/空注册表），`/tmp/mut12b.py` 12 枚（Step 11 那道护栏：表不在也照查、
  表在也不查、包含关系反着判、`schedule_unmigrated` 永远说假、接线了就不问表、两张表少一张、
  屏幕那一支删掉/不走/排在「未接入」之后、版本号写错、只 `return` 不出声、修法指向不存在的
  命令）。两份电池跑完都逐字节 sha256 还原校验。全红。
- **评审接受（`0e2ca4e`）**：① `_start` 那个 `except` 的注释原来把「已经有一个循环在跑」
  写成唯一来路——`run_forever` 先 `_claim()` 才进那个自己兜 `SamsError` 的 try，
  所以心跳写不进去（`StorageError`）是同一个出口，注释改成两个出口；
  ② `_once` 退出码那句要点名兄弟出口（账本读不了那类从 `except SamsError` 出 screen，
  一句 stderr、同一个码 1，不留到屏幕上）；③ `HealthReport.schedule` 的 `None` 今天有
  两种原因（没接入 / 库里还没有那两张表），字段注释原来只写一种；`schedule_stale` 的
  文档串同理不再假装只有「从未启动」一个近亲；④ 那道护栏「只问表不问列」的理由要写下来
  （0018 一次做两件事：建心跳表、给 0001 的 `task_runs` 补 `period_key`/`note`；
  `_notify_data` 没有这个巧合，因为 `channels`/`notify_log` 是 0001 的表而 `outcome`
  那一列要到 0015，所以它多问一句列在不在——两处不齐是有原因的，不是漏）。
- **自己写下的两句假话，撤回**：① 「`sams health` 是迁移前的自检入口」——`cli/__init__.py`
  的 `run()` 对**除 `db` 之外**的每个命令先 `container.migrate()`，所以从命令行走不到
  未迁移的形状；那句改口成「这一支今天只由直接喂 `show` 的用例走到」。
  ② 「库被别的进程占着」举作 `_start` 那个出口的来路之一——独占锁在更早的
  `build_container` 就失败，到不了这里。
- **评审提出、按仓库事实驳回两条**：① 把 `schedule_unmigrated` 也送进退出码——
  未迁移这件事已经有主人（`report.pending_migrations`，非零），加第二个来源只会在
  「迁移没跑」与「调度器停了」之间多一个说不清的组合；② 把容错下推到
  `TaskRunRepo`/`HeartbeatRepo`（表不在就返回空）——那会把「问不了」变成「从未启动」，
  正是这一片代码从头到尾在避免的那种混读。
- 全量离线套件：2951 passed（14 deselected，`-m 'not live'`）。

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