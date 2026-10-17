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
#: list_status 缺省按「在市」；lot_size 没有默认值——港股逐票规定每手股数，
#: 填 1 不是「不知道」而是「知道错了」，M2 的手数换算会差一个数量级。
_DEFAULTS = {"list_status": "L"}


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
    out = out.with_columns(pl.col("list_status").fill_null(_DEFAULTS["list_status"]))
    return out.select(list(COLUMNS)).unique(subset=["market", "code"], keep="last")


class SymbolRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        try:
            prepared = _prepare(df)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — 别让调用方去认 polars 异常
            raise StorageError(f"symbols 写入数据不合法: {exc}") from exc
        self.db.register("incoming_symbols", prepared)
        try:
            self.db.execute(
                """
                INSERT OR REPLACE INTO symbols
                    (market, code, name, currency, exchange, sector, lot_size,
                     list_status, updated_at)
                SELECT market, code, name, currency, exchange, sector, lot_size,
                       list_status, now()
                FROM incoming_symbols
                """
            )
        finally:
            # 不放进 finally 的话，写失败的批次会把视图留在连接上，直到下次同名注册
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
