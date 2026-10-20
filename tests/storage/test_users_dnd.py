"""免打扰的三列是一个整体：同存同空。

只有 start 没有 end 意思是「从今天起永远不再打扰」——这不是任何人的本意，而它在库里
与「设了一个跨午夜的窗口」长得一样。校验放在仓库层而不是 DDL：错误要说清「为什么必须
一起」，CHECK 约束到 CLI 只会剩一个约束名。
"""

from __future__ import annotations

from datetime import time

import pytest

from sams.core.errors import ValidationError
from sams.storage.repos.users import UserRepo


@pytest.fixture
def repo(db):
    return UserRepo(db)


def test_new_user_has_no_dnd(repo):
    user = repo.create("trader", "correct-horse-battery")
    assert user.dnd_start is None and user.dnd_end is None and user.dnd_tz is None


def test_set_the_three_together(repo):
    user = repo.create("trader", "correct-horse-battery")
    repo.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="Asia/Shanghai")
    got = repo.get(user.id)
    assert (got.dnd_start, got.dnd_end, got.dnd_tz) == (time(23, 0), time(7, 0), "Asia/Shanghai")


def test_partial_is_refused(repo):
    user = repo.create("trader", "correct-horse-battery")
    with pytest.raises(ValidationError, match="三个一起"):
        repo.set_dnd(user.id, start=time(23, 0), end=None, tz="Asia/Shanghai")
    with pytest.raises(ValidationError, match="三个一起"):
        repo.set_dnd(user.id, start=None, end=time(7, 0), tz="Asia/Shanghai")
    # 一个都不给 = 清除，不是「半填」
    repo.set_dnd(user.id, start=None, end=None, tz=None)
    assert repo.get(user.id).dnd_start is None


def test_zone_must_exist(repo):
    user = repo.create("trader", "correct-horse-battery")
    with pytest.raises(ValidationError, match="时区"):
        repo.set_dnd(user.id, start=time(1, 0), end=time(2, 0), tz="Mars/Olympus")


def test_cross_midnight_window_is_accepted(repo):
    """23:00→07:00 是这条功能的主要用例，跨午夜的判据在读取侧（router 的 `_in_dnd`）。"""
    user = repo.create("trader", "correct-horse-battery")
    repo.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="UTC")
    assert repo.get(user.id).dnd_start > repo.get(user.id).dnd_end


def test_clear_dnd_leaves_all_three_empty(repo):
    user = repo.create("trader", "correct-horse-battery")
    repo.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="UTC")
    repo.clear_dnd(user.id)
    got = repo.get(user.id)
    assert got.dnd_start is None and got.dnd_end is None and got.dnd_tz is None


def test_list_users_carries_the_window(repo):
    """屏幕要能说「trader 现在在免打扰里」，所以 list 也带这三列。
    少了这一步，`sams user list` 会显示一个没设免打扰的用户，而库里明明设了。"""
    user = repo.create("trader", "correct-horse-battery")
    repo.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="UTC")
    row = next(item for item in repo.list_users() if item.username == "trader")
    assert row.dnd_start == time(23, 0)


def test_every_read_path_carries_the_window(repo):
    """三条读路径（get / get_by_username / list_users）必须都给得出窗口。

    少接一条不会报错：那条路径上的 `dnd_start` 永远是 None，于是「通过用户名解析actor」
    的那条链（`services/actors.resolve_actor`）看不到免打扰，而它正是路由要问的那个对象。
    """
    user = repo.create("trader", "correct-horse-battery")
    repo.set_dnd(user.id, start=time(23, 0), end=time(7, 0), tz="UTC")
    assert repo.get(user.id).dnd_start == time(23, 0)
    assert repo.get_by_username("trader").dnd_start == time(23, 0)
    assert repo.list_users()[0].dnd_start == time(23, 0)
