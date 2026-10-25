"""`sams alert ...` —— 告警的读屏与四态处理（§5 复盘闭环里命令行能做的那一半）。

屏幕上念的是那条告警**当时**写下的那几句话（`payload_json` 里的 `value_text`/
`threshold_text`/`action_note`），不按今天的模板重新渲染：重新渲染会在措辞改版之后说谎，
而「我当时到底看到了什么」正是复盘唯一要看的东西。

闭环的四个动词按 spec §8.1 映射：`handle` 是 acted 的 `other`（说明必填）、
`dismiss` 落 `dismiss_code=other`、`ack` 与 `defer` 是新增的两格。状态机的闸全在
仓库层（`AlertRepo`）与 `services/review`，这里只把它们翻成中文和退出码。

这一整个模块不许 import `sams.storage.*`（`ALLOWED["cli"]` 只有 `core` 与 `services`）：
`alerts`、`notify_log`、`channels` 三张表都通过 `container.alerts_store` /
`container.channels_store` 读，`users` 通过 `resolve_actor`——那是 `Container` 上装配好的对象。
校验也不在这里重复：状态与原因的闸门都在仓库层（D81），命令层只把它们翻成中文和退出码，
同一个闸门出现两处，先失效的那一处会让 `--json` 读到的拒绝话和屏幕上的不是同一句。
"""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime, timedelta

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
    actions = group(
        subparsers, "alert", "告警：看、已读、处置（说明必填）、忽略（原因必填）、稍后"
    )

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

    handle = leaf("handle", handle_alert, "标成已处置：acted 的 other——说明必填（≥2 字）")
    handle.add_argument("alert_id")
    handle.add_argument("--note", help="做了什么（这一条现在必须有：「已处置」三个字回答不了复盘）")

    dismiss = leaf("dismiss", dismiss_alert, "忽略一条：必须带原因，那一列是忽略率唯一的输入")
    dismiss.add_argument("alert_id")
    # 不用 argparse 的 required=True：拒在仓库层才能同时给 JSON（D57），
    # argparse 的失败是 SystemExit 加一段 usage，`--json` 的那一侧只读到空 stdin
    dismiss.add_argument(
        "--reason",
        help="为什么不处理；不给就拒（拒在仓库层，那里也给 JSON）。"
        "首期 CLI 只落 other 码：misreport/expected/data_issue/not_applicable/duplicate/other",
    )

    ack = leaf("ack", ack_alert, "标成已读：从此算「看到了还没做」，仍算未处理")
    ack.add_argument("alert_id")

    defer = leaf("defer", defer_alert, "稍后提醒：状态不变，写一个到点时刻（D138）")
    defer.add_argument("alert_id")
    # 不用 argparse 的 required=True：拒在解析层才能同时给 JSON（D57），
    # argparse 的失败是 SystemExit 加一段 usage，`--json` 的那一侧只读到空 stdin
    defer.add_argument(
        "--until",
        help="不给就拒（拒在解析层，那里也给 JSON）。"
        "1h/3h/2d 这样的相对时长，或带时区的绝对时间（2026-10-26T09:30+08:00）",
    )


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
        "acknowledged_at": _at(alert.acknowledged_at),
        "action_kind": alert.action_kind,
        "action_ref": alert.action_ref,
        "dismiss_code": alert.dismiss_code,
        "deferred_until": _at(alert.deferred_until),
        "thesis_id": alert.thesis_id,
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
    if alert.acknowledged_at is not None:
        print(f"  已读    {stamp(alert.acknowledged_at)}")
    if alert.action_kind:
        print(f"  动作    {alert.action_kind} {alert.action_ref or ''}".rstrip())
    if alert.deferred_until is not None:
        print(f"  稍后    {stamp(alert.deferred_until)}（到点前回收件箱，不计忽略率）")
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


# ---------- ack / defer / handle / dismiss ----------


def _emit_move(args, alert, lines: list[str]) -> int:
    if args.json:
        emit({"status": "ok", "exit": 0, "alert": _alert_payload(alert)})
        return 0
    print(_one_line(alert).removeprefix("  "))
    for line in lines:
        print(line)
    return 0


def ack_alert(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.alerts_store.acknowledge(args.alert_id, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已标成 acknowledged：{stamp(alert.acknowledged_at)}"
            "（看到了、还没做——同票其余告警照旧挡着）"
        ],
    )


def defer_alert(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        until = _parse_until(args.until)
        alert = container.alerts_store.defer(args.alert_id, user_id=user.id, until=until)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已稍后：{stamp(alert.deferred_until)} 之后回收件箱顶部；"
            f"在那之前状态仍是 {alert.status}"
        ],
    )


def handle_alert(container: Container, args) -> int:
    """`handle` 是 acted 的 CLI 入口（§8.1）：动作类型 `other`，说明必填。

    说明的闸在仓库层（`act` 对 `other` 要 ≥2 字），这里不重复——两处闸就会有两处失效。
    """
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.review.act(
            user.id, args.alert_id, action_kind="other", note=args.note or ""
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已标成 handled：{alert.handle_note}",
            "  这一条不再算未处理（`no_open_alert` 数的是 status=new/acknowledged）；"
            "同票上若还有别的新告警，那些照样挡着后面的推送——更响的那一档除外（D94）",
        ],
    )


def dismiss_alert(container: Container, args) -> int:
    """CLI 只收 `--reason`，原因码落 `other`（§8.5）：码的枚举入口在 Web 表单。"""
    try:
        user = resolve_actor(container.users, args.user)
        alert = container.alerts_store.dismiss(
            args.alert_id, user_id=user.id, code="other", reason=args.reason or ""
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    return _emit_move(
        args,
        alert,
        [
            f"  已忽略：{alert.dismiss_reason}",
            "  那一列进忽略率统计：「这条规则是不是在瞎报」只靠它回答",
        ],
    )


_RELATIVE_UNTIL = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _parse_until(value: str | None) -> datetime:
    """`1h`/`3h`/`2d` 折成「真实钟表的未来」，绝对时间按 ISO 读。

    相对时长按 `datetime.now` 而不是注入的 `clock` 算吗？——是：稍后提醒是给**人**的
    时长，仓库那头的闸（`defer` 要求未来时刻）读自己的钟，两边都在真实时间轴上。
    裸 ISO 时刻按本机时区补（`astimezone()`）：一个不带偏移的「09:30」说的是
    人那里的 09:30，不是 UTC 的 09:30。
    """
    text = (value or "").strip().lower()
    if not text:
        raise ValidationError(
            "--until 必填：用 1h/3h/2d，或带时区的绝对时间（例如 2026-10-26T09:30+08:00）"
        )
    match = _RELATIVE_UNTIL.match(text)
    if match:
        amount = int(match.group(1))
        if amount < 1:
            raise ValidationError(f"--until 的相对时长要为正（收到 {value!r}）")
        return datetime.now(UTC) + timedelta(seconds=amount * _UNIT_SECONDS[match.group(2)])
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"--until 读不懂：{value!r}。用 1h/3h/2d，或带时区的绝对时间"
            "（例如 2026-10-26T09:30+08:00）"
        ) from exc
    return moment if moment.tzinfo is not None else moment.astimezone()
