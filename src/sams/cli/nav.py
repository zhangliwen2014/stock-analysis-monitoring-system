"""`sams nav`：组合净值的重建入口（S4-D §6.3）。

净值不是「现在值多少」的一次快照，而是一段逐日重放：每一天的收盘持仓 × 收盘价、
现金余额、外部流入各算一遍，缺价或缺汇率的那一天整日 `complete=false`。盘后 `nav`
job 每晚重建最近 400 天；这个命令做的是同一件事，只是把起点交到人手里——补账之后、
改了一笔成交之后，不用等下一趟。

退出码沿用 D77：0 是这一段重建完了（写 0 天也算，账户还没开立），1 是账户不存在，
2 是参数本身不对。
"""

from __future__ import annotations

from datetime import timedelta

from sams.cli.common import emit, exit_for, group, iso_date, refused
from sams.core.errors import SamsError, ValidationError


def add_to(subparsers) -> None:
    """`sams nav <action>`：目前只有 `rebuild`（整段覆盖）。"""
    actions = group(subparsers, "nav", "组合净值：按账本逐日重放")
    _rebuild_parser(actions)


def _rebuild_parser(actions) -> None:
    add = actions.add_parser(
        "rebuild",
        help="重建最近 N 天的净值（整段覆盖）：缺价/缺汇率的那天 complete=false，nav 为空",
    )
    add.add_argument(
        "--account", required=True, help="账户 id（`sams ledger account list` 的 id 列）"
    )
    add.add_argument(
        "--days", type=int, default=400, help="重建多少天（默认 400，含 --as-of 当天）"
    )
    add.add_argument(
        "--as-of", help="区间终点（默认今天，UTC）；补历史时给一个过去的日期"
    )
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_rebuild)


def run_rebuild(container, args) -> int:
    try:
        if args.days <= 0:
            raise ValidationError(f"--days 要的是一个正整数，收到 {args.days}")
        end = iso_date(args.as_of) if args.as_of else container.clock.today()
        start = end - timedelta(days=args.days)
        written = container.nav.rebuild(args.account, start, end)
        series = container.nav.series(args.account, start, end)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))

    payload = {
        "status": "ok",
        "account": args.account,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_written": written,
        "complete_days": sum(1 for point in series.days if point.complete),
        "gap_days": series.gap_days,
        "currency": series.currency,
        "latest": series.latest.day.isoformat() if series.latest else None,
        "nav": series.latest.nav if series.latest else None,
        "twr": series.twr,
        "twr_start": series.twr_start.isoformat() if series.twr_start else None,
        "max_drawdown": series.max_drawdown,
        "split_warning": series.split_warning,
    }
    if args.json:
        emit(payload)
        return 0

    print(f"{start} → {end} 重建 {written} 天")
    if series.latest is None:
        print("区间里没有一天算得出来：缺价或缺汇率，先补数据再看")
        return 0
    line = f"最新完整日 {series.latest.day} 净值 {series.latest.nav:.2f} {series.currency}"
    if series.day_change is not None:
        line += f"（当日 {series.day_change:+.2f}"
        if series.day_change_pct is not None:
            line += f" / {series.day_change_pct:+.2%}"
        line += "，已扣外部流入）"
    print(line)
    if series.twr is not None and series.twr_start is not None:
        line = f"TWR {series.twr:+.2%}（自 {series.twr_start} 起"
        if series.gap_days:
            line += f"；区间缺口 {series.gap_days} 天，缺口前后各算各的"
        line += "）"
        print(line)
    if series.max_drawdown is not None:
        print(f"最大回撤 {series.max_drawdown:.2%}（按净值峰谷，不是按 TWR）")
    if series.gap_days:
        print(f"缺口 {series.gap_days} 天：缺价或缺汇率的那一天整日不计入 TWR")
    if series.split_warning:
        print(f"提示：{series.split_warning}")
    return 0
