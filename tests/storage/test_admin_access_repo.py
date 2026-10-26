"""`admin_access_log` 仓库（D139）：授权行只进不出，到期靠比时间。"""

from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.storage.repos.admin_access import AdminAccessRepo

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock):
    return AdminAccessRepo(db, clock=clock)


def test_grant_writes_an_active_row(repo):
    log = repo.grant(
        admin_user_id="adm", annotation_id="ann-1", owner_user_id="own",
        reason="核对除权口径", granted_until=NOW + timedelta(minutes=15),
    )
    assert log.action == "break_glass"
    got = repo.active("adm", "ann-1", now=NOW)
    assert got is not None and got.id == log.id


def test_naive_until_is_refused(repo):
    with pytest.raises(Exception, match="带时区"):
        repo.grant(
            admin_user_id="adm", annotation_id="ann-2", owner_user_id="own",
            reason="裸时间", granted_until=datetime(2026, 10, 26, 9, 15),
        )


def test_expired_grant_is_not_active(repo):
    repo.grant(
        admin_user_id="adm", annotation_id="ann-3", owner_user_id="own",
        reason="核对", granted_until=NOW + timedelta(minutes=15),
    )
    assert repo.active("adm", "ann-3", now=NOW + timedelta(minutes=20)) is None


def test_active_is_per_admin(repo):
    repo.grant(
        admin_user_id="adm-1", annotation_id="ann-4", owner_user_id="own",
        reason="核对", granted_until=NOW + timedelta(minutes=15),
    )
    assert repo.active("adm-2", "ann-4", now=NOW) is None


def test_count_since_window(repo):
    for n in range(3):
        repo.grant(
            admin_user_id="adm", annotation_id=f"ann-{100 + n}", owner_user_id="own",
            reason="核对", granted_until=NOW + timedelta(minutes=15),
        )
    assert repo.count_since("adm", since=NOW - timedelta(hours=1)) == 3
    assert repo.count_since("adm", since=NOW + timedelta(minutes=1)) == 0


def test_list_recent_newest_first(repo, clock):
    for n in range(3):
        repo.grant(
            admin_user_id="adm", annotation_id=f"ann-{200 + n}", owner_user_id="own",
            reason="核对",
            granted_until=NOW + timedelta(minutes=15),
        )
        clock.advance(minutes=1)
    rows = repo.list_recent(limit=2)
    assert [r.annotation_id for r in rows] == ["ann-202", "ann-201"]


def test_oldest_in_window_pins_the_first_grant_made(repo, clock):
    """等待秒数的唯一钟点源：窗口内**最早发起**那条的 created_at（不是 granted_until 的
    min——那答的是「最早什么时候到期」，不是「名额什么时候腾出来」）。"""
    assert repo.oldest_in_window("adm", since=NOW - timedelta(hours=1)) is None
    repo.grant(
        admin_user_id="adm", annotation_id="ann-o", owner_user_id="own",
        reason="核对", granted_until=NOW + timedelta(minutes=15),
    )
    clock.advance(minutes=30)
    repo.grant(
        admin_user_id="adm", annotation_id="ann-o2", owner_user_id="own",
        reason="核对", granted_until=clock.now_utc() + timedelta(minutes=15),
    )
    assert repo.oldest_in_window("adm", since=NOW) == NOW
    # 第一条滑出窗口后（since 落在两条之间），最早的就是第二条
    assert repo.oldest_in_window("adm", since=NOW + timedelta(minutes=1)) == NOW + timedelta(
        minutes=30
    )
