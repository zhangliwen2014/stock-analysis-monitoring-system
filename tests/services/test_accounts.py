"""账号服务：重点验「改状态必须连带吊销会话」这条跨表不变式。"""

from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.services.accounts import AccountService
from sams.storage.repos.sessions import SessionRepo
from sams.storage.repos.users import UserRepo

PASSWORD = "correct horse battery"
OTHER = "another strong password"


@pytest.fixture
def accounts(db) -> AccountService:
    return AccountService(UserRepo(db), SessionRepo(db))


def test_signin_issues_a_verifiable_token(accounts):
    accounts.create("zhangsan", PASSWORD)
    token = accounts.signin("zhangsan", PASSWORD)
    assert token is not None
    assert accounts.sessions.verify(token) is not None


def test_wrong_password_signin_returns_none(accounts):
    accounts.create("zhangsan", PASSWORD)
    assert accounts.signin("zhangsan", OTHER) is None


def test_disable_revokes_existing_sessions(accounts):
    accounts.create("zhangsan", PASSWORD)
    accounts.signin("zhangsan", PASSWORD)
    assert accounts.disable("zhangsan") == 1
    assert accounts.signin("zhangsan", PASSWORD) is None


def test_enable_lets_the_user_sign_in_again(accounts):
    accounts.create("zhangsan", PASSWORD)
    accounts.disable("zhangsan")
    assert accounts.enable("zhangsan").status == "active"
    assert accounts.signin("zhangsan", PASSWORD) is not None


def test_reset_password_revokes_sessions(accounts):
    user = accounts.create("zhangsan", PASSWORD)
    accounts.signin("zhangsan", PASSWORD)
    assert accounts.reset_password("zhangsan", OTHER) == 1
    assert accounts.sessions.active_count(user.id) == 0
    assert accounts.signin("zhangsan", OTHER) is not None


def test_unknown_username_is_a_validation_error(accounts):
    with pytest.raises(ValidationError, match="用户不存在"):
        accounts.disable("ghost")


def test_promote_raises_admin_flag(accounts):
    accounts.create("zhangsan", PASSWORD)
    assert accounts.promote("zhangsan").is_admin
