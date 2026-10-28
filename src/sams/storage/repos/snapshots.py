"""`snapshot_daily` 的读写：一次采集几千行，读端只按 `(market, day)` 取一屏。

整日覆盖而不是逐行 upsert：同一天重采时，源答不出的那几只必须从这一天消失——
留着旧行等于让「今天这一篮」里混进一只今天没有报价的票，而页面上没有任何一处
能看出它是昨天的数（D181）。
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from sams.core.codes import canonical_market
from sams.storage.db import Database

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

    def load_day(self, market: str, day: date) -> pl.DataFrame:
        return self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE market = ? AND day = ? ORDER BY code",
            [canonical_market(market), day],
        )

    def count(self) -> int:
        return int(self.db.scalar(f"SELECT count(*) FROM {self.TABLE}") or 0)
