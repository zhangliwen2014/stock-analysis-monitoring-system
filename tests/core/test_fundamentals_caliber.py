"""基本面口径词表与库里那三张表必须说的是同一种话（D58 / D60）。

这一层唯一的价值是「只有一份」：采集侧、仓库侧、算 PR 的那一侧都从这里取列名与单位。
所以这里测的不是函数行为，而是**这份词表与真实 schema 是否仍然对齐**——
它一旦漂移，症状不是报错而是某个数被静默写进另一列。
"""

from __future__ import annotations

from datetime import date

from sams.core.fundamentals import (
    FINANCIAL_COLUMNS,
    FLOW_COLUMNS,
    MONEY_COLUMNS,
    MULTIPLE_COLUMNS,
    PER_SHARE_COLUMNS,
    RATIO_COLUMNS,
    TURNOVER_COLUMNS,
    VALUATION_COLUMNS,
    caliber_note,
    is_year_end,
)


def columns_of(db, table: str) -> set[str]:
    return set(db.query(f"DESCRIBE {table}")["column_name"].to_list())


def test_a_period_is_the_annual_one_only_on_december_31():
    """「12-31 那一报告期」这件事只有一个拼写（D62）。

    两个命令行出口各自判一次时，改口径（比如哪天要开始认「12 月财务年度末」这种非自然
    年末）只会改到一处，另一处继续把季报叫成年报——而 `--roe-basis annual` 那句话
    正是打印出来的那两个字。
    """
    assert is_year_end(date(2023, 12, 31)) is True
    assert is_year_end(date(2024, 3, 31)) is False
    assert is_year_end(date(2024, 12, 30)) is False
    assert is_year_end(None) is False, "没有报告期不是「年末」，而是「不知道」"


def test_the_three_unit_families_do_not_overlap():
    """一列只能属于一个家族。同时属于「比率」和「倍数」的列会被 ÷100 两次或零次。"""
    families = [
        set(RATIO_COLUMNS),
        set(MULTIPLE_COLUMNS),
        set(MONEY_COLUMNS),
        set(PER_SHARE_COLUMNS),
        set(TURNOVER_COLUMNS),
    ]
    for i, left in enumerate(families):
        for right in families[i + 1 :]:
            assert not (left & right), f"{left & right} 同时被归进两个单位家族"


def test_every_value_column_is_classified():
    """没归类的列 = 没人负责它的单位。今天不问，明天就有人按自己的猜想念它。"""
    keys = {
        "market",
        "code",
        "date",
        "report_date",
        "published_date",
        "source",
        "report_type",
    }
    values = (set(FINANCIAL_COLUMNS) | set(VALUATION_COLUMNS) | set(FLOW_COLUMNS)) - keys
    covered = (
        set(RATIO_COLUMNS)
        | set(MULTIPLE_COLUMNS)
        | set(MONEY_COLUMNS)
        | set(PER_SHARE_COLUMNS)
        | set(TURNOVER_COLUMNS)
    )
    assert values - covered == set(), f"这些列没有单位归属: {sorted(values - covered)}"


def test_financials_does_not_carry_the_multiples(db):
    """D60：`financials.pe_ttm/pb` 在 DDL 里存在，但这份词表不写它们。

    报告期那行里放一个「某一天的 PE」，读的人就分不清这是期口的数还是日口的数；
    市赚率要的「今天的 PE」只能来自 valuation_daily。这条断言防的是有人顺手把
    那两列加回词表——加回来不会报错，只会让 PR 用上一个月的 PE 而屏幕上看不出来。
    """
    assert "pe_ttm" not in FINANCIAL_COLUMNS
    assert "pb" not in FINANCIAL_COLUMNS
    assert {"pe_ttm", "pb"} <= columns_of(db, "valuation_daily")


def test_the_vocabulary_matches_the_schema(db):
    """词表里的每一列都必须真在表里：写错一个名字，那一列就永远没人填。"""
    assert set(FINANCIAL_COLUMNS) <= columns_of(db, "financials")
    assert set(VALUATION_COLUMNS) <= columns_of(db, "valuation_daily")
    assert set(FLOW_COLUMNS) <= columns_of(db, "funding_flow")


def test_the_caliber_line_states_the_unit_and_the_two_grains():
    """这句要能被打印，所以它得自己说清「小数」和「两个粒度」这两件事（D54）。"""
    note = caliber_note()
    assert "0.20 = 20%" in note and "报告期" in note and "交易日" in note
