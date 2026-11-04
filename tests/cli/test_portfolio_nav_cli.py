"""`sams portfolio dividends` 与 `sams nav rebuild` 的行为契约（S4-D §6.2/§6.3）。

与 `tests/cli/test_ledger_cli.py` 同一跑法：不起子进程，直接 `build_parser()` 解析参数、
拿 `args.handler(container, args)` 的返回值当退出码。两个命令都对着同一个小世界跑：
一个开在 1 月初的账户、5 月买入的 100 股、6 月的一个除权日——分红与净值读的是同一份账。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from support_valuation import make_container

import sams.cli as cli
from sams.services.dividends import TAX_NOTE
from sams.storage.repos.corp_actions import CorpAction

USER = "trader"
PWD = "correct-horse-battery"
#: 账户开在这一切之前：分红只入开立之后的除权日，净值起点也从这里算
OPENED_AT = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)
EX_DATE = date(2024, 6, 3)
BUY_AT = datetime(2024, 5, 6, 15, 0, tzinfo=UTC)


@pytest.fixture
def world(db, settings):
    container = make_container(db, settings)
    user = container.users.create(USER, PWD)
    account = container.sim_accounts.open(user.id, "主账户")
    db.execute("UPDATE sim_accounts SET created_at = ? WHERE id = ?", [OPENED_AT, account.id])
    # 注资落在开立日：净值重放按流水的时间戳分日，注资落在终点那天会把整段持仓算成负现金
    container.sim_accounts.adjust_cash(
        account.id,
        "CNY",
        100_000.0,
        reason="初始",
        actor_id=USER,
        ts=OPENED_AT,
        kind="deposit",
    )
    container.ledger.buy(
        account.id, USER, "A", "601088", qty=100, price=30.0, ts=BUY_AT
    )
    container.corp_actions.upsert(
        CorpAction(
            market="A",
            code="601088",
            ex_date=EX_DATE,
            kind="dividend",
            src="test",
            cash_per_share=0.5,
        )
    )
    return SimpleNamespace(container=container, account_id=account.id, db=db)


def invoke(world, *extra: str) -> int:
    args = cli.build_parser().parse_args(list(extra))
    return args.handler(world.container, args)


def out(world, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()
    code = invoke(world, *extra)
    return code, capsys.readouterr().out


def payload(world, capsys, *extra: str) -> tuple[int, dict]:
    code, text = out(world, capsys, *extra, "--json")
    return code, json.loads(text)


def dividends(world, *extra: str) -> tuple[str, ...]:
    return ("portfolio", "dividends", "--account", world.account_id, *extra)


def test_dividends_posts_the_ex_date_holding_and_reruns_as_zero(world, capsys):
    code, text = out(world, capsys, *dividends(world, "--as-of", "2024-10-20"))

    assert code == 0
    assert "2024-06-03 A 601088 派息 100 股 × 0.5 = 50.00 CNY" in text
    assert TAX_NOTE in text

    # 重跑不是错误：同一除权日已经入过账，0 新增 + 一句「已入过账」
    code, text = out(world, capsys, *dividends(world, "--as-of", "2024-10-20"))
    assert code == 0
    assert "入账 0 笔" in text
    assert "已入过账 1 笔" in text


def test_dividends_json_carries_the_posting_and_the_tax_note(world, capsys):
    code, body = payload(world, capsys, *dividends(world, "--as-of", "2024-10-20"))

    assert code == 0
    assert body["rows"] == 1
    assert body["tax_note"] == TAX_NOTE
    assert body["postings"] == [
        {
            "market": "A",
            "code": "601088",
            "ex_date": "2024-06-03",
            "qty": 100.0,
            "cash_per_share": 0.5,
            "amount": 50.0,
            "currency": "CNY",
        }
    ]


def test_dividends_refuses_an_unknown_account_and_a_bad_date(world, capsys):
    code, text = out(
        world, capsys, "portfolio", "dividends", "--account", "nope", "--as-of", "2024-10-20"
    )
    assert code == 1
    assert "不存在" in text

    code, text = out(world, capsys, *dividends(world, "--as-of", "2024-7-1"))
    assert code == 2
    assert "YYYY-MM-DD" in text


def test_nav_rebuild_writes_the_window_and_reports_the_reading(world, capsys):
    code, body = payload(world, capsys, "nav", "rebuild", "--account", world.account_id,
                         "--days", "100", "--as-of", "2024-10-26")

    assert code == 0
    assert body["days_written"] > 0
    assert body["gap_days"] == 0
    assert body["latest"] == "2024-10-26"
    # 100000 − 100×30 + 50 = 97050，加持仓 100 股 × 最后一天收盘
    assert body["nav"] is not None
    assert body["currency"] == "CNY"


def test_nav_rebuild_prints_the_twr_and_drawdown_lines(world, capsys):
    code, text = out(world, capsys, "nav", "rebuild", "--account", world.account_id,
                     "--days", "100", "--as-of", "2024-10-26")

    assert code == 0
    assert "重建" in text
    assert "最新完整日" in text
    assert "TWR" in text
    assert "最大回撤" in text


def test_nav_rebuild_refuses_an_unknown_account_and_a_non_positive_window(world, capsys):
    code, text = out(world, capsys, "nav", "rebuild", "--account", "nope")
    assert code == 1
    assert "不存在" in text

    code, text = out(world, capsys, "nav", "rebuild", "--account", world.account_id, "--days", "0")
    assert code == 2
    assert "--days" in text
