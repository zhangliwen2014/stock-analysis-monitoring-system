"""`sams analysis` —— 过闸的只读分析。

在加这条命令之前，命令行能拿到的区间涨跌只有「自己把两根裸价相除」那一条路：
跨除权日时那是一个看起来完全正常的错数（601088 的 −3.81% vs 交易所的 +1.26%），
图上、表里、决策上都不留痕迹。这条命令是唯一能把 `analysis` 那三道闸请出来的入口。

拒绝出数时原因原样打印，并以 1 结尾：脚本不该把「没给数」当成「涨了 0」。
"""

from __future__ import annotations

import argparse

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
    print(f"  复权  {_pct(result.adjusted)}   ← 采信这个（{_confirmed_by(result)}）")
    print(f"  裸价  {_pct(result.raw)}   {_why_raw(result)}")
    if (result.start, result.end) != (start, end):
        print(
            f"  ⚠ 按 {result.start} 与 {result.end} 这两根出的数：请求的 {start}~{end} "
            "边上那几天没有 K 线（周末、长假、停牌）"
        )
    return 0


def _pct(value: float) -> str:
    return f"{value:+.2%}"


def _confirmed_by(result) -> str:
    return f"覆盖确认：{result.covered_by}" if result.covered_by else "无覆盖确认"


def _why_raw(result) -> str:
    if result.differs:
        return "← 跨除权日时它是错数，只是看起来正常"
    return "← 区间内没有除权日：两个数相等是巧合，不是「复权没影响」"
