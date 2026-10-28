"""图表偏好仓库：一行一人，保存即覆盖，清空即回到默认。"""

from datetime import UTC, datetime

from support import FrozenClock

from sams.storage.repos.chart_prefs import ChartPrefRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def users(db):
    repo = UserRepo(db)
    return repo.create("alice", "correct-horse"), repo.create("bob", "correct-horse")


def repo(db):
    return ChartPrefRepo(db, clock=FrozenClock(NOW))


def test_a_user_without_a_row_falls_back_to_the_defaults(db):
    alice, _ = users(db)
    assert repo(db).get(alice.id) is None


def test_saving_then_reading_back_keeps_the_order_and_the_windows(db):
    alice, _ = users(db)
    repo(db).save(alice.id, ("day", "week", "m1"), (5, 10, 20))
    saved = repo(db).get(alice.id)
    assert saved is not None
    assert saved.periods == ("day", "week", "m1")
    assert saved.windows == (5, 10, 20)
    assert saved.updated_at == NOW


def test_saving_twice_replaces_the_one_row(db):
    alice, _ = users(db)
    repo(db).save(alice.id, ("day",), (5,))
    repo(db).save(alice.id, ("week",), (7, 14))
    assert repo(db).get(alice.id).windows == (7, 14)
    assert db.scalar("SELECT count(*) FROM chart_prefs") == 1


def test_clear_drops_the_row_and_says_whether_there_was_one(db):
    alice, _ = users(db)
    repo(db).save(alice.id, ("day",), (5,))
    assert repo(db).clear(alice.id) is True
    assert repo(db).get(alice.id) is None
    assert repo(db).clear(alice.id) is False


def test_one_users_preference_is_invisible_to_another(db):
    alice, bob = users(db)
    repo(db).save(alice.id, ("day",), (5,))
    assert repo(db).get(bob.id) is None
