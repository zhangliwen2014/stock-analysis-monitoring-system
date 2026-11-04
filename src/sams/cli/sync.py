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
from datetime import timedelta

from sams.bootstrap import Container
from sams.cli.common import currency_arg, emit, exit_for, group, iso_date, refused, stamp
from sams.core.errors import SamsError, ValidationError
from sams.core.timeutil import HISTORY_START

#: 除权记录的默认起点：与调度 job 共用同一个常量，免得两处各写一个 1990
DEFAULT_ACTIONS_START = HISTORY_START
#: 基本面与估值的默认起点同上：这些接口的答复本来就是「这只票的全部历史」，
#: 窗口只用来切源返回的帧，把默认值编成「近五年」会让人以为更早的期数不存在
DEFAULT_FUNDAMENTALS_START = DEFAULT_ACTIONS_START

#: 手动补公告时多留一些余量：盘后 job 每晚问最近 30 天，手敲这一条的人通常是在补
#: 更早的缺口。源一次最多给 100 条，这个窗口只裁剪那一页
DEFAULT_ANNOUNCEMENTS_DAYS = 365
#: 新闻与盘后 job 同一个窗口：搜索接口只给最近一页，拉更长的窗口也拿不到更早的
DEFAULT_NEWS_DAYS = 30

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
        if name == "financials":
            # 选股看全市场，而调度的那条只采用户并集：普查得有自己的入口（D227），
            # 否则补覆盖要往 `--code` 里塞几千次
            pool = parser.add_mutually_exclusive_group(required=True)
            pool.add_argument("--code", action="append", help="可多次传入")
            pool.add_argument(
                "--all",
                action="store_true",
                help="按代码表在市的全部票逐只同步（一次人工普查，不挂调度）",
            )
        else:
            parser.add_argument("--code", required=True, action="append", help="可多次传入")
        parser.add_argument("--start", help=f"YYYY-MM-DD，默认 {DEFAULT_FUNDAMENTALS_START}")
        parser.add_argument("--end", help="YYYY-MM-DD，默认今天")
        parser.set_defaults(handler=run_fundamental)

    profile = actions.add_parser(
        "profile", help="公司概况（一票一行，重复执行是覆盖，不吃窗口）"
    )
    profile.add_argument("--market", required=True, help="A / HK / US")
    profile.add_argument("--code", required=True, action="append", help="可多次传入")
    profile.set_defaults(handler=run_profile)

    announcements = actions.add_parser(
        "announcements", help="公告元数据（最近一页；正文与 PDF 只留外链）"
    )
    announcements.add_argument("--market", required=True, help="A / HK / US")
    announcements.add_argument("--code", required=True, action="append", help="可多次传入")
    announcements.add_argument(
        "--start", help=f"YYYY-MM-DD，默认 {DEFAULT_ANNOUNCEMENTS_DAYS} 天前"
    )
    announcements.add_argument("--end", help="YYYY-MM-DD，默认今天")
    announcements.set_defaults(handler=run_announcements)

    news = actions.add_parser(
        "news", help="个股新闻（最近一页；v0 只接 A 股，链接指向原文）"
    )
    news.add_argument("--market", required=True, help="A / HK / US")
    news.add_argument("--code", required=True, action="append", help="可多次传入")
    news.add_argument(
        "--start", help=f"YYYY-MM-DD，默认 {DEFAULT_NEWS_DAYS} 天前"
    )
    news.add_argument("--end", help="YYYY-MM-DD，默认今天")
    news.set_defaults(handler=run_news)

    industry = actions.add_parser(
        "industry",
        help="行业回填（公司概况 → 主档 sector；不联网，重复执行只报变更数）",
    )
    industry.add_argument("--market", required=True, help="A / HK / US")
    industry.set_defaults(handler=run_industry)

    spot = actions.add_parser(
        "spot",
        help="全市场快照（一个市场的一次横截面；盘后自动跑的那条同源）",
    )
    spot.add_argument("--market", required=True, help="A / HK / US")
    spot.add_argument("--day", help="YYYY-MM-DD，默认按报价时刻的众数日")
    spot.add_argument(
        "--code", action="append", help="只采这几只（试跑用；默认=代码表里全部在市）"
    )
    spot.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    spot.set_defaults(handler=run_spot)

    fx = actions.add_parser(
        "fx",
        help="汇率（跨币种持仓的换汇影响全靠这张表；它没有市场维度，所以不吃 --market）",
    )
    fx.add_argument("--base", required=True, help="1 单位哪种币种：USD / HKD …")
    fx.add_argument("--quote", required=True, help="换成哪种币种：CNY …")
    fx.add_argument("--start", required=True, help="YYYY-MM-DD")
    fx.add_argument("--end", help="YYYY-MM-DD，默认今天")
    fx.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    fx.set_defaults(handler=run_fx)


def run_fundamental(container: Container, args: argparse.Namespace) -> int:
    """三条基本面链共用的一条打印路径：种类由表里带出来，不在这里再判一次参数。"""
    method, label = FUNDAMENTAL_KINDS[args.action]
    start = iso_date(args.start) if args.start else DEFAULT_FUNDAMENTALS_START
    end = iso_date(args.end) if args.end else container.clock.today()
    sweep = getattr(args, "all", False)
    if sweep:
        codes = container.symbol_list_store.active_codes(args.market)
        if not codes:
            # 空票池照样「跑完 0 只」会让人以为覆盖已经补齐：先说清票池从哪来
            raise SamsError(
                f"代码表里 {args.market} 没有在市票：先跑 "
                f"`sams run once --job symbol_master --market {args.market}`"
            )
    else:
        codes = list(args.code)
    written = empty = 0
    listed_filled = 0
    listed_gaps: dict[str, int] = {}
    failed: list[str] = []
    for code in codes:
        try:
            summary = getattr(container.fundamental_sync, method)(args.market, code, start, end)
        except SamsError as exc:
            # 几千只的普查不能因为一只超时就整批重来：失败进汇总，其余照跑
            failed.append(code)
            print(f"{code:<10} {label}  失败：{exc}")
            continue
        line = f"{code:<10} {label}  源={summary.source or '—'} 取回={summary.fetched}"
        if summary.empty:
            empty += 1
            # 「这几家都说了没有」不是「取回 0 条」：前者的下一步是接受，后者的下一步是查源
            line += (
                f" 入库=0  链上每个有能力的源都说查无记录"
                f"（{'+'.join(summary.confirmed_empty_by)}）"
            )
        else:
            written += 1
            line += f" 入库={summary.written}"
        if summary.degraded:
            line += f" [降级: {_trail(summary.attempts)}]"
        print(line)
        # 上市日期是主档的一格（U18）：填上了读的人少敲一次 `--listed-on`，
        # 没填上要说清为什么——它不是基本面失败的那一半，所以不打断上面那一行。
        # 全市场普查里这条理由按市场只差一句话：逐票打出来会淹掉真正的取数结果。
        if summary.listed_on is not None:
            listed_filled += 1
            if not sweep:
                print(
                    f"           主档上市日期={summary.listed_on}"
                    "（下次读数不用再传 --listed-on）"
                )
        elif summary.listed_on_note:
            if sweep:
                # 按原因聚合、数只数：全市场普查里同一句话可能重复几千次
                listed_gaps[summary.listed_on_note] = (
                    listed_gaps.get(summary.listed_on_note, 0) + 1
                )
            else:
                print(f"           {summary.listed_on_note}")
        for name, reason in summary.skipped:
            print(f"           跳过 {name}：{reason}")
    if sweep:
        print(
            f"合计 {len(codes)} 只：有数据 {written}，查无记录 {empty}，失败 {len(failed)}"
        )
        if listed_filled:
            print(f"上市日期：本次填上 {listed_filled} 只（主档那一格原来是空的）")
        # 句子本身已经以「上市日期没填上：」开头，再加前缀会印成两句一样的话
        for note, count in listed_gaps.items():
            suffix = f"（{count} 只）" if count > 1 else ""
            print(f"{note}{suffix}")
    return 1 if failed else 0


def run_profile(container: Container, args: argparse.Namespace) -> int:
    """公司概况一票一行：给锁在里面的代码各问一次，重复执行行数不变。"""
    for code in args.code:
        _print_sync(container.f10_sync.sync_profile(args.market, code))
    return 0


def run_announcements(container: Container, args: argparse.Namespace) -> int:
    """最近一页公告。默认 365 天，窗口只裁剪源返回的那 100 条。"""
    end = iso_date(args.end) if args.end else container.clock.today()
    start = (
        iso_date(args.start)
        if args.start
        else end - timedelta(days=DEFAULT_ANNOUNCEMENTS_DAYS)
    )
    if start > end:
        raise ValidationError(f"--start {start} 在 --end {end} 之后：区间反了")
    for code in args.code:
        _print_sync(container.f10_sync.sync_announcements(args.market, code, start, end))
    return 0


def run_industry(container: Container, args: argparse.Namespace) -> int:
    """行业回填与盘后 job 走同一个服务：这里只把「扫了几只、改了几只」写成一句。"""
    report = container.industry.backfill(args.market)
    print(f"{report.market}  {report.note}")
    return 0


def run_news(container: Container, args: argparse.Namespace) -> int:
    """最近一页新闻。默认 30 天；重跑同一晚输出「入库=0」。"""
    end = iso_date(args.end) if args.end else container.clock.today()
    start = (
        iso_date(args.start)
        if args.start
        else end - timedelta(days=DEFAULT_NEWS_DAYS)
    )
    if start > end:
        raise ValidationError(f"--start {start} 在 --end {end} 之后：区间反了")
    for code in args.code:
        _print_sync(container.news.sync(args.market, code, start, end))
    return 0


def _print_sync(summary) -> None:
    line = f"{summary.code:<10} {summary.kind}  源={summary.source or '—'} 取回={summary.fetched}"
    if summary.empty:
        # 「每个有能力的源都说查无记录」不是「取回 0 条」：前者的下一步是接受，
        # 后者的下一步是查源（D43）
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


def run_fx(container: Container, args: argparse.Namespace) -> int:
    """汇率一条命令：区间一次问，答上了才一行行落库。

    取不到要**退出 1**，而不是打一行「0 条」：持仓那一屏对「换不出」的处置是说清哪一笔
    没进去，采集这层要是替它把「没数」报成一次成功的同步，下一层就没人再提了。
    「每个有能力的源都说这段没有」才是 0 与一句确认（D36），那条走 0。
    """
    try:
        base = currency_arg(args.base)
        quote = currency_arg(args.quote)
        start = iso_date(args.start)
        end = iso_date(args.end) if args.end else container.clock.today()
        if start > end:
            # 区间反了是手滑：链会照问不误然后回一句「这段没有」，把参数错报成数据缺
            raise ValidationError(f"--start {start} 在 --end {end} 之后：区间反了")
        summary = container.fx_sync.sync(base, quote, start, end)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(
            {
                "status": "ok",
                "base": summary.base,
                "quote": summary.quote,
                "source": summary.source,
                "fetched": summary.fetched,
                "written": summary.written,
                "degraded": summary.degraded,
                "empty": summary.empty,
                "latest_date": str(summary.latest_date) if summary.latest_date else None,
                "confirmed_empty_by": list(summary.confirmed_empty_by),
                "skipped": [[name, reason] for name, reason in summary.skipped],
            }
        )
        return 0
    line = f"{summary.base}→{summary.quote}  源={summary.source or '—'}"
    if summary.empty:
        said = "+".join(summary.confirmed_empty_by)
        line += f" 入库=0  链上每个有能力的源都说这段没有报价（{said}）"
    else:
        line += (
            f" 取回={summary.fetched} 入库={summary.written} 最新={summary.latest_date}"
        )
    if summary.degraded:
        line += f" [降级: {_trail(summary.attempts)}]"
    print(line)
    # 跳过的是「这个源不给汇率」，不是「这个源没数据」：两者在报告上必须分开
    for name, reason in summary.skipped:
        print(f"           跳过 {name}：{reason}")
    return 0


def run_spot(container: Container, args: argparse.Namespace) -> int:
    """补采与盘后 job 调同一个服务函数；这里只负责把范围、日与缺答写成一句话。"""
    try:
        report = container.snapshots.sync(
            args.market,
            codes=args.code or None,
            day=iso_date(args.day) if args.day else None,
        )
    except SamsError as exc:
        # 源全链挂掉是「没问到」，不是「这个市场没票」：退出 1，让脚本别把空库当成功
        return refused(args, exc, exit_for(exc))
    code = 0 if report.rows else 1
    if args.json:
        emit(
            {
                "status": "empty" if not report.rows else "ok",
                "exit": code,
                "market": report.market,
                "day": str(report.day),
                "taken_at": report.taken_at.isoformat(),
                "rows": report.rows,
                "universe_size": report.universe_size,
                "skipped": list(report.skipped),
                "source": report.source,
                "empty_everywhere": report.empty_everywhere,
            }
        )
        return code
    print(
        f"{report.market} 股  快照 {report.day}  请求 {report.universe_size} 只  "
        f"{report.rows} 行  来源 {report.source or '–'}  采集 {stamp(report.taken_at)}"
    )
    if report.empty_everywhere:
        print("⇒ 源回了一张空表：旧数据保留，未写入任何行（先看健康页的源状态）")
    elif report.skipped:
        print(
            f"⇒ 源没答 {len(report.skipped)} 只（代码表里有、这次响应里没有）："
            f"{'、'.join(report.skipped[:20])}"
            + ("…（--json 拿全表）" if len(report.skipped) > 20 else "")
        )
    return code


def _trail(attempts) -> str:
    return " → ".join(f"{a.source}{'✓' if a.ok else '✗'}" for a in attempts)
