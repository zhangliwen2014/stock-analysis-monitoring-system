"""`sams run`：常驻调度、手工补一轮、按执行顺序列出注册的 job。

模块名用 `schedule` 而不是 `run`：`sams/cli/__init__.py` 里那个包级入口函数也叫 `run`。
叫同一个名字今天不会坏，只因为 `COMMAND_MODULES` 写在 `def run()` 之前——元组拿到的是
子模块，函数随后把包上那个名字改成自己。把两行的顺序换一下，元组里装的就是函数，
`module.add_to(...)` 当场 `AttributeError`。换一个名字，这件事就不依赖那两行的先后了。

这一层不认识「该不该跑这一轮」：到期计算、连败计数、心跳守卫都在 `services.scheduler`，
这里只做三件事——把参数归一化、把一轮的结论落到屏幕上、把退出码分给脚本。
"""

from __future__ import annotations

import argparse
import sys

from sams.bootstrap import Container
from sams.cli.common import exit_for, group, iso_date, stamp
from sams.core.codes import canonical_market
from sams.core.errors import SamsError
from sams.services.scheduler.runner import SKIPPED
from sams.services.scheduler.service import TickReport


def add_to(subparsers) -> None:
    actions = group(subparsers, "run", "调度器：常驻运行、手工跑一轮、列出注册的 job")

    start = actions.add_parser("start", help="常驻运行（Ctrl-C 停止；同一时刻只允许一个）")
    start.set_defaults(handler=_start)

    once = actions.add_parser("once", help="按当下的到期状态手工跑一轮")
    once.add_argument(
        "--as-of", help="把这一轮当成哪一天（YYYY-MM-DD；默认按各市场当地日期）"
    )
    once.add_argument(
        "--job",
        help=(
            "只跑这个 job（bars/actions/financials/valuation/flow/spot/recheck/rules/fx"
            "/calendar）"
        ),
    )
    once.add_argument("--market", help="只跑这个市场（A/HK/US）")
    once.set_defaults(handler=_once)

    actions.add_parser("jobs", help="按执行顺序列出注册的 job").set_defaults(
        handler=_jobs
    )


def _print_result(result, indent: str = "") -> None:
    line = (
        f"{indent}{result.status:<6} {result.job}@{result.market or '全局'}"
        f"  周期 {result.period_key}"
    )
    if result.note:
        line += f" · {result.note}"
    if result.skip_reason:
        line += f" · {result.skip_reason}"
    if result.error:
        line += f" · {result.error}"
    print(line)


def _print_tick(report: TickReport) -> None:
    """常驻模式的一轮：全是「这个周期已经跑过」且没有错误时，整轮不出声。

    常驻循环按 `scheduler_tick_s` 一轮接一轮，出声的那一屏会把真正的失败冲走。
    但「有错误」必须能穿透这条静音——静音的理由是「无事可做」，
    一个算不出到期日的 job 不是无事可做。
    """
    shown = [result for result in report.results if result.status != SKIPPED]
    if not shown and not report.errors:
        return
    print(f"[{stamp(report.now)}]")
    for result in shown:
        _print_result(result, "  ")
    for error in report.errors:
        print(f"  错误  {error}")


def _start(container: Container, args: argparse.Namespace) -> int:
    scheduler = container.scheduler
    # 启动那一句先报「几个 job、多久一轮」：敲的人要能确认自己启动的是刚改完的那份配置，
    # 而不是一个还在跑旧注册表的进程
    print(
        f"调度器启动：{len(scheduler.jobs)} 个 job，"
        f"每 {container.settings.scheduler_tick_s} 秒一轮（Ctrl-C 停止）"
    )
    try:
        ticks = scheduler.run_forever(on_tick=_print_tick)
    except SamsError as exc:
        # 走到这里的是「进不了循环」那一类：`run_forever` 先认领心跳，才进那个自己兜
        # `SamsError` 的 try，所以「已经有一个循环在跑」与「心跳那一行写不进去」
        # （库在那一刻读不了、迁移只应用了一半）是同一个出口。顶层 `run()` 也会兜住、
        # 说同一句话、给同一个码（1，不是 2：命令行没错）；这里先兜一层，是为了这一层的
        # 两个出口由这一层自己的用例钉住——顶层那一句目前没有用例
        print(f"错误: {exc}", file=sys.stderr)
        return exit_for(exc)
    print(f"已停止（{ticks} 个 tick）")
    return 0


def _once(container: Container, args: argparse.Namespace) -> int:
    try:
        # 归一化放在这里而不是交给 argparse 的 `type=`：`iso_date` 抛的是
        # `ValidationError`，argparse 不认它，`--as-of 2024-13-40` 会吐一段栈（D77）
        market = canonical_market(args.market) if args.market else None
        as_of = iso_date(args.as_of) if args.as_of else None
        report = container.scheduler.tick(
            as_of=as_of, only_job=args.job, only_market=market
        )
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return exit_for(exc)
    for result in report.results:
        _print_result(result)
    for title in report.waiting:
        print(f"等待  {title}  还没到点")
    for error in report.errors:
        print(f"错误  {error}")
    # 错误也非零：那几条是「这一轮连该不该跑都算不出来」，与「跑过了、跳过」不同。
    # 能走到这一行的 `errors` 只有到期计算那一条路：账本读不了那类是从上面那个
    # `except SamsError` 出 screen 的（一句 stderr、同一个码 1），不留到屏幕上
    return 1 if report.failed or report.errors else 0


def _jobs(container: Container, args: argparse.Namespace) -> int:
    print(f"注册 {len(container.scheduler.jobs)} 个 job（执行顺序即下列顺序）：")
    for spec in container.scheduler.jobs:
        # 周期紧跟着名字：它是「今晚该不该跑」的分母。两格宽度与健康页 `[调度]`
        # 那一节一致（名字按注册表里最长的 `financials` 留宽），`:<` 只补不截，
        # 留窄一格后面的列就跟着左移
        print(f"  {spec.name:<11} {(spec.market or '全局'):<4} {spec.period}")
    return 0
