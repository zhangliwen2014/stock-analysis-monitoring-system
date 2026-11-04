"""持仓页的限额卡（S4-E §7）：红/灰/无法判定与「百分比进、小数存」。

屏幕上最容易说错的两处：把默认参考线画成红线（用户以为系统替他设了 20%），
以及数据不全时给 0%（缺行业的持仓没进分母，屏幕却写着「正常」）。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

SH = ZoneInfo("Asia/Shanghai")


def _account(logged, name="主账户") -> str:
    ctx = logged.context
    account = ctx.ledger.open_account("alice", name)
    ctx.ledger.deposit(
        account.id,
        "CNY",
        100_000.0,
        username="alice",
        reason="起始资金",
        actor_id=ctx.users.get_by_username("alice").id,
    )
    return account.id


def _symbol(logged, code="601088", name="中国神华", sector="煤炭") -> None:
    logged.context.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": [code],
                "name": [name],
                "currency": ["CNY"],
                "sector": [sector],
            }
        )
    )


def _bars(logged, code="601088", close=10.0) -> None:
    logged.context.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": [code],
                "date": [date(2024, 10, 25)],
                "close": [close],
                "source": ["test"],
            }
        )
    )


def _buy(logged, account_id, code="601088", qty=4_000, price=10.0) -> None:
    logged.context.ledger.buy(
        account_id,
        "alice",
        "A",
        code,
        qty=qty,
        price=price,
        ts=datetime(2024, 6, 3, 15, 0, tzinfo=SH),
    )


def _positioned(logged, *, name="主账户"):
    account_id = _account(logged, name)
    _symbol(logged)
    _bars(logged)
    _buy(logged, account_id)
    return account_id


def _post(logged, path, data):
    return logged.client.post(
        path, data={"csrf": logged.csrf, **data}, follow_redirects=False
    )


def test_the_page_shows_reference_lines_when_nothing_is_configured(logged):
    _positioned(logged)

    page = logged.client.get("/portfolio")

    assert page.status_code == 200
    assert "限额" in page.text
    assert "单票 601088 中国神华" in page.text
    # 40% 的单票高于默认 20%，但没有自定义线时它只是灰色参考
    assert "参考" in page.text
    assert "超限" not in page.text
    assert "不算违规" in page.text


def test_saving_a_percent_stores_a_ratio_and_turns_the_line_red(logged):
    account_id = _positioned(logged)

    response = _post(
        logged,
        "/portfolio/limits",
        {"account_id": account_id, "kind": "single", "scope": "601088", "threshold": "30"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/portfolio?notice=limit"
    user = logged.context.users.get_by_username("alice")
    saved = logged.context.limits_store.list_for(user.id)
    assert len(saved) == 1
    assert saved[0].threshold == 0.30
    assert saved[0].account_id == account_id
    page = logged.client.get("/portfolio")
    assert "超限" in page.text
    assert "40.00%" in page.text


def test_a_line_scoped_to_all_accounts_uses_an_empty_account(logged):
    account_id = _positioned(logged)

    _post(
        logged,
        "/portfolio/limits",
        {"account_id": "", "kind": "industry", "scope": "煤炭", "threshold": "30"},
    )

    user = logged.context.users.get_by_username("alice")
    saved = logged.context.limits_store.list_for(user.id)
    assert saved[0].account_id is None
    assert saved[0].scope == "煤炭"
    # 行业 40% 超 30%：红线出现在这个账户的卡上
    assert "行业「煤炭」" in logged.client.get("/portfolio").text
    assert account_id


def test_a_bad_percent_is_refused_with_the_number_it_saw(logged):
    account_id = _positioned(logged)

    response = _post(
        logged,
        "/portfolio/limits",
        {"account_id": account_id, "kind": "single", "scope": "601088", "threshold": "150"},
    )

    assert response.status_code == 400
    assert "红线要在 0% 与 100% 之间" in response.text
    assert "150" in response.text


def test_a_drawdown_line_must_not_have_a_scope(logged):
    account_id = _positioned(logged)

    response = _post(
        logged,
        "/portfolio/limits",
        {"account_id": account_id, "kind": "drawdown", "scope": "601088", "threshold": "20"},
    )

    assert response.status_code == 400
    assert "回撤线不要填对象" in response.text


def test_deleting_a_line_brings_the_reference_back(logged):
    account_id = _positioned(logged)
    _post(
        logged,
        "/portfolio/limits",
        {"account_id": account_id, "kind": "single", "scope": "601088", "threshold": "30"},
    )
    user = logged.context.users.get_by_username("alice")
    limit_id = logged.context.limits_store.list_for(user.id)[0].id

    response = _post(logged, f"/portfolio/limits/{limit_id}/delete", {})

    assert response.status_code == 303
    assert logged.context.limits_store.list_for(user.id) == ()
    assert "超限" not in logged.client.get("/portfolio").text


def test_deleting_someone_elses_line_is_a_404(logged):
    logged.context.users.create("bob", "correct-horse-battery")
    saved = logged.context.limits.save("bob", kind="single", scope="AAPL", threshold=0.2)

    response = _post(logged, f"/portfolio/limits/{saved.id}/delete", {})

    assert response.status_code == 404
    bob = logged.context.users.get_by_username("bob")
    assert logged.context.limits_store.list_for(bob.id) == (saved,)


def test_a_missing_industry_shows_undecidable_not_zero(logged):
    account_id = _account(logged)
    _symbol(logged, sector="")
    _bars(logged)
    _buy(logged, account_id)
    _post(
        logged,
        "/portfolio/limits",
        {"account_id": account_id, "kind": "industry", "scope": "煤炭", "threshold": "30"},
    )

    page = logged.client.get("/portfolio")

    assert "无法判定" in page.text
    assert "缺行业" in page.text
    # 缺行业时那一格不能是 0.00%：它会被读成「这条线很安全」
    assert "行业「煤炭」" in page.text
