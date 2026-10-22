from __future__ import annotations

from datetime import UTC, datetime

from sams.services.rules.snapshot import Target
from sams.services.scheduler.universe import UniverseBuilder
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.rules import RuleRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo


class StubData:
    """只实现 `expand` 的 `RuleData` 替身：本用例验的是并集，不是展开规则本身。"""

    def __init__(self, watchlists: WatchlistRepo) -> None:
        self.watchlists = watchlists

    def expand(self, rule) -> list[Target]:
        if rule.target_type == "watchlist":
            return [
                Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)
                for market, code in self.watchlists.codes(rule.user_id)
            ]
        market, _, code = rule.target_ref.partition("/")
        return [Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)]


def test_universe_is_the_union_of_watchlist_positions_rules_and_live_theses(db, settings):
    users = UserRepo(db)
    alice = users.create("alice", "correct horse battery")
    watchlists = WatchlistRepo(db)
    watchlists.add(alice.id, "A", "600519")

    accounts = SimAccountRepo(db)
    account = accounts.open(alice.id, "主账户", report_currency="CNY")
    for currency, amount in (("CNY", 100_000.0), ("HKD", 100_000.0), ("USD", 1_000.0)):
        accounts.adjust_cash(account.id, currency, amount, reason="入金", actor_id=alice.id)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    orders.record_fill(
        account_id=account.id,
        user_id=alice.id,
        market="HK",
        code="00700",
        side="buy",
        qty=100,
        price=300.0,
        currency="HKD",
    )

    rules = RuleRepo(db)
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="自选止损",
        target_type="watchlist",
        target_ref="*",
        params={"price": 10.0},
    )
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="美股",
        target_type="symbol",
        target_ref="US/AAPL",
        params={"price": 10.0},
    )
    rules.create(
        alice.id,
        kind="stop.fixed",
        name="停用的",
        target_type="symbol",
        target_ref="A/600036",
        params={"price": 10.0},
        enabled=False,
    )

    theses = ThesisRepo(db)
    live = theses.create(
        user_id=alice.id,
        market="A",
        code="000001",
        style="value",
        actor_id=alice.id,
        now=datetime(2024, 10, 1, tzinfo=UTC),
    )
    theses.create(
        user_id=alice.id,
        market="A",
        code="000002",
        style="value",
        actor_id=alice.id,
        now=datetime(2024, 10, 1, tzinfo=UTC),
    )
    db.execute("UPDATE theses SET status = 'tracking' WHERE id = ?", [live.id])

    universe = UniverseBuilder(
        users=users,
        watchlists=watchlists,
        accounts=accounts,
        orders=orders,
        rules=rules,
        theses=theses,
        data=StubData(watchlists),
        settings=settings,
    ).build()

    assert universe.codes("A") == ("000001", "600519")
    assert universe.codes("HK") == ("00700",)
    assert universe.codes("US") == ("AAPL",)
    assert universe.fx_pairs == (("HKD", "CNY"), ("USD", "CNY"))
    assert not universe.empty
