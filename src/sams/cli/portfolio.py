"""`sams portfolio`：组合层面的补账入口（S4-D §6.2）。

分红不是「记一笔现金」：应得多少由除权日前持股 × 每股派息算出来，归在哪个账户、
哪一天、什么币种都有固定口径。所以补账走服务，不手敲 `ledger cash`——手敲的那一笔
没有 `kind=dividend` 与 `ref`，重跑时会再记一次，净值也会把它当成外部流入。

退出码沿用 D77：0 是这一趟做完了（入账 0 笔也是做完），1 是账户不存在/读不到这类
业务拒绝，2 是参数本身不对（日期写错、市场名不对）。
"""

from __future__ import annotations

from sams.cli.common import emit, exit_for, group, iso_date, refused
from sams.core.codes import canonical_market
from sams.core.errors import SamsError
from sams.services.dividends import TAX_NOTE


def add_to(subparsers) -> None:
    """`sams portfolio <action>`：目前只有 `dividends`（补账）。"""
    actions = group(subparsers, "portfolio", "组合账：分红补账（盘后调度每晚也会跑）")
    _dividends_parser(actions)


def _dividends_parser(actions) -> None:
    add = actions.add_parser(
        "dividends",
        help="把截至 --as-of（含当天）的应得派息入账：除权日前持股 × 每股派息，税前毛额",
    )
    add.add_argument(
        "--account", required=True, help="账户 id（`sams ledger account list` 的 id 列）"
    )
    add.add_argument("--as-of", required=True, help="截至哪天，含当天：YYYY-MM-DD")
    add.add_argument(
        "--market",
        help="只补一个市场 A/HK/US；不传就是所有已配置市场（每晚调度按市场分趟跑）",
    )
    add.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    add.set_defaults(handler=run_dividends)


def run_dividends(container, args) -> int:
    try:
        as_of = iso_date(args.as_of)
        market = canonical_market(args.market) if args.market else None
        report = container.dividends.post(args.account, as_of, market=market)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))

    payload = {
        "status": "ok",
        "account": args.account,
        "as_of": as_of.isoformat(),
        "rows": report.rows,
        "skipped_existing": report.skipped_existing,
        "skipped_no_shares": report.skipped_no_shares,
        "warnings": list(report.warnings),
        "tax_note": TAX_NOTE,
        "postings": [
            {
                "market": row.market,
                "code": row.code,
                "ex_date": row.ex_date.isoformat(),
                "qty": row.qty,
                "cash_per_share": row.cash_per_share,
                "amount": row.amount,
                "currency": row.currency,
            }
            for row in report.postings
        ],
    }
    if args.json:
        emit(payload)
        return 0

    for row in report.postings:
        print(
            f"{row.ex_date} {row.market} {row.code} 派息 "
            f"{row.qty:g} 股 × {row.cash_per_share:g} = {row.amount:.2f} {row.currency}"
        )
    if report.skipped_existing:
        # 重跑不是错误：同一除权日已经入过账的条数就写在这里，0 新增才是正常运行的结果
        print(f"已入过账 {report.skipped_existing} 笔，跳过")
    if report.skipped_no_shares:
        print(f"除权日前无持股 {report.skipped_no_shares} 笔，跳过")
    for warning in report.warnings:
        print(f"提示：{warning}")
    print(f"入账 {report.rows} 笔；{TAX_NOTE}")
    return 0
