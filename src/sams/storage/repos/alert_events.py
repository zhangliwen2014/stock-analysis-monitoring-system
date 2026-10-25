"""`alert_events`：闭环动作的不可变历史（M4-B spec §8.1）。

`alerts` 上的列是当前结论，这张表是「谁在什么时候把它从什么改成了什么」。
两个用途：undo 要读终态前那一条的 `from_status`；详情页要显示动作次序。
表不可改：没有 update/delete 方法——恢复状态是在 `AlertRepo` 里再写一行 undo。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

#: 与 spec §8.1 的状态图一一对应。多一个取值就是漏一个分支。
ACTIONS = ("ack", "defer", "act", "dismiss", "undo")
ACK, DEFER, ACT, DISMISS, UNDO = ACTIONS

_COLUMNS = "id, alert_id, user_id, action, from_status, to_status, note, ts"


@dataclass(frozen=True)
class AlertEvent:
    id: str
    alert_id: str
    user_id: str
    action: str
    from_status: str
    to_status: str
    note: str
    ts: datetime


class AlertEventRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def add(
        self,
        alert_id: str,
        *,
        user_id: str,
        action: str,
        from_status: str,
        to_status: str,
        note: str = "",
    ) -> AlertEvent:
        if action not in ACTIONS:
            raise ValidationError(f"未知动作 action={action!r}：只有 {list(ACTIONS)}")
        if not alert_id or not user_id:
            raise ValidationError("动作历史必须说清是哪条告警、谁按的")
        event_id = new_id()
        moment = self.clock.now_utc()
        text = (note or "").strip()
        self.db.execute(
            "INSERT INTO alert_events"
            " (id, alert_id, user_id, action, from_status, to_status, note, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [event_id, alert_id, user_id, action, from_status, to_status, text, moment],
        )
        return AlertEvent(
            id=event_id,
            alert_id=alert_id,
            user_id=user_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            note=text,
            ts=moment,
        )

    def list_for(self, alert_id: str) -> list[AlertEvent]:
        """动作次序；同一瞬间的两步靠物理行号决胜。

        按 `id` 排就是按 uuid4 排：冻住的时钟、脚本、一次请求里连按两下，
        都会让「先已读后处置」在时间线上说成反的（与 `thesis_events` 同一条规则）。
        """
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM alert_events WHERE alert_id = ? ORDER BY ts, rowid",
            [alert_id],
        )
        return [self._to_event(row) for row in rows.to_dicts()]

    def last(
        self, alert_id: str, *, actions: Sequence[str] | None = None
    ) -> AlertEvent | None:
        clauses = ["alert_id = ?"]
        params: list[object] = [alert_id]
        if actions:
            marks = ", ".join("?" for _ in actions)
            clauses.append(f"action IN ({marks})")
            params.extend(actions)
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM alert_events WHERE {' AND '.join(clauses)}"
            " ORDER BY ts DESC, rowid DESC LIMIT 1",
            params,
        )
        return self._to_event(rows.to_dicts()[0]) if rows.height else None

    def _to_event(self, row: dict) -> AlertEvent:
        return AlertEvent(
            id=str(row["id"]),
            alert_id=str(row["alert_id"]),
            user_id=str(row["user_id"]),
            action=str(row["action"]),
            from_status=str(row["from_status"]),
            to_status=str(row["to_status"]),
            note=str(row["note"] or ""),
            ts=row["ts"],
        )
