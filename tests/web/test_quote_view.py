"""统一报价读模型（S0-E）：休市回落库内最后收盘，盘中只有过闸的报价才算数。

这里的时刻都写死：周中开盘/周六休市两种状态必须各有一条用例钉住，
否则「同一页面在周六显示的是周五哪一刻的价」只能靠运气。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from fastapi.testclient import TestClient
from support import FrozenClock

from sams.core.quotes import Quote
from sams.datasource.spot import SPOT_SCHEMA
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo
from sams.web.quotes import quote_reader

#: 2026-10-30 是周五；02:00 UTC = 北京 10:00，替身日历的 09:30–15:00 开着
OPEN = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)
#: 次日周六同一时刻：市场休市
CLOSED = datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
#: 周五北京 14:47（UTC 06:47）：盘中被最后一拍记下的那个价
AT_1447 = datetime(2026, 10, 30, 6, 47, 13, tzinfo=UTC)


def _seed_bars(context, code="600519", *, closes=(1390.0, 1392.05)):
    days = [date(2026, 10, 29), date(2026, 10, 30)][-len(closes) :]
    context.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "code": [code] * len(closes),
                "date": days,
                "open": list(closes),
                "high": list(closes),
                "low": list(closes),
                "close": list(closes),
                "volume": [1.0] * len(closes),
                "amount": [1.0] * len(closes),
                "source": ["akshare"] * len(closes),
            }
        )
    )


def _push_quote(core, *, price=1390.46, ts=AT_1447, pct=0.5, source="tencent"):
    core.board.update(
        "A",
        [
            Quote(
                market="A",
                code="600519",
                ts=ts,
                price=price,
                pct_chg=pct,
                source=source,
            )
        ],
        now=ts,
        error=None,
    )


def test_a_closed_market_falls_back_to_the_last_raw_close_and_says_which_day(web_app):
    """周六看一眼：板子上还留着周五 14:47 的价，但那不是收盘。

    统一口径回落库内最后一根不复权日线，并把「哪一天、哪一种价、哪个源」一起给出来。
    """
    _seed_bars(web_app.context)
    _push_quote(web_app.core)

    view = quote_reader(web_app.context, web_app.core, CLOSED).view("A", "600519")

    assert view.price == pytest.approx(1392.05)
    assert view.kind == "close"
    assert view.state == "closed"
    assert view.day == date(2026, 10, 30)
    assert view.label == "10-30 收盘"
    assert view.detail == "不复权 · akshare"
    assert view.pct == pytest.approx((1392.05 / 1390.0 - 1) * 100)
    assert view.currency == "CNY" and view.unit == "元"
    assert view.adjustment == "raw"


def test_an_open_market_with_a_fresh_quote_keeps_the_intraday_price(web_app):
    """盘中不回落日线：那一刻图上的收盘还没发生，拿它当现价是把一天说成已经走完。"""
    _seed_bars(web_app.context)
    _push_quote(web_app.core, ts=OPEN)

    view = quote_reader(web_app.context, web_app.core, OPEN).view("A", "600519")

    assert view.price == pytest.approx(1390.46)
    assert view.kind == "live"
    assert view.state == "live"
    assert view.day == date(2026, 10, 30)
    assert view.label == "10:00 盘中"
    assert view.detail == "腾讯"
    assert view.at == OPEN


def test_a_stale_quote_with_no_bars_is_labelled_as_the_old_price(web_app):
    """库里还没有日线（刚加自选、补数还没跑）：这一格只能给板上那口旧价。

    但「旧价」两个字与它的日子时刻必须写在屏幕上，不能冒充现价或收盘。
    """
    _push_quote(web_app.core)

    view = quote_reader(web_app.context, web_app.core, CLOSED).view("A", "600519")

    assert view.price == pytest.approx(1390.46)
    assert view.kind == "stale"
    assert view.label == "10-30 14:47 旧价"
    assert view.detail == "腾讯"


def test_no_quote_and_no_bars_is_no_price_not_zero(web_app):
    """没有价就是 `—`，绝不写 0：0 会被读成一只跌到零的票，而这里只是没数。

    `state` 仍如实报市场此刻是休市——「今天不开市」与「开市了但没数」是两句话。
    """
    view = quote_reader(web_app.context, web_app.core, CLOSED).view("A", "600519")

    assert view.price is None
    assert view.kind == "none"
    assert view.state == "closed"
    assert view.label == "—"


def test_units_follow_the_instrument_not_only_the_market(web_app):
    """沪市 B 股以美元报价：同一市场上「元」与「美元」不能混成一个单位。"""
    reader = quote_reader(web_app.context, web_app.core, CLOSED)
    assert reader.view("A", "900901").currency == "USD"
    assert reader.view("A", "900901").unit == "美元"
    assert reader.view("A", "600519").unit == "元"


def test_views_reads_a_batch_and_keeps_missing_rows_out(web_app):
    _seed_bars(web_app.context, "600519")
    _seed_bars(web_app.context, "000001", closes=(10.0, 10.2))
    views = quote_reader(web_app.context, web_app.core, CLOSED).views(
        "A", ["600519", "000001", "300750"]
    )
    assert set(views) == {"600519", "000001", "300750"}
    assert views["600519"].price == pytest.approx(1392.05)
    assert views["000001"].price == pytest.approx(10.2)
    assert views["300750"].price is None


#: 快照那一行的口径：`/screen` 按快照出结果，价仍由统一读模型给
SNAP_DAY = date(2026, 10, 30)
SNAP_TAKEN = datetime(2026, 10, 30, 8, 41, tzinfo=UTC)


def _seed_snapshot(ctx, price=1392.05):
    SymbolListRepo(ctx.db).upsert_batch(
        "A",
        [
            {
                "code": "600519",
                "name": "贵州茅台",
                "py_initials": "gzym",
                "py_full": "guizhoumaotai",
                "currency": "CNY",
            }
        ],
    )
    row = {
        "code": "600519",
        "name": "贵州茅台",
        "quoted_on": SNAP_DAY,
        "price": price,
        "pct_chg": 0.15,
        "amount": 1e8,
        "turnover_rate": 1.0,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": 30.0,
        "pe_dyn": 30.0,
        "pb": 8.0,
        "market_cap": 1.7e12,
        "float_cap": 1.7e12,
        "is_st": False,
        "source": "tencent",
    }
    frame = pl.DataFrame(
        [{"market": "A", "taken_at": SNAP_TAKEN, **row}],
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(ctx.db).replace_day(market="A", day=SNAP_DAY, taken_at=SNAP_TAKEN, frame=frame)


def test_saturday_shows_the_same_price_and_same_point_in_time_on_all_five_pages(make_app):
    """S0-E 的验收：周六打开五个页面，同一只票逐字同价，并都读得出「10-30 收盘」。

    行情板上还留着周五 14:47 的价（1390.46），快照那份是 1392.05；`/screen` 原先
    显示快照价、看板显示板上的价——同一只票两屏两个数。现在五个页面都回落库内
    最后一根不复权日线：同一个 1,392.05，同一句「10-30 收盘」。
    """
    web = make_app(clock=FrozenClock(CLOSED))
    ctx = web.contexts.current()
    ctx.users.create("alice", "correct-horse-battery", is_admin=True)
    user = ctx.users.get_by_username("alice")
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["600519"],
                "name": ["贵州茅台"],
                "currency": ["CNY"],
            }
        )
    )
    ctx.watchlists.add(user.id, "A", "600519")
    _seed_bars(ctx)
    _push_quote(web.core)
    _seed_snapshot(ctx)
    account = ctx.ledger.open_account("alice", "测试户")
    ctx.ledger.deposit(
        account.id, "CNY", 200_000.0, username="alice", reason="起始资金", actor_id=user.id
    )
    ctx.ledger.buy(account.id, "alice", "A", "600519", qty=100, price=100.0)
    ctx.rule_service.add(
        user.id,
        kind="price.cross_below",
        name="跌破 1200",
        target_type="symbol",
        target_ref="A/600519",
        params={"price": 1200.0},
    )

    with TestClient(web.app) as client:
        login = client.post(
            "/login",
            data={"username": "alice", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        screen = client.get(
            "/screen",
            params=[("market", "A"), ("f", "price"), ("o", "<="), ("v", "2000")],
        )
        pages = {
            "/": client.get("/").text,
            "/symbol/A/600519": client.get("/symbol/A/600519").text,
            "/screen": screen.text,
            "/portfolio": client.get("/portfolio").text,
            "/rules": client.get("/rules").text,
        }

    for path, text in pages.items():
        assert "1,392.05" in text, f"{path} 没有那根收盘价"
        assert "10-30 收盘" in text, f"{path} 没有说清是哪一天的收盘"
        assert "1,390.46" not in text, f"{path} 还在拿周五 14:47 的盘中价冒充收盘"
