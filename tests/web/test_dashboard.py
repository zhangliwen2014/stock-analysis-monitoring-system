"""看板与自选页。行情板与告警都从进程里读，不需要任何网络。"""

from __future__ import annotations

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
