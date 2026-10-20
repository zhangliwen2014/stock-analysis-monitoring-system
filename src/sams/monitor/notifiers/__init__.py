"""通道实现的注册处：协议、注入件、工厂。

`Notifier` 只有三个方法（`available`/`unavailable_reason`/`send`）。刻意没有 `probe()`：
探针就是「发一条 weak 的合成消息」。有独立的 probe 实现，就等于每个通道多一条只有探针
走的代码路径，而「探针成功」这句话的意思会退化成「另一条路径没坏」。

`NotifierDeps` 的四个注入件是这一层与真实世界之间唯一的接缝：测试从这里换掉网络与进程，
`build_notifier_factory()` 不传 deps 时装配真实实现。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Protocol

from sams.core.errors import ValidationError
from sams.core.notify import KINDS, WEAK, AlertMessage, NotifyResult
from sams.monitor.env import DESKTOP, VOICE, LocalEnv

__all__ = [
    "KINDS",
    "Notifier",
    "NotifierDeps",
    "UnavailableNotifier",
    "build_notifier_factory",
    "default_deps",
    "probe_message",
]

#: 只有这两个 kind 读 `deps.env`；其余通道发不发的出去与本机有没有桌面无关
LOCAL_KINDS = (DESKTOP, VOICE)

#: kind → (模块, 类名)。延迟 import（不是在文件顶部 import 六个模块）有两个理由：
#: 拼错 kind 时不该已经把六个通道全加载一遍，而 desktop 不可用时那一条实现根本不该被读进来。
_CHANNELS = {
    "wecom": ("sams.monitor.notifiers.wecom", "WecomNotifier"),
    "dingtalk": ("sams.monitor.notifiers.dingtalk", "DingTalkNotifier"),
    "pushplus": ("sams.monitor.notifiers.pushplus", "PushPlusNotifier"),
    "email": ("sams.monitor.notifiers.email", "EmailNotifier"),
    DESKTOP: ("sams.monitor.notifiers.desktop", "DesktopNotifier"),
    VOICE: ("sams.monitor.notifiers.voice", "VoiceNotifier"),
}


class Post(Protocol):
    """`POST url`，body 是 dict，headers 是 dict；返回解析后的 JSON（dict）。"""

    def __call__(
        self, url: str, body: Mapping[str, object], headers: Mapping[str, str]
    ) -> Mapping[str, object]: ...


class Mail(Protocol):
    def __call__(
        self, config: Mapping[str, object], to: list[str], subject: str, text: str
    ) -> None: ...


class Run(Protocol):
    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess: ...


@dataclass(frozen=True)
class NotifierDeps:
    post: Post
    mail: Mail
    run: Run
    #: 只有 desktop/voice 会读它；其余通道传 None 是合法的
    env: LocalEnv | None


class Notifier(Protocol):
    name: str

    def available(self) -> bool:
        """现在能不能发。`False` 时 `send()` 必须返回 `unavailable=True`，不许抛。"""
        ...

    def unavailable_reason(self) -> str:
        ...

    def send(self, message: AlertMessage) -> NotifyResult:
        ...


class UnavailableNotifier:
    """工厂认得这个 kind，但这台机器上它发不出去（缺 `deps.env` 时也走这里）。

    保留对象而不是抛错：`sams channel list` 要能列出这条通道并显示「不可用」，
    否则用户在屏幕上看到的是一条凭据配好了却凭空消失的通道。
    """

    def __init__(self, kind: str, reason: str) -> None:
        self.name = kind
        self._reason = reason

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return self._reason

    def send(self, message: AlertMessage) -> NotifyResult:
        return NotifyResult(ok=False, error=self._reason, unavailable=True)


def _real_post(
    url: str, body: Mapping[str, object], headers: Mapping[str, str]
) -> Mapping[str, object]:
    """真实的 HTTP 出口。`requests` 只在这里被 import（T-2 的 `GATED` 放行 notifiers）。"""
    import requests

    response = requests.post(url, json=dict(body), headers=dict(headers), timeout=15)
    try:
        return response.json()
    except ValueError:
        # 对端回了 HTML（网关拦截、token 失效跳登录页）时 `.json()` 抛的是解析错误，
        # 而那句话对排障没有用处；把状态码与前 400 字带回去才看得出是哪种坏法
        return {"_status": response.status_code, "_text": response.text[:400]}


def _real_mail(
    config: Mapping[str, object], to: list[str], subject: str, text: str
) -> None:
    from sams.monitor.notifiers.email import send_mail

    send_mail(config, to, subject, text)


def _real_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        # 不是 shell：标的名称来自外部数据源，拼进 shell 字符串就是一次注入（T-13）
        shell=False,
        timeout=float(kwargs.get("timeout") or 15),
    )


def default_deps() -> NotifierDeps:
    """不传 deps 时用的真实装配。探测本机只做一次（`LocalEnv.detect()`）。"""
    return NotifierDeps(post=_real_post, mail=_real_mail, run=_real_run, env=LocalEnv.detect())


def build_notifier_factory(
    *, deps: NotifierDeps | None = None
) -> Callable[[str, Mapping[str, object]], Notifier]:
    """`kind` → 通道实现。名单只有一个出处（`core.notify.KINDS`）。"""
    active = deps if deps is not None else default_deps()

    def build(kind: str, config: Mapping[str, object]) -> Notifier:
        name = (kind or "").strip().lower()
        if name not in KINDS:
            raise ValidationError(
                f"不认识通道类型 {kind!r}：本期只有 {list(KINDS)}；"
                "库里若已有别的 kind，那是手工写入或旧版本的遗留，请用 `sams channel remove` 清掉"
            )
        if name in LOCAL_KINDS:
            env = active.env
            if env is None:
                raise ValidationError(
                    f"通道 {name} 需要本机环境快照：`NotifierDeps.env` 是 None，"
                    "真实装配走 `default_deps()`，测试里给一个 `LocalEnv.detect(...)`"
                )
            # 先问环境再 import 实现：远程终端上这两个通道永远发不出去，
            # 让它们占一份 `subprocess` 依赖没有意义
            if not env.available(name):
                return UnavailableNotifier(name, env.reason(name))
        module_path, class_name = _CHANNELS[name]
        return getattr(import_module(module_path), class_name)(config, deps=active)

    return build


def probe_message(channel_name: str) -> AlertMessage:
    """探针 = 发一条 weak 的合成消息（模块 docstring 那句话）。

    时刻用 `datetime.now(UTC)` 而不是注入的 clock：那一个数字不参与任何计算，它只是让
    这条消息在屏幕上有个时间。为了它能被断言而把时钟穿进参数表，换来的是 router 的
    构造参数多一个没人需要的依赖。
    """
    return AlertMessage(
        market="SYS",
        code=channel_name,
        severity=WEAK,
        triggered_at=datetime.now(UTC),
        name="通知探针",
        account_name="系统",
        rule_name="探针（这条不是告警）",
        value_text="探针",
        threshold_text="—",
        stage="探针",
    )
