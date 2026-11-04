"""`sams sync news` 的出口：新增几行、默认窗口，以及「这个市场没有源」的拒绝。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.errors import ValidationError

TODAY = date(2026, 10, 31)
STAMP = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


class StubNewsAdapter:
    name = "eastmoney_f10"
    markets = frozenset({"A", "HK"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date, date]] = []

    def stock_news(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        return pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "source": [self.name],
                "item_id": [f"{code}-N1"],
                "published_at": [STAMP],
                "title": ["贵州茅台：关于回购股份的进展"],
                "summary": ["公司披露回购进展。"],
                "url": ["https://example.test/N1"],
            }
        )


def container_with(db, settings, adapter):
    markets = {
        **settings.markets,
        "A": replace(settings.markets["A"], sources=("eastmoney_f10",)),
        "HK": replace(settings.markets["HK"], sources=("eastmoney_f10",)),
        "US": replace(settings.markets["US"], sources=("yfinance",)),
    }
    built = build_container(
        replace(settings, markets=markets),
        db=db,
        adapters={
            "eastmoney_f10": adapter,
            "yfinance": StubAdapter("yfinance", ["US"], sample_bars(source="yfinance")),
        },
        clock=FrozenClock(datetime(2026, 10, 31, 15, tzinfo=UTC)),
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_sync_news_writes_and_a_rerun_reports_zero(db, settings, capsys):
    adapter = StubNewsAdapter()
    container = container_with(db, settings, adapter)
    try:
        assert run(container, ["sync", "news", "--market", "A", "--code", "600519"]) == 0
        out = capsys.readouterr().out
        assert "个股新闻" in out and "取回=1" in out and "入库=1" in out
        assert run(container, ["sync", "news", "--market", "A", "--code", "600519"]) == 0
        out = capsys.readouterr().out
        assert "入库=0" in out
        assert container.news_store.count() == 1
    finally:
        container.close()


def test_news_default_window_is_thirty_days(db, settings, capsys):
    adapter = StubNewsAdapter()
    container = container_with(db, settings, adapter)
    try:
        run(container, ["sync", "news", "--market", "A", "--code", "600519"])
        assert adapter.calls == [("A", "600519", TODAY - timedelta(days=30), TODAY)]
    finally:
        container.close()
        capsys.readouterr()


def test_hk_is_refused_before_it_reaches_the_source(db, settings):
    adapter = StubNewsAdapter()
    container = container_with(db, settings, adapter)
    try:
        with pytest.raises(ValidationError, match="新闻源未接入"):
            run(container, ["sync", "news", "--market", "HK", "--code", "00700"])
        assert adapter.calls == []
    finally:
        container.close()


def test_reversed_window_is_refused(db, settings):
    container = container_with(db, settings, StubNewsAdapter())
    try:
        with pytest.raises(ValidationError, match="区间反了"):
            run(
                container,
                [
                    "sync",
                    "news",
                    "--market",
                    "A",
                    "--code",
                    "600519",
                    "--start",
                    "2026-10-31",
                    "--end",
                    "2026-10-01",
                ],
            )
    finally:
        container.close()
