"""一趟求值：判定 → 过滤 → 冷却 → 投递 → 事件（spec §10.1、D94、D95）。

两处顺序不能调：

- **过滤在冷却之前**。冷却问的是「这条去重键上最近一次命中是什么时候」，而被
  「开盘 5 分钟内」拦下的那次读数根本没走到「命中」这一步。让它先排队，冷却窗口就会被
  一堆没推出去的读数填满——真破位那次正好落在窗口里，于是没人听见（§1 那张表第二行）。
- **事件在投递之后**。`RuleHit` / `AlertTriggered` 只在真投递的那条路径上发：
  M3-D 的调度与 M4 的忽略率统计读的都是「发出去过几条」，压制与静默不发事件。

冷却与去重**直接查 `alerts`**：§10.1 那句「进程重启从 `alerts` 表恢复冷却状态」要求判据
是库里那一行，内存里那张表一重启就没了，症状是每次重启后半小时全市场重发一遍。
所以这一层的替身只装在取数（`RuleData`）与投递（`AlertSender`）两个头上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_market
from sams.core.config import Settings
from sams.core.errors import NotAdjustable, SamsError, ValidationError
from sams.core.events import AlertTriggered, EventBus, RuleHit
from sams.core.notify import AlertMessage, AlertSender
from sams.core.quotes import Quote, local_day, source_label
from sams.core.rules import RULE_KINDS, Verdict, escalates, is_intraday, template_for
from sams.services.rules.anchor import move_prices
from sams.services.rules.filters import Gate, OpenAlert, run_filters
from sams.services.rules.notes import Fx, constraint_lines, holding_pnl
from sams.services.rules.snapshot import RuleData, Snapshot, Target, apply_quote
from sams.services.rules.templates import TABLE, Ask
from sams.storage.repos.alerts import AlertRepo
from sams.storage.repos.fx import FxRepo
from sams.storage.repos.ledger import Position
from sams.storage.repos.rules import Rule, RuleRepo

#: 往回找「最近一个已开市的日子」最多找这么多天。20 天盖住春节与国庆两段连休，
#: 再长就该由日历与健康页说话，而不是让引擎拿一个猜出来的日子去判停牌。
SESSION_LOOKBACK_DAYS = 20


class QuoteView(Protocol):
    """盘中一趟向引擎开口的两个问题：这只票现在什么价，上一轮是什么价。

    `QuoteBoard` 天然长成这个形状；替身与它同构（`support_rules.StubQuotes`）。
    """

    def get(self, market: str, code: str) -> Quote | None: ...

    def previous(self, market: str, code: str) -> Quote | None: ...


#: 逐个 kind 的那几句：家族说不出「复核」与「清仓」的差别
ACTIONS: dict[str, str] = {
    "stop.time": "复核这笔：持有到期而没达预期",
    "stop.thesis_drift": "先复核理由，再决定这笔的去留",
    "tp.tiered": "按分档计划处置这笔的一部分",
    "scheduled.reevaluate": "该重读这条论了：新的一期已披露",
}

#: 家族级：同一家族的动作是同一句话。写成家族而不是九个 kind，是为了让下一次
#: 加止损模板的人不必记得补——漏补的症状只是屏幕上退回那句通用的话。
_FAMILY = {
    "stop": "止损位到了，按处置计划动这笔",
    "price": "价位到了，看一眼这笔要不要动",
    "thesis": "这条论变了，复核它的价位与赔率还成不成立",
}

DEFAULT_ACTION = "按这条规则处置"


def _quote_line(quote: Quote, zone: ZoneInfo | None) -> str:
    """正文第二行（D120/D134）：报价的时刻与来源，市场本地时区，秒级。

    「延迟未标注」也要写：没标注不等于保证实时，写出来用户才知道该信几分。
    """
    shown = quote.ts if zone is None else quote.ts.astimezone(zone)
    parts = [f"报价 {shown:%H:%M:%S}", source_label(quote.source)]
    if quote.delayed is True:
        parts.append("延迟")
    elif quote.delayed is None:
        parts.append("延迟未标注")
    return " · ".join(parts)


@dataclass(frozen=True)
class Raised:
    """一条真的推出去的告警。`alert_id` 是库里那一行，`sams alert show` 按它追全过程。"""

    alert_id: str
    rule_id: str
    rule_name: str
    user_id: str
    market: str
    code: str
    name: str
    account_id: str | None
    stage: str
    severity: str
    value_text: str
    threshold_text: str
    action_note: str


@dataclass(frozen=True)
class Suppressed:
    """判到了、这一趟不推。`reason` 是要上屏幕的那句话——沉默是这一层最贵的错法。"""

    rule_id: str
    rule_name: str
    market: str
    code: str
    stage: str
    reason: str


@dataclass(frozen=True)
class Skipped:
    """这一条这一趟没有结论：`reason` 单独上屏，不与「没到」混在一摞。"""

    rule_id: str
    rule_name: str
    market: str
    code: str
    reason: str


@dataclass(frozen=True)
class Blocked:
    """这条规则本身的形状不对（展开失败），一趟里只它停，别的照跑。"""

    rule_id: str
    rule_name: str
    reason: str


@dataclass(frozen=True)
class RunReport:
    """一趟的全部出口。`sams rule check` 的四摞与 `--json` 的四个数组读同一份。"""

    as_of: date
    now: datetime
    dry_run: bool
    rules: int
    raised: tuple[Raised, ...] = ()
    suppressed: tuple[Suppressed, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    quiet: int = 0

    @property
    def judged(self) -> int:
        """到了「判」这一步的对象数：不含展开失败的那几条规则。"""
        return len(self.raised) + len(self.suppressed) + len(self.skipped) + self.quiet


def action_for(kind: str, name: str) -> str:
    """§11 标题行里那句「要做什么」：动词按 kind 定，尾巴挂用户自己起的名。"""
    family = kind.split(".", 1)[0]
    head = ACTIONS.get(kind) or _FAMILY.get(family) or DEFAULT_ACTION
    return f"{head}（{name}）"


def _no_impl(kind: str) -> str:
    """这一层没有这个 kind 的那句话（`Skipped.reason` 的一个出处）。"""
    return f"这一层没有 {kind} 的求值实现：名单的唯一出处是 core/rules.py"


def _moved_payload(snapshot: Snapshot) -> list[dict[str, object]]:
    """`RuleHit.payload` 里那一串搬运：给数，不给格式化后的句子。

    文本在 `Moved.line` 那里已经有了，那一行是给屏幕读的；这几个数是给事件订阅方读的，
    两头都不许去 parse 对方。日期一律成串：`payload` 是要出进程的机器读数，而
    `json.dumps` 的默认编码器装不下一个 `date`（要么炸，要么被手写编码器换成别的东西）。
    """
    return [
        {
            "column": move.column,
            "before": move.before,
            "after": move.after,
            "on": str(move.on),
            "ex_dates": [str(day) for day in move.ex_dates],
        }
        for move in snapshot.moved
    ]


@dataclass
class _Cache:
    """一趟里的三张临时表。它们只在一趟内有效：跨趟复用持仓会把「今天新买的那笔」漏掉。"""

    positions: dict[str, dict[tuple[str, str], Position]] = field(default_factory=dict)
    sessions: dict[tuple[str, date], tuple] = field(default_factory=dict)
    last_session: dict[tuple[str, date], date | None] = field(default_factory=dict)


class RuleEngine:
    """把一条规则跑成一个对象的一次判定。构造参数全是 `Container` 上已有的字段。"""

    def __init__(
        self,
        *,
        rules: RuleRepo,
        alerts: AlertRepo,
        data: RuleData,
        calendar,
        fx: FxRepo,
        bus: EventBus,
        sender: AlertSender,
        settings: Settings,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.rules = rules
        self.alerts = alerts
        self.data = data
        self.calendar = calendar
        self.fx = fx
        self.bus = bus
        self.sender = sender
        self.settings = settings
        self.clock = clock

    # --- 出口 ---------------------------------------------------------------------------

    def check(
        self,
        user_id: str,
        *,
        as_of: date | None = None,
        market: str | None = None,
        dry_run: bool = False,
        rule_id: str | None = None,
        intraday: bool = False,
        quotes: QuoteView | None = None,
    ) -> RunReport:
        """一趟同步求值。调度器的 `rules` job 按市场跑（传 `market`），CLI 那一趟不传，
        而本函数不知道自己被调了几次——一次还是三个市场，是调用方的事。

        `market` 是对象级子集：过滤在展开之后做——规则的目标可以是 `*` 那种
        「我的全部持仓」，展开之前问不出「这里面哪些属于 HK」。大小写先归一（D46）：
        配置里那个键是原样留下的，`"hk"` 不归一就会让每一张规则都「对象全在别的市场」，
        而那一趟出门说的是「触发 0」，与一晚平安长得一模一样。
        """
        now = self.clock.now_utc()
        day = as_of if as_of is not None else self.clock.today()
        if intraday and quotes is None:
            # 不给默认空板：一个忘传的 None 会让整趟「一只票都没报价」悄悄变绿，
            # 那是装配错误，不是业务结论
            raise ValidationError("盘中一趟必须带行情板：quotes 是这一次求值的现价出处")
        if quotes is not None and not intraday:
            # 两个参数是一对的：只给板不给 intraday，收盘那一趟会被新鲜度闸悄悄改写
            raise ValidationError(
                "quotes 只在盘中那一趟用：要么连 intraday=True 一起给，要么都不给"
            )
        if market is not None:
            market = canonical_market(market)
        raised: list[Raised] = []
        suppressed: list[Suppressed] = []
        skipped: list[Skipped] = []
        blocked: list[Blocked] = []
        quiet = 0
        cache = _Cache()
        selected = self._select(user_id, rule_id, intraday=intraday)
        for rule in selected:
            try:
                targets = self.data.expand(rule)
            except ValidationError as exc:
                blocked.append(Blocked(rule_id=rule.id, rule_name=rule.name, reason=str(exc)))
                continue
            if market is not None:
                targets = [target for target in targets if target.market == market]
                if not targets:
                    # 这条规则的对象全在别的市场：这一趟一个都没读，所以五摞出口
                    # （触发/压制/跳过/阻塞/没到）都不因此动一行——只有 `rules` 那个数
                    # 照旧算它一眼，它问的是「这一趟看了几条规则」而不是「看了几个对象」
                    continue
            held = self._holdings(rule, cache)
            for target in targets:
                fate = self._one(
                    rule,
                    target,
                    position=held.get((target.market, target.code)),
                    day=day,
                    now=now,
                    dry_run=dry_run,
                    cache=cache,
                    quotes=quotes,
                )
                if isinstance(fate, Raised):
                    raised.append(fate)
                elif isinstance(fate, Suppressed):
                    suppressed.append(fate)
                elif isinstance(fate, Skipped):
                    skipped.append(fate)
                else:
                    quiet += 1
        return RunReport(
            as_of=day,
            now=now,
            dry_run=dry_run,
            rules=len(selected),
            raised=tuple(raised),
            suppressed=tuple(suppressed),
            skipped=tuple(skipped),
            blocked=tuple(blocked),
            quiet=quiet,
        )

    def _select(
        self, user_id: str, rule_id: str | None, *, intraday: bool = False
    ) -> list[Rule]:
        """点名一条规则时绕过 `enabled`：用户刚停用一条又 `--rule` 点它，
        要的显然是「这条如果开着会判出什么」，回一句空列表最没用。

        盘中那一趟走另一扇门（§5.3）：跨全部用户的启用规则里挑出 intraday 的那半，
        再按用户切——名单来自 `is_intraday` 读模板旗标，不是这里再抄一份旗标；
        名单外的 kind 不滤掉（理由见分支里那句注释）。
        """
        if intraday:
            # 名单只按**认识的** kind 的模板旗标滤：`is_intraday` 对名单外的 kind 是抛
            # KeyError（`TEMPLATES` 是唯一出处），拿它当筛子就等于让库里躺着的一条野行
            # 把整趟拖崩。野行留在名单里交给 `_one`——那里 `TABLE.get` 查不到会给一句
            # skipped（与收盘那一趟同一个取向：那种行不该把整趟拖崩）。
            return [
                rule
                for rule in self.rules.list_enabled()
                if rule.user_id == user_id
                and (rule.kind not in RULE_KINDS or is_intraday(rule.kind))
            ]
        listed = self.rules.list_for(user_id, enabled_only=rule_id is None)
        if rule_id is None:
            return listed
        return [rule for rule in listed if rule.id == rule_id]

    # --- 一个对象 -----------------------------------------------------------------------

    def _one(
        self,
        rule: Rule,
        target: Target,
        *,
        position: Position | None,
        day: date,
        now: datetime,
        dry_run: bool,
        cache: _Cache,
        quotes: QuoteView | None = None,
    ) -> Raised | Suppressed | Skipped | None:
        quote: Quote | None = None
        zone: ZoneInfo | None = None
        if quotes is not None:
            quote = quotes.get(target.market, target.code)
            if quote is None:
                # 没报价不判（§5.3）：连「stale」都谈不上——这只票今天压根没被问过价
                return self._skipped(
                    rule, target.market, target.code,
                    "盘中还没有这只票的报价：这一趟不判",
                )
            stale = self._stale_note(quote)
            if stale:
                return self._skipped(rule, target.market, target.code, stale)
            # 日线读到报价说自己是哪天：US 的收盘在北京是次日凌晨
            zone = self._zone(target.market)
            day = local_day(quote.ts, zone) if zone is not None else quote.ts.date()
        # 这道检查排在 `template_for` 之前：kind 的名单有两份（`TEMPLATES` 在 `core/rules.py`、
        # `TABLE` 在这里），野 kind 原本会先在 `template_for` 那里抛出去——抛出去就是整趟崩，
        # 而这里的取向是「库里已经躺着的那种行不该把整趟拖崩」（与 `NotAdjustable`
        # 那一支同一个理由）。检查因此只做一次，做在最前面：过了它再去 `template_for`
        # 不会再抛，两张名单由 `test_the_table_covers_every_declared_kind` 钉成同一份。
        evaluate = TABLE.get(rule.kind)
        if evaluate is None:
            return self._skipped(rule, target.market, target.code, _no_impl(rule.kind))
        built = template_for(rule.kind)
        try:
            snapshot = self.data.read(target, as_of=day, position=position)
        except NotAdjustable as exc:
            # 「读不到这个对象」与「判不了」是同一类（`Skipped`），而不是整趟崩：
            # 只兜 `NotAdjustable` 这一种，不兜 `SamsError`——后者会把「取数链上有一个
            # 真 bug」也折成屏幕上的一句 skipped，而那一趟看起来是成功的。
            return self._skipped(
                rule, target.market, target.code, f"读不到这个对象：{exc}"
            )
        if quote is not None:
            snapshot = apply_quote(
                snapshot,
                quote=quote,
                previous=quotes.previous(target.market, target.code),
                zone=zone,
            )
        placed = move_prices(rule, built, snapshot)
        if placed.blocked:
            # 「搬不动就整条不判」（口径 1 写在 `anchor.py` 的开头）。一个对象可能同时缺
            # 两处（成本太老加线没有日期戳），而每一句后面跟着的是**它自己**那条下一步命令，
            # 所以全部带上，只并成一条结论：屏幕上分两行写会让读的人以为是两条规则
            return self._skipped(
                rule, snapshot.market, snapshot.code, "；".join(placed.blocked)
            )
        snapshot = placed.snapshot  # 从这一行起，`_one` 往后只认搬完的那一份
        verdict = self._verdict(rule, target, snapshot, evaluate, placed.params)
        if isinstance(verdict, Skipped):
            return verdict
        if verdict is None:
            return None  # 判过了，没到
        reason = run_filters(
            rule.filters,
            snapshot=snapshot,
            gate=self._gate(rule, target, snapshot, now, cache),
            verdict=verdict,
            built=built,
        )
        if reason:
            return self._suppressed(rule, snapshot, verdict, reason)
        cooled = self._cooldown(rule, snapshot, verdict, now)
        if cooled:
            return self._suppressed(rule, snapshot, verdict, cooled)
        if dry_run:
            return self._raised("", rule, snapshot, verdict, now)
        try:
            return self._dispatch(rule, snapshot, verdict, built, now, quote=quote)
        except SamsError as exc:
            # 端口后面是 `NotifyService.dispatch`：它会抛 `StorageError`（库写不进）与
            # `ValidationError`（用户不存在），两者都在 `SamsError` 底下。兜住它们意味着
            # 「这一条这一趟没有结论」（落进 skipped），而不是「这一趟结束了」。
            return self._skipped(rule, snapshot.market, snapshot.code, f"送不出去：{exc}")

    def _verdict(
        self, rule: Rule, target: Target, snapshot: Snapshot, evaluate, params: dict
    ) -> Verdict | Skipped | None:
        """求值。`evaluate` 是 `_one` 查过表的那一个函数：名单只在那里查一次，
        查不到就已经折成 `Skipped` 出门了（库里躺着野 kind 不该把整趟拖崩）。

        `thesis` 从引擎这里递进去而不是让模板自己去查库：`data.read` 已经查过一次，
        两次之间规则还能被改，那样算出来的价位与屏幕上那一条不是同一个数（`Ask` 的注释）。
        `params` 是搬过的那一份：模板自己去取 `rule.params` 是第二处没搬的数，
        除权日会算出一条比现价还高的旧线。
        """
        thesis = self.data.thesis_for(target)
        try:
            outcome = evaluate(Ask(snapshot=snapshot, params=params, thesis=thesis))
        except KeyError as exc:
            # 那一行是**上一个版本的模板**写的：老行要么缺了模板要的那个键，要么带着模板
            # 词表里已经没有了的取值（`stop_ma` 查 `_MA_LINES` 就是后面那种）。
            # `KeyError` 不是 `SamsError`，命令层那句 `except SamsError` 兜不住它——
            # 折成一句带名字的 skipped，屏幕上出现的是一句话而不是一段栈。
            # 名字取自 `exc.args[0]`：两种情况都是它，所以这里不说是「参数」还是「取值」。
            return self._skipped(
                rule,
                snapshot.market,
                snapshot.code,
                f"这条规则用不了 {exc.args[0]!r}：那一行是老模板写的，"
                "`sams rule edit` 改它（要哪几个、能取哪些值见 `sams rule kinds`）",
            )
        # 判「命中」看 `hit is not None` 而不是 `verdict.ok`：状态型命中（`stop.thesis_drift`
        # 那类只报状态、不比数值）的 `Verdict.ok` 是 False，用 `.ok` 会让那一类告警永不投出。
        # 那一类告警不读现价、也没有第二个数可比，退化在别处抓不到——钉它的是
        # `test_a_state_only_hit_still_reaches_the_sender`。
        if outcome.hit is not None:
            return outcome.hit
        if outcome.why_not:
            return self._skipped(rule, snapshot.market, snapshot.code, outcome.why_not)
        return None  # 判过了，没到

    # --- 闸门（§10.1）--------------------------------------------------------------------

    def _gate(
        self, rule: Rule, target: Target, snapshot: Snapshot, now: datetime, cache: _Cache
    ) -> Gate:
        """四开关的输入。本地日按**标的市场**的时区算：拿 UTC 日推美股时段恒错一天。"""
        day = self._local_day(target.market, now)
        if day is None:
            # 市场不在配置里、或那一行的时区名写坏了 ⇒ 三个要日历的开关都读不到输入
            return Gate(now=now)
        sessions = self._sessions(target.market, day, cache)
        open_alert = self.alerts.latest_open(rule.user_id, snapshot.market, snapshot.code)
        return Gate(
            now=now,
            sessions=sessions,
            last_session_on=self._last_session(target.market, day, cache),
            open_alert=(
                None
                if open_alert is None
                else OpenAlert(
                    open_alert.id, open_alert.stage, open_alert.triggered_at.date()
                )
            ),
        )

    def _zone(self, market: str) -> ZoneInfo | None:
        try:
            # 走 `MarketCfg.zone()` 而不是自己 `ZoneInfo(...)`：坏时区名抛的是
            # `ZoneInfoNotFoundError`（`KeyError` 的子类），这一支只接 `ValidationError`——
            # 那一头已经把两种错转译好了，配置写错不该崩整趟。
            return self.settings.market_cfg(market).zone()
        except ValidationError:
            return None

    def _stale_note(self, quote: Quote) -> str:
        """盘中那一道新鲜度闸（§5.2）：陈旧的报价宁可跳过，也不能拿来判止损。"""
        if quote.is_stale:
            return "报价源把这一笔标成了不可信（stale）：这一趟不判"
        age = (self.clock.now_utc() - quote.ts).total_seconds()
        if age > self.settings.quote_stale_s:
            return (
                f"报价距今 {int(age)} 秒，超过上限 {self.settings.quote_stale_s} 秒："
                "这一趟不判"
            )
        return ""

    def _local_day(self, market: str, now: datetime) -> date | None:
        zone = self._zone(market)
        return None if zone is None else local_day(now, zone)

    def _sessions(self, market: str, day: date, cache: _Cache) -> tuple:
        key = (market, day)
        if key not in cache.sessions:
            cache.sessions[key] = tuple(self.calendar.sessions(market, day))
        return cache.sessions[key]

    def _last_session(self, market: str, day: date, cache: _Cache) -> date | None:
        key = (market, day)
        if key in cache.last_session:
            return cache.last_session[key]
        found: date | None = None
        probe = day
        for _ in range(SESSION_LOOKBACK_DAYS):
            if self.calendar.is_trading_day(market, probe):
                found = probe
                break
            probe -= timedelta(days=1)
        cache.last_session[key] = found
        return found

    # --- 冷却（D94）---------------------------------------------------------------------

    def _cooldown(
        self, rule: Rule, snapshot: Snapshot, verdict: Verdict, now: datetime
    ) -> str:
        last = self.alerts.latest_for(
            rule.id, snapshot.market, snapshot.code, account_id=snapshot.account_id
        )
        if last is None:
            return ""
        try:
            louder = escalates(last.stage, verdict.stage)
        except ValidationError:
            # `alerts.stage` 那一列没有词表 CHECK，而写它的那一头（`monitor/router.py`）把
            # 文本原样写进去：入池那类历史行的 stage 是词表外的词。认不出上一档有多响
            # 就是判不出来，而这里的取向是判不出来⇒放行（同 `filters.no_open_alert`）：
            # 让它抛出去等于让一条脏历史把整趟求值打掉。
            return ""
        if louder:
            return ""  # 更响的那一档：止损真破了，降噪不能替它做决定
        gap = now - last.triggered_at
        window = timedelta(minutes=rule.cooldown_min)
        if gap >= window:
            return ""
        return (
            f"冷却：上一条 {last.stage} 推出于 {last.triggered_at:%m-%d %H:%M} UTC，"
            f"距今 {int(gap.total_seconds() // 60)} 分钟，窗口 {rule.cooldown_min} 分钟；"
            f"这一条 {verdict.stage} 不比它更响"
        )

    # --- 投递 ---------------------------------------------------------------------------

    def _dispatch(
        self, rule: Rule, snapshot: Snapshot, verdict: Verdict, built, now: datetime,
        *, quote: Quote | None = None,
    ) -> Raised:
        message = self._message(rule, snapshot, verdict, built, now, quote=quote)
        alert_id = self.sender.dispatch(
            message,
            user_id=rule.user_id,
            channel_ids=rule.channel_ids,
            rule_id=rule.id,
            account_id=snapshot.account_id,
        )
        self.bus.publish(
            RuleHit(
                rule_id=rule.id,
                user_id=rule.user_id,
                market=snapshot.market,
                code=snapshot.code,
                stage=verdict.stage,
                account_id=snapshot.account_id,
                payload={
                    "kind": rule.kind,
                    "value": verdict.value,
                    "threshold": verdict.threshold,
                    "value_text": verdict.value_text,
                    "threshold_text": verdict.threshold_text,
                    "moved": _moved_payload(snapshot),
                },
                ts=now,
            )
        )
        self.bus.publish(
            AlertTriggered(
                alert_id=alert_id, rule_id=rule.id, user_id=rule.user_id, ts=now
            )
        )
        return self._raised(alert_id, rule, snapshot, verdict, now)

    def _raised(
        self, alert_id: str, rule: Rule, snapshot: Snapshot, verdict: Verdict, now
    ) -> Raised:
        return Raised(
            alert_id=alert_id,
            rule_id=rule.id,
            rule_name=rule.name,
            user_id=rule.user_id,
            market=snapshot.market,
            code=snapshot.code,
            name=snapshot.name,
            account_id=snapshot.account_id,
            stage=verdict.stage,
            severity=rule.severity,
            value_text=verdict.value_text,
            threshold_text=verdict.threshold_text,
            action_note=action_for(rule.kind, rule.name),
        )

    def _skipped(self, rule: Rule, market: str, code: str, reason: str) -> Skipped:
        """「这一条这一趟没有结论」的共同形状：判不了、搬不动、送不出去，屏幕上同一摞。

        合成一摞是刻意的：多一摞就要多一份 CLI、多一份 JSON 字段、多一条退出码规则，
        而现在没人能回答「判不了」与「送不出去」该有什么不同的处置。分辨靠 `reason`。

        收 `market`/`code` 而不是 `snapshot`：`NotAdjustable` 那一头根本没有快照，而它
        与「缺参数」、「搬不动」在屏幕上必须是同一摞。
        """
        return Skipped(
            rule_id=rule.id,
            rule_name=rule.name,
            market=market,
            code=code,
            reason=reason,
        )

    def _suppressed(
        self, rule: Rule, snapshot: Snapshot, verdict: Verdict, reason: str
    ) -> Suppressed:
        return Suppressed(
            rule_id=rule.id,
            rule_name=rule.name,
            market=snapshot.market,
            code=snapshot.code,
            stage=verdict.stage,
            reason=reason,
        )

    def _message(
        self, rule: Rule, snapshot: Snapshot, verdict: Verdict, built, now: datetime,
        *, quote: Quote | None = None,
    ) -> AlertMessage:
        return AlertMessage(
            market=snapshot.market,
            code=snapshot.code,
            severity=rule.severity,
            triggered_at=now,
            name=snapshot.name,
            account_name=snapshot.account_name,
            rule_name=rule.name,
            value_text=verdict.value_text,
            threshold_text=verdict.threshold_text,
            action_note=action_for(rule.kind, rule.name),
            stage=verdict.stage,
            body=self._body(rule, built, snapshot, verdict, quote=quote),
            intraday=quote is not None,
            quote_ts=quote.ts if quote is not None else None,
            quote_source=quote.source if quote is not None else None,
        )

    def _body(
        self, rule: Rule, built, snapshot: Snapshot, verdict: Verdict,
        *, quote: Quote | None = None,
    ) -> str:
        """正文的四摞，顺序就是读的顺序：线在哪 → 这笔现在什么样 → 今天能不能执行 → 缺什么。

        「能不能执行」放在盈亏之后（§10.2 要求每条告警都带）：先让人知道这笔亏多少，
        再说这一笔今天动不了——反过来的话，读到第二句的人以为自己已经没有损失可言。

        「换算」紧跟判据：那一行说的是上一行用的那个数从哪儿来。放到盈亏之后，人是先读到
        结论、再读到出处，而出处那一句是要改变结论的（那条线其实比看上去低）。没搬的时候
        这里为空——不写「无除权」，那是一句没有人要的噪音。

        盘中那一趟的第二行是报价出处（D120）：几点的价、哪个源、延迟标没标。
        """
        lines = [f"{built.label}：{verdict.why}" if verdict.why else built.label]
        if quote is not None:
            lines.append(_quote_line(quote, self._zone(snapshot.market)))
        lines.extend(move.line for move in snapshot.moved)
        pnl = holding_pnl(snapshot, fx=self._fx(snapshot))
        if pnl:
            lines.append(pnl)
        lines.extend(constraint_lines(snapshot))
        if snapshot.missing:
            # 每条本身已经是一句完整的话（「没有过闸的现价：这一趟不判任何价格类条件」），
            # 再加一句「缺的输入：」是同一件事的两件外套。一条一行，读的人自己数得过来。
            lines.extend(snapshot.missing)
        lines.append(f"规则 `{rule.name}`（{rule.kind}）· 去重键 {rule.id}/{snapshot.market}/"
                     f"{snapshot.code}/{snapshot.account_id or '-'}")
        return "\n".join(lines)

    def _fx(self, snapshot: Snapshot) -> Fx | None:
        """汇总币种那一边的汇率。同币种不需要，跨币种读不到就说读不到（`holding_pnl` 那句）。"""
        base = snapshot.currency
        report = snapshot.report_currency
        if not base or not report or base == report:
            return None
        got = self.fx.latest(self.clock.today(), base, report)
        if got is None:
            return None
        return Fx(currency=got.quote, rate=got.rate, on=got.day, age_days=got.age_days)

    def _holdings(self, rule: Rule, cache: _Cache) -> dict[tuple[str, str], Position]:
        """一趟里同一个账户只读一次。

        读两次的代价不是性能：两次之间若有一笔成交进来，第二次拿到的成本与第一次不是
        同一个数，而告警里那两个数会同时出现（`tp.tiered` 用均价、`holding_pnl` 也用均价）。
        """
        account_id = rule.account_id
        if account_id is None:
            return {}
        if account_id not in cache.positions:
            cache.positions[account_id] = {
                (row.market, row.code): row for row in self.data.holdings(account_id)
            }
        return cache.positions[account_id]
