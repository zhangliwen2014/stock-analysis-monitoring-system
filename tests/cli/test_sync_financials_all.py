"""`sams sync financials --all`：按代码表逐只普查（D227）。

选股看的是全市场，而调度器的 financials job 只采用户并集——两者错位时，屏幕上的
「PEG ≤ 2 命中 5 只」会被读成「没有便宜票」。`--all` 是补这一次普查的人工入口：
票池来自 `symbol_master`，逐只失败进汇总、不中断整批。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars
from support_fundamentals import StubFundamentalsAdapter

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.errors import DatasourceError, SamsError
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.storage.repos.symbols import SymbolRepo

REPORT = date(2025, 12, 31)


def financials_for(*codes) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "market": "HK",
                "code": code,
                "report_date": REPORT,
                "published_date": None,
                "profit_yoy": 0.1585,
                "source": "eastmoney_hk_f10",
            }
            for code in codes
        ],
        schema_overrides={"published_date": pl.Date},
    )


class FlakyFundamentals(StubFundamentalsAdapter):
    """按代码决定成败：整批普查里最要紧的是「一只挂了，其余照跑」。"""

    def __init__(self, *args, fail_codes=(), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_codes = set(fail_codes)

    def financials(self, market, code, start, end):
        if code in self.fail_codes:
            raise DatasourceError("eastmoney_hk_f10", "接口超时")
        return super().financials(market, code, start, end)


def make_container(db, settings, financials, *, fail_codes=()):
    adapter = FlakyFundamentals(["HK", "US"], financials=financials, fail_codes=fail_codes)
    built = build_container(
        settings,
        db=db,
        adapters={
            "akshare": StubAdapter("akshare", ["A"], sample_bars(source="akshare")),
            "eastmoney": StubAdapter("eastmoney", ["A"], sample_bars(source="eastmoney")),
            "yfinance": adapter,
        },
        clock=FrozenClock(datetime(2026, 10, 30, 15, tzinfo=UTC)),
    )
    built.registry.sleep = lambda _seconds: None
    return built


def run(container, argv: list[str]) -> int:
    args = cli.build_parser().parse_args(argv)
    return args.handler(container, args)


def seed_hk(db, *codes, inactive=()):
    repo = SymbolListRepo(db)
    repo.upsert_batch(
        "HK",
        [
            {"code": code, "name": code, "py_initials": code, "py_full": code, "currency": "HKD"}
            for code in codes
        ],
    )
    if inactive:
        repo.deactivate_missing("HK", [code for code in codes if code not in inactive])


def test_all_takes_the_pool_from_this_markets_symbol_list(db, settings, capsys):
    container = make_container(db, settings, financials_for("00700", "01021"))
    seed_hk(db, "00700", "01021", "03690", inactive={"03690"})
    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {
                "code": "600519",
                "name": "贵州茅台",
                "py_initials": "gzmt",
                "py_full": "guizhoumaotai",
                "currency": "CNY",
            }
        ],
    )
    capsys.readouterr()
    assert run(container, ["sync", "financials", "--market", "HK", "--all"]) == 0
    out = capsys.readouterr().out
    assert "00700" in out and "01021" in out
    assert "03690" not in out, "停用的票不在普查范围内"
    assert "600519" not in out, "别的市场的票不在这次普查里"
    assert "合计 2 只：有数据 2，查无记录 0，失败 0" in out
    assert container.financials_store.count("HK") == 2


def test_all_and_code_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(
            ["sync", "financials", "--market", "HK", "--all", "--code", "00700"]
        )
    assert caught.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_one_failure_does_not_stop_the_batch_and_the_summary_names_it(db, settings, capsys):
    container = make_container(
        db, settings, financials_for("00700", "01021"), fail_codes={"01021"}
    )
    seed_hk(db, "00700", "01021", "03690")
    capsys.readouterr()
    assert run(container, ["sync", "financials", "--market", "HK", "--all"]) == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert any(line.startswith("01021") and "失败" in line for line in lines)
    assert any(line.startswith("00700") and "取回=1 入库=1" in line for line in lines)
    assert "合计 3 只：有数据 1，查无记录 1，失败 1" in out
    assert container.financials_store.count("HK") == 1


def test_an_empty_market_list_names_the_command_that_fills_it(db, settings):
    container = make_container(db, settings, financials_for("00700"))
    with pytest.raises(SamsError, match="symbol_master"):
        run(container, ["sync", "financials", "--market", "HK", "--all"])


def test_the_listed_date_gap_is_said_once_per_sweep_not_once_per_code(
    db, settings, capsys
):
    """几千只票的普查里，每一只都打一句「上市日期没填上」会淹掉真正的逐票结果。

    理由按市场只差一句话，聚合成一次；逐票那一行只讲这次同步取到了什么。
    """
    container = make_container(db, settings, financials_for("00700", "01021"))
    seed_hk(db, "00700", "01021")
    # 主档有行、但链上没有一个源能给上市日期：这是同一个市场级缺口，逐票重复没有信息
    SymbolRepo(db).upsert(
        pl.DataFrame(
            [
                {"market": "HK", "code": "00700", "name": "腾讯控股", "currency": "HKD"},
                {"market": "HK", "code": "01021", "name": "中国银河", "currency": "HKD"},
            ]
        )
    )
    capsys.readouterr()
    assert run(container, ["sync", "financials", "--market", "HK", "--all"]) == 0
    out = capsys.readouterr().out
    assert out.count("上市日期没填上") == 1
    assert out.count("00700") >= 1 and out.count("01021") >= 1


def test_a_missing_master_row_is_one_line_with_a_count_not_one_per_code(
    db, settings, capsys
):
    """主档里没有行的票在全市场普查里是几千只，理由一字不差、只差一个代码。

    逐票打出来就是几千行噪声（US 那次 5303 行），而「这些票的上市日期没处写」
    第一次出现时就已经说完了：聚合成一行、把只数说清楚。
    """
    container = make_container(db, settings, financials_for("00700", "01021"))
    seed_hk(db, "00700", "01021")
    capsys.readouterr()
    assert run(container, ["sync", "financials", "--market", "HK", "--all"]) == 0
    out = capsys.readouterr().out
    assert out.count("主档里还没有这一行") == 1
    assert "（2 只）" in out
