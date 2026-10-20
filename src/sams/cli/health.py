"""`sams health` —— 一屏看清库、日历、数据源是否正常。"""

from __future__ import annotations

import argparse

from sams.bootstrap import Container


def add_to(subparsers) -> None:
    parser = subparsers.add_parser("health", help="显示各表行数、日历覆盖、各源最近错误")
    parser.set_defaults(handler=show)


def _print_data(report) -> None:
    """行数说明不了「能不能用」，这一屏要回答的是新鲜度与字段覆盖。

    `bars_daily = 30000` 可能是 300 只票十年，也可能是三天前停更的三只。
    成交额全空也一样：它不是错误，但依赖成交额的那条规则从此永远不会触发，
    而规则编辑器不会告诉你这件事——只有这一屏会。
    """
    print("[数据]")
    markets = sorted(set(report.market_data) | set(report.calendar_years))
    for market in markets:
        data = report.market_data.get(market)
        if data is None or data.rows == 0:
            print(f"  {market:<4} （无日线，还没采集过）")
            continue
        line = f"  {market:<4} {data.rows:>8} 行 · {data.codes} 只 · "
        line += f"最新 {data.latest:%Y-%m-%d}" if data.latest else "最新 （无）"
        if data.amount_missing:
            pct = data.with_amount / data.rows
            line += f"  ⚠ 成交额只有 {pct:.0%} 的行有值，依赖它的规则不会触发"
        if data.negative_volume:
            # 库里原样存负量（见 bars 仓库的文件头），所以必须在这里露头
            line += f"  ⚠ {data.negative_volume} 行成交量为负，量比一类指标不可用"
        if data.codes_without_actions:
            # 这一格是「能不能算」的判据，不是数据量：这些票的区间涨跌会撞守卫
            line += (
                f"  ⚠ {data.codes_without_actions} 只有 K 线但除权记录从没确认过，"
                f"跨除权日的区间涨跌拒绝出数（sams sync actions --market {market} --code …）"
            )
        print(line)
    has_bars = any(d.rows for d in report.market_data.values())
    if has_bars and not report.tables.get("corp_actions"):
        # 裸价（spec §5.5）本身是选择，跨除权日拿裸价算区间涨跌是错误
        print("  ⚠ corp_actions 0 行：库存裸价，跨除权日的区间涨跌幅不可用")


def _d(value) -> str:
    return "–" if value is None else f"{value:%Y-%m-%d}"


def _print_fundamentals(container: Container, report) -> None:
    """`sams screen pr` 与 `analysis fundamentals` 的输入边界，按市场一屏说完。

    报的是**只数**：A 股主档 5000 多只，同步过基本面的是其中一小撮。「今天没有一只满足
    PR ≤ 0.4」在这一格里是「市场不便宜」，在只同步了 1 只的那一格里是「还没看其余」。
    资金流那一栏跟着链上的能力：库里 0 行可能只是没跑，链上 0 个源则是跑也跑不出来（D38）。
    """
    print("[基本面覆盖]")
    for market, cov in report.fundamentals.items():
        line = (
            f"  {market:<4} 财务 {cov.financials_codes} 只"
            f"（最新报告期 {_d(cov.financials_latest_period)}，"
            f"最新披露 {_d(cov.financials_latest_published)}）"
            f" · 估值 {cov.valuation_codes} 只（最新 {_d(cov.valuation_latest)}）"
            f" · 资金流 {cov.flow_codes} 只（最新 {_d(cov.flow_latest)}）"
        )
        flow_sources = container.registry.chain_for(market, "funding_flow")
        marks = []
        if not flow_sources:
            marks.append("链上没有资金流源，`sams sync flow` 在这个市场取不回任何东西")
        elif len(flow_sources) == 1:
            marks.append(f"资金流单源（{flow_sources[0]}），挂了没有备源")
        if cov.financials_codes == 0:
            marks.append("`sams screen pr` 在这个市场没有输入")
        print(line + "".join(f"  ⚠ {m}" for m in marks))


def _print_ledger(report) -> None:
    """账本那一节：三个数 + 每个币种对各自的新鲜度。

    「账本动过而 `fx_rates` 一行没有」必须单独说一句：那一屏上的 0 会被读成「这个库不需要
    汇率」，而它的实际后果是跨币种持仓的换汇影响从此拆不出（`positions` 那一行会变成
    「换不出」）。陈旧也按**对**点名，不给一个全库最新日——USD 新、HKD 旧时，被那个数
    背书的恰好是错的那一笔。
    """
    ledger = report.ledger
    print("[账本]")
    print(f"  账户 {ledger.accounts} · 成交 {ledger.orders} 笔 · 持有 {ledger.positions} 只")
    if not ledger.fx:
        if ledger.accounts:
            print(
                "  ⚠ fx_rates 0 行而账本已经动过：跨币种持仓的换汇影响拆不出"
                "（`sams sync fx --base USD --quote CNY …`，取不到时 `sams ledger fx set`）"
            )
        return
    for pair in ledger.fx:
        line = (
            f"  汇率 {pair.base}→{pair.quote}  最新 {pair.latest}"
            f"（距今 {pair.age_days} 天，source={pair.source or '—'}）"
        )
        print(line)
    for pair in ledger.stale_pairs:
        print(
            f"  ⚠ {pair.base}→{pair.quote} 已经 {pair.age_days} 天没更新："
            f"谁把 {pair.base} 换成 {pair.quote} 都还在用这一天（`sams sync fx --base {pair.base}"
            f" --quote {pair.quote} --start {pair.latest} …`）"
        )


def show(container: Container, args: argparse.Namespace) -> int:
    report = container.health.report()
    print(f"库: {report.db_path}")
    if report.pending_migrations:
        print(f"  ⚠ 待应用迁移: {', '.join(report.pending_migrations)}")
    print("[表]")
    for name, rows in report.tables.items():
        print(f"  {name:<18} {rows:>8} 行")
    _print_data(report)
    _print_fundamentals(container, report)
    _print_ledger(report)
    print("[日历覆盖]")
    for market, years in report.calendar_years.items():
        shown = ", ".join(str(y) for y in years) or "（无）"
        print(f"  {market:<4} {shown}")
    # 链在这里必须露头：一个市场只剩单源是「暂时看不出问题」的隐患，
    # 光看各源健康是看不出来的（每个源都 ok，只是没有第二个）
    from_db = bool(container.source_admin.list())
    print(f"[降级链]（{'库里的记录' if from_db else '配置文件兜底'}）")
    for market in sorted(container.settings.markets):
        chain = container.registry.market_sources.get(market, ())
        # 按能力数备源（D38）：只有公司行为的源兜不住日线，把它算进日线备源
        # 会让「HK 只有一个源在拉价格」显示成正常
        bars = container.registry.chain_for(market, "daily_bars") if chain else ()
        actions = container.registry.chain_for(market, "corp_actions") if chain else ()
        marks = []
        if not chain:
            marks.append("没有任何可用源")
        else:
            if len(bars) == 1:
                marks.append("日线单源，主源挂了就没有备源")
            if not actions:
                marks.append("没有公司行为源，跨除权日的区间涨跌无从计算")
            elif len(actions) == 1:
                marks.append(f"公司行为单源（{actions[0]}）")
        shown = " → ".join(
            n if n in bars else f"{n}(仅公司行为)" for n in chain
        ) or "（空）"
        print(f"  {market:<4} {shown}" + "".join(f"  ⚠ {m}" for m in marks))
    print("[数据源]")
    for source in report.sources:
        state = (
            "ok" if source.ok is True
            else "未知（未调用）" if source.ok is None
            else f"失败×{source.consecutive_failures}"
        )
        line = f"  {source.name:<10} {state:<13} 市场 {','.join(source.markets)}"
        if source.last_error:
            line += f"  最近错误 {source.last_error[:60]}"
        print(line)
    for source in report.degraded_sources:
        print(f"  ⚠ {source.name} 未恢复，检查网络或换源")
    for source in report.unverified_sources:
        print(f"  · {source.name} 尚未被调用过，健康状态未知")
    return 0 if not report.degraded_sources and not report.pending_migrations else 1
