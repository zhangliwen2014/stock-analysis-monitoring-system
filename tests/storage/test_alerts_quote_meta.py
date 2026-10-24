"""D120/D131 的两件事：报价三件套随行落库，测试告警不进 no_open_alert 与健康积压。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sams.datasource.registry import AdapterRegistry
from sams.services.health import HealthService
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.calendar import CalendarRepo

MOMENT = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def row(repo, **over):
    base = dict(
        rule_id="r1",
        rule_name="跌破 10 元",
        account_id=None,
        account_name="",
        market="A",
        code="601088",
        symbol_name="中国神华",
        severity="mid",
        stage="hit",
        value_text="9.40 CNY",
        threshold_text="10.00 CNY",
        action_note="按计划处置",
        triggered_at=MOMENT,
    )
    base.update(over)
    return repo.create("u1", **base)


def test_quote_metadata_round_trips(db):
    repo = AlertRepo(db)
    created = row(repo, intraday=True, quote_ts=MOMENT, quote_source="tencent")
    got = repo.get(created.id)
    assert got.intraday is True
    assert got.quote_ts == MOMENT
    assert got.quote_source == "tencent"
    assert got.is_test is False


def test_is_test_round_trips_and_defaults_to_false(db):
    repo = AlertRepo(db)
    assert row(repo).is_test is False
    assert repo.get(row(repo, is_test=True).id).is_test is True


def test_latest_open_ignores_test_rows(db):
    # no_open_alert 的输入（§10.1）：/settings/test-alert 发的那条不该把真告警挡一整天。
    # 测试行故意更「新」：latest_open 按 triggered_at 挑最新，新的测试行不许压住真的
    repo = AlertRepo(db)
    real = row(repo)
    row(repo, is_test=True, stage="pre", triggered_at=MOMENT + timedelta(minutes=5))
    got = repo.latest_open("u1", "A", "601088")
    assert got is not None and got.id == real.id


def test_latest_open_on_a_test_only_stream_is_none(db):
    repo = AlertRepo(db)
    row(repo, is_test=True)
    assert repo.latest_open("u1", "A", "601088") is None


def test_health_backlog_ignores_test_rows(db, settings):
    service = HealthService(
        db,
        AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _s: None),
        CalendarRepo(db),
        settings,
    )
    repo = AlertRepo(db)
    row(repo)
    row(repo, code="600519", is_test=True)
    assert service.report().notify.unhandled == 1
