"""日线仓库：幂等 upsert 与读取。

幂等靠 (market, code, date) 主键 + INSERT OR REPLACE，因此采集重试安全（spec §5.3）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import polars as pl

from sams.core.errors import StorageError
from sams.storage.db import Database

logger = logging.getLogger("sams.storage.bars")

REQUIRED = ("market", "code", "date", "close", "source")
COLUMNS = (
    "market",
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover",
    "adj_factor",
    "source",
)


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise StorageError(f"bars_daily 缺少必需列: {missing}")
    out = df
    for col in COLUMNS:
        if col not in out.columns:
            dtype = pl.Date if col == "date" else pl.Float64
            default = 1.0 if col == "adj_factor" else None
            out = out.with_columns(pl.lit(default, dtype=dtype).alias(col))
    out = out.select(list(COLUMNS))
    out = out.with_columns(
        pl.col("market").cast(pl.Utf8),
        pl.col("code").cast(pl.Utf8),
        pl.col("date").cast(pl.Date),
        pl.col("source").cast(pl.Utf8),
    )
    before = out.height
    out = out.filter(pl.col("close").is_not_null() & pl.col("date").is_not_null())
    dropped = before - out.height
    if dropped:
        # 缺收盘价的行不能存成"收盘=0"，但也不静默消失：记录后再丢弃
        logger.warning("bars_daily 丢弃 %d 行（缺收盘价或日期）", dropped)
    return out.unique(subset=["market", "code", "date"], keep="last").sort(
        ["market", "code", "date"]
    )


class BarRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        """写入并返回受影响行数（不含被丢弃的脏行）。"""
        if df.is_empty():
            return 0
        prepared = _prepare(df)
        if prepared.is_empty():
            return 0
        self.db.register("incoming_bars", prepared)
        self.db.execute(
            """
            INSERT OR REPLACE INTO bars_daily
                (market, code, date, open, high, low, close, volume, amount,
                 turnover, adj_factor, source)
            SELECT market, code, date, open, high, low, close, volume, amount,
                   turnover, adj_factor, source
            FROM incoming_bars
            """
        )
        return prepared.height

    def latest_date(self, market: str, code: str) -> date | None:
        value = self.db.scalar(
            "SELECT max(date) FROM bars_daily WHERE market = ? AND code = ?",
            [market, code],
        )
        if value is None:
            return None
        return value.date() if isinstance(value, datetime) else value

    def load(
        self,
        market: str,
        codes: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        if not codes:
            schema = {c: pl.Float64 for c in COLUMNS if c != "date"} | {"date": pl.Date}
            return pl.DataFrame(schema=schema)
        sql = ["SELECT * FROM bars_daily WHERE market = ? AND code = ANY(?)"]
        params: list = [market, codes]
        if start is not None:
            sql.append("AND date >= ?")
            params.append(start)
        if end is not None:
            sql.append("AND date <= ?")
            params.append(end)
        return self.db.query(" ".join(sql) + " ORDER BY code, date", params)

    def count(self, market: str | None = None, code: str | None = None) -> int:
        sql = "SELECT count(*) FROM bars_daily WHERE 1=1"
        params: list = []
        if market:
            sql += " AND market = ?"
            params.append(market)
        if code:
            sql += " AND code = ?"
            params.append(code)
        return int(self.db.scalar(sql, params) or 0)
