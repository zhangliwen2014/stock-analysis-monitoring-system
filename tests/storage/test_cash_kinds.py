"""现金流水的分类列（S4-D）：新写入填 kind，历史行按 reason 前缀回填。

`kind` 不是给人看的标签：净值重放要区分「内部变动」与「外部注入」，
`ref` 则承担「同一笔分红只入一次」的唯一约束。两者写错都不报错，只会让净值
看起来平滑，所以这里逐条钉住。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import duckdb
import pytest

from sams.core.errors import StorageError
from sams.services.ledger import LedgerService
from sams.storage.db import Database
from sams.storage.migrate import MIGRATIONS_DIR, apply_migrations
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo

USER = "trader"
PWD = "correct-horse-battery"


def _account(db: Database) -> str:
    return SimAccountRepo(db).open("u1", "主账户").id


def test_kind_and_ref_round_trip(db: Database) -> None:
    """`kind`/`ref` 跟着流水一起读回来：净值重放靠它们分辨钱从哪来。"""
    accounts = SimAccountRepo(db)
    account_id = _account(db)
    accounts.adjust_cash(
        account_id,
        "CNY",
        226.0,
        reason="分红 601088 2024-07-08",
        actor_id="system",
        kind="dividend",
        ref="A:601088:2024-07-08",
    )
    row = accounts.cash_history(account_id).to_dicts()[0]
    assert row["kind"] == "dividend"
    assert row["ref"] == "A:601088:2024-07-08"


def test_the_same_dividend_ref_cannot_be_posted_twice(db: Database) -> None:
    """唯一索引是最后一道闸：服务层忘了查，库也不许同一笔分红入两次。"""
    accounts = SimAccountRepo(db)
    account_id = _account(db)
    kwargs = dict(
        reason="分红",
        actor_id="system",
        kind="dividend",
        ref="A:601088:2024-07-08",
    )
    accounts.adjust_cash(account_id, "CNY", 226.0, **kwargs)
    with pytest.raises(StorageError):
        accounts.adjust_cash(account_id, "CNY", 226.0, **kwargs)


def test_fills_and_fx_set_their_kinds(db: Database, settings) -> None:
    """成交与换汇的流水不是 manual：买入/卖出/换汇各自有名字（净值要分开算）。"""
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    account_id = _account(db)
    accounts.adjust_cash(account_id, "CNY", 10_000.0, reason="初始", actor_id=USER)
    orders.record_fill(
        account_id=account_id,
        user_id=USER,
        market="A",
        code="601088",
        side="buy",
        qty=10,
        price=40.0,
        currency="CNY",
    )
    accounts.adjust_cash(account_id, "HKD", 1_000.0, reason="初始", actor_id=USER)
    accounts.convert(account_id, "HKD", "CNY", 100.0, 0.92, actor_id=USER)
    history = accounts.cash_history(account_id).to_dicts()
    assert [row["kind"] for row in history] == ["manual", "buy", "manual", "fx", "fx"]


def test_service_deposit_and_withdraw_set_their_kinds(db: Database, settings) -> None:
    accounts = SimAccountRepo(db)
    orders = SimOrderRepo(db, accounts, markets=settings.markets, symbols=SymbolRepo(db))
    users = UserRepo(db)
    users.create(USER, PWD)
    svc = LedgerService(accounts, orders, ThesisRepo(db), users, settings)
    account = svc.open_account(USER, "主账户")
    svc.deposit(
        account.id, "CNY", 1_000.0, username=USER, reason="入金", actor_id=USER
    )
    svc.withdraw(
        account.id, "CNY", 200.0, username=USER, reason="取出", actor_id=USER
    )
    assert [row["kind"] for row in accounts.cash_history(account.id).to_dicts()] == [
        "deposit",
        "withdraw",
    ]


def test_backfill_classifies_known_reasons(tmp_path) -> None:
    """0042 之前的历史行：认得出的前缀各归各位，判不出的留 manual（不猜）。"""
    before = sorted(MIGRATIONS_DIR.glob("*.sql"))
    through_0041 = [p for p in before if int(p.name[:4]) <= 41]
    for path in through_0041:
        shutil.copy(path, tmp_path / path.name)
    db = Database(duckdb.connect(":memory:"))
    try:
        apply_migrations(db, tmp_path)
        account_id = SimAccountRepo(db).open("u1", "旧账户").id
        rows = [
            ("换汇 CNY→HKD @1.1", "fx"),
            ("买入 601088", "buy"),
            ("卖出 601088", "sell"),
            ("手续费 601088", "fee"),
            ("存入 工资", "deposit"),
            ("取出 房租", "withdraw"),
            ("起始资金", "manual"),
        ]
        for reason, _kind in rows:
            db.execute(
                "INSERT INTO sim_cash_moves"
                " (id, account_id, currency, delta, reason, actor_id, ts)"
                " VALUES (?, ?, 'CNY', 1, ?, 'u1', ?)",
                [f"m-{reason}", account_id, reason, datetime(2024, 1, 2, tzinfo=UTC)],
            )
        write_0042(db, tmp_path, before)
        got = {
            row["reason"]: row["kind"]
            for row in db.query("SELECT reason, kind FROM sim_cash_moves").to_dicts()
        }
        assert got == {reason: kind for reason, kind in rows}
    finally:
        db.close()


def write_0042(db: Database, tmp_path, all_paths) -> None:
    """只补上 0042 再迁移：前面那 41 个文件已经在库里记过账。"""
    path = next(p for p in all_paths if int(p.name[:4]) == 42)
    shutil.copy(path, tmp_path / path.name)
    apply_migrations(db, tmp_path)
