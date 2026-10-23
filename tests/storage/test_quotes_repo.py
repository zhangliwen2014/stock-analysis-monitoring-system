"""报价快照与报价源健康：重启回填、同秒覆盖、三态健康。

「同一只票取最近一笔」由窗口查询负责，不在读取侧把整张历史拉进内存——快照一天
就是几万行，而行情板只关心每只一行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb

from sams.core.quotes import Quote, QuoteSourceRecord
from sams.storage.db import Database
from sams.storage.repos.quotes import QuoteHealthRepo, QuoteRepo

T0 = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=10)


def quote(code="600519", *, ts=T0, price=1540.0, market="A", **extra) -> Quote:
    return Quote(
        market=market, code=code, ts=ts, price=price, source="tencent", **extra
    )


def test_save_roundtrips_every_price_column(db):
    repo = QuoteRepo(db)
    written = repo.save([
        quote(
            open=1525.0,
            prev_close=1530.0,
            high=1545.0,
            low=1520.0,
            pct_chg=0.65,
            volume=2_400_000.0,
            amount=3_612_345_678.0,
            bid1=1539.9,
            ask1=1540.1,
            status="交易中",
        )
    ])
    assert written == 1
    stored = repo.latest()[0]
    assert stored.price == 1540.0
    assert (stored.open, stored.prev_close, stored.high, stored.low) == (
        1525.0, 1530.0, 1545.0, 1520.0,
    )
    assert stored.pct_chg == 0.65
    assert stored.volume == 2_400_000.0 and stored.amount == 3_612_345_678.0
    assert stored.bid1 == 1539.9 and stored.ask1 == 1540.1
    assert stored.status == "交易中"
    assert stored.ts == T0 and stored.source == "tencent"
    assert stored.is_stale is False


def test_latest_keeps_one_row_per_code_at_its_newest_time(db):
    repo = QuoteRepo(db)
    repo.save([
        quote(ts=T0, price=100.0),
        quote(ts=T1, price=101.0),
        quote("000001", ts=T0, price=10.0),
    ])
    rows = repo.latest()
    assert [(q.code, q.price) for q in rows] == [("000001", 10.0), ("600519", 101.0)]


def test_latest_can_be_narrowed_to_one_market(db):
    repo = QuoteRepo(db)
    repo.save([quote(), quote("AAPL", market="US", price=230.0)])
    assert [q.code for q in repo.latest(market="US")] == ["AAPL"]


def test_same_code_and_timestamp_replaces_instead_of_duplicating(db):
    repo = QuoteRepo(db)
    repo.save([quote(price=100.0)])
    repo.save([quote(price=101.0)])
    assert db.row_count("quotes_snapshot") == 1
    assert repo.latest()[0].price == 101.0


def test_empty_batch_is_a_noop(db):
    assert QuoteRepo(db).save([]) == 0
    assert db.row_count("quotes_snapshot") == 0


def test_rows_without_a_usable_price_are_skipped_on_read(db):
    """旧快照可能留下 price 为 0 或 NULL 的行（0001 的列可空）。

    行情板启动时读这张表：一行坏数据不该让整个看板起不来，也不该把 0 当成报价。
    """
    db.execute(
        "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
        " VALUES ('A', 'bad', ?, 0.0, 'tencent')",
        [T0],
    )
    db.execute(
        "INSERT INTO quotes_snapshot (market, code, ts, price, source)"
        " VALUES ('A', 'null', ?, NULL, 'tencent')",
        [T0],
    )
    QuoteRepo(db).save([quote()])
    assert [q.code for q in QuoteRepo(db).latest()] == ["600519"]


def test_quote_health_roundtrips_three_states(db):
    repo = QuoteHealthRepo(db)
    repo.save(
        QuoteSourceRecord(
            name="tencent",
            markets=("A", "HK", "US"),
            ok=True,
            last_ok_at=T1,
            consecutive_failures=0,
        )
    )
    repo.save(QuoteSourceRecord(name="sina_quote", markets=("A",), ok=None))
    repo.save(
        QuoteSourceRecord(
            name="dead",
            markets=("A",),
            ok=False,
            last_error="timeout",
            consecutive_failures=3,
        )
    )
    loaded = repo.load()
    assert loaded["tencent"].ok is True
    assert loaded["tencent"].markets == ("A", "HK", "US")
    assert loaded["tencent"].last_ok_at == T1
    assert loaded["sina_quote"].ok is None, "NULL 是「从没被路由到」，不是 False"
    assert loaded["dead"].ok is False
    assert loaded["dead"].last_error == "timeout"
    assert loaded["dead"].consecutive_failures == 3


def test_health_reads_are_quiet_without_the_table():
    """0019 之前的老库：健康页要能出得来，这一节就是空的。"""
    database = Database(duckdb.connect(":memory:"))
    try:
        assert QuoteHealthRepo(database).load() == {}
    finally:
        database.close()
