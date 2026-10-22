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


def test_an_unmigrated_db_says_the_schedule_shape_not_the_unwired_one(invoke) -> None:
    """还没迁移 ≠ 没接线：两句的修法相反，一句跑 `sams db migrate`、一句查装配，
    不能在同一屏上显示成同一行。

    这一格今天从 `sams health` 走不到（`run()` 在非 db 命令之前顺手补齐迁移），钉的是
    `_print_schedule` 对一份 report 的显示合同——与 `[通知]` 那节的「库里还没有 0015 的形状」
    同一条线的是那一句早退（未迁移就往下走只会对着不存在的表发 SQL），不是 `None` 的用法：
    那一头的 `None` 没有第三种原因要分，这一头有。

    钉整行：`[通知]` 那一节同屏也写着「sams db migrate」，只查子串的话两句会互相顶包。
    """
    report = HealthReport(
        db_path=":memory:", schedule=None, schedule_unmigrated=True, notify=NotifyData()
    )
    code, text = invoke(report)
    lines = text.splitlines()
    assert "  库里还没有 0018 的形状：sams db migrate" in lines
    assert "未接入调度器读数" not in lines
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
