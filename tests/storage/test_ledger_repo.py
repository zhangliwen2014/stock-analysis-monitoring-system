"""账本仓库：状态前置条件在 storage 层强制（D81 的形状，同一个理由）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.repos.ledger import Account, SimAccountRepo, SimOrderRepo

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
