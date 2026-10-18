"""`sams analysis` —— 过闸的只读分析。

在加这条命令之前，命令行能拿到的区间涨跌只有「自己把两根裸价相除」那一条路：
跨除权日时那是一个看起来完全正常的错数（601088 的 −3.81% vs 交易所的 +1.26%），
图上、表里、决策上都不留痕迹。这条命令是唯一能把 `analysis` 那三道闸请出来的入口。

退出码分三种，因为「没给数」与「给了但不是那个区间的数」是两件事：
0 是请求的那段；1 是拒绝出数（原因照打）；3 是数照给了、锚点却落在请求之外
且缺口里有开市日或日历判不了的日子——脚本据此决定要不要信这个数。

**但退出码进了管道就没了**（`… | grep 复权; echo $?` 拿的是 `grep` 的码，恒为 0），
而筛选循环处处是管道。所以 `--json`：状态、两个数、除权明细、缺口分堆、豁免都进
payload，机器不必去正则中文文案（D57）。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date

from sams.bootstrap import Container
from sams.cli.common import group, iso_date
from sams.core.codes import exchange_folds_dividend_into_daily_change
from sams.core.errors import NotAdjustable
from sams.core.timeutil import gap_days
from sams.services.calendar import GapKinds


def add_to(subparsers) -> None:
    analysis = group(
        subparsers,
        "analysis",
        "只读分析（复权口径）：数过不了守卫就不出，出了一定带口径",
    )
    ret = analysis.add_parser("return", help="区间涨跌：裸价与复权并列给出")
    ret.add_argument("--market", required=True, help="A / HK / US")
    ret.add_argument("--code", required=True, help="如 601088")
    ret.add_argument("--start", required=True, help="YYYY-MM-DD")
    ret.add_argument("--end", required=True, help="YYYY-MM-DD")
    ret.add_argument(
        "--listed-on",
        help="上市首日：豁免它起 10 个日历日内的跳变检查（D37），用了就会跟着数打印",
    )
    ret.add_argument(
        "--json",
        action="store_true",
        help="机器可读输出（管道里退出码会被下游命令覆盖，脚本用这个）",
    )
    ret.set_defaults(handler=run_return)


def run_return(container: Container, args: argparse.Namespace) -> int:
    start, end = iso_date(args.start), iso_date(args.end)
    listed_on = iso_date(args.listed_on) if args.listed_on else None
    try:
        result = container.analysis.interval_return(
            args.market, args.code, start, end, listed_on=listed_on
        )
    except NotAdjustable as exc:
        # 拒绝是一种答案，不是程序出错：文案里带着下一步该跑的命令
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 1})
        else:
            print(f"拒绝出数：{exc}")
        return 1
    moved = (result.start, result.end) != (start, end)
    # 缺口那几天是不是开市日，问日历；日历答不了就承认答不了（分堆与判定都在日历服务里）
    kinds = (
        container.calendar.gap(result.market, gap_days(start, result.start, result.end, end))
        if moved
        else None
    )
    code = 3 if kinds is not None and kinds.matters else 0
    if args.json:
        _emit(_payload(result, start, end, kinds, code))
        return code
    return _print(result, kinds, code)


def _print(result, kinds: GapKinds | None, code: int) -> int:
    moved = kinds is not None
    claim = f"覆盖确认：{result.covered_by}，记录截至 {result.covered_through}"
    adopted = (
        f"← 采信这个，但它算的是 {result.start}~{result.end} 这一段（{claim}）"
        if moved
        else f"← 采信这个（{claim}）"
    )
    print(f"{result.market}/{result.code}  {result.start} ~ {result.end}")
    print(f"  复权  {_pct(result.adjusted)}   {adopted}")
    print(f"  裸价  {_pct(result.raw)}   {_why_raw(result)}")
    for line in _ex_lines(result):
        print(f"  {line}")
    for line in _claims(result):
        print(f"  {line}")
    if result.differs and _pct(result.raw) == _pct(result.adjusted):
        # 两位小数一样时，「采信复权 +2.44% / 裸价 +2.44%」这句话没有任何信息量，
        # 而差别恰好就在那被舍掉的一位上
        print(
            f"  ⚠ 两个数在两位小数下长得一样，这里给到四位小数："
            f"复权 {result.adjusted:+.4%}、裸价 {result.raw:+.4%}"
        )
    for line in _warnings(result):
        print(f"  {line}")
    if kinds is not None:
        print(f"  ⚠ 按 {result.start} 与 {result.end} 这两根出的数：{_gap_phrase(kinds)}")
        if code == 3:
            print("    ⇒ 这个数不是请求那个区间的数：脚本按退出码 3 处理")
    return code


def _pct(value: float) -> str:
    return f"{value:+.2%}"


def _why_raw(result) -> str:
    if result.differs:
        # 「错数」这句话只在交易所把分红折进前收盘价的市场成立：那里裸价那一行与板上不符。
        # 港美股正相反——板上那个当日涨跌**就是**裸价这一行，它没错，只是不含分红（D53）
        if exchange_folds_dividend_into_daily_change(result.market):
            return "← 跨除权日时它是错数，只是看起来正常"
        return "← 交易所公布的那个数就是它：跨除权日时它不含分红，所以不是这段的总收益"
    if result.start_is_ex_date:
        return f"← 起点 {result.start} 本身就是除权日：效果已在起点价里，两个数必然相等"
    return "← 首尾之间没有除权日：两个数必然相等，但这不等于这只票没分过红"


def _claims(result) -> list[str]:
    """口径那两行由「这个市场的交易所怎么算当日涨跌」决定，不由排版决定（D48 + D53）。"""
    if exchange_folds_dividend_into_daily_change(result.market):
        return [
            "口径  分红按除权参考价再投资：每股现金在除权日折成股数进因子（D34）",
            "      这与交易所公布的当日涨跌幅同一算法；「现金到手不再投资」是另一个数，这里不给",
        ]
    return [
        "口径  分红按本系统自算的除权参考价折成股数进因子（D34）：这是总收益口径",
        "      该交易所不为现金分红复权前收盘价，所以板上那个当日涨跌是「裸价」这一行——",
        "      两个数在这里不该互相核对；要跟板面对齐请用裸价",
    ]


def _ex_lines(result) -> list[str]:
    """把「这两个数为什么差这么多」摊开成逐条事实（D54）。

    只给一对百分数，读的人无法核对任何东西：他得自己去找除权公告、自己算参考价、
    自己判断那笔现金是税前还是到手。而这三步我们全都走过了，就在乘进因子的那几行里。
    """
    if not result.factors:
        return []
    out = []
    head = f"除权  窗口内 {len(result.factors)} 个除权日"
    for f in result.factors:
        parts = []
        if f.prev_close is not None and f.ex_ref is not None:
            parts.append(f"前收 {f.prev_close:.2f} → 参考价 {f.ex_ref:.2f}")
        if f.cash:
            parts.append(f"每股现金 {f.cash:.4f}")
        if f.paid:
            parts.append(f"每股配股付 {f.paid:.4f}")
        if f.multiplier != 1.0:
            parts.append(f"股数 ×{f.multiplier:.4f}")
        parts.append(f"因子 {f.factor:.4f}")
        out.append(f"{head}：{f.ex_date} " + "，".join(parts))
        head = "      "
    return out


def _warnings(result) -> list[str]:
    """跟着数出门的三句丑话：税、股数、还有躲在窗口外一条的那件事。"""
    out = []
    cash = [f for f in result.factors if f.cash]
    if cash and exchange_folds_dividend_into_daily_change(result.market):
        worst = max(f.cash for f in cash)
        out.append(
            f"⚠ 因子按含税派息折股（最大一笔每股 {worst:.4f}）：A 股持有不满一年要缴"
            " 10–20% 红利税，这个复权数不是你的到手收益"
        )
    split = [f for f in result.factors if f.multiplier != 1.0]
    if split:
        mult = "、".join(f"{f.multiplier:.4f}" for f in split)
        out.append(
            f"⚠ 这段里股数变了（×{mult}）：复权数是总收益口径，而「每股成本」跨那一天不可直接比"
        )
    for day, note in result.nearby_unpriceable:
        out.append(
            f"⚠ 止日之后 {day} 有一条无法折算的公司行为（{note}）："
            "这段给得出数，把止日挪到它当天或之后就给不出了"
        )
    if result.listed_on is not None:
        out.append(
            f"⚠ 按你给的上市日 {result.listed_on} 豁免了它起 10 个日历日内的跳变检查："
            "这个数没有 D37 那条「不可能跳变」的证据"
        )
    return out


def _payload(result, start: date, end: date, kinds: GapKinds | None, code: int) -> dict:
    return {
        "status": "anchor_outside_request" if kinds is not None else "ok",
        "exit": code,
        "market": result.market,
        "code": result.code,
        "requested_start": str(start),
        "requested_end": str(end),
        "start": str(result.start),
        "end": str(result.end),
        "raw": result.raw,
        "adjusted": result.adjusted,
        "differs": result.differs,
        "start_is_ex_date": result.start_is_ex_date,
        "ex_dates": [
            {
                "date": str(f.ex_date),
                "prev_close": f.prev_close,
                "ex_reference": f.ex_ref,
                "cash_per_share": f.cash or None,
                "rights_payment_per_share": f.paid or None,
                "share_multiplier": f.multiplier,
                "factor": f.factor,
            }
            for f in result.factors
        ],
        "nearby_unpriceable": [[str(d), n] for d, n in result.nearby_unpriceable],
        "covered_by": result.covered_by,
        "covered_through": str(result.covered_through),
        "listed_on": str(result.listed_on) if result.listed_on else None,
        "exchange_folds_dividend": exchange_folds_dividend_into_daily_change(result.market),
        "gap": None if kinds is None else _gap_payload(kinds),
    }


def _gap_payload(kinds: GapKinds) -> dict:
    return {
        "total": kinds.total,
        "trading": [str(d) for d in kinds.trading],
        "closed": [str(d) for d in kinds.closed],
        "unknown": [str(d) for d in kinds.unknown],
        "matters": kinds.matters,
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _gap_phrase(kinds: GapKinds) -> str:
    total = kinds.total
    if not kinds.trading and not kinds.unknown:
        return f"缺口 {total} 天都是休市日（交易日历）：请求的就是这段行情"
    bits = []
    if kinds.trading:
        bits.append(
            f"{len(kinds.trading)} 个开市日没有 K 线（{_days(kinds.trading)}）"
            "：那是没同步到，或当天停牌"
        )
    if kinds.unknown:
        bits.append(
            f"{len(kinds.unknown)} 天交易日历没铺到、无法判断是否开市"
            "（先 `sams calendar refresh`）"
        )
    if kinds.closed:
        bits.append(f"{len(kinds.closed)} 天休市")
    return f"缺口 {total} 天里 " + "；".join(bits)


def _days(days: Sequence[date]) -> str:
    shown = "、".join(str(d) for d in days[:3])
    return shown + (f" 等 {len(days)} 天" if len(days) > 3 else "")
