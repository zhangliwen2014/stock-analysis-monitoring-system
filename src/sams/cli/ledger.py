"""`sams ledger`：开户、动现金、记成交、看持仓与汇总（spec §5.4）。

命令层只干三件事：把 `--user` 后面那个用户名交给服务、把结论排成一屏、每个出口都给 `--json`
（D57）。币种推定、账户归属、立论状态闸都在下面两层，这里一份都不重写——重写的那份会先失效。

退出码沿用 `sams thesis` 的规矩（D77，那两个函数已经在 `cli/common.py` 里成公共件）：
0 给出一条完整的结论，1 是没给出来（库里没这笔账、现金不够、立论还没建仓），
2 是参数本身不对。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from sams.cli.common import currency_arg, emit, exit_for, group, iso_date, refused, stamp
from sams.core.codes import canonical_market
from sams.core.errors import SamsError, ValidationError
from sams.services.portfolio import AccountSummary, PositionRow

#: 屏幕上的标签列宽：数字对齐了，两行之间才看得出差的是哪一栏
_LABEL = 12


def add_to(subparsers) -> None:
    """`sams ledger <action>`：account / cash / buy / sell / positions / summary / fx。"""
    actions = group(subparsers, "ledger", "模拟盘：开户、记成交、看持仓与汇总")
    _account_parser(actions)
    _cash_parser(actions)
    _trade_parser(actions, "buy")
    _trade_parser(actions, "sell")
    _positions_parser(actions)
    _summary_parser(actions)
    _fx_parser(actions)


def _account_parser(actions) -> None:
    add = actions.add_parser("account", help="add 开一个账户 / list 看名下有哪几个")
    add.add_argument("action", choices=("add", "list"))
    add.add_argument("--user", required=True, help="归属的用户名（不是 id）")
    add.add_argument("--name", help="账户名：`add` 必填，同一个用户名下不重名")
    add.add_argument("--broker", help="记一下这家是哪个券商（只做备注，不参与任何计算）")
    add.add_argument(
        "--margin",
        action="store_true",
        help="允许现金为负（做空/融资）。默认不允许：负余额在屏幕上像一笔丢失的注资",
    )
    add.add_argument(
        "--home-market",
        default="A",
        help="主场市场 A/HK/US（默认 A）：录入成交时默认选它，不是权限",
    )
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_account)


def _cash_parser(actions) -> None:
    add = actions.add_parser(
        "cash",
        help="给某个币种的口袋注资/取出（§5.4 规则 4：换汇在这里是显式流水，不是四舍五入）",
    )
    _account_args(add)
    add.add_argument("--currency", required=True, help="哪个币种口袋：CNY / USD / HKD …")
    add.add_argument("--amount", required=True, type=float, help="数额（正数，方向看 --withdraw）")
    add.add_argument("--withdraw", action="store_true", help="取出而不是注资")
    add.add_argument("--reason", required=True, help="这笔钱为什么动（进流水，回看时用的就是它）")
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_cash)


def _trade_parser(actions, name: str) -> None:
    add = actions.add_parser(
        name,
        help=f"{name} 并记账（标的币种由配置推定，不接受传参：§5.4 规则 1）",
    )
    _account_args(add)
    add.add_argument("--market", required=True, help="A / HK / US")
    add.add_argument("--code", required=True, help="如 601088")
    add.add_argument("--qty", required=True, type=float, help="成交数量（正数）")
    add.add_argument("--price", required=True, type=float, help="成交价，标的币种")
    add.add_argument("--fee", type=float, default=0.0, help="手续费，标的币种（默认 0）")
    add.add_argument("--thesis", help="挂到某个立论（必须已经走到 opened 之后）")
    add.add_argument(
        "--on",
        help=(
            "成交日 YYYY-MM-DD（必填）：默认「今天」在跨时区补录时必错一天，"
            "而错的那天在屏幕上与对的一天一模一样"
        ),
    )
    add.add_argument(
        "--fx-rate",
        type=float,
        help=(
            "标的币种→报告币种汇率：填了就从报告币种现金按它结算（港股通正是这样），"
            "不填就从标的币种现金扣；结算汇率同时作为盈亏拆分的基准"
        ),
    )
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_buy if name == "buy" else run_sell)


def _positions_parser(actions) -> None:
    show = actions.add_parser("positions", help="当前持仓逐行：现价、成本、两段盈亏")
    _account_args(show)
    show.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    show.set_defaults(handler=run_positions)


def _summary_parser(actions) -> None:
    show = actions.add_parser("summary", help="账户汇总：本币小计分开、逐笔换算、换汇单列")
    _account_args(show)
    show.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    show.set_defaults(handler=run_summary)


def _account_args(add) -> None:
    add.add_argument("--user", required=True, help="归属的用户名（不是 id）")
    add.add_argument("--account", required=True, help="`account add` 屏幕上那个 id")


def _fx_parser(actions) -> None:
    """汇率的手工出口，唯一一条不吃 `--user` 的 `ledger` 子命令。

    它写的不是某个人的账，是一条全局事实：`fx_rates` 那一行没有归属，硬加 `--user`
    会读成「这行汇率只对该用户生效」，而换算时谁都不会被问到是谁。
    """
    add = actions.add_parser(
        "fx",
        help="set 手工填一行汇率（`sams sync fx` 那条链取不到时的出口）",
    )
    add.add_argument("action", choices=("set",))
    add.add_argument("--day", required=True, help="哪一天：YYYY-MM-DD")
    add.add_argument("--base", required=True, help="1 单位哪种币种：USD / HKD …")
    add.add_argument("--quote", required=True, help="换成哪种币种：CNY …")
    add.add_argument("--rate", required=True, type=float, help="那一天的汇率（正数）")
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_fx)


# --- 成交时刻 -------------------------------------------------------------------------------


def _moment(args: argparse.Namespace, container, market: str) -> datetime:
    """成交时刻：`--on` 那天市场本地 15:00（收盘附近）。

    时区的唯一出处是 `MarketCfg.zone()`：CLI 硬编码 UTC 会让 A 股的一笔差 8 小时。
    `--on` 的必填在这里查而不是交给 argparse：拒的那一句也要能走 `--json` 那个出口（D57），
    而 argparse 的报错只有 stderr 和一个 2。默认「今天」则是另一回事——补录时会静默记错一天。
    """
    if args.on is None:
        raise ValidationError("--on 是必填的：记账要一个成交日（YYYY-MM-DD）")
    day = iso_date(args.on)
    zone = container.settings.market_cfg(canonical_market(market)).zone()
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=zone)


# --- 命令 ----------------------------------------------------------------------------------


def run_account(container, args) -> int:
    try:
        if args.action == "list":
            accounts = container.ledger.accounts_of(args.user)
            payload = {
                "status": "ok",
                "accounts": [_account_payload(row) for row in accounts],
            }
        else:
            if not args.name:
                raise ValidationError("`account add` 要 --name：账户名是名下唯一认得出的东西")
            account = container.ledger.open_account(
                args.user,
                args.name,
                broker=args.broker,
                margin_enabled=args.margin,
                home_market=args.home_market,
            )
            payload = {"status": "ok", "account": _account_payload(account)}
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(payload)
        return 0
    if args.action == "list":
        _print_account_list(payload["accounts"], args.user)
    else:
        _print_account(payload["account"], args.user)
    return 0


def run_cash(container, args) -> int:
    try:
        if args.amount <= 0:
            raise ValidationError(
                f"--amount 要的是一个正数（方向由 --withdraw 决定），收到 {args.amount}"
            )
        move = container.ledger.withdraw if args.withdraw else container.ledger.deposit
        balance = move(
            args.account,
            args.currency,
            args.amount,
            username=args.user,
            reason=args.reason,
            actor_id=args.user,
        )
        payload = {
            "status": "ok",
            "account": args.account,
            "currency": args.currency,
            "balance": balance,
        }
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(payload)
        return 0
    verb = "取出" if args.withdraw else "注资"
    print(
        f"{verb} {args.amount:.2f} {args.currency} → 余额 {balance:.2f} {args.currency}"
        f"（{args.reason}）"
    )
    return 0


def run_buy(container, args) -> int:
    return _fill(container, args, "buy")


def run_sell(container, args) -> int:
    return _fill(container, args, "sell")


def _fill(container, args, side: str) -> int:
    book = container.ledger
    record = book.buy if side == "buy" else book.sell
    try:
        order = record(
            args.account,
            args.user,
            args.market,
            args.code,
            qty=args.qty,
            price=args.price,
            fee=args.fee,
            thesis_id=args.thesis,
            fx_rate=args.fx_rate,
            ts=_moment(args, container, args.market),
        )
        position = book.position(order.account_id, order.market, order.code)
        payload = {
            "status": "ok",
            "order": _order_payload(order),
            "position": _held_payload(position),
            "cash": book.cash(order.account_id),
        }
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(payload)
        return 0
    _print_fill(payload, args.user, stamp(order.ts))
    return 0


def run_positions(container, args) -> int:
    try:
        rows = container.portfolio.positions(args.user, args.account)
        payload = {"status": "ok", "positions": [_position_payload(row) for row in rows]}
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(payload)
        return 0
    _print_positions(rows)
    return 0


def run_summary(container, args) -> int:
    try:
        summary = container.portfolio.summary(args.user, args.account)
        payload = {"status": "ok", **_summary_payload(summary)}
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(payload)
        return 0
    _print_summary(summary)
    return 0


def run_fx(container, args) -> int:
    """手工填一行汇率：正数、有限、同币种那三道闸门都由 `FxRepo.put` 说，不在这里重问。

    打印的是**落库之后读回来的那一行**（含 `source`）：把手工填进去的字符原样回显，
    等于替库背书说「这一行在这儿」。
    """
    try:
        day = iso_date(args.day)
        base = currency_arg(args.base)
        quote = currency_arg(args.quote)
        rate = container.fx_sync.set_manual(base, quote, day, args.rate)
        payload = {"status": "ok", "rate": _fx_payload(rate)}
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))
    if args.json:
        emit(payload)
        return 0
    print(
        f"手工汇率 {rate.base}→{rate.quote}  1 {rate.base} = {rate.rate:g} {rate.quote}"
        f"  （{rate.day}，库里 source={rate.source}）"
    )
    # 换算按行情日取「当天或之前最近的一行」（D73）：填错一天不会报错，只会静默用不上
    print("  生效条件：这一天要在 `positions` 那一行的行情日当天或之前")
    return 0


# --- 输出 ----------------------------------------------------------------------------------


def _fx_payload(rate) -> dict:
    return {
        "day": str(rate.day),
        "base": rate.base,
        "quote": rate.quote,
        "rate": rate.rate,
        #: 「manual」这一列是这条命令存在的理由：下次 `sams sync fx` 覆盖时要知道覆盖掉的是什么
        "source": rate.source,
    }


def _account_payload(account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "report_currency": account.report_currency,
        "broker": account.broker,
        "margin_enabled": account.margin_enabled,
        "home_market": account.home_market,
    }


def _order_payload(order) -> dict:
    return {
        "id": order.id,
        "account": order.account_id,
        "side": order.side,
        "market": order.market,
        "code": order.code,
        "currency": order.currency,
        "qty": order.qty,
        "price": order.price,
        "fee": order.fee,
        "thesis_id": order.thesis_id,
        "fx_rate_ref": order.fx_rate_ref,
        "ts": order.ts.isoformat() if order.ts else None,
    }


def _held_payload(position) -> dict | None:
    if position is None:
        return None
    return {
        "market": position.market,
        "code": position.code,
        "currency": position.currency,
        "qty": position.qty,
        "avg_cost": position.avg_cost,
    }


def _position_payload(row: PositionRow) -> dict:
    return {
        "market": row.market,
        "code": row.code,
        "currency": row.currency,
        "report_currency": row.report_currency,
        "qty": row.qty,
        "avg_cost": row.avg_cost,
        "last_price": row.last_price,
        "last_price_date": str(row.last_price_date) if row.last_price_date else None,
        "price_pnl": row.price_pnl,
        "fx_pnl": row.fx_pnl,
        "total_report": row.total_report,
        "rate_base": row.rate_base,
        "rate_today": row.rate_today,
        "rate_stale": row.rate_stale,
        "convertible": row.convertible,
        "note": row.unavailable_note,
        "rate_note": row.rate_note,
    }


def _summary_payload(summary: AccountSummary) -> dict:
    """两个盈亏键**同时出现**，即使其中一个是 0（§5.4 规则 2）。

    靠「有没有这个键」判断有没有换汇是错的：0 与「没这一项」在 JSON 里长得一样，
    而在屏幕上它们必须是两句话。
    """
    return {
        "account": {
            "id": summary.account_id,
            "name": summary.name,
            "report_currency": summary.report_currency,
        },
        "cash_by_currency": summary.cash_by_currency,
        "position_value_by_currency": summary.position_value_by_currency,
        "total_report": summary.total_report,
        "price_pnl_report": summary.price_pnl_report,
        "fx_pnl": summary.fx_pnl,
        "total_pnl_report": summary.total_pnl_report,
        "realized_report": summary.realized_report,
        "as_of": str(summary.as_of),
        "notes": list(summary.notes),
        "duplicate_holdings": list(summary.duplicate_holdings),
    }


def _print_account(account: dict, username: str) -> None:
    margin = "，允许负余额" if account["margin_enabled"] else ""
    print(
        f"{account['name']}  报告币种 {account['report_currency']}"
        f"  主场 {account['home_market']}{margin}"
    )
    # 下一步那条命令要能直接敲：屏幕上出现的 id 与用户名，就是它缺的那两个参数
    print(
        "  下一步（数额与理由改成你的）：sams ledger cash --user "
        f"{username} --account {account['id']} --currency CNY --amount 100000 --reason 初始"
    )


def _print_account_list(accounts: list[dict], username: str) -> None:
    if not accounts:
        print(
            f"{username} 名下没有账户：下一步 "
            f"`sams ledger account add --user {username} --name 主账户`"
        )
        return
    print(f"{username} 名下 {len(accounts)} 个账户")
    for account in accounts:
        # 一行一条：20 个账户刷不出下一页（同 `sams thesis list` 那条红线）
        print(
            f"  {account['name']:<10} 报告币种 {account['report_currency']}"
            f"  主场 {account['home_market']}"
            f"  {'允许负余额' if account['margin_enabled'] else '不许负余额'}"
            f"  id {account['id']}"
        )


def _print_fill(payload: dict, username: str, when: str) -> None:
    order = payload["order"]
    fee = f"  手续费 {order['fee']:.2f}" if order["fee"] else ""
    print(
        f"{order['side']} {order['market']}/{order['code']} {order['qty']:g} 股 @ "
        f"{order['price']:g} {order['currency']}{fee}  →  {when}"
    )
    held = payload["position"]
    if held is None:
        print("  这笔之后清仓（`positions` 里不再出现，已实现盈亏在 `summary` 里）")
    else:
        print(f"  持有 {held['qty']:g} 股，均价 {held['avg_cost']:g} {held['currency']}")
    cash = "、".join(f"{ccy} {amount:.2f}" for ccy, amount in sorted(payload["cash"].items()))
    print(f"  现金：{cash or '（没有）'}")
    print(f"  下一步：sams ledger summary --user {username} --account {order['account']}")


def _print_positions(rows: list[PositionRow]) -> None:
    if not rows:
        print("空仓：一笔成交都还没有（`sams ledger buy --help` 看怎么记）")
        return
    print(f"{'市场/代码':<14}{'数量':>9}{'成本':>10}{'现价':>10}{'价格盈亏':>17}{'换汇盈亏':>17}")
    for row in rows:
        label = f"{row.market}/{row.code}"
        if row.price_pnl is None:
            print(f"{label:<14}{row.qty:>9g}{'—':>10}{'—':>10}{'—':>17}{'—':>17}")
            # 那一行为什么没有数，与它是 0，是两句不同的话
            print(f"    {row.unavailable_note}")
            continue
        day = f"（{row.last_price_date} 收盘）" if row.last_price_date else ""
        # 两段各带自己的币种：`price_pnl` 在标的币种里、`fx_pnl` 在报告币种里，
        # 共用一个后缀就是在屏幕上把两个量加成一个（§5.4 规则 3）
        print(
            f"{label:<14}{row.qty:>9g}{row.avg_cost:>10.4g}{row.last_price:>10.4g}"
            f"{row.price_pnl:>13.2f} {row.currency:<4}{row.fx_pnl:>13.2f} "
            f"{row.report_currency}{day}"
        )
        if row.rate_note:
            print(f"    {row.rate_note}")


def _print_summary(summary: AccountSummary) -> None:
    report = summary.report_currency
    print(f"{summary.name}  报告币种 {report}  锚在 {summary.as_of}")
    for ccy, amount in sorted(summary.cash_by_currency.items()):
        _line(f"现金 {ccy}", amount)
    for ccy, amount in sorted(summary.position_value_by_currency.items()):
        _line(f"持仓 {ccy}", amount)
    _line("合计", summary.total_report, unit=report, rule=True)
    _line("价格盈亏", summary.price_pnl_report, unit=report)
    _line("换汇影响", summary.fx_pnl, unit=report)
    _line("已实现", summary.realized_report, unit=report)
    # notes 与跨账户点名逐行打在数字之后：它们不是日志，是「这个合计为什么不完整」的出处
    tail = list(summary.notes) + list(summary.duplicate_holdings)
    if tail:
        print("注意：")
        for note in tail:
            print(f"  · {note}")


def _line(label: str, amount: float, *, unit: str = "", rule: bool = False) -> None:
    pad = "─" * 24 if rule else ""
    suffix = f" {unit}" if unit else ""
    print(f"  {label:<{_LABEL}}{pad} {amount:>16,.2f}{suffix}")
