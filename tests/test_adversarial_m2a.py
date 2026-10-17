"""M2-A 对抗测试：除权链路的说谎路径（A-7 三道闸的复现集）。

对抗测试 agent 报来的每一条都在源码上验证过，逐条修好后留在这里当回归用例：它们锁的
不是「算得对」，而是「那条说谎的路再也走不通」。编号沿用报告里的编号，方便对着
spec §18 与当期计划读。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.analysis.adjust import day_factors
from sams.analysis.returns import interval_return
from sams.core.codes import daily_price_limit
from sams.core.errors import NotAdjustable
from sams.datasource.actions import normalize_actions
from sams.datasource.akshare_cn import AkshareAdapter
from sams.datasource.registry import ActionsResult, Attempt
from sams.services.corp_actions import CorpActionSyncService
from sams.storage.repos.corp_actions import CorpActionRepo
from sams.storage.repos.corp_actions_coverage import CorpActionsCoverageRepo

START = date(2024, 7, 1)


@dataclass(frozen=True)
class Span:
    covered_from: date
    covered_to: date
    full_history: bool = True
    src: str = "akshare"


def frame(closes, code: str = "600000", first: date = START) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [first + timedelta(days=i) for i in range(len(closes))],
            "close": list(closes),
            "market": "A",
            "code": code,
        }
    )


NO_ACTIONS = pl.DataFrame(
    schema={
        "ex_date": pl.Date,
        "kind": pl.Utf8,
        "cash_per_share": pl.Float64,
        "ratio": pl.Float64,
        "price": pl.Float64,
    }
)


def fresh_coverage() -> list[Span]:
    return [Span(covered_from=START, covered_to=date(2024, 12, 31))]


# ======================================================================================
# 1. 同日两笔派息：normalize_actions 的 `unique(subset=["ex_date","kind"], keep="last")`
#    静默吞掉第二笔（特别分红与年度分红同日除权是真实存在的事件）。
#    手算：前收 10，年度 0.10 + 特别 0.05 ⇒ 参考价 9.85、因子 10/9.85=1.015228；
#    实测输出只留 cash=0.05 ⇒ 因子 10/9.9=1.005025，少的 1% 乘不进任何序列。
# ======================================================================================


def test_two_dividends_on_the_same_ex_date_are_both_applied():
    raw = pl.DataFrame(
        {
            "ex_date": [date(2024, 7, 8), date(2024, 7, 8)],
            "kind": ["dividend", "dividend"],
            "cash_per_share": [0.10, 0.05],
            "ratio": [None, None],
            "price": [None, None],
            "note": ["年度分红", "特别分红"],
        }
    )
    out = normalize_actions(raw, market="A", code="601088", source="akshare")
    # 要么两行都在，要么合并成一行 cash=0.15；绝不是静默丢一条
    total = sum(out["cash_per_share"].to_list()) if out.height else 0.0
    assert out.height == 2 or total == pytest.approx(0.15), (
        f"同日两笔派息只剩 {out.height} 行、合计 {total}：第二笔被 keep='last' 吞了"
    )
    bars = pl.DataFrame({"date": [date(2024, 7, 5), date(2024, 7, 8)], "close": [10.0, 9.85]})
    acts = out.select(["ex_date", "kind", "cash_per_share", "ratio", "price"])
    factor = day_factors(bars, acts)[0].factor
    assert factor == pytest.approx(10.0 / 9.85), f"因子 {factor} ≠ 手算 10/9.85"


# ======================================================================================
# 2+3. 全历史源被按窗口切片后，窗口外的事实**被丢弃**，而服务照样写下
#      full_history=True、covered_to=今天 的覆盖确认。
#      症状：库里缺 2015 年的派息行，守卫却放行 2015 年区间 ⇒ 错数静默出门。
# ======================================================================================


def _fhps_payload() -> pl.DataFrame:
    """东财分红送配原始行：2015 与 2024 各一笔实施分配。"""
    return pl.DataFrame(
        {
            "除权除息日": [date(2015, 7, 8), date(2024, 7, 8)],
            "报告期": ["2014年度", "2023年度"],
            "现金分红-现金分红比例": [8.0, 22.60],
            "现金分红-现金分红比例描述": ["10派8.00元", "10派22.60元(含税)"],
            "送转股份-送转总比例": [None, None],
            "送转股份-送股比例": [None, None],
            "送转股份-转股比例": [None, None],
            "方案进度": ["实施分配", "实施分配"],
        }
    )


class _RegistryStub:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def fetch_actions(self, market, code, start, end) -> ActionsResult:
        frame_ = self.adapter.corp_actions(market, code, start, end)
        return ActionsResult(
            market,
            code,
            frame_,
            self.adapter.name,
            (Attempt(self.adapter.name, True),),
            (),
            full_history=True,  # AkshareAdapter 就是这样声明的
        )


def test_full_history_sync_of_a_narrow_window_still_persists_every_fact_it_saw(db):
    adapter = AkshareAdapter(fhps_fetch=lambda symbol: _fhps_payload())
    actions, coverage = CorpActionRepo(db), CorpActionsCoverageRepo(db)
    service = CorpActionSyncService(
        _RegistryStub(adapter),
        actions,
        coverage,
        clock=FrozenClock(datetime(2024, 7, 9, tzinfo=UTC)),
    )
    service.sync_actions("A", "601088", date(2024, 1, 1), date(2024, 3, 1))
    claim = coverage.get("A", "601088", src="akshare")
    assert claim.full_history, "这条断言成立才说明下面的缺口是真危险"
    stored = actions.list_for("A", "601088")["ex_date"].to_list()
    assert date(2015, 7, 8) in stored, (
        f"源已给出 2015 派息、覆盖确认声称全历史，库里却没有这行：{stored}"
    )


def test_the_2015_fact_a_narrow_window_used_to_drop_now_changes_the_number(db):
    """A-7 修好后的正面写法：全历史源把 2015 那笔也存进库，2015 的区间因此给出复权数。

    修之前这条路径给的是裸价 −8%（守卫照覆盖标记放行，库里却没有那笔除息），
    而交易所口径是 0%——同一天、同一个数，只有一个不是错的。
    """
    adapter = AkshareAdapter(fhps_fetch=lambda symbol: _fhps_payload())
    actions, coverage = CorpActionRepo(db), CorpActionsCoverageRepo(db)
    service = CorpActionSyncService(
        _RegistryStub(adapter),
        actions,
        coverage,
        clock=FrozenClock(datetime(2024, 7, 9, tzinfo=UTC)),
    )
    service.sync_actions("A", "601088", date(2024, 1, 1), date(2024, 3, 1))
    old_first = date(2015, 7, 1)
    # 10派8 ⇒ 每股 0.8 元，前收 20 的参考价 19.2；当天收在参考价上 ⇒ 复权涨跌 0%
    bars = frame([20.0] * 7 + [19.2] * 4, code="601088", first=old_first)
    result = interval_return(
        bars,
        actions.list_for("A", "601088"),
        market="A",
        code="601088",
        start=old_first,
        end=old_first + timedelta(days=10),
        coverage=coverage.for_code("A", "601088"),
        today=date(2024, 7, 9),
    )
    assert result.raw == pytest.approx(-0.04)
    assert result.adjusted == pytest.approx(0.0)
    assert result.differs, "复权前后必须给出不同的数，否则这条用例什么都没测到"
    assert result.ex_dates == 1


# ======================================================================================
# 4. D37 不看 actions：一次**已完整记录**的 10送10（−50% 跳变）被当成
#    「一定有未记录的公司行为」拒绝——假指控 + 拒绝了自己存在的理由（U12 场景）。
# ======================================================================================


def test_a_recorded_bonus_split_jump_is_not_refused_as_unrecorded():
    closes = [100.0] * 7 + [50.0] * 4
    acts = pl.DataFrame(
        {
            "ex_date": [START + timedelta(days=7)],
            "kind": ["bonus"],
            "cash_per_share": [None],
            "ratio": [2.0],
            "price": [None],
        }
    )
    result = interval_return(
        frame(closes),
        acts,
        market="A",
        code="600000",
        start=START,
        end=START + timedelta(days=10),
        coverage=fresh_coverage(),
    )
    assert result.adjusted == pytest.approx(0.0), "除权日开盘价恰为参考价 ⇒ 复权涨跌 0%"


# ======================================================================================
# 5. 配股缺口：A 股唯一的源看不见配股，而 D37 也抓不住它——典型 10配2@五折 ⇒ 参考价
#    (P + 0.5P×0.2)/1.2 = 0.9167P，跳变只有 −8.3%，在主板证据阈值 15% 之内。
#    这条**不作为用例锁住**：它不是代码缺陷，是没有信号的盲区（锁住等于宣布它已解决）。
#    记在 spec §18 U16，并写进 akshare_actions 的模块文档。
# ======================================================================================


# ======================================================================================
# 6. D37 的「跳过开头 5 根」按**请求窗口**的位置算，而 interval_return 的 docstring
#    明确鼓励窄窗口取 bars ⇒ 老股票的区间只要从大跳变前 5 天内开始，证据规则失明。
#    手算：−30% 在窗口第 4 根（主板上不可能由交易产生），照样出门。
# ======================================================================================


def test_d37_skip_counts_bars_since_listing_not_since_the_window():
    closes = [10.0] * 4 + [7.0, 7.0]
    with pytest.raises(NotAdjustable):
        interval_return(
            frame(closes),
            NO_ACTIONS,
            market="A",
            code="600000",
            start=START,
            end=START + timedelta(days=5),
            coverage=fresh_coverage(),
        )


# ======================================================================================
# 7. 低：末根收盘为 null ⇒ TypeError 裸奔，而不是 NotAdjustable 带原因拒绝。
#    「缺数就说不出口」是这条链的合同；崩栈不是拒绝。
# ======================================================================================


def test_a_null_close_refuses_with_reason_not_with_a_crash():
    closes = [10.0] * 6 + [None]
    with pytest.raises(NotAdjustable):
        interval_return(
            frame(closes),
            NO_ACTIONS,
            market="A",
            code="600000",
            start=START,
            end=START + timedelta(days=6),
            coverage=fresh_coverage(),
        )


# ======================================================================================
# 攻击失败的记录：这些守卫确实挡住了
# ======================================================================================


def test_an_unknown_kind_inside_the_window_still_refuses():
    acts = pl.DataFrame(
        {
            "ex_date": [START + timedelta(days=4)],
            "kind": ["unknown"],
            "cash_per_share": [None],
            "ratio": [None],
            "price": [None],
            "note": ["以股代息"],
        }
    )
    with pytest.raises(NotAdjustable, match="无法折算"):
        interval_return(
            frame([10.0] * 8),
            acts,
            market="A",
            code="600000",
            start=START,
            end=START + timedelta(days=7),
            coverage=fresh_coverage(),
        )


def test_fractional_bonus_hand_checked_through_the_interval():
    """10转2.5（非整数）：ex_ref = 100/1.25 = 80，跳变 −20% < 创业板阈值 30%，
    复权涨跌应恰好为 0%。手算与输出对齐。"""
    acts = pl.DataFrame(
        {
            "ex_date": [START + timedelta(days=7)],
            "kind": ["transfer"],
            "cash_per_share": [None],
            "ratio": [1.25],
            "price": [None],
        }
    )
    result = interval_return(
        frame([100.0] * 7 + [80.0] * 4, code="301234"),
        acts,
        market="A",
        code="301234",
        start=START,
        end=START + timedelta(days=10),
        coverage=fresh_coverage(),
    )
    assert result.raw == pytest.approx(-0.2)
    assert result.adjusted == pytest.approx(0.0)


def test_hk_and_us_have_no_evidence_rule_but_still_require_coverage():
    assert daily_price_limit("HK", "00700") is None
    assert daily_price_limit("US", "AAPL") is None
    closes = [10.0] * 8 + [4.0] + [4.0] * 3
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        interval_return(
            frame(closes, code="00700"),
            NO_ACTIONS,
            market="HK",
            code="00700",
            start=START,
            end=START + timedelta(days=11),
            coverage=[],
        )


def test_stale_full_history_claim_expires_when_today_is_passed():
    span = Span(covered_from=START, covered_to=date(2024, 7, 20), full_history=True)
    with pytest.raises(NotAdjustable, match="覆盖确认"):
        interval_return(
            frame([10.0] * 8),
            NO_ACTIONS,
            market="A",
            code="600000",
            start=START,
            end=START + timedelta(days=7),
            coverage=[span],
            today=date(2025, 1, 1),
        )


def test_resync_with_a_different_src_replaces_the_fact_instead_of_doubling_it(db):
    """主键 (market, code, ex_date, kind) 不含 src：跨源重同步覆盖写、不重复计因子。"""
    actions = CorpActionRepo(db)

    def payload(src: str, cash: float):
        return pl.DataFrame(
            {
                "market": ["A"],
                "code": ["601088"],
                "ex_date": [date(2024, 7, 8)],
                "kind": ["dividend"],
                "cash_per_share": [cash],
                "ratio": [None],
                "price": [None],
                "src": [src],
                "note": [None],
            }
        )

    actions.upsert_many(payload("akshare", 2.26))
    actions.upsert_many(payload("eastmoney", 2.26))
    stored = actions.list_for("A", "601088")
    assert stored.height == 1, "同一事实两个源各存一行 ⇒ 因子乘两次"
    factor = day_factors(
        pl.DataFrame({"date": [date(2024, 7, 5), date(2024, 7, 8)], "close": [42.0, 39.74]}),
        stored.select(["ex_date", "kind", "cash_per_share", "ratio", "price"]),
    )[0].factor
    assert factor == pytest.approx(42.0 / 39.74), "因子必须只乘一次 2.26 元"
