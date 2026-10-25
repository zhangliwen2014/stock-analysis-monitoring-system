"""批注仓库：可见性过滤在 SQL 里，不在调用方那边（M4-B §4）。

「先查全量再让上层删掉别人的」这种接口，第一个忘了删的入口不会报错，只会把别人的
私有笔记显示在屏幕上。所以 `list_visible` 的第一个参数就是「谁在看」。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.storage.repos.annotations import AnnotationRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
OWNER, OTHER = "u1", "u2"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def repo(db, clock) -> AnnotationRepo:
    return AnnotationRepo(db, clock=clock)


def add(repo: AnnotationRepo, user: str = OWNER, **kw):
    values = {
        "target_type": "symbol",
        "target_ref": "A:600519",
        "target_label": "贵州茅台",
        "body_md": "渠道库存见底",
        "tags": ["逻辑"],
    }
    values.update(kw)
    return repo.create(user, **values)


def test_create_round_trips(repo):
    made = add(repo, pinned=True)
    assert made.tags == ["逻辑"]
    assert made.visibility == "private"
    assert repo.get(made.id) == made


def test_unknown_target_type_is_refused(repo):
    with pytest.raises(ValidationError, match="target_type"):
        add(repo, target_type="stock")


def test_an_empty_body_is_refused(repo):
    with pytest.raises(ValidationError, match="正文"):
        add(repo, body_md="   ")


def test_private_annotations_of_other_people_are_invisible(repo):
    mine = add(repo)
    theirs = add(repo, user=OTHER, body_md="别人的笔记")
    visible = repo.list_visible(OWNER)
    assert [row.id for row in visible] == [mine.id]
    assert theirs.id not in {row.id for row in visible}


def test_team_rows_are_only_visible_when_the_policy_is_on(repo):
    team = add(repo, user=OTHER, visibility="team", target_type="symbol", target_label="贵州茅台")
    assert repo.list_visible(OWNER, team_sharing=False) == []
    assert [row.id for row in repo.list_visible(OWNER, team_sharing=True)] == [team.id]


def test_team_rows_never_leak_from_private_targets(repo):
    """D121：team 只对 symbol 生效。一条 team 批注挂到别人的 thesis 上，
    等于用一条批注泄露「这个 thesis 存在」。"""
    add(repo, user=OTHER, visibility="team", target_type="thesis", target_ref="t1")
    assert repo.list_visible(OWNER, team_sharing=True) == []


def test_mine_only_beats_team_sharing(repo):
    add(repo)
    add(repo, user=OTHER, visibility="team")
    assert [row.user_id for row in repo.list_visible(OWNER, team_sharing=True, mine_only=True)] == [
        OWNER
    ]


def test_deleted_rows_are_hidden_even_from_their_author(repo):
    mine = add(repo)
    repo.soft_delete(mine.id, user_id=OWNER)
    assert repo.list_visible(OWNER) == []
    assert repo.get(mine.id) is None
    assert repo.get(mine.id, include_deleted=True).deleted_at == NOW


def test_target_filter_and_pinned_order(repo, clock):
    older = add(repo, body_md="第一条")
    clock.advance(minutes=1)
    pinned = add(repo, body_md="置顶", pinned=True)
    clock.advance(minutes=1)
    add(repo, body_md="别的票", target_ref="A:601088")
    rows = repo.list_visible(OWNER, target_type="symbol", target_ref="A:600519")
    assert [row.id for row in rows] == [pinned.id, older.id]


def test_search_is_a_literal_substring(repo):
    add(repo, body_md="毛利率 50% 以上")
    add(repo, body_md="渠道库存")
    assert len(repo.list_visible(OWNER, q="50%")) == 1
    assert len(repo.list_visible(OWNER, q="%")) == 1  # `%` 不是通配符


def test_tag_match_is_case_insensitive_and_exact(repo):
    add(repo, tags=["Risk", "逻辑"])
    add(repo, tags=["逻辑链"])
    assert len(repo.list_visible(OWNER, tag="risk")) == 1
    assert len(repo.list_visible(OWNER, tag="逻辑")) == 1  # 不是「逻辑链」


def test_pagination_keeps_the_order_stable(repo, clock):
    for index in range(3):
        add(repo, body_md=f"第 {index} 条")
        clock.advance(minutes=1)
    first = repo.list_visible(OWNER, limit=2)
    second = repo.list_visible(OWNER, limit=2, offset=2)
    assert [row.body_md for row in first] == ["第 2 条", "第 1 条"]
    assert [row.body_md for row in second] == ["第 0 条"]
