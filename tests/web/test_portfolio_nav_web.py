"""持仓页的组合净值卡（S4-D §6.3）：异步局部、折线序列与「算不出来就不画」。

这一屏最容易骗人的两处：缺口被当成 0 收益、回撤被说成「按 TWR」。所以断言盯的是
文案把口径说全，以及 JSON 里算不出来的那天真的是 `null` 而不是 0。
"""

from __future__ import annotations

from datetime import timedelta

from sams.storage.repos.nav import NavPoint


def _open_account(web, username="alice", name="测试户") -> str:
    ctx = web.contexts.current()
    user = ctx.users.get_by_username(username)
    account = ctx.ledger.open_account(username, name)
    ctx.ledger.deposit(
        account.id, "CNY", 200_000.0, username=username, reason="起始资金", actor_id=user.id
    )
    return account.id


def _point(day, nav, *, complete=True, note=None, flow=0.0) -> NavPoint:
    return NavPoint(
        day=day,
        currency="CNY",
        nav=nav if complete else None,
        cash=nav if complete else None,
        market_value=0.0 if complete else None,
        external_flow=flow,
        complete=complete,
        stale_prices=0,
        note=note,
    )


def _seed(web, account_id, points):
    ctx = web.contexts.current()
    ctx.nav_store.replace(account_id, points[0].day, points[-1].day, points)


def test_portfolio_page_asks_for_the_nav_card_per_account(logged):
    account_id = _open_account(logged)
    page = logged.client.get("/portfolio")

    assert page.status_code == 200
    assert f'hx-get="/api/partials/portfolio/nav?account={account_id}"' in page.text
    # 分红口径写在账户卡上：税前毛额，拆股不改股数（S4-D §6.2）
    assert "未扣红利税" in page.text


def test_the_nav_card_names_the_window_the_job_actually_uses(logged, monkeypatch) -> None:
    """那句话里的天数得跟着 `NAV_WINDOW_DAYS` 走，不是抄在模板里的一个 400（D281）。

    抄着的数字会过期：job 改成覆盖 200 天，卡片还在说 400，而这句话的作用是让人判断
    「今晚那一趟能不能覆盖我要的那段历史」。
    """
    account_id = _open_account(logged)
    monkeypatch.setattr("sams.web.pages.portfolio.NAV_WINDOW_DAYS", 77)

    text = logged.client.get(f"/api/partials/portfolio/nav?account={account_id}").text

    assert "盘后 job 每晚重建最近 77 天" in text
    assert "重建最近 400 天" not in text


def test_the_nav_card_says_so_when_nothing_has_been_rebuilt_yet(logged):
    account_id = _open_account(logged)
    text = logged.client.get(f"/api/partials/portfolio/nav?account={account_id}").text

    assert "还没有算得出来的净值" in text
    assert "sams nav rebuild" not in text
    # 那个天数不是抄来的常数：卡片说的是 `NAV_WINDOW_DAYS` 那一晚覆盖的范围（D281）
    assert "盘后 job 每晚重建最近 400 天" in text
    # 没有一行可画时不挂图：一张空白画布看起来像「净值是一条直线」
    assert "data-chart-url" not in text


def test_the_nav_card_shows_the_reading_and_its_caliber(logged):
    account_id = _open_account(logged)
    today = logged.context.clock.today()
    _seed(
        logged,
        account_id,
        [
            _point(today - timedelta(days=2), 100_000.0),
            _point(today - timedelta(days=1), 100_500.0),
            _point(today, 101_000.0),
        ],
    )
    text = logged.client.get(f"/api/partials/portfolio/nav?account={account_id}").text

    assert "最新净值" in text
    assert "101,000.00 CNY" in text
    assert "区间 TWR" in text
    assert "最大回撤" in text
    assert "按净值峰谷，不是按 TWR" in text
    assert "外部流入（存入 − 取出）不计入收益" in text
    assert f'data-chart-url="/api/portfolio/{account_id}/nav"' in text


def test_a_gap_day_shows_up_as_a_gap_and_breaks_the_twr_window(logged):
    account_id = _open_account(logged)
    today = logged.context.clock.today()
    _seed(
        logged,
        account_id,
        [
            _point(today - timedelta(days=3), 100_000.0),
            _point(today - timedelta(days=2), None, complete=False, note="缺 600519 收盘价"),
            _point(today, 101_000.0),
        ],
    )
    text = logged.client.get(f"/api/partials/portfolio/nav?account={account_id}").text

    assert "缺口 1 天" in text
    # TWR 只能从缺口之后那一天起算：0 收益是它最容易说错的一句
    assert f"自 {today.isoformat()} 起" in text


def test_the_nav_series_keeps_gap_days_as_null(logged):
    account_id = _open_account(logged)
    today = logged.context.clock.today()
    _seed(
        logged,
        account_id,
        [
            _point(today - timedelta(days=2), 100_000.0),
            _point(today - timedelta(days=1), None, complete=False, note="缺汇率"),
            _point(today, 101_000.0),
        ],
    )
    body = logged.client.get(f"/api/portfolio/{account_id}/nav").json()

    assert body["days"] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert body["nav"] == [100_000.0, None, 101_000.0]
    assert body["gap_days"] == 1
    assert body["currency"] == "CNY"
    assert body["note"] is None


def test_an_empty_series_comes_back_with_a_note_not_a_flat_line(logged):
    account_id = _open_account(logged)
    body = logged.client.get(f"/api/portfolio/{account_id}/nav").json()

    assert body["days"] == [] and body["nav"] == []
    assert "没有算得出来的净值" in body["note"]


def test_both_nav_endpoints_404_for_an_account_that_is_not_yours(logged):
    assert logged.client.get("/api/portfolio/nope/nav").status_code == 404
    assert logged.client.get("/api/partials/portfolio/nav?account=nope").status_code == 404


def test_an_unreadable_partial_of_another_users_account_is_a_404(logged):
    """归属在服务层的 `get(user_id, account_id)` 里挡住：别人的账户连 404 都不该看出存在。"""
    ctx = logged.context
    other = ctx.users.create("bob", "correct-horse-battery")
    foreign = ctx.sim_accounts.open(other.id, "B 的账户")
    assert logged.client.get(f"/api/portfolio/{foreign.id}/nav").status_code == 404
