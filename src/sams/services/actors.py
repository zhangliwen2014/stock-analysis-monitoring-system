"""「谁在调用」这一步只写一遍：屏幕上的用户名 → 库里的那个 id，以及「替一批人做事」
时那一批是谁。

归属这条不变式现在有三个形状：一个用户（屏幕上敲进来的那一个名字，`resolve_actor`）、
一批启用的用户（盘后链按名单跑的那两头，`active_users`）、一批启用且管得着系统事务的
用户（`admin_users`）。抄几份的话，先失效的那一处一定是某个入口漏掉其中一半——症状
不是报错，而是一个被停用的账号还在往库里写，或一条系统告警发给了普通用户。
解析放在服务层也不放在命令层：后者等于指望每个新命令都记得查。
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


def admin_users(users: UserRepo) -> list[User]:
    """要替「管理员」做的那一批人：启用且 `is_admin`。

    同一个判据抄两份，迟早一份漏一个条件（`active_users` 的文档头说的就是这件事）。
    这里两个消费者读的正是同一头：健康读数里那个「有几个管理员」与系统告警的收件人
    名单——数出来的个数与真发出去的人数对不上，就是「显示有管理员而谁都没收到」
    这种查不出原因的症状。
    """
    return [user for user in active_users(users) if user.is_admin]
