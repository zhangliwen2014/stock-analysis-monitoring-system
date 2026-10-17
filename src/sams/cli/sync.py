"""`sams sync` —— 手动补数：日线与公司行为。

两个子命令走的是两条独立的能力链（D38）：`daily` 只走有日线的源，`actions` 只走能给
除权记录的源。把它们合成一个 `sams sync` 靠参数区分，等于让「今天的日线拿到了」这件事
替「这只票的除权记录齐了」背书——后者才是区间收益能不能信的依据。
"""

from __future__ import annotations

import argparse
from datetime import date

from sams.bootstrap import Container
from sams.cli.common import group, iso_date

#: 除权记录的默认起点：沪深交易所 1990/1991 年才成立，再早不存在可查的 A 股除权日
DEFAULT_ACTIONS_START = date(1990, 1, 1)


def add_to(subparsers) -> None:
    actions = group(
        subparsers,
        "sync",
        "采集入库（可重复执行，行数不变）：daily=日线，actions=分红送配/拆合股",
    )
    daily = actions.add_parser("daily", help="日线")
    daily.add_argument("--market", required=True, help="A / HK / US")
    daily.add_argument("--code", required=True, action="append", help="可多次传入")
    daily.add_argument("--start", required=True, help="YYYY-MM-DD")
    daily.add_argument("--end", required=True, help="YYYY-MM-DD")
    daily.set_defaults(handler=run_daily)

    acts = actions.add_parser("actions", help="公司行为（除权除息、送转配、拆合股）")
    acts.add_argument("--market", required=True, help="A / HK / US")
    acts.add_argument("--code", required=True, action="append", help="可多次传入")
    acts.add_argument("--start", help=f"YYYY-MM-DD，默认 {DEFAULT_ACTIONS_START}")
    acts.add_argument("--end", help="YYYY-MM-DD，默认今天")
    acts.set_defaults(handler=run_actions)


def run_daily(container: Container, args: argparse.Namespace) -> int:
    start, end = iso_date(args.start), iso_date(args.end)
    for code in args.code:
        summary = container.sync.sync_daily(args.market, code, start, end)
        if summary.empty:
            print(f"{code:<10} 所有源均无 {start}~{end} 的数据")
            continue
        line = (
            f"{code:<10} 源={summary.source} 取回={summary.fetched} 入库={summary.written} "
            f"最新={summary.latest_date}"
        )
        if summary.degraded:
            line += f" [降级: {_trail(summary.attempts)}]"
        print(line)
    return 0


def run_actions(container: Container, args: argparse.Namespace) -> int:
    start = iso_date(args.start) if args.start else DEFAULT_ACTIONS_START
    end = iso_date(args.end) if args.end else container.clock.today()
    for code in args.code:
        summary = container.corp_action_sync.sync_actions(args.market, code, start, end)
        line = (
            f"{code:<10} 源={summary.source} 取回={summary.fetched} 入库={summary.written} "
            f"确认至={summary.covered_to}"
            f"{'（全历史：取回与入库不受请求窗口约束）' if summary.full_history else ''}"
        )
        if summary.clamped_to is not None:
            line += f" [请求终点在未来，按今天截断至 {summary.clamped_to}]"
        if summary.degraded:
            line += f" [降级: {_trail(summary.attempts)}]"
        print(line)
        # 跳过的是「这个源不给除权记录」，不是「这个源没数据」：两者在报告上必须分开
        for name, reason in summary.skipped:
            print(f"           跳过 {name}：{reason}")
    return 0


def _trail(attempts) -> str:
    return " → ".join(f"{a.source}{'✓' if a.ok else '✗'}" for a in attempts)
