"""IntradayEvaluator：哪几个市场活了、每个用户×市场各判一趟、坏了不拖垮别人。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from support_rules import NOW, USER, Harness

from sams.core.errors import StorageError
from sams.core.quotes import Quote
from sams.services.intraday import MarketPoll
from sams.services.quotes import QuoteBoard, QuotePollResult
from sams.services.rules.intraday import IntradayEvaluator

#: 与引擎时钟同一分钟：staleness 闸不能抢在缓存之前把这一趟拒掉
TS = NOW - timedelta(seconds=5)


def quote(code="601088", **over):
    base = {"market": "A", "code": code, "ts": TS, "price": 9.4, "source": "tencent"}
    base.update(over)
    return Quote(**base)


def live_board(q) -> QuoteBoard:
    board = QuoteBoard()
    board.update(q.market, (q,), now=NOW)
    return board


def outcome(q):
    return (
        MarketPoll(
            q.market,
            result=QuotePollResult(quotes=(q,), skipped=(), errors=(), sources=()),
        ),
    )


def test_a_round_with_quotes_evaluates_and_counts_hits(db, settings):
    h = Harness(db, settings)
    h.add()
    q = quote()
    evaluator = IntradayEvaluator(h.rules, h.engine(), live_board(q))
    got = evaluator.after_poll(outcome(q))
    assert got.markets == ("A",)
    assert got.rules == 1
    assert got.hits == 1


def test_markets_without_a_result_are_not_evaluated(db, settings):
    h = Harness(db, settings)
    h.add()
    evaluator = IntradayEvaluator(h.rules, h.engine(), live_board(quote()))
    got = evaluator.after_poll((MarketPoll("A", skipped="非交易时段"),))
    assert got.markets == () and got.rules == 0 and got.hits == 0
    assert h.sender.sent == []


def test_an_empty_result_is_not_a_live_market(db, settings):
    h = Harness(db, settings)
    h.add()
    evaluator = IntradayEvaluator(h.rules, h.engine(), live_board(quote()))
    got = evaluator.after_poll((MarketPoll("A", result=QuotePollResult()),))
    assert got.markets == () and got.hits == 0


def test_each_user_gets_its_own_pass_per_market(db, settings):
    h = Harness(db, settings)
    h.add()
    h.rules.create(
        "u-two",
        kind="stop.fixed",
        name="另一条",
        target_type="symbol",
        target_ref="A/601088",
        params={"price": 10.0},
    )
    calls: list[tuple[str, str]] = []

    class CountingEngine:
        def check(self, user_id, **kw):
            calls.append((user_id, kw["market"]))
            return SimpleNamespace(raised=())

    q = quote()
    got = IntradayEvaluator(h.rules, CountingEngine(), live_board(q)).after_poll(outcome(q))
    assert set(calls) == {(USER, "A"), ("u-two", "A")}
    assert got.rules == 2 and got.hits == 0


def test_a_sams_error_is_isolated_not_fatal(db, settings):
    class ExplodingRules:
        def list_enabled(self):
            raise StorageError("库写不进")

    q = quote()
    evaluator = IntradayEvaluator(
        ExplodingRules(), Harness(db, settings).engine(), live_board(q)
    )
    got = evaluator.after_poll(outcome(q))  # 不抛
    assert got.markets == ("A",) and got.rules == 0 and got.hits == 0


def test_a_wild_kind_does_not_kill_the_round(db, settings):
    """库里躺着这一层没实现的 kind：整轮评估照旧出账，别的用户与市场不受牵连。

    `is_intraday` 对名单外的 kind 是抛 KeyError（名单的唯一出处是 `TEMPLATES`），而
    KeyError 不是 `SamsError`，逃得过那道 `except`——拿它当筛子，就等于让库里一条野行
    把盘中线程整个拖崩。引擎在 Task 8 已经认了另一种处置：野行留在名单里，每个市场各
    落一句 skipped（`test_rules_intraday_engine.py` 钉的是同一件事），这里跟着同一个取向，
    于是野行也进这一趟的账。
    """
    h = Harness(db, settings)
    good = h.add()
    h.db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref,"
        " kind, condition_kind, params_json, filters_json, cooldown_min, severity,"
        " channel_ids, created_at, updated_at)"
        " VALUES ('wild-row', ?, '没实现的那种', TRUE, 'symbol', ?, 'stop.lunar',"
        " 'template', '{}', '{}', 30, 'mid', '[]', ?, ?)",
        [USER, "A/601088", NOW, NOW],
    )
    q = quote()
    got = IntradayEvaluator(h.rules, h.engine(), live_board(q)).after_poll(outcome(q))
    assert got.markets == ("A",)
    assert got.rules == 2  # 野行留在名单里：看过两条，判得了的只有一条
    assert got.hits == 1  # 好的那条照旧触发
    assert [m.rule_name for m in h.sender.sent] == [good.name]  # 野行没冒出告警
