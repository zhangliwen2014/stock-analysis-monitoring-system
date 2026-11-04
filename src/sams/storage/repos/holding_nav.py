"""`nav_holding_daily` 的读写（S5-A §2.1）。

与 `NavRepo` 同一套幂等规矩：整段先删后写，重跑同一区间得到同一张表，而不是追加一版历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sams.storage.db import Database


@dataclass(frozen=True)
class HoldingNavPoint:
    day: date
    market: str
    code: str
    #: 清仓当天为 0（当天的价与汇率照写，否则那一段涨跌无人认领）
    qty: float
    #: 重建当天实际用上的本币收盘价，不是「库里最新的一根」
    price: float
    fx_rate: float
    value_report: float


def _row(row: dict) -> HoldingNavPoint:
    return HoldingNavPoint(
        day=row["date"],
        market=row["market"],
        code=row["code"],
        qty=float(row["qty"]),
        price=float(row["price"]),
        fx_rate=float(row["fx_rate"]),
        value_report=float(row["value_report"]),
    )


class HoldingNavRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace(
        self, account_id: str, start: date, end: date, rows: list[HoldingNavPoint]
    ) -> int:
        """整段覆盖一个区间；区间外的旧行不动（与 `NavRepo.replace` 同一条规矩）。"""
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM nav_holding_daily WHERE account_id = ? AND date BETWEEN ? AND ?",
                [account_id, start, end],
            )
            for row in rows:
                self.db.execute(
                    "INSERT INTO nav_holding_daily (account_id, date, market, code, qty,"
                    " price, fx_rate, value_report) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        account_id,
                        row.day,
                        row.market,
                        row.code,
                        row.qty,
                        row.price,
                        row.fx_rate,
                        row.value_report,
                    ],
                )
        return len(rows)

    def series(self, account_id: str, start: date, end: date) -> list[HoldingNavPoint]:
        rows = self.db.query(
            "SELECT * FROM nav_holding_daily WHERE account_id = ? AND date BETWEEN ? AND ?"
            " ORDER BY date, market, code",
            [account_id, start, end],
        )
        return [_row(row) for row in rows.to_dicts()]
