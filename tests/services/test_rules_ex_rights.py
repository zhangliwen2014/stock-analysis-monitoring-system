"""除权日那一趟真链路：从 `corp_actions` 里那一行，到屏幕上那句「换算」（§10.2）。

与 `test_rules_anchor.py`（验算式：手搓快照，三种错法都能复现）和
`test_rules_snapshot.py`（验两个生产者接得上）的分工：这一枚验**中间没有第三个转换**——
因子出自真库，锚出自真 `rules` 行（0017），线出自 `bootstrap` 接上的那份 `RuleData`，
判定出自真引擎。那一行除权没被读到的话取数那枚也会红，但**读到了却没走到判定**只有
这一枚红得出来：它的断言全在引擎的出口上。

验收世界：平盘 299 天收 10.00，最后一根收 9.30，最后一天每股派 1.0（因子 10÷9）。

- 因子是干净的 10/9 ⇒ 写在除权之前的「10 元」今天站在 9.00，一个数都不许多出来的近似，
  人眼也量得出。
- 现价 9.30 正好落在两个坐标系中间：搬对了安静（9.30 > 9.00），不搬就响（9.30 < 10.00）——
  屏幕上那句「跌破止损」会指挥一个人去卖一笔并没有亏的仓，这就是 §10.2 要关掉的东西。
- 这一枚不碰 `stop.atr`：那条模板的两个判据都不在人填的那一头——`multiple` 的单位是倍，
  ATR 出自后复权序列（机器算的那一头归 `_fold.py`），搬无可搬；成本的锚由 `stop.pct`
  那两枚钉住。

数是探针量出来的：`factor=1.1111111111111112`、`prev_close=10.0`、`ex_ref=9.0`、
`anchored_on=2024-01-01`、现价 9.30 / 读数日 2024-10-26，`close_hfq` 最后一根 10.333…
而倒数第二根 10.0 ⇒ 折回现价口径的前收盘正好 9.00（`stop_stage` 的 `urgent` 判据要拿它
跟开盘比，别把它当成 10.00）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import support_valuation as sv
from support_rules import RecordingSender
from support_valuation import CODE, DAYS, TODAY

from sams.core.events import EventBus, RuleHit
from sams.services.rules.derive import RuleDeriver
from sams.services.rules.engine import RuleEngine
from sams.storage.repos.rules import Rule

USER = "u-ex"
PWD = "correct-horse-battery"
#: 除权日就是读数日：这一枚要验的正是「在这一天判一条除权之前写下的线」
AS_OF = TODAY
#: 除权之前写下的那两列数的日期：`params_on` 与论上的 `levels_on` 都用它
STAMPED = date(2024, 3, 1)
MOMENT = datetime(2024, 3, 1, 6, 0, tzinfo=UTC)
FLAT = [10.0] * (len(DAYS) - 1) + [9.3]


def world(db, settings, *, dividend: bool):
    """平盘世界；`dividend` 决定库里有没有那一行除权。

    实验组与对照组只差那一行：同一份 `closes`、同一个冻钟、同一套资产。少了这一句，
    「安静」与「响」的差别可能来自任何别的东西，而 §10.2 要钉的是除权这一件事。
    """
    container = sv.make_container(
        db,
        settings,
        closes=FLAT,
        actions=sv.dividend(AS_OF) if dividend else None,
    )
    # 不铺日历：引擎确实会去查（`_gate` 那一头），但四个过滤开关默认全关 ⇒ 查回来的
    # 那个结果没有任何断言读它，而这一枚的断言全在引擎的出口上
    return container


def person(container) -> str:
    """一个真用户：规则、账户、论都挂在他的 id 上（命令行那头拿的也是 id）。"""
    return container.users.create(USER, PWD).id


def add(container, owner: str, **over) -> Rule:
    """直接过仓库建一条 `stop.fixed`。

    不走维护面：那里的 `add` 只会盖 `clock.today()`，而这一枚要的是**除权之前**那个日期戳，
    以及 0017 之前那种没有日期戳的行。默认带戳，不想要就传 `params_on=None`。
    """
    kw: dict = {
        "kind": "stop.fixed",
        "name": "跌破 10 元",
        "target_type": "symbol",
        "target_ref": f"A/{CODE}",
        "params": {"price": 10.0},
        "params_on": STAMPED,
    }
    kw.update(over)
    return container.rules_store.create(owner, **kw)


def open_position(container, owner: str) -> str:
    """一笔 2024-03-01 建仓、成本 10.00 的持仓；`open_ts` 就是那一笔的时刻。

    `ts` 给显式时刻而不是省掉（`record_fill` 的默认走墙钟）。选 06:00Z 是因为它在 UTC 与
    北京两边都是 3 月 1 日——`_date_of` 取的是 `.date()`，一个跨时区会换日的时刻会让这一枚
    不知道自己在测哪一天。
    """
    account = container.sim_accounts.open(owner, "SIM-1").id
    container.sim_accounts.adjust_cash(
        account, "CNY", 100_000.0, reason="初始", actor_id=owner
    )
    container.sim_ledger.record_fill(
        account_id=account,
        user_id=owner,
        market="A",
        code=CODE,
        side="buy",
        qty=1000.0,
        price=10.0,
        currency="CNY",
        fee=0.0,
        ts=MOMENT,
    )
    return account


def add_position_rule(container, owner: str, account: str) -> Rule:
    """一条 `stop.pct`：它的参数不是金额（`pct` 单位是比例），所以锚不在 0017 那一列，
    而在成本那一头（`positions.open_ts`）。这一枚与上面那条正好是两处锚的两面。
    """
    return add(
        container,
        owner,
        kind="stop.pct",
        name="成本下浮 5%",
        target_type="position",
        target_ref="*",
        params={"pct": 0.05},
        account_id=account,
        params_on=None,
    )


class Run:
    """一台装的是**真取数**的引擎：`data` 直接拿 `container.rule_data`。

    故意不在这里重新装配一份 `RuleData`（`test_rules_snapshot.py` 里的 `as_data` 干的是
    那件事，它在替身快照上验形状）：这一枚要验的就是 `bootstrap` 接上的那一条链——那里
    漏填了 `factors`，这一枚就得红。投递走替身（真通道要打网络），事件挂在一条本地的
    `EventBus` 上，不蹭容器那条总线，免得别处的订阅者替这一枚说话。
    """

    def __init__(self, container) -> None:
        self.sender = RecordingSender()
        self.hits: list[RuleHit] = []
        bus = EventBus()
        bus.subscribe(RuleHit, self.hits.append)
        self.engine = RuleEngine(
            rules=container.rules_store,
            alerts=container.alerts_store,
            data=container.rule_data,
            calendar=container.calendar,
            fx=container.fx_store,
            clock=container.clock,
            bus=bus,
            sender=self.sender,
            settings=container.settings,
        )

    def check(self, owner: str, **over):
        """这一趟的出口。用例只经这里读报告：`engine` 那一个字段是给 `check` 用的。"""
        return self.engine.check(owner, **over)

    @property
    def message(self):
        """那一条推出去的消息。0 条或 2 条都在这里红，不在用例里红。"""
        assert len(self.sender.sent) == 1, self.sender.sent
        return self.sender.sent[0]


# --- 那条绝对价线（§10.2 那句话的正面与反面）------------------------------------------------


def test_the_ex_rights_day_does_not_say_stop_lost(db, settings):
    """除权日不许误报「跌破止损」：10.00 那条线今天站在 9.00，而现价是 9.30。"""
    container = world(db, settings, dividend=True)
    owner = person(container)
    add(container, owner)
    report = Run(container).check(owner)

    # 一条规则一个对象只会有一个去向，所以这一句同时说了三件事：没报、没「判不了」、没被降噪
    assert report.quiet == 1


def test_the_same_rule_fires_when_nothing_was_ex_distributed(db, settings):
    """对照组：同一条线、同一个价，库里少了那一行除权 ⇒ 响，阈值是写着的那一个。

    少了这一枚，上一枚的「安静」可能来自任何原因（现价读不到、过滤拦了、冷却挡了）。
    """
    container = world(db, settings, dividend=False)
    owner = person(container)
    add(container, owner)
    run = Run(container)
    report = run.check(owner)

    assert len(report.raised) == 1
    assert run.message.threshold_text == "10.00 CNY"
    assert "换算" not in run.message.body  # 没搬就不许写那一行


def test_a_line_that_still_reaches_says_how_far_it_was_moved(db, settings):
    """搬过而仍然够得着的线：正文给那两个数与那一天，payload 给原值不给句子。

    线 11.00 → 9.90，现价 9.30 ⇒ 还是报。这一枚钉的是「搬过头也不许漏报」的另一面：
    报出来的阈值必须是搬完的那一个（9.90），不是库里写着的那一个（11.00）。
    """
    container = world(db, settings, dividend=True)
    owner = person(container)
    add(container, owner, params={"price": 11.0})
    run = Run(container)
    report = run.check(owner)

    assert len(report.raised) == 1
    message = run.message
    assert message.threshold_text == "9.90 CNY"
    assert "换算" in message.body
    assert "11.00 CNY" in message.body
    assert "2024-03-01" in message.body and "2024-10-26" in message.body
    moved = run.hits[0].payload["moved"]
    assert [item["column"] for item in moved] == ["params.price"]
    assert moved[0]["before"] == 11.0
    assert moved[0]["after"] == pytest.approx(9.9)
    assert moved[0]["on"] == "2024-03-01"
    assert moved[0]["ex_dates"] == ["2024-10-26"]


def test_an_unstamped_row_is_not_judged_on_the_ex_rights_day(db, settings):
    """0017 之前那种行（没有日期戳）落在除权日：整条不判，并且说得出跑什么。

    拿原值判是误报（第一枚），搬又怕搬少了（那一行不知道自己属于哪天）。屏幕上出现的是
    一句带命令的「不判」，不是一个看起来像结论的数。
    """
    container = world(db, settings, dividend=True)
    owner = person(container)
    add(container, owner, params_on=None)
    report = Run(container).check(owner)

    assert len(report.skipped) == 1
    assert "rule edit" in report.skipped[0].reason


# --- 成本的锚在另一头（`positions.open_ts`）------------------------------------------------


def test_the_cost_moves_with_the_line(db, settings):
    """成本也换坐标系：10.00 的成本除权日站在 9.00 ⇒ 那条 5% 的线是 8.55，不是 9.50。"""
    container = world(db, settings, dividend=True)
    owner = person(container)
    account = open_position(container, owner)
    add_position_rule(container, owner, account)
    run = Run(container)
    report = run.check(owner)

    # 不搬的话那条线是 9.50 ⇒ 现价 9.30 会响，所以这份安静只能是「成本搬到 9.00、
    # 线算成 8.55、仍然没到」换来的
    assert report.quiet == 1


def test_the_same_position_fires_when_nothing_was_ex_distributed(db, settings):
    """对照组：撤掉那次除权，同一笔的成本就是 10.00 ⇒ 线 9.50，现价 9.30 ⇒ 响。

    库里那一行 `avg_cost` 在两个世界里都是 10.00：搬的是判据用的那个数，不是账本。
    """
    container = world(db, settings, dividend=False)
    owner = person(container)
    account = open_position(container, owner)
    add_position_rule(container, owner, account)
    run = Run(container)
    report = run.check(owner)

    assert len(report.raised) == 1
    assert run.message.threshold_text == "9.50 CNY"


# --- §9.2 与 §10.2 在这一枚交汇 -------------------------------------------------------------


def test_a_derived_line_moves_on_the_thesis_reading_day(db, settings):
    """派生那条 `stop.fixed` 的锚是论上的读数日（0013 的 `levels_on`），不是填它的那天。

    盖 `clock.today()` 会红在哪里，这一枚分得出来：容器里那个钟冻在 2024-10-26，那正是
    除权日，而 `steps_after` 不含当日 ⇒ 一步都不搬 ⇒ 9.30 < 10.00 ⇒ 报出那句误报。
    """
    container = world(db, settings, dividend=True)
    owner = person(container)
    thesis = container.theses_store.create(
        user_id=owner,
        market="A",
        code=CODE,
        style="value",
        actor_id=owner,
        now=MOMENT,
        status="proposed",
        stop_price=10.0,
        target_price=11.0,
        levels_on=STAMPED,
    )
    made = RuleDeriver(container.rules_store, container.theses_store).derive(
        owner, thesis.id
    )
    stop = next(row for row in made.created if row.kind == "stop.fixed")
    assert stop.params_on == STAMPED

    report = Run(container).check(owner, rule_id=stop.id)

    assert report.quiet == 1
