"""个股页基本面三块：核心业务的草稿/确认、同业中位、估值分位条（S2 / D236–D241）。

三块都是异步局部，页面首屏只放 loader：任何一块慢或失败都不挡 K 线。这里钉的是
「屏幕上有没有那句话」——草稿必须标「待确认」，同业中位带 n，分位条带窗口与样本，
港美的同业整卡说未覆盖而不是摆一张空表，保存必须过 CSRF 并 303 回这一张卡。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

BAIJIU = "食品饮料-饮料-白酒"


def _symbol(ctx, market="A", code="600519", name="贵州茅台", currency="CNY", sector=None):
    ctx.symbols.upsert(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "name": name,
                    "currency": currency,
                    "sector": sector,
                }
            ]
        )
    )


def _profiles(ctx, rows):
    ctx.profiles_store.upsert(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "name": name,
                    "industry": industry,
                    "summary": summary,
                    "source": "eastmoney_f10",
                }
                for market, code, name, industry, summary in rows
            ]
        )
    )


def _valuation_rows(ctx, rows):
    """rows: (code, day, pe, pb)。D239：这一列的市值是元，屏幕只用快照的亿元。"""
    ctx.valuation_store.upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "date": day,
                    "pe_ttm": pe,
                    "pb": pb,
                    "market_cap": 1e12,
                    "source": "test",
                }
                for code, day, pe, pb in rows
            ]
        )
    )


def _annual_reports(ctx, code, *, years):
    ctx.financials_store.upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": date(year, 12, 31),
                    "published_date": date(year + 1, 3, 1),
                    "roe": 0.15,
                    "revenue": 1e10,
                    "source": "test",
                }
                for year in years
            ]
        ).with_columns(pl.col("published_date").cast(pl.Date))
    )


def _snapshots(ctx, caps, *, day=None):
    day = day or ctx.clock.today()
    rows = [
        {
            "code": code,
            "name": code,
            "price": 10.0,
            "pct_chg": 1.0,
            "amount": 1e8,
            "turnover_rate": 1.0,
            "volume_ratio": 1.0,
            "amplitude": 1.0,
            "pe_ttm": 10.0,
            "pe_dyn": 9.0,
            "pb": 2.0,
            "market_cap": cap,
            "float_cap": None if cap is None else cap * 0.9,
            "is_st": False,
            "source": "tencent",
        }
        for code, cap in caps.items()
    ]
    ctx.snapshot_store.replace_day(
        market="A", day=day, taken_at=ctx.clock.now_utc(), frame=pl.DataFrame(rows)
    )


def _six_baijiu(ctx, *, valuation_day=None, snapshot_day=None):
    """六个同业。两个日期参数是给「一屏一 as-of」那几条留的钩子：
    读卡日、快照日、估值日全取同一天时，标签错了也看不出来。"""
    basket = [
        ("600519", "贵州茅台", 500.0, 20.0, 0.35, 6e10),
        ("600809", "山西汾酒", 400.0, 10.0, 0.10, 5e10),
        ("000568", "泸州老窖", 300.0, 12.0, 0.15, 4e10),
        ("000596", "古井贡酒", 200.0, 14.0, 0.20, 3e10),
        ("603369", "今世缘", 100.0, 16.0, 0.25, 2e10),
        ("603198", "迎驾贡酒", 50.0, 18.0, 0.30, 1e10),
    ]
    today = ctx.clock.today()
    valuation_day = valuation_day or today
    snapshot_day = snapshot_day or today
    for code, name, _, _, _, _ in basket:
        _symbol(ctx, code=code, name=name, sector=BAIJIU)
    _profiles(
        ctx,
        [
            ("A", code, name, BAIJIU, "公司主要业务是白酒的生产与销售。")
            for code, name, *_ in basket
        ],
    )
    _valuation_rows(ctx, [(code, valuation_day, pe, pb) for code, _, _, pe, pb, _ in basket])
    for code, _name, _, _, roe, revenue in basket:
        ctx.financials_store.upsert(
            pl.DataFrame(
                [
                    {
                        "market": "A",
                        "code": code,
                        "report_date": date(2025, 12, 31),
                        "published_date": date(2026, 3, 20),
                        "roe": roe,
                        "revenue": revenue,
                        "source": "test",
                    }
                ]
            ).with_columns(pl.col("published_date").cast(pl.Date))
        )
    _snapshots(ctx, {code: cap for code, _, cap, *_ in basket}, day=snapshot_day)


def test_the_peers_card_names_the_day_of_every_number(logged):
    """一屏一 as-of（D288）：卡头那一句必须是「那个数的日子」，不是读卡那一天。

    11-04 实测：卡头写「截至 2026-11-04」，而市值取的是 11-03 快照、PE 取的是 11-02 估值、
    ROE 取的是 2025 年报——三个数共用一句今天的话，复盘与归因就按今天取了。
    """
    ctx = logged.context
    today = ctx.clock.today()
    snapshot_day = today - timedelta(days=1)
    valuation_day = today - timedelta(days=2)
    _six_baijiu(ctx, valuation_day=valuation_day, snapshot_day=snapshot_day)

    text = logged.client.get("/api/partials/peers?market=A&code=600519").text

    assert f"市值取 {snapshot_day:%m-%d} 快照" in text
    assert f"估值 {valuation_day:%m-%d}" in text
    assert "2025-12-31 年报" in text
    assert f"截至 {today}" not in text, "读卡日不能占掉数据日那个位置"


def test_the_symbol_page_loads_the_three_fundamentals_blocks(logged):
    _symbol(logged.context)

    page = logged.client.get("/symbol/A/600519").text

    assert "/api/partials/business?market=A&code=600519" in page
    assert "/api/partials/peers?market=A&code=600519" in page
    assert "/api/partials/valuation?market=A&code=600519" in page


def test_business_partial_shows_the_summary_draft_and_the_missing_rows(logged):
    _symbol(logged.context)
    _profiles(
        logged.context,
        [("A", "600519", "贵州茅台", BAIJIU, "公司主要业务是茅台酒及系列酒的生产与销售。")],
    )

    text = logged.client.get("/api/partials/business?market=A&code=600519").text

    assert "核心业务描述" in text
    assert "公司主要业务是茅台酒及系列酒的生产与销售。" in text
    assert "待确认" in text
    assert "收入构成" in text and "尚未录入" in text


def test_peers_partial_lists_five_peers_and_the_median_with_n(logged):
    _six_baijiu(logged.context)

    text = logged.client.get("/api/partials/peers?market=A&code=600519").text

    assert "同业对比" in text
    assert "贵州茅台" in text and "山西汾酒" in text
    assert "同业中位" in text
    assert "n=5" in text, "中位只算五家同业，不含自己"
    assert "三级行业" in text and "第 1 位" in text


def test_valuation_partial_draws_the_percentile_strip_with_window_and_samples(logged):
    ctx = logged.context
    today = ctx.clock.today()
    _symbol(ctx)
    _valuation_rows(
        ctx,
        [
            ("600519", today - timedelta(days=259 - i), 10.0 + i * 0.01, 2.0 + i * 0.001)
            for i in range(260)
        ],
    )
    _annual_reports(ctx, "600519", years=range(today.year - 6, today.year))

    text = logged.client.get("/api/partials/valuation?market=A&code=600519").text

    assert "市赚率 PR" in text and "PE(TTM)" in text and "PB" in text
    assert "percentile-strip" in text
    assert "20/50/80 分位" in text
    assert "过去 5 年" in text
    assert "样本" in text


def test_hk_peers_say_not_covered_instead_of_an_empty_table(logged):
    _symbol(logged.context, market="HK", code="00700", name="腾讯控股", currency="HKD")

    text = logged.client.get("/api/partials/peers?market=HK&code=00700").text

    assert "未覆盖" in text
    assert "只覆盖 A 股" in text
    assert "<table>" not in text


def test_the_three_partials_require_a_login(web_app):
    for path in (
        "/api/partials/business?market=A&code=600519",
        "/api/partials/peers?market=A&code=600519",
        "/api/partials/valuation?market=A&code=600519",
    ):
        response = web_app.client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_saving_business_without_csrf_is_refused(logged):
    _symbol(logged.context)

    response = logged.client.post(
        "/symbol/A/600519/business", data={"business": "x"}, follow_redirects=False
    )

    assert response.status_code == 403


def test_saving_business_redirects_and_reads_back_as_confirmed(logged):
    _symbol(logged.context)
    _profiles(logged.context, [("A", "600519", "贵州茅台", BAIJIU, "公司简介")])
    values = {
        "csrf": logged.csrf,
        "business": "卖酒，靠品牌与渠道赚取毛利",
        "revenue_mix": "茅台酒 84%，系列酒 15%",
        "customers": "前五大客户占比 12%",
        "capacity": "设计产能 4.5 万吨",
        "position": "高端白酒市占率第一",
        "trend": "行业增速放缓，集中度提升",
    }

    response = logged.client.post(
        "/symbol/A/600519/business", data=values, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/symbol/A/600519#business"

    text = logged.client.get("/api/partials/business?market=A&code=600519").text
    assert "卖酒，靠品牌与渠道赚取毛利" in text
    assert "用户录入" in text
    assert "已确认（alice" in text
    assert "待确认" not in text, "六项都已确认，不该再有一行草稿"


def test_clearing_a_field_brings_it_back_to_missing(logged):
    _symbol(logged.context)
    _profiles(logged.context, [("A", "600519", "贵州茅台", BAIJIU, "公司简介")])
    base = {
        "business": "卖酒",
        "revenue_mix": "茅台酒 84%",
        "customers": "前五大客户 12%",
        "capacity": "4.5 万吨",
        "position": "第一",
        "trend": "集中度提升",
    }
    logged.client.post(
        "/symbol/A/600519/business",
        data={"csrf": logged.csrf, **base},
        follow_redirects=False,
    )

    logged.client.post(
        "/symbol/A/600519/business",
        data={"csrf": logged.csrf, **{**base, "revenue_mix": ""}},
        follow_redirects=False,
    )

    text = logged.client.get("/api/partials/business?market=A&code=600519").text
    assert "『收入构成』尚未录入" in text
