"""`price.cross_*` 的另一侧：日线还是前收盘，盘中是同一交易日的上一轮报价。

「盘中不再退回前收盘」那一条是这一枚的核心：上一轮 10.05、线 10.04、现价 10.06，
上一轮 10.05 已在线上方，退回前收盘 10.00 会凭空造出一次上穿。
"""

from __future__ import annotations

from sams.services.rules.snapshot import Snapshot
from sams.services.rules.templates import price_cross_above, price_cross_below


def snap(**over):
    base = {
        "market": "A",
        "code": "601088",
        "currency": "CNY",
        "price": 10.02,
        "prev_close": 10.00,
    }
    base.update(over)
    return Snapshot(**base)


def test_daily_path_still_says_prev_close():
    got = price_cross_above(type("Ask", (), {"snapshot": snap(), "params": {"price": 10.0}})())
    assert got.hit is not None
    assert got.hit.why.startswith("前收盘 10.00")


def test_intraday_uses_the_previous_round_and_says_so():
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(cross_from=9.95), "params": {"price": 10.0}},
    )()
    got = price_cross_above(ask)
    assert got.hit is not None
    assert got.hit.why.startswith("上一轮 9.95")


def test_intraday_does_not_fall_back_to_prev_close():
    # 上一轮 10.05 已在线上方，退回前收盘 10.00 会凭空造出一次上穿。
    # 正确：基准 10.05 → 10.05 <= 10.04 为假，不报；
    # 错误（用 prev_close）：10.00 <= 10.04 < 10.06 为真，报
    ask = type(
        "Ask",
        (),
        {
            "snapshot": snap(price=10.06, cross_from=10.05, prev_close=10.00),
            "params": {"price": 10.04},
        },
    )()
    assert price_cross_above(ask).hit is None


def test_no_baseline_at_all_says_why():
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(prev_close=None, cross_from=None), "params": {"price": 10.0}},
    )()
    got = price_cross_above(ask)
    assert got.hit is None
    assert "另一侧" in got.why_not


def test_downward_cross_uses_the_same_origin():
    ask = type(
        "Ask",
        (),
        {"snapshot": snap(cross_from=10.05, price=10.03), "params": {"price": 10.04}},
    )()
    got = price_cross_below(ask)
    assert got.hit is not None
    assert got.hit.why.startswith("上一轮 10.05")
