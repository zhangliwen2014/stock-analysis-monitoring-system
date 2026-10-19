"""`sams screen pr` —— 按市赚率选股。

这条命令的输出**从范围开始**，不从结果开始：库里同步过基本面的票与这个市场的票数差着
三四个数量级，而「今天一只都没命中」在这两种范围上是两件完全不同的事（前者的下一步是
再去同步几只，后者才接近「市场不便宜」）。第一行不说这个数字，屏幕上那句话就在替人
做一个我们没做过的普查（D60）。

第二件必须出声的是**算不出 PR 的票**：`PR ≤ 0.4` 想找的正是困境反转与周期底部，而那一
类票恰恰最容易算不出（PE 为负、只披露到季报、估值行停在两个月前）。静默丢掉它们，结果
看起来完整，实则把这条规则最该抓的东西筛掉了——所以每一只都点名，原因里带着下一步命令。

退出码只有一种非零：一只都没扫成（universe 为空）时给 1。「扫了，没有命中」是 0，
那是这条规则给出的一条有效答案，不是失败。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from math import isfinite

from sams.bootstrap import Container
from sams.cli.common import group, iso_date
from sams.core.errors import ValidationError
from sams.core.fundamentals import (
    PR_SCALE_DECIMALS,
    ROE_BASIS,
    ROE_BASIS_ANNUAL,
    caliber_note,
    is_year_end,
    roe_basis_note,
)
from sams.services.screen import PrHit

PR_USAGE = "例如 0.4（PR 是小数口径的比值，不是百分数：`PR ≤ 0.4` 写 0.4）"


def add_to(subparsers) -> None:
    screen = group(
        subparsers,
        "screen",
        "选股：把一条规则跑在一个说得清范围的票池上，算不出的票逐个点名",
    )
    pr = screen.add_parser(
        "pr",
        help="市赚率选股：PR ≤ 门槛的一篮子，按 PR 升序；没有 PR 的票带原因出门",
    )
    pr.add_argument("--market", required=True, help="A / HK / US")
    pr.add_argument("--pr-max", required=True, help=f"门槛，含等号：{PR_USAGE}")
    pr.add_argument("--codes", help="逗号分隔的代码，只在它们里面扫（默认=库里已同步基本面的全部）")
    pr.add_argument("--as-of", help="YYYY-MM-DD，默认今天（PIT：只采信这天已披露的期）")
    pr.add_argument(
        "--roe-basis",
        default=ROE_BASIS_ANNUAL,
        choices=list(ROE_BASIS),
        help=(
            "PR 分母用哪一期 ROE：annual=最新已披露**年报**（默认，与 PE(TTM) 同为十二个月）"
            f"（默认 {ROE_BASIS_ANNUAL}）"
        ),
    )
    pr.add_argument(
        "--include-pb-branch",
        action="store_true",
        help=(
            "把 PE 不为正那一支（现值 PB 配已披露 ROE）也排进来；"
            "它与 PE 那一支不可比，故分两组、组内升序"
        ),
    )
    pr.add_argument("--json", action="store_true", help="机器可读输出（含 exit）")
    pr.set_defaults(handler=run_pr)


def run_pr(container: Container, args: argparse.Namespace) -> int:
    try:
        threshold = _threshold(args.pr_max)
        scan = container.screen.pr_max(
            args.market,
            threshold,
            as_of=iso_date(args.as_of) if args.as_of else None,
            codes=_codes(args.codes),
            roe_basis=args.roe_basis,
            include_pb_branch=args.include_pb_branch,
        )
    except ValidationError as exc:
        # D57：`--json` 下参数错也是 JSON——让它冒到顶层，管道里的下游只会拿到空 stdin
        if args.json:
            _emit({"status": "refused", "reason": str(exc), "exit": 2})
        else:
            print(f"参数有误：{exc}")
        return 2
    code = 1 if scan.universe_size == 0 else 0
    if args.json:
        _emit(_payload(scan, code))
        return code
    return _print(scan, code)


def _threshold(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise ValidationError(f"--pr-max {value!r} 不是一个数：{PR_USAGE}") from None
    # nan 与 inf 都能被 `float()` 接住，而 `nan <= 0` 是 False：它会一路走进 payload，
    # 那里 `"threshold": NaN` 已经不是合法 JSON（Python 的 json 模块能写、别人读不了）
    if not isfinite(number):
        raise ValidationError(f"--pr-max {value!r} 要的是一个有限的数：{PR_USAGE}")
    if number <= 0:
        raise ValidationError(f"--pr-max {value} 要的是一个正数：{PR_USAGE}")
    if round(number, PR_SCALE_DECIMALS) == 0:
        # 比较是在这个精度上做的，round 成 0 的门槛就是「只收免费的票」——
        # 而 0 在 PR 的刻度上意思是免费，是这条公式最贵的一个误读（§7）
        raise ValidationError(
            f"--pr-max {value} 在比较精度（{PR_SCALE_DECIMALS} 位小数）下就是 0："
            f"门槛要写成能落进这个精度的数，{PR_USAGE}"
        )
    return number


def _codes(value: str | None) -> list[str] | None:
    if value is None:
        return None
    picked = [item.strip() for item in value.split(",") if item.strip()]
    if not picked:
        raise ValidationError("--codes 拆不出任何代码：逗号分隔，例如 601088,600000")
    return picked


def _print(scan, code: int) -> int:
    print(
        f"{scan.market}  市赚率选股  PR ≤ {_g(scan.threshold)}  截至 {scan.as_of}"
        f"（按日估值旧过 {scan.stale_within_days} 天的票进「排除」，不算落选）"
    )
    print(f"扫描范围 {_universe_line(scan)}")
    print(roe_basis_note(scan.roe_basis))
    if code == 1:
        print(_empty_line(scan))
        return code
    print(f"命中 {len(scan.hits)} 只（{_order_note(scan)}）：")
    for hit in scan.hits:
        print(f"  {_hit_line(hit)}")
    _print_unmatched(scan)
    if scan.excluded:
        print(f"排除 {len(scan.excluded)} 只（算不出 PR，或按默认口径不与其他票同列）：")
        for exclusion in scan.excluded:
            print(f"  {exclusion.code}  {exclusion.reason}")
    print(f"落选 {scan.priced_out} 只  算得出 PR、只是高于 {_g(scan.threshold)}")
    print(caliber_note())
    for line in _warnings(scan):
        print(f"⚠ {line}")
    if not scan.hits:
        print(
            f"⇒ 这一篮子是空的：{scan.priced_out} 只算得出 PR 但高于门槛，"
            f"{len(scan.excluded)} 只连 PR 都算不出——空篮子不等于「市场不便宜」"
        )
    return code


def _g(value: float) -> str:
    """门槛按命令行上敲的样子打印：`0.4` 不打印成 `0.400000`。"""
    return f"{value:g}"


def _order_note(scan) -> str:
    """「按 PR 升序」这句话只对一组成立。混进第二支时得说清是分了两组（D59）。"""
    if not scan.pb_branch_count:
        return "按 PR 升序"
    return f"分两组、组内按 PR 升序（PB 那一支 {scan.pb_branch_count} 只，排在后面）"


def _empty_line(scan) -> str:
    """「一只都没扫成」有两种，各自的下一步不同：整个市场没同步，还是 `--codes` 没命中。"""
    command = f"`sams sync financials --market {scan.market} --code <代码>`"
    if scan.unmatched:
        return (
            f"⇒ 一只都没扫成：--codes 里那 {len(scan.unmatched)} 个代码"
            f"（{'、'.join(scan.unmatched)}）在 {scan.market} 的 financials 里一行都没有"
            f"——先跑 {command}，或去掉 --codes 扫库里已有的全部"
        )
    return (
        f"⇒ 一只都没扫成：{scan.market} 在库里没有任何已同步的基本面行。先跑 {command}，"
        "这条命令的输入是**同步过的票**，不是全市场"
    )


def _print_unmatched(scan) -> None:
    """`--codes` 里没命中的代码必须点名（D64）：静默少吃一只，与「这只票不便宜」是两件事。"""
    if not scan.unmatched:
        return
    print(
        f"未同步 {len(scan.unmatched)} 只（--codes 点了名、库里的 financials 一行都没有，"
        "所以压根没进范围——不算排除）："
    )
    for code in scan.unmatched:
        print(f"  {code}  先跑 `sams sync financials --market {scan.market} --code {code}`")


def _archive_note(scan) -> str:
    """子集扫描不写存档，而屏幕上原来连「没写」这一句都没有（D57）。"""
    return (
        "这次是 --codes 子集扫描，**不存档**："
        "`scan_results` 那一行的身份是规则本身，试跑不是「今天这一篮子」——"
        "写了会覆盖当天全量那一条，而不写什么都没说"
    )


def _universe_line(scan) -> str:
    head = f"已同步基本面的 {scan.universe_size} 只"
    if scan.market_size is None:
        # 票数未知时不给 0：「主档 0 只」会被读成「这个市场没有票」，真正的原因是没有票池同步命令
        return f"{head}；这个市场主档里一行都没有，总票数未知（本系统没有全市场票池的同步入口）"
    gap = scan.market_size - scan.universe_size
    if gap > 0:
        return (
            f"{head}；主档 {scan.market_size} 只在市"
            f"——范围外的 {gap} 只先跑 `sams sync financials --market {scan.market} --code <代码>`"
        )
    if gap < 0:
        # 基本面可以按代码直采，不要求主档有行：退市票、或票池没同步都会落到这里。
        # 说成「范围外的 -N 只」是句反话——主档里没那几只票，不是「主档还等着被覆盖」
        return (
            f"{head}；主档只有 {scan.market_size} 只在市"
            f"——其中 {-gap} 只在主档里查不到在市记录（退市或票池未同步），PR 照算"
        )
    return f"{head}；主档 {scan.market_size} 只在市——本次已覆盖主档里的全部"


def _hit_line(hit: PrHit) -> str:
    period = (
        f"{hit.period} {'年报' if is_year_end(hit.period) else f'{hit.period.month} 月末期'}"
        if hit.period
        else "无期"
    )
    return (
        f"{hit.code}  PR {hit.value:.3f}  {hit.basis}  ROE {_ratio(hit.roe_used)}（{period}）"
        f"  PE(TTM) {_number(hit.pe_ttm)}  PB {_number(hit.pb)}  估值日 {hit.valuation_on}"
    )


def _warnings(scan) -> list[str]:
    """屏幕上那些 ⚠ 就是机器读者该读到的同一批话（D57）：`--json` 里叫 `warnings`。"""
    lines = []
    if not scan.archived and scan.universe_size:
        lines.append(_archive_note(scan))
    if scan.pb_branch_count:
        lines.append(
            f"命中里有 {scan.pb_branch_count} 只走的是 PB 那一支（PE 不为正）："
            "两条公式算出的 PR 不是同一个量，**不可比**——所以表按支分组、组内升序，"
            "别把它的 0.06 读成比 PE 那一支的 0.30 更便宜"
        )
    if scan.dropped_unpublished:
        lines.append(
            f"另有 {scan.dropped_unpublished} 行没有披露日、未被采信（D61）："
            "这不等于那几期不存在，也不等于它们已经公开"
        )
    return lines


def _number(value) -> str:
    return "–" if value is None else f"{value:.2f}"


def _ratio(value) -> str:
    """库里是小数口径，屏幕上换成百分数——这一步换算只发生在打印（D58）。"""
    return "–" if value is None else f"{value:.2%}"


def _payload(scan, code: int) -> dict:
    return {
        "status": "empty" if scan.universe_size == 0 else "ok",
        "exit": code,
        "market": scan.market,
        "as_of": str(scan.as_of),
        "threshold": scan.threshold,
        "roe_basis": scan.roe_basis,
        "strategy_id": scan.strategy_id,
        "archived": scan.archived,
        "universe_size": scan.universe_size,
        "market_size": scan.market_size,
        "unmatched": list(scan.unmatched),
        "hits": [_hit_json(hit) for hit in scan.hits],
        "excluded": [asdict(e) for e in scan.excluded],
        "priced_out": scan.priced_out,
        "pb_branch_count": scan.pb_branch_count,
        "warnings": _warnings(scan),
        "dropped_unpublished": scan.dropped_unpublished,
        "stale_within_days": scan.stale_within_days,
        "caliber": caliber_note(),
        "basis_note": roe_basis_note(scan.roe_basis),
    }


def _hit_json(hit: PrHit) -> dict:
    row = asdict(hit)
    row["period"] = str(hit.period) if hit.period else None
    row["valuation_on"] = str(hit.valuation_on) if hit.valuation_on else None
    return row


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
