"""「列表→详情」那条回去的路（spec 2026-11-03-back-link-on-every-detail-page）。

判据三条，每条测试钉其一：列表出链必须把来路带进 `back=`；详情知道来路才说「返回」；
不知道来路时说的是「这张表叫什么」，不是「返回」。`back` 的守门只有一处实现，
三个详情页共用，所以这里对每个详情页都过一遍同一组恶意值。
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import polars as pl
from test_symbol_fundamentals import _six_baijiu

from sams.core.notify import MID
from sams.core.screener_fields import Condition
from sams.datasource.news import empty_news

#: 模板审计的那棵树（发起候选的表单都住在里面）
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "sams" / "web" / "templates"


def assert_no_offsite_href(page: str, hostile: str) -> None:
    """那一串坏字符可能原样躺在「本页 URL」的 query 里（本页确实带着它，退回本页也合理）；
    不许的是它成为一个 href 的**去处**——所以只断言 href 不以它开头。"""
    for href in re.findall(r'href="([^"]*)"', page):
        value = html.unescape(href)
        assert not value.startswith(hostile), (hostile, value)


def _alice_id(logged) -> str:
    return logged.context.users.get_by_username("alice").id


def _open_account(logged) -> str:
    """开户并补一笔现金：`record_fill` 不许买账户里没有那种币的钱。"""
    ctx = logged.context
    user = ctx.users.get_by_username("alice")
    account = ctx.ledger.open_account("alice", "测试户")
    ctx.ledger.deposit(
        account.id, "CNY", 200_000.0, username="alice", reason="起始资金", actor_id=user.id
    )
    return account.id


def _symbol_master(ctx, *, market="A", code="600519", name="贵州茅台") -> None:
    """个股页对「库里没有这只票」答 404：要测这一屏，先给它一行主档。"""
    currency = {"A": "CNY", "HK": "HKD", "US": "USD"}[market]
    ctx.symbols.upsert(
        pl.DataFrame({"market": [market], "code": [code], "name": [name], "currency": [currency]})
    )


def _listed(ctx, *, market="A", code="600519", name="贵州茅台") -> None:
    """候选面板读的是代码表（`symbol_list`），不是主档：想让面板出一行，得进那张表。"""
    ctx.symbol_list_store.upsert_batch(
        market,
        [
            {
                "code": code,
                "name": name,
                "py_initials": "gzmt",
                "py_full": "guizhoumaotai",
                "currency": {"A": "CNY", "HK": "HKD", "US": "USD"}[market],
            }
        ],
    )


def _news_item(ctx, *, code="600519", market="A") -> None:
    _symbol_master(ctx, market=market, code=code)
    ctx.news_store.upsert_many(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "source": "eastmoney_f10",
                    "item_id": "N1",
                    "published_at": ctx.clock.now_utc() - timedelta(hours=1),
                    "title": "资讯 N1",
                    "summary": None,
                    "url": "https://example.test/N1",
                }
            ],
            schema=dict(empty_news().schema),
        )
    )


def _thesis(logged) -> str:
    return logged.context.theses.add("alice", "A", "600519", note="高端酒").thesis.id


def _digest(logged) -> str:
    digest, _ = logged.context.digests_store.save(
        user_id=_alice_id(logged),
        market="A",
        as_of=date(2026, 10, 30),
        title="A · 2026-10-30 收盘摘要",
        body="【持仓】\n· 无变化",
        has_changes=False,
        heartbeat=False,
        source_note="数据：盘后任务无失败。",
    )
    return digest.id


def _alert(logged, **overrides) -> str:
    kwargs = dict(
        user_id=_alice_id(logged),
        rule_id=None,
        rule_name="止损线",
        account_id=None,
        account_name="",
        market="A",
        code="600519",
        symbol_name="贵州茅台",
        severity=MID,
        stage="close",
        value_text="1500.00",
        threshold_text="1550.00",
        action_note="考虑减仓",
        triggered_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    alert = logged.context.alerts_store.create(**kwargs)
    return alert.id


def test_the_alert_list_hands_the_detail_a_way_back(logged):
    """列表那一行的 `详情` 链接要把「这一屏」整条带过去：条件与页码都在 URL 里，
    少一段，退回来就是另一张表——用户要的「回到刚才那一屏」正是这一串字符。"""
    _alert(logged)
    page = logged.client.get("/alerts", params={"status": "all", "page": "1"}).text
    carried = unquote(html.unescape(re.search(r"\?back=([^\"\s]+)", page).group(1)))
    assert carried == "/alerts?status=all&page=1"


def test_alert_detail_uses_the_back_it_was_given(logged):
    alert_id = _alert(logged)
    page = logged.client.get(
        f"/alerts/{alert_id}", params={"back": "/alerts?status=all&page=2"}
    ).text
    assert "← 返回" in page
    assert 'href="/alerts?status=all&amp;page=2"' in page


def test_alert_detail_without_a_back_names_the_table(logged):
    """贴 URL 进来时无从知道来路，「返回」是假话；但「去那张表」是真话，照样得给。"""
    alert_id = _alert(logged)
    page = logged.client.get(f"/alerts/{alert_id}").text
    assert "← 返回" not in page
    assert "告警列表" in page
    assert 'href="/alerts"' in page


def test_alert_detail_refuses_a_back_that_leaves_the_site(logged):
    """`back` 原样进 href，所以协议、`//host`、反斜杠与控制字符一律不收：
    少一条返回链接，好过把「返回」做成一扇跳去别人站点的门。"""
    alert_id = _alert(logged)
    for hostile in (
        "https://evil.example/x",
        "//evil.example",
        "/a\\b",
        "/ok\x00bad",
    ):
        page = logged.client.get(f"/alerts/{alert_id}", params={"back": hostile}).text
        assert_no_offsite_href(page, hostile)
        assert '<p><a href="/alerts">告警列表</a></p>' in page, hostile


def test_the_thesis_list_links_with_a_way_back(logged):
    """论点那一屏有两列都指向同一条详情：带一次 `back` 只算修了一半，
    另一半照样把人带回默认那张表。"""
    thesis_id = _thesis(logged)
    page = logged.client.get("/theses").text
    # 两列（标的与状态）都指同一条详情：带一次 back 只算修了一半
    hrefs = re.findall(rf'/theses/{thesis_id}\?back=([^"\s]+)', page)
    assert len(hrefs) == 2
    for href in hrefs:
        assert unquote(html.unescape(href)) == "/theses"


def test_thesis_detail_uses_the_back_it_was_given(logged):
    thesis_id = _thesis(logged)
    page = logged.client.get(f"/theses/{thesis_id}", params={"back": "/theses"}).text
    assert "← 返回" in page
    assert 'href="/theses"' in page


def test_thesis_detail_without_a_back_names_the_table(logged):
    thesis_id = _thesis(logged)
    page = logged.client.get(f"/theses/{thesis_id}").text
    assert "← 返回" not in page
    # 整句钉住：只写「论点列表」在某处出现，顶栏那条导航就能替它过关
    assert '<p><a href="/theses">论点列表</a></p>' in page


def test_thesis_detail_refuses_a_back_that_leaves_the_site(logged):
    thesis_id = _thesis(logged)
    page = logged.client.get(f"/theses/{thesis_id}", params={"back": "//evil.example"}).text
    assert_no_offsite_href(page, "//evil.example")
    assert '<p><a href="/theses">论点列表</a></p>' in page


def test_the_digest_list_links_with_a_way_back(logged):
    digest_id = _digest(logged)
    page = logged.client.get("/digest").text
    href = re.search(rf'/digest/{digest_id}\?back=([^"\s]+)', page).group(1)
    assert unquote(html.unescape(href)) == "/digest"


def test_digest_detail_uses_the_back_it_was_given(logged):
    digest_id = _digest(logged)
    page = logged.client.get(f"/digest/{digest_id}", params={"back": "/digest"}).text
    assert "← 返回" in page
    assert 'href="/digest"' in page


def test_digest_detail_without_a_back_names_the_table(logged):
    digest_id = _digest(logged)
    page = logged.client.get(f"/digest/{digest_id}").text
    assert "← 返回" not in page
    assert '<p><a href="/digest">每日摘要</a></p>' in page


def test_digest_detail_refuses_a_back_that_leaves_the_site(logged):
    digest_id = _digest(logged)
    page = logged.client.get(f"/digest/{digest_id}", params={"back": "https://evil.example/"}).text
    assert_no_offsite_href(page, "https://evil.example/")
    assert '<p><a href="/digest">每日摘要</a></p>' in page


def test_the_quote_table_links_back_to_the_page_it_s_on(logged):
    """同一张报价板嵌在两处（看板 `/` 与自选 `/watchlist`）：back 得是各自那一屏。
    写死一个，另一处的人按「返回」会被送到别人的列表。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    for path in ("/watchlist", "/"):
        page = logged.client.get(path).text
        # Jinja 的 urlencode 不动 `/`（它在 query 里本就合法），所以这里比对的是原样
        assert f"/symbol/A/600519?back={path}" in page, path


def test_the_ten_second_refresh_does_not_lose_the_page_it_s_on(logged):
    """看板/自选每 10 秒换进去的是 `/api/partials/quotes…` 那段裸片段。片段自己的 URL
    不是「这一屏」：拿它当来路，首屏那一行的返回还是对的，刷新一过「返回」就去了
    一段没有外壳的表——同一张板上两种链接指向两处，用户看不出哪个是真的。
    来路得由发起页在 `hx-get` 里报上来。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    for path, query in (("/watchlist", {"manage": "1"}), ("/", {})):
        fragment = logged.client.get(
            "/api/partials/quotes", params={**query, "back": path}
        ).text
        assert f"/symbol/A/600519?back={path}" in fragment, path
        assert "partials/quotes" not in fragment, "片段把自己的 URL 当成了来路"


def test_the_portfolio_page_links_back_to_itself(logged):
    """持仓页一只票有三个入口（代码、决策、成交记录）：漏一个，那个入口进去就没了退路。
    `#decision` 那一条尤其要紧——锚点必须在 query 之后，否则 `?back=` 会被吞进片段。"""
    account_id = _open_account(logged)
    assert logged.client.post(
        "/ledger/fill",
        data={
            "csrf": logged.csrf, "account_id": account_id, "market": "A",
            "code": "600519", "side": "buy", "qty": "10", "price": "1500", "fee": "5",
        },
        follow_redirects=False,
    ).status_code == 303
    page = logged.client.get("/portfolio").text
    assert 'href="/symbol/A/600519"' not in page, "还有一个裸链接没带来路"
    # 6 个带 back 的不是 bug：这一屏另有两格候选、两处别的出口，它们带的也是本页
    assert len(re.findall(r'\?back=/portfolio', page)) >= 3
    assert 'href="/symbol/A/600519?back=/portfolio#decision"' in page


def test_the_news_page_carries_its_filters_into_the_back(logged):
    """资讯那张时间线能筛市场：筛选条件得一起带进 `back`，
    否则「返回」把人送回一张没筛过的表，等于把刚才那一下筛选重做一遍。"""
    ctx = logged.context
    uid = _alice_id(logged)
    ctx.watchlists.add(uid, "A", "600519")
    _news_item(ctx)
    page = logged.client.get("/news", params={"market": "A"}).text
    href = re.search(r'/symbol/A/600519\?back=([^"\s]+)', page).group(1)
    assert unquote(html.unescape(href)) == "/news?market=A"


def test_the_peers_table_links_back_to_the_symbol_you_came_from(logged):
    """同业是挂在个股页下面的异步局部：它「这一屏」的 URL 是 `/api/partials/peers?…`，
    照搬进 `back` 会把人带回一段裸片段。这里的来路是发起它的那只票。"""
    _six_baijiu(logged.context)
    page = logged.client.get("/api/partials/peers", params={"market": "A", "code": "600519"}).text
    href = re.search(r'/symbol/A/600809\?back=([^"\s]+)', page).group(1)
    assert unquote(html.unescape(href)) == "/symbol/A/600519"


def test_the_review_screen_links_back_to_itself(logged):
    """复盘台同一条告警有三个出口（超期表的标的与处置那一串、已处置那一行）：
    三个都在同一屏，`back` 都是这一屏，漏一个就是从那里进去没路回来。"""
    overdue = _alert(logged, triggered_at=datetime.now(UTC) - timedelta(days=2))
    handled = _alert(logged, triggered_at=datetime.now(UTC) - timedelta(hours=2))
    assert logged.client.post(
        f"/alerts/{handled}/acted",
        data={"csrf": logged.csrf, "action_kind": "other", "note": "按提示减半仓"},
        follow_redirects=False,
    ).status_code == 303
    page = logged.client.get("/review").text
    # 超期表两个出口（标的、处置那一串）+ 已处置那一行一个
    assert page.count(f"/alerts/{overdue}?back=/review") == 2
    assert f"/alerts/{handled}?back=/review" in page


def test_the_symbol_page_hands_its_own_url_to_the_rows_under_it(logged):
    """个股页下面挂的告警表与论点表也是「列表」：从它们进详情要回得来，而那一屏的「返回」
    还得把这一只票自己的来路（`back=/watchlist`）一并带回去——三跳走完整条链。"""
    _symbol_master(logged.context)
    alert_id = _alert(logged)
    thesis_id = _thesis(logged)
    page = logged.client.get("/symbol/A/600519", params={"back": "/watchlist"}).text

    href = html.unescape(re.search(rf'href="(/alerts/{alert_id}[^"]+)"', page).group(1))
    assert f"/alerts/{alert_id}?back=" in href
    detail = logged.client.get(href).text
    assert "← 返回" in detail
    # 每一层只多剥一码：这一屏的返回地址是「那只票」，query 里还留着它自己的来路
    assert 'href="/symbol/A/600519?back=%2Fwatchlist"' in detail

    # 论点那一行同理：详情进去后指回这一只票
    thesis_href = html.unescape(re.search(rf'href="(/theses/{thesis_id}[^"]+)"', page).group(1))
    assert "← 返回" in logged.client.get(thesis_href).text

    last = logged.client.get("/symbol/A/600519?back=%2Fwatchlist").text
    assert 'href="/watchlist"' in last and "← 返回" in last


def test_a_candidate_panel_returns_to_the_form_that_asked_for_it(logged):
    """候选面板是异步片段：它「这一屏」是 /api/symbol-search/…，把那段塞进 `back`
    会把人带回一段裸片段。来路得由发起它的那张表单报上名，端点守门后再交给面板。"""
    _listed(logged.context)
    page = logged.client.get(
        "/api/symbol-search/pick", params={"code": "600519", "back": "/rules/new"}
    ).text
    assert "/symbol/A/600519?back=/rules/new" in page


def test_a_candidate_panel_drops_a_back_that_leaves_the_site(logged):
    _listed(logged.context)
    page = logged.client.get(
        "/api/symbol-search/pick", params={"code": "600519", "back": "//evil.example"}
    ).text
    assert_no_offsite_href(page, "//evil.example")
    assert "?back=" not in page


def test_the_form_asks_for_its_own_url_to_be_the_back(logged):
    """发起页要把自己的 URL（含条件）交给面板：`/watchlist` 上那格是真的带，其余每一格
    由下面那条模板审计兜住——逐个页面 GET 要先给每张表灌数据，代价不值。"""
    page = logged.client.get("/watchlist").text
    # Jinja 的 urlencode 不动 `/`（它在 query 里本就合法）
    assert 'hx-get="/api/symbol-search?back=/watchlist"' in page


def test_every_candidate_box_asks_for_its_own_page_as_the_back() -> None:
    """模板审计：每一格「列表/表单上的候选」都要把「本页 URL」写进它请求面板的那个 `hx-get`。
    漏一处不报错，只是那一页的候选点进个股页后没路回来。

    唯一的例外是 `base.html` 顶栏那格：它住在每一屏上，包括报错页和带着陌生参数的管理页，
    带上就等于把「当前请求的完整 URL」原样印进 HTML。顶栏搜索是「跳到任何一只票」，
    不是「从我刚看的那张表点出去」，本来就没有来路可回。"""
    with_back = []
    without_back = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for href in re.findall(r'hx-get="(/api/symbol-search[^"]*)"', path.read_text("utf-8")):
            (with_back if "?back=" in href else without_back).append(f"{path.name}: {href}")
    assert without_back == ["base.html: /api/symbol-search/nav"], without_back


def test_a_saved_preset_name_rebuilds_that_screen(logged_screen):
    """顶栏那颗「选股」永远指向裸 `/screen`（R3-24），回来是一张空表。
    存过的方案名于是得是入口：点它，条件与市场照原样铺开。
    系统没记「上一次的临时筛选」，所以这一条说的是「方案」，不是「上次跑的」——
    不说没有依据的话。"""
    preset = logged_screen.context.screener_presets.create(
        _alice_id(logged_screen), "便宜的好公司", "A", [Condition("pr_now", "<=", 0.5)]
    )
    page = logged_screen.client.get("/screen").text
    assert f'href="/screen?preset={preset.id}">便宜的好公司</a>' in page
