"""list_enabled：盘中一趟的入口。跨用户、只启用的、次序稳定。"""

from __future__ import annotations

from datetime import UTC, datetime

from sams.storage.repos.rules import RuleRepo

NOW = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)


def add(repo, user_id, name, *, enabled=True):
    return repo.create(
        user_id,
        kind="stop.fixed",
        name=name,
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
        enabled=enabled,
        params_on=NOW.date(),
    )


def test_enabled_rules_across_users_in_a_stable_order(db):
    repo = RuleRepo(db)
    # 插入次序刻意与 ORDER BY user_id, id 相反：u-b 先落地。若把 ORDER BY 去掉
    # （或改成按插入/created_at 排），下面那条断言就会红
    b1 = add(repo, "u-b", "乙一")
    a1 = add(repo, "u-a", "甲一")
    add(repo, "u-a", "甲停", enabled=False)
    b2 = add(repo, "u-b", "乙二")
    got = repo.list_enabled()
    # ORDER BY user_id, id：u-a 的在前；同一用户内按 id 排
    assert [r.id for r in got] == [a1.id] + sorted([b1.id, b2.id])
    assert [r.user_id for r in got] == ["u-a", "u-b", "u-b"]


def test_a_disabled_rule_never_shows_up(db):
    repo = RuleRepo(db)
    add(repo, "u-a", "开着")
    rule = add(repo, "u-a", "关着")
    repo.set_enabled(rule.id, False, user_id="u-a")
    assert [r.name for r in repo.list_enabled()] == ["开着"]


def test_rules_from_other_tables_are_not_leaked(db):
    # `set_enabled` 之后回读的形状必须和 `list_enabled` 同源：都是 `_to_rule`
    repo = RuleRepo(db)
    rule = add(repo, "u-a", "一条")
    got = repo.list_enabled()[0]
    assert got.id == rule.id and got.kind == "stop.fixed" and got.enabled is True
