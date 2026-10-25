"""闭环用例层（spec §8.2/§8.3）：仓库闸之外的三样——账户归属、引用存在、标的相符。

状态机本身（终态不可回退、note 长度、引用必填、`alert_events` 逐动作一行）
住在 `AlertRepo`，这里一枚都不重复断言；这一文件只钉仓库看不见的那半：
`thesis` 的引用必须存在且同标的、`watch` 要与闭环同事务、`trade` 的成交与闭环
在同一个事务里要么都成要么都不留。

回滚那两枚是 §11「成交闭环失败：整体回滚，告警保持原状态」的机器版本——
它们防的不是异常，是「库里多了一笔没发生的成交」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from sams.core.errors import Conflict, StorageError, ValidationError
from sams.services.annotations import AnnotationService
from sams.services.ledger import LedgerService
from sams.services.review import ReviewService
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.annotations import AnnotationRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

USER = "trader"
OTHER = "other"
PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 10, 24, 9, 30, tzinfo=UTC)
#: 规则的 params_on 是日粒度，成交时刻用整分的 UTC
RULE_DAY = NOW.date()


@dataclass
class World:
    review: ReviewService
    users: UserRepo
    alerts: AlertRepo
    ledger: LedgerService
    accounts: SimAccountRepo
    orders: SimOrderRepo
    theses: ThesisRepo
    annotations: AnnotationService
    watchlists: WatchlistRepo

    def uid(self, name: str = USER) -> str:
        return self.users.get_by_username(name).id

    def alert(self, *, user: str = USER, **over):
        kw: dict = {
            "rule_id": "rule-1",
            "rule_name": "跌破止损",
            "account_id": None,
            "account_name": "模拟盘",
            "market": "A",
            "code": "601088",
            "symbol_name": "中国神华",
            "severity": "strong",
            "stage": "破位",
            "value_text": "34.20 CNY",
            "threshold_text": "34.00 CNY",
            "action_note": "按纪律减仓",
            "triggered_at": NOW,
        }
        kw.update(over)
        return self.alerts.create(self.uid(user), **kw)

    def funded_account(self, name: str = "主账户") -> str:
        account = self.ledger.open_account(USER, name)
        self.ledger.deposit(
            account.id, "CNY", 100_000.0, username=USER, reason="初始", actor_id=self.uid()
        )
        return account.id

    def thesis(self, code: str = "601088", *, user: str = USER) -> str:
        repo = self.theses
        made = repo.create(
            user_id=self.uid(user), market="A", code=code, style="PR",
            actor_id=self.uid(user), now=NOW,
        )
        repo.advance(self.uid(user), made.id, "proposed", actor_id=self.uid(user), now=NOW)
        repo.set_levels(
            self.uid(user), made.id, actor_id=self.uid(user), now=NOW,
            entry_low=38.0, entry_high=40.0, stop_price=34.0, target_price=50.0, reward_risk=1.8,
        )
        repo.advance(self.uid(user), made.id, "opened", actor_id=self.uid(user), now=NOW)
        return made.id

    def rule(self, target_ref: str = "A/601088", *, user: str = USER) -> str:
        return RuleRepo(self.theses.db).create(
            self.uid(user), kind="stop.fixed", name="止损", target_type="symbol",
            target_ref=target_ref, params={"price": 34.0}, params_on=RULE_DAY,
        ).id


@pytest.fixture
def world(db, settings) -> World:
    users = UserRepo(db)
    users.create(USER, PASSWORD)
    users.create(OTHER, PASSWORD)
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    theses = ThesisRepo(db)
    alerts = AlertRepo(db)
    annotations = AnnotationService(
        AnnotationRepo(db), users, SymbolRepo(db), theses, RuleRepo(db),
        alerts, accounts, orders, settings,
    )
    review = ReviewService(
        db,
        alerts=alerts,
        ledger=LedgerService(accounts, orders, theses, users, settings),
        users=users,
        theses=theses,
        rules=RuleRepo(db),
        watchlists=WatchlistRepo(db),
        annotations=annotations,
    )
    return World(
        review=review, users=users, alerts=alerts, ledger=review.ledger,
        accounts=accounts, orders=orders, theses=theses,
        annotations=annotations, watchlists=review.watchlists,
    )


def _orders_count(db) -> int:
    return int(db.scalar("SELECT count(*) FROM sim_orders") or 0)


# ---------- other / trade 的入口分工 ----------


def test_other_without_a_note_is_refused(world):
    alert = world.alert()
    with pytest.raises(ValidationError, match="必须写一句说明"):
        world.review.act(world.uid(), alert.id, action_kind="other")
    assert world.alerts.get(alert.id).status == "new"


def test_other_with_a_note_closes_it(world):
    alert = world.alert()
    moved = world.review.act(world.uid(), alert.id, action_kind="other", note="已挂条件单")
    assert moved.status == "handled" and moved.action_kind == "other"
    assert moved.handle_note == "已挂条件单"


def test_plain_act_refuses_trade(world):
    """trade 只能走 `act_with_fill`（D125）：这一格不接「口头说成交」。"""
    alert = world.alert()
    with pytest.raises(ValidationError, match="act_with_fill"):
        world.review.act(world.uid(), alert.id, action_kind="trade", action_ref="order-1")


# ---------- thesis / rule 的引用 ----------


def test_thesis_ref_must_be_mine_and_same_symbol(world):
    alert = world.alert()
    other_thesis = world.thesis(user=OTHER)
    with pytest.raises(ValidationError, match="不存在或不属于"):
        world.review.act(
            world.uid(), alert.id, action_kind="thesis", action_ref=other_thesis, note="推进"
        )
    wrong_symbol = world.thesis(code="000001")
    with pytest.raises(ValidationError, match="不符"):
        world.review.act(
            world.uid(), alert.id, action_kind="thesis", action_ref=wrong_symbol, note="推进"
        )


def test_thesis_ref_closes_the_alert(world):
    alert = world.alert()
    made = world.thesis()
    moved = world.review.act(
        world.uid(), alert.id, action_kind="thesis", action_ref=made, note="推进到止盈"
    )
    assert moved.status == "handled" and moved.action_ref == made


def test_rule_ref_must_be_mine_but_may_guard_another_symbol(world):
    """规则可以跨标的（watchlist 展开）：§8.2 只要求「本人的」，不要求同标的。"""
    alert = world.alert()
    mine = world.rule(target_ref="A/000001")
    moved = world.review.act(
        world.uid(), alert.id, action_kind="rule", action_ref=mine, note="收紧阈值"
    )
    assert moved.action_ref == mine
    theirs = world.rule(user=OTHER)
    with pytest.raises(ValidationError, match="不存在或不属于"):
        world.review.act(
            world.uid(), world.alert().id, action_kind="rule", action_ref=theirs, note="改它"
        )


def test_a_foreign_alert_is_refused(world):
    theirs = world.alert(user=OTHER)
    with pytest.raises(StorageError, match="不属于"):
        world.review.act(world.uid(), theirs.id, action_kind="other", note="顺手关掉别人的")
    assert world.alerts.get(theirs.id).status == "new"


def test_a_missing_alert_is_refused(world):
    with pytest.raises(StorageError, match="不存在"):
        world.review.act(world.uid(), "no-such-alert", action_kind="other", note="查无此条")


# ---------- watch ----------


def test_watch_lands_in_the_watchlist_and_closes_together(world, db):
    alert = world.alert()
    moved = world.review.act(
        world.uid(), alert.id, action_kind="watch", note="进自选盯着"
    )
    assert moved.status == "handled" and moved.action_kind == "watch"
    assert world.watchlists.contains(world.uid(), "A", "601088") is True


# ---------- act_with_fill：同事务的成功与回滚 ----------


def test_fill_and_closure_commit_together(world):
    account_id = world.funded_account()
    # 先买 100 股再按纪律卖：`record_fill` 的超卖闸（§5.4）不认空仓卖出，
    # 这一枚要钉的是「成交与闭环同事务」，不是「能不能透支」
    world.ledger.buy(account_id, USER, "A", "601088", qty=100, price=36.0)
    alert = world.alert(account_id=account_id)
    moved = world.review.act_with_fill(
        world.uid(), alert.id, account_id=account_id, side="sell", qty=100, price=34.5,
        note="按纪律减仓",
    )
    assert moved.status == "handled" and moved.action_kind == "trade"
    order = world.orders.order(account_id, moved.action_ref)
    assert order is not None and order.side == "sell" and order.qty == 100
    # 标的与账户来自告警那一行：构造上就不可能出现「告警是神华、成交是别的票」
    assert (order.market, order.code) == ("A", "601088")


def test_a_terminal_alert_rolls_the_fill_back(world, db):
    """闭环闸在事务里抛 Conflict ⇒ 成交也不留（§11）。

    这条钉的不是异常类型，是库里的行数：一半都没了才算回滚。
    """
    account_id = world.funded_account()
    alert = world.alert()
    world.alerts.dismiss(alert.id, user_id=world.uid(), code="expected")
    before = _orders_count(db)
    with pytest.raises(Conflict):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=account_id, side="buy", qty=100, price=35.0
        )
    assert _orders_count(db) == before
    assert world.alerts.get(alert.id).status == "dismissed"


def test_a_foreign_account_rolls_the_closure_back(world, db):
    """账户归属由 ledger 拒；拒在事务里，告警就不许已经被关上。"""
    alert = world.alert()
    # open_account 按用户名解析归属：OTHER 名下这笔，我的 act_with_fill 用不了
    foreign = world.ledger.open_account(OTHER, "别人的账户")
    before = _orders_count(db)
    with pytest.raises(StorageError, match="不属于|不存在"):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=foreign.id, side="buy", qty=100, price=35.0
        )
    assert _orders_count(db) == before
    assert world.alerts.get(alert.id).status == "new"


def test_non_positive_qty_or_price_is_refused_before_anything_is_written(world, db):
    account_id = world.funded_account()
    alert = world.alert()
    with pytest.raises(ValidationError):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=account_id, side="buy", qty=0, price=35.0
        )
    with pytest.raises(ValidationError):
        world.review.act_with_fill(
            world.uid(), alert.id, account_id=account_id, side="hold", qty=100, price=35.0
        )
    assert _orders_count(db) == 0
    assert world.alerts.get(alert.id).status == "new"


# ---------- 闭环后「顺手写一条标的批注」（D137）----------


def test_sync_annotation_lands_on_the_symbol(world):
    alert = world.alert()
    world.review.act(
        world.uid(), alert.id, action_kind="other", note="已挂条件单",
        sync_annotation="跌破 34 减半，等企稳回补",
    )
    rows = world.annotations.list_target(world.uid(), "symbol", "A:601088")
    assert [made.body_md for made in rows] == ["跌破 34 减半，等企稳回补"]


def test_a_broken_sync_annotation_keeps_the_closure(world):
    """同步批注失败只吞掉批注本身：闭环已经提交，不能被一句话说崩（§12.1）。"""
    alert = world.alert()
    moved = world.review.act(
        world.uid(), alert.id, action_kind="other", note="已挂条件单",
        sync_annotation="超" * 20_001,  # 超过 max_body_chars=20000，AnnotationService 会拒
    )
    assert moved.status == "handled"
    assert world.annotations.list_target(world.uid(), "symbol", "A:601088") == []
