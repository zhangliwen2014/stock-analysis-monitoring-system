"""会话仓库：token 明文只返回一次，库内仅存 sha256 摘要。

泄露数据库不会等于泄露可用会话——这是与 pwd_hash 同样的取证隔离原则。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.storage.db import Database

DEFAULT_TTL_DAYS = 14


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionRepo:
    def __init__(self, db: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    def issue(self, user_id: str, *, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
        if ttl_days <= 0:
            raise ValueError("ttl_days 必须为正")
        token = secrets.token_urlsafe(32)
        now = self.clock.now_utc()
        self.db.execute(
            "INSERT INTO user_sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            [_hash(token), user_id, now, now + timedelta(days=ttl_days)],
        )
        return token

    def verify(self, token: str) -> str | None:
        """返回有效会话的 user_id，否则 None。

        `created_at <= now` 不是多余的：时钟回拨（NTP 校正、重放用的历史时钟）
        会让 expires_at 永远在未来，会话因此永生。签发时刻在过去才是「时间走到了」。
        """
        if not token:
            return None
        now = self.clock.now_utc()
        return self.db.scalar(
            """
            SELECT user_id FROM user_sessions
            WHERE token_hash = ? AND revoked_at IS NULL
              AND expires_at > ? AND created_at <= ?
            """,
            [_hash(token), now, now],
        )

    def revoke(self, token: str) -> None:
        self.db.execute(
            "UPDATE user_sessions SET revoked_at = ?"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            [self.clock.now_utc(), _hash(token)],
        )

    def revoke_all(self, user_id: str) -> int:
        """吊销该用户全部未吊销的会话（含已过期的，它们在审计上仍算「被吊销」）。"""
        flipped = self.db.query(
            "UPDATE user_sessions SET revoked_at = ? "
            "WHERE user_id = ? AND revoked_at IS NULL RETURNING token_hash",
            [self.clock.now_utc(), user_id],
        )
        return flipped.height

    def active_count(self, user_id: str) -> int:
        """与 verify 同一套时间判断：全部用注入时钟，否则「有效」在两处会给出不同答案。"""
        now = self.clock.now_utc()
        return int(
            self.db.scalar(
                """
                SELECT count(*) FROM user_sessions
                WHERE user_id = ? AND revoked_at IS NULL
                  AND expires_at > ? AND created_at <= ?
                """,
                [user_id, now, now],
            )
            or 0
        )
