"""路由：规则的通道 → 级别的默认 → 用户的默认；然后才是免打扰这道闸。

这里断言的全是「屏幕上那句话与库里那一行」：发送本身在 T-11~T-13 已经被逐字节钉过，
router 的价值在于「该发给谁、没发出去时怎么说」。

装配用 `world(...)` 而不是 `make_container`：这一层的输入只有三个仓库加一个工厂，
把 300 天 K 线播进库里再测路由，等于让每一次路由改动都要先跑一遍估值合成。
容器级的那一枚用例在 `tests/test_bootstrap.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.core.events import EventBus, NotifyFailed
from sams.core.notify import (
    FAILED,
    NO_CHANNEL,
    SENT,
    SUPPRESSED,
    AlertMessage,
    NotifyResult,
)
from sams.monitor.router import NotifyService
from sams.storage.crypto import cipher_for
from sams.storage.repos.alerts import Alert, AlertRepo
from sams.storage.repos.channels import Channel, ChannelRepo
from sams.storage.repos.users import UserRepo

PASSWORD = "correct-horse-battery"


class SpyNotifier:
    """`available` 做成方法而不是属性：协议里它是方法，替身长得不一样就会让
    「router 调用的是方法」这一条没被真正测到。"""

    def __init__(self, kind, *, ok=True, can_send=True, reason=""):
        self.name = kind
        self.ok = ok
        self.can_send = can_send
        self.reason = reason
        self.sent: list[AlertMessage] = []

    def available(self) -> bool:
        return self.can_send

    def unavailable_reason(self) -> str:
        return "" if self.can_send else (self.reason or "通道不可用")

    def send(self, message):
        self.sent.append(message)
        if not self.can_send:
            return NotifyResult(ok=False, error=self.unavailable_reason(), unavailable=True)
        return NotifyResult(ok=self.ok, error="" if self.ok else "对端说 no", attempts=1)


@dataclass
class World:
    channels: ChannelRepo
    alerts: AlertRepo
    users: UserRepo
    bus: EventBus
    notify: NotifyService
    made: dict
    waits: list

    def user(self, username="trader"):
        """只建用户、不建通道：路由的结果完全由「有几条什么 kind 的通道」决定，
        把它藏进 fixture 里就等于每个用例都在测同一个隐藏前提。"""
        self.users.create(username, PASSWORD)
        return self.users.get_by_username(username)

    def add(self, user_id, kind, name, config=None) -> Channel:
        return self.channels.create(user_id, kind, name, config or {"url": f"https://x/{kind}"})

    def service(self, factory, **over):
        return NotifyService(
            self.channels,
            self.alerts,
            self.users,
            bus=self.bus,
            factory=factory,
            sleep=self.waits.append,
            **over,
        )


@pytest.fixture
def world(db, settings):
    made: dict[str, SpyNotifier] = {}

    def factory(kind, config):
        node = made.get(kind) or SpyNotifier(kind)
        made[kind] = node
        return node

    cipher = cipher_for(settings)
    bus = EventBus()
    item = World(
        channels=ChannelRepo(db, cipher=cipher),
        alerts=AlertRepo(db),
        users=UserRepo(db),
        bus=bus,
        made=made,
        waits=[],
        notify=None,
    )
    item.notify = item.service(factory)
    return item


def msg(severity="mid", **over):
    base = {
        "alert_id": "",
        "market": "A",
        "code": "601088",
        "name": "中国神华",
        "account_name": "主账户",
        "rule_name": "PR 低",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "severity": severity,
        "stage": "入池",
        "triggered_at": datetime.now(UTC),
        "body": "",
    }
    base.update(over)
    return AlertMessage(**base)


def test_rule_channels_win_over_everything(world):
    user = world.user()
    a = world.add(user.id, "wecom", "群 A")
    world.add(user.id, "dingtalk", "钉 B")
    result = world.notify.dispatch(msg(), user_id=user.id, channel_ids=[a.id])
    assert {d.kind for d in result.deliveries} == {"wecom"}


def test_level_default_route_when_the_rule_says_nothing(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    world.add(user.id, "dingtalk", "钉 B")
    world.add(user.id, "email", "邮件")
    result = world.notify.dispatch(msg(severity="mid"), user_id=user.id, channel_ids=[])
    assert {d.kind for d in result.deliveries} == {"wecom", "dingtalk"}


def test_user_default_channel_covers_a_level_with_no_enabled_match(world):
    """mid 默认走企微+钉钉，用户只配了邮件（且它是默认）：那就要发邮件，而不是回一句
    「没有通道」——「我配了通道却没收到」是这条最容易被报的错。"""
    user = world.user()
    world.add(user.id, "email", "邮件", {"host": "h", "to": ["a@b.c"]})
    result = world.notify.dispatch(msg(severity="mid"), user_id=user.id, channel_ids=[])
    assert {d.kind for d in result.deliveries} == {"email"}


def test_no_channel_at_all_is_written_as_no_channel(world):
    user = world.user()
    result = world.notify.dispatch(msg(), user_id=user.id, channel_ids=[])
    assert [d.outcome for d in result.deliveries] == [NO_CHANNEL]
    assert "启用" in result.deliveries[0].error
    row = world.alerts.deliveries(result.alert_id)[0]
    assert row.channel_id is None and row.outcome == NO_CHANNEL


def test_a_disabled_rule_channel_does_not_fall_through(world):
    """规则明确指向 A 群而 A 群停用了：不降级到默认。降级会把一条指定给 A 的告警
    发到 B 去，而屏幕上只写着「已发送」。

    先建 B 再建 A：仓库把第一条自动设成默认，而默认通道不许停用（那是兜底）。
    """
    user = world.user()
    world.add(user.id, "dingtalk", "钉 B")
    a = world.add(user.id, "wecom", "群 A")
    world.channels.set_enabled(a.id, False)
    result = world.notify.dispatch(msg(), user_id=user.id, channel_ids=[a.id])
    assert [d.outcome for d in result.deliveries] == [NO_CHANNEL]
    assert "已停用" in result.deliveries[0].error
    assert all(node.sent == [] for node in world.made.values())


def test_dnd_suppresses_mid_and_weak(world):
    """`triggered_at` 显式给：窗口的边界用「现在」的话，这条用例在 23:59 那一刻会翻。"""
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    world.notify.set_dnd(user.id, start=time(0, 0), end=time(23, 59), tz="UTC")
    for severity in ("mid", "weak"):
        result = world.notify.dispatch(
            msg(severity=severity, triggered_at=datetime(2026, 10, 20, 3, 0, tzinfo=UTC)),
            user_id=user.id,
            channel_ids=[],
        )
        assert [d.outcome for d in result.deliveries] == [SUPPRESSED], severity
        assert "免打扰" in result.deliveries[0].error
    # 压制发生在选路之前：一条通道都没有被构造过
    assert world.made == {}


def test_dnd_does_not_block_strong(world):
    """止损可能落在北京 22:00（美股时段），那正是免打扰窗口的中间。
    让 DND 挡 strong 等于让用户在睡觉时错过唯一不能错过的那一条。"""
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    world.notify.set_dnd(user.id, start=time(0, 0), end=time(23, 59), tz="UTC")
    result = world.notify.dispatch(
        msg(severity="strong", triggered_at=datetime(2026, 10, 20, 3, 0, tzinfo=UTC)),
        user_id=user.id,
        channel_ids=[],
    )
    assert [d.outcome for d in result.deliveries] == [SENT]


def test_dnd_window_crossing_midnight(world):
    user = world.user()
    world.notify.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="UTC")
    fresh = world.users.get(user.id)
    assert world.notify._in_dnd(fresh, datetime(2026, 10, 20, 2, 0, tzinfo=UTC))
    assert not world.notify._in_dnd(fresh, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    assert world.notify._in_dnd(fresh, datetime(2026, 10, 20, 23, 30, tzinfo=UTC))


def test_dnd_is_read_in_the_users_zone_not_the_market_zone(world):
    """免打扰讲的是这个人在睡觉：住在上海的人在 UTC 15:00（当地 23:00）不该收到 mid。"""
    user = world.user()
    world.notify.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="Asia/Shanghai")
    fresh = world.users.get(user.id)
    assert world.notify._in_dnd(fresh, datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    assert not world.notify._in_dnd(fresh, datetime(2026, 10, 20, 1, 0, tzinfo=UTC))


def test_clear_dnd_opens_the_gate_again(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    world.notify.set_dnd(user.id, start=time(0, 0), end=time(23, 59), tz="UTC")
    world.notify.clear_dnd(user.id)
    result = world.notify.dispatch(msg(severity="mid"), user_id=user.id, channel_ids=[])
    assert [d.outcome for d in result.deliveries] == [SENT]


def test_an_unavailable_local_channel_is_suppressed_not_failed(world):
    user = world.user()
    world.add(user.id, "voice", "语音")
    world.made["voice"] = SpyNotifier("voice", can_send=False, reason="远程会话（SSH）")
    result = world.notify.dispatch(msg(severity="strong"), user_id=user.id, channel_ids=[])
    row = result.deliveries[0]
    assert row.outcome == SUPPRESSED
    assert "SSH" in row.error
    # 压制不改通道状态：健康页上的失败率只该由真失败构成
    assert world.channels.show(row.channel_id).last_error == ""


def test_a_failing_channel_is_recorded_and_published(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    world.made["wecom"] = SpyNotifier("wecom", ok=False)
    seen: list[object] = []
    world.bus.subscribe(NotifyFailed, seen.append)
    result = world.notify.dispatch(msg(), user_id=user.id, channel_ids=[])
    assert [d.outcome for d in result.deliveries] == [FAILED]
    assert len(seen) == 1 and seen[0].channel_id
    # mark_error 也被调用过：健康页要能说出这个通道最后一次为什么失败
    assert world.channels.show(result.deliveries[0].channel_id).last_error


def test_retries_then_succeeds_without_sleeping_real_seconds(world):
    """`attempts` 与 `sleep` 都注入：这里传一个记录用的空 sleep，退避的秒数由
    core.retry 保证。替身自己数调用次数——「重试发生在 router 而不在通道里」这条
    要能被看见：附录 E/F/G 里每家都只发网络上的一次。"""

    counts = {"n": 0}

    class Flaky:
        name = "wecom"

        def available(self):
            return True

        def unavailable_reason(self):
            return ""

        def send(self, message):
            counts["n"] += 1
            done = counts["n"] >= 3
            return NotifyResult(ok=done, error="" if done else "抖动", attempts=1)

    flaky = Flaky()
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    service = world.service(lambda kind, config: flaky, attempts=3)
    result = service.dispatch(msg(), user_id=user.id, channel_ids=[])
    assert [d.outcome for d in result.deliveries] == [SENT]
    assert counts["n"] == 3
    assert world.waits == [1.0, 4.0]
    row = world.alerts.deliveries(result.alert_id)[0]
    assert row.attempts == 3


def test_attempts_one_does_not_sleep(world):
    counts = {"n": 0}

    class Once(SpyNotifier):
        def send(self, message):
            counts["n"] += 1
            return NotifyResult(ok=False, error="对端说 no")

    user = world.user()
    world.add(user.id, "wecom", "群 A")
    service = world.service(lambda kind, config: Once("wecom"), attempts=1)
    result = service.dispatch(msg(), user_id=user.id, channel_ids=[])
    assert [d.outcome for d in result.deliveries] == [FAILED]
    assert counts["n"] == 1 and world.waits == []
    assert result.deliveries[0].attempts == 1


def test_dispatch_never_touches_the_alert_status(world):
    """`alerts.status` 是人对这条告警做了什么（new/handled/dismissed），
    不是发没发出去。router 写的是 `notify_log`；混在一起会让「发失败」把一条
    已经处理过的告警重新变回 new。"""
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    result = world.notify.dispatch(msg(), user_id=user.id, channel_ids=[])
    assert world.alerts.get(result.alert_id).status == "new"


def test_dispatch_rejects_an_unknown_user(world):
    with pytest.raises(ValidationError, match="没有用户"):
        world.notify.dispatch(msg(), user_id="nobody")


def test_the_sent_message_carries_the_real_alert_id(world):
    """通道拿到的 `alert_id` 是库里那一个：M4 的深链、以及「这条通知对应哪一行」
    全靠它，一个手写的串会在屏幕上留下一个查不到的 id。"""
    user = world.user()
    node = world.add(user.id, "wecom", "群 A")
    result = world.notify.dispatch(msg(alert_id="手写的"), user_id=user.id, channel_ids=[node.id])
    assert world.made["wecom"].sent[0].alert_id == result.alert_id


def test_alert_row_keeps_the_rule_and_account(world):
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    result = world.notify.dispatch(
        msg(), user_id=user.id, channel_ids=[], rule_id="r1", account_id="acc1"
    )
    row = world.alerts.get(result.alert_id)
    assert row.rule_name == "PR 低"
    assert row.account_name == "主账户"
    assert row.rule_id == "r1" and row.account_id == "acc1"
    assert row.severity == "mid"


def test_probe_sends_a_synthetic_weak_message(world):
    """没有 probe()：探针就发一条 weak 的合成消息，走的代码路径与真实告警完全相同。"""
    user = world.user()
    node = world.add(user.id, "wecom", "群 A")
    report = world.notify.probe(node.id)
    assert report.ok
    assert world.made["wecom"].sent[0].severity == "weak"
    assert "探针" in world.made["wecom"].sent[0].rule_name


def test_probe_marks_the_channel_but_creates_no_alert(world):
    """探针不是一条告警的投递（`notify_log.alert_id` NOT NULL），所以库里不该多一条
    alert；但它真发过一次，通道状态要跟着变。"""
    user = world.user()
    node = world.add(user.id, "wecom", "群 A")
    world.notify.probe(node.id)
    assert world.alerts.list_for(user.id) == []
    assert world.channels.show(node.id).last_ok_at is not None


def test_probe_of_a_bad_channel_marks_the_error(world):
    user = world.user()
    world.made["wecom"] = SpyNotifier("wecom", ok=False)
    node = world.add(user.id, "wecom", "群 A")
    report = world.notify.probe(node.id)
    assert not report.ok
    assert world.channels.show(node.id).last_error


def test_add_refuses_a_kind_the_factory_does_not_know(world):
    """落库之前先过工厂：库里躺一条认不出的 kind，后果不是报错而是这条通道在路由时被
    静默跳过，用户看到的是「我配了通道却没收到」。

    这一枚用**真工厂**而不是 Spy：要验的正是那道 kind 名单闸门，Spy 会照单全收。
    """
    from sams.monitor.notifiers import build_notifier_factory

    user = world.user()
    service = world.service(build_notifier_factory())
    with pytest.raises(ValidationError, match="telegram"):
        service.add(user.id, "telegram", "跑掉的", {"token": "t"})
    assert world.channels.list_for(user.id) == []


def test_add_refuses_a_remote_channel_missing_its_webhook(world):
    """kind 需要哪些字段是**工厂**的知识：仓库层允许空配置（`desktop` 没得配），
    所以「wecom 少了 url」这一句必须从工厂这里出来，而且必须在落库之前。
    """
    from sams.monitor.notifiers import build_notifier_factory

    user = world.user()
    service = world.service(build_notifier_factory())
    with pytest.raises(ValidationError, match="url"):
        service.add(user.id, "wecom", "空群", {})
    assert world.channels.list_for(user.id) == []


def test_add_accepts_a_local_channel_with_no_config(world):
    from sams.monitor.notifiers import build_notifier_factory

    user = world.user()
    service = world.service(build_notifier_factory())
    assert service.add(user.id, "desktop", "本机弹窗", {}).config == {}


def test_maintenance_entries_scope_by_user_when_given(world):
    mine = world.user("trader")
    theirs = world.user("other")
    node = world.add(mine.id, "wecom", "群 A")
    assert world.notify.show(node.id, user_id=mine.id).id == node.id
    with pytest.raises(ValidationError, match="不存在"):
        world.notify.show(node.id, user_id=theirs.id)
    with pytest.raises(ValidationError, match="不存在"):
        world.notify.remove(node.id, user_id=theirs.id)
    assert world.channels.show(node.id) is not None


def test_local_status_reports_both_local_channels(world):
    from sams.monitor.env import LocalEnv

    env = LocalEnv.detect(
        environ={"SSH_TTY": "/dev/pts/0"},
        which={"osascript": "/usr/bin/osascript", "say": "/usr/bin/say"},
        platform="darwin",
    )
    status = world.service(lambda kind, config: SpyNotifier(kind), env=env).local_status()
    assert set(status) == {"desktop", "voice"}
    assert "远程" in status["desktop"] and "远程" in status["voice"]


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


def test_record_then_deliver_rebuilds_every_text_field(world):
    """`_message_from` 的逐字段核对：对照物必须是**传进 `record` 的那条消息**，
    不是第二次 `_message_from` 的结果——拿同一个函数和它自己比，把任何文本字段
    抹成都空串也照样绿（上一版的比对就有这个毛病，正文、值、阈值全靠它蒙混）。
    """
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    alert = world.notify.record(
        msg(action_note="按计划处置", body="现金流覆盖分红两倍，跌到 0.4 也不减仓"),
        user_id=user.id,
    )
    world.notify.deliver_existing(alert.id)
    sent = world.made["wecom"].sent[-1]
    assert sent.name == "中国神华"
    assert sent.account_name == "主账户"
    assert sent.rule_name == "PR 低"
    assert sent.value_text == "PR=0.31"
    assert sent.threshold_text == "PR≤0.4"
    assert sent.action_note == "按计划处置"
    assert sent.stage == "入池"
    assert sent.body == "现金流覆盖分红两倍，跌到 0.4 也不减仓"
    assert sent.alert_id == alert.id


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


def test_a_vanished_recipient_leaves_a_row_before_raising(world, db):
    """收件人已不在时，`deliver_existing` 的拒绝也要先留下一行 `no_channel` 再抛：
    只打在屏幕上的一句话在用户关掉终端之后就消失了，而「我那天为什么没收到」是
    几周后才要问的那句（模块开头那条不变式对每一条拒绝成立，不只对压制）。

    raise 本身要保住：下一个任务的队列要靠它区分「没有收件人」，并且它会对自己
    那一侧的失败另调 `abandon`。查用户排在拼正文与建上下文**之前**：为一个已经不
    在的人跑一次可能很贵的上下文构建，是白花的工作。
    """
    user = world.user()
    world.add(user.id, "wecom", "群 A")
    alert = world.notify.record(msg(), user_id=user.id)
    db.execute("DELETE FROM users WHERE id = ?", [user.id])
    built: list[str] = []

    def build_context(a: Alert) -> str:
        built.append(a.id)
        return "当时的批注"

    with pytest.raises(ValidationError, match="没有用户"):
        world.notify.deliver_existing(alert.id, context_provider=build_context)
    rows = world.alerts.deliveries(alert.id)
    assert len(rows) == 1
    assert rows[0].outcome == NO_CHANNEL
    assert "接收人" in rows[0].error and "用户" in rows[0].error
    assert built == []
