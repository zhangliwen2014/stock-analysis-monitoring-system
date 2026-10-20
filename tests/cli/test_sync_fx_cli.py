"""`sams sync fx` 的出口：一行说清几分入库，取不到时退出码 1 且库里一行都不多。

采集命令的文本输出就是这个模块的对外行为，所以这里测的是**那句话有没有替数据说谎**：

* 「0 条」与「这条链上没有能给汇率的源」必须分开（D38）。前者下一步是接受，
  后者下一步是加源——混成一行会让人对着一个空库什么也不做。
* 全链挂了不是「今天没有汇率」：`fx_pnl` 拆不出是持仓那一屏的事，采集这一层要给的
  是一次失败（退出码非 0），不是一个看起来成功的 0。
* `--json` 的每个出口都是可解析 JSON 且带 `exit`（D57），参数错也是。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest
from support import FrozenClock, StubAdapter, sample_bars

import sams.cli as cli
from sams.bootstrap import build_container
from sams.core.errors import TransientError
from sams.datasource.fx import normalize_fx

D1, D2 = date(2024, 10, 24), date(2024, 10, 25)
BASE, QUOTE = "USD", "CNY"


class StubFxAdapter(StubAdapter):
    """日线桩 + 一个 `fx_rate`：链上「这个源能给汇率」就是从这个方法看出来的（D38）。"""

    def __init__(self, name: str, markets, rate: float = 7.1, *, outcome=None) -> None:
        super().__init__(name, markets, sample_bars(source=name))
        self._rate = rate
        self._outcome = outcome

    def fx_rate(self, base: str, quote: str, start: date, end: date) -> pl.DataFrame:
        if self._outcome is not None:
            raise self._outcome
        return normalize_fx(
            pl.DataFrame(
                {
                    "date": [D1.isoformat(), D2.isoformat()],
                    "base": [base, base],
                    "quote": [quote, quote],
                    "rate": [str(self._rate), str(self._rate)],
                }
            ),
            base=base,
            quote=quote,
            source=self.name,
        )


def make_container(db, settings, adapters) -> object:
    """时钟冻在 2024-10-26：`--end` 缺省时它决定「今天」是哪天，不许问墙钟。"""
    built = build_container(
        settings,
        db=db,
        adapters=adapters,
        clock=FrozenClock(datetime(2024, 10, 26, 15, tzinfo=UTC)),
    )
    built.registry.sleep = lambda _seconds: None
    return built


def seeded(db, settings, *, fx=None):
    """A 股链上放一个能给汇率的源，另两个只给日线：降级与跳过都要在同一屏上看清。

    能给汇率的那个排在**最后**：链在第一个答上的源就收敛（多打一次网络请求是行为改变，
    不是重构），排在它前面的才会进「跳过」那一栏。
    """
    capable = fx if fx is not None else StubFxAdapter("akshare", ["A"])
    return make_container(
        db,
        settings,
        {
            "eastmoney": StubAdapter("eastmoney", ["A"], sample_bars(source="eastmoney")),
            "yfinance": StubAdapter("yfinance", ["HK", "US"], sample_bars(source="yfinance")),
            "akshare": capable,
        },
    )


def run(container, capsys, *extra: str) -> tuple[int, str]:
    """与 `test_ledger_cli.py` 同一跑法：退出码 + 打印的那一行就是这个命令的全部行为。"""
    args = cli.build_parser().parse_args(["sync", "fx", *extra])
    capsys.readouterr()
    code = args.handler(container, args)
    return code, capsys.readouterr().out


def _window(*extra: str) -> tuple[str, ...]:
    return ("--base", BASE, "--quote", QUOTE, "--start", str(D1), "--end", str(D2), *extra)


def test_a_working_fx_source_reports_the_rows_it_wrote(db, settings, capsys) -> None:
    container = seeded(db, settings)
    code, body = run(container, capsys, *_window())
    assert code == 0
    assert "入库=2" in body
    assert container.fx_store.get(D2, BASE, QUOTE).rate == pytest.approx(7.1)


def test_the_names_that_were_skipped_are_printed(db, settings, capsys) -> None:
    """跳过的是「这个源不给汇率」，不是「这个源没数据」：两者在报告上必须分开。"""
    code, body = run(seeded(db, settings), capsys, *_window())
    assert code == 0
    assert "跳过 eastmoney" in body and "不提供汇率" in body


def test_json_carries_the_days_and_the_source(db, settings, capsys) -> None:
    code, body = run(seeded(db, settings), capsys, *_window("--json"))
    assert code == 0
    payload = json.loads(body)
    assert payload["base"] == BASE and payload["quote"] == QUOTE
    assert payload["written"] == 2
    assert payload["source"] == "akshare"
    assert payload["latest_date"] == str(D2)


def test_a_dead_chain_exits_1_and_writes_nothing(db, settings, capsys) -> None:
    """取不到要说成失败：把「挂了」印成「今天 0 条」，下一层就把没有当成没有。"""
    dead = StubFxAdapter("akshare", ["A"], outcome=TransientError("akshare", "503"))
    container = seeded(db, settings, fx=dead)
    capsys.readouterr()
    code, body = run(container, capsys, *_window("--json"))
    assert code == 1
    assert json.loads(body)["status"] == "refused"
    assert container.fx_store.get(D1, BASE, QUOTE) is None


def test_a_chain_without_any_fx_source_names_the_gap(db, settings, capsys) -> None:
    """一个能给的源都没有：这是 D38 要出声的缺，不是「0 条」。"""
    container = make_container(
        db,
        settings,
        {
            "akshare": StubAdapter("akshare", ["A"], sample_bars(source="akshare")),
            "eastmoney": StubAdapter("eastmoney", ["A"], sample_bars(source="eastmoney")),
            "yfinance": StubAdapter("yfinance", ["HK", "US"], sample_bars(source="yfinance")),
        },
    )
    code, body = run(container, capsys, *_window())
    assert code == 1
    assert "汇率" in body


def test_a_bad_date_is_a_parameter_error(db, settings, capsys) -> None:
    """`2024-7-1` 是手滑：日期要在解析那一步就说清该怎么写，而不是带着 None 往下走。"""
    container = seeded(db, settings)
    code, body = run(container, capsys, "--base", BASE, "--quote", QUOTE, "--start", "2024-7-1")
    assert code == 2
    assert "YYYY-MM-DD" in body
