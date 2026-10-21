"""`sams rule ...` —— 十四个模板的维护面，加三条同步出口（§10、§9.2、§9.3）。

`check` 与 `recheck` 都是**同步一趟**：M3-D 的调度器将来调的就是这里这两个 handler。它们
的退出码判的是「这一趟有没有说清楚」，不是「有没有告警」——`check` 看 `blocked`/`skipped`
是否非空，`recheck` 看 `PARTIAL`/`NO_SCORE` 是否非空，各自算各自的 `code`、屏幕上各说一句。
把「有告警」做成非零，调度器就会把每一次正常命中当成一次故障去重试，而防刷屏是生死线（§10.1）。
别的地方不得再为这两条同步出口立第三种说法。

这一整个模块不许 import `sams.storage.*`：`tests/test_layering.py` 里 `ALLOWED["cli"]`
只有 `core` 与 `services`。库里的对象一律走 `Container` 上那些装配好的字段。
"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container
from sams.cli.common import emit, exit_for, group, iso_date, refused, stamp
from sams.core.errors import SamsError, ValidationError
from sams.core.rules import FILTER_KEYS, RULE_KINDS, TARGET_TYPES, template_for
from sams.services.actors import resolve_actor
from sams.services.rules.derive import DeriveReport
from sams.services.rules.engine import RunReport
from sams.services.rules.recheck import NO_SCORE, PARTIAL, STATE_NAMES, RecheckReport

#: `portfolio` 进不了库（U28：组合级指标这一轮没有输入），屏幕上也不列它
_STORABLE_TARGETS = tuple(t for t in TARGET_TYPES if t != "portfolio")
#: 这两个类型的 `target_ref` 只能是 `*`，屏幕上要说清，否则用户会往这里填一个代码
_WILDCARD_TARGETS = ("watchlist", "position")
#: 回写论上那一列时的中文列名：这句不许拼死在文案里，否则改了 `WRITE_BACK` 屏幕会开始说谎
_COLUMN_LABELS = {"stop_price": "止损价", "entry_high": "买入区上沿"}


def add_to(subparsers) -> None:
    actions = group(subparsers, "rule", "预警规则：模板、增删改、启停、一次求值、一次复检")

    def leaf(name: str, handler, help_text: str, *, needs_user: bool = True):
        parser = actions.add_parser(name, help=help_text)
        if needs_user:
            parser.add_argument("--user", required=True, help="归属的用户名（不是 id）")
        parser.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
        parser.set_defaults(handler=handler)
        return parser

    kinds = leaf(
        "kinds",
        run_kinds,
        "列出十四个模板要什么参数、能挂什么对象、有哪些过滤器（不读库）",
        needs_user=False,
    )
    kinds.add_argument("--kind", help="只看一种")

    add = leaf("add", run_add, "建一条规则（参数与过滤器的校验在仓库层，那里是唯一的口径）")
    add.add_argument("--kind", required=True, help=f"十四个之一：{'/'.join(RULE_KINDS)}")
    add.add_argument("--name", help="不填就用模板那句中文标签")
    add.add_argument("--target-type", required=True, help=f"可挂：{'/'.join(_STORABLE_TARGETS)}")
    add.add_argument("--target-ref", required=True, help=_targets_help())
    add.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="可多次；键名见 `sams rule kinds`",
    )
    add.add_argument(
        "--filter", action="append", default=[], metavar="NAME", help="可多次：给了就是开那个开关"
    )
    add.add_argument("--severity", help="weak/mid/strong；默认按模板")
    add.add_argument(
        "--channel", action="append", default=[], help="只投这几条通道；默认走级别路由"
    )
    add.add_argument("--account", help="持仓类必填：去重键要按账户分开（§10.1）")
    add.add_argument("--cooldown", type=int, help="冷却分钟；默认取 settings.cooldown_min")
    add.add_argument("--off", action="store_true", help="建好就停用：先看形状，先不上线")

    listing = leaf("list", run_list, "列出名下规则：模板、挂在谁身上、启停、现在响不响得起")
    listing.add_argument("--enabled-only", action="store_true", help="只看启用的那些")

    show = leaf("show", run_show, "单看一条：参数、过滤器、目标，外加它现在响不起来的理由")
    show.add_argument("rule_id", help="规则 id（`sams rule list` 的第一列）")

    edit = leaf(
        "edit", run_edit, "改一条：`--param` 只覆盖点名的键；改派生规则的价会回写到论上（§9.2）"
    )
    edit.add_argument("rule_id")
    edit.add_argument("--name")
    edit.add_argument("--kind", help="换模板：必须同时给新的 `--param`")
    edit.add_argument("--param", action="append", default=[], metavar="K=V")
    edit.add_argument("--filter", action="append", default=[], metavar="NAME")
    edit.add_argument("--severity")
    edit.add_argument(
        "--channel", action="append", default=[], help="整份替换；一个都不给就是回到级别路由"
    )
    edit.add_argument("--cooldown", type=int)
    edit.add_argument("--target-type")
    edit.add_argument("--target-ref")

    for name, handler, help_text in (
        ("enable", run_enable, "启用：下一趟 `rule check` 就会取用它"),
        ("disable", run_disable, "停用：派生规则只能停，不能删（§9.2）"),
    ):
        flip = leaf(name, handler, help_text)
        flip.add_argument("rule_id")

    remove = leaf(
        "remove", run_remove, "删除（不可撤销：修订史跟着一起没，想留痕用 `rule disable`）"
    )
    remove.add_argument("rule_id")
    remove.add_argument("--yes", action="store_true", help="确认删除")

    derive = leaf("derive", run_derive, "按 §9.2 从一条论派生那几条规则（幂等：已有的不重建）")
    derive.add_argument("thesis_id")
    derive.add_argument("--account", help="给了才建分批止盈那条：它要读这一笔的成本")

    check = leaf(
        "check", run_check, "同步跑一趟求值：命中就写告警并投递（调度器接的是同一个 handler）"
    )
    _as_of_arg(check)
    check.add_argument("--dry-run", action="store_true", help="判，但不写告警、不投递")
    check.add_argument("--rule", help="只跑这一条：停用中的规则被点名时也会跑")

    recheck = leaf(
        "recheck", run_recheck, "§9.3 周复检：第一次建基线，之后比基线（掉档只提示，不替你平仓）"
    )
    _as_of_arg(recheck)
    recheck.add_argument("--dry-run", action="store_true", help="比，但不写基线、不发事件")
    recheck.add_argument("--thesis", help="只复检这一条论")


# ---------- 参数形状 ----------


def _targets_help() -> str:
    return (
        "symbol 填 A/601088；thesis 填论的 id；"
        f"{'/'.join(_WILDCARD_TARGETS)} 只能填 *（整份清单，不是代码）"
    )


def _as_of_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--as-of", help="按哪一天判（YYYY-MM-DD，默认今天）：重算判断，不重放事件流"
    )


def _params(pairs: list[str]) -> dict[str, object] | None:
    """形状错在这里拒（没有 `=` 是命令行敲错）；值一律原样交下去，`validate_params` 是唯一的口径。

    这里再折一次 `float()` 就是第二处口径，而第二处的失效方式是「命令放行、仓库拒收」。
    """
    out: dict[str, object] = {}
    for item in pairs:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValidationError(f"--param 要的是 k=v，收到 {item!r}（值原样写，不做换算）")
        out[key.strip()] = value.strip()
    return out or None


def _filters(names: list[str]) -> dict[str, object] | None:
    if not names:
        return None
    return {name.strip(): True for name in names if name.strip()}


def _as_of(args) -> object | None:
    return None if args.as_of is None else iso_date(args.as_of)


def _iso(value) -> str:
    return "" if value is None else str(value)


# ---------- kinds ----------


def _kind_payload(template) -> dict:
    return {
        "kind": template.kind,
        "label": template.label,
        "help": template.describe(),
        "severity": template.severity,
        "stages": template.stages,
        "needs_position": template.needs_position,
        "needs_thesis": template.needs_thesis,
        "price_class": template.price_class,
        "targets": list(template.targets),
        "params": [p.describe() for p in template.params],
        "required": [p.name for p in template.params if p.required],
        "defaults": {p.name: p.default for p in template.params if not p.required},
    }


def run_kinds(container: Container, args) -> int:
    if args.kind is not None:
        try:
            template = template_for(args.kind)
        except SamsError as exc:
            return refused(args, exc, exit_for(exc))
        if args.json:
            emit({"status": "ok", "exit": 0, "kind": _kind_payload(template)})
        else:
            print(template.describe())
        return 0
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "kinds": [_kind_payload(template_for(kind)) for kind in RULE_KINDS],
                "targets": list(_STORABLE_TARGETS),
                "filters": list(FILTER_KEYS),
            }
        )
        return 0
    print("规则模板（名单的唯一出处是 `core/rules.py`，加一种就是那里加一行）：")
    for kind in RULE_KINDS:
        print(f"  {template_for(kind).describe()}")
    print(f"可挂对象：{'/'.join(_STORABLE_TARGETS)}")
    print(f"  {_targets_help()}")
    print("  持仓类要带 --account：同一去重键按账户分开，否则 A 报过 B 会被当重复吞掉")
    print(f"过滤器：{'/'.join(FILTER_KEYS)}（默认全关；`market_rel` 这一层没有输入，记 U27）")
    return 0


# ---------- add / list / show ----------


def _rule_payload(view) -> dict:
    rule = view.rule
    return {
        "id": rule.id,
        "name": rule.name,
        "kind": rule.kind,
        "target_type": rule.target_type,
        "target_ref": rule.target_ref,
        "account_id": rule.account_id,
        "thesis_id": rule.thesis_id,
        "params": dict(rule.params),
        # 0017 那一列：除权换算的锚。看不见它的地方，就等于没有它
        "params_on": None if rule.params_on is None else str(rule.params_on),
        "filters": dict(rule.filters),
        "severity": rule.severity,
        "cooldown_min": rule.cooldown_min,
        "enabled": rule.enabled,
        "channel_ids": list(rule.channel_ids),
        "created_at": _iso(rule.created_at),
        "updated_at": _iso(rule.updated_at),
        "targets": view.targets,
        "notes": list(view.notes),
    }


def _params_line(rule) -> str:
    """把参数按模板的中文标签念一遍：念不出的键（改了 kind 留下的旧键）要看得见。"""
    named = {p.name: p.label for p in template_for(rule.kind).params}
    parts = []
    for key, value in sorted(rule.params.items()):
        label = named.get(key, key)
        parts.append(f"{label}={value:g}" if isinstance(value, float) else f"{label}={value}")
    return "；".join(parts) if parts else "无参数"


def _anchor_line(rule) -> str:
    """那一屏参数属于哪一天的价空间——除权换算的锚（0017）。

    没有日期戳的那一版把下一步写在那句话里（D55）：这一行什么时候变成「不判」，
    取决于库里哪一天出现一次除权，用户看见那一句的时候再去找命令就晚了。
    留空的来源不止「0017 之前建的那一行」：从一条手填价位的论派生出来的规则照样是 NULL
    （那一头没有测量日），所以这句话只陈述「不知道属于哪天」，不猜它是哪条路留下的。
    """
    if rule.params_on is None:
        return (
            "无日期戳：这一屏的数不知道属于哪一天的价空间，"
            "库里一旦有除权就整条不判——跑一次 `sams rule edit` 重填它就盖上"
        )
    # 不说「除权日一定按它换算」：`anchor.py` 只搬那一屏里真存在的金额参数，
    # 老模板写下的一行可能根本没有那个键
    return f"{rule.params_on} 写下 · 除权换算的锚"


def _filters_line(rule) -> str:
    on = [key for key, value in rule.filters.items() if value]
    return f"开：{'、'.join(on)}" if on else "全关（默认不降噪）"


def _print_view(view, username: str) -> None:
    rule = view.rule
    flags = "启用" if rule.enabled else "已停用"
    print(f"{rule.id}  {rule.name}")
    print(f"  模板  {rule.kind}（{template_for(rule.kind).label}）  {flags}")
    print(f"  挂    {rule.target_type}:{rule.target_ref}  对象 {view.targets}  "
          f"级别 {rule.severity}  冷却 {rule.cooldown_min} 分钟")
    if rule.account_id:
        print(f"  账户  {rule.account_id}")
    if rule.thesis_id:
        print(f"  论    {rule.thesis_id}（改这条的价位会回写到论上）")
    if rule.channel_ids:
        print(f"  通道  {'、'.join(rule.channel_ids)}")
    print(f"  参数  {_params_line(rule)}")
    if template_for(rule.kind).money_params():
        print(f"  锚    {_anchor_line(rule)}")
    print(f"  过滤  {_filters_line(rule)}")
    for note in view.notes:
        print(f"  注意  {note}")
    print(f"  跑    sams rule check --user {username}"
          + (f" --rule {rule.id}" if not rule.enabled else ""))


def run_add(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        template = template_for(args.kind)
        view = container.rule_service.add(
            user.id,
            kind=args.kind,
            name=args.name or template.label,
            target_type=args.target_type,
            target_ref=args.target_ref,
            params=_params(args.param),
            filters=_filters(args.filter),
            account_id=args.account,
            severity=args.severity,
            channel_ids=tuple(args.channel),
            cooldown_min=args.cooldown,
            enabled=not args.off,
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "rule": _rule_payload(view)})
    else:
        _print_view(view, user.username)
    return 0


def run_list(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        views = container.rule_service.list(user.id, enabled_only=args.enabled_only)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "user": user.username,
                "rules": [_rule_payload(view) for view in views],
            }
        )
        return 0
    if not views:
        print(
            f"{user.username} 名下没有规则：两条路——"
            f"`sams rule derive <论 id> --user {user.username}` 按 §9.2 派生一套，"
            f"或 `sams rule add --user {user.username} --kind stop.fixed …` 手建一条"
        )
        return 0
    print(f"{user.username} 名下 {len(views)} 条规则")
    for view in views:
        rule = view.rule
        flags = "启用" if rule.enabled else "停用"
        line = (
            f"  {rule.id}  {rule.name:<20} {rule.kind:<22} {flags:<4} "
            f"{rule.target_type}:{rule.target_ref:<14} 对象 {view.targets}"
        )
        print(line)
        if view.notes:
            print(f"        {view.notes[0]}")
    return 0


def run_show(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        view = container.rule_service.show(user.id, args.rule_id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "rule": _rule_payload(view)})
    else:
        _print_view(view, user.username)
    return 0


# ---------- edit ----------


def run_edit(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        given = _params(args.param)
        wanted = _filters(args.filter)
        if args.kind is None:
            # 仓库那头的 `update` 是整包替换（它必须这样，否则改 kind 时旧键会留在库里），
            # 而「把 60 天改成 90 天」不该让 max_gain_pct 悄悄回到默认值。现值只在要合的时候
            # 才读：改了 kind 就不合（旧键多半对新 kind 是野键），那一次库读也就没必要。
            now = container.rule_service.show(user.id, args.rule_id)
            given = {**now.rule.params, **given} if given else None
            wanted = {**now.rule.filters, **wanted} if wanted else None
        report = container.rule_service.edit(
            user.id,
            args.rule_id,
            name=args.name,
            kind=args.kind,
            params=given,
            filters=wanted,
            severity=args.severity,
            channel_ids=tuple(args.channel) if args.channel else None,
            cooldown_min=args.cooldown,
            target_type=args.target_type,
            target_ref=args.target_ref,
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    view = container.rule_service.view(report.rule)
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "rule": _rule_payload(view),
                "written_back": dict(report.written_back),
                "thesis_id": report.thesis_id,
            }
        )
        return 0
    _print_view(view, user.username)
    for column, value in report.written_back.items():
        label = _COLUMN_LABELS.get(column, column)
        print(f"  回写    {label} → {value:g}（改这条的价与改论上是同一个数）")
    return 0


# ---------- 启停与删除 ----------


def run_enable(container: Container, args) -> int:
    return _flip(container, args, True)


def run_disable(container: Container, args) -> int:
    return _flip(container, args, False)


def _flip(container: Container, args, enabled: bool) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        view = container.rule_service.set_enabled(user.id, args.rule_id, enabled)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "rule": _rule_payload(view)})
        return 0
    _print_view(view, user.username)
    return 0


def run_remove(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        view = container.rule_service.show(user.id, args.rule_id)
        if not args.yes:
            raise ValidationError(
                f"删除 {view.rule.name} 需要 --yes：修订史跟着一起没，"
                "此后没人能回答「这条线曾经在哪」。只想让它别响就用 `sams rule disable`"
            )
        container.rule_service.remove(user.id, args.rule_id)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, "removed": view.rule.id})
    else:
        print(f"已删除规则 {view.rule.name}（{view.rule.kind}）")
    return 0


# ---------- derive ----------


def _derive_payload(report: DeriveReport) -> dict:
    return {
        "thesis_id": report.thesis_id,
        "created": [
            {"id": rule.id, "name": rule.name, "kind": rule.kind} for rule in report.created
        ],
        "already": list(report.already),
        "skipped": [{"kind": item.kind, "reason": item.reason} for item in report.skipped],
    }


def run_derive(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        report = container.rule_deriver.derive(
            user.id, args.thesis_id, account_id=args.account
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit({"status": "ok", "exit": 0, **_derive_payload(report)})
        return 0
    print(
        f"论 {report.thesis_id} 的派生规则：新建 {len(report.created)} 条、"
        f"没建 {len(report.skipped)} 条、已有 {len(report.already)} 条"
    )
    for rule in report.created:
        print(f"  {rule.id}  {rule.name:<24} {rule.kind}")
    for kind in report.already:
        print(f"  已有 {kind}：幂等，不重建")
    for item in report.skipped:
        print(f"  没建 {item.kind}：{item.reason}")
    print(f"  下一步：sams rule check --user {user.username}")
    return 0


# ---------- check ----------


def _raised_payload(hit) -> dict:
    return {
        "alert_id": hit.alert_id,
        "rule_id": hit.rule_id,
        "rule_name": hit.rule_name,
        "market": hit.market,
        "code": hit.code,
        "name": hit.name,
        "account_id": hit.account_id,
        "stage": hit.stage,
        "severity": hit.severity,
        "value": hit.value_text,
        "threshold": hit.threshold_text,
        "action": hit.action_note,
    }


def _check_payload(report: RunReport, username: str, code: int) -> dict:
    return {
        "status": "ok" if code == 0 else "incomplete",
        "exit": code,
        "user": username,
        "as_of": report.as_of.isoformat(),
        "now": _iso(report.now),
        "dry_run": report.dry_run,
        "rules": report.rules,
        "judged": report.judged,
        "quiet": report.quiet,
        "raised": [_raised_payload(hit) for hit in report.raised],
        "suppressed": [
            {
                "rule_id": item.rule_id,
                "rule_name": item.rule_name,
                "market": item.market,
                "code": item.code,
                "stage": item.stage,
                "reason": item.reason,
            }
            for item in report.suppressed
        ],
        "skipped": [
            {
                "rule_id": item.rule_id,
                "rule_name": item.rule_name,
                "market": item.market,
                "code": item.code,
                "reason": item.reason,
            }
            for item in report.skipped
        ],
        "blocked": [
            {"rule_id": item.rule_id, "rule_name": item.rule_name, "reason": item.reason}
            for item in report.blocked
        ],
    }


def run_check(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        if args.rule is not None:
            # 引擎那一头 `_select` 只会回空列表，屏幕上就成了「跑了 0 条规则」——
            # 点名一条不存在的人要听到「不存在」
            container.rule_service.show(user.id, args.rule)
        report = container.rule_engine.check(
            user.id,
            as_of=_as_of(args),
            dry_run=args.dry_run,
            rule_id=args.rule,
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    code = 1 if report.blocked or report.skipped else 0
    if args.json:
        emit(_check_payload(report, user.username, code))
    else:
        _print_check(container, report, user, code)
    return code


def _print_check(container: Container, report: RunReport, user, code: int) -> None:
    head = (
        f"规则 {report.rules} 条｜判了 {report.judged} 个对象｜没到那一步 {report.quiet} 个｜"
        f"发出 {len(report.raised)} 次"
    )
    print(head)
    print(f"  按 {report.as_of.isoformat()} 那天的数据判，跑在 {stamp(report.now)}")
    if report.dry_run:
        print("  试运行：一条告警都没写，也没投递")
    for hit in report.raised:
        print(f"  命中  {hit.name}（{hit.market}/{hit.code}）{hit.stage}｜{hit.value_text}")
        print(f"        {hit.action_note}")
        print(f"        sams alert show {hit.alert_id} --user {user.username}")
    for item in report.suppressed:
        print(f"  压住  {item.rule_name}（{item.market}/{item.code}，{item.stage}）：{item.reason}")
    for item in report.skipped:
        print(f"  判不了  {item.rule_name}（{item.market}/{item.code}）：{item.reason}")
    for item in report.blocked:
        print(f"  这条规则没跑  {item.rule_name}：{item.reason}")
    if report.rules == 0:
        print(f"  名下没有可用的规则：`sams rule add --user {user.username} …` 或 `rule derive`")
    if code:
        print("  这一趟没说完：上面那几行就是没说清的部分（退出码 1）")
    _channel_note(container, user, report)


def _channel_note(container: Container, user, report: RunReport) -> None:
    """命中了但出不了这扇门，要在屏幕上说：`dispatch` 仍会写下 alerts 那一行。

    那一行是这条告警的身份（`sams alert show` 靠它），不是「已经送出去了」的收据。
    """
    if not report.raised or report.dry_run:
        return
    if container.channels_store.list_for(user.id):
        return
    print(
        f"  名下没有任何通道：这 {len(report.raised)} 次命中写进了库、但没有出发——"
        f"`sams channel add --user {user.username} --kind wecom --name 主群 …`"
        f"（每条的结果在 `sams alert show <id> --user {user.username}` 那一屏）"
    )


# ---------- recheck ----------


def _recheck_payload(report: RecheckReport, username: str, code: int) -> dict:
    counts = {state: report.count(state) for state in STATE_NAMES}
    return {
        "status": "ok" if code == 0 else "incomplete",
        "exit": code,
        "user": username,
        "as_of": report.as_of.isoformat(),
        "dry_run": report.dry_run,
        "counts": counts,
        "rows": [
            {
                "thesis_id": row.thesis_id,
                "market": row.market,
                "code": row.code,
                "name": row.name,
                "status": row.status,
                "state": row.state,
                "label": STATE_NAMES[row.state],
                "baseline_score": row.baseline_score,
                "score": row.score,
                "message": row.message,
                "written": row.written,
            }
            for row in report.rows
        ],
    }


def run_recheck(container: Container, args) -> int:
    try:
        user = resolve_actor(container.users, args.user)
        report = container.rule_rechecker.run(
            user.id,
            as_of=_as_of(args),
            dry_run=args.dry_run,
            thesis_id=args.thesis,
        )
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    code = 1 if report.count(PARTIAL) or report.count(NO_SCORE) else 0
    if args.json:
        emit(_recheck_payload(report, user.username, code))
    else:
        _print_recheck(report, user.username, code)
    return code


def _print_recheck(report: RecheckReport, username: str, code: int) -> None:
    if not report.rows:
        print(f"{username} 名下没有 opened/tracking 的论：复检比的是「建仓时那一眼」，"
              f"没有建仓就没有那一层（`sams thesis list --user {username} --status opened`）")
        return
    counts = {
        STATE_NAMES[state]: report.count(state) for state in STATE_NAMES if report.count(state)
    }
    head = f"复检 {len(report.rows)} 条论（按 {report.as_of.isoformat()}）："
    print(head + "｜".join(f"{name} {num}" for name, num in counts.items()))
    if report.dry_run:
        print("  试运行：没写基线，也没发 ThesisDrifted")
    for row in report.rows:
        base = "–" if row.baseline_score is None else f"{row.baseline_score:g}"
        now = "–" if row.score is None else f"{row.score:g}"
        print(f"  {row.code} {row.name}  {STATE_NAMES[row.state]}  基线 {base} → 现 {now}")
        print(f"        {row.message}")
    if report.drifted:
        print(f"  掉档 {len(report.drifted)} 条：只提示，不替你决定平仓（§9.3）")
    if code:
        print("  这一趟有比不成或比不全的（退出码 1）：那不等于「没漂移」")
