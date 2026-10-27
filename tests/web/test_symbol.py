"""个股页与 K 线 JSON。日历用 StubCalendar，行情与日线都在进程里。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from sams.core.quotes import Quote
from sams.web.display import moment_label

#: 三只种子共用的假档案：改名字/币种时不用挨个函数找
_NAME = "贵州茅台"
_CURRENCY = "CNY"


def _seed(logged, market="A", code="600519"):
    logged.context.symbols.upsert(
        pl.DataFrame({"market": [market], "code": [code], "name": [_NAME], "currency": [_CURRENCY]})
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
        pl.DataFrame({"market": [market], "code": [code], "name": [_NAME], "currency": [_CURRENCY]})
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
    # 图本体改由 chart 局部按需拉（D158）：首屏里从此是局部的 URL，bars 的 URL 在局部渲染时才出现
    assert "/api/partials/chart?market=A&code=600519" in page.text
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
                "name": _NAME,
                "py_initials": "gzym",
                "py_full": "guizhoumaotai",
                "currency": _CURRENCY,
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
    # pending 态按钮照旧给（虽然再点只是排队去重，它不改队列状态）
    assert "/symbol/A/600519/sync" in page.text
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
    assert "3 次" in page.text
    assert "源站超时" in page.text


def test_deferred_state_hides_the_sync_button(logged):
    """deferred 时「立即排一拍」是空按钮：enqueue 对着 open_key 返回 False，队列纹丝不动，
    摆了就是骗人点（D146 评审）。"""
    _seed_list_row(logged)
    ctx = logged.context
    logged.client.get("/symbol/A/600519")
    backfill = ctx.backfills_store.open_for("A", "600519")
    for _ in range(3):
        ctx.backfills_store.note_failure(backfill.id, "源站超时")
    page = logged.client.get("/symbol/A/600519")
    assert page.status_code == 200
    assert "/symbol/A/600519/sync" not in page.text
    assert "立即排一拍" not in page.text


def test_deferred_without_error_leaves_no_empty_parenthesis(logged):
    """last_error 空着时不许印「都没成（）」：空括注是排版事故，不是信息（D146 评审）。"""
    _seed_list_row(logged)
    ctx = logged.context
    logged.client.get("/symbol/A/600519")
    backfill = ctx.backfills_store.open_for("A", "600519")
    for _ in range(3):
        ctx.backfills_store.note_failure(backfill.id, "")  # 空错误串：守卫截第一行得 ""
    page = logged.client.get("/symbol/A/600519")
    assert "都没成，" in page.text
    assert "（）" not in page.text


def test_page_with_bars_has_no_empty_state(logged):
    _seed(logged)
    page = logged.client.get("/symbol/A/600519")
    assert "待同步" not in page.text


def test_delisted_code_does_not_fall_back(logged):
    """代码表里在但已退市（is_active=false）：不兜底建行、不排补数——死码占一个 FIFO 名额，
    每一拍都白试一次（D146 评审）。"""
    ctx = logged.context
    ctx.symbol_list_store.upsert_batch(
        "A",
        [{"code": "600001", "name": "退市股", "py_initials": "tsg", "py_full": "tuishigu",
          "currency": "CNY"}],
    )
    # 用仓库自己的关停法：给一份不含 600001 的在册清单，它就被置为 is_active=false
    ctx.symbol_list_store.deactivate_missing("A", ["600000"])
    assert logged.client.get("/symbol/A/600001").status_code == 404
    assert ctx.symbols.get("A", "600001") is None
    assert ctx.backfills_store.open_for("A", "600001") is None


def test_unknown_code_still_404(logged):
    """代码表也查无 = 「不认识这串代码」：404 保留（D146）。"""
    assert logged.client.get("/symbol/A/999999").status_code == 404


def test_unconfigured_market_never_falls_back(logged):
    """市场不在本机配置清单 = 这一屏不可达：404 本身就是这条规则（不是只为挡 KeyError），
    兜底也不许在这里发生。"""
    logged.context.symbol_list_store.upsert_batch(
        "XX",
        [{"code": "1", "name": "无名", "py_initials": "wm", "py_full": "wuming",
          "currency": "XXD"}],
    )
    assert logged.client.get("/symbol/XX/1").status_code == 404


def test_no_queued_backfill_says_so(logged):
    """主档早有、只是从没排过补数：这一档不许说「下一拍会自动补」——没排队就是没排队，
    按钮才是这一档真正有效的出路（D146 评审）。"""
    logged.context.symbols.upsert(
        pl.DataFrame({"market": ["A"], "code": ["600036"], "name": ["招行"], "currency": ["CNY"]})
    )
    page = logged.client.get("/symbol/A/600036")
    assert page.status_code == 200
    assert "下一拍会自动补" not in page.text
    assert "没有排队" in page.text
    assert "/symbol/A/600036/sync" in page.text


def test_index_shaped_code_is_not_a_stock_page(logged):
    """指数串不许进个股页（D145 撞车）：`sh000001` 归一后就是 000001=平安银行，
    认的是 URL 里**原始**那一段——指数的地方是大盘条，兜底也不许为它建个股行。"""
    _seed_list_row(logged, "A", "000001")  # 在册的 000001：没有守卫的话兜底会放它进来
    assert logged.client.get("/symbol/A/sh000001").status_code == 404
    assert logged.client.get("/symbol/A/sz399001").status_code == 404
    assert logged.client.get("/api/symbol/A/sh000001/bars").status_code == 404
    response = logged.client.post(
        "/symbol/A/sh000001/sync", data={"csrf": logged.csrf}, follow_redirects=False
    )
    assert response.status_code == 404
    # HK/US 的指数形态（前缀 + 大写字母尾）同理
    assert logged.client.get("/symbol/HK/hkHSI").status_code == 404
    assert logged.client.get("/symbol/US/usIXIC").status_code == 404
    assert logged.context.symbols.get("A", "000001") is None
    assert logged.context.backfills_store.open_for("A", "000001") is None
    # 个股不受伤：裸 6 位与 5 位港股码照旧是股票页
    assert logged.client.get("/symbol/A/000001").status_code == 200
    logged.context.symbols.upsert(
        pl.DataFrame(
            {"market": ["HK"], "code": ["00700"], "name": ["腾讯控股"], "currency": ["HKD"]}
        )
    )
    assert logged.client.get("/symbol/HK/00700").status_code == 200


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
    assert payload["close_hfq"][-1] == pytest.approx(payload["main"]["ma"]["ma20"][-1], rel=0.01)


def test_bars_api_without_bars_returns_a_note_not_a_500(logged):
    logged.context.symbols.upsert(
        pl.DataFrame({"market": ["A"], "code": ["600036"], "name": ["招行"], "currency": ["CNY"]})
    )
    payload = logged.client.get("/api/symbol/A/600036/bars?days=120").json()
    assert payload["dates"] == []
    assert payload["note"]


def test_bars_api_gives_the_four_candle_prices_on_the_adjusted_basis(logged):
    """蜡烛要四档，而四档都得在与均线同一基上（D159）。
    复权之后 `high ≥ max(open, close)`、`low ≤ min(open, close)` 仍要成立，
    否则画出来的是一根实体穿过影线的假蜡烛。"""
    _seed(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?days=120").json()
    for column in ("open", "high", "low", "close"):
        assert payload[column], f"{column} 该有数"
        assert len(payload[column]) == len(payload["dates"])
    last = -1
    assert payload["high"][last] >= max(payload["open"][last], payload["close"][last])
    assert payload["low"][last] <= min(payload["open"][last], payload["close"][last])
    assert payload["close"] == payload["close_hfq"], "图上那一档收价就是后复权那一档"


def test_bars_api_5d_resamples_from_the_daily_bars(logged):
    """5 日线吃的是库里的日线，不新增数据源（D160）：三根日线 ⇒ 一根没走完的 5 日 K。

    这里点 `5d` 而不是 `week`：`_seed` 写的是「今天 -6/-5/-4 天」这连续三日，周五和周六
    那两天它会跨进两个自然周，`period=week` 的断言就会跟着跑测试的那天星期几漂移。
    N 日线按根数分组，与星期几无关，因此能钉死。
    """
    _seed(logged, code="600520")
    payload = logged.client.get("/api/symbol/A/600520/bars?days=120&period=5d").json()
    assert payload["bars"] == [3]
    assert payload["pending"], "只盖了三根的 5 日 K 不许冒充走完的那一根"


def test_bars_api_period_week_groups_the_same_daily_bars(logged):
    """周线那一档也要证明它只是聚合：几根 K 线加起来必须正好是三根日线。"""
    _seed(logged, code="600521")
    payload = logged.client.get("/api/symbol/A/600521/bars?days=120&period=week").json()
    assert sum(payload["bars"]) == 3


def test_bars_api_rejects_an_unknown_period_with_400(logged):
    _seed(logged)
    assert logged.client.get("/api/symbol/A/600519/bars?period=weekly").status_code == 400


def test_bars_api_rejects_an_unknown_indicator_with_400_and_names_the_options(logged):
    """拼错一个指标名不该让人去看 500 的空白页：分析层那句「可选的是 …」要上屏（D162）。"""
    _seed(logged)
    resp = logged.client.get("/api/symbol/A/600519/bars?sub=macdd")
    assert resp.status_code == 400
    assert "可选的是" in resp.text


def test_bars_api_refuses_more_than_three_sub_panes(logged):
    """副图最多三格（D162）：第四格不是看图，是刷屏。"""
    _seed(logged)
    assert logged.client.get("/api/symbol/A/600519/bars?sub=macd,kdj,rsi,atr").status_code == 400


def test_bars_api_main_group_carries_every_window_the_ma_family_gives(logged):
    """均线族出几列由 `ma=` 说了算，前端不自己记名单（D162）。"""
    _seed(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?ma=5,20").json()
    assert sorted(payload["main"]["ma"]) == ["ma20", "ma5"]


def test_chart_partial_marks_the_current_period(logged):
    """当前档必须看得出来（D158）：换档之后旧档不再带 `aria-current`。
    钉属性而不是钉整段文本——文案改了这枚用例不该跟着红。
    周期条只有一档，所以「整段里 `aria-current` 恰好一次」就是判据；指标开关表达的是
    「按下/松开」不是「现在在哪」，它带 `aria-pressed`，不占这个数——读屏软件会把
    一排 `aria-current` 念成「这里有七个『当前』，请选一个」，而它其实是七个复选框。"""
    _seed(logged)
    week = logged.client.get("/api/partials/chart?market=A&code=600519&period=week")
    assert week.status_code == 200
    assert 'aria-current="true"' in week.text
    assert 'data-chart-url="/api/symbol/A/600519/bars?period=week' in week.text
    day = logged.client.get("/api/partials/chart?market=A&code=600519&period=day")
    assert day.text.count('aria-current="true"') == 1
    assert day.text.count('aria-pressed="true"') == 2  # 默认那组：主图均线 + 副图成交量


def test_chart_partial_lists_the_indicator_switches_from_the_server_names(logged):
    """开关上的名字来自服务端那份白名单（D162）：前端不自己记一份，
    否则分析层加一个指标，屏幕上永远少一个。"""
    _seed(logged)
    text = logged.client.get("/api/partials/chart?market=A&code=600519").text
    for name in ("ma", "boll", "macd", "kdj", "rsi", "vol"):
        assert f'data-indicator="{name}"' in text


def test_bars_api_dmi_group_holds_only_its_three_lines(logged):
    """`dmi` 那三列的名字不是 `dmi*`（是 `pdi/mdi/adx`），族名当 prefix 会把
    `date/close/bars` 一起拖进那一格，前端就照着日期字符串画一条线。
    这枚钉的是「一族只装这一族的列」，不是 `dmi` 的算法。"""
    _seed(logged)
    payload = logged.client.get("/api/symbol/A/600519/bars?sub=dmi&main=ma").json()
    assert sorted(payload["sub"]["dmi"]) == ["adx", "mdi", "pdi"]


def test_symbol_page_uses_the_chart_partial(logged):
    _seed(logged)
    page = logged.client.get("/symbol/A/600519")
    hit = "/api/partials/chart?market=A&amp;code=600519" in page.text
    assert hit or "api/partials/chart" in page.text


def test_chart_partial_requires_a_login(web_app):
    """未登录拿不到这一块：路由第一行就 `require_user`，别让它先去摸数据库。
    钉 303 与跳转目的地，不钉 401——本站的未登录姿势是重定向去登录页（`app.py:207`）。"""
    response = web_app.client.get(
        "/api/partials/chart?market=A&code=600519", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
