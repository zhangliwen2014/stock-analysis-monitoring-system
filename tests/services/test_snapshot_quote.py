"""apply_quote：盖的是报价给的，留的是日线有的；缺现价那句在盖完之后必须消失。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sams.core.quotes import Quote
from sams.services.rules.snapshot import NO_PRICE_MISSING, Snapshot, apply_quote

ZONE = ZoneInfo("Asia/Shanghai")
TS = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def snap(**over):
    base = {
        "market": "A",
        "code": "601088",
        "price": 39.0,
        "price_on": date(2026, 10, 22),
        "price_age_days": 1,
        "prev_close": 40.0,
        "holding_high": 45.0,
        "missing": (NO_PRICE_MISSING,),
    }
    base.update(over)
    return Snapshot(**base)


def quote(**over):
    base = {
        "market": "A",
        "code": "601088",
        "ts": TS,
        "price": 38.2,
        "open": 38.8,
        "prev_close": 39.5,
        "high": 39.1,
        "source": "tencent",
    }
    base.update(over)
    return Quote(**base)


def test_the_quote_fields_win_and_the_day_follows_the_market():
    got = apply_quote(snap(), quote=quote(), previous=None, zone=ZONE)
    assert got.price == 38.2
    assert got.price_on == date(2026, 10, 23)  # UTC 03:00 的上海日
    assert got.price_age_days == 0
    assert got.day_open == 38.8
    assert got.prev_close == 39.5  # 报价给了就盖：urgent 的跳空判据要真昨收


def test_fields_the_source_did_not_give_keep_the_daily_value():
    got = apply_quote(snap(), quote=quote(open=None, prev_close=None), previous=None, zone=ZONE)
    assert got.day_open is None  # 日线本来就没有当日开盘
    assert got.prev_close == 40.0


def test_holding_high_extends_but_never_invents():
    assert apply_quote(snap(), quote=quote(), previous=None, zone=ZONE).holding_high == 45.0
    no_line = snap(holding_high=None)
    assert apply_quote(no_line, quote=quote(), previous=None, zone=ZONE).holding_high is None


def test_the_no_price_note_is_stripped_once_a_quote_arrives():
    got = apply_quote(snap(), quote=quote(), previous=None, zone=ZONE)
    assert got.missing == ()


def test_cross_from_comes_from_the_previous_round_of_the_same_day():
    previous = quote(price=39.9, ts=datetime(2026, 10, 23, 2, 0, tzinfo=UTC))
    got = apply_quote(snap(), quote=quote(), previous=previous, zone=ZONE)
    assert got.cross_from == 39.9
    yesterday = quote(price=39.9, ts=datetime(2026, 10, 22, 6, 0, tzinfo=UTC))
    got = apply_quote(snap(), quote=quote(), previous=yesterday, zone=ZONE)
    assert got.cross_from is None


def test_without_a_zone_nothing_cross_related_is_set():
    got = apply_quote(snap(), quote=quote(), previous=None, zone=None)
    assert got.cross_from is None
    assert got.price_on == TS.date()  # 没有市场时区就按 UTC 日兜底


def test_the_original_snapshot_is_untouched():
    before = snap()
    apply_quote(before, quote=quote(), previous=None, zone=ZONE)
    assert before.price == 39.0 and before.missing == (NO_PRICE_MISSING,)
