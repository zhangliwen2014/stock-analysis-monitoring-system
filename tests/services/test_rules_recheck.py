"""§9.3：opened/tracking 的论每周重算一次，第一次只建基线，之后才比。

用例对着 `support_valuation` 那个世界跑（300 天日线 + 两期年报 + 逐日估值，时钟冻在
`DAYS[-1]`），因为「这一趟到底有没有分」是那条链的回答，不是这个文件的回答。基线用
`set_baseline` 手写到已知档位上，掉档的那几项因此可以逐字断言。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import support_valuation

from sams.core.errors import StorageError, ValidationError
from sams.core.events import EventBus, ThesisDrifted
from sams.services.rules.recheck import (
    BASELINE,
    CLEAN,
    DRIFT,
    NO_SCORE,
    PARTIAL,
    Rechecker,
)
from sams.services.rules.snapshot import Target
from sams.storage.repos.theses import OPENED, PROPOSED, TRACKING

CODE = support_valuation.CODE
AS_OF = support_valuation.DAYS[-1]
USER = "u-recheck"
T0 = datetime(2024, 10, 26, 15, 0, tzinfo=UTC)
LEVELS = {
    "entry_low": 30.0,
    "entry_high": 36.0,
    "stop_price": 28.0,
    "target_price": 52.0,
    "reward_risk": 2.0,
}


class Recorder:
    """订阅 `ThesisDrifted`：这一枚文件的验收线有一半在「事件发没发」上。"""

    def __init__(self) -> None:
        self.events: list[ThesisDrifted] = []

    def __call__(self, event: ThesisDrifted) -> None:
        self.events.append(event)


@pytest.fixture
def container(db, settings):
    return support_valuation.make_container(db, settings)


def build(container, bus=None) -> Rechecker:
    # 取数装配用容器那头拼好的 `rule_data`（`bootstrap.py` 是接线的唯一出处）
    return Rechecker(
        theses=container.theses_store,
        data=container.rule_data,
        bus=bus or EventBus(),
        clock=container.clock,
    )


def open_thesis(container, *, status: str = OPENED, code: str = CODE):
    theses = container.theses_store
    thesis = theses.create(
        user_id=USER,
        market="A",
        code=code,
        style="value",
        actor_id=USER,
        now=T0,
        status=PROPOSED,
        **LEVELS,
    )
    theses.advance(USER, thesis.id, OPENED, actor_id=USER, now=T0)
    if status == TRACKING:
        theses.advance(USER, thesis.id, TRACKING, actor_id=USER, now=T0)
    return theses.get(USER, thesis.id)


def lay_baseline(container, thesis, items, score=95.0) -> None:
    """把基线写成已知的那几档（`on` 故意写成比复检日更旧的一个日子；`RecheckRow` 不
    带这个日期，新旧只看库）。"""
    container.theses_store.set_baseline(
        USER, thesis.id, score=score, items=items, on=date(2024, 7, 1), now=T0
    )


# --- 第一次跑：建基线，不比 ----------------------------------------------------------------


def test_the_first_run_lays_a_baseline_instead_of_judging(container) -> None:
    thesis = open_thesis(container)
    report = build(container).run(USER)

    assert [row.state for row in report.rows] == [BASELINE]
    row = report.rows[0]
    assert row.written is True  # 真跑的这一趟确实写了库
    assert row.score is not None  # 那个世界的合成分确实出得来
    assert row.baseline_score == row.score
    assert "下一次" in row.message
    # 基线落库了，而且属于复检那一天
    stored = container.theses_store.get(USER, thesis.id)
    assert stored.baseline_score == row.score
    assert stored.baseline_on == AS_OF


def test_a_second_run_compares_instead_of_reevaluating_the_baseline(container) -> None:
    """基线已经在了：第二趟必须走比较那一路，不许把基线刷成「今天」。"""
    thesis = open_thesis(container)
    rechecker = build(container)
    rechecker.run(USER)
    first = container.theses_store.get(USER, thesis.id)

    again = rechecker.run(USER)
    stored = container.theses_store.get(USER, thesis.id)

    assert [row.state for row in again.rows] == [CLEAN]
    assert stored.baseline_on == first.baseline_on


def test_a_thesis_without_a_score_gets_no_baseline_and_says_so(db, settings) -> None:
    """拔掉基本面 → 覆盖不到地板 → 拒分（D70）。这时既不能建基线，也不能说「没漂移」。"""
    thin = support_valuation.make_container(db, settings, with_fundamentals=False)
    thesis = open_thesis(thin)
    rec = Recorder()
    bus = EventBus()
    bus.subscribe(ThesisDrifted, rec)

    report = build(thin, bus).run(USER)

    assert [row.state for row in report.rows] == [NO_SCORE]
    assert report.rows[0].score is None
    assert "覆盖" in report.rows[0].message
    assert thin.theses_store.get(USER, thesis.id).baseline_score is None
    assert rec.events == []


def test_a_thesis_that_cannot_be_read_does_not_stop_the_run(container) -> None:
    """一只没有日线的票（退市或从没同步）：取数那头拒绝的是**这一条**，整趟不该被它打掉，
    而屏幕上那条要说「读不到」——一个空白行会被读成复检过了。"""
    read_ok = open_thesis(container)
    blind = container.theses_store.create(
        user_id=USER,
        market="A",
        code="600000",
        style="value",
        actor_id=USER,
        now=T0,
        status=PROPOSED,
        **LEVELS,
    )
    container.theses_store.advance(USER, blind.id, OPENED, actor_id=USER, now=T0)

    report = build(container).run(USER)

    assert {row.code: row.state for row in report.rows} == {CODE: BASELINE, "600000": NO_SCORE}
    row = next(r for r in report.rows if r.code == "600000")
    assert "读不到" in row.message
    assert "「读不到」不是「没漂移」" in row.message
    assert container.theses_store.get(USER, blind.id).baseline_score is None
    assert container.theses_store.get(USER, read_ok.id).baseline_score is not None


def test_dry_run_writes_nothing_but_still_says_what_it_would_do(container) -> None:
    open_thesis(container)
    rec = Recorder()
    bus = EventBus()
    bus.subscribe(ThesisDrifted, rec)

    report = build(container, bus).run(USER, dry_run=True)

    assert report.dry_run is True
    assert report.rows[0].state == BASELINE
    assert report.rows[0].written is False
    assert "试运行" in report.rows[0].message
    assert report.count(BASELINE) == 1
    # 屏幕上说的是「会建」，库里那三列一个字没动
    assert container.theses_store.list(USER)[0].baseline_score is None
    assert rec.events == []


# --- 之后每次：比基线 ---------------------------------------------------------------------


def test_a_drop_of_one_tier_publishes_the_event_and_names_the_item(container) -> None:
    thesis = open_thesis(container)
    # 这一趟那个世界读到 pr 58.455、roe 85.0：80 与 85 同在 4 档，所以只有 PR 掉档
    lay_baseline(container, thesis, {"pr": 80.0, "roe": 80.0})
    rec = Recorder()
    bus = EventBus()
    bus.subscribe(ThesisDrifted, rec)

    report = build(container, bus).run(USER)

    assert [row.state for row in report.rows] == [DRIFT]
    assert report.drifted[0].thesis_id == thesis.id
    assert "市赚率 PR" in report.rows[0].message
    assert "净资产收益率" not in report.rows[0].message  # 没掉的那一项不进这句话
    assert [event.thesis_id for event in rec.events] == [thesis.id]
    assert "复核" in rec.events[0].detail
    assert "只提示" in rec.events[0].detail  # §9.3：系统不替谁决定平仓


def test_a_drop_inside_one_tier_reads_back_as_compared_and_clean(container) -> None:
    """40 → 58 在同一档里（20 分一档，D 那一头的理由）：跨档才算变了。"""
    thesis = open_thesis(container)
    lay_baseline(container, thesis, {"pr": 40.0})
    rec = Recorder()
    bus = EventBus()
    bus.subscribe(ThesisDrifted, rec)

    report = build(container, bus).run(USER)

    assert [row.state for row in report.rows] == [CLEAN]
    assert rec.events == []


def test_a_drop_on_a_dry_run_is_shown_but_not_published(container) -> None:
    """试运行那一趟屏幕上照旧说「漂移」，但一条事件都不许发：事件是「发生了什么」的记录，
    试运行里什么都没发生——发出去的每一条都是在记一件没发生过的事。"""
    thesis = open_thesis(container)
    lay_baseline(container, thesis, {"pr": 80.0})
    rec = Recorder()
    bus = EventBus()
    bus.subscribe(ThesisDrifted, rec)

    report = build(container, bus).run(USER, dry_run=True)

    assert [row.state for row in report.rows] == [DRIFT]
    assert report.rows[0].written is False
    assert "试运行" in report.rows[0].message
    assert rec.events == []


def test_an_item_the_run_did_not_compute_is_named_not_skipped(container) -> None:
    """基线里有主力净流入、这一趟那一路没数：这句必须是「比不全」，不是「没掉档」。"""
    thesis = open_thesis(container)
    lay_baseline(container, thesis, {"main_inflow": 90.0})

    report = build(container).run(USER)

    assert [row.state for row in report.rows] == [PARTIAL]
    assert "主力净流入" in report.rows[0].message
    assert "没有掉档" not in report.rows[0].message


def test_a_thesis_with_no_baseline_is_never_reported_as_not_drifted(container) -> None:
    """D96 的那句谎单独钉一次：没有基线时屏幕上出现的那个词只能是「建立基线」。"""
    open_thesis(container)
    report = build(container).run(USER)

    for row in report.rows:
        assert row.state != CLEAN
        assert "未漂移" not in row.message


def test_items_are_still_compared_when_the_run_produced_no_total_score(db, settings) -> None:
    """已有基线、这一趟没出总分：分项照样比——`thesis_drift` 判的是分项，不是 `score`。

    拔掉基本面的那个世界实测给的就是这个形状（`score=None` + 四个有数的分项，覆盖不到
    地板只拒合成），基线铺在这四个键上，比出来是「比过了」而不是任何一句谎。
    """
    thin = support_valuation.make_container(db, settings, with_fundamentals=False)
    thesis = open_thesis(thin)
    snapshot = thin.rule_data.read(Target(USER, "A", CODE, None, thesis.id), as_of=AS_OF)
    assert snapshot.score is None and snapshot.items  # 这一枚用例押的就是这个形状
    lay_baseline(thin, thesis, dict(snapshot.items))

    report = build(thin).run(USER)
    row = report.rows[0]

    assert row.state == CLEAN
    assert row.score is None
    assert "总分这一趟没出" in row.message
    # 基线一个字没刷：还是铺下去的那个数、那一天的那笔
    stored = thin.theses_store.get(USER, thesis.id)
    assert stored.baseline_score == 95.0
    assert stored.baseline_on == date(2024, 7, 1)


# --- 谁在复检范围内 -----------------------------------------------------------------------


def test_recheck_walks_opened_and_tracking_only(container) -> None:
    theses = container.theses_store
    # `600000` 没有日线，出行靠读失败那一支——在不在名单里与读不读得到是两件事
    open_thesis(container, code="600000")
    open_thesis(container, status=TRACKING)
    idle = theses.create(
        user_id=USER,
        market="A",
        code="000800",
        style="value",
        actor_id=USER,
        now=T0,
        status=PROPOSED,
    )

    report = build(container).run(USER)

    assert {(row.code, row.status) for row in report.rows} == {
        ("600000", OPENED),
        (CODE, TRACKING),
    }
    assert theses.get(USER, idle.id).baseline_score is None


def test_a_named_thesis_is_rechecked_alone(container) -> None:
    first = open_thesis(container)
    # 同一个代码换一种风格：`(user, market, code, style)` 不同才允许两条同时活着
    second = container.theses_store.create(
        user_id=USER,
        market="A",
        code=CODE,
        style="trend",
        actor_id=USER,
        now=T0,
        status=PROPOSED,
        **LEVELS,
    )
    container.theses_store.advance(USER, second.id, OPENED, actor_id=USER, now=T0)

    report = build(container).run(USER, thesis_id=second.id)

    assert [row.thesis_id for row in report.rows] == [second.id]
    assert report.rows[0].state == BASELINE
    assert container.theses_store.get(USER, first.id).baseline_score is None


def test_a_named_thesis_outside_the_two_statuses_is_refused_with_a_reason(container) -> None:
    """未建仓的论没有「建仓时那一眼」可比：静默返回零行会被读成复检过了。"""
    idle = container.theses_store.create(
        user_id=USER,
        market="A",
        code="600000",
        style="value",
        actor_id=USER,
        now=T0,
        status=PROPOSED,
    )

    with pytest.raises(ValidationError, match="opened"):
        build(container).run(USER, thesis_id=idle.id)


def test_market_filter_skips_theses_from_other_markets(container) -> None:
    """调度器每个市场单独跑一趟复检：不属于这个市场的论一条都不许读。

    点名一条 A 股的论又限定 HK，两句拼不成一句——这里必须出声（静默返回零行会被读成
    「HK 那一趟复检过了」），而 `market=None` 那一头其余用例照旧全比，CLI 不必改。
    """
    thesis = open_thesis(container)
    rec = build(container)
    assert rec.run(USER, as_of=AS_OF, market="HK").rows == ()
    assert len(rec.run(USER, as_of=AS_OF, market="A").rows) == 1
    # 与引擎同一件事：库里存大写，配置里的键原样留着，`"a"` 不归一就是「0 条观点」
    assert len(rec.run(USER, as_of=AS_OF, market="a").rows) == 1
    with pytest.raises(ValidationError, match="不是 HK"):
        rec.run(USER, as_of=AS_OF, thesis_id=thesis.id, market="HK")


def test_an_unknown_thesis_says_unknown(container) -> None:
    with pytest.raises(StorageError, match="thesis"):
        build(container).run(USER, thesis_id="does-not-exist")


def test_the_default_day_is_the_clock_today(container) -> None:
    report = build(container).run(USER)
    assert report.as_of == container.clock.today()


def test_an_explicit_as_of_is_stamped_into_the_write_once_baseline(container) -> None:
    """`as_of` 会落进 `baseline_on`，而那一列写一次就不让改（`set_baseline` 拒第二笔）：
    这一天传错了，基线的日子就永远错着，所以传进来的那天必须原样落库。"""
    thesis = open_thesis(container)
    day = AS_OF - timedelta(days=7)

    report = build(container).run(USER, as_of=day)

    assert report.as_of == day
    assert container.theses_store.get(USER, thesis.id).baseline_on == day
