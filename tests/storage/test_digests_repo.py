"""每日摘要的存储：一条 `(user, market, as_of)` 只写一次，投递结局写回同一行。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from support import FrozenClock

from sams.core.notify import FAILED, SENT
from sams.storage.repos.digests import DigestRepo

NOW = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)
DAY = date(2026, 10, 30)


@pytest.fixture
def repo(db):
    return DigestRepo(db, clock=FrozenClock(NOW))


def save(repo, **overrides):
    fields = {
        "user_id": "u1",
        "market": "A",
        "as_of": DAY,
        "title": "A · 2026-10-30 收盘摘要",
        "body": "【持仓】\n· 无变化",
        "has_changes": False,
        "heartbeat": True,
        "source_note": "",
    }
    fields.update(overrides)
    return repo.save(**fields)


def test_a_day_without_a_digest_is_none(repo):
    assert repo.get("u1", "A", DAY) is None


def test_save_then_get_round_trips_the_snapshot(db, repo):
    got, created = save(repo)
    assert created is True
    assert got.user_id == "u1"
    assert got.as_of == DAY
    assert got.title == "A · 2026-10-30 收盘摘要"
    assert got.body.startswith("【持仓】")
    assert got.has_changes is False
    assert got.heartbeat is True
    assert got.pushed is False
    assert got.delivery_outcome == ""
    assert got.created_at == NOW
    assert repo.get_by_id(got.id) == got


def test_saving_the_same_key_twice_keeps_the_first_body(db, repo):
    first, created = save(repo)
    later = DigestRepo(db, clock=FrozenClock(datetime(2026, 10, 31, 10, tzinfo=UTC)))
    again, created_again = save(later, body="【持仓】\n· 第二次")
    assert created is True
    assert created_again is False
    assert again.id == first.id
    assert again.body == first.body
    assert db.scalar("SELECT count(*) FROM digests") == 1


def test_two_users_and_markets_do_not_collide(db, repo):
    save(repo, user_id="u1", market="A")
    save(repo, user_id="u1", market="HK")
    save(repo, user_id="u2", market="A")
    assert db.scalar("SELECT count(*) FROM digests") == 3


def test_mark_delivery_records_the_outcome_and_the_moment(db, repo):
    got, _ = save(repo)
    repo.mark_delivery(got.id, SENT, error="")
    after = repo.get_by_id(got.id)
    assert after.pushed is True
    assert after.delivery_outcome == SENT
    assert after.delivery_error == ""
    assert after.delivered_at == NOW


def test_a_failed_delivery_is_kept_without_a_sent_moment(db, repo):
    got, _ = save(repo)
    repo.mark_delivery(got.id, FAILED, error="企微 errcode=40001")
    after = repo.get_by_id(got.id)
    assert after.pushed is True
    assert after.delivery_outcome == FAILED
    assert after.delivery_error == "企微 errcode=40001"
    assert after.delivered_at is None


def test_latest_per_market_returns_one_row_per_market(repo):
    save(repo, market="A", as_of=date(2026, 10, 29))
    save(repo, market="A", as_of=date(2026, 10, 30))
    save(repo, market="HK", as_of=date(2026, 10, 29))
    latest = repo.latest_per_market("u1")
    assert set(latest) == {"A", "HK"}
    assert latest["A"].as_of == date(2026, 10, 30)
    assert latest["HK"].as_of == date(2026, 10, 29)


def test_list_for_is_scoped_and_newest_first(repo):
    save(repo, user_id="u1", as_of=date(2026, 10, 29))
    save(repo, user_id="u1", as_of=date(2026, 10, 30))
    save(repo, user_id="u2", as_of=date(2026, 10, 30))
    rows = repo.list_for("u1")
    assert [row.as_of for row in rows] == [date(2026, 10, 30), date(2026, 10, 29)]


def test_count_for_counts_the_heartbeat_horizon(repo):
    assert repo.count_for("u1", "A") == 0
    save(repo, as_of=date(2026, 10, 29))
    save(repo, as_of=date(2026, 10, 30))
    save(repo, market="HK", as_of=date(2026, 10, 30))
    assert repo.count_for("u1", "A") == 2
