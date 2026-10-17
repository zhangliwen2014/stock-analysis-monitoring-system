"""会话仓库：token 明文只返回一次，库内仅存 sha256 摘要。

泄露数据库不会等于泄露可用会话——这是与 pwd_hash 同样的取证隔离原则。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sams.storage.db import Database

DEFAULT_TTL_DAYS = 14


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def issue(self, user_id: str, *, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
        if ttl_days <= 0:
            raise ValueError("ttl_days 必须为正")
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            [_hash(token), user_id, datetime.now(UTC) + timedelta(days=ttl_days)],
        )
        return token

    def verify(self, token: str) -> str | None:
        """返回有效会话的 user_id，否则 None。"""
        if not token:
            return None
        return self.db.scalar(
            """
            SELECT user_id FROM user_sessions
            WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > now()
            """,
            [_hash(token)],
        )

    def revoke(self, token: str) -> None:
        self.db.execute(
            "UPDATE user_sessions SET revoked_at = now()"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            [_hash(token)],
        )

    def revoke_all(self, user_id: str) -> int:
        before = self.active_count(user_id)
        self.db.execute(
            "UPDATE user_sessions SET revoked_at = now() WHERE user_id = ? AND revoked_at IS NULL",
            [user_id],
        )
        return before - self.active_count(user_id)

    def active_count(self, user_id: str) -> int:
        return int(
            self.db.scalar(
                """
                SELECT count(*) FROM user_sessions
                WHERE user_id = ? AND revoked_at IS NULL AND expires_at > now()
                """,
                [user_id],
            )
            or 0
        )
