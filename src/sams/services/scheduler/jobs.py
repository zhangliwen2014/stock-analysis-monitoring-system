"""盘后链上每个 job 的功能本体：只把已有服务拼成一条链，不决定自己何时跑。

五个数据 job 覆盖一个市场的一批票，**按只隔离**：一只票的链断了，剩下的照常入库，
断的那几只进 `JobOutcome.errors`，与「已经写进行数」并存于同一个结果里。
不按只隔离的后果是「一只停牌票让整个市场今晚没有数据」，而它看起来像一次正常失败。

后面四个（汇率、日历、规则、复检）单元不是票：按**对**、按**年**、按**用户**隔离，
「坏一个不带走其余」这句是同一条，写法各是一小段循环。

窗口规则：

* 日线/按日估值/资金流/汇率：库里有最新一天就从下一天起，没有就回补 730 天
  （汇率那一头问的是「这一对还缺哪段」，所以读的是 `FxRepo.latest_date`，不看陈旧度）；
* 公司行为：除非已有「全历史且覆盖到今天」的确认，否则从 1990 年起；
* 财务：每次都从 1990 年起——报告期数据是重述的，增量窗口会漏掉旧期的修正。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from functools import partial

from sams.core.errors import DatasourceError, SamsError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
from sams.services.scheduler.types import (
    JobContext,
    JobDeps,
    JobOutcome,
    JobSpec,
)

#: 首次见到一只票时回补多久：对齐 PR 分位的样本地板 `MIN_SAMPLES = 250`
#: （`analysis/valuation/percentile.py`：少于它，「分位说的是抽样噪声而不是估值位置」）。
#: 730 个日历日是一个交易日年的约两倍，给足余量；但不保证一定凑够 250 个交易日——
#: 长期停牌的票回补下来仍可能不足，那种票本就不该有分位。
INITIAL_SYNC_DAYS = 730


def _window(latest: date | None, as_of: date) -> date | None:
    """增量窗口的起点；返回 None 表示已经追平到今天，没什么可问。"""
    start = latest + timedelta(days=1) if latest else as_of - timedelta(days=INITIAL_SYNC_DAYS)
    return None if start > as_of else start


def _units(ctx: JobContext) -> list[tuple[str, str]]:
    assert ctx.market is not None  # 数据 job 只在市场 job 上登记
    return [(ctx.market, code) for code in ctx.universe.codes(ctx.market)]


def _collect(
    units: list[tuple[str, str]], action: Callable[[str, str], int], label: str
) -> JobOutcome:
    rows = 0
    done = 0
    errors: list[str] = []
    for market, code in units:
        try:
            rows += action(market, code)
        except SamsError as exc:
            # 只接本项目的失败：一个编程错误（AttributeError 之类）不许在这里被接住，
            # 否则它出门的那一句就是「这只票取不到」，而该修的那行代码永远没人去修
            errors.append(f"{market} {code}: {exc}")
        else:
            done += 1
    note = f"{done}/{len(units)} 只{label}"
    if errors:
        note += f"，{len(errors)} 只失败"
    return JobOutcome(rows_affected=rows, note=note, errors=tuple(errors))


def run_bars(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.sync.bars.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.sync.sync_daily(market, code, start, ctx.as_of).written

    return _collect(_units(ctx), one, "日线")


def run_actions(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        covered = deps.corp_actions.coverage.for_code(market, code)
        if any(
            row.full_history and row.covered_to is not None and row.covered_to >= ctx.as_of
            for row in covered
        ):
            # 全历史源已经确认到今天：再拉一遍只会得到同一批行
            return 0
        return deps.corp_actions.sync_actions(market, code, HISTORY_START, ctx.as_of).written

    return _collect(_units(ctx), one, "公司行为")


def run_financials(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        return deps.fundamentals.sync_financials(market, code, HISTORY_START, ctx.as_of).written

    return _collect(_units(ctx), one, "财务")


def run_valuation(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.fundamentals.valuation.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.fundamentals.sync_valuation(market, code, start, ctx.as_of).written

    return _collect(_units(ctx), one, "按日估值")


def run_flow(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.fundamentals.flow.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.fundamentals.sync_flow(market, code, start, ctx.as_of).written

    return _collect(_units(ctx), one, "资金流")


#: 数据 job 的登记表：能力名（= 适配器方法名）→ job 名与实现。
#: 缺能力的市场不登记（D106）：HK/US 没有按报告期的财务源，每天一条失败会把真故障淹掉
_DATA_JOBS = (
    ("daily_bars", "bars", run_bars),
    ("corp_actions", "actions", run_actions),
    ("financials", "financials", run_financials),
    ("valuation", "valuation", run_valuation),
    ("funding_flow", "flow", run_flow),
)


def _has(deps: JobDeps, market: str, method: str) -> bool:
    """这个市场有没有这个能力。

    整个市场一个源都没配上时 `sources_for` 会抛 `DatasourceError`；
    「整块没配」与「缺这一个能力」对注册表来说是同一个答案：不登记这个 job。
    启动不该因为一个市场没配源就整个失败——健康页的降级链那一节才是它该出声的地方。
    """
    try:
        return bool(deps.registry.chain_for(market, method))
    except DatasourceError:
        return False


def run_fx(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """汇率没有市场维度：对象来自账户的币种对，窗口按每对各自的库内最新日。

    这一头的单元是「一对」而不是「一只票」，失败那句话还得带上方向（`fx_rates` 只存
    `(base, quote)` 那一个方向），所以这段循环不并进 `_collect`。接住的仍然只有
    `SamsError`：按对隔离与按只隔离是同一条规矩，用例因此在汇率这一头也单独钉了一枚。
    """
    pairs = ctx.universe.fx_pairs
    rows = 0
    done = 0
    errors: list[str] = []
    for base, quote in pairs:
        try:
            start = _window(deps.fx.fx.latest_date(base, quote), ctx.as_of)
            if start is not None:
                rows += deps.fx.sync(base, quote, start, ctx.as_of).written
        except SamsError as exc:
            errors.append(f"{base}→{quote}: {exc}")
        else:
            done += 1
    note = f"{done}/{len(pairs)} 对汇率"
    if errors:
        note += f"，{len(errors)} 对失败"
    return JobOutcome(rows, note, tuple(errors))


def run_calendar(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """当月刷新一次；12 月顺手把下一年铺好（D109：跨年那天之前要有一整年的日历）。

    按年隔离：下一年那条链挂了不该把当月已经刷到的行一起报没了——行数照收，
    坏的那一年单独进 `errors`。
    """
    assert ctx.market is not None  # 日历 job 只在市场 job 上登记
    years = [ctx.as_of.year]
    if ctx.as_of.month == 12:
        years.append(ctx.as_of.year + 1)
    rows = 0
    errors: list[str] = []
    for year in years:
        try:
            rows += deps.calendar.refresh(ctx.market, year)
        except SamsError as exc:
            errors.append(f"{ctx.market} {year} 年日历: {exc}")
    note = f"刷新 {'/'.join(map(str, years))} 年日历"
    if errors:
        note += f"，{len(errors)} 年失败"
    return JobOutcome(rows, note, tuple(errors))


def run_rules(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """只评估这个市场的对象；每用户一趟，一个用户失败不带走其余（`errors` 与行数并存）。

    `suppressed`（冷却/过滤拦下）、`skipped`（判不了）、`blocked`（展开失败）各留一个数：
    合成一个「异常数」就分不清那三件事，处置的话也不同。判过了没到的 `quiet` 不进 note：
    一个正常的夜里它占绝大多数，写出来只是噪音。
    """
    users = deps.users.list_users()
    raised = suppressed = skipped = blocked = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.rules.check(user.id, as_of=ctx.as_of, market=ctx.market)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        raised += len(report.raised)
        suppressed += len(report.suppressed)
        skipped += len(report.skipped)
        blocked += len(report.blocked)
    note = (
        f"{len(users)} 个用户：触发 {raised}、冷却 {suppressed}、"
        f"跳过 {skipped}、阻塞 {blocked}"
    )
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(raised, note, tuple(errors))


def run_recheck(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """周复检按用户隔离，与 `run_rules` 同一条规矩；行数就是这一趟比过的论数。"""
    users = deps.users.list_users()
    rows = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.recheck.run(user.id, as_of=ctx.as_of, market=ctx.market)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        rows += len(report.rows)
    note = f"{len(users)} 个用户复检 {rows} 条观点"
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(rows, note, tuple(errors))


def build_jobs(deps: JobDeps) -> tuple[JobSpec, ...]:
    """注册顺序就是同一个 tick 里的执行顺序：
    每个市场：数据链 → 周复检 → 规则；然后汇率；最后日历。

    周复检排在规则之前说的是**同一晚之内**：`thesis.drift` 读的是复检写下的
    `baseline_items`，所以一条还没有基线的论——先复检，当晚就比得出漂移，
    先跑规则，那一晚它只能拿到「还没有分项基线」那一句，得等下一晚。
    已经有基线的论不受这个顺序影响。
    """
    jobs: list[JobSpec] = []
    for market in sorted(deps.settings.markets):
        for method, name, run in _DATA_JOBS:
            if _has(deps, market, method):
                jobs.append(JobSpec(name, DAILY, market, partial(run, deps)))
        jobs.append(JobSpec("recheck", WEEKLY, market, partial(run_recheck, deps)))
        jobs.append(JobSpec("rules", DAILY, market, partial(run_rules, deps)))
    jobs.append(JobSpec("fx", FX, None, partial(run_fx, deps)))
    for market in sorted(deps.settings.markets):
        jobs.append(JobSpec("calendar", MONTHLY, market, partial(run_calendar, deps)))
    return tuple(jobs)
