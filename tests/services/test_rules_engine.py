"""一趟求值：判定 → 过滤 → 冷却 → 投递 → 事件（spec §10.1、D94）。

时钟冻在 2026-10-20T07:30Z（北京 15:30，A 股已收盘）：冷却的判据是
「距上一条多少分钟」，用墙钟的话这枚文件会在跑到第 30 分钟边界时随机红。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sams.core.errors import NotAdjustable, ValidationError
from sams.core.events import AlertTriggered, EventBus, RuleHit
from sams.core.notify import AlertMessage
from sams.services.rules.engine import SESSION_LOOKBACK_DAYS, RuleEngine
from sams.services.rules.snapshot import Snapshot, Target
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.rules import RuleRepo

USER = "u-rule"
MARKET = "A"
CODE = "601088"
#: 北京时间 15:30，A 股已收盘：`price_on` 与「今天」是同一天
NOW = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)
TODAY = NOW.date()


class FixedClock:
    def now_utc(self) -> datetime:
        return NOW

    def today(self) -> date:
        return TODAY


class StubCalendar:
    """日历的替身：只有 10-20 开市，其余日子全闭。时段给空元组 ⇒ `time_window` 不拦。"""

    def __init__(self, *, open_days=(TODAY,)) -> None:
        self.open_days = set(open_days)
        self.day_calls: list[tuple[str, date]] = []

    def is_trading_day(self, market: str, day: date) -> bool:
        self.day_calls.append((market, day))
        return day in self.open_days

    def sessions(self, market: str, day: date) -> tuple:
        return ()


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[AlertMessage] = []

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
    ) -> str:
        self.sent.append(message)
        return f"alert-{len(self.sent)}"


class StubData:
    """`RuleData` 的替身：引擎只用到 `expand` / `holdings` / `thesis_for` / `read`。"""

    def __init__(self, snapshot: Snapshot, holdings=()) -> None:
        self.snapshot = snapshot
        self.held = list(holdings)
        #: 装上它，`read` 就抛：一条挂了错代码的规则不该把整趟拖崩
        self.read_raises: Exception | None = None

    def expand(self, rule) -> list[Target]:
        # 真实 `RuleData.expand` 对「持仓类却没有账户」抛：去重键含 account_id，缺了它
        # 同一只票在两个账户里的破位会互相吞。替身只复刻这一条形状检查，让引擎的
        # `Blocked` 那一摞能被钉住（库里已经躺着这种行时，一趟照旧出别的面）。
        if rule.target_type == "position" and rule.account_id is None:
            raise ValidationError(
                f"规则 {rule.name} 是持仓类却没有账户：去重键含 account_id"
            )
        return [Target(rule.user_id, MARKET, CODE, rule.account_id, rule.thesis_id)]

    def holdings(self, account_id: str) -> list:
        return list(self.held)

    def thesis_for(self, target: Target):
        return None

    def read(self, target: Target, *, as_of: date, position=None) -> Snapshot:
        if self.read_raises is not None:
            raise self.read_raises
        return self.snapshot


def snap(**over) -> Snapshot:
    base = {
        "market": MARKET,
        "code": CODE,
        "name": "中国神华",
        "currency": "CNY",
        "report_currency": "CNY",
        "account_name": "SIM-1",
        "price": 9.5,
        "price_on": TODAY,
        "price_age_days": 0,
    }
    base.update(over)
    return Snapshot(**base)


class Harness:
    """一台装好替身的引擎：用例只改「库里有什么」与「快照是什么」。"""

    def __init__(self, db, settings) -> None:
        self.db = db
        self.settings = settings
        self.rules = RuleRepo(db, clock=FixedClock())
        self.alerts = AlertRepo(db, clock=FixedClock())
        self.sender = RecordingSender()
        self.hits: list[RuleHit] = []
        self.triggered: list[AlertTriggered] = []
        self.bus = EventBus()
        self.bus.subscribe(RuleHit, self.hits.append)
        self.bus.subscribe(AlertTriggered, self.triggered.append)
        self.calendar = StubCalendar()
        self.snapshot = snap()
        self.held: list = []
        self.data: StubData | None = None
        self.read_raises: Exception | None = None
        #: `seed_hit` 默认挂在哪条规则上：用例先 `h.add()` 再 `h.seed_hit()` 就对得上
        self.added_id = "r-seed"

    def engine(self) -> RuleEngine:
        data = StubData(self.snapshot, self.held)
        data.read_raises = self.read_raises
        self.data = data
        return RuleEngine(
            rules=self.rules,
            alerts=self.alerts,
            data=data,
            calendar=self.calendar,
            fx=FxRepo(self.db),
            clock=FixedClock(),
            bus=self.bus,
            sender=self.sender,
            settings=self.settings,
        )

    def add(self, **over):
        kw = {
            "kind": "stop.fixed",
            "name": "跌破 10 元",
            "target_type": "symbol",
            "target_ref": f"{MARKET}/{CODE}",
            "params": {"price": 10.0},
        }
        kw.update(over)
        return self.rules.create(USER, **kw)

    def seed_hit(self, *, minutes_ago=0, stage="hit", rule_id=None, account_id=None):
        """在去重键上种一条**已经推过**的告警：冷却与 `no_open_alert` 的输入。"""
        return self.alerts.create(
            USER,
            rule_id=rule_id or self.added_id,
            rule_name="跌破 10 元",
            account_id=account_id,
            account_name="SIM-1",
            market=MARKET,
            code=CODE,
            symbol_name="中国神华",
            severity="strong",
            stage=stage,
            value_text="9.50 CNY",
            threshold_text="10.00 CNY",
            action_note="止损位到了",
            triggered_at=NOW - timedelta(minutes=minutes_ago),
        )


@pytest.fixture
def h(db, settings) -> Harness:
    return Harness(db, settings)


# --- 投递那一条路 ------------------------------------------------------------------------

def test_a_breach_raises_one_alert_and_two_events(h) -> None:
    h.add()
    report = h.engine().check(USER)
    assert report.quiet == 0
    assert len(h.sender.sent) == 1
    message = h.sender.sent[0]
    assert message.severity == "strong"
    assert message.stage == "hit"
    assert message.value_text == "9.50 CNY"
    assert message.threshold_text == "10.00 CNY"
    assert report.raised[0].alert_id == "alert-1"
    assert [hit.stage for hit in h.hits] == ["hit"]
    assert [got.alert_id for got in h.triggered] == ["alert-1"]


def test_the_action_line_names_the_kind_and_the_users_own_name(h) -> None:
    h.add(name="别让它跌过十块")
    report = h.engine().check(USER)
    note = h.sender.sent[0].action_note
    assert "止损位到了" in note and "别让它跌过十块" in note
    assert report.raised[0].action_note == note


def test_the_body_carries_the_pnl_the_constraint_and_the_reason(h) -> None:
    h.add()
    h.snapshot = snap(qty=1000.0, avg_cost=12.0, opened_on=TODAY)
    h.engine().check(USER)
    body = h.sender.sent[0].body
    assert "浮亏 2,500.00 CNY" in body
    assert "T+1" in body  # 今日买入：这条今天执行不了
    assert "绝对价止损线 10.00 CNY" in body


def test_an_above_line_price_leaves_the_run_quiet(h) -> None:
    h.add()
    h.snapshot = snap(price=11.0)
    report = h.engine().check(USER)
    assert report.raised == () and report.suppressed == ()
    assert report.skipped == () and report.quiet == 1


def test_a_missing_price_is_reported_as_unable_not_as_quiet(h) -> None:
    """「判不了」与「没到」是两句话（§1 的取向：判不准就放行，但要说）。"""
    h.add()
    h.snapshot = snap(price=None, price_on=None, price_age_days=None)
    report = h.engine().check(USER)
    assert report.raised == () and report.quiet == 0
    assert len(report.skipped) == 1
    assert "没有" in report.skipped[0].reason and "现价" in report.skipped[0].reason


def test_a_dry_run_judges_everything_and_sends_nothing(h) -> None:
    h.added_id = h.add().id
    report = h.engine().check(USER, dry_run=True)
    assert report.dry_run is True
    assert len(report.raised) == 1
    assert report.raised[0].alert_id == ""
    assert h.sender.sent == [] and h.hits == [] and h.triggered == []


def test_an_unreadable_target_is_skipped_instead_of_killing_the_run(h) -> None:
    """挂在一个查无此码上的规则：这一条判不了，那一趟要跑完并且把原因写出来。"""
    h.read_raises = NotAdjustable("A/000001 全库一根 K 线都没有，主档里也没有这个代码")
    h.add(target_ref="A/000001")
    report = h.engine().check(USER)
    assert report.rules == 1 and report.judged == 1
    assert len(report.skipped) == 1 and report.raised == ()
    assert "K 线" in report.skipped[0].reason
    assert h.sender.sent == []


# --- 冷却（D94）--------------------------------------------------------------------------

def test_cooldown_swallows_a_repeat_of_the_same_stage(h) -> None:
    h.added_id = h.add().id
    h.seed_hit(minutes_ago=5, stage="hit")
    report = h.engine().check(USER)
    assert h.sender.sent == []
    assert len(report.suppressed) == 1
    why = report.suppressed[0].reason
    assert "冷却" in why and "上一条" in why and "hit" in why


def test_an_escalation_punches_through_the_cooldown(h) -> None:
    """D94：`pre` 报过之后 30 分钟内的 `hit` 是真跌破，不能被降噪吞掉。"""
    h.added_id = h.add().id
    h.seed_hit(minutes_ago=5, stage="pre")
    report = h.engine().check(USER)
    assert len(h.sender.sent) == 1
    assert report.suppressed == () and report.raised[0].stage == "hit"


def test_a_hit_beyond_the_window_reports_again(h) -> None:
    h.added_id = h.add().id
    h.seed_hit(minutes_ago=31, stage="hit")
    assert len(h.engine().check(USER).raised) == 1


def test_the_cooldown_window_is_the_rules_own_number(h) -> None:
    """`cooldown_min = 0` 的意思是「每一次都报」（§10.1 那个默认值不是命令）。"""
    h.added_id = h.add(cooldown_min=0).id
    h.seed_hit(minutes_ago=0, stage="hit")
    assert len(h.engine().check(USER).raised) == 1


def test_another_accounts_hit_does_not_silence_this_one(h) -> None:
    """§1 那张表的第三行：去重键含 `account_id`（§10.1）。

    两头都钉：A 账户那条拦不住 B 账户的破位；同一条 B 的才拦得住。
    只钉前一头的话，把键写成「不含 account_id」也能过——因为那时谁也拦不住谁。
    """
    h.added_id = h.add(account_id="acc-b").id
    h.snapshot = snap(account_id="acc-b")
    h.seed_hit(minutes_ago=5, stage="hit", account_id="acc-a")
    assert len(h.engine().check(USER).raised) == 1
    h.seed_hit(minutes_ago=5, stage="hit", account_id="acc-b")
    assert h.engine().check(USER).suppressed != ()


# --- 四个开关在这一趟里的位置（§10.1）------------------------------------------------------

def test_a_switch_suppresses_and_says_which_one(h) -> None:
    h.added_id = h.add(filters={"data_freshness": True}).id
    h.snapshot = snap(price_age_days=10)
    report = h.engine().check(USER)
    assert h.sender.sent == []
    assert "10 天前" in report.suppressed[0].reason


def test_no_open_alert_suppresses_a_quieter_repeat_on_the_same_symbol(h) -> None:
    h.added_id = h.add(filters={"no_open_alert": True}).id
    h.seed_hit(minutes_ago=5, stage="urgent")
    report = h.engine().check(USER)
    assert "未处理" in report.suppressed[0].reason


def test_the_gate_asks_the_calendar_for_the_targets_own_market(h) -> None:
    h.add()
    h.engine().check(USER)
    assert h.calendar.day_calls[0][0] == MARKET


def test_the_last_session_is_walked_back_from_the_local_day(h) -> None:
    """日历一个交易日都没给 ⇒ `exclude_halt` 不拦（判不了就放行），这里钉它确实回走过。"""
    h.add()
    h.calendar = StubCalendar(open_days=())  # 日历说这些天全闭
    h.engine().check(USER)
    assert len(h.calendar.day_calls) >= SESSION_LOOKBACK_DAYS


# --- 一趟的形状：一条坏规则不拖整列（§1 第五行）--------------------------------------------

def test_a_rule_that_cannot_expand_blocks_only_itself(h) -> None:
    good = h.add(name="好的那条")
    # 绕过仓库直接写一行「持仓类却没有账户」：仓库那道闸拦的是写入，这一枚要的是库里
    # 已经躺着这种行时（M3-D 的派生写入方、手工修过库）一趟照旧出别的面
    h.db.execute(
        "INSERT INTO rules (id, user_id, name, enabled, target_type, target_ref,"
        " kind, condition_kind, params_json, filters_json, cooldown_min, severity,"
        " channel_ids, created_at, updated_at)"
        " VALUES ('bad-row', ?, '缺账户的那条', TRUE, 'position', '*', 'stop.pct',"
        " 'template', '{\"pct\": 0.08}', '{}', 30, 'strong', '[]', ?, ?)",
        [USER, NOW, NOW],
    )
    report = h.engine().check(USER)
    assert [got.rule_id for got in report.raised] == [good.id]
    assert len(report.blocked) == 1
    assert "account_id" in report.blocked[0].reason


def test_disabled_rules_are_skipped_unless_named(h) -> None:
    made = h.add(enabled=False)
    assert h.engine().check(USER).raised == ()
    assert len(h.engine().check(USER, rule_id=made.id).raised) == 1


def test_as_of_moves_the_reading_day_without_moving_the_clock(h) -> None:
    h.added_id = h.add().id
    report = h.engine().check(USER, as_of=date(2026, 10, 16))
    assert report.as_of == date(2026, 10, 16)
    assert len(report.raised) == 1
