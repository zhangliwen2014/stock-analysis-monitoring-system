"""`index_daily` 的读写：大盘指数的日线，与个股日线分表分键。

000001 在 A 市是平安银行、在上证是指数——同表同键必撞，所以这里用源形态的
`index_code`（sh000001/hkHSI/usSPX）。prev_close/change_pct 在写入时算：休市看板
要能直接报「昨收 + 涨跌幅」，不该在读取侧再翻一遍历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from sams.storage.db import Database


@dataclass(frozen=True)
class IndexLatest:
    market: str
    index_code: str
    day: date
    close: float
    change_pct: float | None


class IndexDailyRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, frame: pl.DataFrame) -> int:
        """按 (票, 日期) 升序逐行写：事务内的顺序插入让下一行读到刚写入的前收。"""
        written = 0
        with self.db.transaction():
            for row in frame.sort(["market", "index_code", "date"]).to_dicts():
                prev = self.db.scalar(
                    "SELECT close FROM index_daily"
                    " WHERE market = ? AND index_code = ? AND date < ?"
                    " ORDER BY date DESC LIMIT 1",
                    [row["market"], row["index_code"], row["date"]],
                )
                # `if prev` 同时挡掉 None 和 0.0：除以前收为零是编出一个百分比
                change = (
                    round((row["close"] - prev) / prev * 100, 4)
                    if prev and row["close"] is not None
                    else None
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO index_daily"
                    " (market, index_code, date, open, high, low, close, volume, amount,"
                    "  prev_close, change_pct, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        row["market"],
                        row["index_code"],
                        row["date"],
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row["close"],
                        row.get("volume"),
                        row.get("amount"),
                        prev,
                        change,
                        row.get("source"),
                    ],
                )
                written += 1
        return written

    def latest_with_change(self, market: str, index_code: str) -> IndexLatest | None:
        rows = self.db.query(
            "SELECT market, index_code, date, close, change_pct FROM index_daily"
            " WHERE market = ? AND index_code = ? ORDER BY date DESC LIMIT 1",
            [market, index_code],
        )
        if rows.is_empty():
            return None
        row = rows.to_dicts()[0]
        return IndexLatest(
            market=row["market"],
            index_code=row["index_code"],
            day=row["date"],
            close=float(row["close"]),
            change_pct=None if row["change_pct"] is None else float(row["change_pct"]),
        )

    def latest_date(self, market: str, index_code: str) -> date | None:
        value = self.db.scalar(
            "SELECT max(date) FROM index_daily WHERE market = ? AND index_code = ?",
            [market, index_code],
        )
        if value is None:
            return None
        return value.date() if isinstance(value, datetime) else value
