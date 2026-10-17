from __future__ import annotations

import pytest

from sams.core.events import (
    AlertTriggered,
    Event,
    EventBus,
    QuoteUpdated,
    RuleHit,
    SystemAlarm,
)


def collect() -> tuple[EventBus, list]:
    bus = EventBus()
    seen: list = []
    return bus, seen


def test_publish_delivers_to_subscribed_type():
    bus, seen = collect()
    bus.subscribe(QuoteUpdated, seen.append)
    bus.publish(QuoteUpdated(market="A", code="600519", price=1.0))
    assert len(seen) == 1


def test_type_dispatch_is_exact_not_positional():
    """两个 handler 订阅不同类型：只有类型匹配的那个收到，与注册顺序无关。"""
    bus, alarms = collect()
    _, rules = collect()
    bus.subscribe(SystemAlarm, alarms.append)
    bus.subscribe(RuleHit, rules.append)
    bus.publish(SystemAlarm(component="sync", message="boom"))
    assert len(alarms) == 1 and len(rules) == 0


def test_subscribing_to_base_event_receives_every_subclass():
    bus, seen = collect()
    bus.subscribe(Event, seen.append)
    bus.publish(QuoteUpdated(market="A", code="1", price=1.0))
    bus.publish(AlertTriggered(alert_id="a", rule_id="r", user_id="u"))
    assert [type(e).__name__ for e in seen] == ["QuoteUpdated", "AlertTriggered"]


def test_one_failing_handler_does_not_block_the_others():
    """spec §12 任务隔离：通知渠道炸了不能拖累其它订阅者。"""
    bus, seen = collect()

    def bad(event):
        raise RuntimeError("渠道挂了")

    bus.subscribe(SystemAlarm, bad)
    bus.subscribe(SystemAlarm, seen.append)
    result = bus.publish(SystemAlarm(component="x", message="y"))

    assert len(seen) == 1, "第二个 handler 必须照样收到"
    assert not result.ok
    assert result.errors[0][1].startswith("RuntimeError")


def test_publish_result_counts_only_matched_handlers():
    bus, _ = collect()
    bus.subscribe(QuoteUpdated, lambda e: None)
    result = bus.publish(SystemAlarm(component="x", message="y"))
    assert result.handled == 0 and result.ok


def test_subscribe_rejects_non_event():
    bus, _ = collect()
    with pytest.raises(TypeError):
        bus.subscribe(dict, lambda e: None)  # type: ignore[arg-type]
