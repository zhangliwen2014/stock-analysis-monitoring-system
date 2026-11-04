"""单票静音按类别分组（D249）：`all / rule / news / digest`，键是（用户, 市场, 代码）。

静音仍然讲「这只票」，只是不再一按全静。旧表 `alert_mutes` 的行按 `all` 读，
解除「全部」时顺手删掉——否则迁移前静掉的票永远静着，按钮看似按了却没处落。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.alert_mutes import MuteRepo

NOW = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)


def repo(db):
    return MuteRepo(db, clock=FrozenClock(NOW))


def test_a_symbol_without_a_mute_is_not_muted(db):
    assert repo(db).get("u1", "A", "600000") is None


def test_add_then_get_returns_the_note_and_the_moment(db):
    got = repo(db).add("u1", "A", "600000", note="等财报")
    assert got.note == "等财报"
    assert got.created_at == NOW
    assert repo(db).get("u1", "A", "600000") == got


def test_adding_twice_keeps_one_row_and_the_first_moment(db):
    first = repo(db).add("u1", "A", "600000")
    later = MuteRepo(db, clock=FrozenClock(datetime(2026, 11, 1, tzinfo=UTC)))
    again = later.add("u1", "A", "600000", note="第二次")
    assert again.created_at == first.created_at
    assert db.scalar("SELECT count(*) FROM alert_mute_scopes") == 1


def test_remove_says_whether_a_row_was_there(db):
    repo(db).add("u1", "A", "600000")
    assert repo(db).remove("u1", "A", "600000") is True
    assert repo(db).remove("u1", "A", "600000") is False


def test_list_for_is_scoped_to_one_user(db):
    repo(db).add("u1", "A", "600000")
    repo(db).add("u1", "HK", "00700")
    repo(db).add("u2", "A", "600519")
    assert {(m.market, m.code) for m in repo(db).list_for("u1")} == {
        ("A", "600000"),
        ("HK", "00700"),
    }
    assert [(m.market, m.code) for m in repo(db).list_for("u2")] == [("A", "600519")]


def test_a_news_mute_does_not_block_rules(db):
    repo(db).add("u1", "A", "600000", kind="news", note="资讯别推")

    assert repo(db).get("u1", "A", "600000", kind="news") is not None
    assert repo(db).get("u1", "A", "600000", kind="rule") is None
    assert repo(db).get("u1", "A", "600000", kind="all") is None


def test_an_all_mute_covers_every_kind(db):
    repo(db).add("u1", "A", "600000", kind="all")

    for kind in ("all", "rule", "news", "digest"):
        assert repo(db).get("u1", "A", "600000", kind=kind) is not None


def test_a_legacy_row_still_reads_as_all(db):
    db.execute(
        "INSERT INTO alert_mutes (user_id, market, code, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ["u1", "A", "600000", "迁过来的", NOW],
    )

    for kind in ("all", "rule", "news", "digest"):
        got = repo(db).get("u1", "A", "600000", kind=kind)
        assert got is not None and got.kind == "all" and got.note == "迁过来的"
    assert [(m.kind, m.note) for m in repo(db).list_for("u1")] == [("all", "迁过来的")]


def test_unmuting_all_also_clears_the_legacy_row(db):
    """旧行只读，但「取消静音」必须真的解得开：留着它，按钮就是假的。"""
    db.execute(
        "INSERT INTO alert_mutes (user_id, market, code, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ["u1", "A", "600000", "", NOW],
    )

    assert repo(db).remove("u1", "A", "600000") is True
    assert db.scalar("SELECT count(*) FROM alert_mutes") == 0
    assert repo(db).get("u1", "A", "600000") is None


def test_list_for_carries_one_row_per_kind(db):
    repo(db).add("u1", "A", "600000", kind="news", note="资讯别推")
    repo(db).add("u1", "A", "600000", kind="rule")

    assert {(m.kind, m.note) for m in repo(db).list_for("u1")} == {
        ("news", "资讯别推"),
        ("rule", ""),
    }


def test_scopes_are_exact_so_the_page_can_list_what_is_on(db):
    """详情页要逐类解，不能把 `all` 的兜底当成四类都单独静着。"""
    repo(db).add("u1", "A", "600000", kind="all")

    assert [m.kind for m in repo(db).scopes("u1", "A", "600000")] == ["all"]


def test_an_unknown_kind_is_rejected(db):
    with pytest.raises(ValidationError):
        repo(db).add("u1", "A", "600000", kind="weekly")
    with pytest.raises(ValidationError):
        repo(db).get("u1", "A", "600000", kind="weekly")
