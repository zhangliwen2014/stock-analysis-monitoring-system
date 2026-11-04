"""证伪台账仓库：一行一条假设，归属经 `theses` 校验（D232 P0-3）。

这里只做行与校验，不碰基本面：判一次要读哪一期、哪个数，属于用例层。
状态是上一次判出来的结论；`evaluated_on` / `evaluated_value` 与它同生同死——
只有状态没有读数，回看时说不清它当时为什么被证伪。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from sams.core.assumptions import OPEN, STATUSES, metric_label, op_label
from sams.core.errors import StorageError, ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id


@dataclass(frozen=True)
class Assumption:
    id: str
    thesis_id: str
    metric: str
    op: str
    threshold: float
    due_on: date | None
    note: str | None
    status: str
    evaluated_on: date | None
    evaluated_value: float | None
    created_at: datetime
    updated_at: datetime


_COLUMNS = (
    "id, thesis_id, metric, op, threshold, due_on, note, status, "
    "evaluated_on, evaluated_value, created_at, updated_at"
)


def _row(row: dict) -> Assumption:
    return Assumption(
        id=str(row["id"]),
        thesis_id=str(row["thesis_id"]),
        metric=str(row["metric"]),
        op=str(row["op"]),
        threshold=float(row["threshold"]),
        due_on=row["due_on"],
        note=row["note"],
        status=str(row["status"]),
        evaluated_on=row["evaluated_on"],
        evaluated_value=(
            None if row["evaluated_value"] is None else float(row["evaluated_value"])
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _threshold(value: float) -> float:
    if isinstance(value, bool) or not isfinite(value):
        raise ValidationError(f"阈值 {value!r} 不是一个能用的数")
    return float(value)


def _status(value: str) -> str:
    if value not in STATUSES:
        raise ValidationError(
            f"没有叫 {value!r} 的假设状态：能用的是 {'、'.join(STATUSES)}"
        )
    return value


class AssumptionRepo:
    TABLE = "thesis_assumptions"

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        thesis_id: str,
        metric: str,
        op: str,
        threshold: float,
        due_on: date | None,
        note: str | None,
        now: datetime,
    ) -> Assumption:
        if not thesis_id:
            raise ValidationError("thesis_id 不能为空")
        metric_label(metric)
        op_label(op)
        clean_note = (note or "").strip() or None
        row = {
            "id": new_id(),
            "thesis_id": thesis_id,
            "metric": metric,
            "op": op,
            "threshold": _threshold(threshold),
            "due_on": due_on,
            "note": clean_note,
            "status": OPEN,
            "evaluated_on": None,
            "evaluated_value": None,
            "created_at": now,
            "updated_at": now,
        }
        self.db.execute(
            f"INSERT INTO {self.TABLE} ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [row[column.strip()] for column in _COLUMNS.split(",")],
        )
        found = self.get(row["id"])
        assert found is not None  # 刚写完，读不到就是库出了问题
        return found

    def get(self, assumption_id: str) -> Assumption | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {self.TABLE} WHERE id = ?", [assumption_id]
        )
        return _row(rows.to_dicts()[0]) if not rows.is_empty() else None

    def list_for(self, thesis_id: str) -> list[Assumption]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {self.TABLE} WHERE thesis_id = ?"
            " ORDER BY created_at, id",
            [thesis_id],
        )
        return [_row(item) for item in rows.to_dicts()]

    def record(
        self,
        assumption_id: str,
        *,
        status: str,
        value: float | None,
        on: date,
        now: datetime,
    ) -> Assumption:
        _status(status)
        if value is not None and not isfinite(value):
            raise ValidationError(f"判出来的数 {value!r} 不是有限小数")
        self.db.execute(
            f"UPDATE {self.TABLE} SET status = ?, evaluated_on = ?, evaluated_value = ?,"
            " updated_at = ? WHERE id = ?",
            [status, on, value, now, assumption_id],
        )
        found = self.get(assumption_id)
        if found is None:
            raise StorageError(f"没有 id 为 {assumption_id!r} 的假设")
        return found

    def delete(self, assumption_id: str) -> None:
        self.db.execute(f"DELETE FROM {self.TABLE} WHERE id = ?", [assumption_id])
