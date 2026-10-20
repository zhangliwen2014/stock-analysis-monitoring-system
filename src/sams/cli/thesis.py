"""`sams thesis`：一条观点从立论到归档的那条链（spec §9.1，M2-E）。

命令层只干三件事：把 `--user` 后面那个用户名交给服务、把结论排成一屏、在每个出口都给出
`--json`（D57）。状态机与价位闸都在下面两层，这里一份都不重写——重写的那份会先失效，
而失效的样子是「命令放行了一个仓库会拒的跳转」或者反过来。

退出码沿用估值那一屏的规矩：0 是给出一条完整的结论，1 是没给出来（数据缺失、业务拒绝、
写了一半），2 是参数本身不对。区分 1 与 2 是因为脚本对两者的处置不同：前者补数据再来一次，
后者是命令行敲错了，重来多少次都一样。
"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container
from sams.cli.common import emit, exit_for, group, iso_date, ratio_arg, refused, stamp
from sams.core.errors import StorageError, ValidationError
from sams.core.fundamentals import ROE_BASIS, ROE_BASIS_ANNUAL
from sams.services.theses import STATUS_NAMES, ThesisReport
from sams.services.valuation import STYLE_NAMES

#: 每栏在屏幕上的列宽：数字对齐了，「库里那次」与「当前这次」才看得出是不是一样
_LABEL_WIDTH = 8


def add_to(subparsers) -> None:
    """`sams thesis <action>`：add / list / show / advance / revise。"""
    actions = group(subparsers, "thesis", "投资观点：立论、走完那条链、回看当时为什么买")
    _add_parser(actions)
    _list_parser(actions)
    _show_parser(actions)
    _advance_parser(actions)
    _revise_parser(actions)


def _add_parser(actions) -> None:
    add = actions.add_parser(
        "add",
        help="立一条观点：当场算一次估值，把算得出的买入区/止损/目标/赔率落成那几列",
    )
    add.add_argument("--user", required=True, help="归属的用户名（不是 id）")
    add.add_argument("--market", required=True, help="A / HK / US")
    add.add_argument("--code", required=True, help="如 601088")
    add.add_argument(
        "--style",
        default="value",
        help=f"打分模板，可选 {', '.join(STYLE_NAMES)}（默认 value）：这一列决定以后按哪套权重读",
    )
    add.add_argument(
        "--status",
        default="observing",
        help=f"开局状态，只能用 {'、'.join(STATUS_NAMES[:2])} 那两个（其余要走 `advance`）",
    )
    _as_of_arg(add)
    _caliber_args(add)
    add.add_argument("--note", help="写进第一条事件里的那句话：当时为什么立这条论")
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_add)


def _list_parser(actions) -> None:
    show = actions.add_parser("list", help="名下的 thesis：一行一条，带状态与那四个价位")
    show.add_argument("--user", required=True)
    show.add_argument("--status", help=f"只看这一个状态（{'、'.join(STATUS_NAMES)}）")
    show.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    show.set_defaults(handler=run_list)


def _show_parser(actions) -> None:
    show = actions.add_parser(
        "show",
        help="一条 thesis 的全貌：记录、时间线、当前那份估值，以及两栏价位怎么对上",
    )
    show.add_argument("thesis_id", help="`add`/`list` 屏幕上那个 id")
    show.add_argument("--user", required=True)
    _as_of_arg(show)
    _caliber_args(show)
    show.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    show.set_defaults(handler=run_show)


def _advance_parser(actions) -> None:
    move = actions.add_parser(
        "advance",
        help=(
            "沿那条链走一跳（"
            + "、".join(STATUS_NAMES)
            + "）：能走到哪由仓库那张跳转表决定，不是按这个顺序一路往下"
        ),
    )
    move.add_argument("thesis_id")
    move.add_argument("--user", required=True)
    move.add_argument(
        "--to",
        required=True,
        help=f"走到哪一个状态（{'、'.join(STATUS_NAMES)}）；能不能走由仓库那张表说了算",
    )
    move.add_argument("--note", help="这一跳的理由（进时间线，回看时用的就是它）")
    move.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    move.set_defaults(handler=run_advance)


def _revise_parser(actions) -> None:
    fix = actions.add_parser("revise", help="改那几列价位（不改状态，因此不留事件）")
    fix.add_argument("thesis_id")
    fix.add_argument("--user", required=True)
    fix.add_argument("--entry-low", help="买入区下限")
    fix.add_argument("--entry-high", help="买入区上限")
    fix.add_argument("--stop", help="止损价")
    fix.add_argument("--target", help="目标价")
    fix.add_argument("--odds", help="赔率：(目标-现价) ÷ (现价-止损)，要正的有限数")
    fix.add_argument(
        "--from-plan",
        action="store_true",
        help="按现在的库重算一次再落；只覆盖这条结论给得出的那几栏，算不出的原样留着",
    )
    _as_of_arg(fix)
    _caliber_args(fix)
    fix.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    fix.set_defaults(handler=run_revise)


def _as_of_arg(parser) -> None:
    parser.add_argument(
        "--as-of",
        help=(
            "YYYY-MM-DD，默认今天：现价、分位窗口、PIT 闸门、资金流窗口一起回到那一天"
            "（价位是从那一刻算出来的，所以这个参数决定落库的那几列）"
        ),
    )


def _caliber_args(parser) -> None:
    parser.add_argument(
        "--roe-basis",
        default=ROE_BASIS_ANNUAL,
        choices=list(ROE_BASIS),
        help=f"PR 分母用哪一期 ROE：annual=最新已披露**年报**（默认 {ROE_BASIS_ANNUAL}）",
    )
    parser.add_argument(
        "--roe-estimate",
        help="预估 ROE，小数口径（15%% 要写 0.15）：PE 不为正时那一支要用它",
    )


# --- 命令 ----------------------------------------------------------------------------------


def run_add(container: Container, args: argparse.Namespace) -> int:
    try:
        report = container.theses.add(
            args.user,
            args.market,
            args.code,
            style=args.style,
            status=args.status,
            note=args.note,
            as_of=iso_date(args.as_of) if args.as_of else None,
            roe_estimate=_ratio(args.roe_estimate),
            roe_basis=args.roe_basis,
        )
    except (ValidationError, StorageError) as exc:
        return refused(args, exc, exit_for(exc))
    code = 0 if report.complete else 1
    nxt = container.theses.next_steps(report.thesis.status)
    if args.json:
        emit(_report_payload(report, code, nxt))
        return code
    return _print_report(report, code, next_steps=nxt)


def run_list(container: Container, args: argparse.Namespace) -> int:
    try:
        rows = container.theses.list(args.user, status=args.status)
    except (ValidationError, StorageError) as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "user": args.user,
                "count": len(rows),
                "theses": [_thesis_payload(row) for row in rows],
            }
        )
        return 0
    if not rows:
        wanted = f"状态为 {args.status} 的" if args.status else "任何"
        print(f"{args.user} 名下没有{wanted} thesis：下一步 `sams thesis add --user {args.user} …`")
        return 0
    print(f"{args.user} 名下 {len(rows)} 条 thesis")
    for row in rows:
        print(
            f"  {row.market}/{row.code}  {row.status:<12} 模板 {row.style:<6}"
            f"  买入区 {_range(row.entry_low, row.entry_high)}"
            f"  止损 {_money(row.stop_price)}  目标 {_money(row.target_price)}"
            f"  赔率 {_money(row.reward_risk)}  id {row.id}"
        )
    return 0


def run_show(container: Container, args: argparse.Namespace) -> int:
    try:
        report = container.theses.show(
            args.user,
            args.thesis_id,
            as_of=iso_date(args.as_of) if args.as_of else None,
            roe_estimate=_ratio(args.roe_estimate),
            roe_basis=args.roe_basis,
        )
    except (ValidationError, StorageError) as exc:
        return refused(args, exc, exit_for(exc))
    code = 0 if report.complete else 1
    nxt = container.theses.next_steps(report.thesis.status)
    if args.json:
        emit(_report_payload(report, code, nxt))
        return code
    return _print_report(report, code, next_steps=nxt)


def run_advance(container: Container, args: argparse.Namespace) -> int:
    try:
        thesis = container.theses.advance(
            args.user, args.thesis_id, args.to, note=args.note
        )
    except (ValidationError, StorageError) as exc:
        return refused(args, exc, exit_for(exc))
    nxt = container.theses.next_steps(thesis.status)
    if args.json:
        emit(
            {
                "status": "ok",
                "exit": 0,
                "thesis": _thesis_payload(thesis),
                "next_steps": list(nxt),
            }
        )
        return 0
    print(f"{thesis.market}/{thesis.code}  →  {thesis.status}")
    print(
        "  接下来可走："
        + ("、".join(nxt) if nxt else "没有（`archived` 是终点，要再看这只票就另开一条）")
    )
    print(f"  下一步：sams thesis show {thesis.id} --user {args.user}")
    return 0


def run_revise(container: Container, args: argparse.Namespace) -> int:
    # 解析也在闸内（D57）：`--stop abc --json` 的报错得带着 JSON 出来，
    # 冒到 `run()` 的兜底通道只剩一段 stderr 和一个错的退出码
    try:
        levels = {
            "entry_low": _money_arg("--entry-low", args.entry_low),
            "entry_high": _money_arg("--entry-high", args.entry_high),
            "stop_price": _money_arg("--stop", args.stop),
            "target_price": _money_arg("--target", args.target),
            "reward_risk": _money_arg("--odds", args.odds),
        }
        given = {key: value for key, value in levels.items() if value is not None}
        if not given and not args.from_plan:
            raise ValidationError(
                "没有要改的栏：给一两个价位（`--stop/--target/--odds/--entry-low/--entry-high`）"
                "或说「按现在的库重算」（`--from-plan`）"
            )
        report = container.theses.revise(
            args.user,
            args.thesis_id,
            from_plan=args.from_plan,
            as_of=iso_date(args.as_of) if args.as_of else None,
            roe_estimate=_ratio(args.roe_estimate),
            roe_basis=args.roe_basis,
            **given,
        )
    except (ValidationError, StorageError) as exc:
        return refused(args, exc, exit_for(exc))
    # 要求重算而一栏都没落下来 ⇒ 这次没算出任何可写的数（退出码 1），不是「改好了」
    code = 1 if not report.complete or (args.from_plan and not report.written) else 0
    nxt = container.theses.next_steps(report.thesis.status)
    if args.json:
        emit(_report_payload(report, code, nxt))
        return code
    return _print_report(report, code, next_steps=nxt)


# --- 输出 ----------------------------------------------------------------------------------


def _print_report(report: ThesisReport, code: int, *, next_steps: tuple[str, ...]) -> int:
    thesis = report.thesis
    print(f"{thesis.market}/{thesis.code}  模板 {thesis.style}  状态 {thesis.status}")
    print(f"  id    {thesis.id}")
    print(
        f"  归属  {report.username}"
        f"  立论 {stamp(thesis.created_at)}  更新 {stamp(thesis.updated_at)}"
        + (f"  归档 {stamp(thesis.archived_at)}" if thesis.archived_at else "")
    )
    if not report.has_levels:
        print("  价位    空着（建仓那一步会被拒：止损/目标/赔率三列都得有数）")
    print(f"  ── 对照：{report.stored_axis} vs 当前这次 ──")
    for row in report.rows:
        print(
            f"    {row.label:<{_LABEL_WIDTH}}  库里 {_range(*row.stored):<16}"
            f"  当前 {_range(*row.current):<16}  {row.note}"
        )
    print(f"  现价    {_price_line(report)}")
    print(f"  {report.placement}")
    for note in report.notes:
        print(f"    · {note}")
    # 成交排在时间线之前：这一节是「那条链说的话有没有钱真的动过」的凭据，放在后面就成附注了
    print("  ── 成交（状态到了 ≠ 按那个价买过）──")
    print(f"    {report.fills_note}")
    for fill in report.fills:
        # `buy`/`sell` 用账本那一屏的原词：同一笔成交在两处显示成两种说法，
        # 读的人要先猜它们是不是同一件事
        print(f"      {stamp(fill.ts)}  {fill.side} {fill.qty:g} 股 @ {fill.price:g}")
    print("  ── 时间线 ──")
    for event in report.events:
        print(
            f"    {stamp(event.ts)}  {_jump(event.from_status, event.to_status)}"
            f"  {_who(event, thesis, report.username)}"
            + (f"  {event.note}" if event.note else "")
        )
    print(_next_line(thesis, report.username, next_steps))
    print(
        f"  每个分数的来源：sams analysis valuation --market {thesis.market}"
        f" --code {thesis.code} --style {thesis.style}"
    )
    if code == 1:
        print(
            "    ⇒ 这一屏没给出一条完整的结论（退出码 1）：`--json` 里 exit 同值，"
            "缺的那一栏带着自己的原因"
        )
    return code


def _jump(former: str | None, latter: str | None) -> str:
    """时间线里那一跳：起点为空的是「立论」那一条，不是从某个状态走过来的。"""
    return f"（立论）→ {latter}" if former is None else f"{former} → {latter}"


def _who(event, thesis, username: str) -> str:
    """时间线上那一行的主语：归属者本人报名字，别人（M3 的触发器）留着 id。

    库里存的是 `actor_id`，一屏 hex 没人读得动；而把非本人的 actor 也翻成 `username`
    更糟——那会把「谁走的这一步」说成「谁拥有这条记录」。
    """
    if not event.actor_id:
        return "–"
    return username if event.actor_id == thesis.user_id else event.actor_id


def _next_line(thesis, username: str, next_steps: tuple[str, ...]) -> str:
    """下一步要能当场敲通：整条命令带上 id 与 `--to`，不给「参见帮助」这种半成品。"""
    if not next_steps:
        return "  接下来可走：没有（`archived` 是终点，要再看这只票就另开一条）"
    lines = [
        f"    sams thesis advance {thesis.id} --user {username} --to {to}"
        for to in next_steps
    ]
    return "  接下来可走：\n" + "\n".join(lines)


def _price_line(report: ThesisReport) -> str:
    read = report.valuation
    if read is None:
        return f"无 ← 当前这次算不出：{report.unavailable}"
    age = "距今 unknown" if read.price_age_days is None else f"距今 {read.price_age_days} 天"
    score = "–" if not read.composite.ok else f"{read.composite.score:.1f}"
    pr = read.fundamental.pr
    pr_text = "–" if not pr.ok else f"{pr.value:.4f}"
    return (
        f"{read.price:g} ({read.price_on}，{age})  "
        f"合成分 {score}  PR {pr_text}  截至 {read.as_of}"
    )


def _report_payload(report: ThesisReport, code: int, next_steps: tuple[str, ...]) -> dict:
    return {
        "status": "ok" if code == 0 else "incomplete",
        "exit": code,
        "thesis": _thesis_payload(report.thesis),
        "events": [
            {
                "from": event.from_status,
                "to": event.to_status,
                "actor": event.actor_id,
                "note": event.note,
                "ts": event.ts.isoformat() if event.ts else None,
            }
            for event in report.events
        ],
        "comparison": [
            {
                "key": row.key,
                "label": row.label,
                "stored": list(row.stored),
                "current": list(row.current),
                "note": row.note,
            }
            for row in report.rows
        ],
        "placement": report.placement,
        "notes": list(report.notes),
        # 库里那几列属于哪一次读数（0013）：`stored_on` 是这句话的机器可读形态。
        # 一列价位都没落库时它是 null：说「没有测量日（手填）」是替一片空白编出处
        "stored_on": report.stored_on if report.has_levels else None,
        "written": list(report.written),
        # 两个键在有无成交时都出现（同 §5.4 规则 2）：靠「有没有这个键」判断买没买是错的，
        # 空列表与缺键在 JSON 里都读得通，而管道要的是一句「几笔成交」或那句「没有」
        "fills": [
            {
                "side": fill.side,
                "qty": fill.qty,
                "price": fill.price,
                "ts": fill.ts.isoformat() if fill.ts else None,
            }
            for fill in report.fills
        ],
        "fills_note": report.fills_note,
        "next_steps": list(next_steps),
        "unavailable": report.unavailable,
        "valuation": None if report.valuation is None else _valuation_payload(report),
    }


def _valuation_payload(report: ThesisReport) -> dict:
    """当前那一份的摘要：完整分项表与式子在 `sams analysis valuation --json` 里。"""
    read = report.valuation
    assert read is not None
    plan = read.plan
    return {
        "as_of": str(read.as_of),
        "price": read.price,
        "price_on": str(read.price_on) if read.price_on else None,
        "pr": read.fundamental.pr.value,
        "composite": {
            "ok": read.composite.ok,
            "score": read.composite.score,
            "covered_weight": read.composite.covered_weight,
            "missing": list(read.composite.missing),
            "reason": read.composite.reason,
        },
        "plan": {
            "ok": plan.ok,
            "fair": [plan.fair.low, plan.fair.high],
            "entry": [plan.entry.low, plan.entry.high],
            "stop": [plan.stop.low, plan.stop.high],
            "target": [plan.target.low, plan.target.high],
            "odds": plan.odds.value,
            "notes": list(plan.notes),
        },
    }


def _thesis_payload(thesis) -> dict:
    return {
        "id": thesis.id,
        "user_id": thesis.user_id,
        "market": thesis.market,
        "code": thesis.code,
        "style": thesis.style,
        "status": thesis.status,
        "entry_low": thesis.entry_low,
        "entry_high": thesis.entry_high,
        "stop_price": thesis.stop_price,
        "target_price": thesis.target_price,
        "reward_risk": thesis.reward_risk,
        "win_rate": thesis.win_rate,
        "win_rate_samples": thesis.win_rate_samples,
        # 那几列价位属于哪一天的读数；手填的数在这里是 null（0013）
        "levels_on": str(thesis.levels_on) if thesis.levels_on else None,
        "created_at": thesis.created_at.isoformat() if thesis.created_at else None,
        "updated_at": thesis.updated_at.isoformat() if thesis.updated_at else None,
        "archived_at": thesis.archived_at.isoformat() if thesis.archived_at else None,
    }


def _ratio(value: str | None) -> float | None:
    return None if value is None else ratio_arg("--roe-estimate", value)


def _money_arg(name: str, value: str | None) -> float | None:
    """价位类参数：这里只把「不是一个数」变成一句人话，正负与区间的顺序归仓库管（D66 同族）。"""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValidationError(f"{name} 要的是一个数，收到 {value!r}") from None


def _money(value: float | None) -> str:
    return "–" if value is None else f"{value:.4g}"


def _range(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return "–"
    if low == high:
        return _money(low)
    return f"{_money(low)}~{_money(high)}"
