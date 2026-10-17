"""证券主档仓库。"""

from __future__ import annotations

import polars as pl

from sams.core.errors import StorageError
from sams.storage.db import Database

REQUIRED = ("market", "code", "currency")
COLUMNS = (
    "market",
    "code",
    "name",
    "currency",
    "exchange",
    "sector",
    "lot_size",
    "list_status",
)
_DEFAULTS = {"list_status": "L", "lot_size": 1}


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise StorageError(f"symbols 缺少必需列: {missing}")
    out = df.select(list(df.columns))
    for col in COLUMNS:
        if col not in out.columns:
            dtype = pl.Int64 if col == "lot_size" else pl.Utf8
            out = out.with_columns(pl.lit(_DEFAULTS.get(col), dtype=dtype).alias(col))
    # 同一批里有人给了 list_status、有人没给时，缺的那个是 null 而不是默认值：
    # list_market 按 'L' 过滤，null 行会静默消失，所以在这里补平
    out = out.with_columns(
        pl.col("list_status").fill_null(_DEFAULTS["list_status"]),
        pl.col("lot_size").fill_null(_DEFAULTS["lot_size"]),
    )
    return out.select(list(COLUMNS)).unique(subset=["market", "code"], keep="last")


class SymbolRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        prepared = _prepare(df)
        self.db.register("incoming_symbols", prepared)
        self.db.execute(
            """
            INSERT OR REPLACE INTO symbols
                (market, code, name, currency, exchange, sector, lot_size, list_status)
            SELECT market, code, name, currency, exchange, sector, lot_size, list_status
            FROM incoming_symbols
            """
        )
        self.db.unregister("incoming_symbols")
        return prepared.height

    def get(self, market: str, code: str) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM symbols WHERE market = ? AND code = ?", [market, code]
        )
        return rows.to_dicts()[0] if not rows.is_empty() else None

    def list_market(self, market: str, *, listed_only: bool = True) -> pl.DataFrame:
        sql = "SELECT * FROM symbols WHERE market = ?"
        params: list = [market]
        if listed_only:
            sql += " AND list_status = 'L'"
        return self.db.query(sql, params)
