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
