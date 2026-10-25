"""`alert_events`：闭环动作的不可变历史。

这张表回答的是「谁在什么时候把它从什么改成了什么」。它不参与当前状态的读取——
`alerts` 上的列才是结论——所以这里的断言只看写入形状与读取次序。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.alert_events import ACTIONS, AlertEventRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def test_add_returns_the_row_and_stamps_the_clock(db, clock):
    repo = AlertEventRepo(db, clock=clock)
    event = repo.add(
        "a1", user_id="u1", action="ack", from_status="new", to_status="acknowledged"
    )
    assert event.ts == NOW
    assert event.note == ""
    assert repo.list_for("a1") == [event]


def test_unknown_action_is_refused(db):
    with pytest.raises(ValidationError, match="action"):
        AlertEventRepo(db).add(
            "a1", user_id="u1", action="look", from_status="new", to_status="new"
        )


def test_actions_are_read_in_time_order(db, clock):
    repo = AlertEventRepo(db, clock=clock)
    repo.add("a1", user_id="u1", action="ack", from_status="new", to_status="acknowledged")
    clock.advance(seconds=5)
    repo.add(
        "a1", user_id="u1", action="act", from_status="acknowledged", to_status="handled",
        note="挂了条件单",
    )
    rows = repo.list_for("a1")
    assert [r.action for r in rows] == ["ack", "act"]
    assert rows[1].note == "挂了条件单"


def test_last_can_narrow_to_the_terminal_actions(db, clock):
    repo = AlertEventRepo(db, clock=clock)
    repo.add("a1", user_id="u1", action="ack", from_status="new", to_status="acknowledged")
    repo.add(
        "a1", user_id="u1", action="dismiss", from_status="acknowledged", to_status="dismissed"
    )
    assert repo.last("a1").action == "dismiss"
    assert repo.last("a1", actions=("act", "dismiss")).action == "dismiss"
    assert repo.last("a1", actions=("act",)) is None
    assert repo.last("ghost") is None


def test_the_action_vocabulary_is_closed(db):
    assert ACTIONS == ("ack", "defer", "act", "dismiss", "undo")
