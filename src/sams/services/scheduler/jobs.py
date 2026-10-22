"""数据 job 的功能本体：只把已有服务拼成一条链，不决定自己何时跑。

每个 job 覆盖一个市场的一批对象，**按只隔离**：一只票的链断了，剩下的照常入库，
断的那几只进 `JobOutcome.errors`，与「已经写进行数」并存于同一个结果里。
不按只隔离的后果是「一只停牌票让整个市场今晚没有数据」，而它看起来像一次正常失败。

窗口规则：

* 日线/按日估值/资金流：库里有最新一天就从下一天起，没有就回补 730 天；
* 公司行为：除非已有「全历史且覆盖到今天」的确认，否则从 1990 年起；
* 财务：每次都从 1990 年起——报告期数据是重述的，增量窗口会漏掉旧期的修正。
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import partial

from sams.core.errors import DatasourceError, SamsError
from sams.core.timeutil import HISTORY_START
from sams.services.scheduler.types import (
    DAILY,
    JobContext,
    JobDeps,
    JobOutcome,
    JobSpec,
)

#: 首次见到一只票时回补多久：两年足够覆盖 PR 分位的样本地板（250 个交易日）
INITIAL_SYNC_DAYS = 730


def _window(latest: date | None, as_of: date) -> date | None:
    start = latest + timedelta(days=1) if latest else as_of - timedelta(days=INITIAL_SYNC_DAYS)
    return None if start > as_of else start


def _units(ctx: JobContext) -> list[tuple[str, str]]:
    assert ctx.market is not None  # 数据 job 只在市场 job 上登记
    return [(ctx.market, code) for code in ctx.universe.codes(ctx.market)]


def _collect(units: list[tuple[str, str]], action, label: str) -> JobOutcome:
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
    启动不该因为一个市场没配源就整个失败——健康页的数据源那一节才是它该出声的地方。
    """
    try:
        return bool(deps.registry.chain_for(market, method))
    except DatasourceError:
        return False


def build_jobs(deps: JobDeps) -> tuple[JobSpec, ...]:
    jobs: list[JobSpec] = []
    for market in sorted(deps.settings.markets):
        for method, name, run in _DATA_JOBS:
            if _has(deps, market, method):
                jobs.append(JobSpec(name, DAILY, market, partial(run, deps)))
    return tuple(jobs)
