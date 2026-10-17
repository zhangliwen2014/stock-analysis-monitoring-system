"""日线仓库：幂等 upsert 与读取。

幂等靠 (market, code, date, adjust) 主键 + INSERT OR REPLACE，因此采集重试安全（spec §5.3）。
读取一律带 `adjust = 'raw'`：库里目前只有一种口径，但把口径写进查询而不是靠约定，
是为了让 M2 引入后复权序列时不可能悄悄读到混着两种价的同一根 K 线。

仓库层只挡住两类代价不对称的问题：口径混淆，以及 NaN 覆盖上一根好 K 线。
负成交量/负成交额原样存——那是源侧数据质量问题，属于采集与清洗的职责，
在这里丢弃会让「源给了脏数」和「这一天没数据」在库里长得一样。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from uuid import uuid4

import polars as pl

from sams.core.errors import StorageError
from sams.storage.db import Database

logger = logging.getLogger("sams.storage.bars")

REQUIRED = ("market", "code", "date", "close", "source")
#: 入库唯一口径：不复权。后复权序列在读取层由 corp_actions 现算（0003 迁移的注释里写明理由）。
ADJUST = "raw"
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
    "source",
)

#: 除 close 之外的数值列：出现 NaN/Inf 时置空而不是丢整行——缺成交量不该陪葬一根有效 K 线
_OPTIONAL_NUMERIC = ("open", "high", "low", "volume", "amount")


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise StorageError(f"bars_daily 缺少必需列: {missing}")
    if "adjust" in df.columns:
        raise StorageError(
            "bars_daily 的口径由仓库层固定为 raw；自带 adjust 列的推导序列若在这里入库，"
            "会被静默改名成 raw 并覆盖真正的不复权 K 线（后复权请在读取层由 corp_actions 现算）"
        )
    if df.schema["date"] != pl.Date:
        # cast(pl.Date) 会把带时区的 datetime 一律取 UTC 日历日：A 股 00:30+08:00 存成前一天
        raise StorageError(
            f"bars_daily.date 必须是 pl.Date（交易日），收到 {df.schema['date']}；"
            "带时刻请先按该市场时区取 date"
        )
    out = df
    for col in COLUMNS:
        if col not in out.columns:
            dtype = pl.Date if col == "date" else pl.Float64
            out = out.with_columns(pl.lit(None, dtype=dtype).alias(col))
    out = out.select(list(COLUMNS)).with_columns(
        pl.col("market").cast(pl.Utf8),
        pl.col("code").cast(pl.Utf8),
        pl.col("source").cast(pl.Utf8),
        *[pl.col(c).cast(pl.Float64) for c in ("close", *_OPTIONAL_NUMERIC)],
    )
    before = out.height
    # is_not_null 挡不住 NaN：pandas 侧的「缺数据」正是 NaN，它会通过非空校验并覆盖上一根好 K 线
    out = out.filter(pl.col("close").is_finite() & pl.col("date").is_not_null())
    dropped = before - out.height
    if dropped:
        # 缺收盘价的行不能存成"收盘=0"，但也不静默消失：记录后再丢弃
        logger.warning("bars_daily 丢弃 %d 行（收盘价缺失或非法）", dropped)
    bad = out.select(
        pl.sum_horizontal(
            *[~pl.col(c).is_finite() & pl.col(c).is_not_null() for c in _OPTIONAL_NUMERIC]
        ).sum()
    ).item()
    if bad:
        logger.warning("bars_daily 有 %d 个非有限数值（NaN/Inf）已置空", bad)
    out = out.with_columns(
        pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c)
        for c in _OPTIONAL_NUMERIC
    )
    return out.unique(subset=["market", "code", "date"], keep="last").sort(
        ["market", "code", "date"]
    )


class BarRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        """写入并返回受影响行数（不含被丢弃的脏行）。

        同键后写覆盖先写：数据源回填改数是常态，留两行等于让同一根 K 线有两个收盘价。
        """
        if df.is_empty():
            return 0
        try:
            prepared = _prepare(df)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — 脏列型别让调用方去认 polars 异常
            raise StorageError(f"bars_daily 写入数据不合法: {exc}") from exc
        if prepared.is_empty():
            return 0
        # 名字唯一 + finally 注销：同名视图会被后一批覆盖，异常路径下残留会喂错下一批
        view = f"incoming_bars_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO bars_daily
                    (market, code, date, open, high, low, close, volume, amount,
                     source, updated_at)
                SELECT market, code, date, open, high, low, close, volume, amount,
                       source, now()
                FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def latest_date(self, market: str, code: str) -> date | None:
        value = self.db.scalar(
            "SELECT max(date) FROM bars_daily WHERE adjust = ? AND market = ? AND code = ?",
            [ADJUST, market, code],
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
        sql = ["SELECT * FROM bars_daily WHERE adjust = ? AND market = ? AND code = ANY(?)"]
        params: list = [ADJUST, market, codes]
        if start is not None:
            sql.append("AND date >= ?")
            params.append(start)
        if end is not None:
            sql.append("AND date <= ?")
            params.append(end)
        return self.db.query(" ".join(sql) + " ORDER BY code, date", params)

    def count(self, market: str | None = None, code: str | None = None) -> int:
        # 与 load/latest_date 同一个口径过滤，否则行数会算进库里另一套复权序列
        sql = "SELECT count(*) FROM bars_daily WHERE adjust = ?"
        params: list = [ADJUST]
        if market:
            sql += " AND market = ?"
            params.append(market)
        if code:
            sql += " AND code = ?"
            params.append(code)
        return int(self.db.scalar(sql, params) or 0)
