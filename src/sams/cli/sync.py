"""`sams sync` —— 手动补数：日线、公司行为，以及基本面三件套。

每个子命令走的是一条独立的能力链（D38）：`daily` 只走有日线的源，`actions` 只走能给
除权记录的源，`financials`/`valuation`/`flow` 各走能给基本面/按日估值/资金流的源。
把它们合成一个 `sams sync` 靠参数区分，等于让「今天的日线拿到了」这件事替
「这只票的除权记录齐了」背书——后者才是区间收益能不能信的依据。

三条基本面链的 0 行与日线的 0 行不是一件事（D43）：日线那段没数据是一次**成功的**
取数，基本面的 0 行只是「这几家都没这只票的记录」。所以这里的输出把「查无记录」与
「取回 0 条」分开写，并列出到底是哪几家说的「没有」。
"""

from __future__ import annotations

import argparse
from datetime import date

from sams.bootstrap import Container
from sams.cli.common import group, iso_date

#: 除权记录的默认起点：沪深交易所 1990/1991 年才成立，再早不存在可查的 A 股除权日
DEFAULT_ACTIONS_START = date(1990, 1, 1)
#: 基本面与估值的默认起点同上：这些接口的答复本来就是「这只票的全部历史」，
#: 窗口只用来切源返回的帧，把默认值编成「近五年」会让人以为更早的期数不存在
DEFAULT_FUNDAMENTALS_START = DEFAULT_ACTIONS_START

#: 命令行上的名字 → （服务方法名, 给人看的种类名）。`flow` 而不是 `funding_flow`：
#: 命令要短，而名字对不上服务层这件事由这张表一次性承担，不在各处猜
FUNDAMENTAL_KINDS = {
    "financials": ("sync_financials", "基本面"),
    "valuation": ("sync_valuation", "按日估值"),
    "flow": ("sync_flow", "资金流"),
}


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

    for name, (_, label) in FUNDAMENTAL_KINDS.items():
        parser = actions.add_parser(name, help=f"{label}（按各源能力路由）")
        parser.add_argument("--market", required=True, help="A / HK / US")
        parser.add_argument("--code", required=True, action="append", help="可多次传入")
        parser.add_argument("--start", help=f"YYYY-MM-DD，默认 {DEFAULT_FUNDAMENTALS_START}")
        parser.add_argument("--end", help="YYYY-MM-DD，默认今天")
        parser.set_defaults(handler=run_fundamental)


def run_fundamental(container: Container, args: argparse.Namespace) -> int:
    """三条基本面链共用的一条打印路径：种类由表里带出来，不在这里再判一次参数。"""
    method, label = FUNDAMENTAL_KINDS[args.action]
    start = iso_date(args.start) if args.start else DEFAULT_FUNDAMENTALS_START
    end = iso_date(args.end) if args.end else container.clock.today()
    for code in args.code:
        summary = getattr(container.fundamental_sync, method)(args.market, code, start, end)
        line = f"{code:<10} {label}  源={summary.source or '—'} 取回={summary.fetched}"
        if summary.empty:
            # 「这几家都说了没有」不是「取回 0 条」：前者的下一步是接受，后者的下一步是查源
            line += (
                f" 入库=0  链上每个有能力的源都说查无记录"
                f"（{'+'.join(summary.confirmed_empty_by)}）"
            )
        else:
            line += f" 入库={summary.written}"
        if summary.degraded:
            line += f" [降级: {_trail(summary.attempts)}]"
        print(line)
        for name, reason in summary.skipped:
            print(f"           跳过 {name}：{reason}")
    return 0


def run_daily(container: Container, args: argparse.Namespace) -> int:
    start, end = iso_date(args.start), iso_date(args.end)
    rc = 0
    for code in args.code:
        summary = container.sync.sync_daily(args.market, code, start, end)
        if summary.empty:
            # 「这只票这段没数据」与「问它的源都挂了」是两件事：前者的下一步是换区间，
            # 后者的下一步是修源。混成一行会让运维对着一个空库什么也不做
            failed = [a for a in summary.attempts if not a.ok]
            if failed:
                rc = 1
                print(
                    f"{code:<10} 无数据，但有 {len(failed)} 个源失败："
                    f"{'；'.join(f'{a.source}: {a.error}' for a in failed)}"
                )
                print(f"{'':<10} 不能断定这只票这段没有数据：先修上面的源，再重跑同一条命令")
            else:
                print(f"{code:<10} 所有源都回答「{start}~{end} 没有数据」（停牌、新股、区间过短）")
            continue
        line = (
            f"{code:<10} 源={summary.source} 取回={summary.fetched} 入库={summary.written} "
            f"最新={summary.latest_date}"
        )
        if summary.degraded:
            line += f" [降级: {_trail(summary.attempts)}]"
        print(line)
    return rc


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
        if summary.confirmed_empty_by:
            line += f" 确认查无={'+'.join(summary.confirmed_empty_by)}"
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
