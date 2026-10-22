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
