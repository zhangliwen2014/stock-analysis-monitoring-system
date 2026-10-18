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
from sams.cli.common import group, iso_date, ratio_arg
from sams.core.codes import exchange_folds_dividend_into_daily_change
from sams.core.errors import NotAdjustable, ValidationError
from sams.core.fundamentals import (
    BASIS_PB_ESTIMATE,
    ROE_BASIS,
    ROE_BASIS_ANNUAL,
    ROE_BASIS_LATEST,
    caliber_note,
    roe_basis_note,
)
from sams.core.timeutil import gap_days
from sams.services.analysis import PERIODS, default_indicators, indicator_names
from sams.services.calendar import GapKinds

# --- fundamentals：基本面 + 市赚率 PR --------------------------------------------------


def add_fundamentals(subparsers) -> None:
    fun = subparsers.add_parser(
        "fundamentals",
        help="基本面与市赚率 PR：报告期的 ROE 配截至日的 PE，两个粒度各说各的日期",
    )
    fun.add_argument("--market", required=True, help="A / HK / US")
    fun.add_argument("--code", required=True, help="如 601088")
    fun.add_argument("--as-of", help="YYYY-MM-DD，默认今天（PIT：只采信这天已披露的期）")
    fun.add_argument(
        "--roe-basis",
        default=ROE_BASIS_ANNUAL,
        choices=list(ROE_BASIS),
        help=(
            "PR 分母用哪一期 ROE：annual=最新已披露**年报**（默认，与 PE(TTM) 同为十二个月）"
            f"（默认 {ROE_BASIS_ANNUAL}）"
        ),
    )
    fun.add_argument(
        "--roe-estimate",
        help="预估 ROE，小数口径（15% 写 0.15）：PE 不为正时那一支要用它",
    )
    fun.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    fun.set_defaults(handler=run_fundamentals)


def run_fundamentals(container: Container, args: argparse.Namespace) -> int:
    try:
        as_of = iso_date(args.as_of) if args.as_of else None
        estimate = ratio_arg("--roe-estimate", args.roe_estimate) if args.roe_estimate else None
        read = container.fundamentals.read(
            args.market,
            args.code,
            as_of=as_of,
            roe_estimate=estimate,
            roe_basis=args.roe_basis,
        )
    except ValidationError as exc:
        # D57：`--json` 下参数错也是 JSON。让它冒到顶层会变成 stderr 上的一段纯文本，
        # 而管道里的下游只会拿到一个空 stdin
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 2})
        else:
            print(f"参数有误：{exc}")
        return 2
    code = 0 if read.pr.ok else 1
    if args.json:
        _emit(_fundamentals_payload(read, code))
        return code
    return _print_fundamentals(read, code)


def _print_fundamentals(read, code: int) -> int:
    print(f"{read.market}/{read.code}  截至 {read.as_of}")
    print(f"  采信期  {_period_line(read)}")
    print(f"  估值日  {_valuation_line(read)}")
    ratio = f"PR = {read.pr.value:.3f}" if read.pr.ok else "PR = 无"
    print(f"  市赚率  {ratio:<12} ← {read.pr.explain()}")
    print(f"  {roe_basis_note(read.roe_basis)}")
    print(f"  {caliber_note()}")
    for line in _fundamentals_warnings(read):
        print(f"  {line}")
    if code == 1:
        print("    ⇒ 这次没有给出 PR：脚本按退出码 1 处理（`--json` 里 exit 同值）")
    return code


def _period_line(read) -> str:
    if read.period is None:
        return (
            f"无 ← 库里没有 {read.market} {read.code} 在「{read.roe_basis}」口径下"
            "已披露的财务数据（先跑 `sams sync financials`）"
        )
    kind = "年报" if _is_year_end(read.period) else f"{read.period.month} 月末期"
    head = (
        f"{read.period} {kind}（披露 {read.published_on}，源 {read.source}）"
        if read.published_on
        else f"{read.period} {kind}（无披露日，源 {read.source}）"
    )
    return (
        f"{head}  ROE {_ratio(read.roe)}  营收 {_signed(read.revenue_yoy)}"
        f"  利润 {_signed(read.profit_yoy)}  股息率 {_ratio(read.dividend_yield)}"
    )


def _is_year_end(day) -> bool:
    return day is not None and day.month == 12 and day.day == 31


def _valuation_line(read) -> str:
    if read.valuation_on is None:
        return "无 ← 库里没有这只票的按日估值（先跑 `sams sync valuation`）"
    return (
        f"{read.valuation_on}（源 {read.valuation_source}）  PE(TTM) {_number(read.pe_ttm)}"
        f"  PB {_number(read.pb)}  市值 {_money(read.market_cap)}"
    )


def _fundamentals_warnings(read) -> list[str]:
    out = []
    if read.interim_period is not None:
        out.append(
            f"⚠ 库里另有更新的一期 {read.interim_period}（ROE {_ratio(read.interim_roe)}，"
            f"披露 {read.interim_published_on}）未用于 PR：那个 ROE 是该期累计值，"
            f"与 PE(TTM) 的十二个月不同周期（要用它：`--roe-basis {ROE_BASIS_LATEST}`）"
        )
    if read.dropped_unpublished:
        out.append(
            f"⚠ 另有 {read.dropped_unpublished} 行没有披露日、未被采信（D61）："
            "这不等于那几期不存在，也不等于它们已经公开"
        )
    if read.valuation_on is None and read.market_cap is not None:
        out.append(
            f"⚠ 那个市值来自报告期 {read.period} 那一行（没有按日估值可配）："
            "它不是截至日当天的市值（D60）"
        )
    if read.pr.basis == BASIS_PB_ESTIMATE:
        out.append(
            "⚠ 这一支的 PR 里嵌了一个人为的 ROE 预估：它只与**同一个预估**下的数可比，"
            "不能与 PE 那一支的 PR 直接排序"
        )
    return out


def _number(value) -> str:
    return "–" if value is None else f"{value:.2f}"


def _ratio(value) -> str:
    """库里的口径是小数，屏幕上换成百分数——这一步换算只发生在打印（D58）。"""
    return "–" if value is None else f"{value:.2%}"


def _signed(value) -> str:
    return "–" if value is None else f"{value:+.2%}"


def _money(value) -> str:
    if value is None:
        return "–"
    return f"{value / 1e8:,.2f}亿" if abs(value) >= 1e8 else f"{value:,.0f}"


def _fundamentals_payload(read, code: int) -> dict:
    return {
        "status": "ok" if read.pr.ok else "refused",
        "exit": code,
        "market": read.market,
        "code": read.code,
        "as_of": str(read.as_of),
        "period": str(read.period) if read.period else None,
        "published_on": str(read.published_on) if read.published_on else None,
        # JSON 里给**库里的口径**（小数），不给屏幕上的百分数：两套口径同存一个文件
        # 迟早有人在某一侧再乘一次 100
        "roe": read.roe,
        "revenue_yoy": read.revenue_yoy,
        "profit_yoy": read.profit_yoy,
        "dividend_yield": read.dividend_yield,
        "pe_ttm": read.pe_ttm,
        "pb": read.pb,
        "market_cap": read.market_cap,
        "valuation_on": str(read.valuation_on) if read.valuation_on else None,
        "source": read.source,
        "valuation_source": read.valuation_source,
        "dropped_unpublished": read.dropped_unpublished,
        # D62：分母是哪一种周期，是这个数可不可比的前提，脚本侧必须能读到它
        "roe_basis": read.roe_basis,
        "interim_period": str(read.interim_period) if read.interim_period else None,
        "interim_roe": read.interim_roe,
        "interim_published_on": (
            str(read.interim_published_on) if read.interim_published_on else None
        ),
        "pr": {
            "value": read.pr.value,
            "basis": read.pr.basis,
            "roe_used": read.pr.roe_used,
            "estimated": read.pr.estimated,
            "reason": read.pr.reason or read.pr.explain(),
        },
        "caliber": caliber_note(),
        "basis_note": roe_basis_note(read.roe_basis),
    }


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

    ser = analysis.add_parser(
        "series",
        help="复权序列 + 指标：与 `return` 同一条取数路径、同一道闸（U20 / spec §8.2）",
    )
    ser.add_argument("--market", required=True, help="A / HK / US")
    ser.add_argument("--code", required=True, help="如 601088")
    ser.add_argument("--start", required=True, help="YYYY-MM-DD")
    ser.add_argument("--end", required=True, help="YYYY-MM-DD")
    ser.add_argument(
        "--period",
        default="day",
        choices=list(PERIODS),
        help="K 线周期：周与月由日线现算，不入库（spec §8.2）",
    )
    ser.add_argument(
        "--indicators",
        help=(
            f"逗号分隔的指标组，可选 {', '.join(indicator_names())}"
            f"（默认 {', '.join(default_indicators())}）"
        ),
    )
    ser.add_argument("--tail", type=int, default=10, help="打印最后几行（0 = 全部）")
    ser.add_argument(
        "--listed-on",
        help="上市首日：豁免它起 10 个日历日内的跳变检查（D37），用了会跟着数打印",
    )
    ser.add_argument("--json", action="store_true", help="机器可读输出（含整张表）")
    ser.set_defaults(handler=run_series)

    add_fundamentals(analysis)


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


# --- series：一条过闸序列 + 指标 -------------------------------------------------------


def run_series(container: Container, args: argparse.Namespace) -> int:
    start, end = iso_date(args.start), iso_date(args.end)
    listed_on = iso_date(args.listed_on) if args.listed_on else None
    names = (
        [name.strip() for name in args.indicators.split(",") if name.strip()]
        if args.indicators
        else None
    )
    try:
        read = container.analysis.series(
            args.market,
            args.code,
            start=start,
            end=end,
            period=args.period,
            indicators=names,
            listed_on=listed_on,
        )
    except NotAdjustable as exc:
        # 与 `return` 同一条拒绝路：这道闸拒的原因、下一步命令，都是同一套文案在说的事
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 1})
        else:
            print(f"拒绝出数：{exc}")
        return 1
    except ValidationError as exc:
        # D57：`--json` 模式下所有出口都应是 JSON——参数拼错（周期名、指标名、
        # 反向区间）也是拒绝，不是程序崩溃；走顶层 SamsError 的纯文本路径在管道里不可解析
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 2})
        else:
            print(f"参数有误：{exc}")
        return 2
    if args.json:
        _emit(_series_payload(read))
        return 0
    return _print_series(read, tail=args.tail)


def _print_series(read, *, tail: int) -> int:
    print(
        f"{read.market}/{read.code}  {read.period}  "
        f"{read.requested_from} ~ {read.requested_to}  表里 {read.rows} 行"
    )
    print(
        f"  采信  覆盖确认：{read.covered_by}，记录截至 {read.covered_through}"
        f"；读取窗口内 {read.ex_dates} 个除权日已折进因子"
    )
    print(
        f"  预热  往回多读 {read.warmup_bars} 根；后复权倍数锚在 {read.anchored_on}——"
        "绝对价位只在这个锚以下可比，跨锚比请看比值"
    )
    print(f"  指标  {', '.join(read.indicators)}（`–` = 样本不足，不是 0）")
    for line in _table_lines(read.table, tail=tail):
        print(f"  {line}")
    for line in _series_warnings(read):
        print(f"  {line}")
    return 0


def _table_lines(table, *, tail: int) -> list[str]:
    """列宽按内容算，不假设日期 10 位、价格 6 位：A 股几分钱的票与美股几百的票同居一表。"""
    shown = table.tail(tail) if tail > 0 else table
    columns = table.columns
    rows = [[_cell(c, v) for c, v in zip(columns, row, strict=True)] for row in shown.iter_rows()]
    widths = [
        max(len(header), *(len(r[i]) for r in rows)) if rows else len(header)
        for i, header in enumerate(columns)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(columns, widths, strict=True))]
    lines += ["  ".join(v.rjust(w) for v, w in zip(r, widths, strict=True)) for r in rows]
    if shown.height < table.height:
        hidden = table.height - shown.height
        lines.append(f"…… 上面只打最后 {shown.height} 行，还有 {hidden} 行（`--tail 0` 全打）")
    return lines


def _cell(column: str, value) -> str:
    if value is None:
        return "–"
    if column in ("date", "first", "last"):
        return str(value)
    if column == "pending":
        # 「收盘」而不是 `False`：这一列说的是一根 K 是不是定型的，写 False 得让人先猜列的含义
        return "未走完" if value else "已收市"
    if column == "bars":
        return str(value)
    if column in ("volume", "amount"):
        return f"{value:,.0f}"
    return f"{value:.2f}"


def _series_warnings(read) -> list[str]:
    out = []
    if read.warmup_floor is not None:
        out.append(
            f"⚠ 预热只退到 {read.warmup_floor}：库里更早的 K 线落在那条覆盖确认之外，"
            "没有拿来递推（D36）。表头那几行的长窗口指标因此可能给不出值——那不是漏算"
        )
    elif read.warmup_bars == 0:
        out.append(
            "⚠ 没有预热根数：第一个指标值就从请求起日自己起算。EMA/MACD/KDJ 这类递推指标的"
            "种子就是这一天，与预热更长的稳定值会有出入（`ma` 那种样本不足给 `–` 的不受影响）"
        )
    pending = read.pending_periods
    if pending:
        out.append(
            f"⚠ 未走完的周期：{_days(pending)}。它在表里与一根定型的周/月 K 同形，"
            "别当收盘结论用"
        )
    if read.listed_on is not None:
        out.append(
            f"⚠ 按你给的上市日 {read.listed_on} 豁免了它起 10 个日历日内的跳变检查："
            "这条序列没有 D37 那条「不可能跳变」的证据"
        )
    return out


def _series_payload(read) -> dict:
    columns = read.table.columns
    return {
        "status": "ok",
        "exit": 0,
        "market": read.market,
        "code": read.code,
        "period": read.period,
        "requested_start": str(read.requested_from),
        "requested_end": str(read.requested_to),
        "rows": read.rows,
        "last_row": str(read.last_row) if read.last_row else None,
        "warmup_bars": read.warmup_bars,
        "warmup_floor": str(read.warmup_floor) if read.warmup_floor else None,
        "anchored_on": str(read.anchored_on),
        "covered_by": read.covered_by,
        "covered_through": str(read.covered_through),
        "ex_dates": read.ex_dates,
        "listed_on": str(read.listed_on) if read.listed_on else None,
        "indicators": list(read.indicators),
        "pending_periods": [str(d) for d in read.pending_periods],
        "columns": columns,
        "table": [
            {c: _json_cell(v) for c, v in zip(columns, row, strict=True)}
            for row in read.table.iter_rows()
        ],
    }


def _json_cell(value) -> object:
    """`date`/`datetime` 不能直接进 JSON，其余原样（`None` 保持 `null`，别涂成 0）。"""
    return str(value) if isinstance(value, date) else value
