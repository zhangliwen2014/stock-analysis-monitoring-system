"""指数日线：prev_close/change_pct 入库即算，增量行能接上存量行（D145）。"""

from datetime import date

import polars as pl

from sams.storage.repos.index_daily import IndexDailyRepo


def _frame(dates: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": ["A"] * len(dates),
            "index_code": ["sh000001"] * len(dates),
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * len(dates),
            "amount": [1.0] * len(dates),
            "source": ["tencent"] * len(dates),
        }
    )


def test_prev_close_chain_within_one_upsert(db):
    repo = IndexDailyRepo(db)
    written = repo.upsert(_frame([date(2026, 10, 20), date(2026, 10, 21)], [100.0, 101.0]))
    assert written == 2
    latest = repo.latest_with_change("A", "sh000001")
    assert latest.close == 101.0
    assert latest.change_pct == 1.0
    assert latest.day == date(2026, 10, 21)
    # 首日没有前收：prev_close 与 change_pct 都是 NULL，不是 0
    first = db.query(
        "SELECT prev_close, change_pct FROM index_daily WHERE date = ?",
        [date(2026, 10, 20)],
    ).to_dicts()[0]
    assert first["prev_close"] is None and first["change_pct"] is None


def test_incremental_upsert_sees_stored_rows(db):
    repo = IndexDailyRepo(db)
    repo.upsert(_frame([date(2026, 10, 20)], [100.0]))
    repo.upsert(_frame([date(2026, 10, 21)], [102.0]))
    assert repo.latest_with_change("A", "sh000001").change_pct == 2.0
    assert repo.latest_date("A", "sh000001") == date(2026, 10, 21)


def test_latest_missing_is_none(db):
    repo = IndexDailyRepo(db)
    assert repo.latest_with_change("A", "sh000001") is None
    assert repo.latest_date("A", "sh000001") is None
