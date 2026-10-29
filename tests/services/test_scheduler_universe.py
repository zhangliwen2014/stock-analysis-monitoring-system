from __future__ import annotations

import logging
from datetime import UTC, datetime

from sams.core.errors import ValidationError
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
    # bob 只有自选、没有账户/规则/论点：删掉「自选」这一路时只有他会从并集里消失
    bob = users.create("bob", "correct horse battery")
    watchlists = WatchlistRepo(db)
    watchlists.add(alice.id, "A", "600519")
    watchlists.add(bob.id, "A", "601318")

    accounts = SimAccountRepo(db)
    account = accounts.open(alice.id, "主账户", report_currency="CNY")
    for currency, amount in (("CNY", 100_000.0), ("HKD", 100_000.0), ("USD", 1_000.0)):
        accounts.adjust_cash(account.id, currency, amount, reason="入金", actor_id=alice.id)
    # 存过又清零的币种不该产生换汇对：`if amount` 判据钉在这里
    accounts.adjust_cash(account.id, "EUR", 100.0, reason="入金", actor_id=alice.id)
    accounts.adjust_cash(account.id, "EUR", -100.0, reason="出金", actor_id=alice.id)
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
    # 买断再卖光：库里留下 qty=0 的历史行，它不该继续把这只票拉进采集对象
    orders.record_fill(
        account_id=account.id,
        user_id=alice.id,
        market="HK",
        code="00998",
        side="buy",
        qty=100,
        price=20.0,
        currency="HKD",
    )
    orders.record_fill(
        account_id=account.id,
        user_id=alice.id,
        market="HK",
        code="00998",
        side="sell",
        qty=100,
        price=21.0,
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
    opened = theses.create(
        user_id=alice.id,
        market="A",
        code="000003",
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
    # 新建只能从 observing/proposed 开始，在跟的两态靠 UPDATE 直接落库
    db.execute("UPDATE theses SET status = 'tracking' WHERE id = ?", [live.id])
    db.execute("UPDATE theses SET status = 'opened' WHERE id = ?", [opened.id])

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

    assert universe.codes("A") == ("000001", "000003", "600519", "601318")
    assert universe.codes("HK") == ("00700",)
    assert universe.codes("US") == ("AAPL",)
    assert universe.fx_pairs == (("HKD", "CNY"), ("USD", "CNY"))
    # 大小写与空格不算语义：查询侧也归一，否则 "hk" 查出来是空
    assert universe.codes("hk") == ("00700",)


def test_build_carries_the_configured_indices(db, settings):
    """指数清单进 universe 但不进股票格：票跟着用户走，指数跟着设置走（D145）。"""
    accounts = SimAccountRepo(db)
    universe = UniverseBuilder(
        users=UserRepo(db),
        watchlists=WatchlistRepo(db),
        accounts=accounts,
        orders=SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db)),
        rules=RuleRepo(db),
        theses=ThesisRepo(db),
        data=StubData(WatchlistRepo(db)),
        settings=settings,
    ).build()
    for market in settings.markets:
        assert universe.indices(market) == settings.indices.for_market(market)
    assert "sh000001" in universe.indices("A")
    # 指数不混进股票并集：000001 撞车（平安银行 vs 上证指数）正是分格的原因
    assert "sh000001" not in universe.codes("A")
    # 查询侧与市场名归一同口径
    assert universe.indices("hk") == settings.indices.for_market("HK")


def test_a_folded_watchlist_row_of_the_wrong_shape_never_enters_the_universe(
    db, settings, caplog
):
    """A 市场的清单里折起一只港股形态的代码：它进不了采集清单（D210 对 D169 的闸）。

    折起只是从看板消失，这一行一直留在采集清单里；可 `A/00700` 不是一个能表示的 A 股，
    发给 A 股适配器只会抛——夜里的日线 job 因为一条永远取不到的旧行被标成失败，
    三个周期后还会响告警。跳过它是「这只票不采」，不是「今晚全市场没采到」。
    """
    users = UserRepo(db)
    alice = users.create("alice", "correct horse battery")
    watchlists = WatchlistRepo(db)
    watchlists.add(alice.id, "HK", "00700")
    watchlists.add(alice.id, "A", "600519")
    watchlists.add(alice.id, "A", "00700")
    assert watchlists.remove(alice.id, "A", "00700")

    accounts = SimAccountRepo(db)
    with caplog.at_level(logging.WARNING, logger="sams.services.scheduler.universe"):
        universe = UniverseBuilder(
            users=users,
            watchlists=watchlists,
            accounts=accounts,
            orders=SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db)),
            rules=RuleRepo(db),
            theses=ThesisRepo(db),
            data=StubData(watchlists),
            settings=settings,
        ).build()

    assert universe.codes("A") == ("600519",)
    # 同一个代码在它自己的市场里照常采：闸只看形态，不看代码本身
    assert universe.codes("HK") == ("00700",)
    assert any("A 00700" in record.getMessage() for record in caplog.records)


def test_one_rule_that_cannot_expand_does_not_stop_the_others(db, settings, caplog):
    """展开失败的规则只跳过自己，其余对象照常进入采集。

    替身按 `RuleData.expand` 的失败契约抛 `ValidationError`（真展开器只在规则自身
    形状不成时抛，见 `snapshot.py` 的展开分支），并不断言这个形状能从 `RuleRepo`
    建出来——仓库的校验正好把它挡在外面。
    """

    users = UserRepo(db)
    alice = users.create("alice", "correct horse battery")
    rules = RuleRepo(db)
    broken = rules.create(
        alice.id,
        kind="stop.fixed",
        name="形状不成",
        target_type="symbol",
        target_ref="A/600001",
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

    class _Expanding:
        def expand(self, rule):
            if rule.id == broken.id:
                raise ValidationError("market 不能为空：A / HK / US")
            market, _, code = rule.target_ref.partition("/")
            return [Target(rule.user_id, market, code, rule.account_id, rule.thesis_id)]

    accounts = SimAccountRepo(db)
    with caplog.at_level(logging.WARNING, logger="sams.services.scheduler.universe"):
        universe = UniverseBuilder(
            users=users,
            watchlists=WatchlistRepo(db),
            accounts=accounts,
            orders=SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db)),
            rules=rules,
            theses=ThesisRepo(db),
            data=_Expanding(),
            settings=settings,
        ).build()

    assert universe.codes("A") == ()
    assert universe.codes("US") == ("AAPL",)
    # 这一只票之外，配置里的其余市场一个对象都没有，仍各占一格（空元组而不是缺格）
    assert set(universe.by_market) == set(settings.markets)
    assert f"规则 {broken.id} 展开失败" in caplog.text
