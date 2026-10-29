"""个股页 F10 局部：四张卡、键归一与三种空态（D214–D216）。

资料块是异步局部（`/api/partials/f10`），页面首屏只留一个 loader。这一组用例钉的是
「库里有什么、屏幕上说什么」：比率从小数还原成百分数（D58 的对称面）、港股没有披露日
的脚注只在本市场出现（D215）、US 明说没有源而不是摆一张空表（D214）。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl


def _symbol(ctx, market="A", code="600519", name="贵州茅台", currency="CNY"):
    ctx.symbols.upsert(
        pl.DataFrame(
            {"market": [market], "code": [code], "name": [name], "currency": [currency]}
        )
    )


def _profile(ctx, market="A", code="600519", name="贵州茅台酒"):
    ctx.profiles_store.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "name": [name],
                "full_name": [f"{name}股份有限公司"],
                "industry": ["酿酒行业"],
                "industry_csrc": ["酒、饮料和精制茶制造业"],
                "exchange": ["上交所"],
                "board": ["主板"],
                "listed_on": [date(2001, 8, 27)],
                "website": ["www.moutaichina.com"],
                "chairman": ["张德芹"],
                "phone": ["0851-22386002"],
                "employees": [34000],
                "summary": ["公司主要业务是茅台酒及系列酒的生产与销售。"],
                "source": ["eastmoney_f10"],
            }
        )
    )


def _financials(ctx, market="A", code="600519", published=None):
    rows = 3
    ctx.financials_store.upsert(
        pl.DataFrame(
            {
                "market": [market] * rows,
                "code": [code] * rows,
                "report_date": [date(2025, 6, 30), date(2025, 3, 31), date(2024, 12, 31)],
                "published_date": pl.Series(
                    "published_date", [published] * rows, dtype=pl.Date
                ),
                "roe": [0.09966, 0.05, 0.2],
                "revenue_yoy": [0.1007, 0.04, 0.15],
                "profit_yoy": [0.085, -0.03, 0.12],
                "dividend_yield": [0.025, None, 0.03],
                "source": ["eastmoney_hk_f10" if market == "HK" else "akshare"] * rows,
            }
        )
    )


def _wind_financials(ctx, market="A", code="600519"):
    """Wind 口径的三期：两期年报 + 一期中报，金额是源的本币原值。"""
    ctx.financials_store.upsert(
        pl.DataFrame(
            {
                "market": [market] * 3,
                "code": [code] * 3,
                "report_date": [date(2025, 12, 31), date(2024, 12, 31), date(2026, 6, 30)],
                "published_date": [date(2026, 3, 28), date(2025, 3, 29), date(2026, 8, 15)],
                "report_type": ["年报", "年报", "中报"],
                "roe": [0.312, 0.291, 0.15],
                "revenue_yoy": [0.0173, 0.0321, 0.013],
                "profit_yoy": [0.0212, 0.0411, 0.009],
                "revenue": [1.70899e11, 1.74e11, 9.2278e10],
                "net_profit": [8.6e10, 8.4e10, 4.4517e10],
                "eps": [66.0, 64.0, 35.0],
                "bps": [200.0, 190.0, 205.0],
                "ocf_per_share": [70.0, 60.0, 35.0],
                "gross_margin": [0.915, 0.92, 0.90],
                "net_margin": [0.503, 0.48, 0.48],
                "roa": [0.25, 0.24, 0.12],
                "roic": [None, None, None],
                "debt_ratio": [0.18, 0.17, 0.19],
                "current_ratio": [3.5, 3.2, 3.1],
                "quick_ratio": [3.0, 2.8, 2.7],
                "inventory_turnover": [0.4, 0.38, 0.2],
                "total_asset_turnover": [0.5, 0.49, 0.25],
                "source": ["akshare"] * 3,
            }
        )
    )


def _valuation(
    ctx,
    market="A",
    code="600519",
    dividend_yield=0.024,
    market_cap=2.0e12,
):
    ctx.valuation_store.upsert(
        pl.DataFrame(
            {
                "market": [market],
                "code": [code],
                "date": [date(2026, 10, 28)],
                "pe_ttm": [21.5],
                "pb": [5.25],
                "dividend_yield": [dividend_yield],
                "market_cap": [market_cap],
                "source": ["eastmoney"],
            }
        )
    )


def _announcements(ctx, market="A", code="600519"):
    ctx.announcements_store.upsert(
        pl.DataFrame(
            {
                "market": [market, market],
                "code": [code, code],
                "art_code": ["AN202610280001", "AN202610200002"],
                "published_on": [date(2026, 10, 28), date(2026, 10, 20)],
                "title": ["贵州茅台:2026年第三季度报告", "贵州茅台:关于回购股份的进展公告"],
                "category": ["定期报告", "其他"],
                "url": [
                    f"https://data.eastmoney.com/notices/detail/{code}/AN202610280001.html",
                    f"https://data.eastmoney.com/notices/detail/{code}/AN202610200002.html",
                ],
                "source": ["eastmoney_f10", "eastmoney_f10"],
            }
        )
    )


def _dividends(ctx, market="A", code="600519"):
    today = date.today()
    ctx.corp_actions.upsert_many(
        pl.DataFrame(
            {
                "market": [market, market, market],
                "code": [code, code, code],
                "ex_date": [
                    today - timedelta(days=90),
                    today - timedelta(days=400),
                    today + timedelta(days=30),
                ],
                "kind": ["dividend", "unknown", "dividend"],
                "cash_per_share": [23.88, None, 10.0],
                "ratio": [None, None, None],
                "price": [None, None, None],
                "src": ["eastmoney"] * 3,
                "note": [
                    "10派238.80元(含税)",
                    "以股代息：每10股派0.5股",
                    "还没发生的除权",
                ],
            }
        )
    )


def test_partial_renders_the_four_cards_for_a_share(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _profile(ctx)
    _financials(ctx, published=date(2025, 8, 31))
    _valuation(ctx)
    _announcements(ctx)
    _dividends(ctx)

    text = logged_f10.client.get(
        "/api/partials/f10?market=A&code=600519&view=table"
    ).text

    assert "公司概况" in text
    assert "贵州茅台酒股份有限公司" in text
    assert "酿酒行业" in text
    assert "酒、饮料和精制茶制造业" in text
    assert 'href="https://www.moutaichina.com"' in text
    assert "34,000" in text
    assert "公司主要业务是茅台酒及系列酒的生产与销售。" in text

    assert "主要财务指标" in text
    # 库里的比率是小数目（0.09966 = 9.97%）：还原成百分数只经 ratio_pct 一处
    assert "9.97%" in text
    assert "10.07%" in text
    # 估值那一行来自 valuation_daily，不是财务表的某一期（D60）
    assert "21.50" in text and "5.25" in text
    assert "2026-10-28" in text

    assert "分红送转" in text
    assert "派息" in text
    assert "每股 23.88 元" in text
    # 认不出的方案原样留备注，不擦掉（D39）；未来的除权日不进来（PIT）
    assert "以股代息" in text
    assert "还没发生的除权" not in text

    assert "公告" in text
    assert "2026年第三季度报告" in text
    assert (
        'href="https://data.eastmoney.com/notices/detail/600519/AN202610280001.html"'
        in text
    )
    # A 股不摆港股那句披露日脚注：它是一条真实的口径，不是缺口（D215）
    assert "不参与选股与市赚率" not in text


def test_symbol_page_loads_the_f10_partial(logged_f10):
    _symbol(logged_f10.context)
    page = logged_f10.client.get("/symbol/A/600519")
    assert "/api/partials/f10?market=A&code=600519" in page.text


def test_a_market_without_a_source_says_so_instead_of_empty_tables(logged_f10):
    _symbol(logged_f10.context, "US", "AAPL", "苹果", "USD")
    text = logged_f10.client.get("/api/partials/f10?market=US&code=AAPL").text
    assert "美股的这条链上还没有公司概况的源" in text
    assert "美股的这条链上还没有公告的源" in text
    # 不摆一张空表：概况卡里不该出现简介那一段
    assert "公司简介" not in text


def test_a_source_without_local_data_prints_the_command(logged_f10):
    _symbol(logged_f10.context)
    text = logged_f10.client.get("/api/partials/f10?market=A&code=600519").text
    assert "本地还没有这只票的概况" in text
    assert "sams sync profile --market A --code 600519" in text
    assert "sams sync announcements --market A --code 600519" in text


def test_hk_page_normalises_the_code_for_the_same_material(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx, "HK", "00700", "腾讯控股", "HKD")
    _profile(ctx, "HK", "00700", "腾讯控股")
    page = logged_f10.client.get("/symbol/HK/700")
    assert page.status_code == 200
    assert "/api/partials/f10?market=HK&code=00700" in page.text
    loose = logged_f10.client.get("/api/partials/f10?market=HK&code=700").text
    exact = logged_f10.client.get("/api/partials/f10?market=HK&code=00700").text
    assert loose == exact
    assert "腾讯控股" in exact


def test_hk_financials_carry_the_no_published_date_footnote(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx, "HK", "00700", "腾讯控股", "HKD")
    _financials(ctx, "HK", "00700", published=None)
    text = logged_f10.client.get(
        "/api/partials/f10?market=HK&code=00700&view=table"
    ).text
    assert "未给" in text
    assert "不参与选股与市赚率" in text
    assert "9.97%" in text


def test_f10_partial_requires_a_login(web_app):
    response = web_app.client.get(
        "/api/partials/f10?market=A&code=600519", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_f10_partial_404s_for_a_symbol_that_does_not_exist(logged_f10):
    assert (
        logged_f10.client.get("/api/partials/f10?market=A&code=999999").status_code == 404
    )


def test_valuation_line_omits_yield_the_source_does_not_give(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _valuation(ctx, dividend_yield=None)
    text = logged_f10.client.get("/api/partials/f10?market=A&code=600519").text
    # 源的估值口径没有股息率：不拿「—」冒充「没有股息」，整段不出现
    assert "股息率" not in text
    assert "PE(TTM)" in text and "PB 5.25" in text


def test_valuation_line_shows_yield_when_the_source_gives_it(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _valuation(ctx, dividend_yield=0.024)
    text = logged_f10.client.get("/api/partials/f10?market=A&code=600519").text
    assert "股息率 2.40%" in text


def test_market_cap_is_shown_in_compact_units(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _valuation(ctx, market_cap=1.692610487754e12)
    text = logged_f10.client.get("/api/partials/f10?market=A&code=600519").text
    assert "1.69 万亿元" in text
    assert "1,692,610,487,754" not in text


def test_future_disclosure_date_is_labelled_as_scheduled(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    tomorrow = date.today() + timedelta(days=1)
    _financials(ctx, published=tomorrow)
    text = logged_f10.client.get(
        "/api/partials/f10?market=A&code=600519&view=table"
    ).text
    assert str(tomorrow) in text
    assert "（预约）" in text
    # 预约日之后的数不能当真数摆出来：日期留着，数值一律「—」
    assert "9.97%" not in text
    assert "—" in text
    # 已经过去/今天的披露日不加这个标记
    _financials(ctx, published=date(2025, 8, 31))
    text = logged_f10.client.get(
        "/api/partials/f10?market=A&code=600519&view=table"
    ).text
    assert "2025-08-31（预约）" not in text
    assert "9.97%" in text


def test_chart_leaves_out_a_period_whose_disclosure_is_still_ahead(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    ctx.financials_store.upsert(
        pl.DataFrame(
            {
                "market": ["A", "A"],
                "code": ["600519", "600519"],
                "report_date": [date(2024, 12, 31), date(2025, 12, 31)],
                "published_date": [date(2025, 3, 29), date.today() + timedelta(days=1)],
                "report_type": ["年报", "年报"],
                "revenue": [1.74e11, 1.70e11],
                "net_profit": [8.4e10, 8.6e10],
                "source": ["akshare", "akshare"],
            }
        )
    )
    payload = logged_f10.client.get(
        "/api/partials/f10/chart?market=A&code=600519&period_type=annual"
    ).json()
    assert payload["labels"] == ["2024年报"]
    assert payload["revenue"] == [1.74e11]
    # 表里仍留着这一期，但数不露出来
    text = logged_f10.client.get(
        "/api/partials/f10/financials"
        "?market=A&code=600519&period_type=annual&view=table"
    ).text
    assert "2025-12-31" in text and "（预约）" in text
    assert "1700.00" not in text


def test_us_financials_say_the_chain_has_no_source(logged_f10):
    _symbol(logged_f10.context, "US", "AAPL", "苹果", "USD")
    text = logged_f10.client.get("/api/partials/f10?market=US&code=AAPL").text
    assert "美股的这条链上还没有财务指标的源" in text
    # 不再给一条在美股上跑不通的采集命令
    assert "sams sync financials --market US" not in text


# ---------- D218 / D219：五档报告期、图表与明细两种视图 ----------


def test_financials_card_offers_period_and_view_toggles(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    text = logged_f10.client.get("/api/partials/f10?market=A&code=600519").text

    # 两排开关：五档报告期 + 图表/明细，htmx 只换这张卡
    for period in ("all", "annual", "q1", "h1", "q3"):
        assert f"period_type={period}" in text
    assert 'hx-target="#f10-financials"' in text
    assert "view=table" in text
    # 默认图表视图：挂的是财务图的数据端点，不是 K 线那一个
    assert 'data-chart-kind="finance"' in text
    assert "/api/partials/f10/chart?market=A&code=600519&period_type=all" in text
    # 默认不摆明细表：表格里的指标名只在 table 视图出现
    assert "营业总收入（亿）" not in text


def test_financials_table_view_lays_out_multi_periods_and_groups(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    text = logged_f10.client.get(
        "/api/partials/f10/financials"
        "?market=A&code=600519&period_type=annual&view=table"
    ).text

    assert "2025-12-31" in text and "2024-12-31" in text
    # 两期都是年报：表头第二行带档名，而不是把日期孤零零摆着
    assert text.count("年报") >= 2
    # 指标按组排：规模 / 增长 / 盈利能力 / 每股 / 偿债与运营
    for group in ("规模", "增长", "盈利能力", "每股", "偿债与运营"):
        assert group in text
    # 金额是源的本币原值 / 1e8，比率走 ratio_pct，NULL 摆「—」
    assert "1708.99" in text and "1740.00" in text
    assert "31.20%" in text
    assert "ROIC" in text
    assert "—" in text
    # 中报被档位筛掉：它只在「全部」那一档出现
    assert "922.78" not in text


def test_financials_table_view_keeps_the_hk_footnote(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx, "HK", "00700", "腾讯控股", "HKD")
    _wind_financials(ctx, "HK", "00700")
    text = logged_f10.client.get(
        "/api/partials/f10/financials"
        "?market=HK&code=00700&period_type=annual&view=table"
    ).text
    assert "不参与选股与市赚率" in text
    # 港股报表是原币列示（多为人民币）：金额与每股都不能挂 HKD 说成港元
    assert "报表原币" in text
    assert "亿元（HKD）" not in text
    assert "66.00 港元" not in text
    assert "66.00" in text


def test_a_share_financials_state_their_currency(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    text = logged_f10.client.get(
        "/api/partials/f10/financials"
        "?market=A&code=600519&period_type=annual&view=table"
    ).text
    assert "亿元（CNY）" in text
    assert "66.00 元" in text
    assert "报表原币" not in text


def test_empty_period_bucket_says_so_instead_of_an_empty_chart(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)  # 只有年报与中报，没有一季报
    text = logged_f10.client.get(
        "/api/partials/f10/financials?market=A&code=600519&period_type=q1&view=chart"
    ).text
    assert "这一类报告期本地还没有数据。" in text
    assert 'data-chart-kind="finance"' not in text


def test_chart_endpoint_returns_old_to_new_and_keeps_nulls(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    payload = logged_f10.client.get(
        "/api/partials/f10/chart?market=A&code=600519&period_type=annual"
    ).json()

    assert payload["labels"] == ["2024年报", "2025年报"]
    assert payload["revenue"] == [1.74e11, 1.70899e11]
    assert payload["net_profit"] == [8.4e10, 8.6e10]
    # 比率在这里已经是百分数（0.291 → 29.1），轴与 tooltip 共用同一单位
    assert payload["roe"] == [29.1, 31.2]
    assert payload["gross_margin"] == [92.0, 91.5]
    assert payload["net_margin"] == [48.0, 50.3]
    assert payload["currency"] == "CNY"
    assert payload["note"] is None


def test_chart_endpoint_passes_missing_values_through_as_null(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    ctx.financials_store.upsert(
        pl.DataFrame(
            {
                "market": ["A"],
                "code": ["600519"],
                "report_date": [date(2023, 12, 31)],
                "report_type": ["年报"],
                "revenue": [None],
                "net_profit": [None],
                "roe": [None],
                "source": ["akshare"],
            }
        )
    )
    payload = logged_f10.client.get(
        "/api/partials/f10/chart?market=A&code=600519&period_type=annual"
    ).json()
    # 缺的那一期不能补 0：补零会画出一条「利润归零」的假曲线
    assert payload["labels"][0] == "2023年报"
    assert payload["revenue"][0] is None and payload["roe"][0] is None


def test_chart_endpoint_explains_an_empty_bucket(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    payload = logged_f10.client.get(
        "/api/partials/f10/chart?market=A&code=600519&period_type=q1"
    ).json()
    assert payload["labels"] == []
    assert payload["note"] == "这一类报告期本地还没有数据。"


def test_chart_and_table_endpoints_refuse_unknown_options(logged_f10):
    ctx = logged_f10.context
    _symbol(ctx)
    _wind_financials(ctx)
    bad_period = logged_f10.client.get(
        "/api/partials/f10/chart?market=A&code=600519&period_type=monthly"
    )
    assert bad_period.status_code == 400
    bad_view = logged_f10.client.get(
        "/api/partials/f10/financials"
        "?market=A&code=600519&period_type=all&view=picture"
    )
    assert bad_view.status_code == 400
