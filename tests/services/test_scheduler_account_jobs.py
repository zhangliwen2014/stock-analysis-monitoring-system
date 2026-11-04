"""分红与净值两个 job：单元是「一个账户」，按市场分趟但一个账户只重建一次。

这两趟的共同点是**不按票隔离**：转账、派息、净值都是账户级的账，票那一层的
成败在这里没有位置。所以两侧各钉三件事：

* 认的是谁的账户（本市场是它的首页市场的那个）；
* 一趟里坏一个账户，其余照做，坏的那个进 `errors`；
* 重跑不累加——净值的窗口是固定的 400 天，分红的幂等在服务里（这里只验问到了谁）。
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from sams.core.errors import StorageError
from sams.services.dividends import DividendPosting, DividendReport
from sams.services.nav import NAV_WINDOW_DAYS
from sams.services.scheduler.jobs import (
    build_jobs,
    run_dividends,
    run_nav,
)
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.storage.repos.ledger import SimAccountRepo
from sams.storage.repos.users import UserRepo

AS_OF = date(2024, 10, 25)


def _deps(db, settings, *, dividends=None, nav=None, sim_accounts=None, chain=()):
    return JobDeps(
        settings=settings,
        registry=SimpleNamespace(chain_for=lambda market, method: chain),  # type: ignore[arg-type]
        sync=SimpleNamespace(indices=None),  # type: ignore[arg-type]
        corp_actions=None,  # type: ignore[arg-type]
        fundamentals=None,  # type: ignore[arg-type]
        f10=None,  # type: ignore[arg-type]
        fx=None,  # type: ignore[arg-type]
        calendar=None,  # type: ignore[arg-type]
        rules=None,  # type: ignore[arg-type]
        recheck=None,  # type: ignore[arg-type]
        users=UserRepo(db),
        digest=SimpleNamespace(),  # type: ignore[arg-type]
        dividends=dividends,
        nav=nav,
        sim_accounts=sim_accounts if sim_accounts is not None else SimAccountRepo(db),
    )


def _ctx(market="A"):
    return JobContext(
        period_key="2024-10-25",
        as_of=AS_OF,
        market=market,
        universe=Universe(by_market={market: ()}, fx_pairs=()),
    )


def _accounts(db):
    users = UserRepo(db)
    alice = users.create("alice", "correct horse battery")
    bob = users.create("bob", "correct horse battery")
    repo = SimAccountRepo(db)
    a = repo.open(alice.id, "A 股账户", home_market="A")
    hk = repo.open(alice.id, "港股账户", home_market="HK")
    bob_a = repo.open(bob.id, "B 的 A 股", home_market="A")
    return repo, a, hk, bob_a


class RecordingDividends:
    def __init__(self, *, fail_on: str | None = None):
        self.fail_on = fail_on
        self.calls: list[tuple[str, date, str | None]] = []

    def post(self, account_id, as_of, *, market=None):
        self.calls.append((account_id, as_of, market))
        if account_id == self.fail_on:
            raise StorageError("账户账本损坏")
        return DividendReport(
            account_id=account_id,
            postings=(
                DividendPosting(
                    market="A",
                    code="600000",
                    ex_date=AS_OF,
                    qty=100,
                    cash_per_share=0.5,
                    amount=50.0,
                    currency="CNY",
                ),
            ),
        )


def test_dividends_job_asks_every_account_for_this_market(db, settings):
    repo, a, hk, bob_a = _accounts(db)
    service = RecordingDividends()
    outcome = run_dividends(_deps(db, settings, dividends=service), _ctx())

    # 分红只问「这一市场的除权日」：账户不分市场，市场在调用参数里
    assert {call[0] for call in service.calls} == {a.id, hk.id, bob_a.id}
    assert {(call[1], call[2]) for call in service.calls} == {(AS_OF, "A")}
    assert outcome.rows_affected == 3
    assert "3 个账户入账 3 笔分红" in outcome.note
    assert outcome.ok


def test_one_broken_account_does_not_take_the_rest(db, settings):
    repo, a, hk, bob_a = _accounts(db)
    service = RecordingDividends(fail_on=a.id)
    outcome = run_dividends(_deps(db, settings, dividends=service), _ctx())

    assert outcome.rows_affected == 2
    assert len(outcome.errors) == 1 and "alice" in outcome.errors[0]
    assert "1 个账户失败" in outcome.note


def test_dividends_job_skips_existing_and_counts_warnings_separately(db, settings):
    repo, a, hk, bob_a = _accounts(db)

    class AlreadyPosted:
        def post(self, account_id, as_of, *, market=None):
            return DividendReport(
                account_id=account_id, skipped_existing=2, warnings=("600000 缺每股派息",)
            )

    outcome = run_dividends(_deps(db, settings, dividends=AlreadyPosted()), _ctx())

    assert outcome.rows_affected == 0
    assert "6 笔已入过账" in outcome.note
    assert "3 笔缺每股派息" in outcome.note
    # 缺数据不是失败：它们不进 errors，否则健康页会把源的窟窿记成 job 故障
    assert outcome.ok


class RecordingNav:
    def __init__(self, *, fail_on: str | None = None):
        self.fail_on = fail_on
        self.calls: list[tuple[str, date, date]] = []

    def rebuild(self, account_id, start, end):
        self.calls.append((account_id, start, end))
        if account_id == self.fail_on:
            raise StorageError("账户账本损坏")
        return 3


def test_nav_job_rebuilds_only_accounts_whose_home_market_matches(db, settings):
    repo, a, hk, bob_a = _accounts(db)
    service = RecordingNav()
    outcome = run_nav(_deps(db, settings, nav=service), _ctx("A"))

    # 三个市场各跑一趟，账户却只该重建一次：否则后一趟覆盖前一趟、行数还翻三倍
    assert {call[0] for call in service.calls} == {a.id, bob_a.id}
    assert all(call[1] == AS_OF - timedelta(days=NAV_WINDOW_DAYS) for call in service.calls)
    assert all(call[2] == AS_OF for call in service.calls)
    assert outcome.rows_affected == 6
    assert outcome.ok


def test_nav_job_leaves_broken_accounts_in_errors(db, settings):
    repo, a, hk, bob_a = _accounts(db)
    outcome = run_nav(_deps(db, settings, nav=RecordingNav(fail_on=bob_a.id)), _ctx("A"))

    assert outcome.rows_affected == 3
    assert len(outcome.errors) == 1 and "bob" in outcome.errors[0]
    assert "1 个账户失败" in outcome.note


def test_build_jobs_puts_dividends_and_nav_after_actions_and_before_rules(db, settings):
    deps = _deps(
        db, settings, dividends=RecordingDividends(), nav=RecordingNav(), chain=("stub",)
    )
    titles = [job.title for job in build_jobs(deps)]

    for market in sorted(settings.markets):
        assert titles.index(f"actions@{market}") < titles.index(f"dividends@{market}")
        assert titles.index(f"dividends@{market}") < titles.index(f"nav@{market}")
        assert titles.index(f"nav@{market}") < titles.index(f"rules@{market}")
        assert titles.index(f"rules@{market}") < titles.index(f"digest@{market}")


def test_build_jobs_registers_neither_job_without_its_dependency(db, settings):
    titles = [job.title for job in build_jobs(_deps(db, settings))]

    assert not any(title.startswith(("dividends@", "nav@")) for title in titles)
