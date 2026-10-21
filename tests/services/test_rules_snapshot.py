"""一趟求值的两个半边：`expand`（这条规则覆盖哪些对象）与 `read`（那个对象此刻的数）。

这里断言的全是「屏幕上那条规则到底读了谁」：`watchlist` 展开成零个对象、`position` 用了
别人的账户、`thesis` 取到另一条论——三种都不会报错，只会让一条看起来开着的规则永远不响。

世界由 `support_valuation.make_container` 铺：A/601088 有 300 天日线、两期年报、逐日估值，
时钟冻在 `DAYS[-1]`。用例不联网（stub adapter），所以这里的数是可以逐字断言的。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import support_valuation

from sams.core.errors import NotAdjustable, ValidationError
from sams.services.rules.snapshot import RuleData, Snapshot, Target
from sams.storage.repos.rules import Rule, RuleRepo
from sams.storage.repos.theses import ThesisRepo

CODE = support_valuation.CODE  # "601088"
AS_OF = support_valuation.DAYS[-1]
#: `ThesisRepo` 的时间列是 `datetime`（`date` 会在 `created_at` 那里落成 00:00 的另一种形状）
NOW = datetime(2024, 10, 26, 6, 30, tzinfo=UTC)


def as_data(container, settings) -> RuleData:
    """从容器字段直接装配：构造参数与 `bootstrap.py` 里那些服务是同一批对象。

    故意不在这里重新 `XxxRepo(db)` 一遍——那会造出第二个仓库实例，而用例要验的正是
    「引擎读到的数与屏幕上那个数是同一个」。拆成函数而不是只留夹具，是因为下面有用例
    要换一个铺了除权的容器来装配。
    """
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


@pytest.fixture
def data(db, settings):
    container = support_valuation.make_container(db, settings)
    return as_data(container, settings)


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


def seed_thesis(db, *, on: date | None = None) -> str:
    """铺一条带那两个价位的论，返回它的 id：`on` 就是 0013 的 `levels_on`。

    写成 `None` 是这一枚的意思所在——手填的数没有测量日，仓库那道默认值故意不是今天。
    """
    theses = ThesisRepo(db)
    thesis = theses.create(
        user_id="u1", market="A", code=CODE, style="value", actor_id="u1", now=NOW
    )
    theses.set_levels(
        "u1",
        thesis.id,
        actor_id="u1",
        now=NOW,
        stop_price=9.99,
        target_price=30.0,
        reward_risk=2.0,
        levels_on=on,
    )
    return thesis.id


def read_thesis(db, settings, *, on: date | None = None) -> Snapshot:
    """铺一条论（`on` 是它那两个价位的测量日）然后读一次那个对象。

    四枚用例用的是同一个世界，只差要断言的那一列：`levels_on` 填与不填各是一半，
    止损线与目标价又各有各的伴生函数。
    """
    container = support_valuation.make_container(db, settings)
    thesis_id = seed_thesis(db, on=on)
    return as_data(container, settings).read(
        Target("u1", "A", CODE, None, thesis_id), as_of=AS_OF
    )


# --- 展开 --------------------------------------------------------------------------------


def test_a_symbol_rule_expands_to_exactly_one_target(db, data):
    rule = make_rule(db)
    assert data.expand(rule) == [Target("u1", "A", CODE, None, None)]


def test_a_watchlist_rule_expands_over_the_owners_list(db, data):
    from sams.storage.repos.watchlists import WatchlistRepo

    WatchlistRepo(db).add("u1", "A", CODE)
    WatchlistRepo(db).add("u1", "A", "600000")
    WatchlistRepo(db).add("u2", "A", "000001")
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
    # 第二个账户也持有一只：不铺这一行的话，「只收 A 账户」与「谁都收进来」这一枚都绿
    accounts.adjust_cash(
        second.id,
        "CNY",
        100_000.0,
        reason="初始资金",
        actor_id="u1",
        ts=datetime(2024, 10, 24, 6, 30, tzinfo=UTC),
    )
    orders.record_fill(
        user_id="u1",
        account_id=second.id,
        market="A",
        code="600000",
        side="buy",
        qty=100.0,
        price=10.0,
        fee=0.0,
        currency="CNY",
        ts=datetime(2024, 10, 25, 6, 30, tzinfo=UTC),
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
    assert snap.holding_high is None
    assert snap.holding_days is None


def test_read_expands_a_position_into_the_holding_columns(db, data):
    """持仓那一半也要真读一次：`holdings()` 的形状与 `read(position=...)` 是两处代码。

    样本收盘在 2024-07-19 摸到 19.97（不是上涨段最后一根 19.95），bar high 是
    close+0.10 ⇒ 持仓期最高价 20.07。这一枚同时钉住「用 high_hfq」与「按建仓日过滤」。
    """
    from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo

    accounts = SimAccountRepo(db)
    first = accounts.open("u1", "甲券商", market_scope=("A",))
    accounts.adjust_cash(
        first.id,
        "CNY",
        100_000.0,
        reason="初始资金",
        actor_id="u1",
        ts=datetime(2024, 4, 10, 6, 30, tzinfo=UTC),
    )
    SimOrderRepo(db).record_fill(
        user_id="u1",
        account_id=first.id,
        market="A",
        code=CODE,
        side="buy",
        qty=100.0,
        price=10.0,
        fee=0.0,
        currency="CNY",
        ts=datetime(2024, 4, 10, 6, 30, tzinfo=UTC),
    )
    position = next(row for row in data.holdings(first.id) if row.code == CODE)

    snap = data.read(Target("u1", "A", CODE, first.id, None), as_of=AS_OF, position=position)

    assert snap.qty == 100.0
    assert snap.avg_cost == 10.0
    assert snap.opened_on == date(2024, 4, 10)
    assert snap.holding_days == 199  # AS_OF 是 2024-10-26
    assert snap.currency == "CNY"
    assert snap.account_name == "甲券商"
    assert snap.margin_enabled is False
    assert snap.holding_high == pytest.approx(20.07)


def test_a_dirty_high_low_bar_takes_down_atr_not_the_whole_read(db, data):
    """一根 high/low 拒收的 K 只该让 ATR 退成 None：它已经把价格类判断排除在外了，
    而整趟求值不该因为一条规则不需要的那一列而全线不响（T-14 只兜 NotAdjustable）。"""
    from sams.storage.repos.bars import BarRepo

    # `load` 带出来的 `adjust` 列是库里的口径标记，`_prepare` 拒收它（口径由仓库层固定）
    frame = BarRepo(db).load("A", [CODE], start=AS_OF, end=AS_OF).drop("adjust")
    BarRepo(db).upsert(frame.with_columns(high=0.0, low=0.0))

    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)
    assert snap.daily_ma60 is not None
    assert snap.atr is None
    assert any("ATR" in note for note in snap.missing)


def test_the_technical_lines_are_folded_when_a_dividend_sits_inside_the_window(db, settings):
    """常数 10 元 + 早期一次 10 派 1.0：后复权序列会抬到 11.11，折回之后每条线仍是 10.0。

    删掉任何一个 `fold_to_price_basis`，这一枚就会红——现价口径不是「数量级对」就够。
    """
    flat = [10.0] * len(support_valuation.DAYS)
    container = support_valuation.make_container(
        db,
        settings,
        closes=flat,
        actions=support_valuation.dividend(support_valuation.DAYS[10]),
    )
    data = as_data(container, settings)

    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)
    assert snap.price == pytest.approx(10.0)
    assert snap.prev_close == pytest.approx(10.0)
    assert snap.daily_ma60 == pytest.approx(10.0)
    assert snap.weekly_ma20 == pytest.approx(10.0)
    assert snap.atr == pytest.approx(0.2)


def test_a_thesis_line_wins_over_the_synthesized_one(db, data):
    """止损线优先用论上那一列（用户确认过的数），没有才退回本次合成（见 `_stop`）。"""
    thesis_id = seed_thesis(db)

    snap = data.read(Target("u1", "A", CODE, None, thesis_id), as_of=AS_OF)
    assert snap.stop_line == 9.99
    assert snap.target_price == 30.0


def test_day_open_and_prev_close_come_from_the_daily_bars(db, data):
    """`urgent` 与 `cross_*` 靠这两个数：库里只有日线，所以它们来自 `BarRepo`（§3 的裁剪）。"""
    from sams.storage.repos.bars import BarRepo

    frame = BarRepo(db).load("A", [CODE], start=AS_OF, end=AS_OF)
    snap = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    if frame.height == 0:
        pytest.skip("样本库在这天没有 K：开盘价本来就该是 None")
    assert snap.day_open == float(frame["open"][0])
    assert snap.prev_close is not None


# --- 除权那四列（§10.2 的输入，`anchor.move_prices` 吃它们） --------------------------------


def test_read_carries_the_ex_rights_calendar_and_its_floor(db, settings):
    """除权表与「这段 K 线从哪天起」必须一起出门：少了后者就无法证明前者是完整的。

    数是量出来的，不是编的：样本世界倒数第二天收 17.03，10 派 1.0 ⇒ 除权参考价
    17.03 − 1.00 = 16.03，当日因子 = 17.03 / 16.03。
    """
    container = support_valuation.make_container(
        db, settings, actions=support_valuation.dividend(AS_OF)
    )
    got = as_data(container, settings).read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert [step.ex_date for step in got.factors] == [AS_OF]
    assert got.factors[0].factor == pytest.approx(17.03 / 16.03)
    assert got.bars_from == support_valuation.DAYS[0]


def test_read_leaves_the_calendar_empty_when_nothing_was_recorded(data):
    """库里那段没有除权 ⇒ 空表，不是 None、也不是拒绝。

    这是 `move_prices` 那条快路径的入口，也是「默认免搬」的全部根据：0017 之前建的那
    些行没有日期戳，空表如果也算缺口，屏幕上会是一片没有结论的规则。
    """
    got = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert got.factors == ()
    # 覆盖确认是全历史 ⇒ 预热一路铺到库里第一根，`bars_from` 就是它
    assert got.bars_from == support_valuation.DAYS[0]


def test_a_thesis_line_carries_its_reading_day(db, settings):
    """论上那一列的日期在 0013：填了 `levels_on`，快照就得把它带出门。"""
    got = read_thesis(db, settings, on=support_valuation.DAYS[0])
    assert got.stop_line == 9.99
    assert got.stop_on == support_valuation.DAYS[0]


def test_a_hand_typed_thesis_line_has_no_reading_day(db, settings):
    """手填的价位没有测量日（`levels_on` 是 NULL）：带 None 出门，让 `anchor.py` 去注明。

    拿 `now` 当测量日是这一路最顺手的一种假：那是这一行被写下的时刻，不是那个数属于
    哪一天的读数（0013 的注释管这个叫「把填数说成量数」）。
    """
    got = read_thesis(db, settings)
    assert got.stop_line == 9.99
    assert got.stop_on is None


def test_a_synthesized_line_is_anchored_to_the_day_it_was_read(data):
    """没有论时那条线是本次合成的 ⇒ 锚就是这次的读数日，不是 None。

    这一枚与上一枚是同一条优先级的两面（`_stop` 一次给回那两个值）。写成 None 会给
    一条本来就在现价坐标系里的数加一句假的「可能偏高」。
    """
    got = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert got.stop_line is not None  # 样本世界里这条计划的止损线是有数的
    assert got.stop_on == got.price_on


def test_a_thesis_target_carries_its_reading_day(db, settings):
    """目标价与止损线同住 0013 那几列：填了 `levels_on`，它也得带日期出门。

    两条列分开写用例，是因为取数处对它们是两个函数（`_stop` / `_target`），只钉一边的话，
    另一边写成 `levels_on` 以外的一切都还是绿的。
    """
    got = read_thesis(db, settings, on=support_valuation.DAYS[0])
    assert got.target_price == 30.0
    assert got.target_on == support_valuation.DAYS[0]


def test_a_hand_typed_target_has_no_reading_day(db, settings):
    """手填的那个目标价同样没有测量日：带 None 出门，让 `anchor.py` 去注明。"""
    got = read_thesis(db, settings)
    assert got.target_price == 30.0
    assert got.target_on is None


def test_a_synthesized_target_is_anchored_to_the_day_it_was_read(data):
    """本次合成的那个目标价（`tp.tiered` 拿它当刻度终点）锚在这次的读数日。

    先量过一次：样本世界里没有论时 `plan.target.ok` 为真，所以这一枚走的是「有数」
    那一支。写成 None 会让一条今天读来的数带上「可能偏高」那句假话（与 `_stop`
    的同一条理由）。
    """
    got = data.read(Target("u1", "A", CODE, None, None), as_of=AS_OF)

    assert got.target_price is not None
    assert got.target_on == got.price_on
