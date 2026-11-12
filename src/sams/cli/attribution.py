"""`sams attribution`：把持仓页那张归因卡打成文本（S5-D §6）。

它只读已落库的净值与每票日价值：不重放、不联网、不在自己手里算价。所以这条命令唯一的
使用顺序就是「先 `sams nav rebuild`，再读归因」，而算不出来时那句话里带着那条命令。

退出码沿用 D77：0 是这一段读完了（读不出结果也算读完，原因在 `skipped` 里），
1 是账户不存在/不属于这个名字，2 是参数本身不对。
"""

from __future__ import annotations

from datetime import timedelta

from sams.cli.common import emit, exit_for, refused
from sams.core.errors import SamsError, StorageError, ValidationError
from sams.services.attribution import AttributionReport

#: 与持仓页那三个按钮同一份口径：`0` 是「全部」，起点落到台账最早那一笔（D296）
ALL = 0


def add_to(subparsers) -> None:
    parser = subparsers.add_parser(
        "attribution",
        help="业绩归因：这段时间的收益是谁带来的（价格段/换汇段/分红分开列）",
    )
    parser.add_argument(
        "--account", required=True, help="账户 id（`sams ledger account list` 的 id 列）"
    )
    parser.add_argument(
        "--window",
        type=int,
        default=90,
        help=f"往回看多少个日历天，{ALL} = 从台账最早一笔算起（默认 90）",
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    parser.set_defaults(handler=run_report)


def run_report(container, args) -> int:
    try:
        if args.window < ALL:
            raise ValidationError(
                f"--window 要 {ALL} 或一个正整数（{ALL} = 从台账最早一笔起），收到 {args.window}"
            )
        account = container.sim_accounts.find(args.account)
        if account is None:
            raise StorageError(
                f"账户 {args.account} 不存在：`sams ledger account list` 看有哪些 id"
            )
        owner = container.users.get(account.user_id)
        end = container.clock.today()
        if args.window:
            start = end - timedelta(days=args.window)
        else:
            # 「全部」的起点由台账说话（最早一笔成交或流水），不是建账那一刻（D296）
            start = container.nav.curve_start(account.id, end)
        report = container.attribution.evaluate(owner.username, args.account, start, end)
    except SamsError as exc:
        return refused(args, exc, exit_for(exc))

    payload = _payload(report, args.window)
    if args.json:
        emit(payload)
        return 0
    _print(report, args.window)
    return 0


def _payload(report: AttributionReport, window: int) -> dict:
    benchmark = report.benchmark
    return {
        "status": "ok",
        "account": report.account_id,
        "currency": report.currency,
        "window": window,
        "start": None if report.start is None else report.start.isoformat(),
        "end": None if report.end is None else report.end.isoformat(),
        "days": report.days,
        "dropped_days": report.dropped_days,
        "window_return": report.window_return,
        "residual": report.residual,
        "residual_note": report.residual_note,
        "excess": report.excess,
        "benchmark": (
            None
            if benchmark is None
            else {
                "market": benchmark.market,
                "index_code": benchmark.index_code,
                "label": benchmark.label,
                "start": None if benchmark.start is None else benchmark.start.isoformat(),
                "end": None if benchmark.end is None else benchmark.end.isoformat(),
                "value": benchmark.value,
                "note": benchmark.note,
            }
        ),
        "rows": [
            {
                "market": row.market,
                "code": row.code,
                "name": row.name,
                "industry": row.industry,
                "weight": row.weight,
                "contribution": row.contribution,
                "price_part": row.price_part,
                "fx_part": row.fx_part,
                "dividend_part": row.dividend_part,
            }
            for row in report.rows
        ],
        "industries": [
            {"label": group.label, "weight": group.weight, "contribution": group.contribution}
            for group in report.industries
        ],
        "markets": [
            {"label": group.label, "weight": group.weight, "contribution": group.contribution}
            for group in report.markets
        ],
        "skipped": report.skipped,
    }


def _print(report: AttributionReport, window: int) -> None:
    window_label = "全部（自台账最早一笔起）" if window == ALL else f"{window} 天"
    print(f"业绩归因 {report.account_id} · 窗口 {window_label}")
    if report.skipped:
        # skipped 本身是渠道中立的口径（D343）：网页原样印，CLI 这一侧把命令补回来——
        # 终端用户能直接跑，`report.account_id` 就是真账户 id，不留占位符。
        print(f"  {report.skipped}（也可以跑 sams nav rebuild --account {report.account_id}）")
        return
    if report.rows:
        print("  标的\t区间均权\t贡献\t价格段\t换汇段\t分红")
        for row in report.rows:
            label = f"{row.name} {row.market} {row.code}"
            print(
                f"  {label}\t{_pct(row.weight)}\t{_pct(row.contribution)}"
                f"\t{_pct(row.price_part)}\t{_part(row.fx_part)}\t{_part(row.dividend_part)}"
            )
    else:
        print("  这段时间没有持仓变动，也没有可归因的价格变动。")
    if report.industries:
        groups = " · ".join(f"{g.label} {_pct(g.contribution)}" for g in report.industries)
        print(f"按行业：{groups}")
    dropped = (
        f"；丢弃缺口 {report.dropped_days} 天，缺口前后各算各的" if report.dropped_days else ""
    )
    span = f"{report.start} ~ {report.end}，共 {report.days} 天{dropped}"
    print(f"区间收益 {_pct(report.window_return)}（{span}）")
    benchmark = report.benchmark
    label = "—" if benchmark is None or not benchmark.label else benchmark.label
    value = None if benchmark is None else benchmark.value
    note = "" if benchmark is None else benchmark.note
    print(f"基准 {label} {_pct(value)} · 超额 {_pct(report.excess)}")
    if note:
        print(f"  {note}")
    print(f"残差 {_pct(report.residual)}：{report.residual_note}")
    print(
        f"  换汇段是持仓币种兑「{report.currency}」的变动，与持仓卡的「价格段/换汇段」"
        "各锚各的时点（那边锚买入日，这里锚窗口起点），所以数值不等、方向应一致。"
    )


def _pct(value: float | None) -> str:
    """取不到就印「—」：与卡片同一个字形，两处读数不一致时人能看出来，机器能读出来。"""
    return "—" if value is None else f"{value * 100:.2f}%"


def _part(value: float) -> str:
    """拆出来那一段为 0 时印「—」：A 股持仓没有换汇与分红，`0.00%` 会被读成「查过了，是零」。"""
    return "—" if not value else _pct(value)
