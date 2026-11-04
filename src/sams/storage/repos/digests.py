"""每日摘要的落行与投递结局（S1-C）。

键是 `(user_id, market, as_of)`，写一次就不再改：正文是当晚的快照，重跑只答
「已经有这一条」。投递结局写回同一行——摘要不是告警，没有 `notify_log` 可挂。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import ValidationError
from sams.core.notify import OUTCOMES, SENT
from sams.storage.db import Database
from sams.storage.ids import new_id


@dataclass(frozen=True)
class Digest:
    id: str
    user_id: str
    market: str
    as_of: date
    title: str
    body: str
    has_changes: bool
    heartbeat: bool
    source_note: str
    pushed: bool
    delivery_outcome: str
    delivery_error: str
    delivered_at: datetime | None
    created_at: datetime


_COLUMNS = (
    "id, user_id, market, as_of, title, body, has_changes, heartbeat, source_note,"
    " pushed, delivery_outcome, delivery_error, delivered_at, created_at"
)


class DigestRepo:
    def __init__(self, db: Database, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def save(
        self,
        *,
        user_id: str,
        market: str,
        as_of: date,
        title: str,
        body: str,
        has_changes: bool,
        heartbeat: bool,
        source_note: str = "",
    ) -> tuple[Digest, bool]:
        """写这一天的摘要；已经有就原样返回，`created=False`。

        `ON CONFLICT DO NOTHING` 之后无论哪一路都再读一次：把「已存在」的分支写成
        「返回我手里刚拼的那份正文」，重跑就会在屏幕上给出一个库里没有的版本。
        """
        if not user_id or not market:
            raise ValidationError("摘要必须说清是谁、哪个市场")
        digest_id = new_id()
        self.db.execute(
            "INSERT INTO digests"
            " (id, user_id, market, as_of, title, body, has_changes, heartbeat,"
            "  source_note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, market, as_of) DO NOTHING",
            [
                digest_id,
                user_id,
                market,
                as_of,
                title,
                body,
                bool(has_changes),
                bool(heartbeat),
                source_note or "",
                self.clock.now_utc(),
            ],
        )
        got = self.get(user_id, market, as_of)
        assert got is not None  # 刚写完，同一条连接里一定读得回来
        return got, got.id == digest_id

    def get(self, user_id: str, market: str, as_of: date) -> Digest | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM digests WHERE user_id = ? AND market = ? AND as_of = ?",
            [user_id, market, as_of],
        )
        return self._to_digest(rows.to_dicts()[0]) if rows.height else None

    def get_by_id(self, digest_id: str) -> Digest | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM digests WHERE id = ?",
            [digest_id],
        )
        return self._to_digest(rows.to_dicts()[0]) if rows.height else None

    def latest_per_market(self, user_id: str) -> dict[str, Digest]:
        rows = self.db.query(
            "SELECT * FROM ("
            f" SELECT {_COLUMNS}, row_number() OVER ("
            "   PARTITION BY market ORDER BY as_of DESC, created_at DESC"
            " ) AS rnk FROM digests WHERE user_id = ?)"
            " WHERE rnk = 1 ORDER BY market",
            [user_id],
        )
        return {row["market"]: self._to_digest(row) for row in rows.to_dicts()}

    def list_for(
        self, user_id: str, *, market: str | None = None, limit: int = 30
    ) -> list[Digest]:
        clauses = ["user_id = ?"]
        params: list[object] = [user_id]
        if market:
            clauses.append("market = ?")
            params.append(market)
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM digests WHERE {' AND '.join(clauses)}"
            " ORDER BY as_of DESC, market LIMIT ?",
            [*params, int(limit)],
        )
        return [self._to_digest(row) for row in rows.to_dicts()]

    def count_for(self, user_id: str, market: str) -> int:
        return int(
            self.db.scalar(
                "SELECT count(*) FROM digests WHERE user_id = ? AND market = ?",
                [user_id, market],
            )
            or 0
        )

    def mark_delivery(self, digest_id: str, outcome: str, *, error: str = "") -> None:
        """把这一次投递的结局写回摘要行。`pushed` 说的是「已经试过」，不是「成功」。"""
        if outcome not in OUTCOMES:
            raise ValidationError(f"未知投递结局 {outcome!r}：只有 {list(OUTCOMES)}")
        self.db.execute(
            "UPDATE digests SET pushed = TRUE, delivery_outcome = ?, delivery_error = ?,"
            " delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END"
            " WHERE id = ?",
            [outcome, error or "", outcome == SENT, self.clock.now_utc(), digest_id],
        )

    def _to_digest(self, row) -> Digest:
        return Digest(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            market=str(row["market"]),
            as_of=row["as_of"],
            title=str(row["title"]),
            body=str(row["body"]),
            has_changes=bool(row["has_changes"]),
            heartbeat=bool(row["heartbeat"]),
            source_note=str(row["source_note"] or ""),
            pushed=bool(row["pushed"]),
            delivery_outcome=str(row["delivery_outcome"] or ""),
            delivery_error=str(row["delivery_error"] or ""),
            delivered_at=row["delivered_at"],
            created_at=row["created_at"],
        )
