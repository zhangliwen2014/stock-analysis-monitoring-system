"""持仓页与成交录入。账户、现金与成交都在内存库里，不碰任何行情源。"""

from __future__ import annotations


def _open_account(web, username="alice", name="测试户") -> str:
    """开户并补一笔现金：`record_fill` 不许买账户里没有那种币的钱。"""
    ctx = web.contexts.current()
    user = ctx.users.get_by_username(username)
    account = ctx.ledger.open_account(username, name)
    ctx.ledger.deposit(
        account.id, "CNY", 200_000.0,
        username=username, reason="起始资金", actor_id=user.id,
    )
    return account.id


def test_portfolio_lists_an_account_that_has_no_fills_yet(logged):
    """名下有账户、一笔都还没记：这一屏出账户名，持仓表是空的。"""
    _open_account(logged)
    page = logged.client.get("/portfolio")
    assert page.status_code == 200
    assert "测试户" in page.text
    assert "/symbol/A/600519" not in page.text


def test_fill_buy_then_sell_roundtrip(logged):
    account_id = _open_account(logged)
    data = {
        "csrf": logged.csrf, "account_id": account_id, "market": "A",
        "code": "600519", "side": "buy", "qty": "100", "price": "1500", "fee": "5",
    }
    response = logged.client.post("/ledger/fill", data=data, follow_redirects=False)
    assert response.status_code == 303
    page = logged.client.get("/portfolio")
    assert "/symbol/A/600519" in page.text
    data["side"] = "sell"
    data["qty"] = "40"
    assert logged.client.post("/ledger/fill", data=data, follow_redirects=False).status_code == 303
    # 303 只说明「收了」；持仓真的少了 40 股才是「记对了」
    rows = logged.context.portfolio.positions("alice", account_id)
    assert [row.qty for row in rows] == [60.0]


def test_fill_with_a_bad_number_rerenders_with_the_error(logged):
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "一百", "price": "1500"},
    )
    assert response.status_code == 400
    # 「数量」是页面上的静态标签，删掉这句拒绝也照样在——要断的是 `_number` 那句话本身
    assert "要是数字" in response.text
    # 那个链接只在成交真的落库时才出现：这一句才是「被拒的成交什么都没写」
    assert "/symbol/A/600519" not in response.text


def test_fill_without_that_currency_cash_says_so(logged):
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "US",
              "code": "AAPL", "side": "buy", "qty": "10", "price": "200"},
    )
    assert response.status_code == 400
    assert "USD" in response.text


def test_fill_with_a_negative_price_shows_the_repos_answer(logged):
    """负价格由仓库拒（`record_fill` 是唯一那道门）：页面把它那句话原样端出来，不另写一套。"""
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "-1500"},
    )
    assert response.status_code == 400
    assert "价格 必须大于 0" in response.text
    assert "/symbol/A/600519" not in response.text


def test_fill_without_an_account_says_how_to_open_one(logged):
    """一个账户都没有时表单只能交空 id：那句拒绝要说出两条真命令，而不是「账户  不存在」。"""
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": "", "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "1500"},
    )
    assert response.status_code == 400
    assert "ledger account add" in response.text
    assert "不存在或不属于" not in response.text


def test_fill_into_another_users_account_is_an_error(logged):
    logged.context.users.create("bob", "another-correct-horse")
    bob_account = _open_account(logged, username="bob", name="bobs户")
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": bob_account, "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "1500"},
    )
    assert response.status_code == 400
    assert "不属于" in response.text
