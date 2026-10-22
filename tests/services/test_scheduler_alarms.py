"""`SystemAlarm` → 管理员（D102）。

四条规则：只有 `is_admin && active` 收；形状是 `market='SYS'`、`code=component`、
`rule_id=NULL`；同组件同人一个冷却窗口只发一次；去重读库，重启之后仍是同一次故障。
每一枚都钉两头——该沉默的那一次没出声、该出声的那一次确实出声，否则「把冷却写成零」
与「写成无穷」都能全绿。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from support import FrozenClock
from support_scheduler import PersistingSender

from sams.core.errors import TransientError
from sams.core.events import EventBus, SystemAlarm
from sams.core.notify import STRONG
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
