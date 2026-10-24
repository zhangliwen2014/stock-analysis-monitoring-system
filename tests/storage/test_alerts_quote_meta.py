"""D120/D131 的两件事：报价三件套随行落库，测试告警不进 no_open_alert 与健康积压。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sams.core.notify import SYSTEM_MARKET
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
    # 测试行比真告警老三天、code 也不同：只漏掉 oldest 那条过滤的话，
    # `unhandled_oldest` 就会变成这行更老的测试行，而不是 MOMENT
    row(repo, code="600519", is_test=True, triggered_at=MOMENT - timedelta(days=3))
    # /settings/test-alert 记的是 SYS 行（D131）：系统侧那两条查询也得挡住它，
    # 否则点一次测试就常年显示「系统 1 个组件未处理」
    row(repo, market=SYSTEM_MARKET, code="test", is_test=True)
    notify = service.report().notify
    assert notify.unhandled == 1
    assert notify.unhandled_oldest == MOMENT
    assert notify.unhandled_system == 0


def test_health_report_works_without_is_test_column(db, settings):
    repo = AlertRepo(db)
    row(repo)
    # DuckDB 不许在表上有索引时删列，先把 alerts 的索引摘掉；本用例只关心「列不在」
    for name in db.query(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'alerts'"
    )["index_name"].to_list():
        db.execute(f'DROP INDEX IF EXISTS "{name}"')
    # 0018 的库没有这一列：健康页是「先看看这个库怎么了」的入口，不许在这里抛
    db.execute("ALTER TABLE alerts DROP COLUMN is_test")
    notify = HealthService(
        db,
        AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _s: None),
        CalendarRepo(db),
        settings,
    ).report().notify
    assert notify.unhandled == 1
    assert notify.unhandled_system == 0


def test_malformed_quote_ts_does_not_break_read(db):
    repo = AlertRepo(db)
    created = row(repo, quote_ts=MOMENT)
    payload = dict(repo.get(created.id).payload)
    payload["quote_ts"] = "not-a-time"
    db.execute(
        "UPDATE alerts SET payload_json = ? WHERE id = ?",
        [json.dumps(payload, ensure_ascii=False), created.id],
    )
    # 存坏的那一行不许让整张收件箱读不出来：解不开就当没存
    assert repo.get(created.id).quote_ts is None
