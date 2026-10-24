"""个股页与 K 线 JSON。日历用 StubCalendar，行情与日线都在进程里。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from sams.core.quotes import Quote


def _seed(logged, market="A", code="600519"):
    logged.context.symbols.upsert(
        pl.DataFrame(
            {"market": [market], "code": [code], "name": ["贵州茅台"], "currency": ["CNY"]}
        )
    )
    # 不用 support.sample_bars：那两根的 1.2→2.2 是 +83%，A 股那条涨跌幅守卫会把它读成
    # 「一定有未记录的公司行为」而整段拒绝出数。这里要的是「库里有几根能画的线」，
    # 所以给三根涨幅正常的价。
    last = date.today()
    closes = [1490.0, 1500.0, 1510.0]
    logged.context.bars.upsert(
        pl.DataFrame(
            {
                "market": [market] * 3,
                "code": [code] * 3,
                "date": [last - timedelta(days=offset) for offset in (6, 5, 4)],
                "open": closes,
                "high": [value + 10 for value in closes],
                "low": [value - 10 for value in closes],
                "close": closes,
                "volume": [100.0, 200.0, 300.0],
                "amount": [value * 100 for value in closes],
                "source": ["akshare"] * 3,
            }
        )
    )
    # 覆盖确认也要给：`series()` 读完日线还会查那道调整闸，没确认过的区间它不肯出数
    logged.context.corp_actions_coverage.record(
        market,
        code,
        covered_from=date(2000, 1, 1),
        covered_to=last,
        full_history=True,
        src="akshare",
    )


def _seed_quote(web, market="A", code="600519"):
    now = web.core.clock.now_utc()
    web.core.board.update(
        market,
        [Quote(market=market, code=code, ts=now, price=1500.0, source="tencent")],
        now=now,
        error=None,
    )


def test_symbol_page_needs_a_configured_market_and_a_real_row(logged):
    assert logged.client.get("/symbol/XX/1").status_code == 404
    assert logged.client.get("/symbol/A/999999").status_code == 404


def test_symbol_page_shows_quote_bars_rules_and_theses(logged):
    _seed(logged)
    _seed_quote(logged)
    user = logged.context.users.get_by_username("alice")
    logged.context.rule_service.add(
        user.id,
        kind="stop.fixed",
        name="茅台止损",
        target_type="symbol",
        target_ref="A/600519",
        params={"price": 1400.0},
    )
    logged.context.theses.add(user.username, "A", "600519", note="高端酒景气")
    page = logged.client.get("/symbol/A/600519")
    assert page.status_code == 200
    assert "1,500.00" in page.text
    assert "茅台止损" in page.text
    assert "高端酒景气" in page.text
    assert "/api/symbol/A/600519/bars" in page.text


def test_bars_api_returns_dates_and_close(logged):
    _seed(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?days=120").json()
    assert payload["dates"], "窗口内应有最近几根日线"
    assert payload["close"]


def test_bars_api_without_bars_returns_a_note_not_a_500(logged):
    logged.context.symbols.upsert(
        pl.DataFrame({"market": ["A"], "code": ["600036"], "name": ["招行"], "currency": ["CNY"]})
    )
    payload = logged.client.get("/api/symbol/A/600036/bars?days=120").json()
    assert payload["dates"] == []
    assert payload["note"]
