"""账号用例：创建、登录、改密、启停、提权。

这里放「一条规则需要动两张表」的编排——禁用/改密必须同时吊销会话，
否则旧 token 仍能访问已停用的账号。CLI 只做参数解析和输出，
不重复这条不变式。
"""

from __future__ import annotations

from dataclasses import dataclass

from sams.core.errors import ValidationError
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.users import User, UserRepo


@dataclass
class AccountService:
    users: UserRepo
    sessions: SessionRepo

    def create(self, username: str, password: str, *, is_admin: bool = False) -> User:
        return self.users.create(username, password, is_admin=is_admin)

    def signin(self, username: str, password: str, *, ttl_days: int = 14) -> str | None:
        """成功返回 token，失败返回 None（不区分失败原因，见 authenticate）。"""
        user = self.users.authenticate(username, password)
        if user is None:
            return None
        return self.sessions.issue(user.id, ttl_days=ttl_days)

    def reset_password(self, username: str, password: str) -> int:
        user = self._require(username)
        self.users.set_password(user.id, password)
        return self.sessions.revoke_all(user.id)

    def enable(self, username: str) -> User:
        user = self._require(username)
        self.users.enable(user.id)
        return self.users.get(user.id)  # 返回改后的行，不是改前的快照

    def disable(self, username: str) -> int:
        """返回被吊销的会话数——0 也是正常结果，调用方按文案区分。"""
        user = self._require(username)
        self.users.disable(user.id)
        return self.sessions.revoke_all(user.id)

    def promote(self, username: str) -> User:
        user = self._require(username)
        self.users.set_admin(user.id, True)
        return self.users.get(user.id)  # 返回改后的行，不是改前的快照

    def _require(self, username: str) -> User:
        user = self.users.get_by_username(username)
        if user is None:
            raise ValidationError(f"用户不存在: {username}")
        return user
