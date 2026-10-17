"""交易日历仓库：按 (market, date) 存当日时段。

关键语义（spec §6.3）：库里只有交易日的行，因此「某日无行」本身有歧义。
判定方式是先看该年是否已刷新覆盖（year_covered）：
  * 已覆盖 + 无行  = 休市（可信）
  * 未覆盖 + 无行  = 未知，必须由 CalendarService 触发补齐而不是当成休市
把这两种情况混为一谈会导致「日历没刷新 → 整月静默不监控」，是这类系统最坏的失效。
"""

from __future__ import annotations

import json
from datetime import date

import polars as pl

from sams.core.errors import StorageError
from sams.core.timeutil import CalendarDay, SessionWindow
from sams.storage.db import Database


class CalendarRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, days: list[CalendarDay], *, source: str) -> int:
        """批量写入：整批注册成一张表再一次 INSERT OR REPLACE。

        逐行 execute 在「3 市场 × 全年 250 交易日」的量级上会明显拖慢刷新，
        而日历刷新是在盘后任务里跑的，必须可预期。
        """
        if not days:
            return 0
        frame = pl.DataFrame(
            {
                "market": [d.market for d in days],
                "date": [d.day for d in days],
                "is_trading": [d.is_trading for d in days],
                "is_half_day": [d.is_half_day for d in days],
                "tz": [d.tz for d in days],
                "sessions_json": [
                    json.dumps([w.to_dict() for w in d.windows], ensure_ascii=False) for d in days
                ],
                "source": [source] * len(days),
            }
        )
        self.db.register("incoming_calendar", frame)
        self.db.execute(
            """
            INSERT OR REPLACE INTO trading_calendar
                (market, date, is_trading, is_half_day, tz, sessions_json, source, refreshed_at)
            SELECT market, date, is_trading, is_half_day, tz, sessions_json, source, now()
            FROM incoming_calendar
            """
        )
        self.db.conn.unregister("incoming_calendar")
        return frame.height

    def get(self, market: str, day: date) -> CalendarDay | None:
        rows = self.db.query(
            "SELECT * FROM trading_calendar WHERE market = ? AND date = ?",
            [market, day],
        )
        if rows.is_empty():
            return None
        row = rows.to_dicts()[0]
        try:
            windows = tuple(SessionWindow.from_dict(w) for w in json.loads(row["sessions_json"]))
        except (KeyError, ValueError) as exc:
            raise StorageError(
                f"trading_calendar sessions_json 损坏: {market} {day}: {exc}"
            ) from exc
        return CalendarDay(
            market=row["market"],
            day=row["date"],
            is_trading=bool(row["is_trading"]),
            is_half_day=bool(row["is_half_day"]),
            tz=row["tz"],
            windows=windows,
        )

    def year_covered(self, market: str, year: int) -> bool:
        return (
            self.db.scalar(
                "SELECT count(*) FROM trading_calendar WHERE market = ? AND year(date) = ?",
                [market, year],
            )
            or 0
        ) > 0

    def trading_days(self, market: str, year: int) -> int:
        return int(
            self.db.scalar(
                "SELECT count(*) FROM trading_calendar "
                "WHERE market = ? AND year(date) = ? AND is_trading",
                [market, year],
            )
            or 0
        )

    def rows_for_year(self, market: str, year: int) -> int:
        return int(
            self.db.scalar(
                "SELECT count(*) FROM trading_calendar WHERE market = ? AND year(date) = ?",
                [market, year],
            )
            or 0
        )

    def source_of(self, market: str, year: int) -> str | None:
        return self.db.scalar(
            "SELECT any_value(source) FROM trading_calendar WHERE market = ? AND year(date) = ?",
            [market, year],
        )
