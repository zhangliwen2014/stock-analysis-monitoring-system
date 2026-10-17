"""`sams source ...` —— 数据源的可插拔管理入口（M1 的「管理后台」）。

Web 后台在 M4，但**维护数据源这件事不必等它**：命令行走的是同一套
`services.SourceAdminService`，界面试的是按钮，这里试的是同一个函数。
因此所有校验（未注册的实现、不支持的市场、把市场弄成零源）都不在这个模块里重写。

两处有意为之的「不」：
  * 不接受 import 路径或模块名当 `kind`，只认注册表里的实现名——否则加源命令
    就成了任意代码执行入口。
  * 改完不重启当前进程的 registry：一条命令就是一个进程，下次 `sams sync` 自然读到新链。
"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container
from sams.cli.common import group, iso_date
from sams.core.errors import ValidationError


def add_to(subparsers) -> None:
    actions = group(subparsers, "source", "数据源管理：增删、启停、排降级顺序、试跑")

    def leaf(name: str, handler, help_text: str):
        parser = actions.add_parser(name, help=help_text)
        parser.set_defaults(handler=handler)
        return parser

    leaf("list", list_sources, "列出库里的源与各市场生效的降级链")
    leaf("kinds", list_kinds, "列出可用实现（可插拔清单）")

    addp = leaf("add", add, "登记一个源并排进指定市场")
    addp.add_argument("name")
    addp.add_argument("--kind", required=True, help="见 `sams source kinds`")
    addp.add_argument("--markets", required=True, help="逗号分隔，如 A,HK")
    addp.add_argument("--opt", action="append", default=[], metavar="K=V", help="可多次传入")
    addp.add_argument("--note")
    addp.add_argument("--disabled", action="store_true", help="登记但不启用")

    upd = leaf("update", update, "改参数或备注（实现类型不可改，换实现请 remove + add）")
    upd.add_argument("name")
    upd.add_argument("--opt", action="append", default=None, metavar="K=V", help="整组替换")
    upd.add_argument("--note")

    enable = leaf("enable", enable_, "启用；停用时保留的路由会重新生效")
    enable.add_argument("name")
    disable = leaf("disable", disable_, "停用；不会让任何市场失去全部源")
    disable.add_argument("name")
    remove = leaf("remove", remove_, "删除源并连带删掉它的路由")
    remove.add_argument("name")

    route = leaf("route", route_, "把源排进某市场的某个位次")
    route.add_argument("market")
    route.add_argument("name")
    route.add_argument("--priority", type=int, required=True, help="从 1 开始")

    unroute = leaf("unroute", unroute_, "从某市场的链里摘掉这个源")
    unroute.add_argument("market")
    unroute.add_argument("name")

    move = leaf("move", move_, "整条链重排：按给出顺序重写为 1..n")
    move.add_argument("market")
    move.add_argument("names", nargs="+")

    probep = leaf("probe", probe, "绕开降级链，只打这一个源取一段日线")
    probep.add_argument("name")
    probep.add_argument("--market", required=True)
    probep.add_argument("--code", required=True)
    probep.add_argument("--start", required=True, help="YYYY-MM-DD")
    probep.add_argument("--end", required=True, help="YYYY-MM-DD")


def _markets(value: str) -> list[str]:
    items = [m.strip().upper() for m in value.split(",") if m.strip()]
    if not items:
        raise ValidationError(f"--markets 为空，收到 {value!r}")
    return items


def _options(pairs: list[str] | None) -> dict:
    """`--opt timeout=20` → `{"timeout": 20}`。

    数字要转成 int 存：JSON 里存 "20" 会一路带到适配器，`float("20")` 侥幸能跑，
    而 `timeout` 换成布尔参数时侥幸就没有了。
    """
    out: dict = {}
    for pair in pairs or ():
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValidationError(f"--opt 要写成 key=value，收到 {pair!r}")
        out[key.strip()] = _coerce(value)
    return out


def _coerce(value: str):
    text = value.strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        return text


def list_sources(container: Container, args: argparse.Namespace) -> int:
    admin = container.source_admin
    rows = admin.list()
    if not rows:
        # 表是空的（还没迁移，或迁移没带种子数据）：此时生效的是配置文件里的链
        print("库里没有数据源记录，本次运行按配置文件 [markets.*].sources 取数：")
        _print_chains(_running_chains(container))
        print("（跑一次 `sams db migrate` 建表，再用 `sams source add` 维护）")
        return 0
    routes = admin.routes()
    print("[源]")
    for row in rows:
        where = sorted({m for m, n, p in routes if n == row.name})
        flags = "启用" if row.enabled else "停用"
        line = f"  {row.name:<12} {row.kind:<10} {flags:<5} 市场 {','.join(where) or '（未路由）'}"
        if row.options:
            line += f"  参数 {row.options}"
        if row.note:
            line += f"  # {row.note}"
        print(line)
    print("[各市场生效链]")
    _print_chains(admin.chains())
    return 0


def _print_chains(chains: dict[str, tuple[str, ...]]) -> None:
    for market in sorted(chains):
        print(f"  {market:<4} " + " → ".join(chains[market]))


def _running_chains(container: Container) -> dict[str, tuple[str, ...]]:
    """本次进程真正在用的链——库里没记录时它来自配置文件，不是空话。"""
    return dict(container.registry.market_sources)


def list_kinds(container: Container, args: argparse.Namespace) -> int:
    print("[可用实现]（加源只能从这些名字里选；新实现 = datasource/ 下加文件 + @source）")
    for entry in container.source_admin.catalog():
        print(
            f"  {entry.kind:<10} 市场 {','.join(sorted(entry.markets)):<8} "
            f"参数 {list(entry.options) or '（无）'}  来自 {entry.module}.py"
        )
        if entry.note:
            print(f"  {'':<10} {entry.note}")
    return 0


def add(container: Container, args: argparse.Namespace) -> int:
    row = container.source_admin.add(
        args.name,
        args.kind,
        _markets(args.markets),
        options=_options(args.opt),
        note=args.note,
        enabled=not args.disabled,
    )
    state = "启用" if row.enabled else "停用"
    print(f"已登记 {row.name}（实现 {row.kind}，{state}）；下次采集起生效")
    _print_chains(container.source_admin.chains())
    return 0


def update(container: Container, args: argparse.Namespace) -> int:
    if args.opt is None and args.note is None:
        raise ValidationError("没有要改的东西：给 --opt 或 --note")
    row = container.source_admin.update(
        args.name,
        options=None if args.opt is None else _options(args.opt),
        note=args.note,
    )
    print(f"已更新 {row.name}：参数 {row.options}，备注 {row.note}")
    return 0


def enable_(container: Container, args: argparse.Namespace) -> int:
    container.source_admin.set_enabled(args.name, True)
    print(f"已启用 {args.name}")
    _print_chains(container.source_admin.chains())
    return 0


def disable_(container: Container, args: argparse.Namespace) -> int:
    container.source_admin.set_enabled(args.name, False)
    print(f"已停用 {args.name}（路由保留，重新启用即回到原位次）")
    return 0


def remove_(container: Container, args: argparse.Namespace) -> int:
    dropped = container.source_admin.remove(args.name)
    print(f"已删除 {args.name}，连带移除路由 {dropped} 条")
    return 0


def route_(container: Container, args: argparse.Namespace) -> int:
    container.source_admin.route(args.market, args.name, args.priority)
    print(f"已把 {args.name} 排到 {args.market} 的第 {args.priority} 位")
    return 0


def unroute_(container: Container, args: argparse.Namespace) -> int:
    existed = container.source_admin.unroute(args.market, args.name)
    print(f"{args.market} 链里{'没有' if not existed else '已移除'} {args.name}")
    return 0


def move_(container: Container, args: argparse.Namespace) -> int:
    container.source_admin.reorder(args.market, args.names)
    print(f"{args.market} 链已重排")
    _print_chains(container.source_admin.chains())
    return 0


def probe(container: Container, args: argparse.Namespace) -> int:
    result = container.source_admin.probe(
        args.name, args.market, args.code, iso_date(args.start), iso_date(args.end)
    )
    span = ""
    if result.first_date is not None:
        span = f" {result.first_date}~{result.last_date}"
    print(
        f"{result.name} {result.market}/{result.code} "
        f"{'OK' if result.ok else 'FAIL'} {result.verdict}{span} 用时 {result.seconds}s"
    )
    # 失败不是命令失败：试跑的意义就是「看看它到底怎么坏的」，退出码 0 让脚本能继续问下一个源
    return 0
