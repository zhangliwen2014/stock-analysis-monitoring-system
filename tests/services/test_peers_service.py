"""同业对比 v0：选谁、中位怎么拒、行业地位那句代理话（D236–D241）。

这一屏最容易出的错不是「算错」，而是**悄悄换掉了口径**：市值混用元与亿元、
中位把自己也算进去、层级放宽后不说明、ST 与过期估值混进同业。所以这里的用例
逐条钉住「谁进了表、谁被排除、为什么」，而不是只对最后那个中位数。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.core.missing import (
    NOT_APPLICABLE,
    NOT_COVERED,
    NOT_DISCLOSED,
    UNCOMPUTABLE,
)
from sams.services.peers import PeerService
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.financials import FinancialRepo, ValuationRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbols import SymbolRepo

DAY = date(2026, 10, 30)
REPORT = date(2025, 12, 31)
PUBLISHED = date(2026, 3, 20)
TAKEN = datetime(2026, 10, 30, 8, 30, tzinfo=UTC)
BAIJIU = "食品饮料-饮料-白酒"


def service(
    db, *, today: datetime = datetime(2026, 10, 30, 16)
) -> PeerService:
    return PeerService(
        CompanyProfileRepo(db),
        SymbolRepo(db),
        ValuationRepo(db),
        FinancialRepo(db),
        SnapshotRepo(db),
        clock=FrozenClock(today),
    )


def seed(
    db,
    code: str,
    name: str,
    industry: str | None = BAIJIU,
    *,
    sector: str | None = None,
    list_status: str = "L",
    pe: float | None = 10.0,
    pb: float | None = 2.0,
    roe: float | None = 0.20,
    revenue: float | None = 1e10,
    valuation_day: date = DAY,
    published: date | None = PUBLISHED,
) -> None:
    CompanyProfileRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "name": name,
                    "industry": industry,
                    "source": "eastmoney_f10",
                }
            ]
        )
    )
    SymbolRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "currency": "CNY",
                    "name": name,
                    "sector": sector,
                    "list_status": list_status,
                }
            ]
        )
    )
    ValuationRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "date": valuation_day,
                    "pe_ttm": pe,
                    "pb": pb,
                    # 元：与快照的亿元只差 1e8，混用会让选同业静默错位（D239）
                    "market_cap": 1e12,
                    "source": "test",
                }
            ]
        )
    )
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "report_date": REPORT,
                    "published_date": published,
                    "roe": roe,
                    "revenue": revenue,
                    "source": "test",
                }
            ]
        ).with_columns(pl.col("published_date").cast(pl.Date))
    )


def snapshots(
    db, caps: dict[str, float | None], *, day: date = DAY, st: set[str] | None = None
) -> None:
    st = st or set()
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
            "is_st": code in st,
            "source": "tencent",
        }
        for code, cap in caps.items()
    ]
    SnapshotRepo(db).replace_day(market="A", day=day, taken_at=TAKEN, frame=pl.DataFrame(rows))


def seed_baijiu(
    db,
    *,
    self_revenue: float | None = 6e10,
    with_snapshot: bool = True,
    valuation_day: date = DAY,
    peer_valuation_days: dict[str, date] | None = None,
) -> None:
    """自己 + 5 家白酒：市值、PE、PB、ROE、营收各是一把尺子。"""
    seed(db, "600519", "贵州茅台", pe=20.0, pb=7.0, roe=0.35, revenue=self_revenue,
         valuation_day=valuation_day)
    peers = [
        ("600809", "山西汾酒", 10.0, 2.0, 0.10, 5e10),
        ("000568", "泸州老窖", 12.0, 3.0, 0.15, 4e10),
        ("000596", "古井贡酒", 14.0, 4.0, 0.20, 3e10),
        ("603369", "今世缘", 16.0, 5.0, 0.25, 2e10),
        ("603198", "迎驾贡酒", 18.0, 6.0, 0.30, 1e10),
    ]
    for code, name, pe, pb, roe, revenue in peers:
        seed(
            db,
            code,
            name,
            pe=pe,
            pb=pb,
            roe=roe,
            revenue=revenue,
            valuation_day=(peer_valuation_days or {}).get(code, valuation_day),
        )
    if not with_snapshot:
        return
    snapshots(
        db,
        {
            "600519": 500.0,
            "600809": 400.0,
            "000568": 300.0,
            "000596": 200.0,
            "603369": 100.0,
            "603198": 50.0,
        },
    )


def test_six_baijiu_companies_give_five_peers_medians_and_the_top_rank(db):
    seed_baijiu(db)

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.state == "ok"
    assert report.industry == BAIJIU
    assert report.level_used == "三级"
    assert [row.code for row in report.rows] == [
        "600519",  # 自己永远在第一行
        "600809",
        "000568",
        "000596",
        "603369",
        "603198",
    ]
    assert report.rows[0].is_self

    # 中位只算同业（5 家），不含自己
    assert (report.pe.value, report.pe.samples) == (14.0, 5)
    assert (report.pb.value, report.pb.samples) == (4.0, 5)
    assert report.roe.value == pytest.approx(0.2)
    assert report.roe.samples == 5
    assert report.pe.as_of == DAY

    # 行业地位：营收第 1 / 6，超过 83%，并且自称代理
    assert report.position_kind == "revenue"
    assert "第 1 位" in report.position
    assert "83%" in report.position
    assert "代理指标，不是市占率" in report.position
    assert "2025-12-31" in report.position


def test_market_cap_always_comes_from_the_snapshot_in_yi_yuan(db):
    seed_baijiu(db)

    report = service(db).compare("A", "600519", as_of=DAY)

    # valuation_daily.market_cap 写的是 1e12 元；快照写的是 500 亿元
    assert report.rows[0].market_cap == 500.0


def test_a_thin_third_level_widens_to_the_second_and_says_so(db):
    seed(db, "600519", "贵州茅台")
    seed(db, "600809", "山西汾酒")
    seed(db, "000568", "泸州老窖")
    seed(db, "600600", "青岛啤酒", "食品饮料-饮料-啤酒")
    seed(db, "600887", "伊利股份", "食品饮料-饮料-乳品")
    seed(db, "603288", "海天味业", "食品饮料-饮料-调味品")
    snapshots(
        db,
        {
            "600519": 500.0,
            "600809": 400.0,
            "000568": 300.0,
            "600600": 200.0,
            "600887": 100.0,
            "603288": 50.0,
        },
    )

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.level_used == "二级"
    assert any(
        "三级行业只有 2 家，已放宽到二级「食品饮料-饮料」" in note for note in report.notes
    )
    assert len(report.rows) == 6
    assert report.state == "ok"


def test_two_peers_are_listed_but_the_median_refuses(db):
    seed(db, "600519", "贵州茅台")
    seed(db, "600809", "山西汾酒")
    seed(db, "000568", "泸州老窖")
    snapshots(db, {"600519": 500.0, "600809": 400.0, "000568": 300.0})

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.state == "thin"
    assert len(report.rows) == 3
    assert report.pe.value is None
    assert report.pe.samples == 2
    assert report.pe.missing_kind == UNCOMPUTABLE
    assert "样本不足" in report.pe.reason
    assert report.pb.value is None
    assert report.roe.value is None


def test_st_stale_and_negative_multiples_are_excluded_and_counted(db):
    seed(db, "600519", "贵州茅台")
    seed(db, "600809", "山西汾酒")
    seed(db, "000568", "泸州老窖", pe=12.0, pb=3.0)
    seed(db, "600559", "老白干酒", pe=14.0, pb=4.0)
    seed(db, "600779", "水井坊", pe=-5.0, pb=-1.0, roe=0.25)  # 负数倍率不算「便宜」
    seed(db, "000596", "ST舍得")
    seed(db, "603369", "今世缘", valuation_day=DAY - timedelta(days=40))
    seed(db, "603198", "迎驾贡酒", industry=None)  # 概况与主档都没有行业
    snapshots(
        db,
        {
            "600519": 500.0,
            "600809": 400.0,
            "000568": 300.0,
            "600559": 200.0,
            "600779": 150.0,
            "000596": 140.0,
            "603369": 130.0,
            "603198": 120.0,
        },
    )

    report = service(db).compare("A", "600519", as_of=DAY)

    codes = [row.code for row in report.rows]
    assert "000596" not in codes, "ST 不进同业"
    assert "603369" not in codes, "估值行超过 30 天的票不进同业"
    assert "603198" not in codes, "没有行业就没有可比的层级"

    negative = next(row for row in report.rows if row.code == "600779")
    assert negative.pe_ttm is None and negative.pe_missing == NOT_APPLICABLE
    assert negative.pb is None and negative.pb_missing == NOT_APPLICABLE

    # 3 个为正的 PE：10 / 12 / 14 → 中位 12；负的那家只计数
    assert (report.pe.value, report.pe.samples, report.pe.excluded) == (12.0, 3, 1)
    assert (report.pb.value, report.pb.samples, report.pb.excluded) == (3.0, 3, 1)

    joined = "；".join(report.notes)
    assert "ST 1 家" in joined
    assert "估值行超过 30 天或缺失 1 家" in joined
    assert "无行业 1 家" in joined
    assert "PE 不为正 1 家（未计入中位）" in joined
    assert "PB 不为正 1 家（未计入中位）" in joined


def test_hk_is_not_covered_without_touching_any_table(db):
    report = service(db).compare("HK", "00700", as_of=DAY)

    assert report.state == "not_covered"
    assert report.rows == ()
    assert report.position_missing == NOT_COVERED
    assert all(metric.missing_kind == NOT_COVERED for metric in (report.pe, report.pb, report.roe))
    assert "只覆盖 A 股" in "；".join(report.notes)


def test_a_stock_without_any_industry_is_not_disclosed(db):
    seed(db, "600519", "贵州茅台", industry=None)

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.state == "not_disclosed"
    assert report.rows == ()
    assert report.position_missing == NOT_DISCLOSED
    # D348：网页用户没有终端，注记不印命令；概况补采状态指给同页才有三态的 F10 卡
    joined = "；".join(report.notes)
    assert "sams sync profile" not in joined
    assert "还没有行业" in joined and "公司概况" in joined


def test_missing_revenue_falls_back_to_the_market_cap_rank(db):
    seed_baijiu(db, self_revenue=None)

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.position_kind == "market_cap"
    assert "营收缺失" in report.position
    assert "第 1 位" in report.position


def test_undisclosed_periods_are_counted_in_the_notes(db):
    seed_baijiu(db)
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "600519",
                    "report_date": date(2024, 12, 31),
                    "published_date": None,
                    "roe": 0.30,
                    "revenue": 5e10,
                    "source": "test",
                }
            ]
        ).with_columns(pl.col("published_date").cast(pl.Date))
    )

    report = service(db).compare("A", "600519", as_of=DAY)

    assert "没有发布日" in "；".join(report.notes)


def test_without_a_snapshot_market_cap_is_missing_and_selection_uses_revenue(db):
    seed_baijiu(db, with_snapshot=False)

    report = service(db).compare("A", "600519", as_of=DAY)

    # 没有快照时一行都不许有市值；也不许拿 valuation_daily 的元数顶上（D239）
    assert all(row.market_cap is None for row in report.rows)
    # 注记渠道中性（D345）：快照由 run_spot 盘后全市场自动采，不印命令
    assert "sams sync spot" not in "；".join(report.notes)
    assert "盘后" in "；".join(report.notes)
    assert {row.code for row in report.rows[1:]} == {
        "600809",
        "000568",
        "000596",
        "603369",
        "603198",
    }


def test_position_without_revenue_or_cap_defers_to_cards_not_commands(db):
    """有行业、但自己营收与市值都没有：行业地位拒绝那一档不印命令（D349）。

    营收盘后只覆盖票池、服务自身承诺不了，指给同页「主要财务指标」卡；市值是 run_spot
    全市场盘后横截面，照 D345 说「盘后任务自动采集」。两条命令都不该出现在屏幕上。
    """
    seed_baijiu(db, self_revenue=None, with_snapshot=False)

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.position_missing == NOT_DISCLOSED
    assert "营收与市值都没有" in report.position
    assert "主要财务指标" in report.position
    assert "盘后任务自动采集" in report.position
    assert "sams sync financials" not in report.position
    assert "sams sync spot" not in report.position


def test_the_card_names_the_day_of_every_number_not_the_day_it_was_read(db):
    """一屏一 as-of（D288）：卡上那一句日期只能是「那个数自己的数据日」。

    读卡日（11-02）、快照日（10-30）、估值日（10-29）、年报期（2025-12-31）四个故意取不同天，
    谁被当成谁在这一条里就藏不住。
    """
    seed_baijiu(db, valuation_day=DAY - timedelta(days=1))

    report = service(db, today=datetime(2026, 11, 2, 16)).compare("A", "600519")

    assert report.as_of == date(2026, 11, 2), "读卡日仍然要给：它是这一卡的计算时间，不是数据日"
    assert report.snapshot_day == DAY, "市值那一列取的是快照那一天"
    assert report.valuation_days == (DAY - timedelta(days=1),)
    assert report.financial_periods == (REPORT,)


def test_rows_with_different_valuation_days_all_get_named(db):
    """同业那一行不是同一天的数：中位是几行不同估值日并起来的，日期得逐日列出来。"""
    seed_baijiu(
        db,
        valuation_day=DAY,
        peer_valuation_days={"600809": DAY - timedelta(days=3)},
    )

    report = service(db).compare("A", "600519", as_of=DAY)

    assert report.valuation_days == (DAY, DAY - timedelta(days=3))


def test_a_stale_snapshot_names_itself_as_absent_not_as_the_read_day(db):
    """快照超过 7 天就不出市值（D239）：那一句来源也就没有资格写一个日期。"""
    seed_baijiu(db)

    report = service(db, today=datetime(2026, 11, 20, 16)).compare("A", "600519")

    assert report.snapshot_day is None
    assert all(row.market_cap is None for row in report.rows)

