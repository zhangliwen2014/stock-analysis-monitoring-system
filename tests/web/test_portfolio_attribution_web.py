"""持仓页的业绩归因卡（S5-D §5）：异步局部、四种空态各说各的话。

这一屏最容易骗人的是「没有基准」被印成「0.00%」：跑平与取不到在屏幕上必须长得不一样。
所以断言盯三件事——正常态给出每票的价格段与换汇段、未重建净值时给出口径而不是空表、
基准取不到时那一行是「—」。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

SH = ZoneInfo("Asia/Shanghai")


def _open(logged, *, deposit=100_000.0, backdate_days: int | None = None) -> str:
    """开一个户。给了 `backdate_days` 就把开户与首笔存入挪到过去：
    净值重放只认账户存在之后的日子，今天开的户倒不出 90 天。"""
    ctx = logged.context
    user = ctx.users.get_by_username("alice")
    account = ctx.ledger.open_account("alice", "测试户")
    if backdate_days is None:
        ctx.ledger.deposit(
            account.id, "CNY", deposit, username="alice", reason="起始资金", actor_id=user.id
        )
        return account.id
    opened_on = ctx.clock.today() - timedelta(days=backdate_days)
    ctx.db.execute(
        "UPDATE sim_accounts SET created_at = ? WHERE id = ?",
        [datetime(opened_on.year, opened_on.month, opened_on.day, 1, 0, tzinfo=UTC), account.id],
    )
    ctx.sim_accounts.adjust_cash(
        account.id,
        "CNY",
        deposit,
        reason="起始资金",
        actor_id=user.id,
        ts=_at(opened_on),
        kind="deposit",
    )
    return account.id


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=SH)


def _seed_a(world, account_id: str, closes: dict[date, float], *, buy_day: date) -> None:
    """主档 + 一笔买入 + 三天收盘：归因只读已经落库的东西，这一步就是全部前提。"""
    ctx = world.context
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "name": ["中国神华"],
                "currency": ["CNY"],
                "sector": ["煤炭"],
            }
        )
    )
    ctx.ledger.buy(
        account_id, "alice", "A", "601088", qty=1_000, price=10.0, ts=_at(buy_day)
    )
    ctx.bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"] * len(closes),
                "code": ["601088"] * len(closes),
                "date": list(closes),
                "close": list(closes.values()),
                "source": ["test"] * len(closes),
            }
        )
    )


def _partial(logged, account_id: str, days: int = 90) -> str:
    response = logged.client.get(
        f"/api/partials/portfolio/attribution?account={account_id}&days={days}"
    )
    assert response.status_code == 200, response.text
    return response.text


def test_portfolio_page_asks_for_the_attribution_card(logged) -> None:
    account_id = _open(logged)
    page = logged.client.get("/portfolio")

    assert page.status_code == 200
    assert f'hx-get="/api/partials/portfolio/attribution?account={account_id}"' in page.text


def test_the_card_gives_a_way_out_before_the_nav_is_rebuilt(logged) -> None:
    """净值没重建 = 归因没原料：说一句「先跑 nav rebuild」，不给一张空表。"""
    account_id = _open(logged)
    text = _partial(logged, account_id)

    assert "还没有连续两个能算出来的净值日" in text
    assert f"sams nav rebuild --account {account_id}" in text
    # 同一页的净值卡说的是「盘后 job 每晚重建」，归因卡不能只说「先跑命令」（D281）
    assert "盘后" in text
    assert "<table" not in text


def test_the_card_splits_each_holding_into_price_fx_and_dividend(logged) -> None:
    account_id = _open(logged, backdate_days=40)
    today = logged.context.clock.today()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    _seed_a(
        logged, account_id, {day: 10.0 + index for index, day in enumerate(days)}, buy_day=days[0]
    )
    logged.context.nav.rebuild(account_id, days[0], today)

    text = _partial(logged, account_id)

    assert "中国神华" in text
    assert "价格段" in text and "换汇段" in text and "分红" in text
    # 两段相加：10→11 是 1%（1000 股 / 10 万净值），11→12 摊在涨过的 10.1 万上是 0.99%
    assert "1.99%" in text
    assert "残差" in text


def test_the_benchmark_line_stays_a_dash_when_the_index_is_not_stored(logged) -> None:
    """基准没落库 → 那一行是「—」，不是 0.00%：0 会被读成「跑平大盘」。"""
    account_id = _open(logged, backdate_days=40)
    today = logged.context.clock.today()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    _seed_a(logged, account_id, {day: 11.0 for day in days}, buy_day=days[0])
    logged.context.nav.rebuild(account_id, days[0], today)

    text = _partial(logged, account_id)

    assert "上证指数" in text
    assert "超额 —" in text
    assert "没有收盘数据" in text


def test_the_window_switcher_moves_the_start_day(logged) -> None:
    """30 天与全部用的是同一个服务：请求的窗口写在卡上，切换不是重新拼一句话。"""
    account_id = _open(logged, backdate_days=40)
    today = logged.context.clock.today()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    _seed_a(logged, account_id, {day: 11.0 for day in days}, buy_day=days[0])
    logged.context.nav.rebuild(account_id, days[0], today)

    thirty = _partial(logged, account_id, days=30)
    whole = _partial(logged, account_id, days=0)

    assert f"{(today - timedelta(days=30)).isoformat()} ~ {today.isoformat()}" in thirty
    assert "全部" in whole
    assert f"{(today - timedelta(days=40)).isoformat()} ~ {today.isoformat()}" in whole


def test_the_attribution_partial_404s_for_an_account_that_is_not_yours(logged) -> None:
    assert logged.client.get("/api/partials/portfolio/attribution?account=nope").status_code == 404
