"""盘后链上每个 job 的功能本体：只把已有服务拼成一条链，不决定自己何时跑。

七个数据 job 覆盖一个市场的一批票，**按只隔离**：一只票的链断了，剩下的照常入库，
断的那几只进 `JobOutcome.errors`，与「已经写进行数」并存于同一个结果里。
不按只隔离的后果是「一只停牌票让整个市场今晚没有数据」，而它看起来像一次正常失败。

后面四个（汇率、日历、规则、复检）单元不是票：按**对**、按**年**、按**用户**隔离，
「坏一个不带走其余」这句是同一条，写法各是一小段循环。

另两个的单元是整个市场或一组代码：`symbol_master` 一周一次刷整张代码表（一张表拆不成
按只隔离），`indices` 吃 `[indices]` 里那几只（D141/D145）。

窗口规则：

* 日线与指数日线：从库内最新一天**往前回 14 天**（定稿窗口，D300/D305），库里没有就回补 730 天；
* 按日估值/资金流/汇率：库里有最新一天就从下一天起，没有就回补 730 天
  （汇率那一头问的是「这一对还缺哪段」，所以读的是 `FxRepo.latest_date`，不看陈旧度）；
* 公司行为：除非已有「全历史且覆盖到今天」的确认，否则从 1990 年起；
* 财务：每次都从 1990 年起——报告期数据是重述的，增量窗口会漏掉旧期的修正。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial

from sams.core.assumptions import format_value, metric_label, op_label
from sams.core.errors import DatasourceError, SamsError
from sams.core.notify import STRONG, AlertMessage
from sams.core.timeutil import HISTORY_START
from sams.services.actors import active_users
from sams.services.assumptions import Refutation
from sams.services.nav import NAV_WINDOW_DAYS
from sams.services.news import NEWS_MARKETS, NEWS_WINDOW_DAYS
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
from sams.services.scheduler.types import (
    JobContext,
    JobDeps,
    JobOutcome,
    JobSpec,
)
from sams.storage.repos.symbol_backfills import ACTIONS, BARS

logger = logging.getLogger("sams.services.scheduler.jobs")

#: 首次见到一只票时回补多久：对齐 PR 分位的样本地板 `MIN_SAMPLES = 250`
#: （`analysis/valuation/percentile.py`：少于它，「分位说的是抽样噪声而不是估值位置」）。
#: 730 个日历日是一个交易日年的约两倍，给足余量；但不保证一定凑够 250 个交易日——
#: 长期停牌的票回补下来仍可能不足，那种票本就不该有分位。
INITIAL_SYNC_DAYS = 730

#: 日线定稿余量（日历天）：约十个交易日，够盖过一场长假。
SETTLE_DAYS = 14


def _window(latest: date | None, as_of: date) -> date | None:
    """增量窗口的起点；返回 None 表示已经追平到今天，没什么可问。"""
    start = latest + timedelta(days=1) if latest else as_of - timedelta(days=INITIAL_SYNC_DAYS)
    return None if start > as_of else start


def _settle_window(latest: date | None, as_of: date) -> date | None:
    """日线的增量窗口：从 `latest` 往前多回 `SETTLE_DAYS` 天（D300）。

    与 `_window` 差的就是这一格回退。一根 K 线写进库时值多少，取决于写它的那一刻
    收盘了没有，而 `_window` 只往前走——白天手动跑过一次、或补采队列落下一根当日
    半截 K 线之后，`latest + 1 > as_of` 让这一晚一只票都不问，那根盘中价从此永远是
    这一天的收盘价。回退不增加请求数（`fetch_daily` 是每票一次请求，不是每天一次），
    重拉又是幂等的（`upsert`），所以这一条既修当下，也把自己写坏的那批历史盖回去。
    """
    if latest is None:
        return as_of - timedelta(days=INITIAL_SYNC_DAYS)
    start = latest - timedelta(days=SETTLE_DAYS)
    return None if start > as_of else start


def _units(ctx: JobContext) -> list[tuple[str, str]]:
    assert ctx.market is not None  # 数据 job 只在市场 job 上登记
    codes = list(ctx.universe.codes(ctx.market))
    # 按本轮起跑时刻定序，不是随机：直线顺序 + 截断的后果是尾部那两百只永远轮不到，
    # 而洗牌让每轮盖住随机的一批（#380 F3）。种子确定 ⇒ 同一轮重跑同一顺序，可复现。
    # 「旋转起跑点」买不到这件事：它每分钟只挪一位，尾部仍要十几轮才浮上来。
    # 它不改幂等性（`upsert`），也不碰按每只票自己的 `latest_date` 定窗口那一段。
    if ctx.started_at is not None:
        random.Random(int(ctx.started_at.timestamp())).shuffle(codes)
    return [(ctx.market, code) for code in codes]


def _breathe(deps: JobDeps) -> None:
    """两只票之间给补数队列一次喘息（#381 / D313）。

    为什么在这里而不是只在一拍开头（`service.py` 那一处）：一拍的长度由最慢的那条按只
    循环决定（实测 45~60 分钟），把消化排在一拍开头等于让「有人正盯着的那个页面」等一拍。
    而这里已经是单线程 —— 补数用的是同一条连接、同一个线程，不引入 DuckDB 的并发写。

    吞掉一切异常是这条唯一的理由：补数是顺带做的事，它崩了的症状不该变成「公司概况
    今天没跑成」——那会把一个止损机制变成新的噪音源。
    """
    if deps.backfill_pump is None:
        return
    try:
        deps.backfill_pump()
    except Exception as exc:
        logger.warning("补数喘息口自己炸了，忽略并继续按只循环：%s", exc)


def _collect(
    deps: JobDeps,
    units: list[tuple[str, str]],
    action: Callable[[str, str], int],
    label: str,
    *,
    deadline: float | None = None,
    defer_kind: str | None = None,
) -> JobOutcome:
    """按只跑完这批单位，闸落在**两只票之间**，一条规则八种 job 共用。

    不在一只票内部打断：单只票已经有边界（适配器 `timeout=15`、退避三次），最坏 overshoot
    约一分钟，而中间打断会把半截 HTTP 响应留在栈上。

    到点之后剩下的不许被忘掉，收尾分两种，取决于补数队列表里有没有这一种活：
    `defer_kind` 非空（日线、公司行为——`symbol_backfills` 只有这两个 kind，消费者也只服务
    这两样）时交给队列，每拍 3 条慢慢消化；其余那几样不新建 kind——那要给每一种都写一条
    消费者分派，而换回来的只是「今晚没跑到的那只今晚也不会跑到」（D311）。它们靠 `_units`
    的每轮换序保证覆盖，note 说的是留待下一轮。

    **截断不是失败**：`errors` 保持空，所以 `task_runs` 那行是 ok，不进连续失败计数——
    天天被截断的链会把真故障淹掉。
    """
    rows = 0
    done = 0
    errors: list[str] = []
    stopped = len(units)
    for index, (market, code) in enumerate(units):
        if deadline is not None and deps.monotonic() >= deadline:
            stopped = index
            break
        try:
            rows += action(market, code)
        except SamsError as exc:
            # 只接本项目的失败：一个编程错误（AttributeError 之类）不许在这里被接住，
            # 否则它出门的那一句就是「这只票取不到」，而该修的那行代码永远没人去修
            errors.append(f"{market} {code}: {exc}")
        else:
            done += 1
        _breathe(deps)
    note = f"{done}/{len(units)} 只{label}"
    if errors:
        note += f"，{len(errors)} 只失败"
    truncated = stopped < len(units)
    if truncated:
        left = units[stopped:]
        note += f"，到 {deps.settings.scheduler_job_max_min} 分钟收工"
        if defer_kind is not None and deps.backfills is not None:
            for market, code in left:
                deps.backfills.enqueue(market, code, defer_kind)
            note += f"，其余 {len(left)} 只已排队"
        else:
            note += f"，其余 {len(left)} 只留待下一轮"
    return JobOutcome(
        rows_affected=rows,
        note=note,
        errors=tuple(errors),
        truncated=truncated,
    )


def run_bars(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """按只跑日线，并把「库里一根 K 线都没有」的那几只单独说出来。

    链上一个字都没回不是失败（D43：停牌、新股、区间过短都可能是 0 行），所以它不进
    `errors`；但它也不是「这只票有日线」。合进分子里的后果是页面上没有 K 线的票在
    健康页被读成成功——那 24 只北交所代码就是这么藏了两周的（D272）。

    判据取「今晚没写进任何行**且**库里仍没有一根日线」，不只看今晚的行数：后者每天会把
    几十只停牌票念成窟窿，而噪音的下场是没人再读这一句。哪几只不在这里列——空答复不标记
    补数完成，第二天仍留在 `symbol_backfills` 队列里，那才是点名的那一处。
    """
    barless = 0

    def one(market: str, code: str) -> int:
        nonlocal barless
        start = _settle_window(deps.sync.bars.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        summary = deps.sync.sync_daily(market, code, start, ctx.as_of)
        if summary.written == 0 and summary.latest_date is None:
            barless += 1
        return summary.written

    outcome = _collect(
        deps, _units(ctx), one, "日线", deadline=ctx.deadline, defer_kind=BARS
    )
    if barless:
        outcome = replace(outcome, note=f"{outcome.note}，{barless} 只一根 K 线都没有")
    if outcome.rows_affected > 0:
        deps.versions.bump("bars")
    return outcome


def run_actions(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    # 昨天没补上的行今天重新有预算：这一下是 consumer 那句「等今晚」唯一的兑现（D283）
    rearmed = (
        deps.backfills.rearm_deferred(ACTIONS) if deps.backfills is not None else 0
    )
    covered = deps.corp_actions.coverage

    def one(market: str, code: str) -> int:
        if covered.full_history_confirmed(market, code, ctx.as_of):
            # 全历史源已经确认到今天：再拉一遍只会得到同一批行
            return 0
        return deps.corp_actions.sync_actions(market, code, HISTORY_START, ctx.as_of).written

    outcome = _collect(
        deps, _units(ctx), one, "公司行为", deadline=ctx.deadline, defer_kind=ACTIONS
    )
    if rearmed:
        outcome = replace(outcome, note=f"{outcome.note}，{rearmed} 条顺延的补采重新排队")
    if outcome.rows_affected > 0:
        # 公司行为改的是复权口径：缓存的日线读数同样作废
        deps.versions.bump("bars")
    return outcome


def _bump_fundamentals(deps: JobDeps, outcome: JobOutcome) -> JobOutcome:
    """财务/估值/资金流回来就让缓存整表清一次（§5.4）。

    这三条写的是盘中快照里估值那几项的输入（`fundamentals.read`、`valuation.read` 的
    合成分）：不敲这一下，盘中线程会拿着上一期 ROE、上一日估值继续判，而页面上
    什么都没变。写 0 行不敲——缓存清空换来的是一次全市场重读，库里却没变。
    """
    if outcome.rows_affected > 0:
        deps.versions.bump("fundamentals")
    return outcome


def run_financials(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        return deps.fundamentals.sync_financials(market, code, HISTORY_START, ctx.as_of).written

    return _bump_fundamentals(
        deps, _collect(deps, _units(ctx), one, "财务", deadline=ctx.deadline)
    )


def run_valuation(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.fundamentals.valuation.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.fundamentals.sync_valuation(market, code, start, ctx.as_of).written

    return _bump_fundamentals(
        deps, _collect(deps, _units(ctx), one, "按日估值", deadline=ctx.deadline)
    )


def run_flow(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    def one(market: str, code: str) -> int:
        start = _window(deps.fundamentals.flow.latest_date(market, code), ctx.as_of)
        if start is None:
            return 0
        return deps.fundamentals.sync_flow(market, code, start, ctx.as_of).written

    return _bump_fundamentals(
        deps, _collect(deps, _units(ctx), one, "资金流", deadline=ctx.deadline)
    )


#: 公告的盘后窗口：只要最近 30 天。源一次最多给 100 条，今晚没抓到的旧公告
#: 不会影响任何计算——公告不是回测或规则的输入（D216），这里不追求全历史
ANNOUNCEMENTS_WINDOW_DAYS = 30


def run_profile(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """公司概况一票一行：窗口对它没有意义，重复执行是覆盖（D214）。"""
    return _collect(
        deps, _units(ctx), lambda m, c: deps.f10.sync_profile(m, c).written, "公司概况",
        deadline=ctx.deadline,
    )


def run_announcements(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """最近 30 天的公告：按只隔离，一只票的源答不上来不带走其余。"""
    start = ctx.as_of - timedelta(days=ANNOUNCEMENTS_WINDOW_DAYS)

    def one(market: str, code: str) -> int:
        return deps.f10.sync_announcements(market, code, start, ctx.as_of).written

    return _collect(deps, _units(ctx), one, "公告", deadline=ctx.deadline)


def run_news(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """最近 30 天的个股新闻：按只隔离；无源的市场写「未接入」而不是记一次失败。

    港美也登记（D247）：健康页要看见这一趟跑过——「这个市场没有新闻源」与
    「job 没登记」是两件事。A 股链上一个能取新闻的源都没有时同样跳过。
    """
    assert ctx.market is not None  # 新闻 job 只在市场 job 上登记
    assert deps.news is not None  # 登记前查过注入，这里只兜编程错误
    if ctx.market not in NEWS_MARKETS or not _has(deps, ctx.market, "stock_news"):
        return JobOutcome(note=f"{ctx.market} 新闻源未接入，跳过")

    start = ctx.as_of - timedelta(days=NEWS_WINDOW_DAYS)

    def one(market: str, code: str) -> int:
        return deps.news.sync(market, code, start, ctx.as_of).written

    return _collect(deps, _units(ctx), one, "新闻", deadline=ctx.deadline)


def run_industry(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """行业回填：单元是整个市场，读的是今晚刚入库的概况，不联网（D236）。

    它排在数据链之后、`spot` 之前。港美也登记：回填 0 行是合法结果，健康页
    照样看得见这一趟跑过——「这个市场没有概况源」与「job 没登记」是两件事。
    """
    assert ctx.market is not None  # 行业 job 只在市场 job 上登记
    assert deps.industry is not None
    try:
        report = deps.industry.backfill(ctx.market)
    except SamsError as exc:
        return JobOutcome(
            note=f"{ctx.market} 行业回填失败", errors=(f"{ctx.market}: {exc}",)
        )
    return JobOutcome(rows_affected=report.changed, note=f"{ctx.market} {report.note}")


#: 数据 job 的登记表：能力名（= 适配器方法名）→ job 名与实现。
#: 缺能力的市场不登记（D106）：HK/US 没有按报告期的财务源，每天一条失败会把真故障淹掉
_DATA_JOBS = (
    ("daily_bars", "bars", run_bars),
    ("corp_actions", "actions", run_actions),
    ("financials", "financials", run_financials),
    ("valuation", "valuation", run_valuation),
    ("funding_flow", "flow", run_flow),
    ("company_profile", "profile", run_profile),
    ("announcements", "announcements", run_announcements),
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


def run_symbol_master(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """代码表一周一次，单元是整个市场：一张表拆不成「按只隔离」。"""
    assert ctx.market is not None
    try:
        summary = deps.symbol_master.sync(ctx.market)
    except SamsError as exc:
        return JobOutcome(note=f"{ctx.market} 代码表失败", errors=(f"{ctx.market}: {exc}",))
    if summary.kept_old:
        note = f"{ctx.market} 代码表空答复，保留旧表"
    elif summary.kept_floor:
        # 读到半张表就停手（D315）：「N 行、关停 0」是一次正常同步的收尾，
        # 而它的真实含义是「这一轮名单不可信」——后者才需要人看一眼
        note = f"{ctx.market} 代码表比旧表少一半，保留旧表在册"
    else:
        note = f"{ctx.market} 代码表 {summary.rows} 行、关停 {summary.deactivated}"
    return JobOutcome(rows_affected=summary.rows, note=note)


def run_spot(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """全市场快照：单元是整个市场的一张横截面（拆不成按只隔离）。

    失败只说一句带市场名的话：job 失败时保留上一次那天的数据，页面照旧能读，
    只是范围行会写「这是哪天的快照」——**绝不**渲染成「这个市场没有便宜票」。
    """
    assert ctx.market is not None
    assert deps.snapshots is not None  # 登记前查过注入，这里只兜编程错误
    try:
        report = deps.snapshots.sync(ctx.market)
    except SamsError as exc:
        return JobOutcome(note=f"{ctx.market} 全市场快照失败", errors=(f"{ctx.market}: {exc}",))
    if report.empty_everywhere:
        return JobOutcome(note=f"{ctx.market} 快照空答复，保留旧表")
    note = f"{ctx.market} 快照 {report.day} {report.rows} 行"
    if report.skipped:
        note += f"，源没答 {len(report.skipped)} 只"
    return JobOutcome(rows_affected=report.rows, note=note)


#: 收盘对账的容差（相对差）。不能用绝对差：A 股低价票一档 0.01 元就是 0.3%，
#: 而 300 元的票差 0.05 元连 0.02% 都不到。0.1% 以内一律当作「快照与日线取的时刻差几分钟」
CLOSE_TOLERANCE = 0.001


def run_close_check(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """当晚的收盘快照是当天日线收盘的见证；对不上的自动排一次日线重采（D301）。

    整根没进来的也算（D310）：`close_mismatches` 是内连接，它看不见那种缺口，
    于是「断了一根」与「本来就不交易」在对账里是同一个答案，而前者能补、也该补。

    这一趟不联网、不改价，只做「查出来 + 排进队列 + 留一行读数」。屏幕上永远不该出现
    「请你重采日线」那一类话：数据不全是后台的事。重采走 `symbol_backfills` 那条队，
    消费者拉的是 730 天，所以定稿窗口（D300）够不着的老账也由这一条自动盖回去。
    """
    assert ctx.market is not None  # 对账 job 只在市场 job 上登记
    assert deps.snapshots is not None  # 登记前查过注入，这里只兜编程错误
    assert deps.backfills is not None
    audit = deps.snapshots.audit_closes(
        ctx.market, ctx.as_of, tolerance=CLOSE_TOLERANCE, within_days=SETTLE_DAYS
    )
    if audit.witness_day is None:
        return JobOutcome(note=f"{ctx.market} {ctx.as_of} 没有收盘快照：这一晚不对账")
    off = audit.mismatches
    for row in off:
        deps.backfills.enqueue(ctx.market, row.code, BARS)
    # 整个市场当天都没落，那是日线 job 那一层的活（它的单位就是整个市场）：这一层这时候
    # 会一口气报出几千只、每只都是 730 天的补采，把真正等补的那几只挤到后面（#380 F2）
    market_latest = deps.sync.bars.latest_date_for_market(ctx.market)
    landed = market_latest is not None and market_latest >= ctx.as_of
    missing = audit.missing if landed else ()
    for code in missing:
        deps.backfills.enqueue(ctx.market, code, BARS)
    note = f"{ctx.market} {ctx.as_of} 与收盘快照比对：{len(off)} 只不平，{len(missing)} 只缺整根"
    if off:
        head = "、".join(
            f"{row.code} 库{row.stored:.2f}/快照{row.witness:.2f}" for row in off[:5]
        )
        note = f"{note}，已排重采：{head}"
    if missing:
        note = f"{note}，缺的是 {'、'.join(missing[:5])}"
    if not landed:
        note = f"{note}，市场整体未落（最新 {market_latest}），缺口交给日线 job"
    return JobOutcome(rows_affected=len(off) + len(missing), note=note)


def run_screener(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """逐方案重跑启用的选股方案，只把新进名单的票推出去（S4-B）。

    排在 `spot` 之后：方案里的技术面条件读的是当晚刚落的快照；排在此处也让
    `digest` 看得到今晚的选股告警。一个方案失败不带走其余，也不带走摘要。
    """
    assert ctx.market is not None  # 监控 job 只在市场 job 上登记
    assert deps.screener_monitor is not None
    summary = deps.screener_monitor.run_market(ctx.market)
    return JobOutcome(
        rows_affected=summary.new_codes,
        note=summary.note,
        errors=summary.errors,
    )


def run_indices(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """大盘条那几只指数：窗口按每只各自的库内最新日**再往前回 14 天**，与日线同一条规则（D305）。

    单位是「一只指数」而不是「一只票」，所以它不并进 `_DATA_JOBS` 那张按票登记的表：
    指数不在 universe 里，配置里的 `[indices]` 才是它的清单（D145）。
    库里一根都没有的那几只单独点名（D306）——它们的下场不是报错，是大盘条少一段而健康页看着正常。
    """
    assert ctx.market is not None
    assert deps.sync.indices is not None  # 登记前查过注入，这里只兜编程错误

    # 与 `run_bars` 同一条判据：今晚没写进任何行**且**库里仍一根都没有（D306）。
    # 只看今晚的行数会每晚把同一只念成窟窿（源本来就没这一只的序列时尤其如此），
    # 而噪音的下场是没人再读这一句。空答复不进 `errors`：那是「没有」不是「取失败」。
    empty_series = 0

    def one(market: str, index_code: str) -> int:
        nonlocal empty_series
        start = _settle_window(deps.sync.indices.latest_date(market, index_code), ctx.as_of)
        if start is None:
            return 0
        summary = deps.sync.sync_index(market, index_code, start, ctx.as_of)
        if summary.written == 0 and summary.latest_date is None:
            empty_series += 1
        return summary.written

    units = [(ctx.market, ic) for ic in deps.settings.indices.for_market(ctx.market)]
    outcome = _collect(deps, units, one, "指数日线", deadline=ctx.deadline)
    if empty_series:
        outcome = replace(outcome, note=f"{outcome.note}，{empty_series} 只一根 K 线都没有")
    return outcome


def run_fx(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """汇率没有市场维度：对象来自账户的币种对，窗口按每对各自的库内最新日。

    不并进 `_collect` 是因为那一头的单位是 `(market, code)` 那一只票：量词「只」和失败
    那句的前缀都是按它写的，而这里的单位是「一对」。接住的仍然只有 `SamsError`——
    按对隔离与按只隔离是同一条规矩，但那份规矩是两处各写一遍的，所以四个运维 job
    每一个都在自己那一头单独钉一枚：放宽一处不该让其余三处跟着红。

    汇率不敲版本闸：它不进盘中快照缓存——告警文案里的折合是求值时现读的
    （`engine._fx`），所以这里没有「旧读数」要作废。
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
    """每月一次，每次把 `as_of` 那一年整年重铺；12 月顺手把下一年也铺上
    （D109：跨年那天之前要有一整年的日历）。

    按年隔离：下一年那条链挂了不该把当年已经刷到的行一起报没了——行数照收，
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


def run_dividends(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """把这一市场截至今晚的应得派息逐账户入账（S4-D §6.2）。

    排在数据链之后：`actions` 先落库，这一趟才看得见今晚新入库的除权日。重跑不重复
    入账——服务先查 `(account_id, kind, ref)` 再写，所以补账与每晚这一趟走同一条路。

    `warnings` 不进 `errors`：缺 `cash_per_share` 的那种行是源的窟窿，不是这一趟失败；
    它进 note 的数量，明细在 `sams portfolio dividends` 的输出里。
    """
    assert ctx.market is not None  # 分红 job 只在市场 job 上登记
    assert deps.dividends is not None and deps.sim_accounts is not None
    users = active_users(deps.users)
    rows = posted = warnings = done = 0
    errors: list[str] = []
    for user in users:
        for account in deps.sim_accounts.list_for(user.id):
            try:
                report = deps.dividends.post(account.id, ctx.as_of, market=ctx.market)
            except SamsError as exc:
                errors.append(f"{user.username} {account.name}: {exc}")
                continue
            rows += report.rows
            posted += report.skipped_existing
            warnings += len(report.warnings)
            done += 1
    note = f"{done} 个账户入账 {rows} 笔分红"
    if posted:
        note += f"，{posted} 笔已入过账"
    if warnings:
        note += f"，{warnings} 笔缺每股派息"
    if errors:
        note += f"，{len(errors)} 个账户失败"
    return JobOutcome(rows, note, tuple(errors))


def run_rate_repairs(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """把这一市场里的占位汇率（1.0000）按成交日回算（D293）。

    与分红同一形状：单元是「一个账户」，市场在调用参数里，三个市场各跑一趟、各管本市场的
    成交。排在分红之后、净值之前——补的那笔差额要进今晚的曲线，曲线必须在钱修完之后再算。

    取不到汇率、或补差会把口袋扣穿的那些笔整笔不动，明天这一趟再试：这一趟不报错，
    因为「还没修好」不是这一趟失败，而持仓卡上那句「系统会自动回算」本来就是它的去处。
    """
    assert ctx.market is not None  # 回算 job 只在市场 job 上登记
    assert deps.ledger is not None and deps.sim_accounts is not None
    users = active_users(deps.users)
    rows = done = 0
    errors: list[str] = []
    for user in users:
        for account in deps.sim_accounts.list_for(user.id):
            try:
                rows += deps.ledger.repair_placeholder_rates(
                    account.id, ctx.as_of, market=ctx.market
                )
            except SamsError as exc:
                errors.append(f"{user.username} {account.name}: {exc}")
                continue
            done += 1
    note = f"{done} 个账户回算 {rows} 笔占位汇率" if rows else f"{done} 个账户没有占位汇率要回算"
    if errors:
        note += f"，{len(errors)} 个账户失败"
    return JobOutcome(rows, note, tuple(errors))


def run_cash_audit(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """每个账户的现金余额按流水复原一次，不平就地改回来并点名那个口袋（D298）。

    全局一趟，不分市场：口袋属于账户、币种属于钱，按市场登记会把同一个口袋一夜改三遍，
    `rows_affected` 还翻三倍。

    以流水为准不是猜测：余额与流水写在同一事务里（`adjust_cash`），会不平只剩下绕开 API
    的动结果。人对着流水加一遍会把「撤销成交」那条反做读成多扣一次——任务 #377 就是这么
    开出来的，钱本来就平。这一趟把「平不平」变成一个每晚有人答一次的问题。
    """
    assert deps.sim_accounts is not None
    pockets: list[str] = []
    errors: list[str] = []
    for user in active_users(deps.users):
        for account in deps.sim_accounts.list_for(user.id):
            try:
                rows = deps.sim_accounts.reconcile_cash(account.id)
            except SamsError as exc:
                errors.append(f"{user.username} {account.name}: {exc}")
                continue
            pockets.extend(
                f"{account.name} {row.currency} {row.stored:.2f}→{row.derived:.2f}"
                for row in rows
            )
    changed = len(pockets)
    if changed:
        note = f"{changed} 个口袋按流水改回：{'、'.join(pockets)}"
    else:
        note = "所有账户的现金余额都与流水相符"
    if errors:
        note += f"，{len(errors)} 个账户失败"
    return JobOutcome(changed, note, tuple(errors))


def run_nav(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """逐个账户重放最近 400 天的净值（S4-D §6.3）。

    排在分红之后：今晚入账的那笔派息要出现在今晚的净值里。也只跑「本市场是它的
    主场市场」的账户——三个市场各跑一趟，账户却只该重建一次，否则后一趟覆盖前一趟，
    行数还翻三倍。重建是整段覆盖，重跑同一段写同样的行。
    """
    assert ctx.market is not None  # 净值 job 只在市场 job 上登记
    assert deps.nav is not None and deps.sim_accounts is not None
    start = ctx.as_of - timedelta(days=NAV_WINDOW_DAYS)
    users = active_users(deps.users)
    days = done = 0
    errors: list[str] = []
    for user in users:
        for account in deps.sim_accounts.list_for(user.id):
            if account.home_market != ctx.market:
                continue
            try:
                days += deps.nav.rebuild(account.id, start, ctx.as_of)
            except SamsError as exc:
                errors.append(f"{user.username} {account.name}: {exc}")
                continue
            done += 1
    note = f"{done} 个账户重建 {days} 天净值"
    if errors:
        note += f"，{len(errors)} 个账户失败"
    return JobOutcome(days, note, tuple(errors))


def run_rules(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """只评估这个市场的对象；每用户一趟，一个用户失败不带走其余（`errors` 与行数并存）。

    这一趟的出口是投递（`engine._dispatch` → 通知通道），所以只跑启用的账号：
    `router.dispatch` 只查「有没有这个用户」，不查状态。采集那一头不过滤，理由在
    `universe.py` 的文件头。

    `suppressed`（过滤器与冷却合起来拦下的，不是一个「冷却」数）、`skipped`（判不了）、
    `blocked`（展开失败）各留一个数：合成一个「异常数」就分不清那三件事，处置的话也不同。
    判过了没到的 `quiet` 不进 note：那一摞说的是「没到」，而这一屏问的是
    「到了什么、有什么拦着」。
    """
    users = active_users(deps.users)
    raised = suppressed = skipped = blocked = done = 0
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
        done += 1
    note = (
        f"{done}/{len(users)} 个用户：触发 {raised}、压住 {suppressed}、"
        f"判不了 {skipped}、阻塞 {blocked}"
    )
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(raised, note, tuple(errors))


def run_recheck(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """周复检按用户隔离，与 `run_rules` 同一条规矩，也只跑启用的账号——它会写基线。

    行数就是这一趟挑出来的论数，里头含「刚建立基线」与「读不到分」那两类：
    它们没有可比的东西，所以这一句不说「比过」。
    """
    users = active_users(deps.users)
    rows = done = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.recheck.run(user.id, as_of=ctx.as_of, market=ctx.market)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        rows += len(report.rows)
        done += 1
    note = f"{done}/{len(users)} 个用户复检 {rows} 条观点"
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(rows, note, tuple(errors))


def run_assumptions(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """每天把每个启用用户「我的论点」下的假设判一次；只有刚翻成证伪的发告警。

    判是每票一次读库，告警是每人每次一条：一条被证伪的假设改变的是「还买不买」，
    归在 strong。数据不足的那几条只计数（`checked`），不进告警——那不是证据。
    """
    assert ctx.market is not None  # 证伪 job 只在市场 job 上登记
    assert deps.assumptions is not None and deps.alerts is not None
    triggered_at = deps.assumptions.clock.now_utc()
    users = active_users(deps.users)
    checked = sent = done = 0
    errors: list[str] = []
    for user in users:
        try:
            report = deps.assumptions.evaluate_user(
                user.username, on=ctx.as_of, market=ctx.market
            )
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        checked += report.checked
        done += 1
        for hit in report.refuted:
            message = _refutation_message(
                hit, triggered_at=triggered_at, name=_symbol_name(deps, hit)
            )
            try:
                deps.alerts.dispatch(message, user_id=user.id, rule_id=None)
            except SamsError as exc:
                errors.append(f"{user.username} {hit.market} {hit.code}: {exc}")
            else:
                sent += 1
    note = f"{done}/{len(users)} 个用户判了 {checked} 条假设，新证伪 {sent}"
    if errors:
        note += f"，{len(errors)} 处失败"
    return JobOutcome(sent, note, tuple(errors))


def run_digest(deps: JobDeps, ctx: JobContext) -> JobOutcome:
    """每晚给启用用户写一条收盘摘要；一人失败不带走其余。

    摘要读的是这一晚已经落库的告警与 `task_runs`，所以它必须排在 `rules` 与
    `assumptions` 之后——先写摘要再判规则，正文里就永远没有今晚的变化。

    用户没有该市场的持仓/自选/观察时 `run` 返回 `digest=None`：不写空气摘要，
    也不推送。`rows_affected` 只数真正新写下的行，重跑因此是 0。
    """
    assert ctx.market is not None  # 摘要只在市场 job 上登记
    assert deps.digest is not None
    users = active_users(deps.users)
    written = pushed = done = 0
    errors: list[str] = []
    for user in users:
        try:
            outcome = deps.digest.run(user, ctx.market, as_of=ctx.as_of)
        except SamsError as exc:
            errors.append(f"{user.username}: {exc}")
            continue
        written += int(outcome.created)
        pushed += int(outcome.pushed)
        done += 1
    note = f"{done}/{len(users)} 个用户：写入 {written} 条、推送 {pushed} 条"
    if errors:
        note += f"，{len(errors)} 个用户失败"
    return JobOutcome(written, note, tuple(errors))


def _symbol_name(deps: JobDeps, hit: Refutation) -> str:
    if deps.symbols is None:
        return ""
    found = deps.symbols.get(hit.market, hit.code)
    return str(found.get("name") or "") if found is not None else ""


def _refutation_message(
    hit: Refutation, *, triggered_at: datetime, name: str
) -> AlertMessage:
    """正文里带论点页的地址：收到告警的人下一步就是去改那条观点，而不是再找一遍。"""
    reading = format_value(hit.metric, hit.value)
    threshold = f"{op_label(hit.op)} {format_value(hit.metric, hit.threshold)}"
    return AlertMessage(
        market=hit.market,
        code=hit.code,
        severity=STRONG,
        triggered_at=triggered_at,
        name=name,
        rule_name="证伪台账",
        value_text=reading,
        threshold_text=threshold,
        stage="证伪",
        action_note="到论点页复核这条假设",
        body=(
            f"{hit.on} 读到 {metric_label(hit.metric)} {reading}，"
            f"没守住「{metric_label(hit.metric)} {threshold}」。\n"
            f"论点：/theses/{hit.thesis_id}"
        ),
    )


def build_jobs(deps: JobDeps) -> tuple[JobSpec, ...]:
    """注册顺序就是同一个 tick 里的执行顺序：

    每个市场：代码表 → 数据链 → 分红 → 净值 → 行业回填 → 快照 → 方案监控 → 指数 →
    周复检 → 证伪台账 → 规则 → 摘要；然后汇率；最后日历。

    证伪台账排在数据链之后：它比的是今晚刚入库的那一期财报；排在规则之前没有依赖，
    只是让「同一晚先判假设、再判价位规则」这个读法固定下来。

    周复检排在规则之前说的是**同一晚之内**：`thesis.drift` 读的是复检写下的
    `baseline_items`，没有它就只拿到「这条论还没有分项基线」那一句。先复检，那条论当晚
    就有基线可读——但当晚比不出掉档：那份基线正是同一趟同一次读取写下的，
    自己和自己不会掉一档。要真比出漂移，得等下一晚的快照与这一晚的基线对上。
    已经有基线的论不受这个顺序影响。
    """
    jobs: list[JobSpec] = []
    # 按只循环的那几条共用一份墙钟预算（#380 F3）：一条源变慢的 job 不许把同一个 tick
    # 里排在后面的那几条饿死。挂在 job 上而不是 tick 上，理由写在 `JobSpec.max_wall_s`
    budget_s = deps.settings.scheduler_job_max_min * 60.0
    for market in sorted(deps.settings.markets):
        # 代码表排市场组最前：当晚新上市的票先进表，后面的搜索与补数才找得到它
        if deps.symbol_master is not None and _has(deps, market, "symbol_list"):
            jobs.append(JobSpec("symbol_master", WEEKLY, market, partial(run_symbol_master, deps)))
        for method, name, run in _DATA_JOBS:
            if _has(deps, market, method):
                jobs.append(
                    JobSpec(name, DAILY, market, partial(run, deps), max_wall_s=budget_s)
                )
        # 分红与净值都排在数据链之后：`actions` 先落库，分红才看得见今晚的除权日；
        # 净值重放读的是同一晚的收盘价，并且要包含刚入账的那笔派息（S4-D）
        if deps.dividends is not None and deps.sim_accounts is not None:
            jobs.append(JobSpec("dividends", DAILY, market, partial(run_dividends, deps)))
        if deps.ledger is not None and deps.sim_accounts is not None:
            jobs.append(JobSpec("rate-repair", DAILY, market, partial(run_rate_repairs, deps)))
        if deps.nav is not None and deps.sim_accounts is not None:
            jobs.append(JobSpec("nav", DAILY, market, partial(run_nav, deps)))
        # 新闻排在数据链之后、行业回填之前：公告与新闻同一晚落库，行业回填与它们无关；
        # 无源的港美也登记，job 里写「未接入」而不是从注册表里消失（D247）
        if deps.news is not None:
            jobs.append(
                JobSpec("news", DAILY, market, partial(run_news, deps), max_wall_s=budget_s)
            )
        # 行业回填读的是上面那批概况：排在数据链之后，才回填得到今晚的行业
        if deps.industry is not None:
            jobs.append(JobSpec("industry", DAILY, market, partial(run_industry, deps)))
        # 快照排在数据链之后：它读的是当天收盘价，与日线那批互不依赖，但都在盘后完成
        if deps.snapshots is not None and _has(deps, market, "market_snapshot"):
            jobs.append(JobSpec("spot", DAILY, market, partial(run_spot, deps)))
            # 收盘对账紧跟它的见证：比的是今晚这一张快照。没有补数队列就不登记——
            # 查出来却没地方排重采，那一趟只剩一句「有 N 只不平」，把问题念给人听
            if deps.backfills is not None:
                jobs.append(
                    JobSpec("close-check", DAILY, market, partial(run_close_check, deps))
                )
        # 方案监控读的是当晚刚落的快照，排在 spot 之后；先于 rules/digest，
        # 今晚的新命中才进得了同一晚的规则链与摘要（S4-B）
        if deps.screener_monitor is not None:
            jobs.append(JobSpec("screener", DAILY, market, partial(run_screener, deps)))
        if deps.sync.indices is not None and _has(deps, market, "index_daily"):
            jobs.append(
                JobSpec(
                    "indices", DAILY, market, partial(run_indices, deps), max_wall_s=budget_s
                )
            )
        jobs.append(JobSpec("recheck", WEEKLY, market, partial(run_recheck, deps)))
        if deps.assumptions is not None and deps.alerts is not None:
            jobs.append(
                JobSpec("assumptions", DAILY, market, partial(run_assumptions, deps))
            )
        jobs.append(JobSpec("rules", DAILY, market, partial(run_rules, deps)))
        if deps.digest is not None:
            jobs.append(JobSpec("digest", DAILY, market, partial(run_digest, deps)))
    # 现金对账排在全局这一组的开头：上面每个市场的账户都问过了，一夜只改一次余额；
    # 它动的是 `sim_cash_balances`，而净值读的是流水，与它互不依赖
    if deps.sim_accounts is not None:
        jobs.append(JobSpec("cash-audit", DAILY, None, partial(run_cash_audit, deps)))
    jobs.append(JobSpec("fx", FX, None, partial(run_fx, deps)))
    for market in sorted(deps.settings.markets):
        jobs.append(JobSpec("calendar", MONTHLY, market, partial(run_calendar, deps)))
    return tuple(jobs)
