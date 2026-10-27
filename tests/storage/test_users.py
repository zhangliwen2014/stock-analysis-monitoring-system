from __future__ import annotations

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.users import ACTIVE, DISABLED, MIN_PASSWORD_LEN, UserRepo

PASSWORD = "correct horse battery"


@pytest.fixture
def users(db) -> UserRepo:
    return UserRepo(db)


def test_create_and_fetch(users):
    user = users.create("zhangsan", PASSWORD)
    assert user.status == ACTIVE and not user.is_admin
    assert users.get(user.id).username == "zhangsan"
    assert users.get_by_username("ZhangSan").id == user.id, "用户名大小写不敏感"


def test_duplicate_username_rejected(users):
    users.create("zhangsan", PASSWORD)
    with pytest.raises(ValidationError, match="已存在"):
        users.create("zhangsan", PASSWORD + "2")


@pytest.mark.parametrize("name", ["ab", "a" * 33, "有中文", "no spaces", ""])
def test_username_shape(users, name):
    with pytest.raises(ValidationError):
        users.create(name, PASSWORD)


def test_short_password_rejected(users):
    with pytest.raises(ValidationError, match=str(MIN_PASSWORD_LEN)):
        users.create("zhangsan", "short")


def test_six_chars_is_the_floor(users):
    """下限要说得出数：6 位能用、5 位挡。只测「短的被拒」测不出这条线画在哪。"""
    users.create("zhangsan", "abcdef")
    assert users.authenticate("zhangsan", "abcdef") is not None
    with pytest.raises(ValidationError, match="至少 6 位"):
        users.create("lisi", "abcde")


def test_too_long_password_rejected(users):
    """bcrypt 静默截断到 72 字节——不报错等于「更长密码更弱」的假象。"""
    with pytest.raises(ValidationError, match="72"):
        users.create("zhangsan", "x" * 73)


def test_same_password_hashes_differ(users, db):
    """两次创建同一口令的哈希必须不同，否则等于忘了加盐。"""
    a = users.create("usera", PASSWORD)
    b = users.create("userb", PASSWORD)
    hash_a = db.scalar("SELECT pwd_hash FROM users WHERE id = ?", [a.id])
    hash_b = db.scalar("SELECT pwd_hash FROM users WHERE id = ?", [b.id])
    assert hash_a != hash_b
    assert hash_a != PASSWORD and hash_b != PASSWORD


def test_hash_is_stored_not_plaintext(users, db):
    users.create("zhangsan", PASSWORD)
    assert db.scalar("SELECT count(*) FROM users WHERE pwd_hash LIKE '%correct horse%'") == 0


def test_authenticate(users):
    user = users.create("zhangsan", PASSWORD)
    assert users.authenticate("zhangsan", PASSWORD).id == user.id
    assert users.authenticate("zhangsan", PASSWORD).last_login_at is not None
    assert users.authenticate("ZhangSan", PASSWORD) is not None
    assert users.authenticate("zhangsan", "wrong password!!") is None
    assert users.authenticate("ghost", PASSWORD) is None
    assert users.authenticate("zhangsan", "") is None


def test_disabled_user_cannot_authenticate(users):
    user = users.create("zhangsan", PASSWORD)
    users.set_status(user.id, DISABLED)
    assert users.authenticate("zhangsan", PASSWORD) is None
    users.set_status(user.id, ACTIVE)
    assert users.authenticate("zhangsan", PASSWORD) is not None


def test_illegal_status_rejected(users):
    user = users.create("zhangsan", PASSWORD)
    with pytest.raises(ValidationError):
        users.set_status(user.id, "deleted")


def test_set_password_and_admin(users):
    user = users.create("zhangsan", PASSWORD)
    users.set_password(user.id, "another passphrase")
    assert users.authenticate("zhangsan", PASSWORD) is None
    assert users.authenticate("zhangsan", "another passphrase") is not None

    assert not users.get(user.id).is_admin
    users.set_admin(user.id, True)
    assert users.get(user.id).is_admin


def test_list_users_sorted(users):
    users.create("zhangsan", PASSWORD)
    users.create("lisi", PASSWORD)
    assert [u.username for u in users.list_users()] == ["lisi", "zhangsan"]


def test_password_longer_than_utf8_limit_is_rejected_not_truncated(users):
    """中文口令按 UTF-8 字节数计，超过 72 字节必须报错。"""
    with pytest.raises(ValidationError, match="72"):
        users.create("zhangsan", "密码" * 40)


def test_display_name_is_empty_until_set(users):
    """空串=没设置：`username` 是登录凭据，显示名只是礼貌（0019 那一列的头注）。"""
    user = users.create("zhangsan", PASSWORD)
    assert users.get(user.id).display_name == ""


def test_set_display_name_strips_and_validates(users):
    user = users.create("zhangsan", PASSWORD)

    users.set_display_name(user.id, "  卡罗尔  ")
    assert users.get(user.id).display_name == "卡罗尔"

    users.set_display_name(user.id, "")
    assert users.get(user.id).display_name == ""

    with pytest.raises(ValidationError, match="40"):
        users.set_display_name(user.id, "x" * 41)


def test_display_name_is_read_on_every_path(users):
    """三条读路径共用 `_COLUMNS`：少一列不会报错，只会让那一条路径上的显示名永远空着。"""
    user = users.create("zhangsan", PASSWORD)
    users.set_display_name(user.id, "卡罗尔")
    assert users.get(user.id).display_name == "卡罗尔"
    assert users.get_by_username("zhangsan").display_name == "卡罗尔"
    assert [row.display_name for row in users.list_users()] == ["卡罗尔"]
    assert users.authenticate("zhangsan", PASSWORD).display_name == "卡罗尔"
