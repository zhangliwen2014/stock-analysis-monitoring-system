"""四个运维 job 各自的那句话：汇率按对、日历按年、规则与复检按用户。

这里的断言看的是**问出去的对象与窗口**，不是写回来的行数：一个不问库里已有哪一天的
汇率 job 也能把今天的报价放进库，只是每晚把所有币种对重拉一遍；一个不传市场的规则 job
也能触发告警，只是 A 股那一趟顺手把美股也判了——两个市场各自的条件本该互不牵连。
「互不牵连」在四个 job 上是同一件事：坏一个（一对/一个用户）时，其余那一个照常做完，
而坏的那一个进 `errors` 而不是把整趟带走，所以每一类都钉正反两头。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from support import FrozenClock
from support_scheduler import WeekdayProvider

from sams.core.errors import CalendarSourceError, DatasourceError, ValidationError
from sams.datasource.registry import AdapterRegistry
from sams.services.calendar import CalendarService
from sams.services.fx import FxSyncService
from sams.services.scheduler.due import DAILY, FX, MONTHLY, WEEKLY
from sams.services.scheduler.jobs import (
    build_jobs,
    run_calendar,
    run_fx,
    run_recheck,
    run_rules,
)
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.storage.repos.calendar import CalendarRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.users import UserRepo

AS_OF = date(2024, 10, 25)


class StubFxAdapter:
    """按币种对回答的汇率源：给自己那几天报价，`fail_on` 那一对挂掉。

    它对「问哪一对」都答得上来（`fail_on` 之外），所以按对隔离那一枚要的效果是真的：
    坏掉的那一对之外，其余那一对仍然被问过、仍然写了行。
    `markets` 给空元组不是「这个源哪个市场都不吃」：`fetch_fx` 不走市场路由，
    它按能力遍历注册表里的每一个源——汇率这一条链没有市场维度（`services/fx.py` 的文件头）。
    """

    name = "stubfx"
    markets: tuple[str, ...] = ()

    def __init__(
        self, *, days: tuple[date, ...] = (AS_OF,), fail_on: tuple[str, str] | None = None
    ) -> None:
        self.days = days
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, date, date]] = []

    def fx_rate(self, base, quote, start, end):
        self.calls.append((base, quote, start, end))
        if (base, quote) == self.fail_on:
            raise DatasourceError(self.name, "503 服务不可用")
        # 只回被问到的那一段：源给宽区间是常态，`_call_fx` 那一头会裁掉问之外的日子
        days = [day for day in self.days if start <= day <= end]
        return pl.DataFrame(
            {
                "date": days,
                "base": [base] * len(days),
                "quote": [quote] * len(days),
                "rate": [7.1] * len(days),
                "source": [self.name] * len(days),
            }
        )


def deps(db, settings, *, fx=None, calendar=None, rules=None, recheck=None, users=None):
    """三个数据服务的槽位这里是 None——本文件不跑数据 job。

    注册表给空的那一套：`build_jobs` 因此一个数据 job 都不登记，注册表那一枚用例
    断的就只有这四个运维 job。
    """
    return JobDeps(
        settings=settings,
        registry=AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _: None),
        sync=None,  # type: ignore[arg-type]
        corp_actions=None,  # type: ignore[arg-type]
        fundamentals=None,  # type: ignore[arg-type]
        fx=fx,
        calendar=calendar,
        rules=rules,
        recheck=recheck,
        users=users or UserRepo(db),
    )


def ctx(*, market="A", pairs=()):
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market=market,
        universe=Universe(by_market={market: ()} if market else {}, fx_pairs=pairs),
    )


def fx_service(db, adapter):
    registry = AdapterRegistry(
        adapters={"stubfx": adapter}, market_sources={}, sleep=lambda _: None
    )
    return FxSyncService(registry, FxRepo(db))


# --- 汇率：窗口按每对各自问，失败按对隔离 --------------------------------------------------


def test_fx_job_starts_after_the_last_stored_day(db, settings):
    FxRepo(db).put(date(2024, 10, 24), "USD", "CNY", 7.0, source="manual")
    adapter = StubFxAdapter()
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    assert adapter.calls == [("USD", "CNY", date(2024, 10, 25), AS_OF)]
    assert outcome.rows_affected == 1
    assert FxRepo(db).latest_date("USD", "CNY") == AS_OF


def test_fx_job_backfills_the_same_window_as_the_daily_jobs_for_a_new_pair(db, settings):
    """没存过的一对走的是与日线同一条回补规则（`_window`），不是「从今天开始同步」。

    断的是问出去的区间：只写今天那一行的 job 也能把今天的报价放进库，
    而这一对从此永远缺着前面那两年。
    """
    adapter = StubFxAdapter(days=(AS_OF - timedelta(days=730), AS_OF))
    run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    _, _, start, end = adapter.calls[0]
    assert (start, end) == (AS_OF - timedelta(days=730), AS_OF)


def test_fx_job_skips_a_pair_already_at_as_of(db, settings):
    FxRepo(db).put(AS_OF, "USD", "CNY", 7.1, source="manual")
    adapter = StubFxAdapter()
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"),)),
    )
    # 追平到今天就不该问链：每晚重问一次同一个数，症状是「一切正常」
    assert adapter.calls == []
    assert outcome.rows_affected == 0 and outcome.errors == ()


def test_one_bad_pair_does_not_drop_the_other(db, settings):
    FxRepo(db).put(date(2024, 10, 24), "HKD", "CNY", 0.91, source="manual")
    adapter = StubFxAdapter(fail_on=("USD", "CNY"))
    outcome = run_fx(
        deps(db, settings, fx=fx_service(db, adapter)),
        ctx(market=None, pairs=(("USD", "CNY"), ("HKD", "CNY"))),
    )
    assert [call[:2] for call in adapter.calls] == [("USD", "CNY"), ("HKD", "CNY")]
    assert len(outcome.errors) == 1 and "USD→CNY" in outcome.errors[0]
    assert outcome.rows_affected == 1  # 坏那对之外的 HKD 照常被问到并落了库
    assert FxRepo(db).latest_date("HKD", "CNY") == AS_OF
    assert outcome.note == "1/2 对汇率，1 对失败"


def test_a_programming_error_is_not_collected_as_a_failed_pair(db, settings, monkeypatch):
    """和五个数据 job 同一条规矩：只接本项目的失败。

    被接住的编程错误出门时说的是「这一对取不到」，而该修的那行代码永远没人去修。
    汇率这一头是自己的那套循环，所以这条也得在这里单独钉一次：分叉是两次改动的事。
    """

    def boom(self, base, quote):
        raise TypeError("latest_date 收到了没有键的一行")

    monkeypatch.setattr(FxRepo, "latest_date", boom)
    with pytest.raises(TypeError):
        run_fx(
            deps(db, settings, fx=fx_service(db, StubFxAdapter())),
            ctx(market=None, pairs=(("USD", "CNY"), ("HKD", "CNY"))),
        )


# --- 日历：12 月顺手把下一年铺好 -----------------------------------------------------------


def calendar_service(db, *, day: datetime) -> CalendarService:
    return CalendarService(
        CalendarRepo(db),
        {"A": WeekdayProvider("A", "Asia/Shanghai")},
        clock=FrozenClock(day),
    )


def december_ctx(market="A") -> JobContext:
    return JobContext(
        period_key="2024-12",
        as_of=date(2024, 12, 20),
        market=market,
        universe=Universe(by_market={market: ()}, fx_pairs=()),
    )


def test_calendar_job_refreshes_december_next_year_too(db, settings):
    calendar = calendar_service(db, day=datetime(2024, 12, 20, 15, tzinfo=UTC))
    outcome = run_calendar(deps(db, settings, calendar=calendar), december_ctx())
    assert outcome.errors == ()
    assert CalendarRepo(db).year_covered("A", 2024)
    assert CalendarRepo(db).year_covered("A", 2025)


def test_calendar_job_outside_december_leaves_next_year_alone(db, settings):
    """只有 12 月那一趟才顺手铺下一年：十月铺它，一年十二次里十一次都是白问来源。"""
    calendar = calendar_service(db, day=datetime(2024, 10, 25, 15, tzinfo=UTC))
    outcome = run_calendar(deps(db, settings, calendar=calendar), ctx())
    assert outcome.errors == ()
    assert CalendarRepo(db).year_covered("A", 2024)
    assert not CalendarRepo(db).year_covered("A", 2025)


class NextYearBlind(WeekdayProvider):
    """下一年那一问挂掉的日历源：当月这一次刷新不该被它带走。"""

    def days_for_year(self, year: int):
        if year > 2024:
            raise CalendarSourceError(f"{self.market} {year} 年没有日历来源")
        return super().days_for_year(year)


def test_calendar_job_next_year_failure_keeps_the_current_year(db, settings):
    calendar = CalendarService(
        CalendarRepo(db),
        {"A": NextYearBlind("A", "Asia/Shanghai")},
        clock=FrozenClock(datetime(2024, 12, 20, 15, tzinfo=UTC)),
    )
    outcome = run_calendar(deps(db, settings, calendar=calendar), december_ctx())
    assert CalendarRepo(db).year_covered("A", 2024)
    assert not CalendarRepo(db).year_covered("A", 2025)
    assert outcome.rows_affected > 0  # 当月那一年的行还在账上
    assert len(outcome.errors) == 1 and "2025" in outcome.errors[0]


# --- 规则与复检：市场传下去，用户互不牵连 --------------------------------------------------


class RecordingRules:
    """`RuleEngine.check` 的替身：记下问出去的 `(user_id, as_of, market)`。

    报告用 `SimpleNamespace` 而不是真 `RunReport`：这一层只读报告里那几摞的长度，
    真报告的形状由引擎自己的用例钉，装在这里只会让引擎加一摞时两边一起改。
    """

    def __init__(self, *, fail_for: str | None = None) -> None:
        self.calls: list[tuple[str, date | None, str | None]] = []
        self.fail_for = fail_for

    def check(self, user_id, *, as_of=None, market=None, dry_run=False, rule_id=None):
        self.calls.append((user_id, as_of, market))
        if user_id == self.fail_for:
            raise ValidationError(f"{user_id} 这一趟取不到")
        return SimpleNamespace(raised=(1, 2), suppressed=(3, 4, 5), skipped=(6,), blocked=(7,))


class RecordingRecheck:
    def __init__(self, *, fail_for: str | None = None) -> None:
        self.calls: list[tuple[str, date | None, str | None]] = []
        self.fail_for = fail_for

    def run(self, user_id, *, as_of=None, dry_run=False, thesis_id=None, market=None):
        self.calls.append((user_id, as_of, market))
        if user_id == self.fail_for:
            raise ValidationError(f"{user_id} 这一趟读不到")
        return SimpleNamespace(rows=(1, 2, 3))


def one_user(db):
    users = UserRepo(db)
    return users, users.create("alice", "correct horse battery")


def two_users(db):
    users, alice = one_user(db)
    return users, alice, users.create("bob", "correct horse battery")


def test_rules_job_asks_for_that_market_only(db, settings):
    users, alice = one_user(db)
    recorder = RecordingRules()
    outcome = run_rules(deps(db, settings, rules=recorder, users=users), ctx())
    assert recorder.calls == [(alice.id, AS_OF, "A")]
    assert outcome.rows_affected == 2
    # 四摞各是一个数：合成一个「异常数」就分不清「冷却掉了」「判不了」「展开失败」
    assert outcome.note == "1 个用户：触发 2、冷却 3、跳过 1、阻塞 1"


def test_one_bad_user_does_not_drop_the_others_in_rules(db, settings):
    users, alice, bob = two_users(db)
    recorder = RecordingRules(fail_for=bob.id)
    outcome = run_rules(deps(db, settings, rules=recorder, users=users), ctx())
    # 挂掉那一个也真被问过（失败来自这一趟的取数，不是没排上队）
    assert [call[0] for call in recorder.calls] == [alice.id, bob.id]
    assert len(outcome.errors) == 1 and "bob" in outcome.errors[0]
    assert outcome.rows_affected == 2  # alice 那两条触发还在
    assert "1 个用户失败" in outcome.note


def test_recheck_job_asks_for_that_market_only(db, settings):
    users, alice = one_user(db)
    recorder = RecordingRecheck()
    outcome = run_recheck(deps(db, settings, recheck=recorder, users=users), ctx())
    assert recorder.calls == [(alice.id, AS_OF, "A")]
    assert outcome.rows_affected == 3
    assert outcome.note == "1 个用户复检 3 条观点"


def test_one_bad_user_does_not_drop_the_others_in_recheck(db, settings):
    users, alice, bob = two_users(db)
    recorder = RecordingRecheck(fail_for=bob.id)
    outcome = run_recheck(deps(db, settings, recheck=recorder, users=users), ctx())
    assert [call[0] for call in recorder.calls] == [alice.id, bob.id]
    assert len(outcome.errors) == 1 and "bob" in outcome.errors[0]
    assert outcome.rows_affected == 3


# --- 注册表：顺序就是执行顺序 --------------------------------------------------------------


def built(db, settings):
    return build_jobs(
        deps(
            db,
            settings,
            fx=SimpleNamespace(),
            calendar=SimpleNamespace(),
            rules=SimpleNamespace(),
            recheck=SimpleNamespace(),
        )
    )


def test_build_jobs_puts_recheck_before_rules_and_fx_after_every_market(db, settings):
    titles = [job.title for job in built(db, settings)]
    assert titles.index("recheck@A") < titles.index("rules@A")
    assert titles.index("rules@US") < titles.index("fx@全局")
    assert titles.index("fx@全局") < titles.index("calendar@A")


def test_build_jobs_registers_the_ops_jobs_on_every_market_with_their_period(db, settings):
    """四个运维 job 每一个都要有人登记，而且按各自的周期：少一行、或周期写错一格，这一枚就该红。

    写错一格不是笔误那么轻：`fx` 挂到 DAILY 上，`due_for` 对「全局 job（market=None）」直接答
    None，于是它一个晚上都不会跑——而注册表看起来是完整的。
    """
    by_title = {job.title: job for job in built(db, settings)}
    for market in sorted(settings.markets):
        assert by_title[f"recheck@{market}"].period == WEEKLY
        assert by_title[f"recheck@{market}"].market == market
        assert by_title[f"rules@{market}"].period == DAILY
        assert by_title[f"calendar@{market}"].period == MONTHLY
    assert by_title["fx@全局"].period == FX
    assert by_title["fx@全局"].market is None
