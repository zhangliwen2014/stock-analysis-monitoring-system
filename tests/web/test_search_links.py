"""S4-C：中文别名搜得到，候选与导航结果都链到个股页；导航搜索每个页面可见。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

WEB = Path(__file__).resolve().parents[2] / "src" / "sams" / "web"


def _seed(ctx):
    ctx.symbol_list_store.upsert_batch(
        "US",
        [
            {
                "code": "AAPL",
                "name": "Apple Inc.",
                "py_initials": "aapl",
                "py_full": "appleinc",
                "currency": "USD",
            }
        ],
    )
    ctx.bars.upsert(
        pl.DataFrame(
            {
                "market": ["US"],
                "code": ["AAPL"],
                "date": [date(2024, 1, 2)],
                "close": [185.5],
                "source": ["yfinance"],
            }
        )
    )


def test_chinese_alias_finds_the_us_symbol_in_the_candidate_panel(logged):
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search?code=苹果").text
    assert "Apple Inc." in body and "AAPL" in body
    assert "查无" not in body


def test_candidate_panel_links_to_the_symbol_page_and_keeps_add(logged):
    """面板的代码与名称是入口，不是死字：点了能进个股页；「加入自选」照旧（D142/S4-C）。"""
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search?code=AAPL").text
    assert 'href="/symbol/US/AAPL"' in body
    assert "/watchlist/add" in body


def test_nav_panel_renders_whole_rows_linked_to_the_symbol_page(logged):
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search/nav?code=苹果").text
    assert "最近收盘" in body
    assert "185.50 美元" in body
    assert 'href="/symbol/US/AAPL"' in body
    # 导航里的那一行是「去看」，不是「加入自选」：后者留在自选页的候选面板
    assert "/watchlist/add" not in body


def test_the_panel_kind_is_the_path_so_no_form_field_can_be_overridden(logged):
    """面板种类走路径，参数里只剩下「填了什么」。

    `?scope=pick` 那种写法要求 htmx 用 `hx-vals` 把种类盖进去，而 `hx-vals` 会连**同名**的
    输入框值一起盖掉：限额表单那一格就叫 `scope`，于是发出去的只有 `scope=pick`，
    用户敲的那串字根本没进请求（2026-11-01 浏览器实测）。种类进了路径，就没有任何一个
    表单字段名能与它相撞，页面也不必再为它抄一份检索词。
    """
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search?code=AAPL&scope=pick").text
    assert "/watchlist/add" in body, "scope 是检索词的一种，不该被当成面板种类"
    assert "data-pick-code" not in body


def test_nav_search_box_is_on_every_page_for_logged_users(logged):
    body = logged.client.get("/").text
    assert 'hx-get="/api/symbol-search/nav"' in body
    assert 'id="nav-search-results"' in body


def test_login_page_has_no_nav_search(web_app):
    body = web_app.client.get("/login").text
    assert 'hx-get="/api/symbol-search/nav"' not in body


def test_nav_results_collapse_on_escape():
    """键盘 Esc 收起导航搜索的结果（S4-C）：脚本里必须真的有一条 Esc 的路。"""
    text = (WEB / "static" / "app.js").read_text()
    assert "Escape" in text
    assert "nav-search-results" in text


def test_fill_panel_also_links_to_the_symbol_page(logged):
    _seed(logged.context)
    body = logged.client.get("/api/symbol-search/fills?code=AAPL").text
    assert 'href="/symbol/US/AAPL"' in body
    assert 'data-fill-code="AAPL"' in body
