"""决策卡 v0（S1-A）：硬闸、买入口径/持仓口径、赔率区间与四态结论。

这一层只回答「此刻该不该动手」，不算估值本身：合理区间、止损方法、目标候选都来自
`services/valuation` 的那一份读数。四个静默出错的口子都在这里：

1. **闸门先于结论**。日线不足、报价超期、财务超期时，卡片只能出「数据不足，不出结论」；
   拿半个计划给一个 R，比不给结论更糟。
2. **买入口径与持仓口径不是同一个止损**。模板里的 pct 止损按现价算，现价高于买入区时
   它会落在买入区之上；买入口径必须按买入区中值重算，否则 R 的分母是负的。
3. **成本与止损距离要进公式**。不扣成本、不判「止损过近」，赔率就是一张自证纸。
4. **证伪优先于价格**。价格在买入区 + 假设被证伪，输出不追/退出，绝不输出可分批。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
import support_valuation as world

from sams.analysis.valuation.styles import VALUE
from sams.core.assumptions import REFUTED
from sams.services.decision import CardQuote, odds_range, trading_days_between
from sams.storage.repos.financials import FinancialRepo

USER = "trader"
PWD = "correct-horse-battery"


class FakeCalendar:
    """只答 `known_status`：闸门数交易日时不该顺手去补日历。"""

    def __init__(self, statuses: dict[date, bool] | None = None) -> None:
        self.statuses = statuses or {}

    def known_status(self, market: str, day: date) -> bool | None:
        return self.statuses.get(day)


# --- 赔率区间 -----------------------------------------------------------------------------


def test_odds_range_uses_both_ends_of_the_entry_zone_and_charges_costs():
    """黄金手算：T=19.40、买入区 12.29~13.58、S=11.30、成本 0.3%。

    R_low 用更高的买入价（赔率更差），R_high 用更低的买入价；两端都扣成本。
    """
    odds = odds_range(
        t_low=19.40,
        p_low=12.29,
        p_high=13.58,
        stop=11.30,
        cost_pct=0.003,
        reference=12.935,
        atr=0.20,
    )
    assert odds.valid
    ch = 0.003 * 13.58
    cl = 0.003 * 12.29
    assert odds.low == pytest.approx((19.40 - 13.58 - ch) / (13.58 - 11.30 + ch))
    assert odds.high == pytest.approx((19.40 - 12.29 - cl) / (12.29 - 11.30 + cl))
    assert odds.low == pytest.approx(2.4903, rel=1e-4)
    assert "0.30%" in odds.evidence


def test_odds_range_refuses_a_stop_that_is_too_close():
    """止损距离 < max(3%, 1×ATR14)：分母接近 0，R 会被算成一个好看的大数。"""
    odds = odds_range(
        t_low=15.0,
        p_low=13.20,
        p_high=13.50,
        stop=13.00,
        cost_pct=0.003,
        reference=13.35,
        atr=0.40,
    )
    assert not odds.valid
    assert odds.low is None and odds.high is None
    assert "止损过近" in odds.reason


def test_odds_range_refuses_a_stop_not_below_the_entry_zone():
    """模板的 pct 止损按现价算，现价高于买入区时会落在买入区之上。"""
    odds = odds_range(
        t_low=19.40,
        p_low=12.29,
        p_high=13.58,
        stop=15.64,
        cost_pct=0.003,
        reference=12.935,
        atr=0.20,
    )
    assert not odds.valid
    assert "买入区" in odds.reason


def test_odds_range_refuses_a_target_below_the_entry_zone():
    odds = odds_range(
        t_low=13.00,
        p_low=12.29,
        p_high=13.58,
        stop=11.30,
        cost_pct=0.003,
        reference=12.935,
        atr=0.20,
    )
    assert not odds.valid
    assert "目标" in odds.reason


def test_costs_make_the_odds_lower_than_the_frictionless_version():
    cheap = odds_range(
        t_low=19.40,
        p_low=12.29,
        p_high=13.58,
        stop=11.30,
        cost_pct=0.0,
        reference=12.935,
        atr=0.20,
    )
    charged = odds_range(
        t_low=19.40,
        p_low=12.29,
        p_high=13.58,
        stop=11.30,
        cost_pct=0.003,
        reference=12.935,
        atr=0.20,
    )
    assert charged.low < cheap.low
    assert charged.high < cheap.high


# --- 报价新鲜度 ---------------------------------------------------------------------------


def test_quote_age_counts_trading_days_after_the_quote_day():
    """周五收盘、周一来看：只过了周一这一个交易日，不是三个日历日。"""
    friday = date(2024, 10, 25)
    monday = date(2024, 10, 28)
    assert trading_days_between(FakeCalendar(), "A", friday, monday) == 1


def test_quote_age_skips_weekends_and_known_holidays():
    thursday = date(2024, 10, 24)
    monday = date(2024, 10, 28)
    calendar = FakeCalendar({date(2024, 10, 25): False})  # 周五休市
    assert trading_days_between(calendar, "A", thursday, monday) == 1


def test_quote_age_treats_an_unknown_weekday_as_trading():
    """日历没铺到的年份按工作日保守计：宁可早一点判超期，不可晚判。"""
    monday = date(2025, 1, 6)
    friday = date(2025, 1, 3)
    assert trading_days_between(FakeCalendar(), "A", friday, monday) == 1


def test_quote_age_same_day_is_zero():
    day = date(2024, 10, 28)
    assert trading_days_between(FakeCalendar(), "A", day, day) == 0


# --- 卡片：硬闸 ---------------------------------------------------------------------------


def _quote(price=17.0, day=world.TODAY, *, source="akshare", kind="close") -> CardQuote:
    return CardQuote(
        price=price,
        day=day,
        source=source,
        kind=kind,
        state="closed",
        label=f"{day:%m-%d} 收盘",
        detail=f"不复权 · {source}",
    )


def _fresh_financials(db, market="A", code=world.CODE) -> None:
    """一份八月披露的中报：PR 仍用年报口径，但「有没有新财报」看的是这一期。"""
    FinancialRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "report_date": date(2024, 6, 30),
                    "published_date": date(2024, 8, 30),
                    "roe": world.ROE_NOW,
                    "revenue_yoy": 0.10,
                    "profit_yoy": 0.10,
                    "dividend_yield": 0.04,
                    "market_cap": world.MARKET_CAP,
                    "source": "akshare",
                }
            ]
        )
    )


def _world(db, settings, *, fresh=True, **seeded):
    container = world.make_container(db, settings, **seeded)
    container.users.create(USER, PWD)
    if fresh:
        _fresh_financials(db)
    user = container.users.get_by_username(USER)
    return container, user


def _hold(container, user, market="A", code=world.CODE, *, qty=100.0, price=17.0):
    currency = {"A": "CNY", "HK": "HKD", "US": "USD"}[market]
    account = container.sim_accounts.open(user.id, f"账户 {market}", report_currency="CNY")
    container.sim_accounts.adjust_cash(
        account.id, currency, 1_000_000.0, reason="初始", actor_id=user.id
    )
    container.sim_ledger.record_fill(
        account_id=account.id,
        user_id=user.id,
        market=market,
        code=code,
        side="buy",
        qty=qty,
        price=price,
        currency=currency,
    )
    return account


def test_under_300_bars_only_says_insufficient(db, settings):
    container, user = _world(
        db, settings, days=world.DAYS[:100], closes=world.CLOSES[:100]
    )
    card = container.decision.card(user.id, "A", world.CODE, _quote(13.0))
    assert card.data_ok is False
    assert card.conclusion == "证据不足"
    assert any("日线" in gate for gate in card.gates)
    assert card.stop is None and card.target_used is None
    assert not card.odds.valid


def test_empty_bars_still_shows_the_quote_but_no_conclusion(db, settings):
    container, user = _world(db, settings, with_bars=False)
    card = container.decision.card(user.id, "A", world.CODE, _quote(13.0))
    assert card.data_ok is False
    assert card.conclusion == "证据不足"
    assert card.quote.price == 13.0


def test_stale_quote_names_the_quote_gate(db, settings):
    container, user = _world(db, settings)
    card = container.decision.card(
        user.id, "A", world.CODE, _quote(13.0, date(2024, 10, 10))
    )
    assert card.data_ok is False
    assert any("报价" in gate for gate in card.gates)
    assert not any("财报" in gate for gate in card.gates)


def test_stale_financials_name_the_financial_gate(db, settings):
    container, user = _world(db, settings, fresh=False)
    card = container.decision.card(user.id, "A", world.CODE, _quote(17.0))
    assert card.data_ok is False
    assert any("财报" in gate for gate in card.gates)
    assert card.stop is None and card.odds.low is None


def test_a_fresh_interim_report_opens_the_financial_gate(db, settings):
    container, user = _world(db, settings)
    card = container.decision.card(user.id, "A", world.CODE, _quote(17.0))
    assert card.data_ok is True
    assert card.gates == ()


# --- 卡片：买入口径 -----------------------------------------------------------------------


def test_price_in_the_entry_zone_with_good_R_says_starter(db, settings):
    container, user = _world(db, settings)
    card = container.decision.card(user.id, "A", world.CODE, _quote(13.0))
    assert card.data_ok is True
    assert card.held == ()
    assert card.conclusion == "可分批建仓"
    assert card.entry_low == pytest.approx(12.2872, rel=1e-4)
    assert card.entry_high == pytest.approx(13.5806, rel=1e-4)
    # 买入口径的止损按买入区中值重算，不是按现价 13.0 算
    assert card.stop == pytest.approx(12.9339 * 0.92, rel=1e-4)
    assert card.stop_method == "pct"
    assert card.target_used == pytest.approx(19.4009, rel=1e-4)
    assert card.odds.low == pytest.approx(3.356, rel=1e-3)
    assert card.odds.high == pytest.approx(16.656, rel=1e-3)


def test_price_above_the_entry_zone_says_wait(db, settings):
    container, user = _world(db, settings)
    card = container.decision.card(user.id, "A", world.CODE, _quote(17.0))
    assert card.conclusion == "等价格"
    assert "买入区上限" in card.reason


def test_price_above_the_target_says_not_chase(db, settings):
    """价格越过目标不是「更强的可买信号」：目标下沿之上就不再给建仓结论。"""
    container, user = _world(db, settings)
    card = container.decision.card(user.id, "A", world.CODE, _quote(20.0))
    assert card.conclusion == "不追"
    assert "目标" in card.reason


def test_price_at_or_below_the_entry_stop_says_not_chase(db, settings):
    container, user = _world(db, settings)
    card = container.decision.card(user.id, "A", world.CODE, _quote(11.0))
    assert card.conclusion == "不追"
    assert "止损" in card.reason


def test_price_in_the_entry_zone_but_R_below_two_says_wait(db, settings):
    """买入区到了不等于可建仓：R_low < 2 时只能说等，不能给「可分批」。"""
    container, user = _world(db, settings)
    thin = VALUE.with_overrides(params={"fair_pr_high": 1.02})
    card = container.decision.card(user.id, "A", world.CODE, _quote(13.0), style=thin)
    assert card.odds.valid
    assert card.odds.low < 2.0
    assert card.conclusion == "等价格"


def test_a_refuted_assumption_overrides_the_price_and_says_not_chase(db, settings):
    container, user = _world(db, settings)
    thesis = container.theses.add(USER, "A", world.CODE).thesis
    saved = container.assumptions.add(
        USER, thesis.id, metric="roe", op=">=", threshold=0.15, due_on=world.TODAY
    )
    container.assumptions_store.record(
        saved.id, status=REFUTED, value=0.10, on=world.TODAY, now=container.clock.now_utc()
    )
    card = container.decision.card(user.id, "A", world.CODE, _quote(13.0))
    assert card.conclusion == "不追"
    assert [item.id for item in card.refuted] == [saved.id]


# --- 卡片：持仓口径 -----------------------------------------------------------------------


def test_a_held_position_uses_the_position_stop_and_says_hold(db, settings):
    container, user = _world(db, settings)
    _hold(container, user)
    card = container.decision.card(user.id, "A", world.CODE, _quote(17.0))
    assert card.data_ok is True
    assert [item.qty for item in card.held] == [100.0]
    # 持仓口径用按现价算的那一档，不是买入区中值
    assert card.stop == pytest.approx(17.0 * 0.92, rel=1e-6)
    assert card.conclusion == "持有"


def test_price_at_the_target_says_reduce(db, settings):
    container, user = _world(db, settings)
    _hold(container, user)
    card = container.decision.card(user.id, "A", world.CODE, _quote(19.5))
    assert card.conclusion == "减仓"


def test_price_at_the_stop_says_exit(db, settings):
    container, user = _world(db, settings)
    _hold(container, user)
    card = container.decision.card(user.id, "A", world.CODE, _quote(15.0))
    assert card.conclusion == "退出"


def test_a_refuted_assumption_exits_a_held_position(db, settings):
    container, user = _world(db, settings)
    _hold(container, user)
    thesis = container.theses.add(USER, "A", world.CODE).thesis
    saved = container.assumptions.add(
        USER, thesis.id, metric="roe", op=">=", threshold=0.15, due_on=world.TODAY
    )
    container.assumptions_store.record(
        saved.id, status=REFUTED, value=0.10, on=world.TODAY, now=container.clock.now_utc()
    )
    card = container.decision.card(user.id, "A", world.CODE, _quote(17.0))
    assert card.conclusion == "退出"


# --- 卡片：港美降级 -----------------------------------------------------------------------


def test_a_hk_card_says_the_valuation_chain_is_missing(db, settings):
    container, user = _world(db, settings)
    world.seed(db, market="HK", code="00700")
    card = container.decision.card(user.id, "HK", "00700", _quote(17.0))
    assert card.data_ok is True
    assert card.degraded is True
    assert card.conclusion == "证据不足"
    assert not card.odds.valid
    assert "估值链" in card.odds.reason


def test_a_held_hk_position_still_answers_with_the_thesis_levels(db, settings):
    container, user = _world(db, settings)
    world.seed(db, market="HK", code="00700")
    _hold(container, user, "HK", "00700")
    thesis = container.theses.add(USER, "HK", "00700").thesis
    container.theses.revise(USER, thesis.id, stop_price=15.0, target_price=20.0)
    held = container.decision.card(user.id, "HK", "00700", _quote(17.0))
    assert held.conclusion == "持有"
    reduce = container.decision.card(user.id, "HK", "00700", _quote(21.0))
    assert reduce.conclusion == "减仓"
    exit_ = container.decision.card(user.id, "HK", "00700", _quote(14.0))
    assert exit_.conclusion == "退出"
