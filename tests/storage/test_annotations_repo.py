"""批注仓库：可见性过滤在 SQL 里，不在调用方那边（M4-B §4）。

「先查全量再让上层删掉别人的」这种接口，第一个忘了删的入口不会报错，只会把别人的
私有笔记显示在屏幕上。所以 `list_visible` 的第一个参数就是「谁在看」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from support import FrozenClock

from sams.core.errors import Conflict, ValidationError
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


def test_team_rows_never_leak_from_private_targets(repo, db):
    """D121：team 只对 symbol 生效。一条 team 批注挂到别人的 thesis 上，
    等于用一条批注泄露「这个 thesis 存在」。

    写入路径已经拒收，这里直接改库造出历史遗留的坏行：读过滤仍要兜住它。
    """
    legacy = add(repo, user=OTHER, target_type="thesis", target_ref="t1")
    db.execute("UPDATE annotations SET visibility = 'team' WHERE id = ?", [legacy.id])
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


def test_team_visibility_is_refused_on_non_symbol_targets(repo):
    with pytest.raises(ValidationError, match="symbol"):
        add(repo, visibility="team", target_type="thesis", target_ref="t1")
    made = add(repo, target_type="thesis", target_ref="t1")
    with pytest.raises(ValidationError, match="symbol"):
        repo.update(
            made.id, user_id=OWNER, body_md="改", tags=[], visibility="team"
        )


def test_create_cannot_bypass_the_pin_cap(repo):
    for index in range(3):
        add(repo, body_md=f"置顶 {index}", pinned=True)
    with pytest.raises(ValidationError, match="最多置顶"):
        add(repo, body_md="第四条", pinned=True)


def test_two_edits_in_the_same_clock_tick_still_conflict(repo):
    """冻结时钟下两次编辑的 now() 相同：updated_at 必须仍前进，旧版本号才分得出来。"""
    made = add(repo)
    repo.update(
        made.id, user_id=OWNER, body_md="第一改", tags=[], visibility="private",
        expected_updated_at=made.updated_at,
    )
    with pytest.raises(Conflict, match="改过"):
        repo.update(
            made.id, user_id=OWNER, body_md="第二改", tags=[], visibility="private",
            expected_updated_at=made.updated_at,
        )


def test_update_writes_a_revision_with_the_before_and_after_tags(repo, clock):
    made = add(repo)
    clock.advance(seconds=1)
    edited = repo.update(
        made.id,
        user_id=OWNER,
        body_md="渠道库存见底，等回款",
        tags=["逻辑", "风险"],
        visibility="private",
        expected_updated_at=made.updated_at,
    )
    assert edited.body_md.endswith("等回款")
    assert edited.tags == ["逻辑", "风险"]
    revisions = repo.revisions(made.id)
    assert len(revisions) == 1
    assert revisions[0].before_md == "渠道库存见底"
    assert revisions[0].after_md == "渠道库存见底，等回款"
    assert revisions[0].meta["before"]["tags"] == ["逻辑"]
    assert revisions[0].meta["after"]["tags"] == ["逻辑", "风险"]


def test_a_stale_updated_at_is_a_conflict(repo, clock):
    made = add(repo)
    clock.advance(seconds=1)
    repo.update(
        made.id,
        user_id=OWNER,
        body_md="第一改",
        tags=[],
        visibility="private",
        expected_updated_at=made.updated_at,
    )
    with pytest.raises(Conflict, match="改过"):
        repo.update(
            made.id,
            user_id=OWNER,
            body_md="第二改",
            tags=[],
            visibility="private",
            expected_updated_at=made.updated_at,
        )


def test_only_the_author_can_edit_or_delete(repo):
    made = add(repo)
    with pytest.raises(ValidationError, match="不属于"):
        repo.update(
            made.id, user_id=OTHER, body_md="改别人的", tags=[], visibility="private"
        )
    with pytest.raises(ValidationError, match="不属于"):
        repo.soft_delete(made.id, user_id=OTHER)


def test_pinning_is_capped_at_three_per_target(repo):
    for index in range(3):
        add(repo, body_md=f"置顶 {index}", pinned=True)
    extra = add(repo, body_md="第四条")
    with pytest.raises(ValidationError, match="最多置顶"):
        repo.set_pinned(extra.id, user_id=OWNER, pinned=True)


def test_pinning_does_not_move_the_edit_clock(repo):
    """置顶不是编辑：一个开着编辑器的窗口不该因为别人置顶而 409。"""
    made = add(repo)
    pinned = repo.set_pinned(made.id, user_id=OWNER, pinned=True)
    assert pinned.updated_at == made.updated_at


def test_editing_a_deleted_annotation_is_a_validation_error(repo):
    made = add(repo)
    repo.soft_delete(made.id, user_id=OWNER)
    with pytest.raises(ValidationError, match="不存在或已删除"):
        repo.update(made.id, user_id=OWNER, body_md="x", tags=[], visibility="private")


def test_soft_delete_keeps_a_tombstone_for_later_purge(repo):
    made = add(repo)
    repo.soft_delete(made.id, user_id=OWNER)
    tombstone = repo.get(made.id, include_deleted=True)
    assert tombstone.deleted_by == OWNER
    assert tombstone.purged_at is None


def test_list_visible_since_cuts_the_older_rows(repo, clock):
    add(repo, body_md="上周写的那条")
    clock.moment = NOW + timedelta(days=3)
    recent = add(repo, body_md="本周的")
    rows = repo.list_visible(OWNER, since=NOW + timedelta(days=1))
    assert [row.id for row in rows] == [recent.id]


def test_team_only_scope_is_teams_only(repo):
    """`team_only` 是搜索页「团队」档的范围闸：只看 team 行，不再附带自己的私有行。"""
    mine_team = add(repo, OWNER, visibility="team")
    mine_private = add(repo, OWNER, body_md="只给自己")
    theirs_team = add(repo, OTHER, visibility="team")
    add(repo, OTHER, body_md="只给作者")
    rows = repo.list_visible(OWNER, team_sharing=True, team_only=True)
    assert {r.id for r in rows} == {mine_team.id, theirs_team.id}
    off = repo.list_visible(OWNER, team_sharing=False, team_only=True)
    assert {r.id for r in off} == {mine_team.id}
    # 两条 scope 旗子不能同真：同给等于没说
    with pytest.raises(Exception, match="只能选一"):
        repo.list_visible(OWNER, mine_only=True, team_only=True)
    assert mine_private.id and theirs_team.id  # 防 IDE 清 import


def test_count_visible_matches_page(repo):
    """分页的分母与分子必须同源：同一份过滤条件下，总数与逐页取回的并集要相等。"""
    for n in range(25):
        add(repo, OWNER, body_md=f"第 {n} 条")
    rows = repo.list_visible(OWNER, limit=20, offset=0)
    total = repo.count_visible(OWNER)
    assert len(rows) == 20 and total == 25
    rows2 = repo.list_visible(OWNER, limit=20, offset=20)
    assert len(rows2) == 5


# --- 编辑附件后的 revision 补记（§4.3/Task 4）------------------------------------------------


def test_stamp_last_revision_records_attachments(repo):
    """补写而不是随 update 一起写：新附件的 id 要等文件传完才存在，
    revision 落库时还不知道「这次编辑之后有哪些附件」。"""
    made = add(repo, body_md="带图")
    repo.update(made.id, user_id=OWNER, body_md="改一下", tags=[], visibility="private")
    repo.stamp_last_revision_attachments(made.id, ["att-a", "att-b"])
    meta = repo.revisions(made.id)[-1].meta
    assert meta["attachments"] == ["att-a", "att-b"]


def test_stamp_without_revision_is_a_no_op(repo):
    """没 revision（人没改正文）就静默跳过：给不存在的编辑补记等于伪造历史。"""
    made = add(repo)
    repo.stamp_last_revision_attachments(made.id, ["att-a"])
    assert repo.revisions(made.id) == []


# --- admin 元数据列表（§6.1/D139）--------------------------------------------------------------


def test_list_metadata_includes_tombstones_and_excludes_purged(repo):
    """admin 的元数据列表：不按可见性滤，带软删墓碑，排已硬清除。"""
    a = add(repo, OWNER, body_md="别人的也列")
    b = add(repo, OTHER, body_md="将被删")
    c = add(repo, OWNER, body_md="将被清除")
    repo.soft_delete(b.id, user_id=OTHER)
    # 仓库无 purge()——照 services/annotation_prune.py 的裸 SQL 补一行
    repo.db.execute("UPDATE annotations SET purged_at = ? WHERE id = ?", [NOW, c.id])
    ids = {r.id for r in repo.list_metadata(limit=50)}
    assert {a.id, b.id} <= ids and c.id not in ids
    assert repo.count_metadata() == len(ids)


def test_list_metadata_orders_newest_first(repo):
    """ORDER BY created_at DESC, id DESC：admin 要一眼看到刚写的那条，
    而同一时刻的多条要有稳定的次序（id 兜底，翻页才不会重复/漏行）。"""
    first = add(repo, OWNER, body_md="最早")
    clock_now = NOW + timedelta(minutes=5)
    repo.clock.moment = clock_now
    second = add(repo, OWNER, body_md="最晚")
    assert [r.id for r in repo.list_metadata()] == [second.id, first.id]


def test_list_metadata_filters_share_one_source_with_count(repo):
    """§6.5:340 的列表筛选：user/type/visibility/时间窗全部 ?-绑定，
    而 `count_metadata` 与 `list_metadata` 走同一份 WHERE——分母与分子分叉迟早数错。"""
    a = add(repo, OWNER)
    b = add(repo, OTHER, target_type="thesis", target_ref="t-1", target_label="观点")
    c = add(repo, OWNER, visibility="team")
    repo.clock.moment = NOW + timedelta(hours=2)
    d = add(repo, OTHER, body_md="两小时后")
    cases = [
        {"user_id": OTHER},
        {"target_type": "thesis"},
        {"visibility": "team"},
        {"since": NOW + timedelta(hours=1)},
        {"until": NOW + timedelta(hours=1)},
        {"user_id": OWNER, "target_type": "symbol", "visibility": "private"},
        {},
    ]
    for filt in cases:
        ids = {r.id for r in repo.list_metadata(**filt, limit=50)}
        assert repo.count_metadata(**filt) == len(ids), filt
    assert {r.id for r in repo.list_metadata(user_id=OTHER)} == {b.id, d.id}
    assert [r.id for r in repo.list_metadata(since=NOW + timedelta(hours=1))] == [d.id]
    # until 是开区间（页面折成「当天 +1d」）：恰好等于 created_at 的那条不在窗内
    assert {r.id for r in repo.list_metadata(until=NOW + timedelta(minutes=1))} == {
        a.id,
        b.id,
        c.id,
    }


def test_list_metadata_rejects_unknown_target_type(repo):
    with pytest.raises(ValidationError, match="target_type"):
        repo.list_metadata(target_type="stock")


def test_count_all_is_the_annotation_quota_column(repo):
    """用量表的「批注数」：排已硬清除；软删墓碑还算——配额问的是「名下有几条」，
    30 天保留期里的行仍占着这个人的额度（D136）。"""
    add(repo, OWNER)
    add(repo, OWNER, body_md="第二条")
    soft = add(repo, OWNER, body_md="将被删")
    repo.soft_delete(soft.id, user_id=OWNER)
    purged = add(repo, OTHER)
    repo.db.execute("UPDATE annotations SET purged_at = ? WHERE id = ?", [NOW, purged.id])
    assert repo.count_all(OWNER) == 3
    assert repo.count_all(OTHER) == 0


def test_count_revisions_counts_edits_only(repo):
    made = add(repo)
    assert repo.count_revisions(made.id) == 0
    repo.update(made.id, user_id=OWNER, body_md="改一版", tags=[], visibility="private")
    repo.update(made.id, user_id=OWNER, body_md="再改一版", tags=[], visibility="private")
    assert repo.count_revisions(made.id) == 2
