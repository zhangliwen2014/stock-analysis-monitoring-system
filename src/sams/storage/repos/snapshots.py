"""`snapshot_daily` 的读写：一次采集几千行，读端只按 `(market, day)` 取一屏。

整日覆盖而不是逐行 upsert：同一天重采时，源答不出的那几只必须从这一天消失——
留着旧行等于让「今天这一篮」里混进一只今天没有报价的票，而页面上没有任何一处
能看出它是昨天的数（D181）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl

from sams.core.codes import canonical_market
from sams.storage.db import Database
from sams.storage.repos.bars import ADJUST

#: 快照的 `day` 与日线的 `date` 是同一件事的两个名字，这里统一叫 day


@dataclass(frozen=True)
class CloseMismatch:
    """一只票某一天的「库里的收盘」与「见证快照里的收盘」。"""

    code: str
    stored: float
    witness: float

#: 写入的列（顺序与迁移一致）。market/day/taken_at 由 SQL 侧给，不进帧
_COLUMNS = (
    "code",
    "name",
    "price",
    "pct_chg",
    "amount",
    "turnover_rate",
    "volume_ratio",
    "amplitude",
    "pe_ttm",
    "pe_dyn",
    "pb",
    "market_cap",
    "float_cap",
    "is_st",
    "source",
)
_VIEW = "incoming_snapshot_daily"


class SnapshotRepo:
    TABLE = "snapshot_daily"

    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_day(
        self, *, market: str, day: date, taken_at: datetime, frame: pl.DataFrame
    ) -> int:
        """整日覆盖；空帧不动表（一次空答复不该删掉昨天已经采到的历史）。"""
        if frame.is_empty():
            return 0
        market = canonical_market(market)
        incoming = frame.select(list(_COLUMNS)).unique(subset=["code"], keep="last")
        self.db.register(_VIEW, incoming)
        try:
            with self.db.transaction():
                self.db.execute(
                    f"DELETE FROM {self.TABLE} WHERE market = ? AND day = ?", [market, day]
                )
                self.db.execute(
                    f"INSERT INTO {self.TABLE} (market, day, taken_at, {', '.join(_COLUMNS)})"
                    f" SELECT ?, ?, ?, {', '.join(_COLUMNS)} FROM {_VIEW}",
                    [market, day, taken_at],
                )
        finally:
            self.db.unregister(_VIEW)
        return incoming.height

    def latest_day(self, market: str) -> date | None:
        value = self.db.scalar(
            f"SELECT max(day) FROM {self.TABLE} WHERE market = ?",
            [canonical_market(market)],
        )
        return value if isinstance(value, date) else None

    def latest_until(self, market: str, as_of: date) -> date | None:
        """截至日之前最近的一整日横截面。回看某一天时，今天的快照不能穿越回去。"""
        value = self.db.scalar(
            f"SELECT max(day) FROM {self.TABLE} WHERE market = ? AND day <= ?",
            [canonical_market(market), as_of],
        )
        return value if isinstance(value, date) else None

    def load_day(self, market: str, day: date) -> pl.DataFrame:
        return self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND day = ? ORDER BY code",
            [canonical_market(market), day],
        )

    def close_mismatches(
        self, market: str, day: date, *, tolerance: float
    ) -> list[CloseMismatch]:
        """某一天「库里的收盘」与「当天的收盘快照」对不上的那些票，差得最多的排前面。

        只比两边都有数的票：快照里有、库里当天没 K 线，最常见的解释是停牌，把它算成不平
        会往补数队列里塞几千行根本补不出来的活，真正的缺口反而排不上。
        `b.close > 0` 不是防除零的装饰：收盘为 0 的那几行是源给的脏数，除下去就是无穷大。
        """
        rows = self.db.query(
            f"""
            SELECT b.code AS code, b.close AS stored, s.price AS witness
            FROM bars_daily b
            JOIN {self.TABLE} s
              ON s.market = b.market AND s.code = b.code AND s.day = b.date
            WHERE b.market = ? AND b.date = ? AND b.adjust = ? AND b.close > 0
              AND s.price IS NOT NULL
              AND abs(b.close - s.price) / b.close > ?
            ORDER BY abs(b.close - s.price) / b.close DESC
            """,
            [canonical_market(market), day, ADJUST, tolerance],
        )
        return [
            CloseMismatch(
                code=row["code"], stored=float(row["stored"]), witness=float(row["witness"])
            )
            for row in rows.to_dicts()
        ]

    def missing_bars(self, market: str, day: date, *, within_days: int) -> list[str]:
        """当天快照有价、日线里没那根、而这几天有线的那些票（D310）。

        `close_mismatches` 是内连接，它看不见「整根缺了」——那正是它当初故意挡掉的那一批，
        因为最常见的原因是停牌。挡住停牌靠的是最后那个 EXISTS：停牌票的 bars_daily 是成片没有，
        断链票是最近连着有、就缺这一根。这一条只回答「这只票是不是断了」，
        整个市场当天都没落那种情况由调用方用市场自己的最大 K 线日拦（#380 F2）。
        """
        rows = self.db.query(
            f"""
            SELECT s.code AS code
            FROM {self.TABLE} s
            LEFT JOIN bars_daily b
              ON b.market = s.market AND b.code = s.code AND b.date = s.day
                 AND b.adjust = ?
            WHERE s.market = ? AND s.day = ? AND s.price IS NOT NULL AND b.date IS NULL
              AND EXISTS (
                SELECT 1 FROM bars_daily r
                WHERE r.market = s.market AND r.code = s.code AND r.adjust = ?
                  AND r.date >= ? AND r.date < ?
              )
            ORDER BY s.code
            """,
            [
                ADJUST,
                canonical_market(market),
                day,
                ADJUST,
                day - timedelta(days=within_days),
                day,
            ],
        )
        return [str(row["code"]) for row in rows.to_dicts()]

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT count(*) FROM {self.TABLE}") or 0)
