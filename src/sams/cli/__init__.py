"""CLI 层：解析参数、组装依赖、把结果打成文本。

边界规则：命令模块只通过 Container 拿服务，不 import storage/datasource 的实现，
也不在这里写业务判断（「该不该跳过周末」这类分支属于 services）。
加一个命令 = 加一个本包下的模块，不改其它模块。
"""

from __future__ import annotations

import argparse
import sys

from sams.bootstrap import build_container, migrate_before_start
from sams.cli import (
    alert,
    analysis,
    attribution,
    calendar,
    channel,
    db,
    health,
    ledger,
    nav,
    portfolio,
    rule,
    schedule,
    screen,
    serve,
    source,
    sync,
    thesis,
    user,
)
from sams.cli.common import EXIT_LOCKED
from sams.core.config import Settings
from sams.core.errors import DatabaseLocked, SamsError

COMMAND_MODULES = (
    serve,
    db,
    user,
    sync,
    calendar,
    health,
    source,
    analysis,
    screen,
    thesis,
    ledger,
    channel,
    nav,
    portfolio,
    rule,
    schedule,
    alert,
    attribution,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sams", description="SAMS 运维入口")
    parser.add_argument(
        # dest 特意不叫 `config`：子命令有权用自己的 `--config`（`channel add --config k=v`
        # 就是），而两个名字撞在同一个 Namespace 上时，子解析器的默认值会把这里那个路径抹掉——
        # 症状不是报错，是 `sams --config X channel add …` 悄悄读了另一个配置文件
        "--config",
        dest="config_path",
        help="配置文件路径，默认取 SAMS_CONFIG 或 config/settings.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in COMMAND_MODULES:
        module.add_to(subparsers)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.config_path)
    except SamsError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    if args.command == "serve":
        # serve 要三条连接（D113）：它拿 Settings 自己装配，不走 build_container 那一条。
        # 分派必须在这里、在容器之前——先造容器会白开一条并把它握在 CLI 手里
        return serve.start(settings, args)
    try:
        if args.command != "db":
            # #384：迁移排在链快照前面。建容器那一刻链就从库里定下来了；晚一步的迁移
            # 幂等，但这一趟的链已经旧了，新源要等下一次启动才上岗。
            # `db` 命令例外：status 要看的就是「还有哪些没应用」。
            migrate_before_start(settings)
        container = build_container(settings)
    except DatabaseLocked as exc:
        # 「库被占用」不是配置错误：那个进程可能正在干活（D111）。
        # health 是唯一还能说出一点东西的命令——库旁那一拍没被锁
        if args.command == "health":
            return health.show_locked(exc, settings=settings)
        print(f"数据库被占用: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except SamsError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    try:
        return args.handler(container, args)
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        container.close()


def main() -> None:  # pragma: no cover - 控制台入口
    sys.exit(run())
