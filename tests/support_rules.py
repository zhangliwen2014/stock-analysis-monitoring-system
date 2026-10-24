"""规则引擎那几套替身：`test_rules_engine.py` 与对抗面共用同一台假引擎。

搬出来是因为对抗面要验的是「跨层的说谎路径」，而它必须与单元用例用**同一套**替身：
两份替身一旦分叉，一边验过的形状就管不到另一边。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sams.core.errors import ValidationError
from sams.core.events import AlertTriggered, EventBus, RuleHit
from sams.core.notify import AlertMessage
from sams.services.rules.engine import RuleEngine
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
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now

    def today(self) -> date:
        return self.now.date()


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


class StoringSender:
    """会往 `alerts` 里写一行的投递替身。

    冷却那条链的判据是库里那一行（§10.1「重启从 alerts 表恢复」），替身不写库就只剩
    「手工种一行」——那验不到真发出去的那一次到底留下了什么。
    """

    def __init__(self, alerts: AlertRepo) -> None:
        self.alerts = alerts
        self.sent: list[AlertMessage] = []
        #: 装上它，`dispatch` 就抛：投递是引擎够得着的唯一外部动作
        self.raises: Exception | None = None

    def dispatch(
        self,
        message: AlertMessage,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
    ) -> str:
        if self.raises is not None:
            raise self.raises
        alert = self.alerts.create(
            user_id,
            rule_id=rule_id,
            rule_name=message.rule_name,
            account_id=account_id,
            account_name=message.account_name,
            market=message.market,
            code=message.code,
            symbol_name=message.name,
            severity=message.severity,
            stage=message.stage,
            value_text=message.value_text,
            threshold_text=message.threshold_text,
            action_note=message.action_note,
            triggered_at=message.triggered_at,
            channel_ids=list(channel_ids),
            extra={"body": message.body},
        )
        self.sent.append(message)
        return alert.id


class StubData:
    """`RuleData` 的替身：引擎只用到 `expand` / `holdings` / `thesis_for` / `read`。"""

    def __init__(self, snapshot: Snapshot, holdings=(), thesis=None) -> None:
        self.snapshot = snapshot
        self.held = list(holdings)
        self.thesis = thesis
        #: 装上它，`read` 就抛：一条挂了错代码的规则不该把整趟拖崩
        self.read_raises: Exception | None = None
        #: 记下每次日线读数落在哪天：盘中那一趟的日期应当跟着报价走（§5.3）
        self.as_of_seen: list[date] = []

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
        return self.thesis

    def read(self, target: Target, *, as_of: date, position=None) -> Snapshot:
        self.as_of_seen.append(as_of)
        if self.read_raises is not None:
            raise self.read_raises
        return self.snapshot


class StubQuotes:
    """QuoteBoard 的替身：引擎只问 `get` 与 `previous`（§5.3 的 `QuoteView`）。"""

    def __init__(self, current=None, previous=None) -> None:
        self.current = dict(current or {})
        self.previous_map = dict(previous or {})
        self.gets: list[tuple[str, str]] = []

    def get(self, market, code):
        self.gets.append((market, code))
        return self.current.get((market, code))

    def previous(self, market, code):
        return self.previous_map.get((market, code))


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
        self.fx = FxRepo(db)
        self.sender = RecordingSender()
        self.hits: list[RuleHit] = []
        self.triggered: list[AlertTriggered] = []
        self.bus = EventBus()
        self.bus.subscribe(RuleHit, self.hits.append)
        self.bus.subscribe(AlertTriggered, self.triggered.append)
        self.calendar = StubCalendar()
        self.clock = FixedClock()
        self.snapshot = snap()
        self.held: list = []
        self.thesis = None
        self.read_raises: Exception | None = None
        #: `seed_hit` 默认挂在哪条规则上：`add()` 每建一条就把它改成那一条的 id
        self.added_id = "r-seed"

    def engine(self, *, sender=None) -> RuleEngine:
        data = StubData(self.snapshot, self.held, self.thesis)
        data.read_raises = self.read_raises
        return RuleEngine(
            rules=self.rules,
            alerts=self.alerts,
            data=data,
            calendar=self.calendar,
            fx=self.fx,
            clock=self.clock,
            bus=self.bus,
            sender=sender or self.sender,
            settings=self.settings,
        )

    def storing(self, *, raises: Exception | None = None) -> RuleEngine:
        """同一台引擎，只把投递替身换成「会写库」那一个：冷却那一头才走得到底。"""
        sender = StoringSender(self.alerts)
        sender.raises = raises
        return self.engine(sender=sender)

    def add(self, **over):
        kw = {
            "kind": "stop.fixed",
            "name": "跌破 10 元",
            "target_type": "symbol",
            "target_ref": f"{MARKET}/{CODE}",
            "params": {"price": 10.0},
        }
        kw.update(over)
        rule = self.rules.create(USER, **kw)
        self.added_id = rule.id
        return rule

    def seed_hit(
        self,
        *,
        minutes_ago=0,
        stage="hit",
        rule_id=None,
        account_id=None,
        user_id: str = USER,
        code: str = CODE,
    ):
        """在去重键上种一条**已经推过**的告警：冷却与 `no_open_alert` 的输入。

        用户与代码可由调用方给：对抗面要验的正是「换个人、换个代码还挡不挡」，
        写死的那一版只能服务单层用例。
        """
        return self.alerts.create(
            user_id,
            rule_id=rule_id or self.added_id,
            rule_name="跌破 10 元",
            account_id=account_id,
            account_name="SIM-1",
            market=MARKET,
            code=code,
            symbol_name="中国神华",
            severity="strong",
            stage=stage,
            value_text="9.50 CNY",
            threshold_text="10.00 CNY",
            action_note="止损位到了",
            triggered_at=NOW - timedelta(minutes=minutes_ago),
        )
