"""用户仓库：创建、认证、状态与角色。密码只存 bcrypt 哈希。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import bcrypt

from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

MIN_PASSWORD_LEN = 12
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
BCRYPT_MAX_BYTES = 72
ACTIVE, DISABLED = "active", "disabled"


@dataclass(frozen=True)
class User:
    id: str
    username: str
    is_admin: bool
    status: str
    created_at: datetime | None = None
    last_login_at: datetime | None = None


def _validate_username(username: str) -> str:
    cleaned = (username or "").strip().lower()
    if not _USERNAME_RE.match(cleaned):
        raise ValidationError("用户名须为 3–32 位小写字母、数字、_ . -")
    return cleaned


def _validate_password(password: str) -> bytes:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValidationError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        # bcrypt 会静默截断到 72 字节——不报错就会造成"更长密码等于更弱"的假象
        raise ValidationError(f"密码 UTF-8 长度不得超过 {BCRYPT_MAX_BYTES} 字节")
    return encoded


class UserRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, username: str, password: str, *, is_admin: bool = False) -> User:
        name = _validate_username(username)
        pwd = _validate_password(password)
        if self.get_by_username(name) is not None:
            raise ValidationError(f"用户名已存在: {name}")
        user = User(
            id=new_id(),
            username=name,
            is_admin=bool(is_admin),
            status=ACTIVE,
        )
        self.db.execute(
            "INSERT INTO users (id, username, pwd_hash, is_admin, status) VALUES (?, ?, ?, ?, ?)",
            [
                user.id,
                name,
                bcrypt.hashpw(pwd, bcrypt.gensalt()).decode(),
                user.is_admin,
                user.status,
            ],
        )
        return user

    def get(self, user_id: str) -> User | None:
        rows = self.db.query(
            "SELECT id, username, is_admin, status, created_at, last_login_at"
            " FROM users WHERE id = ?",
            [user_id],
        )
        return _to_user(rows) if not rows.is_empty() else None

    def get_by_username(self, username: str) -> User | None:
        rows = self.db.query(
            "SELECT id, username, is_admin, status, created_at, last_login_at"
            " FROM users WHERE username = ?",
            [(username or "").strip().lower()],
        )
        return _to_user(rows) if not rows.is_empty() else None

    def authenticate(self, username: str, password: str) -> User | None:
        """成功返回 User 并刷新 last_login_at；用户名不存在/密码错/已禁用返回 None。

        不区分失败原因，避免把「账号存在」泄漏给探测者。
        """
        user = self.get_by_username(username)
        if user is None or not password:
            return None
        stored = self.db.scalar("SELECT pwd_hash FROM users WHERE id = ?", [user.id])
        if not stored or not bcrypt.checkpw(password.encode("utf-8"), stored.encode()):
            return None
        if user.status != ACTIVE:
            return None
        self.db.execute("UPDATE users SET last_login_at = now() WHERE id = ?", [user.id])
        return self.get(user.id)

    def set_password(self, user_id: str, password: str) -> None:
        self.db.execute(
            "UPDATE users SET pwd_hash = ? WHERE id = ?",
            [bcrypt.hashpw(_validate_password(password), bcrypt.gensalt()).decode(), user_id],
        )

    def set_status(self, user_id: str, status: str) -> None:
        if status not in (ACTIVE, DISABLED):
            raise ValidationError(f"非法状态 {status}")
        self.db.execute("UPDATE users SET status = ? WHERE id = ?", [status, user_id])

    def enable(self, user_id: str) -> None:
        """意图命名而非让调用方拼状态串：CLI 不该知道 "active" 这个字面量。"""
        self.set_status(user_id, ACTIVE)

    def disable(self, user_id: str) -> None:
        self.set_status(user_id, DISABLED)

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        self.db.execute("UPDATE users SET is_admin = ? WHERE id = ?", [bool(is_admin), user_id])

    def list_users(self) -> list[User]:
        rows = self.db.query(
            "SELECT id, username, is_admin, status, created_at, last_login_at"
            " FROM users ORDER BY username"
        )
        return [_to_user(rows, i) for i in range(rows.height)]


def _to_user(rows, index: int = 0) -> User:
    return User(
        id=rows["id"][index],
        username=rows["username"][index],
        is_admin=bool(rows["is_admin"][index]),
        status=rows["status"][index],
        created_at=rows["created_at"][index],
        last_login_at=rows["last_login_at"][index],
    )
