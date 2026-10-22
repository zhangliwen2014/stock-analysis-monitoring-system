"""「谁在调用」这一步只写一遍：屏幕上的用户名 → 库里的那个 id。

归属这条不变式现在有两个形状：一个用户（屏幕上敲进来的那一个名字，`resolve_actor`）
与一批用户（盘后链按名单跑的那两头，`active_users`）。抄三份的话，
先失效的那一处一定是某个入口漏掉「禁用」那半——症状不是报错，而是一个被停用的账号
还在往库里写。解析放在服务层也不放在命令层：后者等于指望每个新命令都记得查。
"""

from __future__ import annotations

from sams.core.errors import ValidationError
from sams.storage.repos.users import ACTIVE, User, UserRepo


def resolve_actor(users: UserRepo, username: str) -> User:
    user = users.get_by_username((username or "").strip())
    if user is None:
        raise ValidationError(
            f"没有用户 {username!r}：先 `sams user add {username}`"
            "（归属只认用户名，屏幕上的 `--user` 也是它）"
        )
    if user.status != ACTIVE:
        raise ValidationError(
            f"用户 {username} 已被禁用：启用之后才能读写，"
            "否则「禁用即停止一切动作」这条就不成立"
        )
    return user


def active_users(users: UserRepo) -> list[User]:
    """替一批用户做事的那一趟，只认启用的账号。

    `resolve_actor` 挡的是屏幕上传进来的那一个名字，而常驻的盘后链按名单跑，
    那一条路上没有任何入口经过 `resolve_actor`：不在这里挡，`sams user disable`
    就只挡住了手敲命令的那一头。
    采集那一头相反（见 `services/scheduler/universe.py` 的文件头）：停用的用户照拉对象，
    数据是公共资源，明天启用时库里该已经有数。
    """
    return [user for user in users.list_users() if user.status == ACTIVE]
