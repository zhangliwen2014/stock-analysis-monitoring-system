"""持仓页与成交录入。账户、现金与成交都在内存库里，不碰任何行情源。"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import polars as pl


def _user_id(web, username="alice") -> str:
    return web.contexts.current().users.get_by_username(username).id


def _seed_close(web, code="600519", close=12.0, on=None):
    on = on or date.today()
    web.contexts.current().bars.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": [code],
                "date": [on],
                "open": [close],
                "high": [close],
                "low": [close],
                "close": [close],
                "volume": [1.0],
                "amount": [close],
                "source": ["akshare"],
            }
        )
    )

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
    # 成交录入即登记兴趣并排补数拍（D144）
    assert logged.context.backfills_store.open_for("A", "600519") is not None
    page = logged.client.get("/portfolio")
    assert "/symbol/A/600519" in page.text
    data["side"] = "sell"
    data["qty"] = "40"
    assert logged.client.post("/ledger/fill", data=data, follow_redirects=False).status_code == 303
    # 303 只说明「收了」；持仓真的少了 40 股才是「记对了」
    rows = logged.context.portfolio.positions("alice", account_id)
    assert [row.qty for row in rows] == [60.0]


def test_the_same_fill_posted_twice_queues_the_backfill_once(logged):
    """浏览器重交（或人连点两次保存）是同一笔成交的第二趟：登记兴趣挂钩再走一遍，
    主档那一步要么没建、要么原样不动，补数队列靠 `open_key` 去重只许留一条。"""
    account_id = _open_account(logged)
    data = {
        "csrf": logged.csrf, "account_id": account_id, "market": "A",
        "code": "600519", "side": "buy", "qty": "10", "price": "1500", "fee": "5",
    }
    assert logged.client.post("/ledger/fill", data=data, follow_redirects=False).status_code == 303
    master = logged.context.symbols.get("A", "600519")
    assert master is not None
    assert logged.client.post("/ledger/fill", data=data, follow_redirects=False).status_code == 303
    # 第二次 enqueue 被去重闸退回：开放队列里还是这一条
    assert logged.context.backfills_store.open_for("A", "600519") is not None
    assert logged.context.db.scalar(
        "SELECT count(*) FROM symbol_backfills WHERE market = 'A' AND code = '600519'"
    ) == 1
    # 主档没被第二趟动过：行还是那一行
    assert logged.context.symbols.get("A", "600519") == master


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
    """一个账户都没有时表单只能交空 id：那句拒绝指向页面上的开户表单，而不是一条
    在 `sams serve` 运行时跑不了的命令。"""
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": "", "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "1500"},
    )
    assert response.status_code == 400
    assert "先开一个账户" in response.text
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


# --- 开户与注资：网页自己走完，不再把用户推给一条在服务运行时跑不了的 CLI -------------------


def test_the_empty_page_opens_an_account_and_seeds_its_cash_from_the_form(logged):
    response = logged.client.post(
        "/portfolio/accounts",
        data={"csrf": logged.csrf, "name": "主账户", "cash": "100000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    ctx = logged.context
    (account,) = ctx.sim_accounts.list_for(_user_id(logged))
    assert account.name == "主账户"
    assert ctx.sim_accounts.cash_balances(account.id) == {"CNY": 100000.0}
    page = logged.client.get(response.headers["location"])
    assert "主账户" in page.text
    assert "100,000.00" in page.text
    assert "总资产" in page.text


def test_an_account_opened_without_cash_is_zero_not_a_rejection(logged):
    response = logged.client.post(
        "/portfolio/accounts",
        data={"csrf": logged.csrf, "name": "空户", "cash": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    (account,) = logged.context.sim_accounts.list_for(_user_id(logged))
    assert logged.context.sim_accounts.cash_balances(account.id) == {}


def test_the_page_renames_an_account_in_place(logged):
    """开户时把名字打成「中国银行」不用重开一个户：同一张卡上改名，持仓与现金不动（D204）。"""
    account_id = _open_account(logged, name="文-中国银行")
    response = logged.client.post(
        f"/portfolio/accounts/{account_id}/rename",
        data={"csrf": logged.csrf, "name": " 文-中国银河 "},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/portfolio?notice=renamed"
    page = logged.client.get(response.headers["location"])
    assert "账户名已修改" in page.text
    assert "文-中国银河" in page.text
    assert "文-中国银行" not in page.text
    # 改名不是新开：钱还是那一笔，id 也没换
    assert logged.context.sim_accounts.cash_balances(account_id) == {"CNY": 200_000.0}


def test_renaming_onto_a_taken_name_keeps_both_cards_as_they_were(logged):
    _open_account(logged, name="主账户")
    account_id = _open_account(logged, name="备用")
    response = logged.client.post(
        f"/portfolio/accounts/{account_id}/rename",
        data={"csrf": logged.csrf, "name": "主账户"},
    )
    assert response.status_code == 400
    assert "已存在" in response.text
    names = [item.name for item in logged.context.sim_accounts.list_for(_user_id(logged))]
    assert names == ["主账户", "备用"]


def test_renaming_to_a_blank_name_is_refused_and_the_old_name_stays(logged):
    account_id = _open_account(logged, name="主账户")
    response = logged.client.post(
        f"/portfolio/accounts/{account_id}/rename",
        data={"csrf": logged.csrf, "name": "   "},
    )
    assert response.status_code == 400
    assert "不能为空" in response.text
    assert logged.context.sim_accounts.get(_user_id(logged), account_id).name == "主账户"


def test_renaming_another_users_account_is_an_error(logged):
    logged.context.users.create("bob", "another-correct-horse")
    bob_account = _open_account(logged, username="bob", name="bobs户")
    response = logged.client.post(
        f"/portfolio/accounts/{bob_account}/rename",
        data={"csrf": logged.csrf, "name": "抢来的名字"},
    )
    assert response.status_code == 400
    assert "不属于" in response.text
    assert logged.context.sim_accounts.get(
        _user_id(logged, "bob"), bob_account
    ).name == "bobs户"


def test_cash_moves_in_and_out_from_the_page(logged):
    account_id = _open_account(logged)
    base = {"csrf": logged.csrf, "account_id": account_id, "currency": "CNY"}
    moved = logged.client.post(
        "/ledger/cash",
        data={**base, "side": "deposit", "amount": "5000", "reason": "追加"},
        follow_redirects=False,
    )
    assert moved.status_code == 303
    assert logged.context.sim_accounts.cash_balances(account_id) == {"CNY": 205000.0}
    taken = logged.client.post(
        "/ledger/cash",
        data={**base, "side": "withdraw", "amount": "105000", "reason": "取出"},
        follow_redirects=False,
    )
    assert taken.status_code == 303
    assert logged.context.sim_accounts.cash_balances(account_id) == {"CNY": 100000.0}


def test_a_withdrawal_beyond_the_balance_is_refused_and_keeps_the_typed_amount(logged):
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/cash",
        data={
            "csrf": logged.csrf, "account_id": account_id, "currency": "CNY",
            "side": "withdraw", "amount": "300000", "reason": "取出",
        },
    )
    assert response.status_code == 400
    assert "现金不足" in response.text
    assert 'value="300000"' in response.text
    assert logged.context.sim_accounts.cash_balances(account_id) == {"CNY": 200_000.0}


def test_a_rejected_cash_move_leaves_the_trade_form_alone(logged):
    """一张表单被拒只回填它自己：现金那条错误不该把成交那边改成「取出」（D197）。"""
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/cash",
        data={
            "csrf": logged.csrf, "account_id": account_id, "currency": "CNY",
            "side": "withdraw", "amount": "300000", "reason": "取出",
        },
    )
    assert response.status_code == 400
    assert '<option value="withdraw" selected' in response.text
    assert '<option value="buy" selected' in response.text


# --- 账户卡：现金与市值分开说，否则「合计」只是一个无法核对的大数 ---------------------------


def test_the_card_separates_cash_from_position_value(logged):
    account_id = _open_account(logged)
    _seed_close(logged)
    logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "100"},
        follow_redirects=False,
    )
    page = logged.client.get("/portfolio").text
    assert "现金" in page and "190,000.00" in page
    assert "持仓市值" in page and "1,200.00" in page
    assert "191,200.00" in page


def test_a_fill_can_be_backdated_to_its_trade_day(logged):
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "100",
              "on": "2026-10-20"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    moment = logged.context.db.scalar(
        "SELECT ts FROM sim_orders WHERE account_id = ?", [account_id]
    )
    local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.date(), local.hour) == (date(2026, 10, 20), 15)


def test_a_bad_trade_day_is_a_400_instead_of_silently_using_today(logged):
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "100",
              "on": "2026-13-01"},
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.text


def test_a_rejected_fill_keeps_every_typed_field(logged):
    account_id = _open_account(logged)
    response = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "sell", "qty": "100", "price": "-1",
              "fee": "3", "on": "2026-10-20"},
    )
    assert response.status_code == 400
    for value in ("600519", "100", "-1", "3", "2026-10-20"):
        assert f'value="{value}"' in response.text, value
    assert '<option value="sell" selected' in response.text


def test_an_account_with_no_positions_says_empty_not_blank(logged):
    _open_account(logged)
    assert "暂无持仓" in logged.client.get("/portfolio").text


def test_a_position_row_carries_the_day_of_its_own_price(logged):
    account_id = _open_account(logged)
    _seed_close(logged, on=date(2026, 10, 20))
    logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "100", "price": "100"},
        follow_redirects=False,
    )
    page = logged.client.get("/portfolio").text
    # 账户头那句「截至」也带这一天；这一条要钉的是持仓行自己那一格
    assert 'class="muted quote-day">2026-10-20' in page


def test_a_symbol_held_in_two_accounts_is_named_under_each_of_them(logged):
    first = _open_account(logged, name="甲户")
    second = _open_account(logged, name="乙户")
    for account_id in (first, second):
        logged.client.post(
            "/ledger/fill",
            data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
                  "code": "600519", "side": "buy", "qty": "10", "price": "100"},
            follow_redirects=False,
        )
    page = logged.client.get("/portfolio").text
    assert "同时在 乙户、甲户 里持有" in page
    assert page.count("同时在") == 2


def test_large_quantities_are_grouped_not_scientific(logged):
    account_id = _open_account(logged)
    logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "A",
              "code": "600519", "side": "buy", "qty": "1000000", "price": "0.1"},
        follow_redirects=False,
    )
    assert "1,000,000" in logged.client.get("/portfolio").text


def test_the_thesis_dropdown_names_market_code_and_status(logged):
    ctx = logged.context
    _open_account(logged)
    ctx.theses.add("alice", "A", "600519", note="长期看好")
    page = logged.client.get("/portfolio").text
    assert "A 600519 · observing" in page



# --- 成交记录与撤销（D205） ----------------------------------------------------------


def _seed_symbol(web, market="A", code="601088", name="中国神华", currency="CNY"):
    from sams.storage.repos.symbols import SymbolRepo

    SymbolRepo(web.contexts.current().db).upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "name": [name],
                "currency": [currency],
                "listed_on": [date(2007, 10, 9)],
            }
        )
    )


def _record(web, account_id, **over):
    fields = {
        "csrf": web.csrf, "account_id": account_id, "market": "A",
        "code": "601088", "side": "buy", "qty": "100", "price": "40", "fee": "5",
    }
    fields.update(over)
    return web.client.post("/ledger/fill", data=fields, follow_redirects=False)


def test_the_fills_table_shows_what_was_recorded_with_its_name(logged):
    _seed_symbol(logged)
    account_id = _open_account(logged)
    assert _record(logged, account_id).status_code == 303
    page = logged.client.get("/portfolio")
    assert "成交记录" in page.text
    assert "中国神华" in page.text
    assert f"/portfolio/accounts/{account_id}/fills/" in page.text
    assert "撤销" in page.text


def test_undoing_a_misrecorded_fill_removes_it_and_returns_the_cash(logged):
    """录错了市场/代码：撤销之后现金回来、持仓消失，再按正确的重记一笔。"""
    account_id = _open_account(logged)
    assert _record(logged, account_id).status_code == 303
    order = logged.context.sim_ledger.orders(account_id)[0]
    response = logged.client.post(
        f"/portfolio/accounts/{account_id}/fills/{order.id}/undo",
        data={"csrf": logged.csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/portfolio?notice=undone"
    page = logged.client.get(response.headers["location"])
    assert "成交已撤销" in page.text
    assert "/symbol/A/601088" not in page.text
    assert logged.context.sim_accounts.cash_balances(account_id) == {"CNY": 200_000.0}
    assert logged.context.sim_ledger.order_rows(account_id) == 0


def test_a_refused_undo_keeps_the_fill_and_says_why(logged):
    account_id = _open_account(logged)
    first = _record(logged, account_id, qty="100", price="40").status_code
    assert first == 303
    bought = logged.context.sim_ledger.orders(account_id)[0]
    assert _record(logged, account_id, side="sell", qty="100", price="45").status_code == 303
    response = logged.client.post(
        f"/portfolio/accounts/{account_id}/fills/{bought.id}/undo",
        data={"csrf": logged.csrf},
    )
    assert response.status_code == 400
    assert "先撤销后面那笔" in response.text
    assert logged.context.sim_ledger.order_rows(account_id) == 2


def test_undoing_into_another_users_account_is_an_error(logged):
    logged.context.users.create("bob", "another-correct-horse")
    bob_account = _open_account(logged, username="bob", name="bobs户")
    bob_order = logged.context.sim_ledger.record_fill(
        account_id=bob_account, user_id=_user_id(logged, "bob"), market="A",
        code="601088", side="buy", qty=100, price=40.0, currency="CNY", fee=0.0,
    )
    response = logged.client.post(
        f"/portfolio/accounts/{bob_account}/fills/{bob_order.id}/undo",
        data={"csrf": logged.csrf},
    )
    assert response.status_code == 400
    assert "不属于" in response.text
    assert logged.context.sim_ledger.order(bob_account, bob_order.id) is not None


def test_filling_a_hk_code_under_the_a_market_is_refused_with_the_hint(logged):
    """用户原话：选了 A、代码是港股通的票，结果记成一笔 A 股人民币成交。
    现在当场拒，并点名它属于哪个市场（D206）。"""
    _seed_symbol(logged, market="HK", code="00700", name="腾讯控股", currency="HKD")
    account_id = _open_account(logged)
    response = _record(
        logged, account_id, market="A", code="00700", qty="100", price="400",
    )
    assert response.status_code == 400
    assert "HK" in response.text and "00700" in response.text
    assert logged.context.sim_ledger.order_rows(account_id) == 0


def test_the_fill_form_has_a_live_candidate_panel(logged):
    _open_account(logged)
    page = logged.client.get("/portfolio")
    assert 'hx-get="/api/symbol-search"' in page.text
    assert 'id="fill-candidates"' in page.text
    assert 'hx-vals=\'{"scope": "fills"}\'' in page.text


# --- 换汇：港股通买入的资金来源（D207） ---------------------------------------------


def _convert(web, account_id, **over):
    data = {
        "csrf": web.csrf, "account_id": account_id,
        "from_ccy": "CNY", "to_ccy": "HKD", "amount": "8000", "rate": "1.08",
    }
    data.update(over)
    return web.client.post("/ledger/fx", data=data, follow_redirects=False)


def test_the_fx_form_prefills_the_latest_stored_rate(logged):
    logged.context.fx_store.put(
        date.today(), "HKD", "CNY", 0.92, source="akshare"
    )
    _open_account(logged)
    page = logged.client.get("/portfolio")
    assert "换汇" in page.text
    # 库里只有 HKD→CNY：预填反过来那一侧，并标明是取倒数
    assert "1.08696" in page.text
    assert "取倒数" in page.text


def test_converting_then_buying_hk_from_cny_works(logged):
    """用户原话那条路：人民币账户买港股通，先换汇再买，不再被「没有 HKD」挡死。"""
    account_id = _open_account(logged)
    response = _convert(logged, account_id)
    assert response.status_code == 303
    assert response.headers["location"] == "/portfolio?notice=fx"
    assert logged.context.sim_accounts.cash_balances(account_id) == {
        "CNY": 192_000.0, "HKD": 8_640.0,
    }
    page = logged.client.get(response.headers["location"])
    assert "换汇已记账" in page.text
    buy = logged.client.post(
        "/ledger/fill",
        data={"csrf": logged.csrf, "account_id": account_id, "market": "HK",
              "code": "00700", "side": "buy", "qty": "100", "price": "80"},
        follow_redirects=False,
    )
    assert buy.status_code == 303
    pos = logged.context.sim_ledger.position(account_id, "HK", "00700")
    assert pos.qty == 100.0


def test_a_bad_rate_is_refused_and_keeps_the_form(logged):
    account_id = _open_account(logged)
    response = _convert(logged, account_id, rate="0")
    assert response.status_code == 400
    assert "汇率" in response.text and "正有限小数" in response.text
    assert logged.context.sim_accounts.cash_balances(account_id) == {"CNY": 200_000.0}


def test_converting_into_another_users_account_is_an_error(logged):
    logged.context.users.create("bob", "another-correct-horse")
    bob_account = _open_account(logged, username="bob", name="bobs户")
    response = _convert(logged, bob_account)
    assert response.status_code == 400
    assert "不属于" in response.text
    assert logged.context.sim_accounts.cash_balances(bob_account) == {"CNY": 200_000.0}


def test_buying_hk_without_hkd_points_at_the_exchange_form(logged):
    account_id = _open_account(logged)
    response = _record(
        logged, account_id, market="HK", code="00700", qty="100", price="400",
    )
    assert response.status_code == 400
    assert "换汇" in response.text
