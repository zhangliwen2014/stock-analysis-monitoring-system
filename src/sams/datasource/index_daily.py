"""`index_daily` 能力的探针：形状与 `has_bars` 一路，方法名不同而已。"""

from __future__ import annotations


def has_index_daily(obj) -> bool:
    return callable(getattr(obj, "index_daily", None))
