"""`sams health` —— 一屏看清库、日历、数据源是否正常。"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container


def add_to(subparsers) -> None:
    parser = subparsers.add_parser("health", help="显示各表行数、日历覆盖、各源最近错误")
    parser.set_defaults(handler=show)


def show(container: Container, args: argparse.Namespace) -> int:
    report = container.health.report()
    print(f"库: {report.db_path}")
    if report.pending_migrations:
        print(f"  ⚠ 待应用迁移: {', '.join(report.pending_migrations)}")
    print("[表]")
    for name, rows in report.tables.items():
        print(f"  {name:<18} {rows:>8} 行")
    print("[日历覆盖]")
    for market, years in report.calendar_years.items():
        shown = ", ".join(str(y) for y in years) or "（无）"
        print(f"  {market:<4} {shown}")
    print("[数据源]")
    for source in report.sources:
        state = (
            "ok" if source.ok is True
            else "未知（未调用）" if source.ok is None
            else f"失败×{source.consecutive_failures}"
        )
        line = f"  {source.name:<10} {state:<13} 市场 {','.join(source.markets)}"
        if source.last_error:
            line += f"  最近错误 {source.last_error[:60]}"
        print(line)
    for source in report.degraded_sources:
        print(f"  ⚠ {source.name} 未恢复，检查网络或换源")
    for source in report.unverified_sources:
        print(f"  · {source.name} 尚未被调用过，健康状态未知")
    return 0 if not report.degraded_sources and not report.pending_migrations else 1
