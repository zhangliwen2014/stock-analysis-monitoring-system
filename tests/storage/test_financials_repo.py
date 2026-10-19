"""基本面三张表的仓库层：PIT、单位、键形态（D58 / D60 / D61 / D50）。"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from sams.core.errors import StorageError
from sams.storage.repos.financials import (
    FinancialRepo,
    FundingFlowRepo,
    ValuationRepo,
)
from sams.storage.repos.scan_results import ScanResultRepo, hits_frame

Q1 = date(2024, 3, 31)
Q4 = date(2023, 12, 31)
PUBLISHED_Q1 = date(2024, 4, 26)
PUBLISHED_Q4 = date(2024, 3, 20)


def row(**over) -> dict:
    base = {
        "market": "A",
        "code": "601088",
        "report_date": Q1,
        "published_date": PUBLISHED_Q1,
        "roe": 0.20,
        "revenue_yoy": 0.12,
        "profit_yoy": 0.08,
        "dividend_yield": 0.042,
        "market_cap": 4.6e11,
        "source": "akshare",
    }
    base.update(over)
    return base


@pytest.fixture
def fin(db) -> FinancialRepo:
    return FinancialRepo(db)


def test_upsert_is_idempotent(fin, db):
    frame = pl.DataFrame([row()])
    assert fin.upsert(frame) == 1
    assert fin.upsert(frame) == 1
    assert db.row_count("financials") == 1, "同一期报表重跑不能长出两行"


def test_a_period_published_after_the_as_of_date_is_not_visible(fin):
    """反未来函数专项：2024Q1 的 ROE 在 4 月 26 日之前不存在，哪怕行已经在库里。

    少了这道闸，任何「按最新一期 ROE 选股」的回测都会在 3 月就用上 4 月才公开的数字，
    结论好看得没有意义——而这在结果上完全看不出来。
    """
    fin.upsert(pl.DataFrame([row()]))
    latest, dropped = fin.load_as_of("A", ["601088"], date(2024, 4, 25), basis="latest")
    assert latest.height == 0
    assert dropped == 0

    adopted, _ = fin.load_as_of("A", ["601088"], PUBLISHED_Q1, basis="latest")
    assert adopted["roe"].to_list() == [0.20]


def test_the_latest_already_published_period_wins(fin):
    fin.upsert(pl.DataFrame([row(), row(report_date=Q4, published_date=PUBLISHED_Q4)]))
    latest, _ = fin.load_as_of("A", ["601088"], date(2024, 4, 30), basis="latest")
    assert latest["report_date"].to_list() == [Q1]
    # 更早的截至日：2024Q1 还没公开，能采信的只有 2023Q4
    earlier, _ = fin.load_as_of("A", ["601088"], date(2024, 3, 25), basis="latest")
    assert earlier["report_date"].to_list() == [Q4]


def test_a_period_without_a_publication_date_is_not_trusted_and_is_counted(fin):
    """D61：披露日为 NULL 有两种含义（源没给 / 真没披露），仓库层一种都不认。

    计数交回调用方是这条的一部分——「最新一期是 2023Q4」与「最新一期是 2023Q4，
    另有 1 期因为我们不知道它何时公开而未被采信」是两个不同的结论。
    """
    fin.upsert(
        pl.DataFrame([row(), row(report_date=date(2024, 6, 30), published_date=None)])
    )
    latest, dropped = fin.load_as_of("A", ["601088"], date(2024, 12, 31), basis="latest")
    assert latest["report_date"].to_list() == [Q1]
    assert dropped == 1


def test_synced_codes_names_every_code_with_a_row_even_with_no_publication(fin):
    """选股的 universe 问的是「库里同步过谁」，不是「谁已披露」——两者差的那些票
    必须在屏幕上被点名（D64 + D66），而不是从「已同步基本面的 N 只」那句话里静默消失。
    """
    fin.upsert(
        pl.DataFrame([row(code="600001", published_date=None), row(code="600002")])
    )
    assert fin.synced_codes("A") == ["600001", "600002"]
    adopted, _ = fin.load_as_of("A", ["600001", "600002"], date(2024, 4, 30), basis="latest")
    assert adopted["code"].to_list() == ["600002"], (
        "同两行数据，两个问法给两个数：差的那一只是「同步了但我们不知道它何时公开」"
    )


# ---------- 年报口径（D62）：PR 的分母要与 PE(TTM) 同为一年 ----------

FY_2023 = date(2023, 12, 31)
PUBLISHED_FY_2023 = date(2024, 3, 20)
H1_2024 = date(2024, 6, 30)
PUBLISHED_H1_2024 = date(2024, 8, 29)


def test_the_annual_basis_is_the_default_and_skips_interim_periods(fin):
    """源的 ROE 是**该期累计**（Q1 是三个月、H1 是六个月），PE 却是滚动十二个月。

    默认按最新已披露一期时，601088 在 2026-04-30 给 PR=8.41、2026-10-18 给 3.51，
    而它的年报口径只有 ~1.6：那个数主要在读「最近报的是哪个季度」，不是读贵不贵。
    """
    fin.upsert(
        pl.DataFrame(
            [
                row(report_date=Q1, published_date=PUBLISHED_Q1, roe=0.024),
                row(report_date=FY_2023, published_date=PUBLISHED_FY_2023, roe=0.19),
            ]
        )
    )
    adopted, _ = fin.load_as_of("A", ["601088"], date(2024, 4, 30))
    assert adopted["report_date"].to_list() == [FY_2023]
    assert adopted["roe"].to_list() == [0.19]

    newest, _ = fin.load_as_of("A", ["601088"], date(2024, 4, 30), basis="latest")
    assert newest["report_date"].to_list() == [Q1], "口径是调用方选的，仓库不替它决定"


def test_annual_means_the_report_date_is_a_year_end(fin):
    """认报告期的日历日，不认名字：`2024-06-30` 是半年报，`2024-12-31` 才是年报。"""
    fin.upsert(
        pl.DataFrame(
            [
                row(report_date=H1_2024, published_date=PUBLISHED_H1_2024, roe=0.058),
                row(report_date=date(2024, 12, 31), published_date=date(2025, 3, 28), roe=0.13),
            ]
        )
    )
    adopted, _ = fin.load_as_of("A", ["601088"], date(2025, 4, 1))
    assert adopted["report_date"].to_list() == [date(2024, 12, 31)]


def test_publication_state_distinguishes_no_date_from_a_later_date(fin):
    """「没有可用期」有两种，下一步命令不同：一句排除要说得出是哪种（D61 + D64）。"""
    fin.upsert(
        pl.DataFrame(
            [
                row(code="600001", published_date=None),
                row(code="600002", report_date=Q4, published_date=date(2024, 3, 20)),
            ]
        )
    )
    state = fin.publication_state("A", ["600001", "600002"], date(2024, 3, 19)).to_dicts()
    assert [r["code"] for r in state] == ["600001", "600002"]
    by_code = {r["code"]: r for r in state}
    assert by_code["600001"]["unpublished"] == 1
    assert by_code["600001"]["next_published"] is None
    assert by_code["600002"]["unpublished"] == 0
    assert by_code["600002"]["next_published"] == date(2024, 3, 20), "那天之前它还不存在"
    # 已披露的那期不再报「等待公开」
    later = fin.publication_state("A", ["600002"], date(2024, 4, 1)).to_dicts()
    assert later[0]["next_published"] is None


def test_a_code_with_only_interim_periods_is_not_covered_on_the_annual_basis(fin):
    """只有季报的票在年报口径下没有可用的一期：它进不了结果，但必须被点名（D64）。"""
    fin.upsert(pl.DataFrame([row()]))
    assert fin.load_as_of("A", ["601088"], date(2024, 4, 30))[0].height == 0
    assert fin.synced_codes("A") == ["601088"], "「没可用期」不等于「没同步」"


def test_an_unknown_basis_is_refused_with_the_two_names(fin):
    with pytest.raises(Exception, match="annual|latest"):
        fin.load_as_of("A", ["601088"], date(2024, 4, 30), basis="quarterly")



def test_a_percent_caliber_ratio_is_emptied_not_stored(fin, db):
    """ROE 20.0 在小数口径里不是「601088 的 ROE」，是「有人忘了 ÷100」（D58）。

    置空那一列、留下那一行：留着那个数，PR 会短两个数量级并把这只票排进低 PR 前列；
    置空则 PR 拒绝出数。静默存下来才是最坏的一种。
    """
    fin.upsert(pl.DataFrame([row(roe=20.0), row(report_date=Q4, roe=0.19)]))
    stored = db.query("SELECT roe FROM financials WHERE report_date = ?", [Q1])
    assert stored["roe"].to_list() == [None]
    other = db.query("SELECT roe FROM financials WHERE report_date = ?", [Q4])
    assert other["roe"].to_list() == [0.19], "同批次里口径正常的行不受影响"


def test_a_known_zero_survives_while_an_unknown_does_not(fin, db):
    """股息率 0 是「真的不分红」，null 是「不知道」——两者在选股里后果不同（D58）。"""
    fin.upsert(pl.DataFrame([row(dividend_yield=0.0)]))
    assert db.query("SELECT dividend_yield FROM financials")["dividend_yield"].to_list() == [0.0]


def test_nan_becomes_null_instead_of_overwriting_a_good_number(fin, db):
    fin.upsert(pl.DataFrame([row(roe=float("nan"))]))
    assert db.query("SELECT roe FROM financials")["roe"].to_list() == [None]


def test_key_forms_converge_on_write(fin):
    """写 `hk`/`700`、读 `HK`/`00700` 必须是同一只票（D50）。

    否则市赚率拿今天的 PE（`00700`）去配 ROE（`700`）时配不上，症状是「这只票算不出
    PR」，而真相是两次采集用了两种写法。
    """
    fin.upsert(pl.DataFrame([row(market="hk", code="00700", roe=0.24)]))
    latest, dropped = fin.load_as_of("HK", ["700"], date(2024, 4, 30), basis="latest")
    assert dropped == 0
    assert latest["roe"].to_list() == [0.24]


def test_report_date_must_be_a_plain_date(fin):
    """带时刻的日期 cast 成 date 会按 UTC 取日历日：A 股 00:30+08:00 存成前一天。"""
    with pytest.raises(StorageError, match="pl.Date"):
        fin.upsert(pl.DataFrame([row(report_date=datetime(2024, 3, 31, 0, 30))]))


def test_financials_refuses_the_daily_multiples(fin):
    """D60 的边界要有护栏：把 PE 塞进报告期不会报错，只会静默用错日期的数。"""
    with pytest.raises(StorageError, match="valuation_daily"):
        fin.upsert(pl.DataFrame([row(pe_ttm=18.5)]))


@pytest.fixture
def val(db) -> ValuationRepo:
    return ValuationRepo(db)


def test_valuation_latest_is_the_newest_row_on_or_before_as_of(val):
    val.upsert(
        pl.DataFrame(
            [
                {"market": "A", "code": "601088", "date": date(2024, 6, 27),
                 "pe_ttm": 18.0, "pb": 2.0, "source": "akshare"},
                {"market": "A", "code": "601088", "date": date(2024, 6, 28),
                 "pe_ttm": 18.5, "pb": 2.1, "source": "akshare"},
            ]
        )
    )
    latest = val.latest("A", ["601088"], date(2024, 6, 28))
    assert latest["pe_ttm"].to_list() == [18.5]
    assert val.latest("A", ["601088"], date(2024, 6, 27))["pe_ttm"].to_list() == [18.0]


def test_a_stale_valuation_row_is_visible_when_asked_and_excluded_when_not(val):
    """停牌两个月的票「有 PE」，但那个 PE 属于两个月前：拿它配今天的 ROE 不是同一天的一篮子。

    `within_days` 由调用方给，被排除的票数由调用方说出口——链不替调用方决定「旧到不能用」。
    """
    val.upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "601088",
                    "date": date(2024, 4, 10),
                    "pe_ttm": 15.0,
                    "source": "akshare",
                }
            ]
        )
    )
    assert val.latest("A", ["601088"], date(2024, 6, 28))["pe_ttm"].to_list() == [15.0]
    assert val.latest("A", ["601088"], date(2024, 6, 28), within_days=7).height == 0
    assert val.covered_codes("A", as_of=date(2024, 6, 28)) == []


def test_a_negative_pe_is_stored_as_is(val, db):
    """亏损票的 PE(TTM) 为负是**一个真数**，不是脏数据：PR 的分支就靠它（D59）。

    把它置空，读取层就分不清「这只票亏损」与「这只票没同步 PE」——前者该走 PB 那一支，
    后者该去补数据。
    """
    val.upsert(
        pl.DataFrame([{"market": "A", "code": "000001", "date": Q1, "pe_ttm": -4.2,
                       "pb": 0.55, "source": "akshare"}])
    )
    stored = db.query("SELECT pe_ttm FROM valuation_daily")["pe_ttm"].to_list()
    assert stored == [-4.2]


def test_flow_rows_round_trip(db):
    flow = FundingFlowRepo(db)
    assert flow.upsert(
        pl.DataFrame(
            [{"market": "A", "code": "601088", "date": Q1, "main_net_in": 1.2e8,
              "north_net": None, "source": "akshare"}]
        )
    ) == 1
    frame = flow.load_range("A", "601088", Q1, Q1)
    assert frame["main_net_in"].to_list() == [1.2e8]
    assert frame["north_net"].to_list() == [None], "不知道 ≠ 0"


def test_scan_results_overwrite_the_same_day_the_same_rule(db):
    repo = ScanResultRepo(db)
    repo.record(
        on=Q1, market="A", strategy_id="pr<=0.4", universe_size=137,
        hits=[{"code": "601088", "name": "中国神华", "pr": 0.31}],
    )
    repo.record(
        on=Q1, market="A", strategy_id="pr<=0.4", universe_size=140,
        hits=[{"code": "601088", "name": "中国神华", "pr": 0.31}],
    )
    assert db.row_count("scan_results") == 1, "同一天同一条规则重跑不该追加行"
    record = repo.get(on=Q1, market="A", strategy_id="pr<=0.4")
    assert record["universe_size"] == 140
    # 中文票名不转义：这张表是给人看的，转义后既没法直接读也不省空间
    assert "中国神华" in db.query("SELECT hits_json FROM scan_results")["hits_json"][0]
    assert hits_frame(record)["pr"].to_list() == [0.31]


def test_a_scan_result_can_be_read_back_after_a_datetime_field(db):
    """命中里带 date 值（报告期、估值日）是常态：JSON 不认 date，序列化成 ISO 字符串。"""
    repo = ScanResultRepo(db)
    repo.record(
        on=Q1, market="A", strategy_id="pr<=0.4", universe_size=1,
        hits=[{"code": "601088", "period": Q1, "at": datetime(2024, 3, 31, 9, 30)}],
    )
    hit = repo.get(on=Q1, market="A", strategy_id="pr<=0.4")["hits"][0]
    assert hit["period"] == "2024-03-31" and hit["at"] == "2024-03-31T09:30:00"


def test_an_empty_batch_writes_nothing_and_returns_zero(fin, val):
    assert fin.upsert(pl.DataFrame()) == 0
    assert val.upsert(pl.DataFrame()) == 0


def test_load_as_of_needs_an_as_of_date(fin):
    """PIT 读取没有「默认今天」这条路：截至日由调用方给，一路到 SQL 不许是 None。"""
    with pytest.raises(Exception, match="截至日"):
        fin.load_as_of("A", ["601088"], None)


def test_a_missing_required_column_is_refused_not_null_filled(fin):
    with pytest.raises(StorageError, match="必需列"):
        fin.upsert(pl.DataFrame([{"market": "A", "code": "601088"}]))
