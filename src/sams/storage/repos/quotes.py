"""报价快照与报价源健康。

两张表各答一个问题：

* `quotes_snapshot` 是历史。每轮每只一行，保留 5 个交易日（`sams db prune` 清）。
  行情板只关心「每只最近一笔」，由 `latest()` 的窗口查询给出——把整张历史拉进
  内存再挑，第一天就会变成几万行的读。
* `quote_source_health` 是报价链路自己的健康（D114）。写进日线的 `source_health`
  会让盘后健康页把日线源标红，而那条链可能完全正常；反过来也一样。

读侧对坏行是宽容的：`price` 为 NULL/0 的快照不构造成 `Quote`。行情板启动时读这张表，
一行历史脏数据不该让整个看板起不来，也不该把 0 当成报价。
"""

from __future__ import annotations

from collections.abc import Iterable

from sams.core.quotes import Quote, QuoteSourceRecord
from sams.storage.db import Database

_QUOTE_COLUMNS = (
    "market, code, ts, price, open, prev_close, high, low, pct_chg,"
    " volume, amount, bid1, ask1, status, is_stale, source"
)


class QuoteRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, quotes: Iterable[Quote]) -> int:
        """一批一个事务：写进去一半的批次比没写更糟——下一轮会把那几只当成已更新。"""
        rows = list(quotes)
        if not rows:
            return 0
        with self.db.transaction():
            for q in rows:
                self.db.execute(
                    f"INSERT OR REPLACE INTO quotes_snapshot ({_QUOTE_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        q.market,
                        q.code,
                        q.ts,
                        q.price,
                        q.open,
                        q.prev_close,
                        q.high,
                        q.low,
                        q.pct_chg,
                        q.volume,
                        q.amount,
                        q.bid1,
                        q.ask1,
                        q.status,
                        q.is_stale,
                        q.source,
                    ],
                )
        return len(rows)

    def latest(self, market: str | None = None) -> tuple[Quote, ...]:
        """每只票最近的一笔；`market` 给了就只看那一个市场。"""
        where, params = ("WHERE market = ?", [market]) if market else ("", [])
        rows = self.db.query(
            f"SELECT {_QUOTE_COLUMNS} FROM quotes_snapshot {where}"
            " QUALIFY row_number() OVER (PARTITION BY market, code ORDER BY ts DESC) = 1"
            " ORDER BY market, code",
            params,
        )
        return tuple(
            quote for quote in (_to_quote(row) for row in rows.to_dicts()) if quote is not None
        )


def _to_quote(row: dict) -> Quote | None:
    value = row["price"]
    if value is None or float(value) <= 0:
        return None
    return Quote(
        market=row["market"],
        code=row["code"],
        ts=row["ts"],
        price=float(value),
        open=row["open"],
        prev_close=row["prev_close"],
        high=row["high"],
        low=row["low"],
        pct_chg=row["pct_chg"],
        volume=row["volume"],
        amount=row["amount"],
        bid1=row["bid1"],
        ask1=row["ask1"],
        status=row["status"] or "",
        source=row["source"],
        is_stale=bool(row["is_stale"]),
    )


class QuoteHealthRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: QuoteSourceRecord) -> None:
        self.db.execute(
            """
            INSERT OR REPLACE INTO quote_source_health
                (name, markets, ok, last_error, last_ok_at, consecutive_failures, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, now())
            """,
            [
                record.name,
                ",".join(record.markets),
                record.ok,
                record.last_error,
                record.last_ok_at,
                record.consecutive_failures,
            ],
        )

    def load(self) -> dict[str, QuoteSourceRecord]:
        if "quote_source_health" not in self.db.table_names():
            return {}
        rows = self.db.query("SELECT * FROM quote_source_health").to_dicts()
        return {row["name"]: _to_record(row) for row in rows}


def _to_record(row: dict) -> QuoteSourceRecord:
    markets = row["markets"] or ""
    return QuoteSourceRecord(
        name=row["name"],
        markets=tuple(markets.split(",")) if markets else (),
        ok=None if row["ok"] is None else bool(row["ok"]),
        last_error=row["last_error"],
        last_ok_at=row["last_ok_at"],
        consecutive_failures=int(row["consecutive_failures"] or 0),
    )
