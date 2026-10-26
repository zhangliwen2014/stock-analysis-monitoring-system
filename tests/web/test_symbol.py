"""个股页与 K 线 JSON。日历用 StubCalendar，行情与日线都在进程里。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from sams.core.quotes import Quote
from sams.web.display import moment_label


def _seed(logged, market="A", code="600519"):
    logged.context.symbols.upsert(
        pl.DataFrame(
            {"market": [market], "code": [code], "name": ["贵州茅台"], "currency": ["CNY"]}
        )
    )
    # 不用 support.sample_bars：那两根的 1.2→2.2 是 +83%，A 股那条涨跌幅守卫会把它读成
    # 「一定有未记录的公司行为」而整段拒绝出数。这里要的是「库里有几根能画的线」，
    # 所以给三根涨幅正常的价。
    last = date.today()
    closes = [1490.0, 1500.0, 1510.0]
    logged.context.bars.upsert(
        pl.DataFrame(
            {
                "market": [market] * 3,
                "code": [code] * 3,
                "date": [last - timedelta(days=offset) for offset in (6, 5, 4)],
                "open": closes,
                "high": [value + 10 for value in closes],
                "low": [value - 10 for value in closes],
                "close": closes,
                "volume": [100.0, 200.0, 300.0],
                "amount": [value * 100 for value in closes],
                "source": ["akshare"] * 3,
            }
        )
    )
    # 覆盖确认也要给：`series()` 读完日线还会查那道调整闸，没确认过的区间它不肯出数
    logged.context.corp_actions_coverage.record(
        market,
        code,
        covered_from=date(2000, 1, 1),
        covered_to=last,
        full_history=True,
        src="akshare",
    )


def _seed_dividend(logged):
    """30 根日线 + 一次**已记录**的除息：裸价那一步掉下来，后复权那一步不掉。

    这一份是专门用来钉「图上那根收盘价与均线同基」的：20 根 42.00、10 根 39.90，
    除息日落在两者之间。`close_hfq` 从此处起乘 42.00/39.90，末根的复权价与 MA20
    都回到 42.00，而裸价是 39.90 —— 两者相差 5.3%，画在同一根轴上就是均线悬在价格线上方。
    不登记这条 corp_actions 的话守卫会当它「未记录的公司行为」整段拒绝出数。
    """
    market, code = "A", "600519"
    logged.context.symbols.upsert(
        pl.DataFrame(
            {"market": [market], "code": [code], "name": ["贵州茅台"], "currency": ["CNY"]}
        )
    )
    last = date.today() - timedelta(days=4)
    days = [last - timedelta(days=offset) for offset in range(29, -1, -1)]
    closes = [42.00] * 20 + [39.90] * 10
    logged.context.bars.upsert(
        pl.DataFrame(
            {
                "market": [market] * 30,
                "code": [code] * 30,
                "date": days,
                "close": closes,
                "source": ["akshare"] * 30,
            }
        )
    )
    logged.context.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "ex_date": [days[20]],
                "kind": ["dividend"],
                "cash_per_share": [2.10],
                "ratio": [None],
                "price": [None],
                "src": ["akshare"],
                "note": ["10派21.00元(含税)"],
            }
        )
    )
    logged.context.corp_actions_coverage.record(
        market,
        code,
        covered_from=date(2000, 1, 1),
        covered_to=date.today(),
        full_history=True,
        src="akshare",
    )


def _seed_quote(web, market="A", code="600519"):
    now = web.core.clock.now_utc()
    web.core.board.update(
        market,
        [Quote(market=market, code=code, ts=now, price=1500.0, source="tencent")],
        now=now,
        error=None,
    )


def test_symbol_page_needs_a_configured_market_and_a_real_row(logged):
    assert logged.client.get("/symbol/XX/1").status_code == 404
    assert logged.client.get("/symbol/A/999999").status_code == 404


def test_symbol_page_shows_quote_bars_rules_and_theses(logged):
    _seed(logged)
    _seed_quote(logged)
    user = logged.context.users.get_by_username("alice")
    logged.context.rule_service.add(
        user.id,
        kind="stop.fixed",
        name="茅台止损",
        target_type="symbol",
        target_ref="A/600519",
        params={"price": 1400.0},
    )
    logged.context.theses.add(user.username, "A", "600519", note="高端酒景气")
    # TIMESTAMPTZ 回读不稳定（有的表给 UTC、有的给会话本地）：模板里那句 strftime 会把
    # 「立论 15:00 / 时间线 23:00」印在同一屏上，所以告警的时刻要和行情那行同样折算到 display_tz
    triggered = datetime(2026, 10, 21, 1, 30, tzinfo=UTC)
    logged.context.alerts_store.create(
        user.id,
        rule_id=None,
        rule_name="跌破止损",
        account_id=None,
        account_name="",
        market="A",
        code="600519",
        symbol_name="贵州茅台",
        severity="strong",
        stage="triggered",
        value_text="1400.00",
        threshold_text="1410.00",
        action_note="跌破止损线",
        triggered_at=triggered,
    )
    page = logged.client.get("/symbol/A/600519")
    assert page.status_code == 200
    assert "1,500.00" in page.text
    assert "茅台止损" in page.text
    assert "高端酒景气" in page.text
    assert "/api/symbol/A/600519/bars" in page.text
    assert moment_label(triggered, logged.context.settings.server.display_tz) in page.text


def test_symbol_page_normalises_a_loose_code(logged):
    """`/symbol/HK/700` 与 `/symbol/HK/00700` 是同一只票：库里存的是后者。

    主档那一眼过得去（`symbols.get` 自己归一），后面的每一眼都不过去：行情板的键是
    `(quote.market, quote.code)` 精确取、论存的是归一后的代码。不归一就会给一只确实
    存在的票画出「无行情 + 空论点」——而它昨天刚被巡检确认过有报价。
    """
    ctx = logged.context
    ctx.symbols.upsert(
        pl.DataFrame(
            {"market": ["HK"], "code": ["00700"], "name": ["腾讯控股"], "currency": ["HKD"]}
        )
    )
    _seed_quote(logged, "HK", "00700")
    user = ctx.users.get_by_username("alice")
    ctx.theses.add(user.username, "HK", "00700", note="微信是入口")
    ctx.rule_service.add(
        user.id,
        kind="stop.fixed",
        name="腾讯止损",
        target_type="symbol",
        target_ref="HK/700",
        params={"price": 1.0},
    )
    page = logged.client.get("/symbol/HK/700")
    assert page.status_code == 200
    assert "腾讯止损" in page.text
    assert "1,500.00" in page.text
    assert "微信是入口" in page.text


def _seed_list_row(logged, market="A", code="600519"):
    """只种代码表（symbol_master），主档与日线都不种：D146 兜底要走的正是这个形态。"""
    logged.context.symbol_list_store.upsert_batch(
        market,
        [
            {
                "code": code,
                "name": "贵州茅台",
                "py_initials": "gzym",
                "py_full": "guizhoumaotai",
                "currency": "CNY",
            }
        ],
    )


def test_code_table_row_renders_pending_state(logged):
    """代码表认识这只票、主档还没追上：进页兜底建行 + 排一拍，不再 404（D146）。"""
    _seed_list_row(logged)
    page = logged.client.get("/symbol/A/600519")
    assert page.status_code == 200
    assert "待同步" in page.text
    assert "已排队" in page.text
    ctx = logged.context
    assert ctx.symbols.get("A", "600519")["name"] == "贵州茅台"
    assert ctx.backfills_store.open_for("A", "600519") is not None


def test_deferred_backfill_tells_today_tries_failed(logged):
    """deferred 的话要说清「今天试过几次、最后错在哪」：光一句待同步会让人反复点按钮。"""
    _seed_list_row(logged)
    ctx = logged.context
    logged.client.get("/symbol/A/600519")  # 兜底那一脚把拍排上
    backfill = ctx.backfills_store.open_for("A", "600519")
    for _ in range(3):
        ctx.backfills_store.note_failure(backfill.id, "源站超时")
    page = logged.client.get("/symbol/A/600519")
    assert "今天已试过 3 次都没成（源站超时）" in page.text


def test_page_with_bars_has_no_empty_state(logged):
    _seed(logged)
    page = logged.client.get("/symbol/A/600519")
    assert "待同步" not in page.text


def test_unknown_code_still_404(logged):
    """代码表也查无 = 「不认识这串代码」：404 保留（D146）。"""
    assert logged.client.get("/symbol/A/999999").status_code == 404


def test_unconfigured_market_never_falls_back(logged):
    """市场没配置时兜底不许发生：`SymbolInterest.ensure` 会拿 `markets[market]` 崩 KeyError，
    挡住的是 500，也更早回答「这市场本机不服务」。"""
    logged.context.symbol_list_store.upsert_batch(
        "XX",
        [{"code": "1", "name": "无名", "py_initials": "wm", "py_full": "wuming",
          "currency": "XXD"}],
    )
    assert logged.client.get("/symbol/XX/1").status_code == 404


def test_sync_button_requeues(logged):
    """「立即排一拍」：入队 + 303 回本页，不承诺同步已经发生（D146）。"""
    _seed_list_row(logged)
    response = logged.client.post(
        "/symbol/A/600519/sync", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/symbol/A/600519"
    assert logged.context.backfills_store.open_for("A", "600519") is not None


def test_sync_button_requires_csrf(logged):
    response = logged.client.post(
        "/symbol/A/600519/sync", data={"csrf": "bogus"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_bars_api_falls_back_too(logged):
    """K 线 JSON 走同一个 `_require_symbol`：只种代码表也该 200（回 note，不是 404）。"""
    _seed_list_row(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?days=120")
    assert payload.status_code == 200
    assert payload.json()["dates"] == []


def test_quote_board_code_links(logged):
    """报价板那行永远挂链接（N4 了结）：主档缺失由代码表兜底，不再有「死文字」那一型。"""
    user = logged.context.users.get_by_username("alice")
    logged.context.watchlists.add(user.id, "A", "600519")
    _seed_quote(logged)
    page = logged.client.get("/api/partials/quotes")
    assert "/symbol/A/600519" in page.text
    assert "主档未同步" not in page.text


def test_bars_api_returns_dates_and_close(logged):
    _seed(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?days=120").json()
    assert payload["dates"], "窗口内应有最近几根日线"
    assert payload["close_hfq"]


def test_bars_api_plots_the_close_on_the_basis_the_mas_use(logged):
    """`metrics()` 的 MA 全算在 `close_hfq` 上（indicators.ma → _prices("close_hfq")）：
    图上那根收盘价若是裸价，一次分红就把两条均线顶到价格线上方，看上去像永远破不了支撑。"""
    _seed_dividend(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?days=120").json()
    assert len(payload["dates"]) == len(payload["close_hfq"])
    assert payload["close_hfq"][-1] == pytest.approx(payload["ma20"][-1], rel=0.01)


def test_bars_api_without_bars_returns_a_note_not_a_500(logged):
    logged.context.symbols.upsert(
        pl.DataFrame({"market": ["A"], "code": ["600036"], "name": ["招行"], "currency": ["CNY"]})
    )
    payload = logged.client.get("/api/symbol/A/600036/bars?days=120").json()
    assert payload["dates"] == []
    assert payload["note"]
