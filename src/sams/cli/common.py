"""命令模块共用的小工具：子命令分组、参数解析辅助。

放这里的东西必须真的被两个以上命令用到，否则写回各自命令模块。
"""

from __future__ import annotations

import getpass
from argparse import _SubParsersAction
from datetime import date

from sams.core.errors import ValidationError
from sams.core.fundamentals import RATIO_BOUNDS


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


def ratio_arg(name: str, value: str) -> float:
    """比率参数按**小数口径**解析（D58）：`--roe-estimate 15` 拒收。

    这不是洁癖。PR 的 PB 那一支把 ROE 乘两遍（`PB ÷ ROE ÷ ROE ÷ 100`），
    把 15% 写成 15 会让那个数小两百倍——屏幕上它长得像「极度便宜」，而这是
    一条会把票排进 `PR ≤ 0.4` 前列的选股规则。仓库层对库里的数有同一道闸
    （`core.fundamentals.RATIO_BOUNDS`），命令行是这条口径的另一个入口，
    不能只挡一边。上限直接取那一处，不在这里再造一个数。
    """
    try:
        number = float(value)
    except ValueError:
        raise ValidationError(f"{name} 要的是一个数，收到 {value!r}") from None
    ceiling = RATIO_BOUNDS["roe"]
    if not 0.0 < number <= ceiling:
        raise ValidationError(
            f"{name}={value} 不像一个小数口径的比率：要 0 < x ≤ {ceiling:g}"
            "（15% 写 0.15，不是 15）"
        )
    return number
