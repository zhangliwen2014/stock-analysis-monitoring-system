"""命令模块共用的小工具：子命令分组、参数解析辅助。

放这里的东西必须真的被两个以上命令用到，否则写回各自命令模块。
"""

from __future__ import annotations

import argparse
import getpass
import json
from argparse import _SubParsersAction
from datetime import UTC, date

from sams.core.errors import ValidationError
from sams.core.fundamentals import RATIO_BOUNDS, out_of_caliber


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
    """时间戳统一到 UTC 再落屏，并把时区说出来。

    DuckDB 对 `TIMESTAMPTZ` 的回读不稳定：`theses.created_at` 回来是 UTC，
    `thesis_events.ts` 与 `sim_orders.ts` 回来是会话本地时区（Asia/Shanghai）。两者是同一个
    瞬间，但按原样 `strftime` 会在同一屏上印出「立论 15:00 / 时间线 23:00」——看着像事件比
    记录晚八小时，而这一屏的用意正是回看「什么时候走了哪一步」（D80）。
    到秒：两步落在同一分钟里时（脚本、M3 的触发器），分是分辨不出先后的。
    """
    if value is None:
        return "–"
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return f"{value:%Y-%m-%d %H:%M:%S} UTC"


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
