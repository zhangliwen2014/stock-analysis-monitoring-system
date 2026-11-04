"""资讯同步用例（S3-B）：链答复 → 幂等入库 → 把「新增几行」带回调用方。

v0 只接 A 股（D247）。港美没有新闻源时不在这里假装 `0 行`：那会让「今晚没有新闻」
与「这个市场根本没接源」看起来一样。市场白名单与 job 的跳过文案都从这里来。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import polars as pl

from sams.core.clock import SYSTEM_CLOCK, Clock
from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import ValidationError
from sams.core.news import InfoItem, merge_info_items
from sams.core.notify import MID, AlertMessage, AlertSender
from sams.datasource.registry import AdapterRegistry, Attempt
from sams.storage.repos.alert_mutes import MuteRepo
from sams.storage.repos.f10 import AnnouncementRepo
from sams.storage.repos.news import NewsRepo
from sams.storage.repos.news_keywords import (
    NewsHitRepo,
    NewsKeyword,
    NewsKeywordRepo,
)

#: 新闻 v0 接入的市场（D247）：HK/US 无源，页面与 job 都写「未接入」
NEWS_MARKETS = frozenset({"A"})
#: 盘后 job 与 CLI 的默认窗口：与公告同一量级，一次取源的第一页
NEWS_WINDOW_DAYS = 30
#: 资讯告警的规则名：告警列表按它显示「资讯」，payload 里另有 `kind=news`
NEWS_RULE_NAME = "资讯"


@dataclass(frozen=True)
class NewsSyncSummary:
    market: str
    code: str
    kind: str = "个股新闻"
    source: str = ""
    fetched: int = 0
    #: 真正新增的行数；重跑同一晚是 0
    written: int = 0
    degraded: bool = False
    empty: bool = False
    skipped: tuple[tuple[str, str], ...] = ()
    attempts: tuple[Attempt, ...] = ()
    confirmed_empty_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewsMatchSummary:
    #: 资讯 × 关键词 的命中对（含已经命过、这一趟不再推的）
    matched: int = 0
    #: 这一趟新落下的命中行；重跑同一批资讯是 0
    written: int = 0
    #: 真正推出去的告警条数（一条资讯命中多个词也只算一条）
    alerts: int = 0
    #: 命中但被静音挡下的资讯条数：命中照记，只是不发
    muted: int = 0


class NewsService:
    def __init__(
        self,
        registry: AdapterRegistry,
        news: NewsRepo,
        *,
        announcements: AnnouncementRepo | None = None,
        keywords: NewsKeywordRepo | None = None,
        hits: NewsHitRepo | None = None,
        mutes: MuteRepo | None = None,
        sender: AlertSender | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.registry = registry
        self.news = news
        self.announcements = announcements
        self.keywords = keywords
        self.hits = hits
        self.mutes = mutes
        self.sender = sender
        self.clock = clock

    def sync(
        self,
        market: str,
        code: str,
        start: date | None = None,
        end: date | None = None,
    ) -> NewsSyncSummary:
        market = canonical_market(market)
        code = canonical_code(market, code)
        if market not in NEWS_MARKETS:
            raise ValidationError(f"{market} 新闻源未接入（v0 只接 A 股）")
        end = end or self.clock.today()
        start = start or end - timedelta(days=NEWS_WINDOW_DAYS)
        if start > end:
            raise ValidationError(f"start {start} 在 end {end} 之后：区间反了")

        result = self.registry.fetch_stock_news(market, code, start, end)
        common = dict(
            market=market,
            code=code,
            source=result.source,
            fetched=result.frame.height,
            degraded=result.degraded,
            skipped=result.skipped,
            attempts=result.attempts,
            confirmed_empty_by=result.confirmed_empty_by,
        )
        if result.frame.is_empty():
            return NewsSyncSummary(empty=True, **common)
        written, fresh = self.news.upsert_new(result.frame)
        self.match_new(fresh)
        return NewsSyncSummary(written=written, **common)

    def feed(
        self,
        user_id: str,
        market: str,
        codes: Sequence[str],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        keyword_only: bool = False,
    ) -> list[InfoItem]:
        """公告与新闻合并成一条时间线；`keyword_only` 只留这个用户命中过的新闻。

        公告没有「新闻 id」也不参与关键词匹配：它进同一条线是因为用户问的是
        「这只票最近发生了什么」，不是「哪个源又更新了」。
        """
        market = canonical_market(market)
        ordered: list[str] = []
        for code in codes:
            canonical = canonical_code(market, code)
            if canonical not in ordered:
                ordered.append(canonical)
        if not ordered:
            return []
        end = until or self.clock.now_utc()
        marks = self._marks(user_id, since=since, until=end)
        items = [
            InfoItem(
                market=str(row["market"]),
                code=str(row["code"]),
                kind="news",
                title=str(row["title"]),
                summary=None if row["summary"] is None else str(row["summary"]),
                published_at=row["published_at"],
                source=str(row["source"]),
                url=str(row["url"]),
                as_of=row["fetched_at"],
                item_id=str(row["item_id"]),
                matched=marks.get((str(row["source"]), str(row["item_id"])), ()),
            )
            for row in self.news.recent(
                market, ordered, since=since, until=end, limit=limit
            ).to_dicts()
        ]
        if self.announcements is not None:
            for row in self.announcements.recent(
                market,
                ordered,
                since=None if since is None else since.date(),
                through=None if until is None else until.date(),
                limit=limit,
            ).to_dicts():
                items.append(
                    InfoItem(
                        market=str(row["market"]),
                        code=str(row["code"]),
                        kind="announcement",
                        title=str(row["title"]),
                        summary=None,
                        published_at=datetime.combine(
                            row["published_on"], time.min, tzinfo=UTC
                        ),
                        source=str(row["source"]),
                        url=str(row["url"]),
                        as_of=row["fetched_at"],
                        item_id=str(row["art_code"]),
                    )
                )
        merged = merge_info_items(items, limit=limit)
        return [item for item in merged if item.matched] if keyword_only else merged

    def _marks(
        self, user_id: str, *, since: datetime | None, until: datetime | None
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        if self.hits is None:
            return {}
        marks: dict[tuple[str, str], list[str]] = {}
        for hit in self.hits.list_window(user_id, since=since, until=until):
            words = marks.setdefault((hit.source, hit.item_id), [])
            if hit.keyword not in words:
                words.append(hit.keyword)
        return {key: tuple(words) for key, words in marks.items()}

    def match_new(self, frame: pl.DataFrame) -> NewsMatchSummary:
        """只匹配**这一趟新入库**的资讯（D245）。

        对每个有启用关键词的用户分别跑一遍：关键词是用户级的事实，命中与推送
        也只会写到那个用户名下。没有关键词、没有命中的日子返回空摘要，不是错误。
        """
        if self.keywords is None or self.hits is None or frame.is_empty():
            return NewsMatchSummary()
        now = self.clock.now_utc()
        rows = frame.to_dicts()
        items = [
            InfoItem(
                market=str(row["market"]),
                code=str(row["code"]),
                kind="news",
                title=str(row["title"]),
                summary=None if row.get("summary") is None else str(row["summary"]),
                published_at=row["published_at"],
                source=str(row["source"]),
                url=str(row["url"]),
                as_of=row.get("fetched_at"),
                item_id=str(row["item_id"]),
            )
            for row in rows
        ]
        total = NewsMatchSummary()
        for market in sorted({item.market for item in items}):
            for user_id in self.keywords.users(market):
                total = _add(total, self.match(user_id, items, now=now))
        return total

    def match(
        self,
        user_id: str,
        items: Sequence[InfoItem],
        *,
        now: datetime | None = None,
    ) -> NewsMatchSummary:
        """把一批资讯与这个用户的关键词对上，写命中、按需推送。

        同一资讯×关键词只落一行：命中已存在时这一对不再推送，但同一趟里新命中的
        其他词仍会触发**一条合并推送**——一条资讯在一个群里刷两遍标题是噪音。
        只认 `kind=news` 且有源 id 的条目：没有 id 就没有去重键，宁可漏提醒也不刷屏。
        """
        if self.keywords is None or self.hits is None or not items:
            return NewsMatchSummary()
        rules = self.keywords.list_for(user_id, enabled_only=True)
        if not rules:
            return NewsMatchSummary()
        moment = now or self.clock.now_utc()
        matched = written = alerts = muted = 0
        for item in items:
            if item.kind != "news" or not item.source or not item.item_id:
                continue
            found = [rule for rule in rules if _matches(rule, item)]
            if not found:
                continue
            matched += len(found)
            fresh = [
                rule
                for rule in found
                if self.hits.record(
                    user_id, item.source, item.item_id, rule.id, hit_at=moment
                )
            ]
            if not fresh:
                continue
            written += len(fresh)
            if self.mutes is not None and self.mutes.get(
                user_id, item.market, item.code, kind="news"
            ) is not None:
                muted += 1
                continue
            channels = _unique(
                channel for rule in fresh for channel in rule.channel_ids
            )
            if not channels or self.sender is None:
                continue
            self._dispatch(user_id, item, found, channels, moment)
            alerts += 1
        return NewsMatchSummary(
            matched=matched, written=written, alerts=alerts, muted=muted
        )

    def _dispatch(
        self,
        user_id: str,
        item: InfoItem,
        hits: Sequence[NewsKeyword],
        channels: Sequence[str],
        moment: datetime,
    ) -> None:
        labels = _unique(rule.keyword for rule in hits)
        as_of = item.as_of or moment
        body = "\n".join(
            part
            for part in (
                f"命中关键词：{'、'.join(labels)}",
                (item.summary or "").strip(),
                item.url,
            )
            if part
        )
        message = AlertMessage(
            market=item.market,
            code=item.code,
            severity=MID,
            triggered_at=moment,
            rule_name=NEWS_RULE_NAME,
            value_text=item.title,
            threshold_text=f"关键词：{'、'.join(labels)}",
            action_note="点开原文核对；这不是买卖建议",
            stage="hit",
            body=body,
            facts={
                "kind": "news",
                "title": item.title,
                "url": item.url,
                "keyword": "、".join(labels),
                "published_at": item.published_at.isoformat(),
                "source": item.source,
                "as_of": as_of.isoformat(),
            },
        )
        assert self.sender is not None  # 调用方已经挡过一次
        self.sender.dispatch(
            message, user_id=user_id, channel_ids=list(channels), rule_id=None
        )


def _matches(rule: NewsKeyword, item: InfoItem) -> bool:
    """纯文本包含匹配，大小写不敏感（D245）：不做分词、正则与布尔表达式。"""
    if rule.market != item.market:
        return False
    if rule.code is not None and rule.code != item.code:
        return False
    needle = rule.keyword.casefold()
    haystack = f"{item.title}\n{item.summary or ''}".casefold()
    return needle in haystack


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _add(left: NewsMatchSummary, right: NewsMatchSummary) -> NewsMatchSummary:
    return NewsMatchSummary(
        matched=left.matched + right.matched,
        written=left.written + right.written,
        alerts=left.alerts + right.alerts,
        muted=left.muted + right.muted,
    )
