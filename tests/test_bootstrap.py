from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from support import FrozenClock, StubAdapter, sample_bars

from sams.bootstrap import attach_log_sink, build_container
from sams.core.events import EventBus, SystemAlarm


def stub_adapters() -> dict:
    return {
        a.name: a
        for a in (
            StubAdapter("akshare", ["A"], sample_bars()),
            StubAdapter("eastmoney", ["A"], sample_bars(source="eastmoney")),
            StubAdapter("yfinance", ["US", "HK"], sample_bars("US", "AAPL")),
        )
    }


@pytest.fixture
def container(db, settings):
    clock = FrozenClock(datetime(2026, 3, 9, 4, 0, tzinfo=UTC))
    built = build_container(settings, db=db, clock=clock, adapters=stub_adapters())
    yield built, clock
    built.close()


def test_one_clock_is_shared_across_the_container(container):
    """组装根只造一个时钟：各处各读墙钟，重放时它们会互相不一致。"""
    built, clock = container
    assert built.clock is clock
    assert built.sessions.clock is clock
    assert built.registry.clock is clock
    assert built.health.clock is clock


def test_alarm_reaches_the_log(container, caplog):
    """总线不能是装饰：publish 之后至少要在日志里看得见（spec §12）。"""
    built, _ = container
    with caplog.at_level(logging.ERROR, logger="sams.bootstrap"):
        built.bus.publish(SystemAlarm(component="calendar", message="日历源全挂"))
    assert "日历源全挂" in caplog.text


def test_log_sink_does_not_swallow_other_subscribers():
    bus = EventBus()
    attach_log_sink(bus)
    seen = []
    bus.subscribe(SystemAlarm, seen.append)
    result = bus.publish(SystemAlarm(component="x", message="y"))
    assert result.ok and len(seen) == 1
