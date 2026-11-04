"""选股方案的当前名单（S4-B / D255）：进名单记 first_seen，留在名单更新 last_seen。

名单回答的是「这个方案现在有哪些票」。它不保存历史：一只票离开再来，就是一次新的
命中——「上次进来过」不能把今天的新消息压成旧闻。
"""

from __future__ import annotations

from datetime import date

from sams.storage.repos.screener_hits import ScreenerHitRepo

DAY = date(2026, 10, 28)
NEXT = date(2026, 10, 29)


def test_first_seen_stays_and_last_seen_moves(db):
    repo = ScreenerHitRepo(db)
    first = repo.sync("u1", "p1", "A", ["600519", "000001"], DAY)
    assert first.new == ("000001", "600519")
    assert first.kept == 0 and first.removed == 0

    second = repo.sync("u1", "p1", "A", ["600519", "000002"], NEXT)
    assert second.new == ("000002",)
    assert second.kept == 1 and second.removed == 1

    members = repo.members("u1", "p1")
    assert set(members) == {"600519", "000002"}
    assert members["600519"] == DAY
    assert members["000002"] == NEXT


def test_resyncing_the_same_codes_reports_no_new_and_no_removed(db):
    repo = ScreenerHitRepo(db)
    repo.sync("u1", "p1", "A", ["600519"], DAY)
    again = repo.sync("u1", "p1", "A", ["600519"], DAY)
    assert again.new == () and again.removed == 0 and again.kept == 1
    assert repo.count("u1", "p1") == 1


def test_a_code_that_leaves_and_returns_is_new_again(db):
    repo = ScreenerHitRepo(db)
    repo.sync("u1", "p1", "A", ["600519"], DAY)
    repo.sync("u1", "p1", "A", [], NEXT)
    assert repo.count("u1", "p1") == 0

    back = repo.sync("u1", "p1", "A", ["600519"], NEXT)
    assert back.new == ("600519",)
    assert repo.members("u1", "p1")["600519"] == NEXT


def test_presets_do_not_share_a_membership(db):
    repo = ScreenerHitRepo(db)
    repo.sync("u1", "p1", "A", ["600519"], DAY)
    repo.sync("u1", "p2", "A", ["000001"], DAY)
    assert set(repo.members("u1", "p1")) == {"600519"}
    assert set(repo.members("u1", "p2")) == {"000001"}
