"""批注用例：目标是不是你的、标签怎么归一、正文多长（M4-B §4）。

仓库只认「这一行的 user_id 是不是你」；「这个 thesis/rule/alert/order 是不是你的」
要问各自的仓库，所以这道闸在这里。目标不存在与不属于你统一 404（NotFound）：
两种回答都告诉探测者同一件事——这里没有你能用的东西。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from support import FrozenClock

from sams.core.errors import Conflict, NotFound, ValidationError
from sams.services.annotations import AnnotationService, normalize_tags
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
USER, OTHER = "trader", "keeper"
PWD = "correct-horse-battery"


@pytest.fixture
def world(db, settings):
    clock = FrozenClock(NOW)
    users = UserRepo(db)
    user = users.create(USER, PWD)
    other = users.create(OTHER, PWD)
    symbols = SymbolRepo(db)
    theses = ThesisRepo(db)
    rules = RuleRepo(db, clock=clock)
    alerts = AlertRepo(db, clock=clock)
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=symbols)
    service = AnnotationService(
        AnnotationRepo(db, clock=clock),
        users,
        symbols,
        theses,
        rules,
        alerts,
        accounts,
        orders,
        settings,
    )
    return SimpleNamespace(
        clock=clock,
        users=users,
        user=user,
        other=other,
        theses=theses,
        rules=rules,
        alerts=alerts,
        accounts=accounts,
        orders=orders,
        service=service,
    )


def make_thesis(world, user_id: str | None = None):
    return world.theses.create(
        user_id=user_id or world.user.id,
        market="A",
        code="601088",
        style="value",
        actor_id=user_id or world.user.id,
        now=NOW,
    )


def make_rule(world, user_id: str | None = None):
    return world.rules.create(
        user_id or world.user.id,
        kind="price.cross_below",
        name="跌破 10 元",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
        account_id=None,
    )


def make_alert(world, user_id: str | None = None):
    return world.alerts.create(
        user_id or world.user.id,
        rule_id=None,
        rule_name="跌破 10 元",
        account_id=None,
        account_name="",
        market="A",
        code="601088",
        symbol_name="中国神华",
        severity="mid",
        stage="入池",
        value_text="10.1",
        threshold_text="10",
        action_note="",
        triggered_at=NOW,
    )


def make_order(world, user_id: str | None = None):
    owner = user_id or world.user.id
    account = world.accounts.open(owner, "主账户", report_currency="CNY")
    world.accounts.adjust_cash(account.id, "CNY", 100_000.0, reason="初始", actor_id=owner)
    return world.orders.record_fill(
        account_id=account.id,
        user_id=owner,
        market="A",
        code="601088",
        side="buy",
        qty=100,
        price=30.0,
        currency="CNY",
    )


def test_symbol_target_needs_no_master_row(world):
    """symbol 是全实例共享的研究对象：标的还没同步进 `symbols`，也可以先写研究笔记。"""
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A/600519", body_md="先看图"
    )
    assert made.target_ref == "A:600519"
    assert made.target_label == "600519"


def test_symbol_ref_accepts_the_slash_form(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="a/600519", body_md="先看图"
    )
    assert made.target_ref == "A:600519"


def test_a_bad_symbol_ref_is_a_validation_error(world):
    with pytest.raises(ValidationError, match="市场:代码"):
        world.service.create(
            world.user.id, target_type="symbol", target_ref="600519", body_md="x"
        )


def test_your_own_targets_are_accepted(world):
    thesis = make_thesis(world)
    rule = make_rule(world)
    alert = make_alert(world)
    order = make_order(world)
    for target_type, target_ref in (
        ("thesis", thesis.id),
        ("rule", rule.id),
        ("alert", alert.id),
        ("order", order.id),
    ):
        made = world.service.create(
            world.user.id, target_type=target_type, target_ref=target_ref, body_md="备注"
        )
        assert made.target_type == target_type
        assert made.target_label


def test_someone_elses_targets_are_not_found(world):
    thesis = make_thesis(world, world.other.id)
    rule = make_rule(world, world.other.id)
    alert = make_alert(world, world.other.id)
    order = make_order(world, world.other.id)
    for target_type, target_ref in (
        ("thesis", thesis.id),
        ("rule", rule.id),
        ("alert", alert.id),
        ("order", order.id),
    ):
        with pytest.raises(NotFound):
            world.service.create(
                world.user.id, target_type=target_type, target_ref=target_ref, body_md="x"
            )


def test_a_missing_target_is_the_same_not_found(world):
    with pytest.raises(NotFound):
        world.service.create(
            world.user.id, target_type="thesis", target_ref="ghost", body_md="x"
        )


def test_an_unknown_target_type_is_refused(world):
    with pytest.raises(ValidationError, match="target_type"):
        world.service.create(
            world.user.id, target_type="stock", target_ref="A:600519", body_md="x"
        )


def test_the_body_is_bounded(world, settings):
    with pytest.raises(ValidationError, match="正文"):
        world.service.create(
            world.user.id, target_type="symbol", target_ref="A:600519", body_md=" "
        )
    too_long = "x" * (settings.annotations.max_body_chars + 1)
    with pytest.raises(ValidationError, match="最多"):
        world.service.create(
            world.user.id, target_type="symbol", target_ref="A:600519", body_md=too_long
        )


def test_tags_are_normalized_and_deduped_case_insensitively():
    assert normalize_tags("#逻辑,  risk ,  RISK,逻辑") == ["逻辑", "risk"]


def test_too_many_or_too_long_tags_are_refused():
    with pytest.raises(ValidationError, match="10"):
        normalize_tags(",".join(f"t{index}" for index in range(11)))
    with pytest.raises(ValidationError, match="20"):
        normalize_tags("x" * 21)


def test_list_target_hides_other_peoples_private_notes(world):
    mine = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="我的"
    )
    world.service.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    rows = world.service.list_target(world.user.id, "symbol", "A:600519")
    assert [row.id for row in rows] == [mine.id]


def test_get_refuses_someone_elses_private_note(world):
    made = world.service.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    with pytest.raises(NotFound):
        world.service.get(world.user.id, made.id)


def test_edit_records_history_and_bumps_updated_at(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="第一版"
    )
    world.clock.advance(seconds=2)
    edited = world.service.edit(
        world.user.id, made.id, body_md="第二版", tags="#逻辑", expected_updated_at=made.updated_at
    )
    assert edited.body_md == "第二版"
    assert edited.updated_at != made.updated_at
    history = world.service.revisions(world.user.id, made.id)
    assert [(row.before_md, row.after_md) for row in history] == [("第一版", "第二版")]


def test_a_stale_edit_is_a_conflict(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="第一版"
    )
    world.clock.advance(seconds=2)
    world.service.edit(
        world.user.id, made.id, body_md="第二版", expected_updated_at=made.updated_at
    )
    with pytest.raises(Conflict):
        world.service.edit(
            world.user.id, made.id, body_md="第三版", expected_updated_at=made.updated_at
        )


def test_only_the_author_can_edit_pin_or_delete(world):
    made = world.service.create(
        world.other.id, target_type="symbol", target_ref="A:600519", body_md="别人的"
    )
    for action in (
        lambda: world.service.edit(world.user.id, made.id, body_md="x"),
        lambda: world.service.pin(world.user.id, made.id, pinned=True),
        lambda: world.service.delete(world.user.id, made.id),
        lambda: world.service.revisions(world.user.id, made.id),
    ):
        with pytest.raises(NotFound):
            action()


def test_delete_hides_the_annotation_from_every_read_path(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="要删"
    )
    world.service.delete(world.user.id, made.id)
    assert world.service.list_target(world.user.id, "symbol", "A:600519") == []
    with pytest.raises(NotFound):
        world.service.get(world.user.id, made.id)
    with pytest.raises(NotFound):
        world.service.edit(world.user.id, made.id, body_md="复活")


def test_pin_is_idempotent(world):
    made = world.service.create(
        world.user.id, target_type="symbol", target_ref="A:600519", body_md="置顶"
    )
    assert world.service.pin(world.user.id, made.id, pinned=True).pinned is True
    assert world.service.pin(world.user.id, made.id, pinned=True).pinned is True


def test_list_recent_forwards_the_window(world):
    from datetime import timedelta

    world.service.create(
        world.user.id, target_type="symbol", target_ref="A/600519", body_md="上周的"
    )
    world.clock.moment = NOW + timedelta(days=3)
    recent = world.service.create(
        world.user.id, target_type="symbol", target_ref="A/600519", body_md="本周的"
    )
    got = world.service.list_recent(world.user.id, since=NOW + timedelta(days=1))
    assert [row.id for row in got] == [recent.id]
