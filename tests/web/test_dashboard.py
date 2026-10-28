"""看板与自选页。行情板与告警都从进程里读，不需要任何网络。"""

from __future__ import annotations

import re
from datetime import timedelta

import polars as pl
from fastapi.testclient import TestClient

from sams.core.quotes import Quote


def _login(client, username, password) -> str:
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client.cookies.get("sams_csrf").rpartition(".")[0]


def _seed_quote(web, market="A", code="600519", price=1500.0):
    now = web.core.clock.now_utc()
    web.core.board.update(
        market,
        [
            Quote(
                market=market,
                code=code,
                ts=now - timedelta(seconds=5),
                price=price,
                source="tencent",
            )
        ],
        now=now,
        error=None,
    )


def _seed_symbol(ctx, market="A", code="600519"):
    ctx.symbols.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "name": ["贵州茅台"],
                "currency": ["CNY"],
            }
        )
    )


def _add_watch(logged, market: str, code: str, note: str | None = None) -> None:
    """加一只票进自选：走真实端点，不直接写仓库——补数登记那条 Side effect 也在路上。"""
    data = {"csrf": logged.csrf, "market": market, "code": code}
    if note is not None:
        data["note"] = note
    logged.client.post("/watchlist/add", data=data)


def test_watchlist_page_carries_the_remove_button_on_the_row_itself(logged):
    """移除在那一行最右侧，不再另开一张表（D166）：
    同一只票在同一屏上出现两遍的日子到此为止。"""
    _add_watch(logged, "A", "600519")
    page = logged.client.get("/watchlist")
    assert page.text.count("/watchlist/remove") == 1
    assert "600519" in page.text


def test_watchlist_remove_survives_the_ten_second_refresh(logged):
    """局部刷新会把 `#quote-board` 整块换掉：移除表单必须活过这一次替换，
    所以 `manage` 是那个 GET 的参数，不是只写在页面里的一段静态表格。"""
    _add_watch(logged, "A", "600519")
    partial = logged.client.get("/api/partials/quotes?manage=1")
    assert "/watchlist/remove" in partial.text


def test_overview_board_has_no_remove_buttons(logged):
    """看板那一屏不带 `manage`：看板上摆一排删除按钮是要人误点的（D166）。"""
    _add_watch(logged, "A", "600519")
    assert "/watchlist/remove" not in logged.client.get("/api/partials/quotes").text
    assert "/watchlist/remove" not in logged.client.get("/").text


def test_watchlist_page_shows_the_note_inline(logged):
    """备注并进名称那一列：删掉那张管理表不能顺手把「为什么加这只票」删了。"""
    _add_watch(logged, "A", "600519", note="拿长线")
    assert "拿长线" in logged.client.get("/watchlist").text


def test_overview_requires_login(web_app):
    response = web_app.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_overview_lists_markets_and_the_first_run_checklist(logged):
    page = logged.client.get("/")
    assert page.status_code == 200
    for market in ("A", "HK", "US"):
        assert market in page.text
    assert "第一次运行" in page.text
    assert "今日告警" in page.text


def test_overview_shows_watchlist_quotes(logged):
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    page = logged.client.get("/")
    assert "1,500.00" in page.text
    assert "600519" in page.text


def test_the_account_column_says_assets_not_market_value(logged):
    """看板那格是「现金 + 持仓市值」，不是持仓市值；旁边那格只是浮动盈亏。"""
    ctx = logged.context
    user = ctx.users.get_by_username("alice")
    account = ctx.ledger.open_account("alice", "测试户")
    ctx.ledger.deposit(
        account.id, "CNY", 100_000.0,
        username="alice", reason="起始资金", actor_id=user.id,
    )
    table = logged.client.get("/").text
    assert "总资产" in table and "浮动盈亏" in table
    assert "总市值" not in table and "<th class=\"num\">总盈亏" not in table


def test_quotes_partial_is_an_htmx_fragment(logged):
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    page = logged.client.get("/api/partials/quotes")
    assert page.status_code == 200
    assert "600519" in page.text
    assert "<html" not in page.text.lower()


def test_watchlist_add_remove_roundtrip(logged):
    client = logged.client
    response = client.post(
        "/watchlist/add",
        data={"csrf": logged.csrf, "market": "A", "code": "600519", "note": "白酒"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # 加票即登记兴趣并排补数拍（D144）：库里有它的开放队列行
    assert logged.context.backfills_store.open_for("A", "600519") is not None
    # 加票那一脚的 ensure 已把主档行建了（D144/D146）：链接直接挂，不再需要补种子
    # 断言行链接而不是裸代码：表单的 placeholder 里也写着 600519，
    # 断「页面里没有这串数字」永远成立不了
    assert "/symbol/A/600519" in client.get("/watchlist").text
    assert "白酒" in client.get("/watchlist").text
    client.post(
        "/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    assert "/symbol/A/600519" not in client.get("/watchlist").text


def test_watchlist_rejects_an_unconfigured_market(logged):
    response = logged.client.post(
        "/watchlist/add", data={"csrf": logged.csrf, "market": "XX", "code": "1"}
    )
    assert response.status_code == 200
    assert "XX" in response.text


def test_watchlist_is_private_per_user(web_app):
    web_app.context.users.create("bob", "another-correct-horse")
    _login(web_app.client, "alice", "correct-horse-battery")
    bob = TestClient(web_app.app)
    bob_csrf = _login(bob, "bob", "another-correct-horse")
    bob.post("/watchlist/add", data={"csrf": bob_csrf, "market": "A", "code": "600519"})
    assert "/symbol/A/600519" not in web_app.client.get("/watchlist").text


def test_symbol_name_comes_from_the_master(logged):
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    _seed_symbol(logged.context)
    assert "贵州茅台" in logged.client.get("/watchlist").text


def _quote_row(page, code: str, table_marker: str | None = None) -> str:
    """页面某张表里某个代码的那一行（`<tr>…</tr>` 原样）。

    自选页里含代码的表不止一张（行情板与页底「已移除」那一栏），用 `table_marker`
    （如「/watchlist/remove」）挑出要的那张；单位/链接/名称三件事都要按
    「交易员看到的那一行」来钉。
    """
    rows = re.findall(r"<tr>(.*?)</tr>", page.text, re.S)
    if table_marker is not None:
        rows = [row for row in rows if table_marker in row]
    hits = [row for row in rows if f">{code}<" in row]
    assert hits, f"表里没有 {code} 这一行"
    return hits[0]


def _quote_cells(page, code: str) -> list[str]:
    """那一行各格的纯文本（市场 / 代码 / 名称 / 现价 / 涨跌 / 状态 / 时刻）。"""
    cells = re.findall(r"<td[^>]*>(.*?)</td>", _quote_row(page, code), re.S)
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]


def test_watchlist_row_without_master_symbol_links_anyway(logged):
    """链接统一（D146/N4）：主档没这行也挂 /symbol 链接——那一屏由代码表兜底建行，
    兜不住才 404，不再是「点下去必死」的死路。名称列仍交代码：空白看起来像页面坏了。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    _seed_quote(logged, market="US", code="AAPL", price=250.0)
    page = logged.client.get("/")
    assert page.status_code == 200
    assert '<a href="/symbol/US/AAPL">' in _quote_row(page, "AAPL")
    cells = _quote_cells(page, "AAPL")
    assert cells[2] == "AAPL", "名称列不许留空：空白看起来像页面坏了"


def test_watchlist_management_table_links_code(logged):
    """那一行最右侧同时是移除入口：代码链接和「移除」在同一行里（D166 之后不再有第二张表）。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    page = logged.client.get("/watchlist")
    row = _quote_row(page, "AAPL", table_marker="/watchlist/remove")
    assert '<a href="/symbol/US/AAPL">' in row


def test_quotes_partial_links_every_row(logged):
    """10 秒一刷的那块与首屏同一个折法：链接统一之后每一行都挂anchor（D146）。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    _seed_quote(logged, market="US", code="AAPL", price=250.0)
    page = logged.client.get("/api/partials/quotes")
    assert '<a href="/symbol/US/AAPL">' in page.text


def test_price_unit_follows_the_market_currency(logged):
    """现价那一格带的单位必须是该市场的币种：港股写「元」是把 400 港元说成 400 人民币。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "HK", "00700")
    logged.context.watchlists.add(user.id, "US", "AAPL")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged, market="HK", code="00700", price=400.0)
    _seed_quote(logged, market="US", code="AAPL", price=250.0)
    _seed_quote(logged, price=1500.0)
    page = logged.client.get("/")
    assert _quote_cells(page, "00700")[3] == "400.00 港元"
    assert _quote_cells(page, "AAPL")[3] == "250.00 美元"
    assert _quote_cells(page, "600519")[3] == "1,500.00 元"


def test_board_unit_follows_currency_override_for_dual_counter(logged):
    """行情板与搜索面板共用同一条单位规则：双柜台 80700 在板上也得报「元」，不随市场默认写港元。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "HK", "80700")
    _seed_quote(logged, market="HK", code="80700", price=300.0)
    assert _quote_cells(logged.client.get("/"), "80700")[3] == "300.00 元"


def test_removed_symbol_moves_to_the_hidden_section(logged):
    """折起之后它不该在行情板里，但要在页底那一栏里带着备注露出来（D169）。"""
    _add_watch(logged, "A", "600519", note="白酒龙头")
    logged.client.post(
        "/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    page = logged.client.get("/watchlist").text
    assert "已移除" in page and "白酒龙头" in page
    assert "/watchlist/restore" in page


def test_restore_puts_the_symbol_back_on_the_board(logged):
    _add_watch(logged, "A", "600519")
    logged.client.post(
        "/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    resp = logged.client.post(
        "/watchlist/restore",
        data={"csrf": logged.csrf, "market": "A", "code": "600519"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "600519" in logged.client.get("/watchlist").text


def test_purge_asks_first_and_then_really_goes(logged):
    """真删之前要有一屏确认：那一刀会连采集清单一起放手，值得多点一下（D169）。"""
    _add_watch(logged, "A", "600519")
    logged.client.post(
        "/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    confirm = logged.client.get("/watchlist/A/600519/purge")
    assert confirm.status_code == 200
    assert "彻底删除" in confirm.text
    assert "/watchlist/remove" not in confirm.text, "确认屏上不许再有第二个删除入口"
    logged.client.post(
        "/watchlist/purge", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    assert "600519" not in logged.client.get("/watchlist").text
    assert logged.client.get("/watchlist/A/600519/purge").status_code == 404


def test_hidden_symbol_leaves_the_quote_board(logged):
    """看板（首屏与 10 秒那一刷）都不该再看见折起的行：`manage` 只管要不要按钮。
    「采集清单继续包含它」那一半已由仓库层那条用例钉住，屏幕上这一半只钉反向：不在板上。"""
    _add_watch(logged, "A", "600519")
    logged.client.post(
        "/watchlist/remove", data={"csrf": logged.csrf, "market": "A", "code": "600519"}
    )
    assert "600519" not in logged.client.get("/api/partials/quotes?manage=1").text
    assert "600519" not in logged.client.get("/").text


def test_watchlist_search_input_includes_only_the_market_select(logged):
    """搜索框只带上市场下拉：整表单入 GET 查询串会把 csrf 和备注写进 URL 与日志。"""
    page = logged.client.get("/watchlist")
    assert 'hx-include="select[name=market]"' in page.text
    assert 'hx-include="#wl-form"' not in page.text
