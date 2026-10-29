"""账本仓库：状态前置条件在 storage 层强制（D81 的形状，同一个理由）。"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from sams.core.config import MarketCfg
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.repos.ledger import Account, SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo

USER = "u1"


@pytest.fixture
def accounts(db: Database) -> SimAccountRepo:
    return SimAccountRepo(db)


def test_opening_an_account_returns_the_row_we_can_read_back(accounts: SimAccountRepo) -> None:
    account = accounts.open(USER, "主账户", report_currency="CNY")
    assert isinstance(account, Account)
    assert accounts.get(USER, account.id).name == "主账户"
    assert accounts.list_for(USER)[0].id == account.id


def test_two_users_can_have_accounts_with_the_same_name(accounts: SimAccountRepo) -> None:
    """名字的唯一键是 (user_id, name)：`0001` 就是这么定的，仓库不许放宽。"""
    accounts.open(USER, "主账户", report_currency="CNY")
    accounts.open("u2", "主账户", report_currency="CNY")
    assert len(accounts.list_for(USER)) == 1


def test_a_duplicate_name_for_one_user_is_a_business_refusal(accounts: SimAccountRepo) -> None:
    """不是 ValidationError：重名是「这一步不让走」，不是「参数打错了」（D77）。"""
    accounts.open(USER, "主账户", report_currency="CNY")
    with pytest.raises(StorageError, match="已存在"):
        accounts.open(USER, "主账户", report_currency="CNY")


def test_an_empty_account_name_is_a_typo(accounts: SimAccountRepo) -> None:
    with pytest.raises(ValidationError, match="空"):
        accounts.open(USER, "  ", report_currency="CNY")


def test_renaming_an_account_reads_back_under_the_new_name(accounts: SimAccountRepo) -> None:
    """开户时把名字打错（「中国银行」/「中国银河」）只能开一个新户？不：改名读得回来。"""
    account = accounts.open(USER, "文-中国银行", report_currency="CNY")
    renamed = accounts.rename(USER, account.id, " 文-中国银河 ")
    assert renamed.name == "文-中国银河"
    assert accounts.get(USER, account.id).name == "文-中国银河"
    # 改名不动身份：id 与币种还是那一行
    assert renamed.id == account.id
    assert renamed.report_currency == account.report_currency


def test_renaming_onto_an_existing_name_is_the_same_business_refusal(
    accounts: SimAccountRepo,
) -> None:
    accounts.open(USER, "主账户", report_currency="CNY")
    other = accounts.open(USER, "备用", report_currency="CNY")
    with pytest.raises(StorageError, match="已存在"):
        accounts.rename(USER, other.id, "主账户")


def test_renaming_to_a_blank_name_is_a_typo(accounts: SimAccountRepo) -> None:
    account = accounts.open(USER, "主账户", report_currency="CNY")
    with pytest.raises(ValidationError, match="空"):
        accounts.rename(USER, account.id, "  ")
    assert accounts.get(USER, account.id).name == "主账户"


def test_another_user_cannot_rename_the_account(accounts: SimAccountRepo) -> None:
    account = accounts.open(USER, "主账户", report_currency="CNY")
    with pytest.raises(StorageError, match="不存在"):
        accounts.rename("u2", account.id, "抢来的名字")
    assert accounts.get(USER, account.id).name == "主账户"


def test_another_user_cannot_read_the_account(accounts: SimAccountRepo) -> None:
    """用户隔离与 watchlists/theses 同形：没有「查全量再由上层过滤」的接口。"""
    account = accounts.open(USER, "主账户", report_currency="CNY")
    assert accounts.get("u2", account.id) is None
    assert accounts.list_for("u2") == []


def test_cash_starts_empty_per_currency_not_zero_everywhere(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    assert accounts.cash_balances(account_id) == {}
    accounts.adjust_cash(account_id, "CNY", 100_000.0, reason="初始资金", actor_id=USER)
    assert accounts.cash_balances(account_id) == {"CNY": pytest.approx(100_000.0)}


def test_every_cash_move_leaves_an_audit_row(accounts: SimAccountRepo) -> None:
    """§5.4 规则 4 的 M3 版本：换汇留 M5，但**手工**动现金从第一天起就得有出处。"""
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 100_000.0, reason="初始资金", actor_id=USER)
    accounts.adjust_cash(account_id, "CNY", -20_000.0, reason="转出", actor_id=USER)
    history = accounts.cash_history(account_id)
    assert history["delta"].to_list() == [100_000.0, -20_000.0]
    assert history["reason"].to_list() == ["初始资金", "转出"]


def test_balances_and_moves_are_written_together_or_not_at_all(accounts: SimAccountRepo) -> None:
    """留一行没有余额变动的流水，比留一个对不上流水的余额更难查。

    这里用「给不存在的账户调现金」触发失败：写流水在前、更新余额在后时，
    前半段会留在库里。
    """
    with pytest.raises(StorageError):
        accounts.adjust_cash("不存在", "CNY", 10.0, reason="试探", actor_id=USER)
    assert accounts.cash_history("不存在").height == 0


def test_cash_cannot_go_negative_without_margin(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 100.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="现金不足"):
        accounts.adjust_cash(account_id, "CNY", -101.0, reason="透支", actor_id=USER)
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(100.0)


def test_a_margin_account_may_go_negative(accounts: SimAccountRepo) -> None:
    """`margin_enabled` 是 0001 就有的列；不认它等于把这张列变成装饰。"""
    account_id = accounts.open(
        USER, "杠杆", report_currency="CNY", margin_enabled=True
    ).id
    accounts.adjust_cash(account_id, "CNY", -100.0, reason="融资", actor_id=USER)
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(-100.0)


def test_an_empty_reason_is_refused(accounts: SimAccountRepo) -> None:
    """审计行没有理由就不是审计：留 `""` 会让这张表在第一周就变成没人读的日志。"""
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    with pytest.raises(ValidationError, match="理由"):
        accounts.adjust_cash(account_id, "CNY", 1.0, reason="", actor_id=USER)


def test_a_zero_delta_is_refused(accounts: SimAccountRepo) -> None:
    """0 不是「没动」的合法表达，它是一条会出现在审计报告里的假流水。"""
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    with pytest.raises(ValidationError, match="0"):
        accounts.adjust_cash(account_id, "CNY", 0.0, reason="占位", actor_id=USER)


def test_an_unknown_currency_is_refused(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "A", report_currency="CNY").id
    with pytest.raises(ValidationError, match="币种"):
        accounts.adjust_cash(account_id, "RMB", 1.0, reason="试探", actor_id=USER)


def test_a_bare_timestamp_is_refused(accounts: SimAccountRepo) -> None:
    """TIMESTAMPTZ 会按会话时区解释无时区的值：A 股的一笔会被记成 UTC，差 8 小时。"""
    account_id = accounts.open(USER, "T", report_currency="CNY").id
    with pytest.raises(ValidationError, match="时区"):
        accounts.adjust_cash(
            account_id, "CNY", 1.0, reason="试探", actor_id=USER,
            ts=datetime(2024, 10, 25, 15, 0),  # 裸时刻
        )


@pytest.fixture
def fills(db: Database) -> SimOrderRepo:
    return SimOrderRepo(db)


def _funded(accounts: SimAccountRepo, currency: str = "CNY", amount: float = 100_000.0) -> str:
    account_id = accounts.open(USER, f"F{currency}", report_currency="CNY").id
    accounts.adjust_cash(account_id, currency, amount, reason="初始", actor_id=USER)
    return account_id


def _fill(fills: SimOrderRepo, account_id: str, side: str, **over: object) -> object:
    """三条用例共用的那笔成交：只有标的与数量会变，写在参数里比复制四遍清楚。"""
    fields: dict[str, object] = {
        "account_id": account_id, "user_id": USER, "market": "A", "code": "601088",
        "side": side, "qty": 100, "price": 40.0, "currency": "CNY", "fee": 0.0,
    }
    fields.update(over)
    return fills.record_fill(**fields)  # type: ignore[arg-type]


def _order_rows(db: Database, account_id: str) -> int:
    """直接数库里的订单行：拒绝路径要证明「什么都没写」，不能只数仓库方法的返回值。"""
    return int(
        db.scalar("SELECT count(*) FROM sim_orders WHERE account_id = ?", [account_id]) or 0
    )


def test_a_buy_writes_order_position_and_both_cash_rows(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    fills.record_fill(
        account_id=account_id, user_id=USER, market="A", code="601088",
        side="buy", qty=100, price=40.0, currency="CNY", fee=5.0,
    )
    assert fills.order_rows(account_id) == 1
    pos = fills.position(account_id, "A", "601088")
    # 手续费计入成本：100 股 4000 元 + 5 元费 → 均价 40.05
    assert pos.avg_cost == pytest.approx(40.05)
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(95_995.0)
    assert accounts.cash_history(account_id)["reason"].to_list() == ["初始", "买入 601088"]


def test_a_second_buy_blends_the_average_cost(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy")
    _fill(fills, account_id, "buy", price=44.0)
    assert fills.position(account_id, "A", "601088").avg_cost == pytest.approx(42.0)


def test_a_full_sale_keeps_the_position_row_and_zeroes_the_average_cost(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """清仓不删行：删掉行就删掉了「我持有过它」这条历史，而 §9.3 的周复检要看得见的
    是「已经没有了」，不是「从来没买过」。均价留 0 而不是留最后一笔成本。"""
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy")
    _fill(fills, account_id, "sell", price=44.0)
    pos = fills.position(account_id, "A", "601088")
    assert pos.qty == 0
    assert pos.avg_cost == pytest.approx(0.0)
    assert fills.realized_pnl(account_id, "A", "601088") == pytest.approx(400.0)


def test_a_partial_sale_keeps_the_average_cost(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """卖掉一半不动均价：卖出的那部分按比例带走成本，剩下的还是原来那个成本基础。"""
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy", price=40.0)
    _fill(fills, account_id, "sell", qty=40, price=44.0)
    pos = fills.position(account_id, "A", "601088")
    assert pos.qty == pytest.approx(60.0)
    assert pos.avg_cost == pytest.approx(40.0)


def test_overselling_is_refused_before_anything_is_written(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy")
    with pytest.raises(StorageError, match="超卖"):
        _fill(fills, account_id, "sell", qty=101, price=44.0)
    assert fills.order_rows(account_id) == 1
    assert fills.position(account_id, "A", "601088").qty == 100


def test_a_buy_that_outdraws_the_cash_writes_nothing(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """「现金已扣、持仓未建」不是待修的脏数据，是那本账作废：整段必须回滚。"""
    account_id = _funded(accounts, amount=1_000.0)
    with pytest.raises(StorageError, match="现金不足"):
        _fill(fills, account_id, "buy")
    assert fills.order_rows(account_id) == 0
    assert fills.positions(account_id) == []
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(1_000.0)


def test_an_unknown_side_is_a_typo_error_not_a_business_refusal(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    with pytest.raises(ValidationError, match="side"):
        _fill(fills, account_id, "hold")


def test_a_non_finite_price_or_quantity_is_refused(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """inf/nan 会一路乘进汇总，最后表现为「总市值是 nan」，最难倒查。"""
    account_id = _funded(accounts)
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            _fill(fills, account_id, "buy", qty=bad)


def test_a_negative_fee_is_refused(accounts: SimAccountRepo, fills: SimOrderRepo) -> None:
    """负手续费 = 交易所倒贴钱，而且它是「把成本做低」最方便的入口。"""
    account_id = _funded(accounts)
    with pytest.raises(ValidationError, match="手续费"):
        _fill(fills, account_id, "buy", fee=-5.0)


def test_a_negative_price_is_refused(accounts: SimAccountRepo, fills: SimOrderRepo) -> None:
    """负价格是一笔「买它还倒找钱」的成交：毛额是负的，现金反而变多，
    而 `blended_cost` 会存下一个看着完全正常的负均价——此后这一行每一笔 `price_pnl`
    都是错数，且没有哪一层会觉得不对。退款/调整是现金动作，走 `sams ledger cash`。
    """
    account_id = _funded(accounts)
    with pytest.raises(ValidationError, match="价格 必须大于 0"):
        _fill(fills, account_id, "buy", price=-1500.0)
    assert fills.order_rows(account_id) == 0
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(100_000.0)


def test_orders_can_be_listed_by_thesis(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """U26 的交叉核对靠这一条：`sim_orders.thesis_id` 就是「有没有真按这个价位成交」。"""
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy", thesis_id="th1")
    assert [o.thesis_id for o in fills.orders_for_thesis("th1")] == ["th1"]
    assert fills.orders_for_thesis("th2") == []


def test_positions_list_carries_the_currency_of_each_row(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy")
    assert fills.positions(account_id)[0].currency == "CNY"


def test_the_open_time_is_the_first_fill_not_the_last(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """§9.3 的「拿了多久」看第一次建仓那一刻；覆盖它等于抹掉这段历史。"""
    from datetime import UTC, datetime, timedelta

    account_id = _funded(accounts)
    first = datetime(2024, 1, 3, 15, 0, tzinfo=UTC)
    _fill(fills, account_id, "buy", ts=first)
    _fill(fills, account_id, "buy", ts=first + timedelta(days=200))
    assert fills.position(account_id, "A", "601088").open_ts == first


def _markets() -> dict[str, MarketCfg]:
    return {
        "A": MarketCfg(
            market="A", tz="Asia/Shanghai", currency="CNY",
            post_close_min=30, sources=("akshare",),
        ),
        "HK": MarketCfg(
            market="HK", tz="Asia/Hong_Kong", currency="HKD",
            post_close_min=30, sources=("akshare",),
        ),
        "US": MarketCfg(
            market="US", tz="America/New_York", currency="USD",
            post_close_min=30, sources=("yfinance",),
        ),
    }


@pytest.fixture
def gated(db: Database) -> SimOrderRepo:
    return SimOrderRepo(db, markets=_markets(), symbols=SymbolRepo(db))


def test_a_b_share_cannot_be_bought_with_the_market_currency(
    accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    """200011 是深市 B 股，HKD 报价（`codes.py:52`）。按 CNY 记账会污染整条链。"""
    account_id = accounts.open(USER, "G", report_currency="CNY").id
    accounts.adjust_cash(account_id, "HKD", 10_000.0, reason="初始", actor_id=USER)
    with pytest.raises(ValidationError, match="HKD.*CNY|CNY.*HKD"):
        gated.record_fill(
            account_id=account_id, user_id=USER, market="A", code="200011",
            side="buy", qty=100, price=3.0, currency="CNY", fee=0.0,
        )


def test_the_override_currency_is_accepted(
    accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    account_id = accounts.open(USER, "G2", report_currency="CNY").id
    accounts.adjust_cash(account_id, "HKD", 10_000.0, reason="初始", actor_id=USER)
    order = gated.record_fill(
        account_id=account_id, user_id=USER, market="A", code="200011",
        side="buy", qty=100, price=3.0, currency="HKD", fee=0.0,
    )
    assert order.currency == "HKD"


def test_a_collected_symbol_currency_that_disagrees_is_refused(
    db: Database, accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    """库里 `symbols.currency` 是采集时写下的事实；它和推定值不一致说明有一条链路错了。

    这时候**两边都不能信**：记账选一个、展示按另一个，用户看到的是同一只票两个价格。
    所以报错要说「请先 sync 或核对代码」，而不是替用户挑一个。
    """
    SymbolRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "name": ["中国神华"],
                "currency": ["USD"],  # 故意写错
                "listed_on": [date(2007, 10, 9)],
            }
        )
    )
    account_id = accounts.open(USER, "G3", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="601088"):
        gated.record_fill(
            account_id=account_id, user_id=USER, market="A", code="601088",
            side="buy", qty=100, price=40.0, currency="CNY", fee=0.0,
        )
    assert _order_rows(db, account_id) == 0


def test_a_symbol_row_that_agrees_is_not_a_problem(
    db: Database, accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    SymbolRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "name": ["中国神华"],
                "currency": ["CNY"],
                "listed_on": [date(2007, 10, 9)],
            }
        )
    )
    account_id = accounts.open(USER, "G4", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    assert gated.record_fill(
        account_id=account_id, user_id=USER, market="A", code="601088",
        side="buy", qty=100, price=40.0, currency="CNY", fee=0.0,
    )


def test_no_markets_configured_means_no_gate(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """闸门是加的，不是既有的：没传 cfg 的调用方（测试、早期脚本）行为不变。

    这样「忘了传 markets」变成少一道保护而不是拒掉所有单——反过来写会让 T-4 的
    测试全部报错，而报错原因和币种毫无关系。
    """
    account_id = _funded(accounts)
    assert _fill(fills, account_id, "buy")


def test_cross_currency_cash_is_refused_naming_both(
    accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    """USD 定价的标的不能花 CNY 现金买（M3 不做自动换汇，§5.4 规则 4 把换汇留给流水）。"""
    account_id = accounts.open(USER, "G5", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="USD.*CNY|CNY.*USD"):
        gated.record_fill(
            account_id=account_id, user_id=USER, market="US", code="AAPL",
            side="buy", qty=1, price=200.0, currency="USD", fee=0.0,
        )


def test_the_cross_currency_refusal_writes_no_order(
    db: Database, accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    """报错点名了两个币种还不够，那笔单本身不能留下痕迹。"""
    account_id = accounts.open(USER, "G6", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError):
        gated.record_fill(
            account_id=account_id, user_id=USER, market="US", code="AAPL",
            side="buy", qty=1, price=200.0, currency="USD", fee=0.0,
        )
    assert _order_rows(db, account_id) == 0
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(10_000.0)


# --- 撤销成交：录错市场/代码之后唯一的出口（D205） ---------------------------------


def test_undoing_a_buy_gives_the_cash_back_to_the_cent(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    order = _fill(fills, account_id, "buy", fee=5.0)
    assert fills.undo_fill(account_id, order.id, actor_id=USER)
    assert fills.order_rows(account_id) == 0
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(100_000.0)
    # 0 股行是历史不是持仓（§5.4）：撤销不删这一行，但也不许它带着成本
    pos = fills.position(account_id, "A", "601088")
    assert pos.qty == pytest.approx(0.0)
    assert pos.avg_cost == pytest.approx(0.0)
    assert accounts.cash_history(account_id)["reason"].to_list() == [
        "初始", "买入 601088", f"撤销成交 {order.id}",
    ]


def test_undoing_a_sell_puts_the_shares_back_at_the_old_average(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """卖出一笔之后撤掉它：股数与均价回到卖出前，不是「按撤销时的价格重建」。"""
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy", qty=100, price=40.0, fee=5.0)
    sold = _fill(fills, account_id, "sell", qty=40, price=50.0, fee=3.0)
    assert fills.undo_fill(account_id, sold.id, actor_id=USER)
    pos = fills.position(account_id, "A", "601088")
    assert pos.qty == pytest.approx(100.0)
    assert pos.avg_cost == pytest.approx(40.05)
    assert accounts.cash_balances(account_id)["CNY"] == pytest.approx(95_995.0)


def test_undoing_a_buy_that_later_sells_relied_on_is_refused(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """重放时后面的卖出会超卖：这不是「能撤」，是要先撤后面那笔。"""
    account_id = _funded(accounts)
    first = _fill(fills, account_id, "buy", qty=100, price=40.0)
    _fill(fills, account_id, "sell", qty=100, price=45.0)
    with pytest.raises(StorageError, match="超卖|先撤销"):
        fills.undo_fill(account_id, first.id, actor_id=USER)
    # 拒绝路径什么都不动
    assert fills.order_rows(account_id) == 2
    assert fills.position(account_id, "A", "601088").qty == pytest.approx(0.0)


def test_undoing_a_sell_whose_proceeds_were_spent_is_refused(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    """撤销售出要扣回这笔钱；扣完为负就是透支，无杠杆账户不许。"""
    account_id = _funded(accounts)
    _fill(fills, account_id, "buy", qty=100, price=40.0, fee=0.0)
    sold = _fill(fills, account_id, "sell", qty=100, price=40.0, fee=0.0)
    accounts.adjust_cash(account_id, "CNY", -99_990.0, reason="花掉", actor_id=USER)
    with pytest.raises(StorageError, match="现金不足"):
        fills.undo_fill(account_id, sold.id, actor_id=USER)
    assert fills.order_rows(account_id) == 2


def test_undoing_an_order_that_does_not_exist_is_refused(
    accounts: SimAccountRepo, fills: SimOrderRepo
) -> None:
    account_id = _funded(accounts)
    with pytest.raises(StorageError, match="不存在"):
        fills.undo_fill(account_id, "no-such-order", actor_id=USER)


# --- 跨市场误记：A 股表单里填了港股通代码（D206） -----------------------------------


def test_a_code_that_lives_in_another_market_is_refused(
    db: Database, accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    """选了 A 却填 HK 00700：A 的主档没有它、HK 有——这不是「新票」，是记错了市场。"""
    SymbolRepo(db).upsert(
        pl.DataFrame(
            {
                "market": ["HK"], "code": ["00700"], "name": ["腾讯控股"],
                "currency": ["HKD"], "listed_on": [date(2004, 6, 16)],
            }
        )
    )
    account_id = accounts.open(USER, "G7", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="HK"):
        gated.record_fill(
            account_id=account_id, user_id=USER, market="A", code="00700",
            side="buy", qty=100, price=400.0, currency="CNY", fee=0.0,
        )
    assert _order_rows(db, account_id) == 0


def test_a_code_missing_from_every_market_still_records(
    db: Database, accounts: SimAccountRepo, gated: SimOrderRepo
) -> None:
    """主档两边都没有不等于记错了：新上市、退市整理、还没同步都长这样，照收。"""
    account_id = accounts.open(USER, "G8", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    assert gated.record_fill(
        account_id=account_id, user_id=USER, market="A", code="999999",
        side="buy", qty=100, price=1.0, currency="CNY", fee=0.0,
    )


# --- 换汇：港股通先换 HKD 再买（D207） ----------------------------------------------


def test_converting_moves_both_balances_and_writes_one_fx_trade(
    db: Database, accounts: SimAccountRepo
) -> None:
    account_id = accounts.open(USER, "F1", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    trade = accounts.convert(
        account_id, "CNY", "HKD", 1_000.0, 1.08, actor_id=USER
    )
    assert accounts.cash_balances(account_id) == {
        "CNY": pytest.approx(9_000.0),
        "HKD": pytest.approx(1_080.0),
    }
    row = db.query("SELECT * FROM fx_trades WHERE account_id = ?", [account_id]).to_dicts()[0]
    assert row["from_ccy"] == "CNY" and row["to_ccy"] == "HKD"
    assert row["from_amt"] == pytest.approx(1_000.0)
    assert row["to_amt"] == pytest.approx(1_080.0)
    assert row["rate"] == pytest.approx(1.08)
    assert trade.from_amt == pytest.approx(1_000.0)
    assert accounts.cash_history(account_id)["reason"].to_list() == [
        "初始", "换汇 CNY→HKD @1.0800", "换汇 CNY→HKD @1.0800",
    ]


def test_converting_the_same_currency_is_a_typo(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "F2", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 1_000.0, reason="初始", actor_id=USER)
    with pytest.raises(ValidationError, match="不同"):
        accounts.convert(account_id, "CNY", "CNY", 100.0, 1.0, actor_id=USER)


def test_a_non_positive_rate_or_amount_is_a_typo(accounts: SimAccountRepo) -> None:
    account_id = accounts.open(USER, "F3", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 1_000.0, reason="初始", actor_id=USER)
    with pytest.raises(ValidationError, match="汇率"):
        accounts.convert(account_id, "CNY", "HKD", 100.0, 0.0, actor_id=USER)
    with pytest.raises(ValidationError, match="金额"):
        accounts.convert(account_id, "CNY", "HKD", 0.0, 1.0, actor_id=USER)


def test_converting_more_than_the_balance_is_refused(
    accounts: SimAccountRepo, db: Database
) -> None:
    account_id = accounts.open(USER, "F4", report_currency="CNY").id
    accounts.adjust_cash(account_id, "CNY", 100.0, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="现金不足"):
        accounts.convert(account_id, "CNY", "HKD", 200.0, 1.08, actor_id=USER)
    assert db.scalar("SELECT count(*) FROM fx_trades WHERE account_id = ?", [account_id]) == 0
    assert accounts.cash_balances(account_id) == {"CNY": pytest.approx(100.0)}
