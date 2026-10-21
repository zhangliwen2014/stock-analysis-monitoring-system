"""`rules` 的写入契约（spec §10.1 / §9.2 / §4.2）。

`rules` 这张表从 0001 就在库里而没人写它：这一整个文件就是那个「第一次写」。
所以这里的每条断言都在钉「第一次写进来的人不许留下什么」，而不是在测一个已有实现。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.rules import RuleRepo


@pytest.fixture
def repo(db):
    return RuleRepo(db)


def seed_user(db):
    from sams.storage.repos.users import UserRepo

    return UserRepo(db).create("trader", "correct-horse-battery").id


def seed_thesis(db, user_id):
    from sams.storage.repos.theses import ThesisRepo

    return ThesisRepo(db).create(
        user_id=user_id,
        market="A",
        code="601088",
        style="value",
        actor_id=user_id,
        now=datetime(2026, 10, 20, 9, 30, tzinfo=UTC),
    )


def add(repo, user_id, **kw):
    """一条最小可入库的规则：`kind` 与 `params` 齐备，其余走默认。"""
    params = {"kind": "stop.pct", "target_type": "position", "target_ref": "*",
              "account_id": "acct-1", "name": "止损 8%", "params": {"pct": 0.08}}
    params.update(kw)
    return repo.create(user_id, **params)


def test_a_position_rule_without_an_account_is_refused(repo):
    """§10.1 的去重键含 `account_id`：缺了它，A 账户报过之后 B 账户的破位会被当重复吞掉。"""
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="account_id"):
        add(repo, user, account_id=None)


def test_a_non_position_rule_may_stay_out_of_any_account(repo):
    user = seed_user(repo.db)
    row = add(repo, user, kind="price.cross_below", params={"price": 10.0},
              target_type="symbol", target_ref="A/601088", account_id=None)
    assert row.account_id is None


def test_unknown_params_are_refused_before_the_insert(repo):
    """库里躺着一条「参数没人认」的规则时，屏幕上那个「止损 8%」是装饰。"""
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="percent"):
        add(repo, user, params={"pct": 0.08, "percent": 8})


def test_defaults_are_stored_not_left_absent(repo):
    user = seed_user(repo.db)
    row = add(repo, user, kind="stop.atr", params={})
    assert row.params == {"multiple": 2.0}


def test_a_thesis_rule_must_point_at_an_existing_thesis(repo):
    """外键那一层 DuckDB 不查（`0001` 里 `thesis_id` 只是列），所以「指向一条不存在的论」
    是能被写进库的：此后每次求值都对它取不到价位，屏幕上那条规则永远是「没触发」。"""
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="观点"):
        add(repo, user, kind="thesis.odds_below", target_type="thesis",
            target_ref="does-not-exist", thesis_id="does-not-exist", account_id=None,
            params={"threshold": 1.5})


def test_an_empty_target_ref_is_refused(repo):
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="target_ref"):
        add(repo, user, target_type="symbol", target_ref="", account_id=None,
            kind="price.cross_below", params={"price": 1.0})


def test_a_symbol_target_ref_must_carry_the_market(repo):
    """只有代码的引用在 A 股与港股之间是同一个串（`00700`），去重键会撞。"""
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="A/601088"):
        add(repo, user, target_type="symbol", target_ref="601088", account_id=None,
            kind="price.cross_below", params={"price": 1.0})


def test_the_template_decides_which_target_types_are_allowed(repo):
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="position"):
        add(repo, user, kind="stop.pct", target_type="watchlist", target_ref="*",
            account_id="acct-1")


def test_a_severity_outside_the_three_is_refused(repo):
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="strong"):
        add(repo, user, severity="critical")


def test_the_first_rule_for_a_user_becomes_visible_and_round_trips(repo):
    user = seed_user(repo.db)
    made = add(repo, user)
    read = repo.get(made.id, user_id=user)
    assert read is not None
    assert read.name == "止损 8%"
    assert read.kind == "stop.pct"
    assert read.enabled is True
    assert read.channel_ids == []
    assert read.created_at is not None


def test_reading_another_users_rule_gives_nothing(repo):
    """仓库层的用户隔离（§4.2）：不提供「查全量再由调用方过滤」的接口。"""
    from sams.storage.repos.users import UserRepo

    mine = seed_user(repo.db)
    theirs = UserRepo(repo.db).create("other", "correct-horse-battery").id
    made = add(repo, mine)
    assert repo.get(made.id, user_id=theirs) is None
    assert repo.list_for(theirs) == []


def test_an_update_writes_one_revision_with_both_full_snapshots(repo):
    """§4.2：「跌破后偷偷把止损线往下挪」要能被查出来 ⇒ 改前那一份完整规则必须在库里。"""
    user = seed_user(repo.db)
    made = add(repo, user)
    after = repo.update(made.id, changed_by=user, params={"pct": 0.12})
    assert after.params == {"pct": 0.12}
    rows = repo.revisions(made.id)
    assert len(rows) == 1
    assert rows[0].before["params"] == {"pct": 0.08}
    assert rows[0].after["params"] == {"pct": 0.12}
    assert rows[0].changed_by == user


def test_an_update_that_changes_nothing_writes_no_revision(repo):
    """空修订史会让「改了几次」这个数虚高，而那一列是审计页要读的。"""
    user = seed_user(repo.db)
    made = add(repo, user)
    repo.update(made.id, changed_by=user, params={"pct": 0.08})
    assert repo.revisions(made.id) == []


def test_an_update_revalidates_the_params_of_the_whole_kind(repo):
    user = seed_user(repo.db)
    made = add(repo, user)
    with pytest.raises(ValidationError, match="pct"):
        repo.update(made.id, changed_by=user, params={})


def test_switching_the_kind_rejects_params_that_do_not_fit(repo):
    """改 kind 时旧参数不能留：`stop.pct` 的 `pct` 对 `stop.ma` 没有意义，
    留着它屏幕上那条规则会带着一个没人读的键，而 `validate_params` 本来会拒它。"""
    user = seed_user(repo.db)
    made = add(repo, user)
    with pytest.raises(ValidationError, match="basis"):
        repo.update(made.id, changed_by=user, kind="stop.ma")


def test_a_thesis_bound_rule_cannot_be_deleted(repo):
    """§9.2：派生规则只能停用。「删掉」会把「它曾经存在」这句话一起删掉。"""
    user = seed_user(repo.db)
    thesis = seed_thesis(repo.db, user)
    made = add(repo, user, kind="stop.pct", thesis_id=thesis.id, target_type="position")
    with pytest.raises(ValidationError, match="停用"):
        repo.remove(made.id, user_id=user)
    repo.set_enabled(made.id, False, user_id=user)
    assert repo.get(made.id, user_id=user).enabled is False


def test_an_ordinary_rule_can_be_removed(repo):
    user = seed_user(repo.db)
    made = add(repo, user)
    assert repo.remove(made.id, user_id=user) is True
    assert repo.get(made.id, user_id=user) is None


def test_the_default_channel_list_is_stored_as_json_and_read_back(repo):
    user = seed_user(repo.db)
    made = add(repo, user, channel_ids=["c1", "c2"])
    assert repo.get(made.id, user_id=user).channel_ids == ["c1", "c2"]


def test_a_cooldown_of_zero_is_allowed_and_means_every_hit_reports(repo):
    """0 不是「没填」：填了 0 就是「每次都报」，那是有人真的要这么干时的形状。"""
    user = seed_user(repo.db)
    made = add(repo, user, cooldown_min=0)
    assert repo.get(made.id, user_id=user).cooldown_min == 0


def test_a_negative_cooldown_is_refused(repo):
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="cooldown"):
        add(repo, user, cooldown_min=-1)


def test_only_known_condition_kinds_are_written(repo):
    """`expression` 是 M5 的东西（§10.4）：现在收进来会得到一条永远不会被求值的规则。"""
    user = seed_user(repo.db)
    with pytest.raises(ValidationError, match="template"):
        add(repo, user, condition_kind="expression")


def test_list_for_orders_by_creation_and_can_filter_enabled(repo):
    user = seed_user(repo.db)
    first = add(repo, user, name="一条")
    second = add(repo, user, name="两条", kind="stop.fixed",
                 params={"price": 9.0}, target_type="symbol", target_ref="A/601088",
                 account_id=None)
    repo.set_enabled(first.id, False, user_id=user)
    assert [row.id for row in repo.list_for(user)] == [first.id, second.id]
    assert [row.id for row in repo.list_for(user, enabled_only=True)] == [second.id]


def test_counts_splits_total_from_enabled(repo):
    """`on` 是 DuckDB 的保留字：别名撞上它时 `counts()` 每次都是 Parser Error，
    而仪表盘那一格只是空着——没有这条用例就没人会发现。"""
    user = seed_user(repo.db)
    stopped = add(repo, user, name="停用的")
    add(repo, user, name="在用的")
    repo.set_enabled(stopped.id, False, user_id=user)
    assert repo.counts(user) == {"total": 2, "enabled": 1}


def test_by_thesis_finds_the_derived_rules_in_creation_order(repo):
    user = seed_user(repo.db)
    thesis = seed_thesis(repo.db, user)
    a = add(repo, user, name="止损", thesis_id=thesis.id)
    b = add(repo, user, name="到价", thesis_id=thesis.id, kind="price.cross_above",
            params={"price": 60.0}, target_type="thesis", target_ref=thesis.id)
    assert [row.id for row in repo.by_thesis(thesis.id)] == [a.id, b.id]
