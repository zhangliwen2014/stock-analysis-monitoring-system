"""一条去重键上的当前状态：跃迁去重的唯一持久化事实。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.alert_states import AlertStateRepo

NOW = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)
BAR = date(2026, 10, 30)
KEY = {
    "user_id": "u1",
    "rule_id": "r1",
    "market": "A",
    "code": "600000",
    "account_id": None,
}


@pytest.fixture
def repo(db):
    return AlertStateRepo(db, clock=FrozenClock(NOW))


def test_a_key_without_a_row_has_no_state(repo):
    assert repo.get(**KEY) is None


def test_set_then_get_round_trips_the_stage_and_the_bar(repo):
    repo.set(**KEY, state="hit", bar_ts=BAR, value_text="9.10")
    got = repo.get(**KEY)
    assert got is not None
    assert got.state == "hit"
    assert got.bar_ts == BAR
    assert got.value_text == "9.10"
    assert got.updated_at == NOW
    assert got.account_id is None


def test_set_twice_updates_the_one_row(repo, db):
    repo.set(**KEY, state="pre", bar_ts=BAR)
    repo.set(**KEY, state="hit", bar_ts=BAR)
    assert db.scalar("SELECT count(*) FROM alert_states") == 1
    assert repo.get(**KEY).state == "hit"


def test_two_accounts_on_the_same_symbol_keep_separate_states(repo):
    repo.set(**KEY, state="hit", bar_ts=BAR)
    other = {**KEY, "account_id": "acc1"}
    repo.set(**other, state="pre", bar_ts=BAR)
    assert repo.get(**KEY).state == "hit"
    assert repo.get(**other).state == "pre"


def test_clear_only_touches_a_non_clear_state_and_keeps_the_bar(repo):
    repo.set(**KEY, state="hit", bar_ts=BAR)
    assert repo.clear(**KEY) is True
    got = repo.get(**KEY)
    assert got.state == "clear"
    # 证据不因状态清零而丢：它仍是最后一根判过的 bar
    assert got.bar_ts == BAR
    # 已经是 clear：不写
    assert repo.clear(**KEY) is False


def test_clear_on_a_missing_key_is_a_no_op(repo):
    assert repo.clear(**KEY) is False


def test_unknown_stage_is_rejected(repo):
    with pytest.raises(ValidationError, match="state"):
        repo.set(**KEY, state="maybe", bar_ts=BAR)


def test_one_users_state_is_invisible_to_another(repo):
    repo.set(**KEY, state="hit", bar_ts=BAR)
    assert repo.get(**{**KEY, "user_id": "u2"}) is None
