"""`sams channel ...` —— 通道的维护入口（M3-B 的全部界面）。

Web 后台在 M4，但维护通道这件事不必等它：命令行走的是同一套 `monitor.NotifyService`。

屏幕上永远不许出现明文凭据（§4.2）：这一整个模块**不许**取 `ChannelRepo` 里那份密文的原文，
它只读 `Channel.config`（掩码视图）。这件事由 `tests/test_layering.py` 里那条静态守卫
钉住，不是由这里的自觉。
"""

from __future__ import annotations

import argparse
from datetime import time

from sams.bootstrap import Container
from sams.cli.common import emit, exit_for, group, refused, stamp
from sams.core.errors import SamsError, ValidationError
from sams.core.notify import KINDS, SENT, SUPPRESSED
from sams.services.actors import resolve_actor

_LABEL = 14


def add_to(subparsers) -> None:
    actions = group(subparsers, "channel", "通知通道：增删、启停、默认、探针、免打扰")

    def leaf(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        parser = actions.add_parser(name, help=help_text)
        parser.add_argument("--user", required=True, help="归属的用户名（不是 id）")
        parser.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
        parser.set_defaults(handler=handler)
        return parser

    add = leaf("add", add_channel, "登记一条通道（凭据整份加密入库，屏幕上只见掩码）")
    add.add_argument("--kind", required=True, help=f"六个之一：{'/'.join(KINDS)}")
    add.add_argument("--name", required=True)
    add.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="可多次传入：url=/secret=/token=/host=/to=；短值全掩，长值只露末尾几位",
    )
    add.add_argument("--no-default", action="store_true", help="不要设成默认通道")

    leaf("list", list_channels, "列出名下通道：名称、kind、启停、默认、上次失败")

    show = leaf("show", show_channel, "单看一条：掩码后的每一项配置与失败原因")
    show.add_argument("--channel", required=True)

    for name, handler, help_text in (
        ("enable", enable, "启用；重新启用会清掉上次的失败原因"),
        ("disable", disable, "停用；默认通道要先把默认移走才能停用"),
        ("default", default, "设为默认：级别路由全部落空时的兜底"),
    ):
        parser = leaf(name, handler, help_text)
        parser.add_argument("--channel", required=True)

    remove = leaf("remove", remove_channel, "删除（不可撤销：凭据只有那一份密文）")
    remove.add_argument("--channel", required=True)
    remove.add_argument("--yes", action="store_true", help="确认删除")

    probe_parser = leaf("probe", probe, "真发一条探针消息：走的是与告警完全相同的那条代码")
    probe_parser.add_argument("--channel", required=True)

    dnd = actions.add_parser("dnd", help="set 设窗口 / show 看窗口 / clear 清除")
    dnd_actions = dnd.add_subparsers(dest="dnd_action", required=True)
    for name, handler, help_text in (
        ("set", dnd_set, "设免打扰窗口（起止与 --tz 三个一起给）"),
        ("show", dnd_show, "看当前窗口；没有窗口就是「不受挡」"),
        ("clear", dnd_clear, "清除窗口"),
    ):
        parser = dnd_actions.add_parser(name, help=help_text)
        parser.add_argument("--user", required=True)
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(handler=handler)
        if name == "set":
            parser.add_argument("--start", help="HH:MM，例如 23:00")
            parser.add_argument("--end", help="HH:MM，例如 07:00（可比 --start 早 = 跨午夜）")
            parser.add_argument("--tz", help="IANA 时区名，例如 Asia/Shanghai")


# ---------- 参数形状 ----------


def _config_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValidationError(f"--config 要的是 K=V，收到 {item!r}")
        out[key.strip()] = value
    return out


def _time_arg(value: str | None, flag: str) -> time | None:
    if value is None:
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(f"{flag} 要的是 HH:MM（24 小时），收到 {value!r}") from None


def _channel_row(node) -> dict:
    """出屏/出 JSON 的那一份视图：只有掩码值，永远不含 `config_encrypted`。"""
    return {
        "id": node.id,
        "name": node.name,
        "kind": node.kind,
        "enabled": node.enabled,
        "is_default": node.is_default,
        "last_ok_at": node.last_ok_at,
        "last_error": node.last_error,
        "last_error_at": node.last_error_at,
        "config": node.config,
    }


# ---------- 增删 ----------


def add_channel(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        if args.no_default and not container.notify.list(user.id):
            # 第一个通道会自动成为默认（仓库层的规则）；--no-default 是在说「不要」，
            # 而这里不能把默认取消到「一个都没有」——那是兜底路由失效的形状。
            # 检查放在落库之前：拒绝过的命令不许在库里留下半个对象。
            raise ValidationError(
                "这是名下第一条通道，取消默认会留下「没有任何兜底」的状态；"
                "先加第二条，再 `sams channel default` 指过去"
            )
        node = container.notify.add(
            user.id, args.kind, args.name, _config_pairs(args.config)
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "channel": _channel_row(node)})
    else:
        print(f"已添加通道 {node.name}（{node.kind}，默认={node.is_default}）")
        print(f"  下一步：sams channel probe --user {args.user} --channel {node.id}")
    return 0


def list_channels(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        rows = container.notify.list(user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "channels": [_channel_row(node) for node in rows]})
        return 0
    if not rows:
        print(
            f"用户 {user.username} 名下没有通道："
            f"sams channel add --user {user.username} --kind wecom --name 主群 "
            "--config url=<webhook>"
        )
        return 0
    print(f"通道（{user.username}）：")
    for node in rows:
        flags = "默认" if node.is_default else ("启用" if node.enabled else "停用")
        ok = stamp(node.last_ok_at) if node.last_ok_at else "从没成功发过"
        line = f"  {node.name:<{_LABEL}} {node.kind:<10} {flags:<4} {ok}"
        print(line + (f"\n      上次失败：{node.last_error}" if node.last_error else ""))
    return 0


def show_channel(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        node = container.notify.show(args.channel, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "channel": _channel_row(node)})
        return 0
    print(f"通道 {node.name}（{node.kind}）")
    print(f"  状态：{'启用' if node.enabled else '停用'}｜默认={node.is_default}")
    print(f"  上次成功：{stamp(node.last_ok_at) if node.last_ok_at else '–'}")
    print(f"  上次失败：{node.last_error or '–'}")
    print("  配置（值已掩码）：")
    for key in sorted(node.config):
        print(f"    {key:<12} {node.config[key]}")
    return 0


def remove_channel(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        node = container.notify.show(args.channel, user_id=user.id)
        if not args.yes:
            raise ValidationError(
                f"删除通道 {node.name} 需要 --yes：凭据只有那一份密文，删掉就要重新录入"
            )
        container.notify.remove(node.id, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "removed": node.id})
    else:
        print(f"已删除通道 {node.name}")
    return 0


# ---------- 启停与默认 ----------


def enable(container: Container, args) -> int:
    return _flip(container, args, True)


def disable(container: Container, args) -> int:
    return _flip(container, args, False)


def _flip(container: Container, args, on: bool) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        node = container.notify.show(args.channel, user_id=user.id)
        container.notify.set_enabled(node.id, on, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "channel": node.id, "enabled": on})
    else:
        print(f"{'已启用' if on else '已停用'}通道 {node.name}")
        if not on:
            print(
                "  注意：停用的通道仍在规则里被引用（`rules.channel_ids`），"
                "那条规则命中时会写成一条 no_channel 记录"
            )
    return 0


def default(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        node = container.notify.show(args.channel, user_id=user.id)
        container.notify.set_default(node.id, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "channel": node.id, "is_default": True})
    else:
        print(f"默认通道已设为 {node.name}（原默认仍在启用，只是不再是兜底）")
    return 0


# ---------- 探针 ----------


def probe(container: Container, args) -> int:
    """退出码恒 0，即使探针失败：探针是在问一个问题，不是在承诺一个答案。

    与 `sams source probe` 同一个约定——把它做成非零，脚本里「配完通道顺手试一次」
    就会在探针报「远程终端没有桌面」时整条中断，而那句话本来就是要显示的答复本身。
    """
    try:
        user = resolve_actor(container.users, args.user)
        node = container.notify.show(args.channel, user_id=user.id)
        report = container.notify.probe(node.id, user_id=user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "probe": {
                    "channel": node.id,
                    "name": node.name,
                    "kind": report.kind,
                    "outcome": report.outcome,
                    "error": report.error,
                    "attempts": report.attempts,
                },
            }
        )
    else:
        head = f"探针 → {node.name}（{report.kind}）："
        if report.outcome == SENT:
            print(f"{head}已送达。规则里选这条通道就能收到真实告警")
        elif report.outcome == SUPPRESSED:
            print(f"{head}本机发不出去（不算失败）：{report.error}")
        else:
            print(f"{head}没送出去：{report.error}")
    return 0


# ---------- 免打扰 ----------


def dnd_set(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        container.notify.set_dnd(
            user.id,
            start=_time_arg(args.start, "--start"),
            end=_time_arg(args.end, "--end"),
            tz=(args.tz or None),
        )
        node = container.users.get(user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "dnd": _dnd(node)})
    else:
        print(f"免打扰窗口：{node.dnd_start:%H:%M}–{node.dnd_end:%H:%M}（{node.dnd_tz}）")
        print("  strong 级不受挡（止损在深夜也必须响）；mid/weak 会被记成一条压制记录")
        print("  跨午夜是合法的：23:00–07:00 就是这条功能最主要的那个窗口")
    return 0


def dnd_show(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "dnd": _dnd(user)})
    else:
        print(_dnd_text(user))
    return 0


def dnd_clear(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        container.notify.clear_dnd(user.id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "dnd": {"start": None, "end": None, "tz": None}})
    else:
        print(f"已清除 {user.username} 的免打扰窗口（三个字段一起清，不留残留）")
    return 0


def _dnd(user) -> dict:
    return {
        "start": None if user.dnd_start is None else f"{user.dnd_start:%H:%M}",
        "end": None if user.dnd_end is None else f"{user.dnd_end:%H:%M}",
        "tz": user.dnd_tz,
    }


def _dnd_text(user) -> str:
    if user.dnd_start is None:
        return f"{user.username} 没有免打扰窗口：任何时刻都会发"
    return (
        f"{user.username} 的窗口：{user.dnd_start:%H:%M}–{user.dnd_end:%H:%M}"
        f"（{user.dnd_tz}）——strong 不受挡"
    )
