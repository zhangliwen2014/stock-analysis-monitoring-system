"""选股引擎：每条件三个数、三者相加等于票池、无数出局的票带原因点名（D180）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from support_valuation import financial_rows

from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition
from sams.services.screener import ScreenerService
from sams.storage.repos.financials import FinancialRepo
from sams.storage.repos.snapshots import SnapshotRepo
from sams.storage.repos.symbol_list import SymbolListRepo

DAY = date(2026, 10, 28)
TAKEN = datetime(2026, 10, 28, 8, 41, tzinfo=UTC)


def snap(code, *, pe_dyn=10.0, pb=1.0, turnover=1.0, price=10.0, quoted=DAY):
    return {
        "code": code,
        "name": f"票{code}",
        "quoted_on": quoted,
        "price": price,
        "pct_chg": 1.0,
        "amount": 1e8,
        "turnover_rate": turnover,
        "volume_ratio": 1.0,
        "amplitude": 1.0,
        "pe_ttm": pe_dyn,
        "pe_dyn": pe_dyn,
        "pb": pb,
        "market_cap": 100.0,
        "float_cap": 90.0,
        "is_st": False,
        "source": "tencent",
    }


def seed(db, *rows, codes=("600519",)):
    from sams.datasource.spot import SPOT_SCHEMA

    SymbolListRepo(db).upsert_batch(
        "A",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "CNY"}
            for c in codes
        ],
    )
    data = [{"market": "A", "taken_at": TAKEN, **row} for row in rows]
    frame = pl.DataFrame(
        data,
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(db).replace_day(market="A", day=DAY, taken_at=TAKEN, frame=frame)


def service(db):
    return ScreenerService(SnapshotRepo(db), SymbolListRepo(db), FinancialRepo(db))


def seed_hk(db, *rows, codes=("00700",)):
    from sams.datasource.spot import SPOT_SCHEMA

    SymbolListRepo(db).upsert_batch(
        "HK",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "HKD"}
            for c in codes
        ],
    )
    data = [{"market": "HK", "taken_at": TAKEN, **row} for row in rows]
    frame = pl.DataFrame(
        data,
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(db).replace_day(market="HK", day=DAY, taken_at=TAKEN, frame=frame)


def seed_us(db, *rows, codes=("AAPL",)):
    from sams.datasource.spot import SPOT_SCHEMA

    SymbolListRepo(db).upsert_batch(
        "US",
        [
            {"code": c, "name": c, "py_initials": c, "py_full": c, "currency": "USD"}
            for c in codes
        ],
    )
    data = [{"market": "US", "taken_at": TAKEN, **row} for row in rows]
    frame = pl.DataFrame(
        data,
        schema={
            "market": pl.Utf8,
            "taken_at": pl.Datetime("us", "UTC"),
            **SPOT_SCHEMA,
        },
    )
    SnapshotRepo(db).replace_day(market="US", day=DAY, taken_at=TAKEN, frame=frame)


def test_every_condition_reports_passed_failed_missing_that_sum_to_the_universe(db):
    seed(
        db,
        snap("600519", pe_dyn=5.0, pb=1.0),   # PR = 25/100 = 0.25 通过
        snap("000001", pe_dyn=6.0, pb=1.0),   # PR = 36/100 = 0.36 通过
        snap("300750", pe_dyn=10.0, pb=1.0),  # PR = 100/100 = 1.00 不通过
        snap("600000", pe_dyn=-5.0, pb=1.0),  # 亏损 → 无数
        codes=("600519", "000001", "300750", "600000"),
    )
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    counts = result.fields[0]
    assert (counts.passed, counts.failed, counts.missing) == (2, 1, 1)
    assert counts.passed + counts.failed + counts.missing == result.universe_size == 4
    assert [hit.code for hit in result.hits] == ["600519", "000001"]


def test_hits_rank_quality_descending_and_valuation_ascending(db):
    """首批条件决定顺序（D224）：ROE 越高越靠前，PE 越低越靠前——不能一律升序。"""
    seed(
        db,
        snap("600519", pe_dyn=30.0),
        snap("000001", pe_dyn=10.0),
        codes=("600519", "000001"),
    )
    FinancialRepo(db).upsert(
        pl.concat(
            [
                financial_rows(code="600519").with_columns(pl.lit(0.20).alias("roe")),
                financial_rows(code="000001").with_columns(pl.lit(0.35).alias("roe")),
            ]
        )
    )
    roe = service(db).run("A", [Condition("roe", ">=", 0.1)])
    assert [hit.code for hit in roe.hits] == ["000001", "600519"]
    assert roe.order_note == "年报 ROE 从高到低"
    pe = service(db).run("A", [Condition("pe_ttm", ">=", 1.0)])
    assert [hit.code for hit in pe.hits] == ["000001", "600519"]
    assert pe.order_note == "市盈率(TTM) 从低到高"


def test_hits_carry_the_values_of_the_condition_that_picked_them(db):
    seed(db, snap("600519", pe_dyn=5.0, pb=1.0))
    hit = service(db).run("A", [Condition("pr_now", "<=", 0.5)]).hits[0]
    assert hit.values["pr_now"] == pytest.approx(0.25)
    assert hit.values["price"] == 10.0


def test_nothing_missing_means_no_missing_groups(db):
    seed(db, snap("600519"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.missing == () and result.fields[0].missing == 0


def test_missing_codes_are_grouped_with_a_reason_and_a_next_step(db):
    seed(db, snap("600519", pe_dyn=-5.0, pb=1.0))
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    (group,) = result.missing
    assert group.field == "pr_now" and group.codes == ("600519",)
    assert "亏损" in group.reason


def test_loss_makers_with_different_pe_share_one_group(db):
    """无数原因按**类**归组，不按每只票的数：1386 只亏损票各写一路 PE 值，页面就成了 1386 组。"""
    seed(
        db,
        snap("600519", pe_dyn=5.0, pb=1.0),
        snap("000001", pe_dyn=-8.0, pb=1.0),
        snap("300750", pe_dyn=-2.0, pb=1.0),
        snap("600000", pe_dyn=-40.0, pb=1.0),
        snap("600476", pe_dyn=12.0, pb=-3.82),
        codes=("600519", "000001", "300750", "600000", "600476"),
    )
    result = service(db).run("A", [Condition("pr_now", "<=", 0.5)])
    by_reason = {group.reason: group.codes for group in result.missing}
    assert len(by_reason) == 2
    assert by_reason["亏损（PE(动) ≤ 0）"] == ("000001", "300750", "600000")
    assert by_reason["净资产为负（PB ≤ 0）"] == ("600476",)
    assert result.fields[0].missing == 4


def test_a_negative_valuation_is_missing_not_cheap_by_default(db):
    """R-09：`PE(TTM) ≤ 8` 的首行不该是 -72980.5。

    负 PE / 负 PB 不是「更便宜」，是亏损或净资产为负。默认把它记成「无数」并说清
    开关在哪；否则一条估值条件会先把最贵的那类公司排到最前面。
    """
    seed(
        db,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
        snap("300750", pe_dyn=30.0),
        codes=("600519", "000001", "300750"),
    )
    result = service(db).run("A", [Condition("pe_ttm", "<=", 8)])
    assert [hit.code for hit in result.hits] == ["600519"]
    counts = result.fields[0]
    assert (counts.passed, counts.failed, counts.missing) == (1, 1, 1)
    assert counts.nonpositive == 1
    (group,) = result.missing
    assert group.field == "pe_ttm" and group.codes == ("000001",)
    assert "估值不为正" in group.reason


def test_the_nonpositive_reason_does_not_choose_a_surface(db):
    """同一句原因同时上网页与命令行：它只讲事实，开关由两边各自指（D283/T1a）。

    网页的下一条是勾选框、命令行的下一条是 `--include-negatives`；共享串里写死其中
    一个，另一边就在说另一个房间的东西——R3-02 报的正是这一句。
    """
    seed(
        db,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
        codes=("600519", "000001"),
    )
    (group,) = service(db).run("A", [Condition("pe_ttm", "<=", 8)]).missing
    assert "估值不为正" in group.reason
    assert "命令行" not in group.reason
    assert "--include-negatives" not in group.reason
    assert "勾选" not in group.reason


def test_negative_valuations_can_be_included_on_request(db):
    seed(
        db,
        snap("600519", pe_dyn=5.0),
        snap("000001", pe_dyn=-72980.5),
        snap("300750", pe_dyn=30.0),
        codes=("600519", "000001", "300750"),
    )
    result = service(db).run(
        "A", [Condition("pe_ttm", "<=", 8)], positive_only=False
    )
    assert [hit.code for hit in result.hits] == ["000001", "600519"]
    assert result.fields[0].nonpositive == 0
    assert result.missing == ()


def test_st_and_delisting_names_sit_out_by_default_and_are_counted(db):
    """R-09：ST 只靠名字判断，且默认不该出现在首屏；剔除的人数必须说出来。"""
    seed(
        db,
        snap("600519"),
        {**snap("600001"), "name": "*ST海医", "is_st": True},
        {**snap("600002"), "name": "退市海医", "is_st": False},
        codes=("600519", "600001", "600002"),
    )
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.st_excluded == 2
    assert result.universe_size == 1
    assert [hit.code for hit in result.hits] == ["600519"]

    included = service(db).run("A", [Condition("price", ">=", 1.0)], include_st=True)
    assert included.st_excluded == 0
    assert included.universe_size == 3
    assert {hit.code for hit in included.hits} == {"600519", "600001", "600002"}


def test_financial_fields_join_as_of_the_snapshot_day_and_report_the_gap(db):
    seed(db, snap("600519"), snap("000001"), codes=("600519", "000001"))
    # support_valuation.financial_rows 的签名是 (*, market="A", code=CODE)，FY23 的 ROE=0.20
    FinancialRepo(db).upsert(financial_rows(code="600519"))
    result = service(db).run("A", [Condition("roe", ">=", 0.1)])
    assert result.fields[0].passed == 1
    (group,) = result.missing
    assert group.codes == ("000001",) and "financials" in group.reason


def test_one_missing_financial_field_is_one_group_not_one_per_code(db):
    """真实库里几千只票没有年报行：逐票把代码写进理由，页面就散成几千组。"""
    seed(
        db,
        snap("600519"),
        snap("000001"),
        snap("300750"),
        codes=("600519", "000001", "300750"),
    )
    FinancialRepo(db).upsert(financial_rows(code="600519"))
    result = service(db).run("A", [Condition("roe", ">=", 0.1)])
    (group,) = result.missing
    assert group.codes == ("000001", "300750")
    assert "--code <代码>" in group.reason
    assert "000001" not in group.reason and "300750" not in group.reason


def financial(db, code, **values):
    """一期年报的最小行：没给的列按可选留空，与适配器缺列时的处理一致。"""
    row = {
        "market": "A",
        "code": code,
        "report_date": date(2025, 12, 31),
        "published_date": date(2026, 3, 20),
        "source": "akshare",
    } | values
    # 全员 NULL 的披露日会被 polars 推成 Null 列；真实入库走 empty_financials 的
    # schema，那一列始终是 Date（D215），这里跟上
    FinancialRepo(db).upsert(
        pl.DataFrame([row], schema_overrides={"published_date": pl.Date})
    )


def test_annual_quality_fields_are_joined_at_the_snapshot_day(db):
    seed(db, snap("600519"), snap("000001"), codes=("600519", "000001"))
    financial(
        db,
        "600519",
        roe=0.20,
        gross_margin=0.50,
        net_margin=0.25,
        roa=0.12,
        roic=0.18,
        debt_ratio=0.30,
        current_ratio=2.0,
        dividend_yield=0.04,
    )
    result = service(db).run(
        "A",
        [
            Condition("gross_margin", ">=", 0.40),
            Condition("debt_ratio", "<=", 0.50),
            Condition("dividend_yield", ">=", 0.03),
        ],
    )
    assert [hit.code for hit in result.hits] == ["600519"]
    assert result.hits[0].values["gross_margin"] == pytest.approx(0.50)
    (group,) = result.missing
    assert group.codes == ("000001",) and "毛利率" in group.reason


def test_peg_is_pe_divided_by_the_growth_percent_and_needs_the_financial_join(db):
    """PEG 只选了推导列，引擎也必须把年报净利同比带进来——否则每一行都是无数。"""
    seed(
        db,
        snap("600519", pe_dyn=20.0),
        snap("000001", pe_dyn=10.0),
        codes=("600519", "000001"),
    )
    financial(db, "600519", profit_yoy=0.20)
    financial(db, "000001", profit_yoy=-0.10)
    result = service(db).run("A", [Condition("peg", "<=", 1.0)])
    assert result.fields[0].passed == 1 and result.fields[0].missing == 1
    (hit,) = result.hits
    assert hit.code == "600519" and hit.values["peg"] == pytest.approx(1.0)
    (group,) = result.missing
    assert group.field == "peg" and group.codes == ("000001",)
    assert "PEG 没有意义" in group.reason


def test_hk_peg_reads_the_latest_annual_report_even_without_a_disclosure_date(db):
    """港股指标源不给披露日（D215）：PEG 的净利同比只能按报告期取最新年报。

    这只票的财务行 `published_date` 是 NULL——默认的 PIT 读法一行都看不见。港股
    快照有 PE(TTM)（腾讯 57 格），所以这一条能算出数，而不是整列「无数」。
    """
    seed_hk(db, snap("00700", pe_dyn=12.8))
    financial(db, "00700", market="HK", published_date=None, profit_yoy=0.1585)
    result = service(db).run("HK", [Condition("peg", "<=", 1.0)])
    assert result.fields[0].passed == 1 and result.fields[0].missing == 0
    (hit,) = result.hits
    assert hit.code == "00700"
    assert hit.values["peg"] == pytest.approx(12.8 / (0.1585 * 100))
    assert result.order_note == "PEG（PE÷净利增速） 从低到高"
    assert "报告期 ≤ 快照日" in result.basis_note


def test_the_undated_caliber_note_appears_only_when_the_condition_reads_an_annual(
    db,
):
    """放宽的是「哪一期年报」；只用快照列的结果不该背这条口径说明。

    A 股更不该出现：它的年报仍按已披露日挡，屏幕说「大概按报告期取」就是另一句谎话。
    """
    seed(db, snap("600519", pe_dyn=10.0))
    financial(db, "600519", roe=0.20)
    assert service(db).run("A", [Condition("pe_ttm", "<=", 20)]).basis_note == ""
    assert service(db).run("A", [Condition("roe", ">=", 0.10)]).basis_note == ""

    seed_hk(db, snap("00700", pe_dyn=12.8))
    pe_only = service(db).run("HK", [Condition("pe_ttm", "<=", 20)])
    assert pe_only.basis_note == ""
    assert service(db).run("HK", [Condition("peg", "<=", 1.0)]).basis_note != ""

    seed_us(db, snap("AAPL", pe_dyn=30.0))
    assert service(db).run("US", [Condition("pe_ttm", "<=", 40)]).basis_note == ""
    assert service(db).run("US", [Condition("roe", ">=", 0.10)]).basis_note != ""


def test_hk_peg_without_an_annual_report_is_missing_not_zero(db):
    seed_hk(db, snap("00700", pe_dyn=12.8))
    result = service(db).run("HK", [Condition("peg", "<=", 1.0)])
    assert result.fields[0].missing == 1 and result.hits == ()
    assert result.missing[0].field == "peg" and "PEG 没有意义" in result.missing[0].reason


def test_hk_roe_is_derived_from_roa_and_debt_ratio_when_the_source_omits_it(db):
    """港股源没给 ROE 时，同一期年报的 ROA ÷ (1−资产负债率) 就是股东回报的近似（D228）。"""
    seed_hk(db, snap("00700", pe_dyn=12.8))
    financial(db, "00700", market="HK", published_date=None, roa=0.06, debt_ratio=0.60)
    result = service(db).run("HK", [Condition("roe", ">=", 0.15)])
    assert result.fields[0].passed == 1 and result.fields[0].missing == 0
    (hit,) = result.hits
    assert hit.values["roe"] == pytest.approx(0.15)
    assert "ROA ÷ (1−资产负债率)" in result.basis_note
    assert "港股" in result.basis_note


def test_us_roe_is_derived_from_a_fiscal_year_annual(db):
    """美股财年可以 9 月末结账（D228）：兜底公式读的是 report_type 认出的那份年报。"""
    seed_us(db, snap("AAPL", pe_dyn=30.0))
    financial(
        db,
        "AAPL",
        market="US",
        report_date=date(2025, 9, 27),
        published_date=None,
        report_type="年报",
        roa=0.20,
        debt_ratio=0.75,
    )
    result = service(db).run("US", [Condition("roe", ">=", 0.50)])
    (hit,) = result.hits
    assert hit.values["roe"] == pytest.approx(0.80)
    assert "美股" in result.basis_note and "财年" in result.basis_note
    assert "ROA ÷ (1−资产负债率)" in result.basis_note


def test_a_source_roe_wins_over_the_derived_estimate(db):
    """源给了数就用源的：估算是缺口兜底，不是对源值的第二次解释（D228）。"""
    seed_hk(db, snap("00700", pe_dyn=12.8))
    financial(
        db, "00700", market="HK", published_date=None, roe=0.25, roa=0.06, debt_ratio=0.60
    )
    result = service(db).run("HK", [Condition("roe", ">=", 0.20)])
    (hit,) = result.hits
    assert hit.values["roe"] == pytest.approx(0.25)
    assert "估算" not in result.basis_note


def test_a_debt_ratio_at_or_above_one_refuses_the_derivation_and_says_why(db):
    """负债 ≥ 总资产时 (1−资产负债率) ≤ 0：公式没有意义，这一格必须是「无数」而不是负数。"""
    seed_hk(db, snap("00700", pe_dyn=12.8))
    financial(db, "00700", market="HK", published_date=None, roa=0.06, debt_ratio=1.20)
    result = service(db).run("HK", [Condition("roe", ">=", 0.10)])
    assert result.hits == () and result.fields[0].missing == 1
    (group,) = result.missing
    assert group.field == "roe"
    assert "ROA ÷ (1−资产负债率)" in group.reason and "资产负债率" in group.reason


def test_a_derived_roe_that_breaks_the_caliber_gate_is_missing_not_cheap(db):
    """ROA 0.90 ÷ (1−0.50) = 1.80：小数口径下 ROE ≥ 100% 本身就不可信（D66）。"""
    seed_hk(db, snap("00700", pe_dyn=12.8))
    financial(db, "00700", market="HK", published_date=None, roa=0.90, debt_ratio=0.50)
    result = service(db).run("HK", [Condition("roe", ">=", 0.10)])
    assert result.hits == () and result.fields[0].missing == 1
    assert "估不出" in result.missing[0].reason


def test_the_derivation_is_not_an_hk_only_shortcut(db):
    """A 股源偶发缺 ROE 时同一个兜底也成立：它有同一期年报的 ROA 与资产负债率。"""
    seed(db, snap("600519"))
    financial(db, "600519", roa=0.08, debt_ratio=0.50)
    result = service(db).run("A", [Condition("roe", ">=", 0.15)])
    (hit,) = result.hits
    assert hit.values["roe"] == pytest.approx(0.16)


def test_us_annual_fields_are_read_undated_and_the_note_names_the_fiscal_year(db):
    """美股的季报不能冒充年报，且口径说明要写出「财年可以不是 12-31」（D228）。"""
    seed_us(db, snap("AAPL", pe_dyn=30.0))
    financial(
        db,
        "AAPL",
        market="US",
        report_date=date(2025, 9, 27),
        published_date=None,
        report_type="年报",
        gross_margin=0.46,
    )
    financial(
        db,
        "AAPL",
        market="US",
        report_date=date(2026, 6, 27),
        published_date=None,
        report_type="单季报",
        gross_margin=0.90,
    )
    result = service(db).run("US", [Condition("gross_margin", ">=", 0.40)])
    (hit,) = result.hits
    assert hit.values["gross_margin"] == pytest.approx(0.46)
    assert "美股财务源没有可核对的披露日" in result.basis_note
    assert "财年" in result.basis_note


def test_the_missing_annual_reason_matches_the_markets_disclosure_rule(db):
    """美股没有披露日可核，理由不能说成「没有已披露年报」——那句话在这里无从验证。"""
    seed_us(db, snap("AAPL"), snap("MSFT"), codes=("AAPL", "MSFT"))
    result = service(db).run("US", [Condition("roe", ">=", 0.10)])
    (group,) = result.missing
    assert group.codes == ("AAPL", "MSFT")
    assert "截至快照日" in group.reason
    assert "已披露" not in group.reason
    assert "--market US --code <代码>" in group.reason


def test_hk_missing_annual_reason_does_not_claim_a_disclosure_date(db):
    seed_hk(db, snap("00700", pe_dyn=12.8))
    result = service(db).run("HK", [Condition("roe", ">=", 0.10)])
    (group,) = result.missing
    assert "截至快照日" in group.reason and "已披露" not in group.reason


def test_annual_coverage_counts_the_pool_with_a_report_as_of_the_snapshot_day(db):
    """「命中 1 只」得和「覆盖 1 / 3 只」一起读（D227）：只同步了 1 只年报，
    不等于另外 2 只不便宜。"""
    seed_hk(
        db,
        snap("00700", pe_dyn=12.8),
        snap("01021", pe_dyn=8.0),
        snap("03690", pe_dyn=20.0),
        codes=("00700", "01021", "03690"),
    )
    financial(db, "00700", market="HK", published_date=None, profit_yoy=0.1585)
    # 报告期在快照日之后的那一期还没发生：不算覆盖
    financial(
        db,
        "01021",
        market="HK",
        report_date=date(2026, 12, 31),
        published_date=None,
        profit_yoy=0.10,
    )
    result = service(db).run("HK", [Condition("peg", "<=", 1.0)])
    assert result.financial_coverage == (1, 3)


def test_coverage_is_absent_until_a_condition_reads_an_annual_report(db):
    seed(db, snap("600519"), snap("000001"), codes=("600519", "000001"))
    financial(db, "600519", roe=0.20)
    pe_only = service(db).run("A", [Condition("pe_ttm", "<=", 20)])
    assert pe_only.financial_coverage is None
    assert service(db).run("A", [Condition("roe", ">=", 0.10)]).financial_coverage == (1, 2)


def test_quality_fields_take_the_latest_annual_report_not_a_quarter(db):
    """PIT + 年报口径：季报的毛利率不能冒充年报，否则「年报 ROE/毛利率」就是一句假话（D62）。"""
    seed(db, snap("600519"))
    financial(db, "600519", report_date=date(2025, 12, 31), gross_margin=0.30)
    financial(
        db,
        "600519",
        report_date=date(2026, 3, 31),
        published_date=date(2026, 4, 30),
        gross_margin=0.90,
    )
    result = service(db).run("A", [Condition("gross_margin", "between", (0.29, 0.31))])
    assert result.fields[0].passed == 1
    assert result.hits[0].values["gross_margin"] == pytest.approx(0.30)


def test_a_quality_number_published_after_the_snapshot_day_is_missing_not_guessed(db):
    seed(db, snap("600519"))
    financial(
        db,
        "600519",
        published_date=date(2026, 11, 1),
        gross_margin=0.50,
    )
    result = service(db).run("A", [Condition("gross_margin", ">=", 0.40)])
    assert result.fields[0].missing == 1 and not result.hits


def test_snapshot_only_and_list_only_are_two_separate_numbers(db):
    """D174：快照里多出来的、代码表里缺的，两个数分开报，不合并成一个「有效票池」。"""
    seed(db, snap("600519"), snap("999999"), codes=("600519", "000001"))
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.snapshot_only == 1  # 999999 不在代码表
    assert result.list_only == 1      # 000001 没有快照
    assert result.universe_size == 1


def test_no_snapshot_returns_a_named_empty_result_not_an_exception(db):
    result = service(db).run("A", [Condition("price", ">=", 1.0)])
    assert result.universe_size == 0 and result.day is None
    assert "sams sync spot" in result.note


def test_limit_truncates_but_the_total_stays_visible(db):
    seed(
        db,
        *(snap(c) for c in ("600519", "000001", "300750")),
        codes=("600519", "000001", "300750"),
    )
    result = service(db).run("A", [Condition("price", ">=", 1.0)], limit=2)
    assert len(result.hits) == 2 and result.total_hits == 3 and result.truncated == 1


def test_validation_happens_before_the_database_is_read(db):
    """词表不认识的字段在碰库之前就拒：否则拼错的字段会被读成「没有票通过」。"""
    with pytest.raises(ValidationError):
        service(db).run("A", [Condition("unknown", "<=", 0.5)])

def test_annual_pr_is_pe_ttm_over_the_annual_roe_and_ands_with_other_conditions(db):
    """D335：网页能写「年报口径 PR ≤ 0.5 且资产负债率 ≤ 0.6」。

    PR = PE(TTM) ÷ 年报 ROE ÷ 100：20 × 0.20 → 1.0，5 × 0.25 → 0.2。
    """
    seed(
        db,
        snap("600519", pe_dyn=20.0),
        snap("000001", pe_dyn=10.0),
        snap("300750", pe_dyn=5.0),
        codes=("600519", "000001", "300750"),
    )
    financial(db, "600519", roe=0.20, debt_ratio=0.30)
    financial(db, "000001", roe=0.25, debt_ratio=0.70)
    financial(db, "300750", roe=0.25, debt_ratio=0.40)
    result = service(db).run(
        "A",
        [Condition("pr_annual", "<=", 0.5), Condition("debt_ratio", "<=", 0.6)],
    )
    assert [hit.code for hit in result.hits] == ["300750"]
    assert result.hits[0].values["pr_annual"] == pytest.approx(0.2)
    assert result.hits[0].values["debt_ratio"] == pytest.approx(0.40)
    pr_counts, debt_counts = result.fields
    assert (pr_counts.passed, pr_counts.failed, pr_counts.missing) == (2, 1, 0)
    assert (debt_counts.passed, debt_counts.failed, debt_counts.missing) == (2, 1, 0)
    assert result.order_note == "年报市赚率 PR 从低到高"


def test_annual_pr_does_not_fall_through_to_the_pb_branch_when_pe_is_not_positive(db):
    """D59：网页结果表只有一个排序列，两支公式的 PR 混排不可比。

    PE 为负时命令行默认也把这只票排除（`--include-pb-branch` 才进来），网页沿用同一条规矩。
    """
    seed(db, snap("600519", pe_dyn=-5.0, pb=1.0))
    financial(db, "600519", roe=0.20)
    result = service(db).run("A", [Condition("pr_annual", "<=", 0.5)])
    assert result.hits == () and result.fields[0].missing == 1
    (group,) = result.missing
    assert group.field == "pr_annual" and group.codes == ("600519",)
    assert "PE(TTM) 不为正" in group.reason and "D59" in group.reason


def test_annual_pr_negative_roe_is_missing_and_grouped_by_kind(db):
    """拒绝理由带着自己的数，分组键不能也带：两只亏损票要落在同一组（D180）。"""
    seed(db, snap("600519"), snap("000001"), codes=("600519", "000001"))
    financial(db, "600519", roe=-0.10)
    financial(db, "000001", roe=-0.25)
    result = service(db).run("A", [Condition("pr_annual", "<=", 0.5)])
    assert result.fields[0].missing == 2
    (group,) = result.missing
    assert group.codes == ("000001", "600519")
    assert "ROE 不为正" in group.reason


def test_annual_pr_without_an_annual_report_names_the_next_sync(db):
    seed(db, snap("600519"))
    result = service(db).run("A", [Condition("pr_annual", "<=", 0.5)])
    (group,) = result.missing
    assert group.field == "pr_annual"
    assert "sams sync financials --market A --code <代码>" in group.reason


def test_annual_pr_derives_the_roe_when_the_source_omits_it(db):
    """与 `roe` 字段同一条兜底（D228）：PR 的分母也可以是估出来的那一期。"""
    seed_hk(db, snap("00700", pe_dyn=12.8))
    financial(db, "00700", market="HK", published_date=None, roa=0.09, debt_ratio=0.40)
    result = service(db).run("HK", [Condition("pr_annual", "<=", 1.0)])
    (hit,) = result.hits
    assert hit.values["pr_annual"] == pytest.approx(12.8 / 0.15 / 100)
    assert "ROA ÷ (1−资产负债率)" in result.basis_note


def test_us_annual_pr_uses_the_market_caliber_bound(db):
    """美股 ROE 142% 是常态（D228/D334）：PR 的分母不能撞 A 股 100% 的上界。"""
    seed_us(db, snap("AAPL", pe_dyn=20.0))
    financial(
        db,
        "AAPL",
        market="US",
        report_date=date(2025, 9, 27),
        published_date=None,
        report_type="年报",
        roe=1.714,
    )
    result = service(db).run("US", [Condition("pr_annual", "<=", 0.5)])
    (hit,) = result.hits
    assert hit.values["pr_annual"] == pytest.approx(20 / 1.714 / 100)


def test_preset_strategy_id_moves_only_when_the_conditions_move():
    """同名方案不再共用一个键（D257）：跨用户的「便宜」不会互相覆盖。"""
    from sams.services.screener import preset_strategy_id

    base = [Condition("pr_now", "<=", 0.5)]
    assert preset_strategy_id("p1", base) == preset_strategy_id(
        "p1", [Condition("pr_now", "<=", 0.5)]
    )
    assert preset_strategy_id("p1", base) != preset_strategy_id(
        "p1", [Condition("pr_now", "<=", 0.4)]
    )
    assert preset_strategy_id("p1", base) != preset_strategy_id("p2", base)
    assert preset_strategy_id("p1", base).startswith("preset:p1:")
