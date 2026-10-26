"""看板与自选页。行情板与告警都从进程里读，不需要任何网络。"""

from __future__ import annotations

import re
from datetime import timedelta

import polars as pl
from fastapi.testclient import TestClient

from sams.core.quotes import Quote


def _login(client, username, password) -> str:
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("sams_csrf").rpartition(".")[0]


def _seed_quote(web, market="A", code="600519", price=1500.0):
    now = web.core.clock.now_utc()
    web.core.board.update(
        market,
        [
            Quote(
                market=market,
                code=code,
                ts=now - timedelta(seconds=5),
                price=price,
                source="tencent",
            )
        ],
        now=now,
        error=None,
    )


def _seed_symbol(ctx, market="A", code="600519"):
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "name": ["贵州茅台"],
                "currency": ["CNY"],
            }
        )
    )


def test_overview_requires_login(web_app):
    response = web_app.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_overview_lists_markets_and_the_first_run_checklist(logged):
    page = logged.client.get("/")
    assert page.status_code == 200
    for market in ("A", "HK", "US"):
        assert market in page.text
    assert "第一次运行" in page.text
    assert "今日告警" in page.text


def test_overview_shows_watchlist_quotes(logged):
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    page = logged.client.get("/")
    assert "1,500.00" in page.text
    assert "600519" in page.text


def test_quotes_partial_is_an_htmx_fragment(logged):
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    page = logged.client.get("/api/partials/quotes")
    assert page.status_code == 200
    assert "600519" in page.text
    assert "<html" not in page.text.lower()


def test_watchlist_add_remove_roundtrip(logged):
    client = logged.client
    response = client.post(
        "/watchlist/add",
        data={"csrf": logged.csrf, "market": "A", "code": "600519", "note": "白酒"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # 加票即登记兴趣并排补数拍（D144）：库里有它的开放队列行
    assert logged.context.backfills_store.open_for("A", "600519") is not None
    # 行链接要有主档才挂得出来（没主档点下去是 404 死路），所以先补一行主档再断链接
    _seed_symbol(logged.context)
    # 断言行链接而不是裸代码：表单的 placeholder 里也写着 600519，
    # 断「页面里没有这串数字」永远成立不了
    assert "/symbol/A/600519" in client.get("/watchlist").text
    assert "白酒" in client.get("/watchlist").text
    client.post(
        "/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    assert "/symbol/A/600519" not in client.get("/watchlist").text


def test_watchlist_rejects_an_unconfigured_market(logged):
    response = logged.client.post(
        "/watchlist/add", data={"csrf": logged.csrf, "market": "XX", "code": "1"}
    )
    assert response.status_code == 200
    assert "XX" in response.text


def test_watchlist_is_private_per_user(web_app):
    web_app.context.users.create("bob", "another-correct-horse")
    _login(web_app.client, "alice", "correct-horse-battery")
    bob = TestClient(web_app.app)
    bob_csrf = _login(bob, "bob", "another-correct-horse")
    bob.post("/watchlist/add", data={"csrf": bob_csrf, "market": "A", "code": "600519"})
    assert "/symbol/A/600519" not in web_app.client.get("/watchlist").text


def test_symbol_name_comes_from_the_master(logged):
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    _seed_symbol(logged.context)
    assert "贵州茅台" in logged.client.get("/watchlist").text


def _quote_row(page, code: str) -> str:
    """看板那张行情表里某个代码的那一行（`<tr>…</tr>` 原样）。

    只在看板那一屏取行：自选页有两张表都含代码，断言会挑错表。表只有这一屏这一张，
    而单位/链接/名称三件事都要按「交易员看到的那一行」来钉。
    """
    rows = re.findall(r"<tr>(.*?)</tr>", page.text, re.S)
    hits = [row for row in rows if f">{code}<" in row]
    assert hits, f"看板行情表里没有 {code} 这一行"
    return hits[0]


def _quote_cells(page, code: str) -> list[str]:
    """那一行各格的纯文本（市场 / 代码 / 名称 / 现价 / 涨跌 / 状态 / 时刻）。"""
    cells = re.findall(r"<td[^>]*>(.*?)</td>", _quote_row(page, code), re.S)
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]


def test_watchlist_row_without_master_symbol_is_not_a_dead_link(logged):
    """主档还没同步的自选代码（US 采集没跑过）不许挂 /symbol 链接：点下去是 404，
    而 404 那一屏（`pages/symbol.py` 的 `_require_symbol`）不区分「没这行」与「没这码」，
    人只会以为代码写错了。名称列照出代码 + 「主档未同步」提示。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    _seed_quote(logged, market="US", code="AAPL", price=250.0)
    page = logged.client.get("/")
    assert page.status_code == 200
    assert "/symbol/US/AAPL" not in page.text
    assert "主档未同步" in _quote_row(page, "AAPL")
    cells = _quote_cells(page, "AAPL")
    assert cells[2] == "AAPL", "名称列不许留空：空白看起来像页面坏了"


def test_the_link_stays_when_the_master_has_the_row(logged):
    """主档有行时链接照旧：遮蔽只针对查无此码的行。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    _seed_symbol(logged.context)
    page = logged.client.get("/")
    assert "/symbol/A/600519" in page.text
    assert "主档未同步" not in _quote_row(page, "600519")


def test_quotes_partial_masks_the_link_too(logged):
    """10 秒一刷的那块走的是同一个折法：部分刷新不许把 404 链接又长回来。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    _seed_quote(logged, market="US", code="AAPL", price=250.0)
    page = logged.client.get("/api/partials/quotes")
    assert "/symbol/US/AAPL" not in page.text
    assert "主档未同步" in page.text


def test_price_unit_follows_the_market_currency(logged):
    """现价那一格带的单位必须是该市场的币种：港股写「元」是把 400 港元说成 400 人民币。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "HK", "00700")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged, market="HK", code="00700", price=400.0)
    _seed_quote(logged, market="US", code="AAPL", price=250.0)
    _seed_quote(logged, price=1500.0)
    page = logged.client.get("/")
    assert _quote_cells(page, "00700")[3] == "400.00 港元"
    assert _quote_cells(page, "AAPL")[3] == "250.00 美元"
    assert _quote_cells(page, "600519")[3] == "1,500.00 元"
