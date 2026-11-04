"""`nav_daily` 的读写（S4-D §6.3）。

一天一行。`replace` 先把区间整段删掉再写：重跑同一晚得到同一张图，而不是
两条重叠的历史；用 DELETE + INSERT 而不是逐行 upsert，是因为「这一天不在新序列里」
（例如账户开立在区间之后）也必须消失。

`nav` 为 NULL 表示那一天**算不出来**（缺价/缺汇率），不是 0：读侧靠 `complete`
区分，TWR 与回撤都不许跨过它。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sams.storage.db import Database


@dataclass(frozen=True)
class NavPoint:
    day: date
    currency: str
    nav: float | None
    cash: float | None
    market_value: float | None
    external_flow: float
    complete: bool
    stale_prices: int
    note: str | None


def _row(row: dict) -> NavPoint:
    def opt(name: str) -> float | None:
        return None if row[name] is None else float(row[name])

    return NavPoint(
        day=row["date"],
        currency=row["currency"],
        nav=opt("nav"),
        cash=opt("cash"),
        market_value=opt("market_value"),
        external_flow=float(row["external_flow"] or 0.0),
        complete=bool(row["complete"]),
        stale_prices=int(row["stale_prices"] or 0),
        note=row["note"],
    )


class NavRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace(self, account_id: str, start: date, end: date, points: list[NavPoint]) -> int:
        """整段覆盖一个区间；区间外旧行不动（补一小段不该抹掉其余历史）。"""
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM nav_daily WHERE account_id = ? AND date BETWEEN ? AND ?",
                [account_id, start, end],
            )
            for point in points:
                self.db.execute(
                    "INSERT INTO nav_daily (account_id, date, currency, nav, cash,"
                    " market_value, external_flow, complete, stale_prices, note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        account_id,
                        point.day,
                        point.currency,
                        point.nav,
                        point.cash,
                        point.market_value,
                        point.external_flow,
                        point.complete,
                        point.stale_prices,
                        point.note,
                    ],
                )
        return len(points)

    def series(self, account_id: str, start: date, end: date) -> list[NavPoint]:
        rows = self.db.query(
            "SELECT * FROM nav_daily WHERE account_id = ? AND date BETWEEN ? AND ?"
            " ORDER BY date",
            [account_id, start, end],
        )
        return [_row(row) for row in rows.to_dicts()]

    def latest(self, account_id: str) -> NavPoint | None:
        rows = self.db.query(
            "SELECT * FROM nav_daily WHERE account_id = ? ORDER BY date DESC LIMIT 1",
            [account_id],
        )
        return None if rows.is_empty() else _row(rows.to_dicts()[0])
