"""行业回填：概况的行业落进主档的 `sector`，空答复绝不冲掉旧值（D236）。

回填只读概况、不联网，所以它的每一条分支都是「库里的两行数据如何对齐」：
概况有行业就写、没行业就留旧值、主档缺行就只计数（主档只由代码表建）。
幂等的判据是第二次执行 `changed=0`——不是「跑完了没报错」。
"""

from __future__ import annotations

import polars as pl

from sams.services.industry import IndustryService
from sams.storage.repos.f10 import CompanyProfileRepo
from sams.storage.repos.symbols import SymbolRepo


def symbols_frame(**over: dict) -> pl.DataFrame:
    base = {"market": "A", "code": "600519", "currency": "CNY", "name": "贵州茅台"}
    base.update(over)
    return pl.DataFrame([base])


def profiles_frame(**over: dict) -> pl.DataFrame:
    base = {
        "market": "A",
        "code": "600519",
        "name": "贵州茅台",
        "industry": "食品饮料-饮料-白酒",
        "source": "eastmoney_f10",
    }
    base.update(over)
    return pl.DataFrame([base])


def service(db) -> IndustryService:
    return IndustryService(CompanyProfileRepo(db), SymbolRepo(db))


def test_backfill_copies_the_profile_industry_onto_the_symbol(db):
    SymbolRepo(db).upsert(symbols_frame())
    CompanyProfileRepo(db).upsert(profiles_frame())

    report = service(db).backfill("A")

    assert (report.scanned, report.changed, report.unchanged) == (1, 1, 0)
    assert SymbolRepo(db).get("A", "600519")["sector"] == "食品饮料-饮料-白酒"


def test_a_second_run_changes_nothing(db):
    SymbolRepo(db).upsert(symbols_frame())
    CompanyProfileRepo(db).upsert(profiles_frame())
    service(db).backfill("A")

    report = service(db).backfill("A")

    assert report.changed == 0
    assert report.unchanged == 1


def test_the_industry_is_normalized_before_it_is_written(db):
    SymbolRepo(db).upsert(symbols_frame())
    CompanyProfileRepo(db).upsert(profiles_frame(industry="食品饮料／饮料／白酒"))

    service(db).backfill("A")

    assert SymbolRepo(db).get("A", "600519")["sector"] == "食品饮料-饮料-白酒"


def test_a_blank_profile_industry_keeps_the_old_sector(db):
    """概况这一期没给行业时保留主档旧值：清空会让持仓页的行业分组凭空消失。"""
    symbols = SymbolRepo(db)
    symbols.upsert(symbols_frame(sector="食品饮料-饮料-白酒"))
    CompanyProfileRepo(db).upsert(profiles_frame(industry=None))

    report = service(db).backfill("A")

    assert report.kept_stale == 1
    assert report.changed == 0
    assert symbols.get("A", "600519")["sector"] == "食品饮料-饮料-白酒"


def test_a_blank_profile_industry_without_an_old_value_is_counted_missing(db):
    SymbolRepo(db).upsert(symbols_frame())
    CompanyProfileRepo(db).upsert(profiles_frame(industry=""))

    report = service(db).backfill("A")

    assert report.missing_industry == 1
    assert report.kept_stale == 0
    assert SymbolRepo(db).get("A", "600519")["sector"] is None


def test_a_profile_without_a_symbol_row_is_counted_not_created(db):
    """主档只由代码表建：概况里多出来的一行不能悄悄长出主档。"""
    CompanyProfileRepo(db).upsert(profiles_frame(code="300750", name="宁德时代"))

    report = service(db).backfill("A")

    assert report.missing_symbol == 1
    assert SymbolRepo(db).get("A", "300750") is None


def test_other_markets_are_not_scanned(db):
    SymbolRepo(db).upsert(
        pl.concat(
            [
                symbols_frame(),
                pl.DataFrame(
                    [{"market": "HK", "code": "00700", "currency": "HKD", "name": "腾讯控股"}]
                ),
            ]
        )
    )
    CompanyProfileRepo(db).upsert(
        pl.concat(
            [
                profiles_frame(),
                pl.DataFrame(
                    [
                        {
                            "market": "HK",
                            "code": "00700",
                            "name": "腾讯控股",
                            "industry": "资讯科技-软件",
                            "source": "eastmoney_f10",
                        }
                    ]
                ),
            ]
        )
    )

    report = service(db).backfill("A")

    assert report.scanned == 1
    assert SymbolRepo(db).get("HK", "00700")["sector"] is None


def test_industry_of_reads_the_symbol_first(db):
    SymbolRepo(db).upsert(symbols_frame(sector="煤炭"))
    CompanyProfileRepo(db).upsert(profiles_frame(industry="食品饮料-饮料-白酒"))

    assert service(db).industry_of("A", "600519") == "煤炭"


def test_industry_of_falls_back_to_the_profile_and_normalizes_it(db):
    """主档还没回填时读概况；两个来源先归一，持仓页的行业分组才对得上同一只票。"""
    SymbolRepo(db).upsert(symbols_frame())
    CompanyProfileRepo(db).upsert(profiles_frame(industry="食品饮料／饮料／白酒"))

    assert service(db).industry_of("A", "600519") == "食品饮料-饮料-白酒"


def test_industry_of_returns_an_empty_string_when_both_are_missing(db):
    SymbolRepo(db).upsert(symbols_frame())

    assert service(db).industry_of("A", "600519") == ""
