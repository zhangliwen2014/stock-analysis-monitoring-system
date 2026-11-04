"""事件与复检页 /calendar（D232 P0-2）。

页面只回答「我的标的最近披露/公告了什么」：自选、持仓、推进中的论点各算一路。
未来的预约披露日与除权日没有可靠数据源，页面不猜——这件事也要有用例钉住。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl


def _user_id(ctx) -> str:
    return ctx.users.get_by_username("alice").id


def _seed_symbol(ctx, market="A", code="600519", name="贵州茅台") -> None:
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "name": [name],
                "currency": ["CNY" if market == "A" else "HKD"],
            }
        )
    )


def _seed_financials(ctx, published, *, market="A", code="600519", report=date(2026, 9, 30)):
    ctx.financials_store.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "report_date": [report],
                "published_date": pl.Series("published_date", [published], dtype=pl.Date),
                "source": ["akshare"],
            }
        )
    )


def _seed_announcement(
    ctx,
    published_on,
    *,
    title="贵州茅台:2026年第三季度报告",
    market="A",
    code="600519",
    art_code="AN202610280001",
):
    ctx.announcements_store.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "art_code": [art_code],
                "published_on": [published_on],
                "title": [title],
                "category": ["定期报告"],
                "url": [f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"],
                "source": ["eastmoney_f10"],
            }
        )
    )


def _hold(ctx, code="600519") -> None:
    account = ctx.ledger.open_account("alice", "复检测试户")
    user = ctx.users.get_by_username("alice")
    ctx.ledger.deposit(account.id, "CNY", 200_000.0, username="alice", reason="起始资金",
                       actor_id=user.id)
    ctx.ledger.buy(account.id, "alice", "A", code, qty=100, price=100.0)


def test_login_is_required(web_app) -> None:
    response = web_app.client.get("/calendar", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_recent_disclosure_of_a_watchlist_symbol_shows_its_age(logged) -> None:
    ctx = logged.context
    today = ctx.clock.today()
    _seed_symbol(ctx)
    ctx.watchlists.add(_user_id(ctx), "A", "600519")
    _seed_financials(ctx, today - timedelta(days=3))

    page = logged.client.get("/calendar").text

    assert "贵州茅台" in page
    assert "3 天" in page
    assert "2026-09-30" in page
    assert "/symbol/A/600519" in page


def test_an_old_disclosure_falls_out_of_the_window(logged) -> None:
    ctx = logged.context
    today = ctx.clock.today()
    _seed_symbol(ctx)
    ctx.watchlists.add(_user_id(ctx), "A", "600519")
    _seed_financials(ctx, today - timedelta(days=40))

    page = logged.client.get("/calendar").text

    assert "贵州茅台:2026年第三季度报告" not in page
    assert "最近 30 天没有已披露的财报" in page


def test_a_holding_is_included_even_without_a_watchlist_row(logged) -> None:
    ctx = logged.context
    today = ctx.clock.today()
    _seed_financials(ctx, today - timedelta(days=2), code="000001")
    _hold(ctx, code="000001")

    page = logged.client.get("/calendar").text

    assert "/symbol/A/000001" in page
    assert "2 天" in page


def test_a_recent_announcement_shows_its_title_and_link(logged) -> None:
    ctx = logged.context
    today = ctx.clock.today()
    _seed_symbol(ctx)
    ctx.watchlists.add(_user_id(ctx), "A", "600519")
    _seed_announcement(ctx, today - timedelta(days=1))
    _seed_announcement(
        ctx, today - timedelta(days=45), title="旧公告", art_code="AN202609150002"
    )

    page = logged.client.get("/calendar").text

    assert "贵州茅台:2026年第三季度报告" in page
    assert "AN202610280001.html" in page
    assert "旧公告" not in page


def test_financials_without_a_publication_date_get_a_named_note(logged) -> None:
    """港股/美股的披露日没入库：说「没披露」是把缺数据读成了事实。"""
    ctx = logged.context
    _seed_symbol(ctx, market="HK", code="00700", name="腾讯控股")
    ctx.watchlists.add(_user_id(ctx), "HK", "00700")
    _seed_financials(ctx, None, market="HK", code="00700")

    page = logged.client.get("/calendar").text

    assert "披露日未入库" in page
    assert "腾讯控股" in page


def test_the_page_says_future_events_have_no_source(logged) -> None:
    page = logged.client.get("/calendar").text
    assert "预约披露日" in page
    # 没有稳定源就不接：口径要写明「预计披露日未接入」，而不是留一句含糊的「暂无」
    assert "预计披露日未接入" in page
    assert "不编造" in page
