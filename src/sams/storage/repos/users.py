"""用户仓库：创建、认证、状态与角色。密码只存 bcrypt 哈希。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt

from sams.core.errors import ValidationError
from sams.storage.db import Database
from sams.storage.ids import new_id

MIN_PASSWORD_LEN = 12
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
BCRYPT_MAX_BYTES = 72
ACTIVE, DISABLED = "active", "disabled"
#: 屏幕上那一列的宽度上限：比 `channels.name` 那一条同数，两处不同会长出两种折行
_DISPLAY_NAME_MAX_CHARS = 40
#: 三条读路径共用的列名串。少一列不会报错，只会让那一条路径上的 DND 永远读到 None
_COLUMNS = (
    "id, username, is_admin, status, created_at, last_login_at,"
    " dnd_start, dnd_end, dnd_tz, display_name"
)


@dataclass(frozen=True)
class User:
    id: str
    username: str
    is_admin: bool
    status: str
    created_at: datetime | None = None
    last_login_at: datetime | None = None
    #: 免打扰窗口（§4.1）。三列同存同空，由 `set_dnd` 保证；`None` 是「没有窗口」，
    #: 不是「窗口是空的」——后者在屏幕上会被读成「一天 24 小时都不打扰」
    dnd_start: time | None = None
    dnd_end: time | None = None
    dnd_tz: str | None = None
    #: 页面上的称呼；空串=没设置（登录名才是凭据，显示名只是礼貌）
    display_name: str = ""


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


def _validate_dnd(
    start: time | None, end: time | None, tz: str | None
) -> tuple[time, time, str]:
    """三列同存同空；全空是「清除」，半空是错误。

    跨午夜（23:00→07:00）是这条功能的主要用例，所以**不**要求 `start < end`：
    拒绝它等于拒绝掉正确的用法。跨午夜的判据在读取侧（router 的 `_in_dnd`）。
    """
    given = (start is not None, end is not None, bool(tz))
    if not any(given):
        raise ValidationError("三个一起给，或者一个都不给（一个都不给=清除）")
    if not all(given):
        raise ValidationError(
            "免打扰的起止时间与时区要三个一起给：只有开始时间没有结束时间，意思是"
            "「从这一刻起永远不打扰」——这不是任何人的本意，而它在库里与一个跨午夜的"
            "窗口长得一模一样"
        )
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"免打扰时区非法: {tz}") from exc
    return start, end, tz.strip()


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
            f"SELECT {_COLUMNS}"
            " FROM users WHERE id = ?",
            [user_id],
        )
        return _to_user(rows) if not rows.is_empty() else None

    def get_by_username(self, username: str) -> User | None:
        rows = self.db.query(
            f"SELECT {_COLUMNS}"
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

    def set_display_name(self, user_id: str, value: str) -> None:
        """空串是「没设置」而不是「删掉这一列」：库里那一列可以为 NULL，读回来是空串。"""
        name = (value or "").strip()
        if len(name) > _DISPLAY_NAME_MAX_CHARS:
            raise ValidationError(
                f"显示名请控制在 {_DISPLAY_NAME_MAX_CHARS} 字以内（收到 {len(name)}）"
            )
        self.db.execute("UPDATE users SET display_name = ? WHERE id = ?", [name, user_id])

    def set_dnd(
        self, user_id: str, *, start: time | None, end: time | None, tz: str | None
    ) -> None:
        if start is None and end is None and not tz:
            self.clear_dnd(user_id)
            return
        window_start, window_end, zone = _validate_dnd(start, end, tz)
        self.db.execute(
            "UPDATE users SET dnd_start = ?, dnd_end = ?, dnd_tz = ? WHERE id = ?",
            [window_start, window_end, zone, user_id],
        )

    def clear_dnd(self, user_id: str) -> None:
        """三个列名写在一条语句里：「漏掉一个」的症状是用户以为自己清了，
        而库里还残留着另一半窗口，下一告警照旧发出去。"""
        self.db.execute(
            "UPDATE users SET dnd_start = NULL, dnd_end = NULL, dnd_tz = NULL WHERE id = ?",
            [user_id],
        )

    def list_users(self) -> list[User]:
        rows = self.db.query(
            f"SELECT {_COLUMNS}"
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
        dnd_start=rows["dnd_start"][index],
        dnd_end=rows["dnd_end"][index],
        dnd_tz=rows["dnd_tz"][index],
        display_name=str(rows["display_name"][index] or ""),
    )
