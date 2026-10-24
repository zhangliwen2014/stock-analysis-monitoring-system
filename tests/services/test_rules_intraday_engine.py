"""盘中那一趟（§5.3）：闸在缓存之前，日线读数的日期跟着报价走，正文多一行出处。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from support_rules import NOW, USER, Harness, StubQuotes

from sams.core.errors import ValidationError
from sams.core.quotes import Quote

#: 比引擎时钟新 5 秒（上海 15:29:55）：staleness 闸按「引擎时钟 − 报价 ts」算岁数，
#: 报价时刻写死成将来时的话，age 是负数，按岁数拒掉的那一枚永远测不到
TS = NOW - timedelta(seconds=5)


def quote(**over):
    base = {
        "market": "A",
        "code": "601088",
        "ts": TS,
        "price": 9.4,
        "open": 9.8,
        "prev_close": 10.2,
        "source": "tencent",
    }
    base.update(over)
    return Quote(**base)


def board(q, prev=None):
    previous = {(q.market, q.code): prev} if prev is not None else {}
    return StubQuotes({(q.market, q.code): q}, previous)


def test_without_a_quote_the_target_is_skipped(db, settings):
    h = Harness(db, settings)
    h.add()
    engine = h.engine()
    got = engine.check(USER, intraday=True, quotes=StubQuotes())
    assert got.rules == 1 and len(got.skipped) == 1
    assert "盘中还没有这只票的报价" in got.skipped[0].reason
    assert engine.data.as_of_seen == []  # 闸在取数之前：连日线都没读


def test_a_stale_quote_is_refused_at_the_gate(db, settings):
    h = Harness(db, settings)
    h.add()
    engine = h.engine()
    got = engine.check(USER, intraday=True, quotes=board(quote(is_stale=True)))
    assert "stale" in got.skipped[0].reason
    got = engine.check(
        USER, intraday=True, quotes=board(quote(ts=TS - timedelta(seconds=120)))
    )
    assert "超过上限 60 秒" in got.skipped[0].reason
    assert engine.data.as_of_seen == []


def test_a_fresh_quote_overlays_and_flows_to_a_hit(db, settings):
    h = Harness(db, settings)
    h.add()
    got = h.engine().check(USER, intraday=True, quotes=board(quote()))
    assert len(got.raised) == 1
    message = h.sender.sent[0]
    assert message.intraday is True
    assert message.quote_ts == TS
    assert message.quote_source == "tencent"
    assert "报价 15:29:55 · 腾讯 · 延迟未标注" in message.body


def test_the_daily_read_lands_on_the_quote_day(db, settings):
    h = Harness(db, settings)
    h.add()
    engine = h.engine()
    engine.check(USER, intraday=True, quotes=board(quote()))
    assert engine.data.as_of_seen == [TS.date()]


def test_only_intraday_kinds_run_in_the_intraday_pass(db, settings):
    h = Harness(db, settings)
    h.add()
    # 计划原文只写 `kind="stop.time"` 就 `h.add`：创建那头先拒（stop.time 只认
    # position/thesis，且 position 的 ref 必须是 `*`、必须挂账户）。这里补成合法形状，
    # 要钉的是名单滤不滤得掉它，不是目标形状
    h.add(
        kind="stop.time",
        name="到期复核",
        params={"days": 30},
        target_type="position",
        target_ref="*",
        account_id="acc-1",
    )
    got = h.engine().check(USER, intraday=True, quotes=board(quote()))
    assert got.rules == 1  # stop.time 被名单滤掉，压根不展开


def test_the_daily_path_still_runs_without_quotes(db, settings):
    h = Harness(db, settings)
    h.add()
    got = h.engine().check(USER)
    assert len(got.raised) == 1
    assert h.sender.sent[0].intraday is False


def test_intraday_without_a_board_is_refused(db, settings):
    h = Harness(db, settings)
    h.add()
    with pytest.raises(ValidationError):
        h.engine().check(USER, intraday=True)
