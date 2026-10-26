"""`admin_access_log` 的仓库（D139）：break-glass 授权的唯一事实来源。

只有 INSERT 与 SELECT：审计行能改就等于授权能被抹掉。「限时 15 分钟」不做定时
任务——读的时候比 `granted_until`，过期行留在表里当历史，重启也不丢。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

_COLUMNS = (
    "id, admin_user_id, annotation_id, owner_user_id, action, reason,"
    " granted_until, created_at"
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AdminAccess:
    id: str
    admin_user_id: str
    annotation_id: str
    owner_user_id: str
    action: str
    reason: str
    granted_until: datetime
    created_at: datetime


class AdminAccessRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def grant(
        self,
        *,
        admin_user_id: str,
        annotation_id: str,
        owner_user_id: str,
        reason: str,
        granted_until: datetime,
    ) -> AdminAccess:
        """写一行授权。「至少 4 个字」是使用规则，归服务层；仓库只挡空串与裸时间。"""
        text = (reason or "").strip()
        if not text:
            raise ValidationError("break-glass 必须写理由：审计里没有「想看看」")
        if granted_until.tzinfo is None:
            raise ValidationError("granted_until 必须带时区：到没到期不能取决于服务器 locale")
        moment = self.clock.now_utc()
        rid = new_id()
        self.db.execute(
            "INSERT INTO admin_access_log"
            " (id, admin_user_id, annotation_id, owner_user_id, action, reason,"
            "  granted_until, created_at)"
            " VALUES (?, ?, ?, ?, 'break_glass', ?, ?, ?)",
            [
                rid,
                admin_user_id,
                annotation_id,
                owner_user_id,
                text,
                _utc(granted_until),
                moment,
            ],
        )
        return AdminAccess(
            id=rid,
            admin_user_id=admin_user_id,
            annotation_id=annotation_id,
            owner_user_id=owner_user_id,
            action="break_glass",
            reason=text,
            granted_until=_utc(granted_until),
            created_at=moment,
        )

    def _to_row(self, row: dict) -> AdminAccess:
        return AdminAccess(
            id=str(row["id"]), admin_user_id=str(row["admin_user_id"]),
            annotation_id=str(row["annotation_id"]),
            owner_user_id=str(row["owner_user_id"]), action=str(row["action"]),
            reason=str(row["reason"]),
            granted_until=_utc(row["granted_until"]), created_at=_utc(row["created_at"]),
        )

    def active(
        self, admin_user_id: str, annotation_id: str, *, now: datetime
    ) -> AdminAccess | None:
        """此刻仍然有效的那一条授权（最新的一个），没有就返回 None。"""
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM admin_access_log"
            " WHERE admin_user_id = ? AND annotation_id = ? AND granted_until > ?"
            " ORDER BY granted_until DESC, created_at DESC, id DESC LIMIT 1",
            [admin_user_id, annotation_id, _utc(now)],
        ).to_dicts()
        return self._to_row(rows[0]) if rows else None

    def count_since(self, admin_user_id: str, *, since: datetime) -> int:
        """限次闸用：这个管理员在 `since` 之后发起过几次授权。"""
        return int(
            self.db.scalar(
                "SELECT count(*) FROM admin_access_log WHERE admin_user_id = ?"
                " AND created_at >= ?",
                [admin_user_id, _utc(since)],
            )
            or 0
        )

    def oldest_in_window(self, admin_user_id: str, *, since: datetime) -> datetime | None:
        """窗口内**最早发起**那条的 created_at，没有行就 None。

        等待秒数只能问这一个时刻：`min(granted_until)` 答的是「最早什么时候到期」，
        而限次滑出靠的是发起时间——名额腾出的那一刻 = 最早那条 created_at + 窗口长。
        """
        raw = self.db.scalar(
            "SELECT min(created_at) FROM admin_access_log WHERE admin_user_id = ?"
            " AND created_at >= ?",
            [admin_user_id, _utc(since)],
        )
        return _utc(raw) if raw is not None else None

    def list_recent(self, *, limit: int = 20) -> list[AdminAccess]:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM admin_access_log"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            [int(limit)],
        ).to_dicts()
        return [self._to_row(row) for row in rows]
