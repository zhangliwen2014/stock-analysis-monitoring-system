"""筛选方案：per-user 的一张表，名字在一个人手里唯一（D182）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition
from sams.storage.repos.screener_presets import ScreenerPresetRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
CONDITIONS = [
    Condition("pr_now", "<=", 0.5),
    Condition("turnover_rate", ">=", 1.0),
]


def users(db):
    repo = UserRepo(db)
    alice = repo.create("alice", "correct-horse")
    bob = repo.create("bob", "correct-horse")
    return alice, bob


def repo(db, clock=None):
    return ScreenerPresetRepo(db, clock=clock or FrozenClock(NOW))


def test_create_reads_back_the_conditions_and_the_times(db):
    alice, _ = users(db)
    saved = repo(db).create(alice.id, "便宜", "a", CONDITIONS)
    assert saved.name == "便宜" and saved.market == "A"
    assert saved.conditions == tuple(CONDITIONS)
    assert saved.created_at == NOW and saved.updated_at == NOW
    got = repo(db).get(alice.id, saved.id)
    assert got == saved


def test_between_survives_the_json_round_trip(db):
    alice, _ = users(db)
    saved = repo(db).create(alice.id, "区间", "A", [Condition("price", "between", (1.0, 5.0))])
    expected = (Condition("price", "between", (1.0, 5.0)),)
    assert repo(db).get(alice.id, saved.id).conditions == expected


def test_the_same_name_is_unique_within_one_user_but_not_across_users(db):
    alice, bob = users(db)
    repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    with pytest.raises(ValidationError, match="已有同名方案"):
        repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    bob_saved = repo(db).create(bob.id, "便宜", "A", CONDITIONS)
    assert repo(db).list(bob.id)[0].id == bob_saved.id


def test_empty_conditions_are_refused_because_that_means_the_whole_market(db):
    alice, _ = users(db)
    with pytest.raises(ValidationError, match="一条条件都没有"):
        repo(db).create(alice.id, "空", "A", [])


def test_another_users_preset_is_invisible(db):
    alice, bob = users(db)
    saved = repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    assert repo(db).get(bob.id, saved.id) is None
    assert repo(db).delete(bob.id, saved.id) is False
    assert repo(db).get(alice.id, saved.id) is not None


def test_rename_moves_updated_at_but_not_created_at(db):
    alice, _ = users(db)
    clock = FrozenClock(NOW)
    repo(db, clock).create(alice.id, "便宜", "A", CONDITIONS)
    clock.advance(hours=1)
    saved = repo(db).list(alice.id)[0]
    assert repo(db, clock).rename(alice.id, saved.id, "很便宜")
    got = repo(db).get(alice.id, saved.id)
    assert (got.name, got.created_at, got.updated_at) == ("很便宜", NOW, clock.moment)


def test_rename_onto_another_name_is_refused_but_onto_itself_is_fine(db):
    alice, _ = users(db)
    first = repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    repo(db).create(alice.id, "更便宜", "A", CONDITIONS)
    with pytest.raises(ValidationError, match="已有同名方案"):
        repo(db).rename(alice.id, first.id, "更便宜")
    assert repo(db).rename(alice.id, first.id, "便宜")


def test_overwriting_conditions_keeps_the_name(db):
    alice, _ = users(db)
    clock = FrozenClock(NOW)
    saved = repo(db, clock).create(alice.id, "便宜", "A", CONDITIONS)
    clock.advance(minutes=5)
    assert repo(db, clock).update_conditions(
        alice.id, saved.id, [Condition("pr_now", "<=", 0.3)]
    )
    got = repo(db).get(alice.id, saved.id)
    assert got.name == "便宜" and got.conditions == (Condition("pr_now", "<=", 0.3),)
    assert got.updated_at == clock.moment


def test_delete_removes_one_row_only(db):
    alice, _ = users(db)
    saved = repo(db).create(alice.id, "便宜", "A", CONDITIONS)
    assert repo(db).delete(alice.id, saved.id) is True
    assert repo(db).delete(alice.id, saved.id) is False
    assert repo(db).list(alice.id) == []
