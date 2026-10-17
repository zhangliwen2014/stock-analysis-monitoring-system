from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.users import UserRepo

PASSWORD = "correct horse battery"


@pytest.fixture
def repo(db) -> SessionRepo:
    return SessionRepo(db)


@pytest.fixture
def user_id(db) -> str:
    return UserRepo(db).create("zhangsan", PASSWORD).id


def test_token_returned_once_and_only_hash_stored(db, repo, user_id):
    token = repo.issue(user_id)
    rows = db.query("SELECT token_hash, user_id FROM user_sessions").to_dicts()
    assert rows == [{"token_hash": rows[0]["token_hash"], "user_id": user_id}]
    assert token not in rows[0]["token_hash"], "库里出现的必须是哈希，不是明文"
    assert len(rows[0]["token_hash"]) == 64  # sha256 hex
    assert repo.verify(token) == user_id


def test_wrong_or_empty_token_is_rejected(repo):
    assert repo.verify("not-a-real-token") is None
    assert repo.verify("") is None


def test_revoked_token_fails(repo, user_id, db):
    token = repo.issue(user_id)
    repo.revoke(token)
    assert repo.verify(token) is None
    repo.revoke(token)  # 重复吊销不该报错也不影响状态
    assert repo.verify(token) is None


def test_expired_token_fails(repo, user_id, db):
    token = repo.issue(user_id)
    db.execute("UPDATE user_sessions SET expires_at = now() - INTERVAL 1 DAY")
    assert repo.verify(token) is None


def test_revoke_all_counts_only_active_sessions(db, repo, user_id):
    other = UserRepo(db).create("lisi", PASSWORD).id
    for _ in range(3):
        repo.issue(user_id)
    stale = repo.issue(user_id)
    repo.revoke(stale)
    repo.issue(other)

    assert repo.revoke_all(user_id) == 3
    assert repo.active_count(user_id) == 0
    assert repo.active_count(other) == 1


def test_ttl_must_be_positive(repo, user_id):
    with pytest.raises(ValueError, match="ttl_days"):
        repo.issue(user_id, ttl_days=0)


def test_expiry_follows_the_injected_clock(db, user_id):
    """过期判定靠注入的时钟，不靠墙钟也不靠 sleep：重放时同一条会话必须同判。"""
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    repo = SessionRepo(db, clock)
    token = repo.issue(user_id, ttl_days=1)
    assert repo.verify(token) == user_id

    clock.advance(days=2)
    assert repo.verify(token) is None
