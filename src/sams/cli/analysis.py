"""`sams analysis` —— 过闸的只读分析。

在加这条命令之前，命令行能拿到的区间涨跌只有「自己把两根裸价相除」那一条路：
跨除权日时那是一个看起来完全正常的错数（601088 的 −3.81% vs 交易所的 +1.26%），
图上、表里、决策上都不留痕迹。这条命令是唯一能把 `analysis` 那三道闸请出来的入口。

退出码分三种，因为「没给数」与「给了但不是那个区间的数」是两件事：
0 是请求的那段；1 是拒绝出数（原因照打）；3 是数照给了、锚点却落在请求之外
且缺口里有开市日或日历判不了的日子——脚本据此决定要不要信这个数。
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from sams.bootstrap import Container
from sams.cli.common import group, iso_date
from sams.core.errors import NotAdjustable


def add_to(subparsers) -> None:
    analysis = group(
        subparsers,
        "analysis",
        "只读分析（复权口径）：数过不了守卫就不出，出了一定带口径",
    )
    ret = analysis.add_parser("return", help="区间涨跌：裸价与复权并列给出")
    ret.add_argument("--market", required=True, help="A / HK / US")
    ret.add_argument("--code", required=True, help="如 601088")
    ret.add_argument("--start", required=True, help="YYYY-MM-DD")
    ret.add_argument("--end", required=True, help="YYYY-MM-DD")
    ret.set_defaults(handler=run_return)


def run_return(container: Container, args: argparse.Namespace) -> int:
    start, end = iso_date(args.start), iso_date(args.end)
    try:
        result = container.analysis.interval_return(args.market, args.code, start, end)
    except NotAdjustable as exc:
        # 拒绝是一种答案，不是程序出错：文案里带着下一步该跑的命令
        print(f"拒绝出数：{exc}")
        return 1
    print(f"{result.market}/{result.code}  {result.start} ~ {result.end}")
    print(f"  复权  {_pct(result.adjusted)}   ← 采信这个（覆盖确认：{result.covered_by}）")
    print(f"  裸价  {_pct(result.raw)}   {_why_raw(result)}")
    print("  口径  现金到手不再投资：派息按交易所除权参考价折进因子（D34），不是「分红再投资」")
    if result.differs and _pct(result.raw) == _pct(result.adjusted):
        # 两位小数一样时，「采信复权 +2.44% / 裸价 +2.44%」这句话没有任何信息量，
        # 而差别恰好就在那被舍掉的一位上
        print(
            f"  ⚠ 两个数在两位小数下长得一样，这里给到四位小数："
            f"复权 {result.adjusted:+.4%}、裸价 {result.raw:+.4%}"
        )
    if (result.start, result.end) != (start, end):
        return _anchor_exit(container, result, start, end)
    return 0


def _pct(value: float) -> str:
    return f"{value:+.2%}"


def _why_raw(result) -> str:
    if result.differs:
        return "← 跨除权日时它是错数，只是看起来正常"
    if result.start_is_ex_date:
        return f"← 起点 {result.start} 本身就是除权日：效果已在起点价里，两个数必然相等"
    return "← 首尾之间没有除权日：两个数必然相等，但这不等于这只票没分过红"


def _anchor_exit(container: Container, result, start: date, end: date) -> int:
    """缺口那几天是不是开市日，问日历；日历答不了就承认答不了。

    「（周末、长假、停牌）」以前是无依据的猜测，而它猜的方向恰好把最危险的那一种
    缺口——开市日没采到——说成了人人都会遇到的常态。
    """
    kinds = _classify(container, result.market, _gap_days(start, result.start, result.end, end))
    print(f"  ⚠ 按 {result.start} 与 {result.end} 这两根出的数：{_gap_phrase(kinds)}")
    if kinds["trading"] or kinds["unknown"]:
        print("    ⇒ 这个数不是请求那个区间的数：脚本按退出码 3 处理")
        return 3
    return 0


def _gap_days(start: date, used_start: date, used_end: date, end: date) -> list[date]:
    """请求了、但两端 K 线没覆盖到的那些日历天。"""
    days = []
    cursor = start
    while cursor < used_start:
        days.append(cursor)
        cursor += timedelta(days=1)
    cursor = used_end + timedelta(days=1)
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _classify(container: Container, market: str, days: list[date]) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {"trading": [], "closed": [], "unknown": []}
    buckets = {True: "trading", False: "closed", None: "unknown"}
    for day in days:
        out[buckets[container.calendar.known_status(market, day)]].append(day)
    return out


def _gap_phrase(kinds: dict[str, list[date]]) -> str:
    total = sum(len(v) for v in kinds.values())
    if not kinds["trading"] and not kinds["unknown"]:
        return f"缺口 {total} 天都是休市日（交易日历）：请求的就是这段行情"
    bits = []
    if kinds["trading"]:
        bits.append(
            f"{len(kinds['trading'])} 个开市日没有 K 线（{_days(kinds['trading'])}）"
            "：那是没同步到，或当天停牌"
        )
    if kinds["unknown"]:
        bits.append(
            f"{len(kinds['unknown'])} 天交易日历没铺到、无法判断是否开市"
            "（先 `sams calendar refresh`）"
        )
    if kinds["closed"]:
        bits.append(f"{len(kinds['closed'])} 天休市")
    return f"缺口 {total} 天里 " + "；".join(bits)


def _days(days: list[date]) -> str:
    shown = "、".join(str(d) for d in days[:3])
    return shown + (f" 等 {len(days)} 天" if len(days) > 3 else "")
