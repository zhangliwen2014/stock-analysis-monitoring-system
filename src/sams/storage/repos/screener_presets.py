"""筛选方案的增删改查（D182）：per-user，名字在一个人手里唯一。

条件以 JSON 存：形状跟着词表走，加一个字段不必改 schema。`updated_at` 只被「编辑」
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
MAX_DESCRIPTION = 200


@dataclass(frozen=True)
class Preset:
    id: str
    user_id: str
    name: str
    market: str
    conditions: tuple[Condition, ...]
    created_at: datetime
    updated_at: datetime
    description: str = ""
    #: 每个交易日收盘后是否自动跑（S4-B / D254）
    enabled: bool = False
    #: 新命中推送到哪几个通道；空 = 只进告警中心
    channel_ids: tuple[str, ...] = ()
    #: 监控最后一次真的看过市场；NULL 同时是「下一趟只记基线」的闸
    last_run_at: datetime | None = None


class ScreenerPresetRepo:
    TABLE = "screener_presets"

    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def create(
        self,
        user_id: str,
        name: str,
        market: str,
        conditions: list[Condition],
        *,
        description: str = "",
    ) -> Preset:
        user_id = _user(user_id)
        name = _name(name)
        description = _description(description)
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
            " (id, user_id, name, market, conditions_json, description, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [preset_id, user_id, name, market, _to_json(checked), description, now, now],
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

    def list_enabled(self, *, market: str | None = None) -> list[Preset]:
        """调度读的那一半：所有打开监控的方案，按市场与名字排。"""
        sql = f"SELECT * FROM {self.TABLE} WHERE enabled"
        params: list[object] = []
        if market is not None:
            sql += " AND market = ?"
            params.append(canonical_market(market))
        rows = self.db.query(sql + " ORDER BY market, user_id, name", params)
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

    def edit(self, user_id: str, preset_id: str, name: str, description: str) -> bool:
        user_id = _user(user_id)
        name = _name(name)
        description = _description(description)
        existing = self.find(user_id, name)
        if existing is not None and existing.id != preset_id:
            raise ValidationError(f"已有同名方案「{name}」：换个名字，或先删掉那一条")
        if self.get(user_id, preset_id) is None:
            return False
        self.db.execute(
            f"UPDATE {self.TABLE} SET name = ?, description = ?, updated_at = ?"
            " WHERE user_id = ? AND id = ?",
            [name, description, self.clock.now_utc(), user_id, preset_id],
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
        # 改了条件就是另一句话（D257）：下一趟只记基线，不拿旧名单去比新条件
        self.db.execute(
            f"UPDATE {self.TABLE} SET conditions_json = ?, updated_at = ?, last_run_at = NULL"
            " WHERE user_id = ? AND id = ?",
            [_to_json(checked), self.clock.now_utc(), user_id, preset_id],
        )
        return True

    def set_monitor(
        self,
        user_id: str,
        preset_id: str,
        *,
        enabled: bool,
        channel_ids: list[str] | tuple[str, ...] = (),
    ) -> bool:
        """打开/关闭自动监控，并写下新命中要走的通道。

        「关 → 开」清空 `last_run_at`：重新打开之后的第一趟又是一次基线，不把关闭
        期间的进出当成今晚发生的事。一直开着只改通道时，基线不受影响。
        """
        user_id = _user(user_id)
        preset = self.get(user_id, preset_id)
        if preset is None:
            return False
        cleaned = _channels(channel_ids)
        sql = f"UPDATE {self.TABLE} SET enabled = ?, channel_ids = ?"
        params: list[object] = [bool(enabled), json.dumps(cleaned, ensure_ascii=False)]
        if enabled and not preset.enabled:
            sql += ", last_run_at = NULL"
        sql += " WHERE user_id = ? AND id = ?"
        params += [user_id, preset_id]
        self.db.execute(sql, params)
        return True

    def mark_run(self, user_id: str, preset_id: str, at: datetime) -> None:
        self.db.execute(
            f"UPDATE {self.TABLE} SET last_run_at = ? WHERE user_id = ? AND id = ?",
            [at, _user(user_id), preset_id],
        )

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


def _description(description: str | None) -> str:
    cleaned = (description or "").strip()
    if len(cleaned) > MAX_DESCRIPTION:
        raise ValidationError(
            f"说明最多 {MAX_DESCRIPTION} 个字，收到 {len(cleaned)} 个：长文请存在别处，"
            "这里只写选它/不选它的那句话"
        )
    return cleaned


def _channels(channel_ids: list[str] | tuple[str, ...]) -> list[str]:
    cleaned: list[str] = []
    for value in channel_ids:
        item = str(value).strip()
        if item and item not in cleaned:
            cleaned.append(item)
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
        description=row["description"] or "",
        enabled=bool(row.get("enabled")),
        channel_ids=tuple(json.loads(row.get("channel_ids") or "[]")),
        last_run_at=row.get("last_run_at"),
    )
