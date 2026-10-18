"""写侧键形态的一段公共实现：`market` 与 `code` 都按 `core.codes` 的规则归一。

为什么不放 core：core 不认 polars，而这一段的操作对象就是一列一列的帧；为什么不让两个
仓库各写一遍：`bars` 与 `symbols` 过去用 polars 的 `strip_chars().to_uppercase()` 自己实现
了一次 `canonical_market`——同一句话的第二个实现。今天两边算出来的结果恰好一样，所以
没有错数；**而「恰好一样」正是它危险的地方**：`canonical_market` 对空市场名是抛
`ValidationError`，Polars 那版是把它留成空串再由调用方丢弃。哪天域规则多一条（比如 A 股
也补零到 6 位），只有走这个函数的仓库会跟着变，另一条链上的形态就分叉了。

键的一半为空时这里**丢弃并 warn**，不抛：批量入口的一行坏数据不该让另外几千行白采，
而那几千行的价格是好的；读侧与行式入口则照旧抛——那里调用方就在栈的另一头，能立刻改。
"""

from __future__ import annotations

import logging

import polars as pl

from sams.core.codes import canonical_code, canonical_market


def normalize_keys(out: pl.DataFrame, *, logger: logging.Logger, table: str) -> pl.DataFrame:
    """归一 `market` 与 `code`；空的那一半丢弃并说明丢了几行。"""
    trimmed = out.with_columns(
        pl.col("market").cast(pl.Utf8).str.strip_chars(),
        pl.col("code").cast(pl.Utf8).str.strip_chars(),
    )
    for column, reason in (("market", "市场名"), ("code", "代码")):
        kept = trimmed.filter((trimmed[column].str.len_chars() > 0).fill_null(False))
        if kept.height < trimmed.height:
            logger.warning(
                "%s 丢弃 %d 行（%s为空：那一行永远读不到）",
                table,
                trimmed.height - kept.height,
                reason,
            )
        trimmed = kept
    markets = [canonical_market(m) for m in trimmed["market"].to_list()]
    codes = [
        canonical_code(m, c)
        for m, c in zip(markets, trimmed["code"].to_list(), strict=True)
    ]
    return trimmed.with_columns(
        pl.Series("market", markets, dtype=pl.Utf8), pl.Series("code", codes, dtype=pl.Utf8)
    )
