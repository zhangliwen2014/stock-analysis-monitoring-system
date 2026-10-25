"""通知的词汇与形状：级别、默认路由、字数上限、那条标题行（spec §11）。

不含 I/O 是这一层的定义性约束：`storage` 要按 `OUTCOMES` 校验写入、`monitor` 要按
`LIMITS` 剪消息、`cli` 要按 `SEVERITIES` 解释参数，三层都能引用它，而它不引用任何一层。

`DEFAULT_ROUTE` 与 `LIMITS` 各只有一个出处：路由表写在通道实现里，加一个通道就要同时改
六个文件；上限写在各个通道文件里，改一个数字就要记得另外五个是不是也该改。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

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

#: `alerts.market` 里代表「这不是一只票」的那一个值（D102）。
#: 系统告警与规则告警共用一张表，靠它与 `rule_id IS NULL` 分开；写入方（调度器的
#: `SystemAlerter`、通知探针）、读取方（健康页的积压统计、告警仓库的去重查询）
#: 必须是同一个字面量——两处写得不一样，系统告警会被算进用户积压，或者去重永远不命中。
SYSTEM_MARKET = "SYS"

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
    #: 盘中三件套（D120）：这一条是盘中判的、报价是哪一刻、出自哪个源。
    #: 放在消息上而不是事后查库：通知线程拿到的是重建出来的那份，别让它再回头查
    intraday: bool = False
    quote_ts: datetime | None = None
    quote_source: str | None = None

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


# ---------- 推送上下文（spec §7.2，D126）----------

#: 上下文块的题头那一行：屏幕上与 `notify_log` 里都按这一句逐字断言，所以它是常量。
CONTEXT_HEADER = "—— 最近批注 ——"
#: 一条推送最多带几条批注（§7.1）。固定 3 条的代价写在 spec §15：重要的旧批注要置顶。
CONTEXT_MAX_ITEMS = 3
#: 单行正文的硬上限。`plain_text` 默认也是这个数，这里再挡一次：
#: provider 递来的若是未压过的原文，一行也不许炸开。
CONTEXT_LINE_CHARS = 120
#: 只有文本通道带上下文：语音/桌面的 `LIMITS` 是 140/200 字节，放不下且价值低（D126）。
PUSH_CONTEXT_KINDS = ("wecom", "dingtalk", "pushplus", "email")


@dataclass(frozen=True)
class AnnotationContext:
    """一条要进推送的候选批注：只有「这行怎么读」与「谁先谁后」的那几样。

    `moment_label` 是**已经折好**的 `MM-DD HH:MM`：时区换算读 `server.display_tz`，
    那是 provider 的事——这一层不许 import 任何配置，也不碰 zoneinfo。
    """

    annotation_id: str
    created_at: datetime
    moment_label: str
    author_label: str
    body_text: str
    tags: tuple[str, ...] = ()
    pinned: bool = False


@dataclass(frozen=True)
class PushContext:
    """provider 给 router 的那一包：候选批注加深链。

    `items` 为空与 provider 返回 `None` 是两个事实：前者是「这只票没人写过批注」，
    后者是「这条告警不该有上下文」（系统告警没有标的）。`notify_log.context_json`
    要能分出这两样，审计才回答得了「为什么那条推送里没有批注」。
    """

    items: tuple[AnnotationContext, ...] = ()
    link: str = ""


def _select_annotation_context(
    items: Sequence[AnnotationContext],
) -> tuple[AnnotationContext, ...]:
    """按 id 去重（保留第一次出现），置顶优先、其余按时间倒序，最多 3 条（§7.1）。"""
    seen: set[str] = set()
    unique: list[AnnotationContext] = []
    for entry in items:
        if entry.annotation_id in seen:
            continue
        seen.add(entry.annotation_id)
        unique.append(entry)
    unique.sort(key=lambda entry: (entry.pinned, entry.created_at), reverse=True)
    return tuple(unique[:CONTEXT_MAX_ITEMS])


def annotation_context_line(item: AnnotationContext) -> str:
    """一条批注一行：`· MM-DD HH:MM 作者 [标签] 正文`。"""
    tags = f" [{' '.join(item.tags)}]" if item.tags else ""
    body = " ".join((item.body_text or "").split())
    if len(body) > CONTEXT_LINE_CHARS:
        body = body[: CONTEXT_LINE_CHARS - 1] + "…"
    return f"· {item.moment_label} {item.author_label}{tags} {body}".rstrip()


def format_annotation_context(
    items: Sequence[AnnotationContext],
    *,
    budget_bytes: int | None = None,
) -> tuple[str, tuple[str, ...], int]:
    """渲染上下文块，返回（文本、实际带入的 id、去重后没进这一块的条数）。

    `budget_bytes` 是「这个通道的正文还剩多少字节」：超预算从**尾部**剪（尾部是
    最新但非置顶的那几条）；连第一条都放不下就整块不发而不是截半行（§7.2）。
    预算为 `None`（`budget_bytes is None`）表示不剪——Web 详情页那种「与推送同源」
    的展示要的是完整 3 条。
    `dropped` 按**去重后**的候选计：跨源合并（标的 + 观点）会交出同一条批注两次，
    那不是「被剪掉」——把它计进审计，是在谎报这个通道装不下。
    """
    chosen = _select_annotation_context(items)
    unique = len({entry.annotation_id for entry in items})
    if not chosen:
        return "", (), 0
    lines = [CONTEXT_HEADER] + [annotation_context_line(entry) for entry in chosen]
    while (
        len(lines) > 2
        and budget_bytes is not None
        and len("\n".join(lines).encode()) > budget_bytes
    ):
        lines.pop()
    kept = len(lines) - 1
    if budget_bytes is not None and len("\n".join(lines).encode()) > budget_bytes:
        # 剪到只剩「题头 + 第一条」仍放不下：题头自己也不发了
        return "", (), unique
    text = "\n".join(lines)
    ids = tuple(entry.annotation_id for entry in chosen[:kept])
    return text, ids, unique - kept


@runtime_checkable
class AlertSender(Protocol):
    """规则引擎唯一的发送出口（D95）。`services` 不许 import `monitor`，而投递必须发生。

    返回的是 `alerts` 那一行的 **id**，不是 `DispatchResult`：那个结果类型住在
    `monitor/router.py`，`core` 引用它就是反向依赖。引擎拿 id 做两件 `monitor` 之外也能做的
    事——发 `AlertTriggered`（M3-D 的调度按它数当日已推送）与把 id 报回屏幕。
    `bootstrap` 用四行把 `NotifyService.dispatch` 的 `DispatchResult.alert_id` 折成这个串。

    参数表与 `NotifyService.dispatch` 逐字对齐（`tests/core/test_notify_alert_sender.py`
    钉这一条）：差一个参数名，红的是 M3-D 挂机跑到某条规则时，而不是这里。

    `is_test` 不属于引擎：它只会按 `False` 走（规则告警与系统告警都是真的）。它在端口
    上是因为 `NotifyService.dispatch` 有这一格（`/settings/test-alert` 要按 D131 记下
    「这条是测试」），而两份参数表差一个字就是「接得上」这句话没被钉住。
    """

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids: Sequence[str] = (),
        rule_id: str | None = None,
        account_id: str | None = None,
        is_test: bool = False,
    ) -> str: ...
