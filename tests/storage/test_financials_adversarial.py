"""对抗性测试（M2-C 第一道闸）：`RATIO_BOUNDS` 那道量级闸的真实覆盖范围。

写法与本仓库既有的 adversarial 文件一致：每个用例只证一件事。
`test_should_*` 断言的是「我认为必须成立、当场不成立」的那类——它**允许是红的**，
红的那一条就是finding，实现者不许把断言改软来让它变绿。
其余用例刻画当前行为（含刻意宽松的边缘），文档里写明后果。

全部离线：内存库，不碰网络。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.analysis.fundamentals import pr
from sams.core.fundamentals import RATIO_BOUNDS
from sams.services.fundamentals import FundamentalService
from sams.storage.repos.financials import FinancialRepo, ValuationRepo

FY = date(2023, 12, 31)
PUBLISHED = date(2024, 3, 20)
VALUATION_DAY = date(2024, 7, 11)


def fin_row(**over) -> dict:
    base = {
        "market": "A",
        "code": "601088",
        "report_date": FY,
        "published_date": PUBLISHED,
        "roe": 0.20,
        "revenue_yoy": 0.12,
        "profit_yoy": 0.08,
        "dividend_yield": 0.042,
        "market_cap": 6.4e11,
        "source": "akshare",
    }
    base.update(over)
    return base


@pytest.fixture
def fin(db) -> FinancialRepo:
    return FinancialRepo(db)


@pytest.fixture
def val(db) -> ValuationRepo:
    return ValuationRepo(db)


def stored_roe(db, code: str = "601088") -> list[float | None]:
    return db.query("SELECT roe FROM financials WHERE code = ?", [code])["roe"].to_list()


# ---------- 量级闸的边缘：5.0 到底是「含」还是「不含」 ----------


def test_the_guard_is_inclusive_and_the_roe_ceiling_is_one(fin, db):
    """D66：判据含边界，且 `roe` 的上界从 5.0 收到 1.0。

    5.0 是按「500% 不可能」定的，却留下了 1.0–5.0 这段死区：没 ÷100 的「3.5%」正好落在
    里面，而 3.5% 恰恰是周期底部与困境反转票的真实 ROE 区间——最需要算 PR 的那批票，
    它们的百分数写法全在闸门之内。小数口径下 ROE 达到 100% 本身就不可信，而它与
    「忘了 ÷100」无法区分，所以一起拒。
    """
    assert RATIO_BOUNDS["roe"] == 1.0
    fin.upsert(pl.DataFrame([fin_row(code="600001", roe=0.99)]))
    fin.upsert(pl.DataFrame([fin_row(code="600002", roe=1.0)]))
    fin.upsert(pl.DataFrame([fin_row(code="600003", roe=3.5)]))
    assert stored_roe(db, "600001") == [0.99], "0.99 = 99% 还在可信范围里"
    assert stored_roe(db, "600002") == [None], "1.0 = 100% ROE 同样不可信，判据含边界"
    assert stored_roe(db, "600003") == [None], "3.5 正是「3.5%」忘 ÷100 的形状"


def test_the_growth_columns_keep_a_wider_ceiling_but_the_same_inclusive_guard(fin, db):
    """增速与 ROE 不同：+500% 的营收增速是小基数公司的正常数字，所以上界留 5.0；
    但判据同样含边界——一道闸有两种判据比一种判据统一更危险。"""
    fin.upsert(pl.DataFrame([fin_row(code="600001", revenue_yoy=4.99)]))
    fin.upsert(pl.DataFrame([fin_row(code="600002", revenue_yoy=5.0)]))

    def yoy(code):
        return db.query(
            "SELECT revenue_yoy FROM financials WHERE code = ?", [code]
        )["revenue_yoy"].to_list()

    assert yoy("600001") == [4.99]
    assert yoy("600002") == [None], "含边界：5.0 与 5.01 同侧"


def test_the_negative_side_uses_the_same_abs_ceiling(fin, db):
    """负方向同样只看绝对值：ROE 的 −1.0 与 +1.0 一道被拒。

    后果有限：`pr()` 对 ≤ 0 的 ROE 两条支都拒，所以负向越界不会算出一个「便宜」。
    """
    fin.upsert(pl.DataFrame([fin_row(code="600001", roe=-0.99)]))
    fin.upsert(pl.DataFrame([fin_row(code="600002", roe=-1.0)]))
    assert stored_roe(db, "600001") == [-0.99]
    assert stored_roe(db, "600002") == [None]


def test_the_dividend_yield_bound_is_one_and_zero_survives(fin, db):
    """股息率的闸是 1.0（=100%）。刻画两侧：0.99 活、1.01 空、0 活。"""
    fin.upsert(pl.DataFrame([fin_row(code="600001", dividend_yield=0.99)]))
    fin.upsert(pl.DataFrame([fin_row(code="600002", dividend_yield=1.01)]))
    fin.upsert(pl.DataFrame([fin_row(code="600003", dividend_yield=0.0)]))

    def dy(code):
        return db.query(
            "SELECT dividend_yield FROM financials WHERE code = ?", [code]
        )["dividend_yield"].to_list()

    assert dy("600001") == [0.99]
    assert dy("600002") == [None], "1.01 越界必须置空"
    assert dy("600003") == [0.0], "0 是「真的不分红」，不能被当成「不知道」"
    # 边界：正好 1.0（=100% 股息率）与上面 ROE 的 5.0 同一个问题
    fin.upsert(pl.DataFrame([fin_row(code="600004", dividend_yield=1.0)]))
    assert dy("600004") == [None], "1.0 = 100% 股息率同样不可能，判据应当含边界"


def test_the_guard_also_covers_the_daily_valuation_table(val, db):
    """`dividend_yield` 在 `valuation_daily` 里也是比率列（同一份 RATIO_BOUNDS）。"""
    val.upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "601088",
                    "date": VALUATION_DAY,
                    "pe_ttm": 14.54,
                    "pb": 2.09,
                    "dividend_yield": 5.01,  # 百分数未 ÷100
                    "source": "akshare",
                }
            ]
        )
    )
    got = db.query("SELECT dividend_yield FROM valuation_daily")["dividend_yield"].to_list()
    assert got == [None], "按日那一侧也必须过同一道量级闸"
    # 而倍数列不受它影响：PE/PB 是倍数，5 倍市净率不该被擦掉
    assert db.query("SELECT pe_ttm, pb FROM valuation_daily").to_dicts() == [
        {"pe_ttm": 14.54, "pb": 2.09}
    ]


# ---------- 置空之后 PR 是不是「拒绝」而不是「算得出」 ----------


def test_a_nulled_roe_makes_pr_refuse_rather_than_compute(fin, val, db):
    """D58 那条处置的全部意义：置空 ⇒ PR 不出数。

    没做到这一条的话，「置空」只是把 20.0 换成 null，下游把 null 当 0 就算出 PR=0
    （0 在 PR 刻度上是「免费」，设计文档 §7 明说是这最贵的一个误读）。
    """
    fin.upsert(pl.DataFrame([fin_row(roe=20.0)]))
    val.upsert(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "601088",
                    "date": VALUATION_DAY,
                    "pe_ttm": 20.0,
                    "pb": 2.0,
                    "source": "akshare",
                }
            ]
        )
    )
    read = FundamentalService(fin, val).read("A", "601088", as_of=VALUATION_DAY)
    assert read.roe is None
    assert read.pr.value is None, "被置空的 ROE 必须让 PR 拒绝"
    assert read.pr.reason, "拒绝必须带原因"
    assert "0.0" not in f"{read.pr.value}"  # 绝不打出一个当作「免费」的 0


def test_a_row_nulled_for_one_column_keeps_the_other_columns(fin, db):
    """处置是「置空那一列、留下那一行」：不能顺手把整行丢掉（否则 universe 会少一只）。"""
    fin.upsert(pl.DataFrame([fin_row(roe=20.0, revenue_yoy=0.12, dividend_yield=0.042)]))
    row = db.query("SELECT roe, revenue_yoy, dividend_yield FROM financials").to_dicts()[0]
    assert row == {"roe": None, "revenue_yoy": 0.12, "dividend_yield": 0.042}


# ---------- 闸没挡住的量级错：0%–5% 这一带的百分数完全合法地存活 ----------


def test_a_percent_caliber_roe_in_the_former_dead_zone_is_nulled_and_pr_refuses(fin, db):
    """D66 关掉的那道口子，正是本文件原来「让它当场可见」的那一种漏网。

    ROE 在 0%–5% 之间的票（周期底部、困境反转）数量极大；它们的百分数写法 3.5 曾经落在
    `abs > 5.0` 之内，于是**不 warn、不置空**地进库，PR 从 5.71 变成 0.0571——而后者
    低于 0.4，会被排进 `PR ≤ 0.4` 的第一名。现在它被置空，PR 直接拒绝出数。
    """
    fin.upsert(pl.DataFrame([fin_row(roe=3.5)]))  # 未 ÷100 的「3.5%」
    assert stored_roe(db) == [None], "死区里的百分数写法必须被拦下"
    honest = pr(pe_ttm=20.0, roe=0.035)
    assert honest.value == pytest.approx(5.7142857, rel=1e-6)  # 这只票真正的 PR：远高于 0.4
    assert honest.value > 0.4, "真值不进篮子——这条规则该把它排在外面"


def test_pr_refuses_a_denominator_that_can_only_be_a_percentage():
    """口径闸在**算式那一层**也有一份（D66）：不依赖上游有没有拦。

    `pr()` 是整个功能里唯一算 PR 的地方。只把闸放在仓库写入侧，等于默认「每个把数交给
    它的调用方都已经过库」——`--roe-estimate` 就是那样一条路：它压根不经过 `financials`。
    """
    ratio = pr(pe_ttm=20.0, roe=3.5)
    assert ratio.value is None, (
        f"ROE=3.5 只能按百分数读，PR 却给出了 {ratio.value}（真值应为 5.71），"
        "并且带着 basis='pe' 与『已披露，小数口径』那句话进篮子"
    )
    assert ratio.basis == "refused" and ratio.reason
    estimated = pr(pe_ttm=-12.5, pb=2.0, roe_estimate=5.0)
    assert estimated.value is None, "预估那一支把 ROE 乘两遍，口径错一次错一万倍"
