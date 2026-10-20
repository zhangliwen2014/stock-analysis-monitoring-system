"""命令模块共用的小工具：子命令分组、参数解析辅助。

放这里的东西必须真的被两个以上命令用到，否则写回各自命令模块。
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
from argparse import _SubParsersAction
from datetime import date

from sams.core.errors import ValidationError
from sams.core.fundamentals import RATIO_BOUNDS, out_of_caliber
from sams.core.timeutil import format_utc_moment


def exit_for(exc: Exception) -> int:
    """1 与 2 的分界只在这里写一遍（D77）：认得出的错在参数上就是 2，其余是业务拒绝。

    脚本对两者的处置不同：2 是敲错了，重来多少次都一样；1 是这条记录还没到那一步，
    补数据或走完上一步就能重跑。把这条线抄在两个命令模块里，总有一处会先失效。
    """
    return 2 if isinstance(exc, ValidationError) else 1


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def refused(args: argparse.Namespace, exc: Exception, code: int) -> int:
    """拒绝也要有 JSON（D57）：管道里读的人拿到的是一句原因加一个码，不是一个空 stdin。"""
    if getattr(args, "json", False):
        emit({"status": "refused", "reason": str(exc), "exit": code})
    else:
        print(f"{exc}")
    return code


def stamp(value) -> str:
    """时间戳的统一落屏格式；怎么折 UTC、为什么要到秒，见 `core.timeutil.format_utc_moment`。"""
    return "–" if value is None else format_utc_moment(value)


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


def currency_arg(value: str) -> str:
    """币种码归一成 ISO 4217 的大写，形状不对就当场拒收。

    小写不会让任何一层报错，只会让那一行**永远查不到**：持仓换算拿配置里的
    `USD`/`CNY` 去问 `fx_rates`，库里躺着的是 `usd`，症状是「换汇影响永远是 0」。
    这与仓库层「`RMB`/`rmb` 一律拒绝、不做模糊匹配」那条不是同一件事：这里不改字母，
    只是把大小写收到唯一的那种写法上（同 `canonical_code` 收键形态的道理，D50）。
    """
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValidationError(f"币种 {value!r} 看不懂：要三字母代码，例如 USD / CNY")
    return code


def ratio_arg(name: str, value: str) -> float:
    """比率参数按**小数口径**解析（D58 + D66）：`--roe-estimate 15` 与 `--roe-estimate 5` 都拒收。

    这不是洁癖。PR 的 PB 那一支把 ROE 乘两遍（`PB ÷ ROE ÷ ROE ÷ 100`），
    把 15% 写成 15 会让那个数小两百倍——屏幕上它长得像「极度便宜」，而这是
    一条会把票排进 `PR ≤ 0.4` 前列的选股规则。仓库层对库里的数有同一道闸，
    命令行是这条口径的另一个入口，不能只挡一边：判据取 core 那一个 `out_of_caliber`，
    所以「库里存不下 1.0 以上的 ROE」与「命令行收不下 1.0 以上的预估」不会是两句话。
    """
    try:
        number = float(value)
    except ValueError:
        raise ValidationError(f"{name} 要的是一个数，收到 {value!r}") from None
    ceiling = RATIO_BOUNDS["roe"]
    if number <= 0 or out_of_caliber("roe", number):
        raise ValidationError(
            f"{name}={value} 不像一个小数口径的比率：要 0 < x < {ceiling:g}"
            "（15% 写 0.15，不是 15；而 1 以上在这里只能读成 100% 开外，同样不收）"
        )
    return number
