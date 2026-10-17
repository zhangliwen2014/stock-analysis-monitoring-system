"""`sams calendar` —— 刷新与查询交易日历。"""

from __future__ import annotations

import argparse
from datetime import UTC

from sams.bootstrap import Container
from sams.cli.common import group, iso_date


def add_to(subparsers) -> None:
    actions = group(subparsers, "calendar", "交易日历")

    refresh = actions.add_parser("refresh", help="从来源拉取整年日历并入库")
    refresh.add_argument("--market", required=True)
    refresh.add_argument("--year", type=int, required=True)
    refresh.set_defaults(handler=refresh_year)

    check = actions.add_parser("check", help="某天是否交易日、时段（本地 / UTC）")
    check.add_argument("--market", required=True)
    check.add_argument("--date", required=True, help="YYYY-MM-DD")
    check.set_defaults(handler=check_day)


def refresh_year(container: Container, args: argparse.Namespace) -> int:
    written = container.calendar.refresh(args.market, args.year)
    print(f"{args.market} {args.year} 年写入 {written} 个交易日")
    return 0


def check_day(container: Container, args: argparse.Namespace) -> int:
    day = iso_date(args.date)
    info = container.calendar.day(args.market, day)
    if not info.is_trading:
        print(f"{args.market} {day} 休市（时区 {info.tz}）")
        return 0
    print(f"{args.market} {day} 交易日{'（半天）' if info.is_half_day else ''}")
    for session in container.calendar.sessions(args.market, day):
        # 同一个绝对时刻的两种写法：漏掉 astimezone(UTC) 就会把本地时间标成 UTC
        utc_open = session.open.astimezone(UTC)
        utc_close = session.close.astimezone(UTC)
        print(
            f"  {session.name:<8} 本地 {session.open.strftime('%H:%M')}–"
            f"{session.close.strftime('%H:%M')}   "
            f"UTC {utc_open.strftime('%m-%d %H:%M')}–{utc_close.strftime('%m-%d %H:%M')}"
        )
    return 0
