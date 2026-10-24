"""通道仓库：写侧加密、读侧掩码、`credentials()` 是明文唯一出口。

这条边界要挡的不是攻击者，是一个好心的下一位读者：他在 `list_for` 里顺手把
`config_encrypted` 解开「方便调试」，屏幕上就有了别人的 webhook token，而这件事
不留痕迹（§4.2）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support import FrozenClock

from sams.core.errors import StorageError, ValidationError
from sams.storage.crypto import cipher_for
from sams.storage.repos.channels import (
    Channel,
    ChannelRepo,
)
from sams.storage.repos.users import UserRepo


@pytest.fixture
def repo(db, settings):
    return ChannelRepo(
        db, cipher=cipher_for(settings), clock=FrozenClock(datetime(2026, 10, 20, tzinfo=UTC))
    )


@pytest.fixture
def seeded_user(db):
    return UserRepo(db).create("trader", "correct-horse-battery").id


@pytest.fixture
def other_user(db):
    return UserRepo(db).create("keeper", "correct-horse-battery").id


def create(repo, user_id, *, kind="wecom", name="主群", **config):
    conf = {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123456789", **config}
    return repo.create(user_id, kind, name, conf)


class SettingsStub:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key


def db_row(repo, channel_id):
    return repo.db.query("SELECT * FROM channels WHERE id = ?", [channel_id]).to_dicts()[0]


def test_write_is_encrypted_at_rest(db, repo, seeded_user):
    channel = create(repo, seeded_user)
    raw = db.query("SELECT config_encrypted FROM channels WHERE id = ?", [channel.id])
    stored = raw["config_encrypted"][0]
    assert "abc123456789" not in stored
    assert stored.startswith("gAAAAA")  # Fernet 的 version+timestamp 头


def test_list_for_returns_masked_values(repo, seeded_user):
    create(repo, seeded_user, name="主群")
    rows = repo.list_for(seeded_user)
    assert [row.name for row in rows] == ["主群"]
    assert rows[0].config["url"].startswith("***")
    assert "abc123456789" not in rows[0].config["url"]


def test_credentials_is_the_plaintext_exit(repo, seeded_user):
    channel = create(repo, seeded_user)
    assert "abc123456789" in repo.credentials(channel.id)["url"]


def test_credentials_of_an_unknown_channel_is_a_storage_error(repo):
    with pytest.raises(StorageError, match="不存在"):
        repo.credentials("nope")


def test_name_is_required_and_trimmed(repo, seeded_user):
    with pytest.raises(ValidationError, match="名字"):
        repo.create(seeded_user, "wecom", "   ", {"url": "https://x"})


def test_kind_must_be_a_known_one(repo, seeded_user):
    """kind 的名单在 `monitor/notifiers` 那边，仓库层不 import 它（storage 不许 import monitor）。
    所以这里只挡空/超长，真正「不认识这个 kind」由 CLI 层的 factory 报错——
    两处都报错才是对的：仓库不许假设自己认识所有通道。"""
    with pytest.raises(ValidationError):
        repo.create(seeded_user, "", "n", {"url": "https://x"})


def test_first_channel_becomes_the_default(repo, seeded_user):
    first = create(repo, seeded_user)
    second = create(repo, seeded_user, name="副群")
    assert first.is_default and not second.is_default


def test_set_default_moves_it_and_only_it(repo, seeded_user):
    a = create(repo, seeded_user)
    b = create(repo, seeded_user, name="副群")
    repo.set_default(b.id)
    rows = {row.id: row.is_default for row in repo.list_for(seeded_user)}
    assert rows == {a.id: False, b.id: True}


def test_the_default_cannot_be_disabled(repo, seeded_user):
    """「默认」与「停用」同时成立是一句矛盾的话，而它会路由到一条发不出去的通道。"""
    a = create(repo, seeded_user)
    with pytest.raises(ValidationError, match="默认"):
        repo.set_enabled(a.id, False)


def test_set_default_refuses_a_disabled_channel(repo, seeded_user):
    create(repo, seeded_user, name="主群")
    b = create(repo, seeded_user, name="副群")
    repo.set_enabled(b.id, False)
    with pytest.raises(ValidationError, match="停用"):
        repo.set_default(b.id)


def test_re_enabling_clears_the_error_note(repo, seeded_user):
    """重新启用要清掉上次失败的那句话：一个已经恢复的通道旁边挂着旧错误，
    「这条现在能用吗」的答案就被噪音盖住。默认那条不能停用，所以这里用副群。"""
    create(repo, seeded_user, name="主群")
    b = create(repo, seeded_user, name="副群")
    repo.mark_error(b.id, error="429")
    repo.set_enabled(b.id, False)
    repo.set_enabled(b.id, True)
    assert repo.show(b.id).last_error == ""
    assert repo.show(b.id).last_error_at is None


def test_disabling_keeps_the_error_note(repo, seeded_user):
    """停用**不**清：`last_error` 是「为什么这条现在不发」的唯一答案。"""
    create(repo, seeded_user, name="主群")
    b = create(repo, seeded_user, name="副群")
    repo.mark_error(b.id, error="企微返回 errcode=93000")
    repo.set_enabled(b.id, False)
    assert repo.show(b.id).last_error == "企微返回 errcode=93000"


def test_mark_error_and_mark_ok_are_visible(repo, seeded_user):
    a = create(repo, seeded_user)
    repo.mark_ok(a.id)
    assert repo.show(a.id).last_ok_at is not None
    assert repo.show(a.id).last_error == ""
    repo.mark_error(a.id, error="企微返回 errcode=93000")
    shown = repo.show(a.id)
    assert shown.last_error == "企微返回 errcode=93000"
    assert shown.last_error_at is not None


def test_remove_drops_the_default_flag_with_it(repo, seeded_user):
    a = create(repo, seeded_user)
    b = create(repo, seeded_user, name="副群")
    repo.remove(a.id)
    assert repo.show(b.id).is_default, "唯一的默认被删掉后，用户就没有默认通道了"
    assert repo.show(a.id) is None


def test_scope_is_the_owner(repo, seeded_user, other_user):
    a = create(repo, seeded_user)
    assert repo.list_for(other_user) == []
    assert repo.show(a.id, user_id=other_user) is None


def test_credentials_ignores_the_owner_check_is_untouched(repo, seeded_user, other_user):
    """`credentials()` 不带 user_id：它是给 router 用的内部读，归属在路由那一步按
    「这条通道属不属于这次告警的接收人」判。写在这里是因为下一个人会想加参数。"""
    a = create(repo, seeded_user)
    assert repo.show(a.id).user_id == seeded_user


def test_by_kind_only_returns_enabled_channels(repo, seeded_user):
    """router 按 kind 找通道时不能拿到一条停用的：那会「发成功」到一个已经关掉的群。"""
    create(repo, seeded_user, name="主群")
    b = create(repo, seeded_user, name="副群", kind="dingtalk")
    repo.set_enabled(b.id, False)
    assert [row.name for row in repo.by_kind(seeded_user, "dingtalk")] == []
    assert [row.name for row in repo.by_kind(seeded_user, "wecom")] == ["主群"]


def test_an_empty_config_is_a_channel_with_nothing_to_configure(repo, seeded_user):
    """`desktop`/`voice` 没有要配的字段：仓库层若把「空配置」当错误，本机通道就建不出来。

    「wecom 缺 url 就发不出去」是 kind 的知识，住在 `monitor/notifiers`，由
    `NotifyService.add` 在落库之前问工厂（`tests/monitor/test_router.py` 那枚用例守着）；
    仓库层自己判就得认识六个 kind，而它连名单都不该知道。
    """
    local = repo.create(seeded_user, "desktop", "本机弹窗", {})
    assert local.config == {}
    assert repo.show(local.id).config == {}


def test_a_non_mapping_config_is_refused(repo, seeded_user):
    with pytest.raises(ValidationError, match="要是一份 k=v"):
        repo.create(seeded_user, "wecom", "主群", None)


def test_created_at_is_recorded(repo, seeded_user):
    """`sams channel list` 要能回答「这条是刚配的还是三个月没动过的」。"""
    a = create(repo, seeded_user)
    row = db_row(repo, a.id)
    assert row["created_at"] == datetime(2026, 10, 20, tzinfo=UTC)


def test_masked_view_survives_an_undecryptable_row(db, seeded_user):
    """换过 secret_key 的老库里这一行读不回明文：列表要照样列得出这条通道（好让人删掉它），
    而不是让它凭空消失，也不把异常抛到屏幕上。"""
    repo = ChannelRepo(db, cipher=cipher_for(SettingsStub("other-secret")))
    a = create(repo, seeded_user)
    other = ChannelRepo(
        db, cipher=cipher_for(SettingsStub("first-secret")), clock=FrozenClock(datetime.now(UTC))
    )
    b = other.create(seeded_user, "wecom", "副群", {"url": "https://x"})
    shown = repo.show(b.id)
    assert shown is not None
    assert shown.name == "副群"
    assert list(shown.config) == ["（凭据读不回来）"]
    assert repo.show(a.id).config["url"].startswith("***")


def test_channel_is_a_frozen_value_object(repo, seeded_user):
    a = create(repo, seeded_user)
    assert isinstance(a, Channel)
    with pytest.raises(AttributeError):
        a.name = "改名"


def test_update_config_merges_and_keeps_the_blank_keys(repo, seeded_user):
    """空值=不动那一键：编辑表单从不回显明文（掩码视图才上屏幕），所以「没填」
    只能意味着保持不变——`credentials` 仍是明文的唯一出口。"""
    channel = create(repo, seeded_user, token="old-token-value")

    updated = repo.update_config(channel.id, {"url": "https://new.example/hook", "token": ""})

    assert repo.credentials(channel.id) == {
        "url": "https://new.example/hook",
        "token": "old-token-value",
    }
    # 读回来的还是掩码视图：合并写不把明文带回调用方
    assert updated.user_id == seeded_user
    assert "https://new.example/hook" not in updated.config["url"]


def test_update_config_can_add_a_key_and_refuses_an_unknown_channel(repo, seeded_user):
    channel = create(repo, seeded_user)
    updated = repo.update_config(channel.id, {"at_all": "someone@example.com"})
    assert repo.credentials(channel.id) == {
        "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123456789",
        "at_all": "someone@example.com",
    }
    assert updated.name == "主群"

    with pytest.raises(StorageError, match="不存在"):
        repo.update_config("nope", {"url": "https://x"})
