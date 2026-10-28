"""筛选方案的增删改查（D182）：per-user，名字在一个人手里唯一。

条件以 JSON 存：形状跟着词表走，加一个字段不必改 schema。`updated_at` 只被「改名」
与「改条件」碰，重跑不碰——它回答的是「这条方案现在是什么时候这样写的」，
「上次跑」是 `scan_results` 那一列的事（D183）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_market
from sams.core.errors import ValidationError
from sams.core.screener_fields import Condition, validate
from sams.storage.db import Database

MAX_NAME = 40


@dataclass(frozen=True)
class Preset:
    id: str
    user_id: str
    name: str
    market: str
    conditions: tuple[Condition, ...]
    created_at: datetime
    updated_at: datetime


class ScreenerPresetRepo:
    TABLE = "screener_presets"

    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def create(
        self, user_id: str, name: str, market: str, conditions: list[Condition]
    ) -> Preset:
        user_id = _user(user_id)
        name = _name(name)
        market = canonical_market(market)
        checked = _checked(market, conditions)
        existing = self.find(user_id, name)
        if existing is not None:
            raise ValidationError(
                f"已有同名方案「{name}」（id={existing.id}）：要覆盖就点那一行的「覆盖为当前条件」，"
                "要保留它请换个名字"
            )
        preset_id = uuid4().hex
        now = self.clock.now_utc()
        self.db.execute(
            f"INSERT INTO {self.TABLE}"
            " (id, user_id, name, market, conditions_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [preset_id, user_id, name, market, _to_json(checked), now, now],
        )
        saved = self.get(user_id, preset_id)
        assert saved is not None
        return saved

    def list(self, user_id: str) -> list[Preset]:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ? ORDER BY updated_at DESC, name",
            [_user(user_id)],
        )
        return [_to_preset(row) for row in rows.to_dicts()]

    def get(self, user_id: str, preset_id: str) -> Preset | None:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ? AND id = ?",
            [_user(user_id), preset_id],
        )
        return _to_preset(rows.to_dicts()[0]) if not rows.is_empty() else None

    def find(self, user_id: str, name: str) -> Preset | None:
        rows = self.db.query(
            f"SELECT * FROM {self.TABLE} WHERE user_id = ? AND name = ?",
            [_user(user_id), _name(name)],
        )
        return _to_preset(rows.to_dicts()[0]) if not rows.is_empty() else None

    def rename(self, user_id: str, preset_id: str, name: str) -> bool:
        user_id = _user(user_id)
        name = _name(name)
        existing = self.find(user_id, name)
        if existing is not None and existing.id != preset_id:
            raise ValidationError(f"已有同名方案「{name}」：换个名字，或先删掉那一条")
        if self.get(user_id, preset_id) is None:
            return False
        self.db.execute(
            f"UPDATE {self.TABLE} SET name = ?, updated_at = ?"
            " WHERE user_id = ? AND id = ?",
            [name, self.clock.now_utc(), user_id, preset_id],
        )
        return True

    def update_conditions(
        self, user_id: str, preset_id: str, conditions: list[Condition]
    ) -> bool:
        user_id = _user(user_id)
        preset = self.get(user_id, preset_id)
        if preset is None:
            return False
        checked = _checked(preset.market, conditions)
        self.db.execute(
            f"UPDATE {self.TABLE} SET conditions_json = ?, updated_at = ?"
            " WHERE user_id = ? AND id = ?",
            [_to_json(checked), self.clock.now_utc(), user_id, preset_id],
        )
        return True

    def delete(self, user_id: str, preset_id: str) -> bool:
        user_id = _user(user_id)
        exists = self.db.scalar(
            f"SELECT count(*) FROM {self.TABLE} WHERE user_id = ? AND id = ?",
            [user_id, preset_id],
        )
        if not exists:
            return False
        self.db.execute(
            f"DELETE FROM {self.TABLE} WHERE user_id = ? AND id = ?", [user_id, preset_id]
        )
        return True


def _user(user_id: str) -> str:
    if not user_id:
        raise ValidationError("user_id 不能为空")
    return user_id


def _name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("方案要一个名字")
    if len(cleaned) > MAX_NAME:
        raise ValidationError(f"方案名最多 {MAX_NAME} 个字，收到 {len(cleaned)} 个")
    return cleaned


def _checked(market: str, conditions: list[Condition]) -> tuple[Condition, ...]:
    if not conditions:
        raise ValidationError("一条条件都没有：空条件等于「全市场都通过」，那不是一个能重跑的方案")
    return validate(market, list(conditions))


def _to_json(conditions: tuple[Condition, ...]) -> str:
    return json.dumps(
        [{"field": item.field, "op": item.op, "value": item.value} for item in conditions],
        ensure_ascii=False,
    )


def _to_preset(row: dict) -> Preset:
    conditions = []
    for item in json.loads(row["conditions_json"] or "[]"):
        value = item["value"]
        conditions.append(
            Condition(item["field"], item["op"], tuple(value) if isinstance(value, list) else value)
        )
    return Preset(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        market=row["market"],
        conditions=tuple(conditions),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
