"""发送路由：三级选路、免打扰这道闸、每一次投递都落 `notify_log`（spec §11）。

住在 `monitor` 而不是 `services`：`services` 不许 import `monitor`（分层守卫），
把路由放 services 就等于要么放宽守卫、要么让通道实现反向 import 服务层——两条路都会
把「谁认识谁」变成一个问题。

这一层的每一条拒绝都要留下一行 `notify_log`。一句只打在屏幕上的拒绝，在用户关掉终端
之后就消失了，而「我那天为什么没收到」正是几天后才要问的那句话（§4.3）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from sams.core.errors import SamsError, StorageError, ValidationError
from sams.core.events import EventBus, NotifyFailed
from sams.core.notify import (
    FAILED,
    NO_CHANNEL,
    SENT,
    STRONG,
    SUPPRESSED,
    AlertMessage,
    NotifyResult,
    route_for,
)
from sams.core.retry import Sleeper, backoff_delays
from sams.monitor.env import DESKTOP, VOICE, LocalEnv
from sams.monitor.notifiers import Notifier, build_notifier_factory, probe_message
from sams.storage.repos.alerts import Alert, AlertRepo
from sams.storage.repos.channels import Channel, ChannelRepo
from sams.storage.repos.users import User, UserRepo

logger = logging.getLogger("sams.monitor.router")

#: 免打扰的屏幕上那句措辞（要包含「免打扰」四个字：§11 的说明与用例都引用它）
DND_WORD = "免打扰"


def _message_from(alert: Alert) -> AlertMessage:
    """从库里那一行重建要发的话：发送可以发生在写入之后很久（队列、重发）。"""
    return AlertMessage(
        market=alert.market,
        code=alert.code,
        severity=alert.severity,
        triggered_at=alert.triggered_at,
        name=alert.symbol_name,
        account_name=alert.account_name,
        rule_name=alert.rule_name,
        value_text=str(alert.payload.get("value_text") or ""),
        threshold_text=str(alert.payload.get("threshold_text") or ""),
        action_note=str(alert.payload.get("action_note") or ""),
        stage=alert.stage,
        body=str((alert.payload.get("extra") or {}).get("body") or ""),
        alert_id=alert.id,
        intraday=alert.intraday,
        quote_ts=alert.quote_ts,
        quote_source=alert.quote_source,
    )


@dataclass(frozen=True)
class DeliveryReport:
    """一次投递的人话结论。与 `AlertRepo.Delivery`（库里那一行）不同：这里带着 `kind`，
    而 kind 不在 `notify_log` 里——它在 `channels` 那一行，读的时候才并起来。"""

    kind: str
    channel_id: str | None
    outcome: str
    error: str
    attempts: int

    @property
    def ok(self) -> bool:
        return self.outcome == SENT


@dataclass(frozen=True)
class DispatchResult:
    alert_id: str
    deliveries: tuple[DeliveryReport, ...]

    @property
    def any_sent(self) -> bool:
        return any(item.outcome == SENT for item in self.deliveries)

    @property
    def failed(self) -> tuple[DeliveryReport, ...]:
        return tuple(item for item in self.deliveries if item.outcome == FAILED)


class NotifyService:
    """通道的维护入口 + 一条告警的发送入口。

    两件事在一个类里不是凑合：它们共用同一份通道名单与同一个工厂，而「能维护却发不出去」
    与「能发出去却改不动」都是同一种故障（第二个入口走的是另一套选路）。

    参数表里没有 `cipher`：加解密与掩码是 `ChannelRepo` 的事，这一层拿到的一直是
    密文/掩码视图，只有发送前那一次 `credentials()` 例外。也没有 `clock`：投递时刻由
    两个仓库各自的时钟打，这里再收一个就会有两套「现在」。
    """

    def __init__(
        self,
        channels: ChannelRepo,
        alerts: AlertRepo,
        users: UserRepo,
        *,
        bus: EventBus,
        factory: Callable[[str, Mapping[str, object]], Notifier] | None = None,
        sleep: Sleeper = time.sleep,
        attempts: int = 3,
        env: LocalEnv | None = None,
    ) -> None:
        self.channels = channels
        self.alerts = alerts
        self.users = users
        self.bus = bus
        self._factory = factory if factory is not None else build_notifier_factory()
        self.sleep = sleep
        self.attempts = max(1, int(attempts))
        self.env = env

    def record(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids: Sequence[str] = (),
        rule_id: str | None = None,
        account_id: str | None = None,
        is_test: bool = False,
    ) -> Alert:
        """只落库，不发送：发送在 `deliver_existing`，两者之间可以隔着一条队列。

        `is_test` 是 `/settings/test-alert` 那条链的标记（D131）：测试告警不进
        `no_open_alert` 与健康积压，收件箱里带「测试」徽标。
        """
        user = self.users.get(user_id)
        if user is None:
            raise ValidationError(f"没有用户 {user_id}：告警没有接收人就不能发")
        return self.alerts.create(
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
            is_test=is_test,
            intraday=message.intraday,
            quote_ts=message.quote_ts,
            quote_source=message.quote_source,
        )

    def deliver_existing(
        self, alert_id: str, *, context_provider: Callable[[Alert], str] | None = None
    ) -> DispatchResult:
        """把库里已有的一条告警走完路由与投递。正文从库里重建，不信任内存里的对象。

        `context_provider` 是 M4-B 的缝：把「这条论当时的批注」接在正文后面，
        队列这一侧不需要知道上下文长什么样。
        """
        alert = self.alerts.get(alert_id)
        if alert is None:
            raise StorageError(f"告警 {alert_id} 不存在：发送必须落在一条真实告警上")
        message = _message_from(alert)
        note = context_provider(alert) if context_provider is not None else ""
        if note:
            message = replace(message, body=f"{message.body}\n{note}")
        user = self.users.get(alert.user_id)
        if user is None:
            raise ValidationError(f"没有用户 {alert.user_id}：告警没有接收人就不能发")
        dnd = self._dnd_note(user, message)
        if dnd:
            self.alerts.record(alert.id, None, SUPPRESSED, error=dnd, attempts=0)
            return DispatchResult(alert.id, (DeliveryReport("", None, SUPPRESSED, dnd, 0),))
        targets, refusal = self._route(user, message.severity, list(alert.channel_ids))
        if not targets:
            self.alerts.record(alert.id, None, NO_CHANNEL, error=refusal, attempts=0)
            return DispatchResult(alert.id, (DeliveryReport("", None, NO_CHANNEL, refusal, 0),))
        return DispatchResult(
            alert.id, tuple(self._deliver(alert.id, message, node) for node in targets)
        )

    def abandon(self, alert_id: str, *, reason: str) -> None:
        """宣告放弃：这条告警不再会发了，但那一行留在收件箱里可以重发。"""
        text = (reason or "").strip()
        if not text:
            raise ValidationError(
                "abandon 必须带原因：那一句话在屏幕上的「为什么没发出去」要能指到一件事"
            )
        self.alerts.record(alert_id, None, NO_CHANNEL, error=text, attempts=0)

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids: Sequence[str] = (),
        rule_id: str | None = None,
        account_id: str | None = None,
    ) -> DispatchResult:
        """记录这条告警，然后把它送出去。**只写 `notify_log`，不碰 `alerts.status`**。"""
        alert = self.record(
            message,
            user_id=user_id,
            channel_ids=channel_ids,
            rule_id=rule_id,
            account_id=account_id,
        )
        return self.deliver_existing(alert.id)

    def probe(self, channel_id: str, *, user_id: str | None = None) -> DeliveryReport:
        """探针 = 发一条 weak 的合成消息（`notifiers` 模块开头那句话）。

        它**不**写 `notify_log`（那里每行都要挂一条 alert，而探针不是告警），但会
        `mark_ok`/`mark_error`：一次真发过的尝试，值得改变那条通道在健康页上的状态。
        """
        node = self._require(channel_id, user_id=user_id)
        return self._attempt(node, probe_message(node.name))

    def local_status(self) -> dict[str, str]:
        """`{desktop: …, voice: …}`，值是可用的空串或那一句原因。健康页读它。"""
        env = self.env if self.env is not None else LocalEnv.detect()
        return {DESKTOP: _status(env, DESKTOP), VOICE: _status(env, VOICE)}

    # —— 维护入口（CLI 唯一认识的那一半）——

    def add(self, user_id: str, kind: str, name: str, config: Mapping[str, object]) -> Channel:
        """写之前先过一遍工厂：`kind` 拼错要在**落库之前**被拒。

        库里躺一条工厂不认识的 kind 的后果不是报错，是那条通道路由时被静默跳过，
        而用户看到的是「我配了通道却没收到」（`local_status` 与健康页的 `unknown_kinds`
        都在补这一句）。
        """
        self._build(kind, config)  # 未知 kind / 缺必填字段 → ValidationError，从这里抛出去
        return self.channels.create(user_id, kind, name, config)

    def list(self, user_id: str) -> list[Channel]:
        return self.channels.list_for(user_id)

    def show(self, channel_id: str, *, user_id: str | None = None) -> Channel:
        return self._require(channel_id, user_id=user_id)

    def set_enabled(self, channel_id: str, enabled: bool, *, user_id: str | None = None) -> None:
        node = self._require(channel_id, user_id=user_id)
        self.channels.set_enabled(node.id, enabled)

    def set_default(self, channel_id: str, *, user_id: str | None = None) -> None:
        node = self._require(channel_id, user_id=user_id)
        self.channels.set_default(node.id)

    def remove(self, channel_id: str, *, user_id: str | None = None) -> None:
        node = self._require(channel_id, user_id=user_id)
        self.channels.remove(node.id)

    def set_dnd(
        self, user_id: str, *, start: dtime | None, end: dtime | None, tz: str | None
    ) -> None:
        self._require_user(user_id)
        self.users.set_dnd(user_id, start=start, end=end, tz=tz)

    def clear_dnd(self, user_id: str) -> None:
        self._require_user(user_id)
        self.users.clear_dnd(user_id)

    # —— 内部 ——

    def _require_user(self, user_id: str) -> User:
        user = self.users.get(user_id)
        if user is None:
            raise ValidationError(f"没有用户 {user_id}")
        return user

    def _require(self, channel_id: str, *, user_id: str | None) -> Channel:
        node = self.channels.show(channel_id, user_id=user_id)
        if node is None:
            who = "" if user_id is None else f"（在用户 {user_id} 名下）"
            raise ValidationError(f"通道 {channel_id} 不存在{who}")
        return node

    def _build(self, kind: str, config: Mapping[str, object]) -> Notifier:
        return self._factory(kind, dict(config))

    def _dnd_note(self, user: User, message: AlertMessage) -> str:
        """在免打扰里就该压制的那一句原因；不该压制时空串。

        `strong` 不受挡：止损会落在北京 22:00（美股时段），而那正是唯一一条
        不能错过的告警（§4.1 与 §11 相交处的方向性决定）。
        """
        if message.severity == STRONG:
            return ""
        if not self._in_dnd(user, message.triggered_at):
            return ""
        window = f"{user.dnd_start:%H:%M}-{user.dnd_end:%H:%M} {user.dnd_tz}"
        return f"{window} {DND_WORD}（{message.severity} 级被压制，strong 不受挡）"

    def _in_dnd(self, user: User, moment: datetime) -> bool:
        """跨午夜窗口用 `start <= t or t <= end`，非跨午夜用 `start <= t <= end`。

        时刻折到**用户的那个时区**而不是 `settings` 里的市场时区：免打扰讲的是这个人
        在睡觉，而他住哪儿与他的票在哪个交易所挂牌是两件事。
        """
        if user.dnd_start is None or user.dnd_end is None or not user.dnd_tz:
            return False
        try:
            zone = ZoneInfo(user.dnd_tz)
        except Exception as exc:  # noqa: BLE001 — 库里那串时区名可能是旧的
            logger.warning(
                "用户 %s 的免打扰时区 %r 读不出：%s（按不在窗口里处理）",
                user.username,
                user.dnd_tz,
                exc,
            )
            return False
        aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        now = aware.astimezone(zone).timetz().replace(tzinfo=None)
        start, end = user.dnd_start, user.dnd_end
        return now >= start or now <= end if start > end else start <= now <= end

    def _route(self, user: User, severity: str, channel_ids: list[str]) -> tuple[list, str]:
        picked, missing = [], []
        for channel_id in channel_ids:
            node = self.channels.show(channel_id, user_id=user.id)
            if node is None:
                missing.append(f"{channel_id}（不存在）")
            elif not node.enabled:
                missing.append(f"{node.name}（已停用）")
            else:
                picked.append(node)
        if picked:
            return picked, ""
        if channel_ids:
            # 规则指定了通道却全都不可用：不再降级到默认。降级会把一条明确指向 A 群的
            # 告警发到 B 群去，而屏幕上只写着「已发送」
            return [], (
                f"规则指定的通道都不可用：{'、'.join(missing)}。"
                f"修法：`sams channel enable --user {user.username} --channel <id>`"
                " 或改规则的 channel_ids"
            )
        wanted = set(route_for(severity))
        enabled = [node for node in self.channels.list_for(user.id) if node.enabled]
        by_level = [node for node in enabled if node.kind in wanted]
        if by_level:
            return by_level, ""
        default = next((node for node in enabled if node.is_default), None)
        if default is not None:
            return [default], ""
        if not enabled:
            return [], (
                f"用户 {user.username} 名下没有启用的通道，这条告警发不出去。"
                f"三步：`sams channel add --user {user.username} --kind wecom --name 主群 "
                "--config url=…` → `sams channel probe` 试一次 → 在规则里选它"
            )
        return [], (
            f"没有 kind 属于 {severity} 级默认路由（{sorted(wanted)}）的启用通道，"
            f"也没有默认通道；已有的是 {sorted({node.kind for node in enabled})}"
        )

    def _deliver(self, alert_id: str, message: AlertMessage, node: Channel) -> DeliveryReport:
        report = self._attempt(node, message)
        self.alerts.record(
            alert_id, node.id, report.outcome, error=report.error, attempts=report.attempts
        )
        if report.outcome == FAILED:
            # §12 的「连续失败降 SystemAlarm」那个计数属于调度器（M3-D），不属于这里：
            # 一个 CLI 命令一个进程，进程里没有「连续」可言
            self.bus.publish(
                NotifyFailed(
                    alert_id=alert_id,
                    channel_id=node.id,
                    attempt=report.attempts,
                    error=report.error,
                )
            )
        return report

    def _attempt(self, node: Channel, message: AlertMessage) -> DeliveryReport:
        """真正的发送 + 退避。探针与真实告警共用这一个循环，唯一的区别是**调用方**要不要
        往 `notify_log` 写一行：探针不写（它不是一条告警的投递），但它一样会
        `mark_ok`/`mark_error`——一次真发过的尝试值得改变这条通道在健康页上的状态。
        """
        try:
            notifier = self._build(node.kind, self.channels.credentials(node.id))
        except ValidationError as exc:
            self.channels.mark_error(node.id, error=str(exc))
            return DeliveryReport(node.kind, node.id, FAILED, str(exc), 0)
        except SamsError as exc:  # 解密失败之类：也是这条通道的失败，不是整条路由的失败
            self.channels.mark_error(node.id, error=str(exc))
            return DeliveryReport(node.kind, node.id, FAILED, str(exc), 0)
        if not notifier.available():
            # 自动禁用不算失败：远程终端上的 desktop/voice 每次都「失败」，
            # 健康页就永远在报故障，真坏掉的那条通道藏在噪音里
            return DeliveryReport(
                node.kind, node.id, SUPPRESSED, notifier.unavailable_reason(), 0
            )
        delays = backoff_delays(self.attempts)
        error = ""
        for index in range(self.attempts):
            result: NotifyResult = notifier.send(message)
            if result.ok:
                self.channels.mark_ok(node.id)
                return DeliveryReport(node.kind, node.id, SENT, "", index + 1)
            if result.unavailable:
                # 发出去才发现本机做不到（`osascript` 从 `PATH` 上消失了）：
                # 与 `available()` 那一支同待——压制、不记失败，理由见上面那条注释
                return DeliveryReport(node.kind, node.id, SUPPRESSED, result.error, index + 1)
            error = result.error or "（通道没有给原因）"
            if index < len(delays):
                logger.warning(
                    "通道 %s 第 %d 次失败（%s），%.1fs 后重试",
                    node.name,
                    index + 1,
                    error,
                    delays[index],
                )
                self.sleep(delays[index])
        self.channels.mark_error(node.id, error=error)
        return DeliveryReport(node.kind, node.id, FAILED, error, self.attempts)


def _status(env: LocalEnv, kind: str) -> str:
    return "" if env.available(kind) else env.reason(kind)
