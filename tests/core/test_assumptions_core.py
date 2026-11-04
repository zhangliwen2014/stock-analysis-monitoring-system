"""证伪台账的词表与比较（D232 P0-3）。

词表只有一份：下拉框、校验、页面上的中文名都从这里取。比较方向也只在这里实现一次。
"""

from __future__ import annotations

import pytest

from sams.core.assumptions import (
    METRICS,
    OPS,
    format_value,
    holds,
    metric_label,
    op_label,
)
from sams.core.errors import ValidationError


def test_every_metric_and_op_has_a_label() -> None:
    assert set(METRICS) == {
        "roe",
        "revenue_yoy",
        "profit_yoy",
        "dividend_yield",
        "pe_ttm",
        "pb",
        "pr",
    }
    assert all(label for label in METRICS.values())
    assert set(OPS) == {">=", "<=", ">", "<"}
    assert metric_label("roe") == "净资产收益率"
    assert op_label(">=") == "≥"


@pytest.mark.parametrize(
    ("op", "value", "threshold", "expected"),
    [
        (">=", 0.2, 0.2, True),
        (">=", 0.199, 0.2, False),
        ("<=", 0.2, 0.2, True),
        ("<=", 0.201, 0.2, False),
        (">", 0.2, 0.2, False),
        ("<", 0.199, 0.2, True),
    ],
)
def test_holds_covers_the_four_directions_without_off_by_one(
    op: str, value: float, threshold: float, expected: bool
) -> None:
    assert holds(op, value, threshold) is expected


def test_an_unknown_metric_or_op_is_refused() -> None:
    with pytest.raises(ValidationError):
        metric_label("pe")
    with pytest.raises(ValidationError):
        op_label("=")
    with pytest.raises(ValidationError):
        holds("=", 1.0, 1.0)


def test_values_are_formatted_by_unit_and_empties_stay_empty() -> None:
    assert format_value("roe", 0.1523) == "15.23%"
    assert format_value("pb", 3.456) == "3.46"
    assert format_value("pr", 1.2) == "1.20"
    assert format_value("roe", None) == "—"
