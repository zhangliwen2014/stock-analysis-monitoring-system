"""`sams sync industry` 的出口：第一次回填几只、第二次必须 0 只。

回填不联网，所以这一枚验的是「两行库数据如何对齐」在命令行上的那次呈现：
行数就是真的改了几只主档——不是「扫过几只」。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import polars as pl
import support

import sams.cli as cli
from sams.bootstrap import build_container
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.symbols import SymbolRepo


def container_with(db, settings):
    adapter = support.StubAdapter("stub", ["A", "HK", "US"], support.sample_bars(source="stub"))
    markets = {
        market: replace(cfg, sources=("stub",)) for market, cfg in settings.markets.items()
    }
    built = build_container(
        replace(settings, markets=markets),
        db=db,
        adapters={"stub": adapter},
        clock=support.FrozenClock(datetime(2026, 10, 31, 15, tzinfo=UTC)),
    )
    built.registry.sleep = lambda _seconds: None
    return built


def seed(db) -> None:
    SymbolRepo(db).upsert(
        pl.DataFrame(
            [
                {"market": "A", "code": "600519", "currency": "CNY", "name": "贵州茅台"},
                {"market": "A", "code": "000001", "currency": "CNY", "name": "平安银行"},
            ]
        )
    )
    CompanyProfileRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "600519",
                    "name": "贵州茅台",
                    "industry": "食品饮料-饮料-白酒",
                    "source": "eastmoney_f10",
                },
                {
                    "market": "A",
                    "code": "000001",
                    "name": "平安银行",
                    "industry": "银行-股份行",
                    "source": "eastmoney_f10",
                },
            ]
        )
    )


def run(container, argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_first_run_backfills_and_second_run_reports_no_change(db, settings, capsys):
    seed(db)
    container = container_with(db, settings)
    try:
        assert run(container, ["sync", "industry", "--market", "A"]) == 0
        out = capsys.readouterr().out
        assert "概况 2 只" in out and "回填 2 只" in out

        assert SymbolRepo(db).get("A", "600519")["sector"] == "食品饮料-饮料-白酒"
        assert SymbolRepo(db).get("A", "000001")["sector"] == "银行-股份行"

        assert run(container, ["sync", "industry", "--market", "A"]) == 0
        again = capsys.readouterr().out
        assert "回填 0 只" in again and "未变 2 只" in again
    finally:
        container.close()
