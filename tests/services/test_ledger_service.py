"""账本写用例（spec §5.4、§9.1 的 thesis↔成交那条链）。

`--user` 在屏幕上是一个用户名，库里认的是 `users.id`。这一步解析放在服务层（同
`ThesisService._actor`）：放在命令层就成了「每个入口记得查一遍」，而漏掉的那个不会报错，
只会留下一条挂在不存在的用户身上的账——`buy --thesis` 那条链也因此必须走同一个 id。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sams.core.errors import StorageError, ValidationError
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import (
    Order,
    SimAccountRepo,
    SimOrderRepo,
)
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"
#: 立论那三步要落时刻（0013 的测量日、时间线的 ts），测试里冻死它，不看真实钟表
NOW = datetime(2024, 10, 26, 2, 0, tzinfo=UTC)
#: 占位汇率那笔的成交时刻：HK 本地 15:00，回算按这一天去汇率表取数
FILL_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)
FILL_DAY = date(2024, 6, 3)


def _placeholder_fill(svc: LedgerService, db: Database, account_id: str) -> Order:
    """把一笔港股通成交还原成 D213 之前的样子：汇率两列是 1.0，CNY 也按 1.0 扣过。

    `record_fill` 现在拒 1.0（它是唯一一条 INSERT 路径），存量就得手工还原。还原要连现金
    一起改：只动汇率列，被测的是一笔本来就没扣错钱的账——补出来的差额是真的，场景是假的。
    """
    order = svc.buy(
        account_id, USER, "HK", "01021", qty=10, price=400.0, fx_rate=0.92, ts=FILL_TS
    )
    # 占位值该扣的与当初真扣的差多少（fee=0，所以就是名义额 × (1 − 0.92)）
    short = order.qty * order.price * (1.0 - 0.92)
    db.execute(
        "UPDATE sim_orders SET fx_rate_ref = 1.0, fx_rate_applied = 1.0 WHERE id = ?",
        [order.id],
    )
    db.execute(
        "UPDATE sim_cash_moves SET delta = delta - ?"
        " WHERE account_id = ? AND kind = 'buy'",
        [short, account_id],
    )
    db.execute(
        "UPDATE sim_cash_balances SET amount = amount - ?"
        " WHERE account_id = ? AND currency = 'CNY'",
        [short, account_id],
    )
    return order


def _adjust_rows(db: Database, account_id: str) -> list[dict]:
    return db.query(
        "SELECT currency, delta, reason, actor_id FROM sim_cash_moves"
        " WHERE account_id = ? AND kind = 'adjust' ORDER BY ts",
        [account_id],
    ).to_dicts()


@pytest.fixture
def svc(db: Database, settings) -> LedgerService:
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    users = UserRepo(db)
    users.create(USER, PWD)
    return LedgerService(accounts, orders, ThesisRepo(db), users, settings)


def _uid(svc: LedgerService, username: str = USER) -> str:
    """仓库层要的那个 id：用例里直接写仓库时得用它，不能用用户名。"""
    return svc.users.get_by_username(username).id


def _funded(svc: LedgerService, name: str) -> str:
    account = svc.open_account(USER, name)
    svc.deposit(account.id, "CNY", 100_000.0, username=USER, reason="初始", actor_id=USER)
    return account.id


def _opened_thesis(db: Database, user_id: str) -> str:
    """走到 opened 的一条立论：挂成交要的就是这个状态。"""
    repo = ThesisRepo(db)
    thesis = repo.create(
        user_id=user_id, market="A", code="601088", style="PR", actor_id=user_id, now=NOW
    )
    repo.advance(user_id, thesis.id, "proposed", actor_id=user_id, now=NOW)
    repo.set_levels(
        user_id,
        thesis.id,
        actor_id=user_id,
        now=NOW,
        entry_low=38.0,
        entry_high=40.0,
        stop_price=34.0,
        target_price=50.0,
        reward_risk=1.8,
    )
    repo.advance(user_id, thesis.id, "opened", actor_id=user_id, now=NOW)
    return thesis.id


def test_open_account_delegates_with_the_configured_report_currency(svc) -> None:
    account = svc.open_account(USER, "主账户")
    assert account.report_currency == "CNY"


def test_open_account_keeps_the_home_market_and_canonicalizes_it(svc) -> None:
    """主场市场决定录入成交默认选哪个：写错大小写不该变成另一个市场（D208）。"""
    assert svc.open_account(USER, "A 户").home_market == "A"
    assert svc.open_account(USER, "港户", home_market="hk").home_market == "HK"


def test_a_home_market_that_is_not_configured_is_a_typo(svc) -> None:
    """未配置市场的币种推不出来：开户这一步就拒，别等第一笔成交才炸（D84 同一道闸）。"""
    with pytest.raises(ValidationError, match="JP"):
        svc.open_account(USER, "日户", home_market="JP")


def test_a_username_that_is_not_in_the_library_is_refused_with_the_fix(svc) -> None:
    """不是 ValidationError 就错了：`--user trder` 是参数打错了（2），不是业务拒绝。"""
    with pytest.raises(ValidationError, match="sams user add"):
        svc.open_account("trder", "主账户")


def test_a_disabled_user_is_refused(svc) -> None:
    """「禁用即停止一切动作」要覆盖记账：只对读生效的禁用等于没禁用。"""
    svc.users.disable(_uid(svc))
    with pytest.raises(ValidationError, match="禁用"):
        svc.open_account(USER, "主账户")


def test_writing_into_another_users_account_is_refused(svc, db) -> None:
    """仓库层的 `_require` 只查存在、不查归属：绕过服务就写得进别人的账。"""
    other = SimAccountRepo(db).open("someone-else", "别人的账户", report_currency="CNY")
    account_id = _funded(svc, "我的")
    with pytest.raises(StorageError, match="不属于"):
        svc.buy(other.id, USER, "A", "601088", qty=100, price=40.0)
    assert svc.cash(account_id) == {"CNY": 100_000.0}


def test_rename_account_resolves_the_username_and_reads_back(svc) -> None:
    """屏幕上写的是用户名，库里认 id：改名和其它写面走同一道解析（D204）。"""
    account = svc.open_account(USER, "文-中国银行")
    renamed = svc.rename_account(USER, account.id, "文-中国银河")
    assert renamed.name == "文-中国银河"
    assert [item.name for item in svc.accounts.list_for(_uid(svc))] == ["文-中国银河"]


def test_renaming_another_users_account_is_refused(svc, db) -> None:
    other = SimAccountRepo(db).open("someone-else", "别人的账户", report_currency="CNY")
    with pytest.raises(StorageError, match="不属于"):
        svc.rename_account(USER, other.id, "抢来的名字")
    assert SimAccountRepo(db).get("someone-else", other.id).name == "别人的账户"


def test_deposit_into_an_unknown_account_is_refused(svc) -> None:
    """1 不是 2：`--account` 这个参数本身没写错，错的是库里没有这笔账。"""
    with pytest.raises(StorageError, match="不存在"):
        svc.deposit(
            "no-such-account",
            "CNY",
            100.0,
            username=USER,
            reason="初始",
            actor_id=USER,
        )


def test_buy_derives_the_currency_from_market_and_code(svc) -> None:
    """调用方不必也不应自己传币种：传了就要防着传错，不传则只有一条推导链。"""
    account_id = _funded(svc, "A")
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, fee=5.0)
    assert order.currency == "CNY"


def test_the_hk_counter_part_derives_hkd(svc) -> None:
    """同一份代码要按市场给出不同币种，否则「推定」其实只是写死了 CNY。"""
    account = svc.open_account(USER, "HK")
    svc.deposit(account.id, "HKD", 100_000.0, username=USER, reason="初始", actor_id=USER)
    order = svc.buy(account.id, USER, "HK", "700", qty=100, price=400.0)
    assert order.currency == "HKD"


def test_buy_can_settle_in_the_report_currency_at_the_given_rate(svc) -> None:
    """港股通：填了汇率，现金从 CNY 口袋按汇率出，订单与持仓仍是 HKD（D209）。"""
    account_id = _funded(svc, "港通")
    order = svc.buy(account_id, USER, "HK", "01021", qty=10, price=400.0, fx_rate=0.91)
    assert order.currency == "HKD"
    assert order.fx_rate_applied == pytest.approx(0.91)
    assert svc.cash(account_id) == {"CNY": pytest.approx(100_000.0 - 3_640.0)}


def test_a_placeholder_rate_is_repaved_from_the_days_fx_rate(svc, db) -> None:
    """存量占位汇率由后台按成交日汇率回算，并补一条说得清来处的现金流水（D293）。

    测试者说这一屏「最可信」的是每条流水都带理由，那就不能改历史那行：余额是重算出来的，
    把当初那条 buy 的数额改掉，等于抹掉「谁在什么时候因为什么动过多少钱」。
    """
    FxRepo(db).put(FILL_DAY, "HKD", "CNY", 0.92)
    account_id = _funded(svc, "文-中国银河")
    order = _placeholder_fill(svc, db, account_id)

    repaired = svc.repair_placeholder_rates(account_id, date(2024, 6, 4))

    assert repaired == 1
    after = svc.orders.order(account_id, order.id)
    assert after.fx_rate_ref == pytest.approx(0.92)
    assert after.fx_rate_applied == pytest.approx(0.92)
    rows = _adjust_rows(db, account_id)
    assert len(rows) == 1
    assert rows[0]["currency"] == "CNY"
    # 按 1.0 扣了 4,000，按 0.92 只该扣 3,680：退 320 回 CNY 口袋
    assert rows[0]["delta"] == pytest.approx(320.0)
    assert rows[0]["actor_id"] == "system"
    assert "1.0000" in rows[0]["reason"] and "0.9200" in rows[0]["reason"]
    assert svc.cash(account_id)["CNY"] == pytest.approx(100_000.0 - 4_000.0 + 320.0)
    # 幂等：这一笔已经不是占位值，重跑不能再补一条流水
    assert svc.repair_placeholder_rates(account_id, date(2024, 6, 4)) == 0
    assert len(_adjust_rows(db, account_id)) == 1


def test_a_placeholder_rate_without_fx_history_is_left_alone(svc, db) -> None:
    """那天前后都取不到汇率 ⇒ 一行不改、一条流水都不写。

    回算的依据是成交日的汇率，不是「拿最近那个数凑」：凑出来的结算额比留着占位值
    更难发现——屏幕上那句「汇率记成了 1.0000」至少还看得出来不对。
    """
    account_id = _funded(svc, "文-中国银河")
    order = _placeholder_fill(svc, db, account_id)

    assert svc.repair_placeholder_rates(account_id, date(2024, 6, 4)) == 0
    after = svc.orders.order(account_id, order.id)
    assert after.fx_rate_applied == pytest.approx(1.0)
    assert _adjust_rows(db, account_id) == []
    assert svc.cash(account_id)["CNY"] == pytest.approx(96_000.0)


def test_a_repair_that_would_overdraw_the_account_changes_nothing(svc, db) -> None:
    """补差会把口袋扣穿 ⇒ 汇率两列也不动：要么整笔修好，要么整笔不碰（D293）。

    只改汇率不补现金是最坏的结果：持仓卡上的成本换算与现金余额开始各说一套，
    而「查不出原因」的症状就是这么来的。
    """
    FxRepo(db).put(FILL_DAY, "HKD", "CNY", 2.0)
    account_id = svc.open_account(USER, "薄口袋").id
    svc.deposit(account_id, "CNY", 4_000.0, username=USER, reason="初始", actor_id=USER)
    order = _placeholder_fill(svc, db, account_id)

    assert svc.repair_placeholder_rates(account_id, date(2024, 6, 4)) == 0
    after = svc.orders.order(account_id, order.id)
    assert after.fx_rate_applied == pytest.approx(1.0)
    assert _adjust_rows(db, account_id) == []
    assert svc.cash(account_id)["CNY"] == pytest.approx(0.0)


def test_a_same_currency_fill_is_not_a_placeholder_rate(svc, db) -> None:
    """1.0 只有在「标的币种 ≠ 报告币种」时才是占位值：同币种那笔的 1.0 是真数。

    只看第二条会把手工补录、迁移带进来的同币种成交一起卷进来，改的是一个本来就对的值。
    """
    FxRepo(db).put(FILL_DAY, "HKD", "CNY", 0.92)
    account_id = _funded(svc, "A 户")
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, ts=FILL_TS)
    db.execute(
        "UPDATE sim_orders SET fx_rate_ref = 1.0, fx_rate_applied = 1.0 WHERE id = ?",
        [order.id],
    )

    assert svc.repair_placeholder_rates(account_id, date(2024, 6, 4)) == 0
    assert _adjust_rows(db, account_id) == []


def test_buy_refuses_when_cash_is_short_with_the_amount_in_the_message(svc) -> None:
    account = svc.open_account(USER, "B")
    svc.deposit(account.id, "CNY", 1_000.0, username=USER, reason="初始", actor_id=USER)
    with pytest.raises(StorageError, match="现金不足"):
        svc.buy(account.id, USER, "A", "601088", qty=100, price=40.0)


def test_an_unknown_market_is_refused_before_any_write(svc) -> None:
    """D84 的同一条：没配置的市场连币种都推不出来，不该出现半笔账。"""
    account_id = _funded(svc, "C")
    with pytest.raises(ValidationError, match="SG"):
        svc.buy(account_id, USER, "SG", "D05", qty=100, price=1.0)
    assert svc.accounts_of(USER)[0].id == account_id
    assert svc.cash(account_id) == {"CNY": 100_000.0}


def test_linking_a_fill_to_a_non_opened_thesis_is_refused(svc, db) -> None:
    """「还在观察的观点」被记了一笔成交，时间线就变成假的（§9.1，D81 的同一动机）。"""
    account_id = _funded(svc, "D")
    repo = ThesisRepo(db)
    thesis = repo.create(
        user_id=_uid(svc), market="A", code="601088", style="PR", actor_id=_uid(svc), now=NOW
    )
    with pytest.raises(ValidationError, match="observing"):
        svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=thesis.id)


def test_linking_a_fill_to_someone_elses_thesis_is_refused(svc, db) -> None:
    """thesis 与账本认的是同一个 id：两条链各认各的身份，挂接就形同虚设。"""
    account_id = _funded(svc, "E")
    users: UserRepo = svc.users
    other_user = users.create("shortseller", PWD)
    other = ThesisRepo(db).create(
        user_id=other_user.id,
        market="A",
        code="601088",
        style="PR",
        actor_id=other_user.id,
        now=NOW,
    )
    with pytest.raises(ValidationError, match="不属于"):
        svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=other.id)


def test_an_opened_thesis_accepts_the_link(svc, db) -> None:
    account_id = _funded(svc, "F")
    thesis_id = _opened_thesis(db, _uid(svc))
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, thesis_id=thesis_id)
    assert order.thesis_id == thesis_id


def test_a_sell_takes_the_currency_from_the_position_not_the_caller(svc) -> None:
    """卖出记回哪个币种，是持仓定的；让它传参就多一条能传错的路。"""
    account_id = _funded(svc, "G")
    svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0)
    order = svc.sell(account_id, USER, "A", "601088", qty=50, price=44.0)
    assert order.currency == "CNY"
    assert svc.position(account_id, "A", "601088").qty == 50


def test_withdraw_takes_the_absolute_value_so_a_negative_amount_cannot_top_up_cash(svc) -> None:
    """`withdraw(-100)` 若原样传下去会变成存钱：方向由方法名定，不由符号定。"""
    account_id = _funded(svc, "H")
    after = svc.withdraw(
        account_id, "CNY", 500.0, username=USER, reason="取出", actor_id=USER
    )
    assert after == 99_500.0
    assert svc.cash(account_id) == {"CNY": 99_500.0}


def test_undo_fill_resolves_the_username_and_reverses_the_fill(svc) -> None:
    account_id = _funded(svc, "纠错户")
    order = svc.buy(account_id, USER, "A", "601088", qty=100, price=40.0, fee=5.0)
    assert svc.undo_fill(USER, account_id, order.id)
    assert svc.cash(account_id) == {"CNY": 100_000.0}
    assert svc.orders.order_rows(account_id) == 0


def test_undoing_another_users_fill_is_refused(svc, db) -> None:
    other = SimAccountRepo(db).open("someone-else", "别人的账户", report_currency="CNY")
    SimAccountRepo(db).adjust_cash(
        other.id, "CNY", 10_000.0, reason="初始", actor_id="someone-else"
    )
    order = SimOrderRepo(db).record_fill(
        account_id=other.id, user_id="someone-else", market="A", code="601088",
        side="buy", qty=100, price=40.0, currency="CNY", fee=0.0,
    )
    with pytest.raises(StorageError, match="不属于"):
        svc.undo_fill(USER, other.id, order.id)
    assert SimOrderRepo(db).order(other.id, order.id) is not None


def test_convert_resolves_the_username_and_moves_both_balances(svc) -> None:
    account_id = _funded(svc, "港通户")
    trade = svc.convert(USER, account_id, "CNY", "HKD", 1_000.0, 1.08)
    assert trade.rate == pytest.approx(1.08)
    assert svc.cash(account_id) == {
        "CNY": pytest.approx(99_000.0), "HKD": pytest.approx(1_080.0),
    }


def test_converting_another_users_account_is_refused(svc, db) -> None:
    other = SimAccountRepo(db).open("someone-else", "别人的账户", report_currency="CNY")
    with pytest.raises(StorageError, match="不属于"):
        svc.convert(USER, other.id, "CNY", "HKD", 100.0, 1.08)
