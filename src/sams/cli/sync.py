"""`sams sync` —— 手动补日线。

常驻轮询在 M2/M3 落地；这一期只有显式补数，参数即区间。
"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container
from sams.cli.common import iso_date


def add_to(subparsers) -> None:
    parser = subparsers.add_parser("sync", help="采集日线入库（可重复执行，行数不变）")
    parser.add_argument("--market", required=True, help="A / HK / US")
    parser.add_argument("--code", required=True, action="append", help="可多次传入")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.set_defaults(handler=run)


def run(container: Container, args: argparse.Namespace) -> int:
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
            trail = " → ".join(f"{a.source}{'✓' if a.ok else '✗'}" for a in summary.attempts)
            line += f" [降级: {trail}]"
        print(line)
    return 0
