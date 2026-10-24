"""盘中那一趟（§5.3）：闸在缓存之前，日线读数的日期跟着报价走，正文多一行出处。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from support_rules import CODE, MARKET, NOW, USER, FixedClock, Harness, StubQuotes

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
    # 判据用的是**盖过之后**的价：日线快照上是 9.5，报价上是 9.4。留着 9.5 的话
    # 屏幕上那句「现价」说的是半小时前的收盘价，而止损判的也是那个旧数
    assert message.value_text == "9.40 CNY"
    assert "报价 15:29:55 · 腾讯 · 延迟未标注" in message.body


def test_the_daily_read_lands_on_the_quote_day(db, settings):
    """日线读数的日期跟着报价走，不跟着墙钟走（§5.3）。

    报价 ts 是北京 2026-10-20 凌晨 00:30，引擎时钟晚 5 秒还在 UTC 的 10-19：
    拿 `clock.today()` 就会读到 10-19 那根日线。美股收盘在北京是次日凌晨，
    症状正是「拿昨天的收盘价判今天的破位」，而屏幕上那句「现价」还是新的。
    """
    h = Harness(db, settings)
    h.add()
    ts = datetime(2026, 10, 19, 16, 30, tzinfo=UTC)
    h.clock = FixedClock(ts + timedelta(seconds=5))
    engine = h.engine()
    engine.check(USER, intraday=True, quotes=board(quote(ts=ts)))
    assert engine.data.as_of_seen == [date(2026, 10, 20)]


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


def test_quotes_without_the_intraday_flag_is_refused(db, settings):
    """两个参数是一对：只给板不给 `intraday` 会让收盘那一趟被新鲜度闸悄悄改写。

    症状是「收盘的告警写着报价出处、正文多一行延迟」，而 `intraday=True` 那半条
    路由（挑 intraday 那批规则）没走——同一份板在两条路径上是两种判法。装配错误
    在这里拒绝，而不是折成一个业务结论（同上面那枚）。
    """
    h = Harness(db, settings)
    h.add()
    with pytest.raises(ValidationError):
        h.engine().check(USER, quotes=board(quote()))


def test_a_wild_kind_in_the_db_does_not_kill_the_intraday_pass(db, settings):
    """库里躺着这一层没实现的 kind：盘中那一趟也照旧「跳这一条、别的照跑」。

    `is_intraday` 对名单外的 kind 是抛 KeyError（名单的唯一出处是 `TEMPLATES`），
    拿它当筛子就等于让一条野行把整趟拖崩——而收盘那一趟早就认了另一种处置：
    野行进到 `_one`，落成一句 skipped（`test_rules_engine.py` 钉的是同一件事）。
    """
    h = Harness(db, settings)
    good = h.add()
    h.db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref,"
        " kind, condition_kind, params_json, filters_json, cooldown_min, severity,"
        " channel_ids, created_at, updated_at)"
        " VALUES ('wild-row', ?, '没实现的那种', TRUE, 'symbol', ?, 'stop.lunar',"
        " 'template', '{}', '{}', 30, 'mid', '[]', ?, ?)",
        [USER, f"{MARKET}/{CODE}", NOW, NOW],
    )
    got = h.engine().check(USER, intraday=True, quotes=board(quote()))
    assert got.rules == 2  # 野行照旧进这一趟，由 `_one` 说它判不了
    assert [hit.rule_id for hit in got.raised] == [good.id]
    assert len(got.skipped) == 1
    assert got.skipped[0].rule_id == "wild-row"
    assert "求值实现" in got.skipped[0].reason
