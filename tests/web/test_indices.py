"""大盘条：板上新鲜价优先，否则库里最后一天带日期说明（D145/§3）。

「新鲜」一律问 `quote_state`（与自选行同一套五态）：`Quote.is_stale` 说的是「源没给
时刻」，不是「它旧」，休市时板上留着上一交易日那一笔并不 stale，用它当判活条件会把
几小时前的价标成「实时」。所以这里的用例都冻住时钟，盘中/休市各一个确定时刻。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi.testclient import TestClient
from support import FrozenClock

from sams.core.quotes import Quote

DAYS = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]

#: 板上有报价时用的市场：只配 A 的指数，别的市场不干扰断言
MARKET = "A"
INDEX_CODE = "sh000001"


def _seed_index(ctx, closes: list[float]):
    ctx.index_daily_store.upsert(
        pl.DataFrame(
            {
                "market": [MARKET] * len(closes),
                "index_code": [INDEX_CODE] * len(closes),
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


def _moment(weekday: int, hour_utc: int) -> datetime:
    """真实当下之后第一个「星期几 = weekday、UTC = hour_utc」的时刻。

    冻结点严格在未来：会话空闲校验拿真实墙钟比对（auth.py），过去的冻结点会先让登录失败。
    """
    now = datetime.now(UTC)
    day = (now + timedelta(days=(weekday - now.weekday()) % 7)).date()
    moment = datetime.combine(day, dtime(hour=hour_utc), tzinfo=UTC)
    while moment <= now + timedelta(minutes=10):
        moment += timedelta(days=7)
    return moment


def _board_quote(core, moment: datetime, **kwargs) -> None:
    core.board.update(
        MARKET,
        [Quote(market=MARKET, code=INDEX_CODE, ts=moment - timedelta(seconds=5), **kwargs)],
        now=moment,
        error=None,
    )


@pytest.fixture
def logged_at(make_app):
    """按给定时刻装一台已登录的 app：大盘条的「算不算实时」读的就是这只时钟。"""
    opened: list[TestClient] = []

    def make(moment: datetime):
        web = make_app(clock=FrozenClock(moment))
        ctx = web.contexts.current()
        ctx.users.create("alice", "correct-horse-battery", is_admin=True)
        client = TestClient(web.app)
        client.__enter__()
        opened.append(client)
        login = client.post(
            "/login",
            data={"username": "alice", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        return SimpleNamespace(client=client, core=web.core, ctx=ctx, moment=moment)

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def test_market_closed_shows_last_close(logged):
    _seed_index(logged.context, [100.0, 101.0])
    r = logged.client.get("/api/partials/indices")
    assert r.status_code == 200
    assert "上证指数" in r.text and "收盘" in r.text and "10-21" in r.text
    # 涨跌幅是库算好的那一列（+1%），不是读取侧再翻历史
    assert "+1.00%" in r.text


def test_closed_market_does_not_label_the_board_quote_live(logged_at):
    """休市 + 板上留着上一交易日那一笔：不许说「实时」，要落回库里的收盘（D145 评审 重要1）。

    这一笔 `is_stale=False`、时刻也「新」：判活的旧写法只看这两条，于是收盘几小时后
    屏幕上仍挂「实时」——正是休市兜底要防的事。
    """
    page = logged_at(_moment(weekday=5, hour_utc=2))
    _seed_index(page.ctx, [100.0, 101.0])
    _board_quote(page.core, page.moment, price=3900.0, pct_chg=0.5)
    r = page.client.get("/api/partials/indices")
    assert "实时" not in r.text
    assert "收盘" in r.text and "10-21" in r.text
    assert "3,900.00" not in r.text


def test_open_market_board_quote_wins_with_live_badge(logged_at):
    """盘中 + 报价够新：用板上的价并挂「实时」，不再提库里的收盘。"""
    page = logged_at(_moment(weekday=0, hour_utc=2))
    _seed_index(page.ctx, [100.0, 101.0])
    _board_quote(page.core, page.moment, price=3900.0, pct_chg=0.5)
    r = page.client.get("/api/partials/indices")
    assert "实时" in r.text
    assert "3,900.00" in r.text and "+0.50%" in r.text
    # 两个口径同屏会让读数的人猜哪个是现在的
    assert "收盘" not in r.text


def test_delayed_quote_shows_the_delayed_state_not_live(logged_at):
    """源自己标了延迟：盘中也不说「实时」，说「延迟」，价还是板上那一个。"""
    page = logged_at(_moment(weekday=0, hour_utc=2))
    _seed_index(page.ctx, [100.0, 101.0])
    _board_quote(page.core, page.moment, price=3900.0, pct_chg=0.5, delayed=True)
    r = page.client.get("/api/partials/indices")
    assert "延迟" in r.text
    assert "实时" not in r.text
    assert "3,900.00" in r.text
    assert "收盘" not in r.text


def test_stale_board_quote_falls_back_to_the_last_close(logged_at):
    """盘中但这一轮全失败（板子标了 stale）：落回库里的收盘，别挂旧价。"""
    page = logged_at(_moment(weekday=0, hour_utc=2))
    _seed_index(page.ctx, [100.0, 101.0])
    _board_quote(page.core, page.moment, price=3900.0)
    page.core.board.fail(MARKET, "源挂了", now=page.moment)  # fail 把全市场标 stale
    r = page.client.get("/api/partials/indices")
    assert "实时" not in r.text and "收盘" in r.text
    assert "3,900.00" not in r.text


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
