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


class TestHeartbeatStates:
    """心跳三态：过期（退出码 1）、新鲜（0）、从未启动（0——没跑过不是事故）。"""

    def test_beat_exactly_at_the_threshold_is_stale_and_exit_1(self, invoke) -> None:
        """恰好等于阈值算过期：`stale_at` 取 `>=`，这一枚钉的就是那个等号。"""
        data = ScheduleData(
            heartbeat=beat(HEARTBEAT_STALE_S), beat_age_s=HEARTBEAT_STALE_S, admins=2, jobs=()
        )
        code, text = invoke(report_with(schedule=data))
        assert code == 1, "「它可能已经停了」必须让 cron 拿到非零"
        assert "常驻调度器可能已经停了" in text

    def test_fresh_beat_exits_0_and_shows_the_age(self, invoke) -> None:
        data = ScheduleData(
            heartbeat=beat(HEARTBEAT_STALE_S - 1),
            beat_age_s=HEARTBEAT_STALE_S - 1,
            admins=2,
            jobs=(),
        )
        code, text = invoke(report_with(schedule=data))
        assert code == 0
        assert f"心跳 {(HEARTBEAT_STALE_S - 1) // 60} 分钟前 · 管理员 2 名" in text

    def test_never_started_prints_a_line_but_exits_0(self, invoke) -> None:
        """一次都没跑过的库不是事故：`never_started` 不进退出码，那一格自己会说。"""
        data = ScheduleData(heartbeat=None, beat_age_s=None, admins=2, jobs=())
        code, text = invoke(report_with(schedule=data))
        assert code == 0
        assert "从未启动" in text and "sams run start" in text

    def test_zero_admins_is_said_loudly_without_touching_the_exit_code(self, invoke) -> None:
        """「有 0 个管理员但循环还在跑」是合法的一格：两句话都要打，码还是 0。"""
        data = ScheduleData(
            heartbeat=beat(60), beat_age_s=60, admins=0, jobs=()
        )
        code, text = invoke(report_with(schedule=data))
        assert "没有在用的管理员" in text
        assert "管理员 0 名" in text, "新鲜那一行的「管理员 N 名」照打，两句不冲突"
        assert code == 0


class TestAgeRendering:
    """`_age` 的三个数字分支各钉一次——通过屏幕上印出来的那个时长串。"""

    def _stale_text(self, invoke, seconds: int) -> str:
        data = ScheduleData(
            heartbeat=beat(seconds), beat_age_s=seconds, admins=1, jobs=()
        )
        _, text = invoke(report_with(schedule=data))
        return text

    def test_minutes_branch_shows_whole_minutes(self, invoke) -> None:
        # 取 600 秒（阈值之上）走过期那一句：钉的时长串是 600 秒本身，不是阈值
        text = self._stale_text(invoke, 10 * 60)
        assert "已经 10 分钟没更新" in text

    def test_under_an_hour_shows_minutes(self, invoke) -> None:
        text = self._stale_text(invoke, 30 * 60 + 40)
        assert "已经 30 分钟没更新" in text

    def test_over_an_hour_shows_hours_and_minutes(self, invoke) -> None:
        text = self._stale_text(invoke, 2 * 3600 + 5 * 60)
        assert "已经 2 小时 5 分钟没更新" in text

    def test_under_60_seconds_shows_seconds_on_the_fresh_line(self, invoke) -> None:
        data = ScheduleData(heartbeat=beat(30), beat_age_s=30, admins=1, jobs=())
        _, text = invoke(report_with(schedule=data))
        assert "心跳 30 秒前" in text


class TestJobRows:
    """每个 job 一行：周期是「连续失败 N 个周期」的分母，必须露头。"""

    def test_failed_run_shows_period_status_streak_and_error(self, invoke) -> None:
        run = TaskRun(
            id="r1",
            job_name="bars",
            market="A",
            period_key="2024-10-25",
            started_at=MOMENT - timedelta(minutes=5),
            finished_at=MOMENT,
            status="failed",
            rows_affected=0,
            error="connection reset by peer after 3 retries",
            note=None,
        )
        job = JobHealth(name="bars", market="A", period="daily", latest=run, streak=2)
        data = ScheduleData(heartbeat=beat(60), beat_age_s=60, admins=2, jobs=(job,))
        _, text = invoke(report_with(schedule=data))
        assert "bars" in text
        assert "failed" in text
        # 「daily」是 `job.period` 的读者：没有这一断言，健康页上的周期字段没有主人
        assert "daily" in text
        assert "周期 2024-10-25" in text
        assert "连续失败 2 个周期" in text
        assert "connection reset by peer" in text

    def test_job_never_run_says_so(self, invoke) -> None:
        job = JobHealth(name="bars", market="A", period="daily", latest=None, streak=0)
        data = ScheduleData(heartbeat=beat(60), beat_age_s=60, admins=2, jobs=(job,))
        _, text = invoke(report_with(schedule=data))
        assert "从未运行" in text


class TestScheduleAbsent:
    def test_unwired_reading_keeps_the_section_and_exits_0(self, invoke) -> None:
        """这一节不因「没接入」而消失：缺席会被读成「调度器好好的」，
        而那一句说的才是事实——还没有读数接进来。"""
        code, text = invoke(HealthReport(db_path=":memory:"))
        assert "[调度]" in text
        assert "未接入调度器读数" in text
        assert code == 0


class TestSystemBacklogLine:
    def test_system_backlog_gets_its_own_line_not_the_user_one(self, invoke) -> None:
        """系统与用户是两个受害者，分两行：给用户的那句修法（`sams alert list`）
        对「哪个 job 在反复失败」这个问题没有意义。"""
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
        assert "sams run jobs" in text
        assert "未处理告警 2 条" not in text, "用户那句说的还是用户的 0 条"
        assert "未处理告警 0 条" in text
        assert code == 0
