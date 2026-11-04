"""收盘摘要的调度那一趟（S1-C）：排在规则之后，只跑启用的账号，一人失败不带走别人。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

from sams.core.errors import StorageError
from sams.datasource.registry import AdapterRegistry
from sams.services.digest import DigestOutcome
from sams.services.scheduler.due import DAILY
from sams.services.scheduler.jobs import build_jobs, run_digest
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe
from sams.storage.repos.users import UserRepo

AS_OF = date(2026, 10, 30)


@dataclass
class RecordingDigest:
    calls: list[tuple[str, str, date]] = field(default_factory=list)
    fail_for: tuple[str, ...] = ()

    def run(self, user, market, *, as_of):
        if user.username in self.fail_for:
            raise StorageError("摘要写不进去")
        self.calls.append((user.username, market, as_of))
        return DigestOutcome(None, created=True, pushed=True)


def deps(db, settings, *, digest=None):
    return JobDeps(
        settings=settings,
        registry=AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _: None),
        sync=SimpleNamespace(indices=None),  # type: ignore[arg-type]
        corp_actions=None,  # type: ignore[arg-type]
        fundamentals=None,  # type: ignore[arg-type]
        f10=None,  # type: ignore[arg-type]
        fx=None,  # type: ignore[arg-type]
        calendar=None,  # type: ignore[arg-type]
        rules=None,  # type: ignore[arg-type]
        recheck=None,  # type: ignore[arg-type]
        users=UserRepo(db),
        digest=digest,
    )


def ctx(*, market="A"):
    return JobContext(
        period_key=AS_OF.isoformat(),
        as_of=AS_OF,
        market=market,
        universe=Universe(by_market={market: ()}, fx_pairs=()),
    )


def test_the_digest_job_is_registered_after_rules_and_only_when_wired(db, settings):
    wired = build_jobs(deps(db, settings, digest=RecordingDigest()))
    titles = [job.title for job in wired]
    for market in settings.markets:
        assert titles.index(f"rules@{market}") < titles.index(f"digest@{market}")
        assert titles.index(f"digest@{market}") < titles.index("fx@全局")
    assert all(
        job.period == DAILY for job in wired if job.name == "digest"
    )

    assert not [job for job in build_jobs(deps(db, settings)) if job.name == "digest"]


def test_the_sweep_covers_enabled_users_only_and_counts_what_was_written(db, settings):
    users = UserRepo(db)
    users.create("trader", "correct-horse-battery")
    sleeping = users.create("sleeping", "correct-horse-battery")
    users.disable(sleeping.id)
    digest = RecordingDigest()

    outcome = run_digest(deps(db, settings, digest=digest), ctx())

    assert outcome.rows_affected == 1
    assert digest.calls == [("trader", "A", AS_OF)]
    assert "1/1 个用户" in outcome.note
    assert "写入 1 条" in outcome.note
    assert "推送 1 条" in outcome.note


def test_one_user_failure_does_not_take_the_rest_of_the_sweep(db, settings):
    users = UserRepo(db)
    users.create("trader", "correct-horse-battery")
    users.create("sleeper", "correct-horse-battery")
    outcome = run_digest(
        deps(db, settings, digest=RecordingDigest(fail_for=("trader",))), ctx()
    )

    assert outcome.rows_affected == 1
    assert len(outcome.errors) == 1 and "trader" in outcome.errors[0]
    assert "1 个用户失败" in outcome.note


def test_the_job_requires_a_market(settings, db):
    digest = RecordingDigest()
    job = next(job for job in build_jobs(deps(db, settings, digest=digest)) if job.name == "digest")
    assert job.market == "A"
