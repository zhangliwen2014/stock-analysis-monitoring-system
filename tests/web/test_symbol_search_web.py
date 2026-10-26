"""搜索面板 fragment：命中给可点加的候选，查无给一句话（D142/D143）。"""

from __future__ import annotations

from datetime import date

import polars as pl


def _seed(ctx):
    ctx.symbol_list_store.upsert_batch(
        "A",
        [
            {
                "code": "000001",
                "name": "平安银行",
                "py_initials": "payh",
                "py_full": "pinganyinhang",
                "currency": "CNY",
            }
        ],
    )
    ctx.symbol_list_store.upsert_batch(
        "HK",
        [
            {
                "code": "00700",
                "name": "平安控股",
                "py_initials": "payh",
                "py_full": "pingankonggu",
                "currency": "HKD",
            }
        ],
    )


def test_symbol_search_requires_login(web_app):
    response = web_app.client.get("/api/symbol-search?code=payh", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_candidate_row_carries_an_add_form(logged):
    _seed(logged.context)
    response = logged.client.get("/api/symbol-search?code=payh&market=A")
    assert response.status_code == 200
    body = response.text
    assert "平安银行" in body and "加入" in body
    assert 'value="000001"' in body and 'value="A"' in body
    # 候选行的 form 必须带 csrf：它 POST 的是现有 /watchlist/add
    assert 'name="csrf"' in body


def test_selected_market_first_but_others_not_dropped(logged):
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search?code=payh&market=A").text
    assert body.index('value="000001"') < body.index('value="00700"')
    body = logged.client.get("/api/symbol-search?code=payh&market=hk").text
    assert body.index('value="00700"') < body.index('value="000001"')


def test_none_found_says_so(logged):
    _seed(logged.context)
    response = logged.client.get("/api/symbol-search?code=不存在的东西")
    assert "查无" in response.text


def test_empty_query_is_empty_panel(logged):
    response = logged.client.get("/api/symbol-search?code=")
    assert "查无" not in response.text and "<table" not in response.text


def test_last_close_column_two_states(logged):
    ctx = logged.context
    ctx.symbol_list_store.upsert_batch(
        "A",
        [
            {
                "code": "600519",
                "name": "贵州茅台",
                "py_initials": "gzym",
                "py_full": "guizhoumaotai",
                "currency": "CNY",
            },
            {
                "code": "600520",
                "name": "有码无库",
                "py_initials": "gzymwx",
                "py_full": "youmaguwu",
                "currency": "CNY",
            },
        ],
    )
    ctx.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A", "A"],
                "code": ["600519", "600519"],
                "date": [date(2024, 1, 1), date(2024, 1, 2)],
                "close": [1500.0, 1499.5],
                "source": ["akshare", "akshare"],
            }
        )
    )
    body = logged.client.get("/api/symbol-search?code=gzym").text
    assert "1,499.50" in body, "昨收取库内最后收盘，不是报价板"
    assert "—" in body, "库里没行时写「—」：空位是真话，0 是假话"


def test_unit_follows_currency_override(logged):
    """双柜台 80700 报人民币：单位跟着标的走，不跟着市场默认走。"""
    ctx = logged.context
    ctx.symbol_list_store.upsert_batch(
        "HK",
        [
            {
                "code": "80700",
                "name": "腾讯人民币柜",
                "py_initials": "txcny",
                "py_full": "tengxunrenminbigui",
                "currency": "CNY",
            }
        ],
    )
    ctx.bars.upsert(
        pl.DataFrame(
            {
                "market": ["HK"],
                "code": ["80700"],
                "date": [date(2024, 1, 2)],
                "close": [300.0],
                "source": ["yfinance"],
            }
        )
    )
    body = logged.client.get("/api/symbol-search?code=txcny").text
    assert "300.00 元" in body and "港元" not in body
