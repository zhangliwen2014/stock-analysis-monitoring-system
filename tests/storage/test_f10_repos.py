"""0027 两表 + 港股指标的展示读法：键归一、幂等、PIT 与展示的分界。

`FinancialRepo.recent` 是本轮唯一「故意绕过 PIT」的读法（D215），所以这里同时钉住
它的边界：`recent` 含 NULL 披露日的行，`load_as_of` 与 `history` 仍然不含。
"""

from __future__ import annotations

from datetime import date

import polars as pl

from sams.storage.repos.f10 import AnnouncementRepo, CompanyProfileRepo
from sams.storage.repos.financials import FinancialRepo

Q2 = date(2026, 6, 30)
Q1 = date(2026, 3, 31)


def profile(**over) -> pl.DataFrame:
    base = {
        "market": "A",
        "code": "600519",
        "name": "贵州茅台",
        "full_name": "贵州茅台酒股份有限公司",
        "industry": "食品饮料-饮料-白酒",
        "listed_on": date(2001, 8, 27),
        "founded_on": date(1999, 11, 20),
        "employees": 34992,
        "hk_connect": None,
        "summary": "简介",
        "source": "eastmoney_f10",
    }
    base.update(over)
    return pl.DataFrame([base])


def announcement(**over) -> pl.DataFrame:
    base = {
        "market": "A",
        "code": "600519",
        "art_code": "AN1",
        "published_on": date(2026, 8, 15),
        "title": "贵州茅台:公告",
        "category": "其他",
        "url": "https://data.eastmoney.com/notices/detail/600519/AN1.html",
        "source": "eastmoney_f10",
    }
    base.update(over)
    return pl.DataFrame([base])


def test_profile_upsert_is_idempotent_and_normalizes_the_key(db):
    repo = CompanyProfileRepo(db)
    frame = pl.concat(
        [
            profile(),
            profile(market="hk", code="700", name="腾讯控股", listed_on=date(2004, 6, 16)),
        ]
    )
    assert repo.upsert(frame) == 2
    assert repo.upsert(frame) == 2
    assert db.row_count("company_profiles") == 2

    got = repo.get("HK", "00700")
    assert got.height == 1
    assert got.row(0, named=True)["name"] == "腾讯控股"
    assert repo.get("hk", "700").height == 1, "小写市场名与短代码是同一只票"


def test_profile_keeps_unknown_fields_null_instead_of_zero(db):
    CompanyProfileRepo(db).upsert(profile(isin=None, lot_size=None, hk_connect=None))
    row = CompanyProfileRepo(db).get("A", "600519").row(0, named=True)
    assert row["isin"] is None
    assert row["lot_size"] is None
    assert row["hk_connect"] is None


def test_announcement_rewrite_does_not_add_a_row(db):
    repo = AnnouncementRepo(db)
    repo.upsert(announcement(title="旧标题"))
    repo.upsert(announcement(title="新标题"))
    assert db.row_count("announcements") == 1
    assert repo.latest("A", "600519").row(0, named=True)["title"] == "新标题"


def test_announcements_latest_is_newest_first_and_limited(db):
    repo = AnnouncementRepo(db)
    repo.upsert(
        pl.concat(
            [
                announcement(art_code="AN1", published_on=date(2026, 1, 1)),
                announcement(art_code="AN2", published_on=date(2026, 8, 15)),
                announcement(art_code="AN3", published_on=date(2026, 5, 1)),
            ]
        )
    )
    rows = repo.latest("A", "600519", limit=2)
    assert rows["published_on"].to_list() == [date(2026, 8, 15), date(2026, 5, 1)]


def test_announcements_without_a_title_are_dropped(db):
    repo = AnnouncementRepo(db)
    assert repo.upsert(announcement(title="   ")) == 0
    assert db.row_count("announcements") == 0


def test_recent_includes_the_rows_without_a_publication_date(db):
    fin = FinancialRepo(db)
    fin.upsert(
        pl.DataFrame(
            [
                {
                    "market": "HK",
                    "code": "00700",
                    "report_date": Q1,
                    "published_date": None,
                    "roe": 0.05,
                    "revenue_yoy": 0.09,
                    "profit_yoy": 0.21,
                    "dividend_yield": None,
                    "market_cap": None,
                    "source": "eastmoney_hk_f10",
                },
                {
                    "market": "HK",
                    "code": "00700",
                    "report_date": Q2,
                    "published_date": None,
                    "roe": 0.0996,
                    "revenue_yoy": 0.1007,
                    "profit_yoy": 0.1031,
                    "dividend_yield": None,
                    "market_cap": None,
                    "source": "eastmoney_hk_f10",
                },
            ],
            # 全 NULL 的列 polars 会推成 Null；生产侧的空帧带 Date 类型（D215）
            schema={
                "market": pl.Utf8,
                "code": pl.Utf8,
                "report_date": pl.Date,
                "published_date": pl.Date,
                "roe": pl.Float64,
                "revenue_yoy": pl.Float64,
                "profit_yoy": pl.Float64,
                "dividend_yield": pl.Float64,
                "market_cap": pl.Float64,
                "source": pl.Utf8,
            },
        )
    )
    recent = fin.recent("HK", "700", limit=8)
    assert recent["report_date"].to_list() == [Q2, Q1], "展示读法按报告期倒序，且含 NULL 披露日"
    assert recent["published_date"].to_list() == [None, None]

    # PIT 读法一行都不该看见（D61 / D215）
    assert fin.history("HK", "00700").is_empty()
    latest, _ = fin.load_as_of("HK", ["00700"], date(2026, 10, 1), basis="latest")
    assert latest.is_empty()
