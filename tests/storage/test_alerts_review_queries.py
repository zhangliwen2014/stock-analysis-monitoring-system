"""复盘的四条只读查询：原始行、半开窗口、24 小时那道闸、带测试的排除。

这些都是 `/review` 那一屏替不掉的断言：窗口的半开区间、`is_test` 的排除、
未到点排后面——漂移了屏幕上的数照样像个数，只是是假的。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 北京 09:00，2026-10-25 是周日

CREATE_KW = {
    "rule_id": "r1",
    "rule_name": "止损线",
    "account_id": None,
    "account_name": "",
    "market": "A",
    "code": "600519",
    "symbol_name": "贵州茅台",
    "severity": "mid",
    "stage": "close",
    "value_text": "1500.00",
    "threshold_text": "1550.00",
    "action_note": "考虑减仓",
    "triggered_at": NOW,
}


@pytest.fixture
def clock():
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock):
    return AlertRepo(db, clock=clock)


@pytest.fixture
def seeded_user(db):
    return UserRepo(db).create("alice", "correct-horse-battery").id


def _mk(repo, user, *, at=NOW, **over):
    kw = dict(CREATE_KW)
    kw["triggered_at"] = at
    kw.update(over)
    return repo.create(user, **kw)


def test_list_window_is_half_open_and_carries_rule_name(repo, seeded_user):
    since = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
    until = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    first = _mk(repo, seeded_user, at=since)  # 恰在起点：含
    _mk(repo, seeded_user, at=since - timedelta(microseconds=1))
    _mk(repo, seeded_user, at=until)  # 恰在上界：排（与 `list_for` 同规）
    mine = _mk(repo, seeded_user, at=NOW)
    _mk(repo, "other", at=NOW)  # 别人的不进我的窗口
    rows = repo.list_window(seeded_user, since=since, until=until)
    assert [r["id"] for r in rows] == [first.id, mine.id]  # 升序
    assert rows[0]["rule_name"] == "止损线"  # 原始行直接带名字，页面不再查规则表


def test_list_window_excludes_test_alerts(repo, seeded_user):
    _mk(repo, seeded_user, is_test=True)
    real = _mk(repo, seeded_user)
    rows = repo.list_window(
        seeded_user, since=NOW - timedelta(days=1), until=NOW + timedelta(days=1)
    )
    # /settings 发的那条不属于任何一周的「新增」（D131）
    assert [r["id"] for r in rows] == [real.id]


def test_list_overdue_skips_fresh_and_sorts_not_yet_due_last(repo, seeded_user):
    old = _mk(repo, seeded_user, at=NOW - timedelta(hours=30))
    older = _mk(repo, seeded_user, at=NOW - timedelta(hours=40))
    _mk(repo, seeded_user, at=NOW - timedelta(hours=2))  # 不到 24h，不列
    deferred = _mk(repo, seeded_user, at=NOW - timedelta(hours=50))
    repo.defer(deferred.id, user_id=seeded_user, until=NOW + timedelta(hours=5))
    rows = repo.list_overdue(seeded_user, now=NOW)
    # 到点的按最老在前；让人说了「稍后」而还没到点的那条排最后——它仍算未处理
    assert [r["id"] for r in rows] == [older.id, old.id, deferred.id]


def test_count_overdue_is_the_full_number_behind_the_truncated_list(repo, seeded_user):
    for index in range(23):
        _mk(repo, seeded_user, at=NOW - timedelta(hours=25, minutes=index))
    assert len(repo.list_overdue(seeded_user, now=NOW)) == 20  # 清单只列 20
    assert repo.count_overdue(seeded_user, now=NOW) == 23  # 顶上那个数必须是全量


def test_count_open_counts_new_and_acknowledged_excluding_test(repo, seeded_user):
    first = _mk(repo, seeded_user)
    second = _mk(repo, seeded_user)
    repo.acknowledge(second.id, user_id=seeded_user)
    _mk(repo, seeded_user, is_test=True)
    repo.dismiss(first.id, user_id=seeded_user, code="expected")
    assert repo.count_open(seeded_user) == 1  # 只剩那条已读：终态与测试都不算


def test_list_overdue_excludes_terminal(repo, seeded_user):
    old = _mk(repo, seeded_user, at=NOW - timedelta(hours=30))
    repo.act(old.id, user_id=seeded_user, action_kind="watch")
    assert repo.list_overdue(seeded_user, now=NOW) == []
