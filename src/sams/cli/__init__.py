"""CLI 层：解析参数、组装依赖、把结果打成文本。

边界规则：命令模块只通过 Container 拿服务，不 import storage/datasource 的实现，
也不在这里写业务判断（「该不该跳过周末」这类分支属于 services）。
加一个命令 = 加一个本包下的模块，不改其它模块。
"""

from __future__ import annotations

import argparse
import sys

from sams.bootstrap import build_container
from sams.cli import (
    analysis,
    calendar,
    channel,
    db,
    health,
    ledger,
    rule,
    screen,
    source,
    sync,
    thesis,
    user,
)
from sams.core.config import Settings
from sams.core.errors import SamsError

COMMAND_MODULES = (
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
    rule,
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
        container = build_container(Settings.load(args.config_path))
    except SamsError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command != "db":
            # 迁移幂等，因此非 db 命令顺手补齐表结构：新装环境直接 `sams user add`
            # 不该撞 CatalogException；`db migrate/status` 保留手工检视迁移的语义。
            container.migrate()
        return args.handler(container, args)
    except SamsError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        container.close()


def main() -> None:  # pragma: no cover - 控制台入口
    sys.exit(run())
