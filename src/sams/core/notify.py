"""通知的词汇与形状：级别、默认路由、字数上限、那条标题行（spec §11）。

不含 I/O 是这一层的定义性约束：`storage` 要按 `OUTCOMES` 校验写入、`monitor` 要按
`LIMITS` 剪消息、`cli` 要按 `SEVERITIES` 解释参数，三层都能引用它，而它不引用任何一层。

`DEFAULT_ROUTE` 与 `LIMITS` 各只有一个出处：路由表写在通道实现里，加一个通道就要同时改
六个文件；上限写在各个通道文件里，改一个数字就要记得另外五个是不是也该改。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sams.core.errors import ValidationError
from sams.core.timeutil import format_utc_moment

#: 三级（§11）。拼死三个而不是开放字符串：路由表按它分支，多一个取值就是漏一个分支。
SEVERITIES = ("strong", "mid", "weak")
STRONG, MID, WEAK = SEVERITIES

#: `notify_log.outcome` 的四态。后两种既不是成功也不是失败——把它们算进「失败」，
#: 健康页就会把「你自己设了免打扰」报成「企微挂了」。
OUTCOMES = ("sent", "failed", "suppressed", "no_channel")
SENT, FAILED, SUPPRESSED, NO_CHANNEL = OUTCOMES

#: 级别的默认去处（§11 的三级路由里第二级）。
#: `strong` 带 voice：止损可能落在北京 22:00（美股时段），那时只有本机出声才算「送到」。
DEFAULT_ROUTE: dict[str, tuple[str, ...]] = {
    STRONG: ("voice", "wecom", "dingtalk"),
    MID: ("wecom", "dingtalk"),
    WEAK: ("email",),
}

#: 六个通道的名字（顺序即 `sams channel add --kind` 的帮助文本顺序）。
#: 它住在 core 而不是 monitor：`services/health.py` 要读它判断「库里有种通道工厂不认识」，
#: 而 `services` 不许 import `monitor`。「有哪些通道」是域知识，不是实现细节。
KINDS = ("wecom", "dingtalk", "pushplus", "email", "desktop", "voice")

#: 每条通道的正文字节上限（实测/文档值）。超出就按 UTF-8 边界剪，末尾补一个省略号。
LIMITS: dict[str, int] = {
    "wecom": 4000,
    "dingtalk": 18000,
    "pushplus": 60000,
    "email": 20000,
    "desktop": 200,
    "voice": 140,
}

#: 标题行的级别前缀：只有 strong 加。给 mid/weak 也加前缀，等于让最响的那个安静下来。
_SEVERITY_PREFIX: dict[str, str] = {STRONG: "【强】", MID: "", WEAK: ""}

#: 屏幕上「不知道」要说出来的那几个字。写成常量是因为标题行要能被逐字断言。
UNKNOWN_ACCOUNT = "未知账户"
UNKNOWN_NAME = "无名标的"


@dataclass(frozen=True)
class AlertMessage:
    """一条要发出去的东西：所有字段都是**已经决定过**的事实，不做任何取数或换算。

    `*_text` 三件套（值、阈值、动作说明）是**已经格式化好的串**而不是数值：数值进到这里
    就会有两个口径问题（本币 vs 报告币、小数 vs 百分数），而口径是读用例那一层的事
    （§5.4 规则 3）。通知层只负责把那句话搬到六个通道上。
    """

    market: str
    code: str
    severity: str
    triggered_at: datetime
    name: str = ""
    account_name: str = ""
    rule_name: str = ""
    value_text: str = ""
    threshold_text: str = ""
    action_note: str = ""
    stage: str = ""
    body: str = ""
    #: 那条 alert 的 id。**由 `NotifyService.dispatch` 填**（附录 H 里那句
    #: `dataclasses.replace`），调用方给什么都会被覆盖：库里那一行才是这条告警的身份，
    #: 一个手写的串会变成「屏幕上有个 id，按它查不到任何东西」。
    #: 通道模板里若要用它（M4 的深链就是），拿到的一定是真实那一个。
    alert_id: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValidationError(
                f"未知严重级别 severity={self.severity!r}：只有 {list(SEVERITIES)}，"
                "路由表按这三个值分支"
            )
        if not self.market or not self.code:
            raise ValidationError(
                "AlertMessage 必须说清是哪只票：`market` 与 `code` 是标题行的前两段，"
                "空着的标题行在手机上读不成一件事"
            )


@dataclass(frozen=True)
class NotifyResult:
    """一次发送的结果。`unavailable` 是第三个维度，不是 `ok=False` 的一种写法。

    只分成功/失败的话，「这台机器没有桌面」与「企微 token 失效」都会记成失败，
    健康页于是永远在报「通知在失败」，而真坏掉的那个通道藏在噪音里。
    router 按这个标志写 `suppressed` 而不是 `failed`，并且**不**调 `mark_error`。
    """

    ok: bool
    error: str = ""
    unavailable: bool = False
    attempts: int = 1


def route_for(severity: str) -> tuple[str, ...]:
    """这一级默认走哪几个通道 kind（顺序即尝试顺序）。"""
    if severity not in DEFAULT_ROUTE:
        raise ValidationError(
            f"未知严重级别 severity={severity!r}：只有 {list(SEVERITIES)}，"
            "路由表按这三个值分支"
        )
    return DEFAULT_ROUTE[severity]


def format_headline(message: AlertMessage) -> str:
    """§11 那条标题行。六个通道共用同一句：同一件事在企微和邮件里长得不一样，
    读的人就要多花一次「这两条是不是同一条」的力气。"""
    prefix = _SEVERITY_PREFIX[message.severity]
    account = message.account_name.strip() or UNKNOWN_ACCOUNT
    name = message.name.strip() or UNKNOWN_NAME
    when = format_utc_moment(message.triggered_at)
    return (
        f"{prefix}[{message.market}][{account}] {name} {message.code}"
        f" | {message.rule_name or '未命名规则'}"
        f" | {message.value_text or '?'}/{message.threshold_text or '?'}"
        f" | {when} | {message.stage or '触发'}"
    )


def trim(text: str, *, limit: int) -> str:
    """按 UTF-8 **字节**剪到 `limit` 以内，末尾补 `…`；剪在半个汉字上就丢掉那半个。

    通道限制的是请求体字节数。按 `len(text)` 剪的话，一条 4000 个汉字的内容在这里
    是 4000「通过」，到企微那边是一个 41003 错误——而错误发生在网络上，
    比在这里少发两个字贵得多。
    """
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    room = max(0, limit - len("…".encode()))
    kept = encoded[:room].decode(errors="ignore")
    return kept + "…"
