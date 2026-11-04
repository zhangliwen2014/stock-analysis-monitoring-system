"""证伪台账的日常那一趟（D232 P0-3b）。

要钉的三件事：告警只在新证伪那一次发（每趟重报等于教人忽略它）、判的是本市场那一趟、
登记与否只看两个槽位给没给齐——没装配这一层的部署不该因此起不来。
"""

from __future__ import annotations

from types import SimpleNamespace

from support_valuation import CODE, TODAY, make_container

from sams.core.notify import AlertMessage
from sams.datasource.registry import AdapterRegistry
from sams.services.scheduler.due import DAILY
from sams.services.scheduler.jobs import build_jobs, run_assumptions
from sams.services.scheduler.types import JobContext, JobDeps
from sams.services.scheduler.universe import Universe

USER = "trader"
PWD = "correct-horse-battery"


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, AlertMessage]] = []

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
        is_test: bool = False,
    ) -> str:
        self.sent.append((user_id, message))
        return f"alert-{len(self.sent)}"


def deps(container, sender, settings, *, assumptions="default", alerts="default"):
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
        users=container.users,
        assumptions=container.assumptions if assumptions == "default" else assumptions,
        alerts=sender if alerts == "default" else alerts,
    )


def ctx(*, market="A"):
    return JobContext(
        period_key="2024-10-20",
        as_of=TODAY,
        market=market,
        universe=Universe(by_market={market: ()}, fx_pairs=()),
    )


def build(db, settings):
    container = make_container(db, settings, today=TODAY)
    container.users.create(USER, PWD)
    thesis = container.theses.add(USER, "A", CODE).thesis
    return container, thesis


def test_a_refutation_reaches_the_inbox_once_and_points_back_to_the_thesis(
    db, settings
) -> None:
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.25)
    sender = RecordingSender()
    outcome = run_assumptions(deps(container, sender, settings), ctx())
    assert outcome.rows_affected == 1
    assert "判了 1 条假设，新证伪 1" in outcome.note
    ((user_id, message),) = sender.sent
    assert user_id == container.users.get_by_username(USER).id
    assert (message.market, message.code) == ("A", CODE)
    assert message.rule_name == "证伪台账"
    assert message.stage == "证伪"
    assert message.value_text == "20.00%"
    assert message.threshold_text == "≥ 25.00%"
    assert f"/theses/{thesis.id}" in message.body


def test_the_next_sweep_does_not_repeat_the_same_alert(db, settings) -> None:
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.25)
    sender = RecordingSender()
    run_assumptions(deps(container, sender, settings), ctx())
    again = run_assumptions(deps(container, sender, settings), ctx())
    assert again.rows_affected == 0
    assert "判了 1 条假设，新证伪 0" in again.note
    assert len(sender.sent) == 1


def test_the_sweep_only_judges_the_market_it_runs_for(db, settings) -> None:
    container, thesis = build(db, settings)
    container.assumptions.add(USER, thesis.id, metric="roe", op=">=", threshold=0.25)
    sender = RecordingSender()
    outcome = run_assumptions(deps(container, sender, settings), ctx(market="HK"))
    assert outcome.rows_affected == 0
    assert "判了 0 条假设" in outcome.note
    assert sender.sent == []


def test_the_job_is_registered_only_when_both_slots_are_wired(db, settings) -> None:
    container, _ = build(db, settings)
    sender = RecordingSender()
    wired = build_jobs(deps(container, sender, settings))
    registered = [job for job in wired if job.name == "assumptions"]
    assert len(registered) == len(settings.markets)
    assert all(job.period == DAILY for job in registered)

    without_alerts = build_jobs(deps(container, sender, settings, alerts=None))
    assert not [job for job in without_alerts if job.name == "assumptions"]
