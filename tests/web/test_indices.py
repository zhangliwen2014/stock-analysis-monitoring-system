"""大盘条：板上新鲜价优先，否则库里最后一天带日期说明（D145/§3）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from sams.core.quotes import Quote

DAYS = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]


def _seed_index(web, closes: list[float]):
    ctx = web.context
    ctx.index_daily_store.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "index_code": ["sh000001"] * len(closes),
                "date": DAYS[: len(closes)],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": [1.0] * len(closes),
                "amount": [1.0] * len(closes),
                "source": ["tencent"] * len(closes),
            }
        )
    )


def test_market_closed_shows_last_close(logged):
    _seed_index(logged, [100.0, 101.0])
    r = logged.client.get("/api/partials/indices")
    assert r.status_code == 200
    assert "上证指数" in r.text and "收盘" in r.text and "10-21" in r.text
    # 涨跌幅是库算好的那一列（+1%），不是读取侧再翻历史
    assert "+1.00%" in r.text


def test_fresh_quote_wins(logged):
    _seed_index(logged, [100.0, 101.0])
    now = logged.core.clock.now_utc()
    logged.core.board.update(
        "A",
        [
            Quote(
                market="A",
                code="sh000001",
                ts=now - timedelta(seconds=5),
                price=3900.0,
                pct_chg=0.5,
            )
        ],
        now=now,
        error=None,
    )
    r = logged.client.get("/api/partials/indices")
    assert "实时" in r.text
    assert "3,900.00" in r.text
    # 板上有新鲜数时不再提「收盘」：两个口径同屏会让读数的人猜哪个是现在的
    assert "收盘" not in r.text


def test_stale_board_quote_falls_back_to_the_last_close(logged):
    _seed_index(logged, [100.0, 101.0])
    now = logged.core.clock.now_utc()
    logged.core.board.update(
        "A",
        [Quote(market="A", code="sh000001", ts=now - timedelta(seconds=5), price=3900.0)],
        now=now,
        error=None,
    )
    logged.core.board.fail("A", "源挂了", now=now)  # fail 把全市场标 stale
    r = logged.client.get("/api/partials/indices")
    assert "实时" not in r.text and "收盘" in r.text


def test_missing_everywhere_market_absent(logged):
    r = logged.client.get("/api/partials/indices")
    assert r.status_code == 200
    assert "index-group" not in r.text


def test_indices_requires_login(web_app):
    r = web_app.client.get("/api/partials/indices", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_overview_carries_the_index_bar_anchor(logged):
    # 大盘条锚点在看板顶部：htmx load 首拉 + 30s 重拉（与 quotes partial 同套路）
    page = logged.client.get("/")
    assert 'hx-get="/api/partials/indices"' in page.text
    assert 'id="index-bar"' in page.text
