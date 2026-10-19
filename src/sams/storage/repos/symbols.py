"""证券主档仓库。

`market` / `code` 的形态在这里落定（D46 的下沉，理由见 bars.py 的同名说明）：主档的键
是 (market, code)，而 list_market 按大写市场名过滤——一个小写形态落库后，这一行在
「列出某市场的所有标的」里永久消失，症状是「这只票不存在」而不是「这行写错了」。

**写入是「补上知道的」，不是「重写整行」**（`listed_on` 逼出来的这条）：主档在每次日线
采集里都会被写一遍，而过去那句 `INSERT OR REPLACE` 是删了再插——不在插入列表里的列一律
回默认值。于是基本面源填进来的上市日，会在下一次采集时被抹回 NULL，D37 的上市豁免从此
每天早晨失效一次，屏幕上还说一切正常。所以这里的冲突处理逐列 `COALESCE`：新值优先，
没给的那一列保留库里已有的答案。
"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError
from sams.storage.db import Database
from sams.storage.repos._keys import normalize_keys

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
    "listed_on",
)
#: list_status 缺省按「在市」；lot_size 没有默认值——港股逐票规定每手股数，
#: 填 1 不是「不知道」而是「知道错了」，M2 的手数换算会差一个数量级。
_DEFAULTS = {"list_status": "L"}
#: 只有这两列不是文本：`listed_on` 是日期（0012 迁移加的列，U18 的那一格）
_DTYPES = {"lot_size": pl.Int64, "listed_on": pl.Date}


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise StorageError(f"symbols 缺少必需列: {missing}")
    out = df.select(list(df.columns))
    for col in COLUMNS:
        if col not in out.columns:
            out = out.with_columns(
                pl.lit(_DEFAULTS.get(col), dtype=_DTYPES.get(col, pl.Utf8)).alias(col)
            )
    out = normalize_keys(out, logger=logger, table="symbols")
    # 调用方给的是字符串（源的 F10 页就是这么写的）时也要落进 DATE 列：在这里 cast，
    # 而不是让 DuckDB 在插入时报一句不带列名的转换错误
    out = out.with_columns(pl.col("listed_on").cast(pl.Date, strict=True))
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
                INSERT INTO symbols
                    (market, code, name, currency, exchange, sector, lot_size,
                     list_status, listed_on, updated_at)
                SELECT market, code, name, currency, exchange, sector, lot_size,
                       list_status, listed_on, now()
                FROM incoming_symbols
                ON CONFLICT (market, code) DO UPDATE SET
                    name      = COALESCE(EXCLUDED.name, symbols.name),
                    currency  = COALESCE(EXCLUDED.currency, symbols.currency),
                    exchange  = COALESCE(EXCLUDED.exchange, symbols.exchange),
                    sector    = COALESCE(EXCLUDED.sector, symbols.sector),
                    lot_size  = COALESCE(EXCLUDED.lot_size, symbols.lot_size),
                    list_status = COALESCE(EXCLUDED.list_status, symbols.list_status),
                    listed_on = COALESCE(EXCLUDED.listed_on, symbols.listed_on),
                    updated_at = now()
                """
            )
        finally:
            # 不放进 finally 的话，写失败的批次会把视图留在连接上，直到下次同名注册
            self.db.unregister("incoming_symbols")
        return prepared.height

    def fill_listed_on(self, market: str, code: str, day: date) -> bool:
        """只在「还不知道」那一天填上市日，返回这次有没有写进去。

        填一次而不是每次覆盖：换一家源就可能换一天（有的给申购日、有的给上市日），
        而 D37 的豁免窗口跟着这个日子走——豁免的日子不许取决于今天问了哪一家。
        """
        # DuckDB 对 UPDATE 回一列 Count，就是这次真正改写的行数
        written = self.db.scalar(
            "UPDATE symbols SET listed_on = ?, updated_at = now() "
            "WHERE market = ? AND code = ? AND listed_on IS NULL",
            [day, canonical_market(market), canonical_code(market, code)],
        )
        return bool(written)

    def get(self, market: str, code: str) -> dict | None:
        # 读侧的键形态必须与 `_prepare` 写侧同一条规则，否则「归一过的 code」
        # 存进去、拿原样去查，又是同一个读不到且不报错的洞
        rows = self.db.query(
            "SELECT * FROM symbols WHERE market = ? AND code = ?",
            [canonical_market(market), canonical_code(market, code)],
        )
        return rows.to_dicts()[0] if not rows.is_empty() else None

    def list_market(self, market: str, *, listed_only: bool = True) -> pl.DataFrame:
        sql = "SELECT * FROM symbols WHERE market = ?"
        params: list = [canonical_market(market)]
        if listed_only:
            sql += " AND list_status = 'L'"
        return self.db.query(sql, params)
