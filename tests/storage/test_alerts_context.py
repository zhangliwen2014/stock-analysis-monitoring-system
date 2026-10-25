"""`notify_log.context_json`：这一次发送**实际**带入了哪些批注（D126）。

列是 0020 加的，可空。这里钉的是仓库层的三件事：给了就写、没给是 NULL、
`ensure_ascii=False` 让审计行在 DuckDB Studio 里读得出来——排查「是不是上下文太长被剪」
的人看的就是这一列。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.users import UserRepo

T0 = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
PASSWORD = "correct-horse-battery"


@pytest.fixture
def repo(db) -> AlertRepo:
    return AlertRepo(db)


@pytest.fixture
def user_id(db) -> str:
    users = UserRepo(db)
    users.create("trader", PASSWORD)
    return users.get_by_username("trader").id


def _alert(repo: AlertRepo, user_id: str, **over):
    kw: dict = {
        "rule_id": "rule-1",
        "rule_name": "PR 低",
        "account_id": None,
        "account_name": "主账户",
        "market": "A",
        "code": "601088",
        "symbol_name": "中国神华",
        "severity": "mid",
        "stage": "入池",
        "value_text": "PR=0.31",
        "threshold_text": "PR≤0.4",
        "action_note": "",
        "triggered_at": T0,
    }
    kw.update(over)
    return repo.create(user_id, **kw)


def _context_column(db, alert_id: str) -> list[object]:
    rows = db.query(
        "SELECT context_json FROM notify_log WHERE alert_id = ? ORDER BY ts, id", [alert_id]
    )
    return [row["context_json"] for row in rows.to_dicts()]


def test_a_context_lands_as_json_on_the_delivery_row(repo, user_id, db) -> None:
    alert = _alert(repo, user_id)
    channel_id = "chan-1"
    repo.record(
        alert.id,
        channel_id,
        "sent",
        context={"ids": ["ann-1", "ann-2"], "dropped": 0, "rendered_chars": 42},
    )
    raw = _context_column(db, alert.id)[0]
    assert json.loads(raw) == {"ids": ["ann-1", "ann-2"], "dropped": 0, "rendered_chars": 42}


def test_no_context_stays_null_not_an_empty_string(repo, user_id, db) -> None:
    """NULL 与 `''` 必须分得开：前者是「这一行没记上下文这件事」，后者是坏了的行。"""
    alert = _alert(repo, user_id)
    repo.record(alert.id, None, "no_channel", error="没有启用的通道")
    assert _context_column(db, alert.id) == [None]


def test_cjk_in_the_context_is_written_not_escaped(repo, user_id, db) -> None:
    alert = _alert(repo, user_id)
    repo.record(alert.id, "chan-1", "sent", context={"note": "中文原因"})
    raw = _context_column(db, alert.id)[0]
    assert "中文原因" in raw


def test_each_channel_row_carries_its_own_context(repo, user_id, db) -> None:
    """每个通道的投递行各记一份（§7.2）：同一批批注在不同通道可能被剪成不同样子。"""
    alert = _alert(repo, user_id)
    repo.record(
        alert.id, "chan-1", "sent", context={"ids": ["a"], "dropped": 0, "rendered_chars": 5}
    )
    repo.record(alert.id, "chan-2", "sent", context={"ids": [], "dropped": 1, "rendered_chars": 0})
    first, second = _context_column(db, alert.id)
    assert json.loads(first)["ids"] == ["a"]
    assert json.loads(second)["dropped"] == 1
