"""港股公司行为解析（东财 `stock_hk_dividend_payout_em`）。

样本取自 2026-10-17 的实测响应：腾讯 00700 的实物分派、00005 的美元派息、
01211 的「派息 + 送股 + 转增」同一天、00016 的红利认股权证。
留这些形状而不是留一条通用的「能解析」断言，是因为每一种都会静默折错数。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from sams.core.errors import DatasourceError, FieldDrift
from sams.datasource.akshare_hk import (
    AkshareHkAdapter,
    expand_payout,
    parse_scheme,
)
from sams.datasource.sources import known

EX = date(2024, 5, 17)


def payout(*schemes: str, ex_dates: list[date] | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "除净日": (ex_dates or [EX] * len(schemes)),
            "分红方案": list(schemes),
            "分配类型": ["年度分配"] * len(schemes),
        }
    )


def kinds(frame: pl.DataFrame) -> list[str]:
    return frame.sort("kind")["kind"].to_list()


# --- 单个事实 --------------------------------------------------------------------


def test_hkd_dividend_is_taken_at_face_value():
    assert expand_payout(payout("每股派港币0.34元")).to_dicts() == [
        {
            "ex_date": EX,
            "kind": "dividend",
            "cash_per_share": 0.34,
            "ratio": None,
            "price": None,
            "note": "年度分配｜每股派港币0.34元",
        }
    ]


def test_foreign_currency_uses_the_hkd_equivalent_not_the_listed_one():
    """K 线是港币报价：拿人民币数去除港币价格，因子差一个汇率倍数而图上看不出来。"""
    out = expand_payout(payout("每股派人民币0.358元(相当于港币0.41141元)"))
    assert out["cash_per_share"].to_list() == [0.41141]


def test_foreign_currency_without_an_equivalent_is_unknown():
    """不存汇率就不猜：记一条 unknown，让跨那天的区间收益拒绝出数（D39）。"""
    out = expand_payout(payout("每股派美元0.1元"))
    assert kinds(out) == ["unknown"]
    assert "无港币等价" in out["note"][0]


def test_bonus_shares_become_a_share_multiplier():
    out = expand_payout(payout("每10股派送8股"))
    assert out.select(["kind", "ratio"]).to_dicts() == [{"kind": "bonus", "ratio": 1.8}]


def test_capital_reserve_conversion_is_its_own_kind():
    out = expand_payout(payout("每10股转12股"))
    assert out.select(["kind", "ratio"]).to_dicts() == [{"kind": "transfer", "ratio": 2.2}]


# --- 一条方案里有多个事实 ---------------------------------------------------------


def test_one_scheme_with_cash_bonus_and_transfer_splits_into_three_rows():
    """01211 的 2025 年度分配实测就是这么写的。

    塞成一行只能选一种语义（D34 的主键），而漏掉任何一件都会让因子少乘一次。
    """
    out = expand_payout(
        payout("每股派人民币3.974元(相当于港币4.33596元),每10股派送8股,每10股转12股")
    )
    assert kinds(out) == ["bonus", "dividend", "transfer"]
    assert out.filter(pl.col("kind") == "dividend")["cash_per_share"].to_list() == [4.33596]
    assert out.filter(pl.col("kind") == "transfer")["ratio"].to_list() == [2.2]


def test_two_cash_segments_on_the_same_ex_date_merge_into_one_dividend():
    """`corp_actions` 主键是 (market, code, ex_date, kind)，同键两行会互相覆盖。"""
    out = expand_payout(payout("每股派港币0.1元", "每股派港币0.2元"))
    assert out.height == 1
    assert out["cash_per_share"].to_list() == [0.3]


# --- 不能折算的那些（D39） --------------------------------------------------------


def test_bonus_warrants_are_not_shares():
    """00016 实测有「每12股派送1股红利认股权证」——那是另付行权价的期权，不是白送的股。"""
    out = expand_payout(payout("每12股派送1股红利认股权证"))
    assert kinds(out) == ["unknown"]
    assert "认股权证" in out["note"][0]


def test_in_kind_distribution_is_recorded_rather_than_dropped():
    """腾讯 2023-01-05 分派美团股：真实发生过，但折不出每股现金或股数乘数。"""
    out = expand_payout(payout("特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元)"))
    assert kinds(out) == ["unknown"]
    assert "美团" in out["note"][0]


def test_scrip_option_still_counts_as_the_cash_dividend():
    """「可选择以股代息」的现金基准是确定的，选择权在股东那边——按现金记。"""
    out = expand_payout(payout("每股派港币0.95元(可选择以股代息)"))
    assert out["kind"].to_list() == ["dividend"]
    assert out["cash_per_share"].to_list() == [0.95]


def test_an_unrecognized_segment_becomes_unknown_rather_than_nothing():
    out = expand_payout(payout("每10股派送0.5份认购权证，行使价未披露"))
    assert kinds(out) == ["unknown"]


# --- 形状与边界 -------------------------------------------------------------------


def test_missing_required_column_is_drift():
    with pytest.raises(FieldDrift, match="除净日"):
        expand_payout(pl.DataFrame({"分红方案": ["每股派港币0.1元"]}))


def test_a_blank_scheme_is_a_placeholder_row_not_an_event():
    """unknown 必须带原文（仓库层的校验）：空方案折不出事件，按占位行丢弃。"""
    assert expand_payout(payout("")).is_empty()


def test_no_rows_yields_the_typed_empty_frame():
    out = expand_payout(pl.DataFrame({"除净日": [], "分红方案": [], "分配类型": []}))
    assert out.is_empty()
    assert out.columns == ["ex_date", "kind", "cash_per_share", "ratio", "price", "note"]


def test_parse_scheme_returns_nothing_for_blank_text():
    assert parse_scheme(None) == []
    assert parse_scheme("   ") == []


# --- 适配器：窗口、空答案、能力与错误归类 ------------------------------------------


def hk_adapter(*schemes: str, ex_dates: list[date] | None = None) -> AkshareHkAdapter:
    frame = payout(*schemes, ex_dates=ex_dates)
    return AkshareHkAdapter(payout_fetch=lambda symbol: frame)


def test_the_adapter_adds_market_canonical_code_and_src():
    out = hk_adapter("每股派港币0.34元").corp_actions(
        "HK", "HK00700", date(2024, 1, 1), date(2024, 12, 31)
    )
    assert out.select(["market", "code", "ex_date", "kind", "src"]).to_dicts() == [
        {"market": "HK", "code": "00700", "ex_date": EX, "kind": "dividend", "src": "akshare_hk"}
    ], "键不归一，同一天的分红会为一只票写出两个主键，自选清单只认其中一个"


def test_a_full_history_source_is_not_sliced_to_the_window():
    """接口给全史 ⇒ 交回全史。窗口只决定这次「问的是哪段」，不决定库里存几行。

    切掉窗口外的行，服务层却照样写「全历史，确认至今天」，守卫照着它放行 2015 年的
    区间——而那笔 2015 年的除息从没入库，区间涨跌就成了裸价。少存几行省下的空间，
    买回来的是 U12 那个看起来正常的错数。
    """
    out = hk_adapter(
        "每股派港币0.1元", "每股派港币0.2元", ex_dates=[date(2023, 5, 17), EX]
    ).corp_actions("HK", "00700", date(2024, 1, 1), date(2024, 12, 31))
    assert out["ex_date"].to_list() == [date(2023, 5, 17), EX]
    assert out["cash_per_share"].to_list() == [0.1, 0.2]


def test_a_stock_that_never_paid_out_also_answers_empty():
    frame = pl.DataFrame({"除净日": [], "分红方案": [], "分配类型": []})
    out = AkshareHkAdapter(payout_fetch=lambda symbol: frame).corp_actions(
        "HK", "00700", date(1990, 1, 1), date(2024, 12, 31)
    )
    assert out.is_empty()


def test_an_unpriceable_action_reaches_the_repo_with_its_note():
    """unknown 要在归一化里活下来：D39 的拒绝出数依赖它带着原文进了库。"""
    out = hk_adapter("每12股派送1股红利认股权证").corp_actions(
        "HK", "00016", date(2024, 1, 1), date(2024, 12, 31)
    )
    assert out["kind"].to_list() == ["unknown"]
    assert "认股权证" in out["note"][0]


def test_a_network_failure_is_classified_for_the_router():
    def boom(symbol):
        raise ConnectionError("connection reset by peer")

    with pytest.raises(DatasourceError, match="TransientError|连接|reset"):
        AkshareHkAdapter(payout_fetch=boom).corp_actions(
            "HK", "00700", date(2024, 1, 1), date(2024, 12, 31)
        )


def test_the_adapter_refuses_a_market_it_does_not_serve():
    with pytest.raises(ValueError, match="A"):
        hk_adapter("每股派港币0.1元").corp_actions("A", "600519", date(2024, 1, 1), EX)


def test_the_source_is_registered_as_actions_only():
    """路由与备源判据都读这个能力标记（D38），所以它是契约不是实现细节。"""
    entry = known()["akshare_hk"]
    assert entry.capabilities == ("corp_actions",)
    assert not hasattr(AkshareHkAdapter(), "daily_bars")
    assert AkshareHkAdapter.corp_actions_full_history is True


@pytest.mark.live
def test_live_tencent_payout_carries_the_in_kind_distributions_as_unknown():
    """00700 分派过美团股（实测 2023-01 前后）：这类实物分派折不出数，必须是 unknown。

    这一条只有真接口能给形状：录制样本会跟着接口改版一起失效。
    """
    from sams.core.clock import SYSTEM_CLOCK

    out = AkshareHkAdapter().corp_actions("HK", "00700", date(2022, 1, 1), SYSTEM_CLOCK.today())
    kinds_seen = set(out["kind"].to_list())
    assert "dividend" in kinds_seen
    assert "unknown" in kinds_seen, "实物分派被解析成空 = 漏掉一次除权，图上什么都不留下"
    assert out.filter(pl.col("kind") == "unknown")["note"].to_list()
