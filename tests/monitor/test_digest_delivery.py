"""摘要投递（S1-C）：一条文字通道、免打扰照旧、结局写回摘要行。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

import pytest

from sams.core.events import EventBus
from sams.core.notify import FAILED, NO_CHANNEL, SENT, SUPPRESSED, DigestMessage, NotifyResult
from sams.monitor.router import NotifyService
from sams.storage.crypto import cipher_for
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.channels import ChannelRepo
from sams.storage.repos.digests import DigestRepo
from sams.storage.repos.users import UserRepo

PASSWORD = "correct-horse-battery"
AS_OF = date(2026, 10, 30)


class SpyNotifier:
    def __init__(self, kind: str) -> None:
        self.name = kind
        self.sent: list[DigestMessage] = []

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def send(self, message) -> NotifyResult:
        self.sent.append(message)
        return NotifyResult(ok=True)


@dataclass
class World:
    db: object
    settings: object
    users: UserRepo
    channels: ChannelRepo
    alerts: AlertRepo
    digests: DigestRepo
    made: dict[str, SpyNotifier]
    notify: NotifyService

    def user(self, username="trader"):
        self.users.create(username, PASSWORD)
        return self.users.get_by_username(username)

    def add(self, user_id, kind, name):
        return self.channels.create(user_id, kind, name, {"url": f"https://x/{kind}"})

    def digest(self, user_id):
        row, _ = self.digests.save(
            user_id=user_id,
            market="A",
            as_of=AS_OF,
            title=f"A · {AS_OF} 收盘摘要",
            body="【自选】\n· 无变化",
            has_changes=False,
            heartbeat=True,
        )
        return row


@pytest.fixture
def world(db, settings):
    made: dict[str, SpyNotifier] = {}

    def factory(kind, config):
        node = made.get(kind) or SpyNotifier(kind)
        made[kind] = node
        return node

    digests = DigestRepo(db)
    users = UserRepo(db)
    channels = ChannelRepo(db, cipher=cipher_for(settings))
    alerts = AlertRepo(db)
    notify = NotifyService(
        channels,
        alerts,
        users,
        bus=EventBus(),
        factory=factory,
        sleep=lambda _: None,
        digests=digests,
    )
    return World(db, settings, users, channels, alerts, digests, made, notify)


def test_a_digest_goes_to_one_text_channel_and_records_sent(world):
    user = world.user()
    world.add(user.id, "wecom", "群")
    world.add(user.id, "desktop", "本机")
    digest = world.digest(user.id)

    report = world.notify.deliver_digest(digest.id)

    assert report.outcome == SENT
    assert "wecom" in world.made and "desktop" not in world.made
    message = world.made["wecom"].sent[0]
    assert isinstance(message, DigestMessage)
    assert message.body == "【自选】\n· 无变化"
    got = world.digests.get_by_id(digest.id)
    assert got is not None
    assert got.delivery_outcome == SENT
    assert got.delivered_at is not None


def test_a_digest_prefers_the_default_text_channel(world):
    user = world.user()
    world.add(user.id, "wecom", "群")
    mail = world.add(user.id, "email", "邮箱")
    world.channels.set_default(mail.id)
    digest = world.digest(user.id)

    world.notify.deliver_digest(digest.id)

    assert world.made["email"].sent
    assert not world.made.get("wecom") or not world.made["wecom"].sent


def test_no_text_channel_says_no_channel_instead_of_trying_desktop(world):
    user = world.user()
    world.add(user.id, "desktop", "本机")
    digest = world.digest(user.id)

    report = world.notify.deliver_digest(digest.id)

    assert report.outcome == NO_CHANNEL
    assert "文字通道" in report.error
    assert "desktop" not in world.made
    assert world.digests.get_by_id(digest.id).delivery_outcome == NO_CHANNEL


def test_dnd_suppresses_a_digest(world):
    user = world.user()
    world.add(user.id, "wecom", "群")
    world.users.set_dnd(user.id, start=time(0, 0), end=time(23, 59), tz="UTC")
    digest = world.digest(user.id)

    report = world.notify.deliver_digest(digest.id)

    assert report.outcome == SUPPRESSED
    assert "免打扰" in report.error
    # 压制发生在选路之前：连通道实现都不该被建出来
    assert "wecom" not in world.made
    assert world.digests.get_by_id(digest.id).delivery_outcome == SUPPRESSED


def test_a_send_failure_is_recorded_on_the_digest(world):
    user = world.user()
    channel = world.add(user.id, "wecom", "群")
    digest = world.digest(user.id)
    notifier = world.notify._build("wecom", world.channels.credentials(channel.id))
    notifier.send = lambda message: NotifyResult(ok=False, error="对端说 no")  # type: ignore[method-assign]

    report = world.notify.deliver_digest(digest.id)

    assert report.outcome == FAILED
    got = world.digests.get_by_id(digest.id)
    assert got.delivery_outcome == FAILED
    assert "对端说 no" in got.delivery_error
