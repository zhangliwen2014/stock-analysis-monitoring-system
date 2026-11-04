"""资讯流的落库与读取（S3-A，D244）。

两件在这里定死的事：

1. **去重键是 `(source, item_id)`**：同一篇文章在不同源出现算两条，跨源不合并。
   源没给 id 时用 `(title, published_at)` 的 SHA1 指纹——同一源内稳定；
   拿「当前时间」或自增号当 id，会让每次同步都多出一条「看起来是新的旧闻」。
2. **`fetched_at` 是「我们什么时候抓的」**：由注入的时钟写，不是源给的业务时间。
   关键词只对入库之后的新资讯生效（D245），所以 `since` 看的是 `fetched_at`，
   不是 `published_at`——一篇昨天发布的文章今天才采到，对用户就是今天的提醒。

标题或原文链接为空的行直接丢弃并 warn：`title`/`url` 都是 NOT NULL，留着只会在
入库时炸掉整批；而一条没有链接的资讯，用户点不开，也就无法核对。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.repos._keys import normalize_keys

logger = logging.getLogger("sams.storage.news")

TABLE = "news_items"
COLUMNS = (
    "market",
    "code",
    "source",
    "item_id",
    "published_at",
    "title",
    "summary",
    "url",
)
REQUIRED = ("market", "code", "source", "published_at", "title", "url")
KEY = ("source", "item_id")


def _fingerprint(title: str, published_at: datetime) -> str:
    return hashlib.sha1(f"{title}|{published_at.isoformat()}".encode()).hexdigest()


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise StorageError(f"{TABLE} 缺少必需列: {missing}")
    unknown = [c for c in df.columns if c not in COLUMNS]
    if unknown:
        raise StorageError(
            f"{TABLE} 的列名单里没有 {unknown}：这一列会被丢掉而你以为写成功了"
        )
    out = df
    for column in COLUMNS:
        if column not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias(column))
    out = out.select(list(COLUMNS))
    dtype = out.schema["published_at"]
    if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
        raise StorageError(
            f"{TABLE}.published_at 必须是带时区的 datetime，收到 {dtype}；"
            "源的时间先按该市场时区换算，不要拿本地时间冒充 UTC"
        )
    out = out.with_columns(pl.col("published_at").cast(pl.Datetime("us", "UTC")))
    for column in ("source", "item_id", "title", "summary", "url"):
        out = out.with_columns(pl.col(column).cast(pl.Utf8, strict=False))
    out = normalize_keys(out, logger=logger, table=TABLE)

    blank_title = pl.col("title").is_null() | (
        pl.col("title").str.strip_chars().str.len_chars() == 0
    )
    blank_url = pl.col("url").is_null() | (
        pl.col("url").str.strip_chars().str.len_chars() == 0
    )
    dropped = int(out.select((blank_title | blank_url).sum()).item() or 0)
    if dropped:
        logger.warning(
            "%s 丢弃 %d 行（标题或原文链接为空：写进去也读不出是什么、点不开）",
            TABLE,
            dropped,
        )
        out = out.filter(~(blank_title | blank_url))
    if out.is_empty():
        return out

    ids = []
    for row in out.iter_rows(named=True):
        item_id = (row["item_id"] or "").strip()
        if not item_id:
            item_id = _fingerprint(row["title"].strip(), row["published_at"])
        ids.append(item_id)
    out = out.with_columns(pl.Series("item_id", ids, dtype=pl.Utf8))
    return out.unique(subset=list(KEY), keep="last")


class NewsRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def upsert_many(self, df: pl.DataFrame) -> int:
        """写入并返回**新增**行数：重同步同一篇不是「又入库一条」。

        先数「这批键里库里已有的」，再 `INSERT OR REPLACE`：覆盖已有行会刷新
        `fetched_at`，但行数不变；调用方拿到的数就是真正多出来的资讯。
        """
        written, _ = self.upsert_new(df)
        return written

    def upsert_new(self, df: pl.DataFrame) -> tuple[int, pl.DataFrame]:
        """落库，并把**这一趟新出现的行**原样交回调用方（关键词只对这些生效）。

        返回的不是「问回来的全部行」：已存在的键重同步时也会被覆盖，但它们不是
        今天的提醒。关键词匹配只认这份差集，所以「加词后重跑」不会把旧闻翻出来。
        """
        prepared = _prepare(df)
        if prepared.is_empty():
            return 0, prepared
        view = f"incoming_news_{uuid4().hex[:8]}"
        self.db.register(view, prepared)
        columns = ", ".join(COLUMNS)
        try:
            fresh = self.db.query(
                f"SELECT v.* FROM {view} v ANTI JOIN {TABLE} n"
                " ON n.source = v.source AND n.item_id = v.item_id"
            )
            self.db.execute(
                f"""
                INSERT OR REPLACE INTO {TABLE} ({columns}, fetched_at)
                SELECT {columns}, ? FROM {view}
                """,
                [self.clock.now_utc()],
            )
        finally:
            self.db.unregister(view)
        return fresh.height, fresh

    def recent(
        self,
        market: str,
        codes: Sequence[str],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> pl.DataFrame:
        """一批标的的资讯，按发布时刻倒序；页面与个股卡共用这一条查询。"""
        canonical = [canonical_code(market, code) for code in codes]
        if not canonical:
            return self.db.query(f"SELECT * FROM {TABLE} WHERE FALSE")
        placeholders = ", ".join("?" for _ in canonical)
        clauses = ["market = ?", f"code IN ({placeholders})"]
        params: list[object] = [canonical_market(market), *canonical]
        if since is not None:
            clauses.append("published_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("published_at < ?")
            params.append(until)
        params.append(int(limit))
        return self.db.query(
            f"SELECT * FROM {TABLE} WHERE {' AND '.join(clauses)}"
            f" ORDER BY published_at DESC, item_id DESC LIMIT ?",
            params,
        )

    def latest(self, market: str, code: str, *, limit: int = 20) -> pl.DataFrame:
        return self.db.query(
            f"SELECT * FROM {TABLE} WHERE market = ? AND code = ?"
            f" ORDER BY published_at DESC, item_id DESC LIMIT {int(limit)}",
            [canonical_market(market), canonical_code(market, code)],
        )

    def since(self, as_of: datetime) -> pl.DataFrame:
        """入库时间不早于 `as_of` 的资讯（关键词只对这些生效）。"""
        return self.db.query(
            f"SELECT * FROM {TABLE} WHERE fetched_at >= ?"
            " ORDER BY published_at DESC, item_id DESC",
            [as_of],
        )

    def prune(self, *, days: int) -> int:
        if days < 1:
            raise ValidationError(f"days={days}：至少要 1；0 会把当前这一份全删掉")
        cutoff = self.clock.now_utc() - timedelta(days=days)
        with self.db.transaction():
            count = int(
                self.db.scalar(f"SELECT count(*) FROM {TABLE} WHERE published_at < ?", [cutoff])
                or 0
            )
            if count:
                self.db.execute(f"DELETE FROM {TABLE} WHERE published_at < ?", [cutoff])
        return count

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT count(*) FROM {TABLE}") or 0)
