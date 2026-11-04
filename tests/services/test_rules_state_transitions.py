"""跃迁去重、静音与证据（S1-B）。

这一层的核心口径：同一条去重键上只说状态**变化**。`bar_ts` 是证据，
不是键——把它放进键里，每天一根新 bar 就重发一次。
"""

from __future__ import annotations

import pytest
from support_rules import CODE, MARKET, TODAY, USER, Harness, snap

from sams.core.errors import ValidationError


@pytest.fixture
def h(db, settings) -> Harness:
    return Harness(db, settings)


def _state(h):
    return h.states.get(USER, h.added_id, MARKET, CODE)


def test_a_second_run_of_the_same_stage_does_not_raise_again(h) -> None:
    h.add(cooldown_min=0)
    engine = h.engine()
    first = engine.check(USER)
    assert len(first.raised) == 1
    second = engine.check(USER)
    assert second.raised == ()
    assert len(second.suppressed) == 1
    assert "状态未变" in second.suppressed[0].reason
    assert len(h.sender.sent) == 1


def test_the_first_alert_carries_the_bar_and_the_dedupe_key(h) -> None:
    rule = h.add(cooldown_min=0)
    report = h.storing().check(USER)
    alert = h.alerts.get(report.raised[0].alert_id)
    assert alert is not None
    assert alert.trigger_bar_ts == TODAY
    assert alert.dedupe_key == f"{rule.id}|{MARKET}|{CODE}|-"


def test_a_recovery_clears_the_state_so_a_new_breach_alerts(h) -> None:
    h.add(cooldown_min=0)
    assert len(h.engine().check(USER).raised) == 1
    h.snapshot = snap(price=11.0)
    assert h.engine().check(USER).quiet == 1
    assert _state(h).state == "clear"
    h.snapshot = snap(price=9.5)
    assert len(h.engine().check(USER).raised) == 1


def test_a_filtered_hit_leaves_the_state_untouched(h) -> None:
    """时段外的那次读数确实命中了；写下去的话，窗口开了就再也收不到。"""
    h.add(filters={"data_freshness": True}, cooldown_min=0)
    h.snapshot = snap(price_age_days=10)
    report = h.engine().check(USER)
    assert len(report.suppressed) == 1
    assert _state(h) is None
    h.snapshot = snap()
    assert len(h.engine().check(USER).raised) == 1


def test_mute_suppresses_and_records_the_state_without_sending(h) -> None:
    h.add(cooldown_min=0)
    h.mutes.add(USER, MARKET, CODE, note="等财报")
    report = h.engine().check(USER)
    assert report.raised == ()
    assert "静音" in report.suppressed[0].reason
    assert h.sender.sent == []
    assert _state(h).state == "hit"

    # 解除后同一档不补发；下一次真正的跃迁照发
    h.mutes.remove(USER, MARKET, CODE)
    again = h.engine().check(USER)
    assert again.raised == () and "状态未变" in again.suppressed[0].reason
    h.snapshot = snap(price=9.4, prev_close=10.2, day_open=9.5)
    assert len(h.engine().check(USER).raised) == 1


def test_a_test_drill_bypasses_the_state_and_the_mute(h) -> None:
    h.add(cooldown_min=0)
    h.storing().check(USER)
    assert _state(h).state == "hit"
    h.mutes.add(USER, MARKET, CODE)
    report = h.storing().check(USER, is_test=True)
    assert len(report.raised) == 1
    assert _state(h).state == "hit"


def test_a_dry_run_never_writes_the_state(h) -> None:
    h.add(cooldown_min=0)
    report = h.engine().check(USER, dry_run=True)
    assert len(report.raised) == 1
    assert _state(h) is None

    h.storing().check(USER)
    assert _state(h).state == "hit"
    h.snapshot = snap(price=11.0)
    h.engine().check(USER, dry_run=True)
    # dry-run 也不能把真实状态清零
    assert _state(h).state == "hit"


def test_a_failed_dispatch_leaves_the_state_for_the_next_retry(h) -> None:
    h.add(cooldown_min=0)
    failed = h.storing(raises=ValidationError("库写不进"))
    report = failed.check(USER)
    assert len(report.skipped) == 1
    assert _state(h) is None
    assert len(h.storing().check(USER).raised) == 1
    assert _state(h).state == "hit"


def test_a_news_mute_does_not_silence_price_rules(h) -> None:
    """静音分组（D249）：关掉资讯，不该连止损线一起关。"""
    h.add(cooldown_min=0)
    h.mutes.add(USER, MARKET, CODE, kind="news")

    assert len(h.engine().check(USER).raised) == 1


def test_a_digest_mute_does_not_silence_price_rules(h) -> None:
    h.add(cooldown_min=0)
    h.mutes.add(USER, MARKET, CODE, kind="digest")

    assert len(h.engine().check(USER).raised) == 1


def test_a_rule_mute_silences_price_rules(h) -> None:
    h.add(cooldown_min=0)
    h.mutes.add(USER, MARKET, CODE, kind="rule")

    report = h.engine().check(USER)

    assert report.raised == ()
    assert "静音" in report.suppressed[0].reason
    assert h.sender.sent == []
