"""每日收盘摘要 v0（S1-C）：三栏、只列变化、心跳、源失败说「未更新」。

这一层只读已落库的告警与任务记录，不重新求值：摘要是当晚的快照。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.analysis.valuation.styles import VALUE
from sams.core.errors import SamsError
from sams.core.notify import SEVERITIES
from sams.datasource.news import empty_news
from sams.services.digest import DigestService
from sams.storage.repos.alert_mutes import MuteRepo
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.digests import DigestRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.news import NewsRepo
from sams.storage.repos.news_keywords import NewsHitRepo, NewsKeywordRepo
from sams.storage.repos.symbols import SymbolRepo
from sams.storage.repos.task_runs import FAILED, OK, TaskRunRepo
from sams.storage.repos.theses import ThesisRepo
from sams.storage.repos.users import UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

NOW = datetime(2026, 10, 30, 7, 30, tzinfo=UTC)
DAY = date(2026, 10, 30)
PERIOD = DAY.isoformat()


class World:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.clock = FrozenClock(NOW)
        self.users = UserRepo(db)
        self.user = self.users.create("trader", "correct-horse-battery")
        self.other_user = self.users.create("other", "correct-horse-battery")
        self.accounts = SimAccountRepo(db)
        self.account = self.accounts.open(self.user.id, "主账户", report_currency="CNY")
        self.accounts.adjust_cash(
            self.account.id, "CNY", 1_000_000.0, reason="初始", actor_id=self.user.id
        )
        self.orders = SimOrderRepo(
            db, self.accounts, markets=settings.markets, symbols=SymbolRepo(db)
        )
        self.watchlists = WatchlistRepo(db)
        self.theses = ThesisRepo(db)
        self.alerts = AlertRepo(db, clock=self.clock)
        self.mutes = MuteRepo(db, clock=self.clock)
        self.digests = DigestRepo(db, clock=self.clock)
        self.task_runs = TaskRunRepo(db, clock=self.clock)
        self.news = NewsRepo(db, clock=self.clock)
        self.keywords = NewsKeywordRepo(db, clock=self.clock)
        self.hits = NewsHitRepo(db, clock=self.clock)
        self.sent: list[str] = []
        self.service = DigestService(
            settings=settings,
            users=self.users,
            watchlists=self.watchlists,
            theses=self.theses,
            accounts=self.accounts,
            orders=self.orders,
            alerts=self.alerts,
            mutes=self.mutes,
            digests=self.digests,
            task_runs=self.task_runs,
            news_hits=self.hits,
            send=self.sent.append,
            clock=self.clock,
        )

    def hold(self, code: str, *, market: str = "A") -> None:
        currency = {"A": "CNY", "HK": "HKD", "US": "USD"}[market]
        self.orders.record_fill(
            account_id=self.account.id,
            user_id=self.user.id,
            market=market,
            code=code,
            side="buy",
            qty=100,
            price=10.0,
            currency=currency,
            ts=NOW,
        )

    def watch(self, code: str, *, market: str = "A") -> None:
        self.watchlists.add(self.user.id, market, code)

    def observe(self, code: str, *, market: str = "A") -> None:
        self.theses.create(
            user_id=self.user.id,
            market=market,
            code=code,
            style=VALUE.name,
            actor_id=self.user.id,
            now=NOW,
        )

    def alert(
        self,
        code: str,
        *,
        market: str = "A",
        at: datetime = NOW,
        value: str = "10.00",
        threshold: str = "9.50",
        rule: str = "跌破止损",
        severity: str = "strong",
        is_test: bool = False,
        user_id: str | None = None,
    ):
        return self.alerts.create(
            user_id or self.user.id,
            rule_id="r1",
            rule_name=rule,
            account_id=None,
            account_name="",
            market=market,
            code=code,
            symbol_name=f"名称{code}",
            severity=severity,
            stage="hit",
            value_text=value,
            threshold_text=threshold,
            action_note="",
            triggered_at=at,
            is_test=is_test,
        )

    def run(self, *, as_of: date = DAY, market: str = "A"):
        return self.service.run(self.user, market, as_of=as_of)

    def news_item(
        self,
        code: str,
        item_id: str = "N1",
        *,
        title: str | None = None,
        market: str = "A",
        published_at: datetime = NOW,
        summary: str | None = None,
        url: str | None = None,
    ) -> str:
        frame = pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "source": "eastmoney_f10",
                    "item_id": item_id,
                    "published_at": published_at,
                    "title": title or f"标题 {item_id}",
                    "summary": summary,
                    "url": url or f"https://example.test/{item_id}",
                }
            ],
            schema=dict(empty_news().schema),
        )
        self.news.upsert_many(frame)
        return item_id

    def hit(
        self,
        code: str,
        item_id: str = "N1",
        *,
        keyword: str = "回购",
        market: str = "A",
        at: datetime = NOW,
        title: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.news_item(code, item_id, title=title, market=market)
        owner = user_id or self.user.id
        rule = self.keywords.add(owner, market, keyword, code=code)
        self.hits.record(owner, "eastmoney_f10", item_id, rule.id, hit_at=at)

    def failed_job(self, name: str = "bars", *, market: str = "A", period: str = PERIOD):
        run_id = self.task_runs.start(name, market=market, period_key=period)
        self.task_runs.finish(run_id, status=FAILED, error="源超时")

    def ok_job(self, name: str = "bars", *, market: str = "A", period: str = PERIOD):
        run_id = self.task_runs.start(name, market=market, period_key=period)
        self.task_runs.finish(run_id, status=OK)


@pytest.fixture
def world(db, settings):
    return World(db, settings)


def test_today_alerts_are_sorted_into_hold_watch_observe_and_other(world):
    world.hold("600519")
    world.watch("601318")
    world.observe("600036")
    held = world.alert("600519", value="1500.00", threshold="1600.00", rule="止损")
    watched = world.alert("601318", severity="mid", rule="PR 低于 0.4")
    observed = world.alert("600036", severity="weak", rule="假设证伪")
    other = world.alert("000001", severity="mid", rule="放量")

    outcome = world.run()

    body = outcome.digest.body
    assert body.index("【持仓】") < body.index("【自选】") < body.index("【观察】")
    assert held.id in body
    assert watched.id in body
    assert observed.id in body
    assert "【其他】" in body and other.id in body
    assert "1500.00 / 1600.00" in body
    assert "强「止损」" in body
    assert outcome.digest.has_changes is True
    assert outcome.created is True
    assert outcome.pushed is True


def test_a_test_alert_and_a_muted_symbol_are_not_changes(world):
    world.hold("600519")
    world.watch("601318")
    world.alert("600519", is_test=True)
    world.alert("601318", market="A")
    world.mutes.add(world.user.id, "A", "601318", note="等财报")

    outcome = world.run()

    assert "600519" not in outcome.digest.body
    assert "601318" not in outcome.digest.body
    assert outcome.digest.has_changes is False
    assert outcome.digest.body.count("· 无变化") == 2


def test_yesterday_open_alerts_are_listed_once_per_symbol_and_skipped_when_deferred(world):
    world.hold("600519")
    older = world.alert("600519", at=NOW - timedelta(days=2), value="old")
    latest = world.alert("600519", at=NOW - timedelta(days=1), value="latest")
    deferred = world.alert("600519", at=NOW - timedelta(days=1), value="deferred")
    world.alerts.defer(deferred.id, user_id=world.user.id, until=NOW + timedelta(days=1))

    outcome = world.run()

    body = outcome.digest.body
    assert "昨日未处置" in body
    assert latest.id in body
    assert older.id not in body
    assert deferred.id not in body
    # T-1 只是提醒，不是「今天有变化」：变化只认今天落库的告警
    assert outcome.digest.has_changes is False


def test_a_user_without_symbols_in_the_market_gets_no_digest(world):
    world.watch("00700", market="HK")

    outcome = world.run(market="A")

    assert outcome.digest is None
    assert outcome.created is False
    assert world.digests.list_for(world.user.id) == []


def test_the_first_ten_days_push_even_without_changes(world):
    world.watch("601318")

    outcome = world.run()

    assert outcome.digest.heartbeat is True
    assert outcome.pushed is True
    assert world.sent == [outcome.digest.id]


def _seed_history(world, days: int, *, end: date) -> None:
    for offset in range(days, 0, -1):
        day = end - timedelta(days=offset)
        world.digests.save(
            user_id=world.user.id,
            market="A",
            as_of=day,
            title=f"A · {day} 收盘摘要",
            body="【持仓】\n· 无变化",
            has_changes=False,
            heartbeat=True,
        )


def test_after_the_heartbeat_quiet_days_do_not_push(world):
    world.watch("601318")
    # 2026-10-19 周一，往前十个日历日（含周末）只为把计数推到 10
    _seed_history(world, 10, end=date(2026, 10, 19))

    quiet = world.run(as_of=date(2026, 10, 20))

    assert quiet.digest.heartbeat is False
    assert quiet.pushed is False
    assert world.sent == []


def test_a_monday_a_change_or_a_source_failure_breaks_the_silence(world):
    world.watch("601318")
    _seed_history(world, 10, end=date(2026, 10, 16))

    monday = world.run(as_of=date(2026, 10, 19))
    assert monday.pushed is True

    changed = world.run(as_of=date(2026, 10, 20))
    world.alert("601318", at=datetime(2026, 10, 20, 7, 0, tzinfo=UTC))
    changed_again = world.run(as_of=date(2026, 10, 20))
    assert changed.pushed is False
    assert changed_again.created is False  # 当天已落行：重跑不补写、不补推

    world.failed_job(period="2026-10-21")
    failed = world.run(as_of=date(2026, 10, 21))
    assert failed.pushed is True


def test_a_failed_task_run_says_not_updated_instead_of_no_change(world):
    world.watch("601318")
    _seed_history(world, 10, end=date(2026, 10, 16))
    world.failed_job("bars", period="2026-10-20")
    world.failed_job("valuation", period="2026-10-20")

    outcome = world.run(as_of=date(2026, 10, 20))

    body = outcome.digest.body
    assert "未更新" in body
    assert "bars" in body and "valuation" in body
    assert "无变化" not in body
    assert outcome.digest.source_note.startswith("数据：未更新")


def test_five_quiet_trading_days_still_produce_five_digests(world):
    """验收：连续 5 个交易日零操作，每天照样产出一条（前 10 条心跳期照推）。"""
    world.watch("601318")
    days = [date(2026, 10, 26) + timedelta(days=offset) for offset in range(5)]

    outcomes = [world.run(as_of=day) for day in days]

    assert [outcome.created for outcome in outcomes] == [True] * 5
    assert all(outcome.digest.has_changes is False for outcome in outcomes)
    assert [digest.as_of for digest in world.digests.list_for(world.user.id, market="A")] == (
        list(reversed(days))
    )
    assert len(world.sent) == 5


def test_a_second_run_of_the_same_day_writes_and_pushes_nothing(world):
    world.watch("601318")
    first = world.run()
    second = world.run()

    assert first.created is True
    assert second.created is False
    assert second.pushed is False
    assert second.digest.id == first.digest.id
    assert world.sent == [first.digest.id]


def test_another_users_alert_never_enters_this_digest(world):
    world.watch("601318")
    world.alert("601318", user_id=world.other_user.id, value="别人的")

    outcome = world.run()

    assert "别人的" not in outcome.digest.body
    assert outcome.digest.has_changes is False


def test_only_the_configured_severities_are_rendered_by_their_chinese_names(world):
    world.watch("601318")
    for index, level in enumerate(SEVERITIES):
        world.alert("601318", severity=level, value=f"v{index}")

    body = world.run().digest.body

    assert "强「跌破止损」" in body
    assert "中「跌破止损」" in body
    assert "弱「跌破止损」" in body


class _FakeLimits:
    """只验摘要这一侧的接线：限额自己怎么算已经在 test_limits 里钉过。"""

    def __init__(self, lines=(), error: Exception | None = None) -> None:
        self.lines = tuple(lines)
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def breach_lines(self, username: str, market: str) -> tuple[str, ...]:
        self.calls.append((username, market))
        if self.error is not None:
            raise self.error
        return self.lines


def test_limit_breaches_land_after_the_source_note_and_count_as_changes(world):
    world.watch("601318")
    limits = _FakeLimits(["主账户 单票 601318 21.00% > 20.00%"])
    world.service.limits = limits

    outcome = world.run()

    body = outcome.digest.body
    assert "【限额】" in body
    assert body.index("【限额】") > body.index("数据：")
    assert "· 主账户 单票 601318 21.00% > 20.00%" in body
    # 一个安静的交易日，被踩红的线就是当天唯一的变化
    assert outcome.digest.has_changes is True
    assert limits.calls == [("trader", "A")]


def test_a_quiet_day_without_breaches_has_no_limit_section(world):
    world.watch("601318")
    world.service.limits = _FakeLimits()

    outcome = world.run()

    assert "【限额】" not in outcome.digest.body
    assert outcome.digest.has_changes is False


def test_an_unjudgeable_limit_is_said_out_loud_instead_of_hidden(world):
    world.watch("601318")
    world.service.limits = _FakeLimits(error=SamsError("账户总资产不为正，权重算不出来"))

    outcome = world.run()

    body = outcome.digest.body
    assert "【限额】" in body
    assert "· 限额未判：账户总资产不为正，权重算不出来" in body
    assert outcome.digest.has_changes is True
