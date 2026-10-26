"""代码表取数的公共形状：源给什么列，出口只留 (code, name) 两列。

`symbol_list(market)` 与 `daily_bars` 的差别是没有时间窗：一次答全表。
空表与缺列都归 `EmptyResponse`——「这源没这张表」与「这张表是空的」对降级链
是同一句话（继续问下一个源），不该攒成 `empty_everywhere` 之外的失败。
"""

from __future__ import annotations

import polars as pl

from sams.core.codes import canonical_code
from sams.core.errors import EmptyResponse


def has_symbol_list(obj) -> bool:
    return callable(getattr(obj, "symbol_list", None))


def empty_symbol_list() -> pl.DataFrame:
    return pl.DataFrame(schema={"code": pl.Utf8, "name": pl.Utf8})


def normalize_symbol_list(market: str, frame: pl.DataFrame) -> pl.DataFrame:
    lowered = {c.lower(): c for c in frame.columns}
    code_col = lowered.get("code") or lowered.get("代码")
    name_col = lowered.get("name") or lowered.get("名称")
    if code_col is None or name_col is None:
        raise EmptyResponse("symbol_list", f"代码表缺 code/name 列：{frame.columns}")
    rows: list[dict] = []
    seen: set[str] = set()
    for row in frame.select(code_col, name_col).iter_rows(named=True):
        raw_code = str(row[code_col] or "").strip()
        raw_name = str(row[name_col] or "").strip()
        if not raw_code:
            continue
        code = canonical_code(market, raw_code)
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": raw_name or None})
    if not rows:
        raise EmptyResponse("symbol_list", f"{market} 代码表归一后为空")
    return pl.DataFrame(rows)
