"""证券主档仓库。

`market` / `code` 的形态在这里落定（D46 的下沉，理由见 bars.py 的同名说明）：主档的键
是 (market, code)，而 list_market 按大写市场名过滤——一个小写形态落库后，这一行在
「列出某市场的所有标的」里永久消失，症状是「这只票不存在」而不是「这行写错了」。
"""

from __future__ import annotations

import logging

import polars as pl

from sams.core.codes import canonical_market
from sams.core.errors import StorageError
from sams.storage.db import Database

logger = logging.getLogger("sams.storage.symbols")

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
    out = out.with_columns(
        pl.col("market").cast(pl.Utf8).str.strip_chars().str.to_uppercase(),
        pl.col("code").cast(pl.Utf8).str.strip_chars().str.to_uppercase(),
    )
    # 键的一半为空，这一行就谁也都读不到：与 bars 同一个处理（丢弃 + warn），
    # 而不是让一条来路不明的空市场名占住主档的一行
    keyed = out.filter(pl.col("market").str.len_chars() > 0)
    if keyed.height < out.height:
        logger.warning(
            "symbols 丢弃 %d 行（市场名为空：那一行永远读不到）", out.height - keyed.height
        )
    # 同一批里有人给了 list_status、有人没给时，缺的那个是 null 而不是默认值：
    # list_market 按 'L' 过滤，null 行会静默消失，所以在这里补平
    keyed = keyed.with_columns(pl.col("list_status").fill_null(_DEFAULTS["list_status"]))
    return keyed.select(list(COLUMNS)).unique(subset=["market", "code"], keep="last")


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
        # 读侧的键形态必须与 `_prepare` 写侧同一条规则，否则「大写过的 code」
        # 存进去、拿原样去查，又是同一个读不到且不报错的洞
        rows = self.db.query(
            "SELECT * FROM symbols WHERE market = ? AND code = ?",
            [canonical_market(market), (code or "").strip().upper()],
        )
        return rows.to_dicts()[0] if not rows.is_empty() else None

    def list_market(self, market: str, *, listed_only: bool = True) -> pl.DataFrame:
        sql = "SELECT * FROM symbols WHERE market = ?"
        params: list = [canonical_market(market)]
        if listed_only:
            sql += " AND list_status = 'L'"
        return self.db.query(sql, params)
