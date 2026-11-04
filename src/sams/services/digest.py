"""每日收盘摘要 v0（S1-C）：只读已落库的告警与任务记录，不重新求值。

摘要是一份快照：同一 `(user, market, as_of)` 写一次就不再改，重跑返回已有行。
正文按「持仓 / 自选 / 观察」三栏组织，有告警但不在三栏里的标的进「其他」。
没有变化的日子也落行，好让「系统还活着」这件事在网页上看得到；推送则按心跳、
变化、周一与数据失败四条理由决定。日历能说的那一天是休市时一行都不落（D289）：
「收盘摘要」挂在周日身上就是一句假话，而这一趟的入口有可能是被强指定的 `as_of`。

投递口是一个 `Callable[[str], None]`（参数是摘要 id）：本层不认识通知队列，
队列版由 `bootstrap` 接线，避免 `services` import `monitor`。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.errors import SamsError, ValidationError
from sams.core.notify import FAILED, SEVERITY_LABELS, SYSTEM_MARKET
from sams.storage.repos.alert_mutes import MuteRepo
from sams.storage.repos.alerts import ACKNOWLEDGED, NEW, Alert, AlertRepo
from sams.storage.repos.digests import Digest, DigestRepo
from sams.storage.repos.ledger import SimAccountRepo, SimOrderRepo
from sams.storage.repos.news_keywords import NewsHit, NewsHitRepo
from sams.storage.repos.task_runs import TaskRunRepo
from sams.storage.repos.theses import OBSERVING, ThesisRepo
from sams.storage.repos.users import User, UserRepo
from sams.storage.repos.watchlists import WatchlistRepo

#: 前两周心跳：历史摘要少于这么多条时，每天推，哪怕正文是「今日无变化」
HEARTBEAT_DAYS = 10
#: 每个标的一天最多列几条告警，其余折成「另有 N 条」
MAX_LINES_PER_SYMBOL = 3
#: 每个标的最多列几条资讯：摘要只负责「今天有人提到它」，不是资讯流
MAX_NEWS_PER_SYMBOL = 2
#: 读一日告警的上限：它只是防御一条坏数据把内存拉爆的闸，正常一天远到不了
_ALERT_LIMIT = 500


@dataclass(frozen=True)
class DigestOutcome:
    digest: Digest | None
    created: bool
    pushed: bool
    #: 这一天被跳过的原因（日历说休市）；没跳过就是 `None`
    skipped_reason: str | None = None


@dataclass(frozen=True)
class _Sections:
    held: tuple[str, ...]
    watched: tuple[str, ...]
    observed: tuple[str, ...]

    def codes(self) -> set[str]:
        return set(self.held) | set(self.watched) | set(self.observed)


class DigestService:
    def __init__(
        self,
        *,
        settings,
        #: 只问「日历能不能回答这一天」：写摘要不该顺手去补日历（D289）
        calendar,
        users: UserRepo,
        watchlists: WatchlistRepo,
        theses: ThesisRepo,
        accounts: SimAccountRepo,
        orders: SimOrderRepo,
        alerts: AlertRepo,
        mutes: MuteRepo,
        digests: DigestRepo,
        task_runs: TaskRunRepo,
        news_hits: NewsHitRepo | None = None,
        #: 组合限额（S4-E §7）：给的是同一台 LimitService，摘要只读它的违规条目
        limits=None,
        send: Callable[[str], None] | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.calendar = calendar
        self.users = users
        self.watchlists = watchlists
        self.theses = theses
        self.accounts = accounts
        self.orders = orders
        self.alerts = alerts
        self.mutes = mutes
        self.digests = digests
        self.task_runs = task_runs
        self.news_hits = news_hits
        self.limits = limits
        self.send = send
        self.clock = clock

    def run(self, user: User, market: str, *, as_of: date) -> DigestOutcome:
        zone = self._zone(market)
        existing = self.digests.get(user.id, market, as_of)
        if existing is not None:
            return DigestOutcome(existing, created=False, pushed=False)

        # 休市日没有「收盘摘要」可写（D289）：调度的 `daily_due` 已经过了交易日闸，
        # 被强指定 `as_of` 跑那一趟才是漏口。日历答不上时照常写——
        # 「不知道」不能变成「摘要被静默吞掉」。
        if self.calendar.known_status(market, as_of) is False:
            return DigestOutcome(
                None,
                created=False,
                pushed=False,
                skipped_reason=f"{market} 在 {as_of.isoformat()} 休市，不写摘要",
            )

        sections = self._sections(user, market)
        if not sections.codes():
            return DigestOutcome(None, created=False, pushed=False)

        start, end = _local_day(zone, as_of)
        scopes = self._mute_scopes(user.id)
        rule_muted = {
            key for key, kinds in scopes.items() if kinds & {"all", "rule", "digest"}
        }
        news_muted = {
            key for key, kinds in scopes.items() if kinds & {"all", "news", "digest"}
        }
        screen_muted = {
            key for key, kinds in scopes.items() if kinds & {"all", "screen", "digest"}
        }
        today = self._alerts_between(user.id, market, start, end, rule_muted)
        news = self._news_between(user.id, market, start, end, news_muted)
        screen = self._screen_between(user.id, market, start, end, screen_muted)
        yesterday = self._open_before(user.id, market, start, rule_muted)
        failures = self.task_runs.failed_names_for(
            market, as_of.isoformat(), exclude=("digest",)
        )
        limit_lines = self._limit_lines(user, market)

        body = self._body(
            market=market,
            as_of=as_of,
            sections=sections,
            today=today,
            news=news,
            screen=screen,
            yesterday=yesterday,
            failures=failures,
            limit_lines=limit_lines,
        )
        heartbeat = self.digests.count_for(user.id, market) < HEARTBEAT_DAYS
        has_changes = bool(today) or bool(news) or bool(screen) or bool(limit_lines)
        should_push = (
            self.send is not None
            and (heartbeat or has_changes or as_of.weekday() == 0 or bool(failures))
        )
        title = f"{market} · {as_of.isoformat()} 收盘摘要"
        digest, created = self.digests.save(
            user_id=user.id,
            market=market,
            as_of=as_of,
            title=title,
            body=body,
            has_changes=has_changes,
            heartbeat=heartbeat,
            source_note=self._source_note(sections, failures),
        )
        if not created or not should_push:
            return DigestOutcome(digest, created=created, pushed=False)

        try:
            self.send(digest.id)
        except Exception as exc:  # noqa: BLE001 - 投递失败不能带走调度线程
            self.digests.mark_delivery(
                digest.id, FAILED, error=f"{type(exc).__name__}: {exc}"[:400]
            )
            fresh = self.digests.get_by_id(digest.id) or digest
            return DigestOutcome(fresh, created=True, pushed=False)
        return DigestOutcome(digest, created=True, pushed=True)

    def _zone(self, market: str) -> ZoneInfo:
        cfg = self.settings.markets.get(market)
        if cfg is None:
            raise ValidationError(f"未知市场 {market!r}：摘要按市场的本地日记账")
        return cfg.zone()

    def _sections(self, user: User, market: str) -> _Sections:
        held: list[str] = []
        for account in self.accounts.list_for(user.id):
            for position in self.orders.positions(account.id):
                if position.market == market and position.qty != 0 and position.code not in held:
                    held.append(position.code)
        held_set = set(held)
        watched = [
            item.code
            for item in self.watchlists.list(user.id)
            if item.market == market and item.code not in held_set
        ]
        watched_set = set(watched)
        observed = [
            thesis.code
            for thesis in self.theses.list(user.id, status=OBSERVING)
            if thesis.market == market
            and thesis.code not in held_set
            and thesis.code not in watched_set
        ]
        return _Sections(tuple(held), tuple(watched), tuple(observed))

    def _mute_scopes(self, user_id: str) -> dict[tuple[str, str], set[str]]:
        """一只票静了哪几类：`all` 已在仓库层覆盖，但摘要要按栏分开判（D249）。"""
        scopes: dict[tuple[str, str], set[str]] = {}
        for mute in self.mutes.list_for(user_id):
            scopes.setdefault((mute.market, mute.code), set()).add(mute.kind)
        return scopes

    def _alerts_between(
        self,
        user_id: str,
        market: str,
        start: datetime,
        end: datetime,
        muted: set[tuple[str, str]],
    ) -> dict[str, list[Alert]]:
        rows = self.alerts.list_for(
            user_id, market=market, since=start, until=end, limit=_ALERT_LIMIT
        )
        grouped: dict[str, list[Alert]] = {}
        for alert in rows:
            if not self._usable(alert, muted):
                continue
            grouped.setdefault(alert.code, []).append(alert)
        return grouped

    def _news_between(
        self,
        user_id: str,
        market: str,
        start: datetime,
        end: datetime,
        muted: set[tuple[str, str]],
    ) -> dict[str, list[NewsHit]]:
        """今天命中的资讯；`all`/`news`/`digest` 静音的标的都不进摘要（D249）。"""
        if self.news_hits is None:
            return {}
        grouped: dict[str, list[NewsHit]] = {}
        for hit in self.news_hits.list_window(user_id, since=start, until=end):
            if hit.market != market or (hit.market, hit.code) in muted:
                continue
            grouped.setdefault(hit.code, []).append(hit)
        return grouped

    def _open_before(
        self,
        user_id: str,
        market: str,
        start: datetime,
        muted: set[tuple[str, str]],
    ) -> dict[str, Alert]:
        rows = self.alerts.list_for(user_id, market=market, until=start, limit=_ALERT_LIMIT)
        now = self.clock.now_utc()
        latest: dict[str, Alert] = {}
        for alert in rows:
            if alert.status not in (NEW, ACKNOWLEDGED):
                continue
            if alert.deferred_until is not None and alert.deferred_until > now:
                continue
            if not self._usable(alert, muted):
                continue
            latest.setdefault(alert.code, alert)
        return latest

    @staticmethod
    def _usable(alert: Alert, muted: set[tuple[str, str]]) -> bool:
        return (
            bool(alert.code)
            and alert.market != SYSTEM_MARKET
            and not alert.is_test
            # 选股/资讯告警在摘要里有自己的归并方式（按方案、按资讯），
            # 再按「一只票一行」摊开就是同一件事说两遍（S4-B）
            and _kind(alert) not in {"screen", "news"}
            and (alert.market, alert.code) not in muted
        )

    def _screen_between(
        self,
        user_id: str,
        market: str,
        start: datetime,
        end: datetime,
        muted: set[tuple[str, str]],
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        """今晚的方案新命中：按方案归并，只留代码——摘要说的是「哪个方案放了什么进来」。

        `screen`/`all`/`digest` 静音的票不进这一节；没有渠道时告警仍落库，
        所以「只进告警中心」的方案一样会在摘要里出现。
        """
        rows = self.alerts.list_for(
            user_id, market=market, since=start, until=end, limit=_ALERT_LIMIT
        )
        grouped: dict[tuple[str, str], set[str]] = {}
        for alert in rows:
            if (
                not alert.code
                or alert.is_test
                or _kind(alert) != "screen"
                or (alert.market, alert.code) in muted
            ):
                continue
            payload = alert.payload or {}
            key = (
                str(payload.get("preset_id") or ""),
                str(payload.get("preset_name") or "") or alert.rule_name or "未命名方案",
            )
            grouped.setdefault(key, set()).add(alert.code)
        return {
            key: tuple(sorted(codes))
            for key, codes in sorted(grouped.items(), key=lambda item: item[0][1])
        }

    def _limit_lines(self, user: User, market: str) -> tuple[str, ...]:
        """摘要尾行的违规条目（S4-E §7）。

        限额自己不推送（权重日常波动会刷屏），但它在这一份摘要里必须说：
        「今天有一条线被踩了」是当天最该被看见的变化之一。判不出来的原因也印出来，
        不把一次算不出折成「没有违规」。
        """
        if self.limits is None:
            return ()
        try:
            return self.limits.breach_lines(user.username, market)
        except SamsError as exc:
            return (f"限额未判：{exc}",)

    def _body(
        self,
        *,
        market: str,
        as_of: date,
        sections: _Sections,
        today: dict[str, list[Alert]],
        news: dict[str, list[NewsHit]],
        screen: dict[tuple[str, str], tuple[str, ...]],
        yesterday: dict[str, Alert],
        failures: tuple[str, ...],
        limit_lines: tuple[str, ...] = (),
    ) -> str:
        lines = [f"{market} · {as_of.isoformat()} 收盘摘要", ""]
        empty = (
            "· 数据未更新，无法确认今天的变化"
            if failures
            else "· 无变化"
        )
        for heading, codes in (
            ("【持仓】", sections.held),
            ("【自选】", sections.watched),
            ("【观察】", sections.observed),
        ):
            if not codes:
                continue
            lines.append(heading)
            wrote = False
            for code in codes:
                wrote = (
                    self._append_symbol(
                        lines, code, today.get(code, ()), yesterday.get(code)
                    )
                    or wrote
                )
            wrote = self._append_news(lines, codes, news) or wrote
            if not wrote:
                lines.append(empty)
            lines.append("")

        others = self._other_codes(sections, today, yesterday, news)
        if others:
            lines.append("【其他】")
            for code in others:
                self._append_symbol(lines, code, today.get(code, ()), yesterday.get(code))
            self._append_news(lines, others, news)
            lines.append("")

        # 选股方案单列一节：同一晚命中几十只时，按「一只票一行」摊开会把摘要
        # 变成第二份选股结果；这里只答「哪个方案今晚放了什么进来」（S4-B）
        if screen:
            lines.append("【选股方案】")
            for (_, name), codes in screen.items():
                lines.append(
                    f"· 方案《{name}》新命中 {len(codes)} 只（{'、'.join(codes)}）"
                )
            lines.append("")

        if failures:
            lines.append(
                f"数据未更新（{'、'.join(failures)} 失败）：今天的变化可能不全"
            )
        else:
            lines.append(self._source_note(sections, failures))

        # 限额放最后：它是一条横跨持仓的结论，不是某只票今天的动静。
        # 只列被踩红的线；参考线（灰色）不进摘要，否则每天都会有一条「参考」噪声
        if limit_lines:
            lines.append("【限额】")
            for line in limit_lines:
                lines.append(f"· {line}")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _other_codes(
        sections: _Sections,
        today: dict[str, list[Alert]],
        yesterday: dict[str, Alert],
        news: dict[str, list[NewsHit]],
    ) -> list[str]:
        known = sections.codes()
        ordered: list[str] = []
        for source in (today, yesterday, news):
            for code in source:
                if code not in known and code not in ordered:
                    ordered.append(code)
        return ordered

    def _append_news(
        self,
        lines: list[str],
        codes: Sequence[str],
        news: dict[str, list[NewsHit]],
    ) -> bool:
        wrote = False
        for code in codes:
            for line in self._news_lines(code, news.get(code, ())):
                lines.append(line)
                wrote = True
        return wrote

    @staticmethod
    def _news_lines(code: str, hits: Sequence[NewsHit]) -> list[str]:
        """同一篇资讯命中多个词只印一行，词面并列——摘要不替用户数命中次数。"""
        merged: dict[tuple[str, str], tuple[str, str, list[str]]] = {}
        for hit in hits:
            key = (hit.source, hit.item_id)
            if key not in merged:
                merged[key] = (hit.title, hit.url, [])
            words = merged[key][2]
            if hit.keyword not in words:
                words.append(hit.keyword)
        return [
            f"· {code}：资讯「{title}」（关键词：{'、'.join(words)}）— {url}"
            for title, url, words in list(merged.values())[:MAX_NEWS_PER_SYMBOL]
        ]

    def _append_symbol(
        self,
        lines: list[str],
        code: str,
        alerts: Sequence[Alert],
        yesterday: Alert | None,
    ) -> bool:
        base = self.settings.server.public_base_url
        wrote = False
        for alert in alerts[:MAX_LINES_PER_SYMBOL]:
            lines.append(self._line(code, alert, base, prefix=""))
            wrote = True
        if len(alerts) > MAX_LINES_PER_SYMBOL:
            name = alerts[0].symbol_name or code
            lines.append(f"· {code} {name}：另有 {len(alerts) - MAX_LINES_PER_SYMBOL} 条")
            wrote = True
        if yesterday is not None:
            lines.append(self._line(code, yesterday, base, prefix="昨日未处置 "))
            wrote = True
        return wrote

    @staticmethod
    def _line(code: str, alert: Alert, base: str, *, prefix: str) -> str:
        level = SEVERITY_LABELS.get(alert.severity, alert.severity)
        name = alert.symbol_name or code
        rule = alert.rule_name or "未命名规则"
        value = alert.payload.get("value_text", "") if alert.payload else ""
        threshold = alert.payload.get("threshold_text", "") if alert.payload else ""
        return (
            f"· {code} {name}：{prefix}{level}「{rule}」"
            f"({value} / {threshold})— {base}/alerts/{alert.id}"
        )

    @staticmethod
    def _source_note(sections: _Sections, failures: tuple[str, ...]) -> str:
        coverage = (
            f"覆盖 持仓 {len(sections.held)}、自选 {len(sections.watched)}、"
            f"观察 {len(sections.observed)}"
        )
        if failures:
            return f"数据：未更新（{'、'.join(failures)}）；{coverage}。"
        return f"数据：盘后任务无失败；{coverage}。"


def _kind(alert: Alert) -> str:
    """告警的类别标记（`payload.kind`）：价格规则告警没有这一格，返回空串。"""
    return str((alert.payload or {}).get("kind") or "")


def _local_day(zone: ZoneInfo, as_of: date) -> tuple[datetime, datetime]:
    start = datetime.combine(as_of, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end
