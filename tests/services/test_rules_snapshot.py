"""一趟求值的两个半边：`expand`（这条规则覆盖哪些对象）与 `read`（那个对象此刻的数）。

这里断言的全是「屏幕上那条规则到底读了谁」：`watchlist` 展开成零个对象、`position` 用了
别人的账户、`thesis` 取到另一条论——三种都不会报错，只会让一条看起来开着的规则永远不响。

世界由 `support_valuation.make_container` 铺：A/601088 有 300 天日线、两期年报、逐日估值，
时钟冻在 `DAYS[-1]`。用例不联网（stub adapter），所以这里的数是可以逐字断言的。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import support_valuation

from sams.core.errors import NotAdjustable, ValidationError
from sams.services.rules.snapshot import RuleData, Target
from sams.storage.repos.rules import Rule, RuleRepo

CODE = support_valuation.CODE  # "601088"
AS_OF = support_valuation.DAYS[-1]
#: `ThesisRepo` 的时间列是 `datetime`（`date` 会在 `created_at` 那里落成 00:00 的另一种形状）
NOW = datetime(2024, 10, 26, 6, 30, tzinfo=UTC)


@pytest.fixture
def data(db, settings):
    """从容器字段直接装配：构造参数与 `bootstrap.py` 里那些服务是同一批对象。

    故意不在这里重新 `XxxRepo(db)` 一遍——那会造出第二个仓库实例，而用例要验的正是
    「引擎读到的数与屏幕上那个数是同一个」。
    """
    container = support_valuation.make_container(db, settings)
    return RuleData(
        valuation=container.valuation,
        analysis=container.analysis,
        bars=container.bars,
        symbols=container.symbols,
        accounts=container.sim_accounts,
        ledger=container.sim_ledger,
        theses=container.theses_store,
        watchlists=container.watchlists,
        settings=settings,
    )


def make_rule(db, **over) -> Rule:
    args = {
        "user_id": "u1",
        "kind": "stop.fixed",
        "name": "跌破 10 元",
        "target_type": "symbol",
        "target_ref": f"A/{CODE}",
        "params": {"price": 10.0},
    }
    args.update(over)
    return RuleRepo(db).create(**args)


# --- 展开 --------------------------------------------------------------------------------


def test_a_symbol_rule_expands_to_exactly_one_target(db, data):
    rule = make_rule(db)
    assert data.expand(rule) == [Target("u1", "A", CODE, None, None)]


def test_a_watchlist_rule_expands_over_the_owners_list(db, data):
    from sams.storage.repos.watchlists import WatchlistRepo

    WatchlistRepo(db).add("u1", "A", CODE)
    WatchlistRepo(db).add("u1", "A", "600000")
    rule = make_rule(
        db,
        kind="price.cross_below",
        target_type="watchlist",
        target_ref="*",
        params={"price": 1.0},
    )

    got = sorted((t.market, t.code) for t in data.expand(rule))
    assert got == [("A", "600000"), ("A", CODE)]


def test_a_position_rule_expands_over_that_account_only(db, data):
    """§1 那张表的第三行在展开这一半：A 账户的规则不许把 B 账户的持仓也收进来。"""
    from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo

    accounts = SimAccountRepo(db)
    first = accounts.open("u1", "甲券商", market_scope=("A",))
    second = accounts.open("u1", "乙券商", market_scope=("A",))
    # `record_fill` 的买入分支要求账户已有同币种现金（`SimOrderRepo` 的既有闸门），
    # 计划里的这段省略了入金，那样会停在「账户没有 CNY 现金」而不是测到展开。
    accounts.adjust_cash(
        first.id,
        "CNY",
        100_000.0,
        reason="初始资金",
        actor_id="u1",
        ts=datetime(2024, 10, 24, 6, 30, tzinfo=UTC),
    )
    orders = SimOrderRepo(db)
    orders.record_fill(
        user_id="u1",
        account_id=first.id,
        market="A",
        code=CODE,
        side="buy",
        qty=100.0,
        price=10.0,
        fee=0.0,
        currency="CNY",
        ts=datetime(2024, 10, 25, 6, 30, tzinfo=UTC),  # 建仓日在 `AS_OF` 前一天
    )
    rule = make_rule(
        db,
        kind="stop.pct",
        target_type="position",
        target_ref="*",
        params={"pct": 0.08},
        account_id=first.id,
    )

    got = data.expand(rule)
    assert [(t.account_id, t.code) for t in got] == [(first.id, CODE)]
    assert second.id not in [t.account_id for t in got]


def test_a_thesis_rule_expands_to_that_thesis_symbol(db, data):
    from sams.storage.repos.theses import ThesisRepo

    thesis = ThesisRepo(db).create(
        user_id="u1", market="A", code="600000", style="value", actor_id="u1", now=NOW
    )
    rule = make_rule(
        db,
        kind="thesis.odds_below",
        target_type="thesis",
        target_ref=thesis.id,
        thesis_id=thesis.id,
        # `thesis.odds_below` 不认 `stop.fixed` 的 `price`：沿用默认参数会停在「不认这些参数」
        params={},
    )

    assert data.expand(rule) == [Target("u1", "A", "600000", None, thesis.id)]


def test_an_unsupported_target_type_is_refused_not_expanded_empty(db, data):
    """`portfolio` 进不了库（没有模板认它，T-2 的校验先拒），所以这里是第二条闸。

    返回空列表在这里是最坏的错法：屏幕上那条规则显示「启用」，而它谁也没看。
    """
    rule = make_rule(db)
    broken = make_rule(
        db, kind="price.cross_below", target_type="symbol", target_ref=f"A/{CODE}"
    )
    object.__setattr__(broken, "target_type", "portfolio")  # 绕过入库校验，只测 expand

    with pytest.raises(ValidationError, match="组合"):
        data.expand(broken)
    assert data.expand(rule)  # 正常那条照常展开，证明上面那一枚没有污染夹具


# --- 读取 --------------------------------------------------------------------------------


def test_read_carries_the_price_and_which_day_it_belongs_to(data):
    """现价与「它是哪天的」必须同时出门（§10.2 的新鲜度、§1 的那条谎都靠它）。"""
    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert snap.price is not None
    assert snap.price_on is not None
    assert snap.currency == "CNY"
    assert snap.report_currency == "CNY"
    assert snap.name  # 读不到名字时至少是代码本身，屏幕上不能是空的


def test_read_gives_the_folded_technical_lines(data):
    """三条线都必须是**现价口径**：它们在后复权序列上算，不折就与现价不可比（§10.2 除权那条）。"""
    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert snap.weekly_ma20 is not None
    assert snap.daily_ma60 is not None
    assert snap.atr is not None
    # 折回之后必须与现价同量级：差一个 100 倍就是漏折（样本里现价与 MA 同数量级）
    assert 0.2 <= snap.daily_ma60 / snap.price <= 5.0


def test_a_symbol_with_no_history_at_all_is_refused_not_read_as_empty(db, data):
    """查无此码抛 `NotAdjustable`，不给回一份全是 None 的快照。

    「这个标的读不到」与「这个标的此刻没有止损线」是两句话，合成一份 None 快照等于
    替后者做了证。引擎那一头兜住它（T-14 Step 6.6 的 skipped），取数这一头不许兜。
    """
    with pytest.raises(NotAdjustable, match="600000"):
        data.read(Target("u1", "A", "600000", None, None), as_of=AS_OF)


def test_read_gives_none_not_zero_when_there_is_no_position(db, data):
    """没有持仓时那三列是 None，不是 0。

    `0` 在阈值语言里是一个能触发止损的值，「没有这个数」必须写成 None。这一枚走的是
    有 K 线、无论、无仓的那个形状（`CODE` 在样本里有行情）。
    """
    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert snap.avg_cost is None
    assert snap.qty is None
    assert snap.opened_on is None


def test_a_thesis_line_wins_over_the_synthesized_one(db, data):
    """止损线优先用论上那一列（用户确认过的数），没有才退回本次合成（见 `_stop_line`）。"""
    from sams.storage.repos.theses import ThesisRepo

    theses = ThesisRepo(db)
    thesis = theses.create(
        user_id="u1",
        market="A",
        code=CODE,
        style="value",
        actor_id="u1",
        now=NOW,
    )
    theses.set_levels(
        "u1",
        thesis.id,
        actor_id="u1",
        now=NOW,
        stop_price=9.99,
        target_price=30.0,
        reward_risk=2.0,
    )

    snap = data.read(Target("u1", "A", CODE, None, thesis.id), as_of=AS_OF)
    assert snap.stop_line == 9.99


def test_day_open_and_prev_close_come_from_the_daily_bars(db, data):
    """`urgent` 与 `cross_*` 靠这两个数：库里只有日线，所以它们来自 `BarRepo`（§3 的裁剪）。"""
    from sams.storage.repos.bars import BarRepo

    frame = BarRepo(db).load("A", [CODE], start=AS_OF, end=AS_OF)
    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    if frame.height == 0:
        pytest.skip("样本库在这天没有 K：开盘价本来就该是 None")
    assert snap.day_open == float(frame["open"][0])
    assert snap.prev_close is not None
