"""公司概况与公告两张表的写入与读取（D214 / D216）。

两件在这里定死的事：

1. **键形态**（D50）：`market`/`code` 在入口归一，`HK/700` 与 `HK/00700` 不能各存
   一行。否则个股页按一种写法读、采集按另一种写，屏幕上说「还没采集」。
2. **空值不冒充**：源没给的格留 NULL，不拿 `—`、`0` 或空串填。概况一行里「员工数
   不知道」与「员工数是 0」在屏幕上是两个意思；公告标题为空的行直接丢弃并 warn——
   `title` 是 NOT NULL，留着它只会在入库时炸掉整批。

`fetched_at` 由 SQL `now()` 写：它是「我们什么时候抓的」，不是源给的业务时间，
所以仓库不接受调用方传时钟。
"""

from __future__ import annotations

import logging
from uuid import uuid4

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError
from sams.core.f10 import (
    ANNOUNCEMENT_COLUMNS,
    ANNOUNCEMENT_DATE_COLUMNS,
    PROFILE_BOOL_COLUMNS,
    PROFILE_COLUMNS,
    PROFILE_DATE_COLUMNS,
    PROFILE_INT_COLUMNS,
)
from sams.storage.db import Database
from sams.storage.repos._keys import normalize_keys

logger = logging.getLogger("sams.storage.f10")


def _dtype(
    column: str,
    dates: tuple[str, ...],
    ints: tuple[str, ...],
    bools: tuple[str, ...],
) -> pl.DataType:
    if column in dates:
        return pl.Date
    if column in ints:
        return pl.Int64
    if column in bools:
        return pl.Boolean
    return pl.Utf8


def _typed(
    df: pl.DataFrame,
    *,
    columns: tuple[str, ...],
    required: tuple[str, ...],
    dates: tuple[str, ...],
    ints: tuple[str, ...],
    bools: tuple[str, ...],
    key: tuple[str, ...],
    table: str,
) -> pl.DataFrame:
    """补齐缺列、定型、归一键——两张表共用这一段。"""
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise StorageError(f"{table} 缺少必需列: {missing}")
    unknown = [c for c in df.columns if c not in columns]
    if unknown:
        raise StorageError(
            f"{table} 的列名单里没有 {unknown}：这一列会被丢掉而你以为写成功了"
        )
    out = df
    for column in columns:
        if column not in out.columns:
            out = out.with_columns(
                pl.lit(None, dtype=_dtype(column, dates, ints, bools)).alias(column)
            )
    out = out.select(list(columns))
    for column in dates:
        if out.schema[column] != pl.Date:
            # 带时区的 datetime 直接 cast 会取 UTC 日历日：A 股 00:30+08:00 会存成前一天
            raise StorageError(
                f"{table}.{column} 必须是 pl.Date，收到 {out.schema[column]}；"
                "带时刻请先按该市场时区取 date"
            )
    for column in ints:
        out = out.with_columns(pl.col(column).cast(pl.Int64, strict=False))
    for column in bools:
        out = out.with_columns(pl.col(column).cast(pl.Boolean, strict=False))
    textual = [c for c in columns if c not in (*dates, *ints, *bools)]
    out = out.with_columns(pl.col(c).cast(pl.Utf8, strict=False) for c in textual)
    out = normalize_keys(out, logger=logger, table=table)
    return out.unique(subset=list(key), keep="last")


class CompanyProfileRepo:
    """一票一行的公司概况。重复执行是覆盖，不是两条历史。"""

    TABLE = "company_profiles"
    KEY = ("market", "code")
    REQUIRED = ("market", "code", "source")

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        prepared = _typed(
            df,
            columns=PROFILE_COLUMNS,
            required=self.REQUIRED,
            dates=PROFILE_DATE_COLUMNS,
            ints=PROFILE_INT_COLUMNS,
            bools=PROFILE_BOOL_COLUMNS,
            key=self.KEY,
            table=self.TABLE,
        )
        if prepared.is_empty():
            return 0
        view = f"incoming_profiles_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        columns = ", ".join(PROFILE_COLUMNS)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE} ({columns}, fetched_at)
                SELECT {columns}, now() FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def get(self, market: str, code: str) -> pl.DataFrame:
        return self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND code = ?",
            [canonical_market(market), canonical_code(market, code)],
        )

    def has(self, market: str, code: str) -> bool:
        return bool(
            self.db.scalar(
                f"SELECT count(*) FROM {self.TABLE} WHERE market = ? AND code = ?",
                [canonical_market(market), canonical_code(market, code)],
            )
        )

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT count(*) FROM {self.TABLE}") or 0)


class AnnouncementRepo:
    """公告元数据，按 (market, code, art_code) 幂等；正文与 PDF 留外链（D216）。"""

    TABLE = "announcements"
    KEY = ("market", "code", "art_code")
    REQUIRED = ("market", "code", "art_code", "published_on", "title", "source")

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, df: pl.DataFrame) -> int:
        if df.is_empty():
            return 0
        prepared = _typed(
            df,
            columns=ANNOUNCEMENT_COLUMNS,
            required=self.REQUIRED,
            dates=ANNOUNCEMENT_DATE_COLUMNS,
            ints=(),
            bools=(),
            key=self.KEY,
            table=self.TABLE,
        )
        blank = pl.col("title").is_null() | (
            pl.col("title").str.strip_chars().str.len_chars() == 0
        )
        dropped = int(prepared.select(blank.sum()).item() or 0)
        if dropped:
            logger.warning(
                "%s 丢弃 %d 行公告（标题为空，写进去也读不出是什么）", self.TABLE, dropped
            )
            prepared = prepared.filter(~blank)
        if prepared.is_empty():
            return 0
        view = f"incoming_announcements_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        columns = ", ".join(ANNOUNCEMENT_COLUMNS)
        try:
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE} ({columns}, fetched_at)
                SELECT {columns}, now() FROM {view}
                """
            )
        finally:
            self.db.unregister(view)
        return prepared.height

    def latest(
        self, market: str, code: str, *, limit: int = 10
    ) -> pl.DataFrame:
        """最近几条公告，日期倒序。只给个股页看，不参与任何计算。"""
        return self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND code = ?"
            f" ORDER BY published_on DESC, art_code DESC LIMIT {int(limit)}",
            [canonical_market(market), canonical_code(market, code)],
        )

    def count(self, market: str | None = None) -> int:
        sql = f"SELECT count(*) FROM {self.TABLE}"
        params: list = []
        if market:
            sql += " WHERE market = ?"
            params.append(canonical_market(market))
        return int(self.db.scalar(sql, params) or 0)
