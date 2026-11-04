"""每股价格按源给的精度显示（D308）：钱仍按两位，价不许被抹位。

起因是 #379 的核对：港股行情本身就在三位小数上（hk00386 今日 4.855/4.795，
腾讯、东财、新浪三家一致），而 `money()` 的 `,.2f` 会把它印成 4.86——
同一屏的 K 线画的却是原值。
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from sams.core.quotes import Quote
from sams.web.display import money, price

WEB = Path(__file__).resolve().parents[2] / "src" / "sams" / "web"


def _seed_hk_close(logged, code="00386", close=4.855):
    """一只只有日线的港股：休市时统一读模型回落这根收盘，页面上那一格就是它。"""
    logged.context.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["HK"],
                "code": [code],
                "name": ["中国石油化工股份"],
                "currency": ["HKD"],
            }
        )
    )
    last = date.today()
    closes = [close - 0.06, close - 0.03, close]
    logged.context.bars.upsert(
        pl.DataFrame(
            {
                "market": ["HK"] * 3,
                "code": [code] * 3,
                "date": [last - timedelta(days=offset) for offset in (6, 5, 4)],
                "open": closes,
                "high": [value + 0.02 for value in closes],
                "low": [value - 0.02 for value in closes],
                "close": closes,
                "volume": [100.0, 200.0, 300.0],
                "amount": [value * 100 for value in closes],
                "source": ["tencent"] * 3,
            }
        )
    )
    logged.context.corp_actions_coverage.record(
        "HK",
        code,
        covered_from=date(2000, 1, 1),
        covered_to=last,
        full_history=True,
        src="tencent",
    )


def test_price_keeps_the_third_decimal():
    """4.855 不许被印成 4.86：那是人要拿去对照下单的数。"""
    assert price(4.855) == "4.855"


def test_price_does_not_invent_a_third_decimal():
    """两位能表示就停在两位：A 股与美股的价全在这一档，多摆一个 0 是假精度。"""
    assert price(4.58) == "4.58"


def test_price_empty_is_a_dash_not_zero():
    """没数给「—」：这一点和 `money` 一样，页面上 0.00 会被读成一个真价。"""
    assert price(None) == "—"


def test_price_ignores_the_float_tail():
    """0.1+0.2 的浮点尾巴不许多出第三位：判据比的是四舍五入后的差，不是二进制位。"""
    assert price(0.1 + 0.2) == "0.30"


def test_price_goes_to_four_decimals_when_the_source_does():
    """只到三位的规则会把 0.0045 印成 0.005（美股仙股真有四位），那比两位更坏。"""
    assert price(0.0045) == "0.0045"


def test_price_keeps_the_thousands_separator_and_currency():
    assert price(7568.92, "USD") == "7,568.92 USD"


def test_money_still_rounds_amounts_to_two():
    """钱不是价：总资产/市值/盈亏这些仍按两位，不许被 F1「顺手统一」。"""
    assert money(4.855) == "4.86"
    assert money(1234567.891, "港元") == "1,234,567.89 港元"


def test_symbol_page_shows_the_three_decimal_hk_close(logged):
    """现价那一格给 4.855：同一屏的 K 线画的就是这个原值，两个数不能打架。"""
    _seed_hk_close(logged)
    text = logged.client.get("/symbol/HK/00386").text
    assert "4.855 港元" in text
    assert "4.86 港元" not in text


def test_watchlist_board_shows_the_three_decimal_hk_quote(logged):
    """自选/看板那张行情板的现价列同理：盘中那位半分的价不许被抹成两位。"""
    _seed_hk_close(logged)
    logged.client.post(
        "/watchlist/add", data={"csrf": logged.csrf, "market": "HK", "code": "00386"}
    )
    now = logged.core.clock.now_utc()
    logged.core.board.update(
        "HK",
        [Quote(market="HK", code="00386", ts=now, price=4.855, source="tencent")],
        now=now,
        error=None,
    )
    text = logged.client.get("/api/partials/quotes").text
    assert "4.855 港元" in text
    assert "4.86 港元" not in text


def test_timeshare_prev_close_label_uses_the_same_precision_rule(tmp_path):
    """分时图那句「昨收」由 JS 排版：同一条精度规则不能只在 Python 侧成立。

    拿 node 真跑一遍抽出来的那个函数，而不是在 app.js 里做文本匹配——文本匹配看见的
    是字，不是这条规则在 4.855 上到底印出几位。
    """
    if shutil.which("node") is None:
        pytest.skip("没有 node：这条钉的是前端那段 JS 的算法")
    source = (WEB / "static" / "app.js").read_text()
    start = source.index("function priceText(value)")
    body = source[start : source.index("\n  }", start) + len("\n  }")]
    assert '"昨收 " + priceText(baseline)' in source, "那段算法有了却没被昨收那一格用上"
    script = tmp_path / "price_text.js"
    check = "console.log([4.855, 4.58, 0.0045].map(priceText).join(' '));"
    script.write_text(
        "var echarts = {format: {addCommas: function (value) { return value; }}};\n"
        f"{body}\n{check}\n"
    )
    done = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["4.855", "4.58", "0.0045"]
