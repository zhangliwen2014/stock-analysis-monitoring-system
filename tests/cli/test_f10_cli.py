"""`sams sync profile|announcements` 的两个出口：写了几行、窗口问了哪一段。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.errors import ValidationError

TODAY = date(2024, 7, 9)


class StubF10Adapter:
    name = "eastmoney_f10"
    markets = frozenset({"A", "HK"})

    def __init__(self, *, empty_announcements: bool = False) -> None:
        self.empty_announcements = empty_announcements
        self.calls: list[tuple[str, str, str, date, date]] = []

    def company_profile(self, market, code, start, end):
        self.calls.append(("profile", market, code, start, end))
        return pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "source": self.name,
                }
            ]
        )

    def announcements(self, market, code, start, end):
        self.calls.append(("announcements", market, code, start, end))
        if self.empty_announcements:
            return pl.DataFrame()
        return pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "art_code": ["AN-1"],
                "published_on": [end],
                "title": ["年度报告"],
                "category": ["定期报告"],
                "url": ["https://example.test/AN-1"],
                "source": [self.name],
            }
        )


def container_with(db, settings, adapter):
    """A 与 HK 的链上都放这一个源；US 的普通桩只为让注册表能解析每个市场。"""
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
        clock=FrozenClock(datetime(2024, 7, 9, 15, tzinfo=UTC)),
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def test_sync_profile_writes_one_row_and_rerun_is_stable(db, settings, capsys):
    adapter = StubF10Adapter()
    container = container_with(db, settings, adapter)
    try:
        assert run(container, ["sync", "profile", "--market", "A", "--code", "600519"]) == 0
        out = capsys.readouterr().out
        assert "公司概况" in out and "取回=1" in out and "入库=1" in out
        assert run(container, ["sync", "profile", "--market", "A", "--code", "600519"]) == 0
        capsys.readouterr()
        assert container.f10.read("A", "600519").profile["name"] == "贵州茅台"
    finally:
        container.close()


def test_announcements_default_window_is_one_year(db, settings, capsys):
    adapter = StubF10Adapter()
    container = container_with(db, settings, adapter)
    try:
        assert run(container, ["sync", "announcements", "--market", "A", "--code", "600519"]) == 0
        capsys.readouterr()
        assert adapter.calls == [
            ("announcements", "A", "600519", TODAY - timedelta(days=365), TODAY)
        ]
        assert len(container.f10.read("A", "600519").announcements) == 1
    finally:
        container.close()


def test_announcements_empty_answer_says_who_said_no(db, settings, capsys):
    container = container_with(db, settings, StubF10Adapter(empty_announcements=True))
    try:
        assert run(container, ["sync", "announcements", "--market", "A", "--code", "600519"]) == 0
        out = capsys.readouterr().out
        assert "查无记录" in out and "入库=0" in out
        assert container.f10.read("A", "600519").announcements == ()
    finally:
        container.close()


def test_reversed_window_is_refused_before_it_reaches_the_source(db, settings):
    adapter = StubF10Adapter()
    container = container_with(db, settings, adapter)
    try:
        with pytest.raises(ValidationError, match="区间反了"):
            run(
                container,
                [
                    "sync",
                    "announcements",
                    "--market",
                    "A",
                    "--code",
                    "600519",
                    "--start",
                    "2024-07-01",
                    "--end",
                    "2024-06-01",
                ],
            )
        assert adapter.calls == []
    finally:
        container.close()
