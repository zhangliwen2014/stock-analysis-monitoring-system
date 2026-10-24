"""SnapshotCache：版本闸整表失效、日子变了当没有、TTL 兜底；引擎两轮只读一次日线。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from support_rules import NOW, USER, Harness, StubQuotes

from sams.core.quotes import Quote
from sams.core.version import VersionGate
from sams.services.rules.cache import CacheKey, SnapshotCache

#: 与引擎时钟同一分钟：staleness 闸不能抢在缓存之前把这一趟拒掉
TS = NOW - timedelta(seconds=5)
KEY = CacheKey(user_id="u1", rule_id="r1", market="A", code="601088")
DAY = TS.date()


def cache(monotonic=lambda: 0.0) -> SnapshotCache:
    return SnapshotCache(VersionGate(), monotonic=monotonic)


def test_roundtrip_within_the_same_day():
    got_cache = cache()
    got_cache.put(KEY, "snap", day=DAY)
    assert got_cache.get(KEY, day=DAY) == "snap"


def test_a_different_day_is_a_miss():
    got_cache = cache()
    got_cache.put(KEY, "snap", day=DAY)
    assert got_cache.get(KEY, day=DAY + timedelta(days=1)) is None


@pytest.mark.parametrize("kind", ["rules", "theses", "ledger", "bars"])
def test_any_version_bump_clears_everything(kind):
    # 分 kind 失效省下的那点读，不值「哪一版忘了 bump」的风险——四个键一个待遇
    gate = VersionGate()
    got_cache = SnapshotCache(gate, monotonic=lambda: 0.0)
    got_cache.put(KEY, "snap", day=DAY)
    gate.bump(kind)
    assert got_cache.get(KEY, day=DAY) is None


def test_ttl_expires():
    # put 记下 0.0；第一取 0.0（岁数 0，命中）；第二取 301.0（岁数 > 300，过期）
    ticks = iter([0.0, 0.0, 301.0])
    got_cache = SnapshotCache(VersionGate(), monotonic=lambda: next(ticks))
    got_cache.put(KEY, "snap", day=DAY)
    assert got_cache.get(KEY, day=DAY) == "snap"
    assert got_cache.get(KEY, day=DAY) is None


def test_two_intraday_rounds_share_one_daily_read(db, settings):
    h = Harness(db, settings)
    h.add()
    tick = {"t": 0.0}
    engine = h.engine(cache=SnapshotCache(VersionGate(), monotonic=lambda: tick["t"]))

    def q(price):
        return Quote(
            market="A", code="601088", ts=TS, price=price, source="tencent"
        )

    board = StubQuotes({("A", "601088"): q(9.4)})
    first = engine.check(USER, intraday=True, quotes=board)
    assert len(first.raised) == 1
    board.current[("A", "601088")] = q(9.1)
    second = engine.check(USER, intraday=True, quotes=board)
    assert len(second.raised) == 1
    assert "9.10" in second.raised[0].value_text  # 判的是新价，不是缓存里的旧价
    assert len(engine.data.as_of_seen) == 1  # 第二轮没再读日线


def test_a_bump_makes_the_next_round_read_again(db, settings):
    h = Harness(db, settings)
    h.add()
    gate = VersionGate()
    engine = h.engine(cache=SnapshotCache(gate, monotonic=lambda: 0.0))

    def q(price):
        return Quote(market="A", code="601088", ts=TS, price=price, source="tencent")

    board = StubQuotes({("A", "601088"): q(9.4)})
    engine.check(USER, intraday=True, quotes=board)
    gate.bump("rules")
    engine.check(USER, intraday=True, quotes=board)
    assert len(engine.data.as_of_seen) == 2
