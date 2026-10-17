"""命令模块共用的小工具：子命令分组、参数解析辅助。

放这里的东西必须真的被两个以上命令用到，否则写回各自命令模块。
"""

from __future__ import annotations

import getpass
from argparse import _SubParsersAction
from datetime import date

from sams.core.errors import ValidationError


def group(subparsers: _SubParsersAction, name: str, help_text: str) -> _SubParsersAction:
    """建一层 `sams <name> <action>` 分组，返回 action 用的 subparsers。"""
    parser = subparsers.add_parser(name, help=help_text)
    return parser.add_subparsers(dest="action", required=True)


def ask_password(prompt: str = "密码") -> str:
    """交互取密码：给了 --password 就用它，否则问两次（无人值守时用 --password）。"""
    first = getpass.getpass(f"{prompt}: ")
    second = getpass.getpass(f"再输一次{prompt}: ")
    if first != second:
        raise ValueError(f"两次{prompt}不一致")
    return first


def iso_date(value: str) -> date:
    """解析 `YYYY-MM-DD`。

    包一层是因为 `date.fromisoformat` 抛的是裸 `ValueError`，而 CLI 只兜 `SamsError`：
    手滑打成一个 2024-7-1 会让整条命令吐一段栈，运维读到的是 Python 的行号，
    不是「这个日期该怎么写」。
    """
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            f"日期 {value!r} 看不懂：要的是 YYYY-MM-DD，例如 2024-07-01"
        ) from None
