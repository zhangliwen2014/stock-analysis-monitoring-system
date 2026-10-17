"""`sams user ...` 与 `sams login` —— 账号与会话。

只解析参数、调用 services/accounts、把结果打成文本；
口令强度、状态机、「禁用即吊销会话」这类规则都在下层。
需要交互口令时用 --password（无人值守），否则提示输入。
"""

from __future__ import annotations

import argparse
import sys

from sams.bootstrap import Container
from sams.cli.common import ask_password, group


def add_to(subparsers) -> None:
    actions = group(subparsers, "user", "账号管理")

    def leaf(name: str, handler, help_text: str, *, password: bool = False, admin: bool = False):
        parser = actions.add_parser(name, help=help_text)
        if name != "list":
            parser.add_argument("--username", required=True)
        if password:
            parser.add_argument("--password", help="不传则交互式询问")
        if admin:
            parser.add_argument("--admin", action="store_true", help="创建即为管理员")
        parser.set_defaults(handler=handler)
        return parser

    leaf("add", add, "创建用户", password=True, admin=True)
    leaf("list", list_users, "列出用户")
    leaf("passwd", passwd, "重置密码并吊销该用户全部会话", password=True)
    leaf("enable", enable, "启用用户")
    leaf("disable", disable, "禁用用户并吊销其全部会话")
    leaf("promote", promote, "提升为管理员")

    login = subparsers.add_parser("login", help="校验口令并签发会话 token")
    login.add_argument("--username", required=True)
    login.add_argument("--password", help="不传则交互式询问")
    login.add_argument("--ttl-days", type=int, default=14)
    login.set_defaults(handler=signin)


def _password(args: argparse.Namespace) -> str:
    # 只认「没给这个参数」：`--password ""` 是想用空口令，应交给口令强度校验，
    # 而不是被当成没传、转身去问终端要（无人值守时那里直接 EOFError 炸栈）
    if args.password is None:
        return ask_password()
    return args.password


def add(container: Container, args: argparse.Namespace) -> int:
    user = container.accounts.create(args.username, _password(args), is_admin=args.admin)
    role = "admin" if user.is_admin else "user"
    print(f"已创建 {user.username}（{role}，id={user.id}）")
    return 0


def list_users(container: Container, args: argparse.Namespace) -> int:
    for user in container.users.list_users():
        role = "admin" if user.is_admin else "user "
        last = (
            user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "从未登录"
        )
        print(f"{user.username:<20} {role} {user.status:<9} 最近登录 {last} id={user.id[:8]}")
    return 0


def passwd(container: Container, args: argparse.Namespace) -> int:
    revoked = container.accounts.reset_password(args.username, _password(args))
    print(f"已重置 {args.username} 的密码，吊销会话 {revoked} 个")
    return 0


def enable(container: Container, args: argparse.Namespace) -> int:
    user = container.accounts.enable(args.username)
    print(f"已启用 {user.username}")
    return 0


def disable(container: Container, args: argparse.Namespace) -> int:
    revoked = container.accounts.disable(args.username)
    print(f"已禁用 {args.username}，吊销会话 {revoked} 个")
    return 0


def promote(container: Container, args: argparse.Namespace) -> int:
    user = container.accounts.promote(args.username)
    print(f"已提升 {user.username} 为管理员")
    return 0


def signin(container: Container, args: argparse.Namespace) -> int:
    token = container.accounts.signin(args.username, _password(args), ttl_days=args.ttl_days)
    if token is None:
        print("登录失败：用户名或密码错误，或账号已禁用", file=sys.stderr)
        return 1
    print(token)
    return 0
