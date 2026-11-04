"""美股代码表：NASDAQ Trader 的两份官方清单（D226）。

过滤的是**不是普通股**的行：权证/权利/单位/优先股/票据的价格口径与普通股不可比，
ETF 也一样。测试钉的是「哪些行留下、哪些行必须被剔除」，而不是一份完整清单。
"""

from __future__ import annotations

import pytest

from sams.core.errors import DatasourceError, EmptyResponse
from sams.datasource.nasdaq_trader import (
    NasdaqTraderAdapter,
    parse_nasdaq_listed,
    parse_other_listed,
)

NASDAQ = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size"
    "|ETF|NextShares\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
    "QQQ|Invesco QQQ Trust|G|N|N|100|Y|N\n"
    "AACIU|Armada Acquisition Corp. III - Units|S|N|N|100|N|N\n"
    "AACIW|Armada Acquisition Corp. III - Warrants|S|N|N|100|N|N\n"
    "TEST|Test Issue Common Stock|Q|Y|N|100|N|N\n"
    "File Creation Time: 20261030120000|||||||\n"
)

OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. Class B Common Stock|N|BRK.B|N|100|N|BRK.B
BAC$A|Bank of America Corporation Depositary Shares|N|BAC$A|N|100|N|BAC$A
XYZ.PR|Example Preferred Stock|N|XYZ.PR|N|100|N|XYZ.PR
XYZ.NT|Example Notes due 2030|N|XYZ.NT|N|100|N|XYZ.NT
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
GOOG|Alphabet Inc. Class C Capital Stock|N|GOOG|N|100|N|GOOG
File Creation Time: 20261030120000|||||||
"""


def test_nasdaq_listed_keeps_common_stock_and_drops_etf_units_warrants_test_issues():
    rows = parse_nasdaq_listed(NASDAQ)
    assert rows == [{"code": "AAPL", "name": "Apple Inc. - Common Stock"}]


def test_other_listed_keeps_class_shares_and_drops_preferred_notes_etf():
    rows = parse_other_listed(OTHER)
    assert rows == [
        {"code": "BRK.B", "name": "Berkshire Hathaway Inc. Class B Common Stock"},
        {"code": "GOOG", "name": "Alphabet Inc. Class C Capital Stock"},
    ]


def test_a_line_that_is_not_a_table_row_is_not_a_symbol():
    """`File Creation Time` 那一行没有竖线；不能让它变成一只叫 File 的票。"""
    rows = parse_nasdaq_listed(NASDAQ + "\n")
    assert all(row["code"] != "File Creation Time" for row in rows)


def test_the_adapter_combines_both_files():
    adapter = NasdaqTraderAdapter(
        nasdaq_fetch=lambda: NASDAQ,
        other_fetch=lambda: OTHER,
    )
    assert adapter.symbol_list("US").to_dicts() == [
        {"code": "AAPL", "name": "Apple Inc. - Common Stock"},
        {"code": "BRK.B", "name": "Berkshire Hathaway Inc. Class B Common Stock"},
        {"code": "GOOG", "name": "Alphabet Inc. Class C Capital Stock"},
    ]


def test_either_file_failing_keeps_the_old_table_instead_of_a_partial_one():
    """一半的清单会让 `deactivate_missing` 把另一半全关掉——宁可整次失败。"""

    def broken():
        raise RuntimeError("Remote end closed connection without response")

    adapter = NasdaqTraderAdapter(nasdaq_fetch=lambda: NASDAQ, other_fetch=broken)
    with pytest.raises(DatasourceError):
        adapter.symbol_list("US")


def test_an_empty_file_is_a_no_answer_not_an_empty_universe():
    adapter = NasdaqTraderAdapter(
        nasdaq_fetch=lambda: NASDAQ,
        other_fetch=lambda: (
            "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
            "NASDAQ Symbol\nFile Creation Time: x|||||||\n"
        ),
    )
    with pytest.raises(EmptyResponse):
        adapter.symbol_list("US")


def test_other_markets_are_an_answer_not_a_failure():
    adapter = NasdaqTraderAdapter(nasdaq_fetch=lambda: NASDAQ, other_fetch=lambda: OTHER)
    with pytest.raises(EmptyResponse):
        adapter.symbol_list("A")
