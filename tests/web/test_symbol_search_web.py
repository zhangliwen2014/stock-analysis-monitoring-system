"""搜索面板 fragment：命中给可点加的候选，查无给一句话（D142/D143）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest


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


def test_prefix_hint_beats_the_selected_market(logged):
    """选着 A 敲 `hkpayh`：归一剥掉前缀后检索词是 PAYH，两个市场都命中——
    顺序由前缀提示定（D141）。"""
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search?code=hkpayh&market=A").text
    assert body.index('value="00700"') < body.index('value="000001"')


def test_none_found_says_so(logged):
    _seed(logged.context)
    response = logged.client.get("/api/symbol-search?code=不存在的东西")
    assert "查无" in response.text
    # 「查无」要回显敲过的那串（D143）：只说「查无」的人不知道是自己打错了还是真没有
    assert "不存在的东西" in response.text


def test_empty_query_is_empty_panel(logged):
    response = logged.client.get("/api/symbol-search?code=")
    assert "查无" not in response.text and "<table" not in response.text


def test_unsearchable_query_is_empty_panel_without_none_found(logged):
    """剥完不剩东西的检索词（乱符号、光一个市场字母）不是「查无」：没有检索就没有查。"""
    for junk in ("%%%", "HK"):
        response = logged.client.get("/api/symbol-search", params={"code": junk})
        assert "查无" not in response.text, junk
        assert "<table" not in response.text, junk


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


def test_the_price_column_is_named_for_what_it_is(logged):
    """那一格取的是库里最后一根日线，可能停在几天前：叫「昨收」是把旧数说成昨天的。"""
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search?code=payh").text
    assert "最近收盘" in body
    assert "昨收" not in body


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


def test_fill_panel_offers_a_button_that_fills_the_trade_form(logged):
    """录入成交的代码格要的是「把这行填进表单」，不是「加入自选」：同一个检索，
    两种用途（D206）。"""
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search/fills?code=payh").text
    assert "平安银行" in body and "填入" in body
    assert 'data-fill-market="A"' in body and 'data-fill-code="000001"' in body
    assert "/watchlist/add" not in body


def test_pick_panel_carries_every_shape_a_form_field_can_want(logged):
    """规则、建论、限额、关键词、探源都要「点一行填进本表单」，而它们要写的字段各不相同：
    `code`（裸代码）、`scope`（限额的对象）、`target_ref`（规则写 `A/600519`）。
    一行把三种形态都带上，接线的每一页只取自己那一格，不必再为每页做一个面板种类。"""
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search/pick?code=payh").text
    assert "平安银行" in body and "填入" in body
    assert 'data-pick-market="A"' in body
    assert 'data-pick-code="000001"' in body
    assert 'data-pick-ref="A/000001"' in body
    # 资讯那格搜的是标题里的名字，不是代码：不带名称就得再为它单开一个面板
    assert 'data-pick-name="平安银行"' in body
    assert "/watchlist/add" not in body


@pytest.mark.parametrize("field", ["code", "q", "target_ref", "scope"])
def test_the_pick_panel_reads_the_term_whatever_the_form_calls_that_box(logged, field):
    """候选面板不该要求每张表单把它的格子改叫 `code`。

    那几张表单的字段名是它们自己 POST 处理函数定的（资讯筛标题叫 `q`、规则的目标叫
    `target_ref`、限额的对象叫 `scope`）。htmx 只会把**这个输入框自己的值**用**它自己的
    名字**发出去，端点少认一个名字，那一页接线看着对、屏上一行候选都不出。
    """
    _seed(logged.context)
    body = logged.client.get(f"/api/symbol-search/pick?{field}=payh").text
    assert "平安银行" in body, f"{field} 里的检索词被丢了"
    assert 'data-pick-code="000001"' in body
