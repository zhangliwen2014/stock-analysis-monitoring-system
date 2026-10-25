"""`sams alert ...` —— 告警的读屏与四态处理（§5 复盘闭环里命令行能做的那一半）。

屏幕上念的是那条告警**当时**写下的那几句话（`payload_json` 里的 `value_text`/
`threshold_text`/`action_note`），不按今天的模板重新渲染：重新渲染会在措辞改版之后说谎，
而「我当时到底看到了什么」正是复盘唯一要看的东西。

这一整个模块不许 import `sams.storage.*`（`ALLOWED["cli"]` 只有 `core` 与 `services`）：
`alerts`、`notify_log`、`channels` 三张表都通过 `container.alerts_store` /
`container.channels_store` 读，`users` 通过 `resolve_actor`——那是 `Container` 上装配好的对象。
校验也不在这里重复：状态与原因的闸门都在仓库层（D81），命令层只把它们翻成中文和退出码，
同一个闸门出现两处，先失效的那一处会让 `--json` 读到的拒绝话和屏幕上的不是同一句。
"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container
from sams.cli.common import emit, exit_for, group, refused, stamp
from sams.core.errors import SamsError, StorageError, ValidationError
from sams.services.actors import resolve_actor

#: 这一屏的默认页长。仓库层 `list_for` 也有一个默认值，但这条路永远显式传 `limit + 1`
#: （多取的那一条只用来判断屏幕上要不要说「还有更多」），所以它在这里不生效；
#: 屏幕上那句「默认 50」的唯一出处就是这个数
DEFAULT_LIMIT = 50
#: 屏幕上 `--status` 的取值加一个「都要」。`all` 不是库里的状态，是命令层的一个别名，
#: 折成 `None` 之后由仓库层去校验真正的取值——校验只有那一个口径
STATUS_HELP = "new/acknowledged/handled/dismissed（默认 new = 只看没人处理过的），或 all"
#: 那一列存的是英文（`alerts.status`，取值与仓库层 `STATUS` 那四个一致），屏幕上说中文。
#: 「全部状态」是 `status=None` 那个分支，不在这个表里
_STATUS_WORDS = {
    "new": "未处理",
    "acknowledged": "已读",
    "handled": "已处理",
    "dismissed": "已忽略",
}


def add_to(subparsers) -> None:
    actions = group(subparsers, "alert", "告警：看、标成已处理、带原因忽略")

    def leaf(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        parser = actions.add_parser(name, help=help_text)
        parser.add_argument("--user", required=True, help="归属的用户名（不是 id）")
        parser.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
        parser.set_defaults(handler=handler)
        return parser

    listing = leaf("list", list_alerts, "列出名下告警：默认只看没处理过的那些")
    listing.add_argument("--status", help=STATUS_HELP)
    listing.add_argument("--limit", type=int, help=f"最多几条（默认 {DEFAULT_LIMIT}）")

    show = leaf(
        "show",
        show_alert,
        "单看一条：当时那三句话、每条通道的投递结果、何时按的、留了哪一句",
    )
    show.add_argument("alert_id", help="告警 id（`sams alert list` 每条的 id 那一行）")

    handle = leaf("handle", handle_alert, "标成已处理：这一条从此不再算「没人看过」")
    handle.add_argument("alert_id")
    handle.add_argument("--note", help="打算怎么处理（可空：那一列不是必填的）")

    dismiss = leaf("dismiss", dismiss_alert, "忽略一条：必须带原因，那一列是忽略率唯一的输入")
    dismiss.add_argument("alert_id")
    # 不用 argparse 的 required=True：拒在仓库层才能同时给 JSON（D57），
    # argparse 的失败是 SystemExit 加一段 usage，`--json` 的那一侧只读到空 stdin
    dismiss.add_argument("--reason", help="为什么不处理；不给就拒（拒在仓库层，那里也给 JSON）")


# ---------- 形状 ----------


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if value < 1:
        raise ValidationError(f"--limit 要的是正整数（收到 {value}）")
    return value


def _at(value) -> str | None:
    """JSON 里的时刻：没有就是 `null`，有就是 ISO 串。

    给人看的那一屏走 `stamp`；机器读的人要拿到 `null` 还是一个串，两者不能混成一句。
    """
    return None if value is None else value.isoformat()


def _text(alert, key: str) -> str:
    """`payload_json` 里那三句之一：缺键给空串，而不是让屏幕上那一行崩在一个 KeyError 上。"""
    return str(alert.payload.get(key) or "")


def _raw_extra(alert) -> dict:
    return alert.payload.get("extra") or {}


def _extra(alert) -> dict:
    """`extra` 里除了正文的那几项：`body` 单独上屏（它是多行的，混在一行 `k=v` 里没法读）。"""
    return {key: value for key, value in _raw_extra(alert).items() if key != "body"}


def _body(alert) -> str:
    return str(_raw_extra(alert).get("body") or "")


def _alert_payload(alert) -> dict:
    return {
        "id": alert.id,
        "rule_id": alert.rule_id,
        "rule_name": alert.rule_name,
        "account_id": alert.account_id,
        "account_name": alert.account_name,
        "market": alert.market,
        "code": alert.code,
        "symbol_name": alert.symbol_name,
        "stage": alert.stage,
        "severity": alert.severity,
        "triggered_at": _at(alert.triggered_at),
        "status": alert.status,
        "handled_at": _at(alert.handled_at),
        "handle_note": alert.handle_note,
        "dismiss_reason": alert.dismiss_reason,
        "value_text": _text(alert, "value_text"),
        "threshold_text": _text(alert, "threshold_text"),
        "action_note": _text(alert, "action_note"),
        "body": _body(alert),
        "extra": _extra(alert),
        "channel_ids": list(alert.channel_ids),
    }


def _one_line(alert) -> str:
    return (
        f"  {stamp(alert.triggered_at)}  {alert.severity:<6} {alert.stage:<6}"
        f" {alert.market}/{alert.code} {alert.symbol_name}  {alert.rule_name}"
    )


def _channel_name(container: Container, channel_id: str | None, user_id: str) -> str:
    """投递那一行要说的是通道**名字**。

    查不到就是它已经不在了（多半是删过——这一屏只按本用户的通道去查）：屏幕上既不能
    假装它没发过，也不能只留一段没人认得出的 hex。
    """
    if channel_id is None:
        return "（没选到通道）"
    node = container.channels_store.show(channel_id, user_id=user_id)
    return node.name if node is not None else f"{channel_id[:8]}（已删除）"


# ---------- list ----------


def list_alerts(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        limit = _limit(args.limit)
        status = None if args.status == "all" else (args.status or "new")
        # 多取一条：那一条用来告诉屏幕上要不要说「后面还有」
        rows = container.alerts_store.list_for(user.id, status=status, limit=limit + 1)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    more = len(rows) > limit
    shown = rows[:limit]
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "user": user.username,
                "filter": "all" if status is None else status,
                "limit": limit,
                "more": more,
                "alerts": [_alert_payload(alert) for alert in shown],
            }
        )
        return 0
    wanted = "全部状态" if status is None else _STATUS_WORDS[status]
    if not shown:
        print(
            f"{user.username} 名下没有{wanted}的告警："
            f"`sams rule check --user {user.username}` 跑一趟，"
            f"或 `sams alert list --user {user.username} --status all` 看历史"
        )
        return 0
    tail = f"（最新 {limit} 条之外还有更多）" if more else ""
    print(f"{user.username} 名下{wanted}的告警 {len(shown)} 条{tail}")
    for alert in shown:
        print(_one_line(alert))
        print(f"      id {alert.id}")
        print(f"      看到 {_text(alert, 'value_text')}｜阈值 {_text(alert, 'threshold_text')}")
    return 0


# ---------- show ----------


def _delivery_payload(container: Container, alert, user_id: str) -> list[dict]:
    return [
        {
            "channel": _channel_name(container, item.channel_id, user_id),
            "channel_id": item.channel_id,
            "outcome": item.outcome,
            "attempts": item.attempts,
            "error": item.error,
            "ts": _at(item.ts),
        }
        for item in container.alerts_store.deliveries(alert.id)
    ]


def show_alert(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.alerts_store.get(args.alert_id, user_id=user.id)
        if alert is None:
            # 「不存在」与「是别人的」在这一屏折成一句：两边的处置完全一样（都读不到），
            # 而把「是别人的」单独说出去就是在替别人探测 id
            raise StorageError(
                f"告警 {args.alert_id} 不存在（或不属于 {user.username}）："
                "别的用户名下的告警在这里读不到"
            )
        deliveries = _delivery_payload(container, alert, user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "alert": _alert_payload(alert),
                "deliveries": deliveries,
            }
        )
        return 0
    print(_one_line(alert).removeprefix("  "))
    print(f"  id      {alert.id}")
    print(f"  看到    {_text(alert, 'value_text')}")
    print(f"  阈值    {_text(alert, 'threshold_text')}")
    print(f"  建议    {_text(alert, 'action_note') or '（这条没有给动作建议）'}")
    body = _body(alert)
    if body:
        lines = body.splitlines()
        print(f"  正文    {lines[0]}")
        for line in lines[1:]:
            print(f"          {line}")
    extra = _extra(alert)
    if extra:
        print("  依据    " + "；".join(f"{key}={value}" for key, value in sorted(extra.items())))
    if alert.account_name or alert.account_id:
        print(f"  账户    {alert.account_name or alert.account_id}")
    if alert.rule_id:
        print(f"  规则    {alert.rule_id}")
    print(f"  状态    {alert.status}（{_STATUS_WORDS[alert.status]}）")
    if alert.handled_at is not None:
        note = alert.handle_note or alert.dismiss_reason or "（没写）"
        print(f"    {stamp(alert.handled_at)} 按的：{note}")
    if deliveries:
        print("  投递：")
        for item in deliveries:
            line = f"    {item['channel']:<14} {item['outcome']:<10} {item['attempts']} 次"
            print(line + (f"  {item['error']}" if item["error"] else ""))
    else:
        print("  投递    （库里没有这一条告警的任何投递记录：它还没出发过）")
    return 0


# ---------- handle / dismiss ----------


def handle_alert(container: Container, args) -> int:
    return _move(container, args, "handled", args.note or "")


def dismiss_alert(container: Container, args) -> int:
    return _move(container, args, "dismissed", args.reason or "")


def _move(container: Container, args, status: str, note: str) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.alerts_store.set_status(args.alert_id, status, user_id=user.id, note=note)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "alert": _alert_payload(alert)})
        return 0
    print(_one_line(alert).removeprefix("  "))
    if alert.status == "handled":
        print(f"  已标成 handled：{alert.handle_note or '（没写备注）'}")
        print(
            "  这一条不再算未处理（`no_open_alert` 数的是 status=new/acknowledged）；"
            "同票上若还有别的新告警，那些照样挡着后面的推送——更响的那一档除外（D94）"
        )
    else:
        print(f"  已忽略：{alert.dismiss_reason}")
        print("  那一列进忽略率统计：「这条规则是不是在瞎报」只靠它回答")
    return 0
