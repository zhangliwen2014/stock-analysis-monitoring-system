"""每人的图表偏好（D193）：周期条顺序与均线窗口。

一行一人。没有行=用默认（`CHART_PERIODS` 的注册顺序、`MA_WINDOWS`）——不存一份
「默认值副本」，默认改了所有人跟着改；清空就是删掉这一行。
两列都是短列表，JSON 存：加一档周期不必改 schema。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database


@dataclass(frozen=True)
class ChartPref:
    user_id: str
    periods: tuple[str, ...]
    windows: tuple[int, ...]
    updated_at: datetime


class ChartPrefRepo:
    TABLE = "chart_prefs"

    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def get(self, user_id: str) -> ChartPref | None:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ?", [_user(user_id)]
        )
        return _to_pref(rows.to_dicts()[0]) if not rows.is_empty() else None

    def save(
        self, user_id: str, periods: tuple[str, ...], windows: tuple[int, ...]
    ) -> ChartPref:
        user_id = _user(user_id)
        now = self.clock.now_utc()
        self.db.execute(
            f"INSERT INTO {self.TABLE} (user_id, periods_json, ma_json, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
            " periods_json = excluded.periods_json,"
            " ma_json = excluded.ma_json,"
            " updated_at = excluded.updated_at",
            [user_id, json.dumps(list(periods)), json.dumps(list(windows)), now],
        )
        saved = self.get(user_id)
        assert saved is not None
        return saved

    def clear(self, user_id: str) -> bool:
        user_id = _user(user_id)
        existed = bool(
            self.db.scalar(
                f"SELECT count(*) FROM {self.TABLE} WHERE user_id = ?", [user_id]
            )
        )
        if not existed:
            return False
        self.db.execute(f"DELETE FROM {self.TABLE} WHERE user_id = ?", [user_id])
        return True


def _user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


def _to_pref(row: dict) -> ChartPref:
    return ChartPref(
        user_id=row["user_id"],
        periods=tuple(json.loads(row["periods_json"] or "[]")),
        windows=tuple(json.loads(row["ma_json"] or "[]")),
        updated_at=row["updated_at"],
    )
