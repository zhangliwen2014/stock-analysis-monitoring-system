"""统一资讯视图（S3-D）：公告与新闻同一条时间线，命中词随行带出。

页面不该自己拼两张表：`feed` 只按「哪一天、哪只票、什么标题、来源、原文」回答，
排序与合并都在这一层定死，`/news` 与个股卡用的是同一份。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
from support import FrozenClock

from sams.datasource.f10 import empty_announcements
from sams.datasource.news import empty_news
from sams.datasource.registry import AdapterRegistry
from sams.services.news import NewsService
from sams.storage.repos.f10 import AnnouncementRepo
from sams.storage.repos.news import NewsRepo
from sams.storage.repos.news_keywords import NewsHitRepo, NewsKeywordRepo

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
#: 写入时刻比查询截止早一分钟：窗口是 `[since, until)`，边界那一刻不属于任何人
AT = NOW - timedelta(minutes=1)
TODAY = date(2026, 10, 30)


def service(db) -> NewsService:
    clock = FrozenClock(NOW)
    registry = AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _s: None)
    return NewsService(
        registry,
        NewsRepo(db, clock=clock),
        announcements=AnnouncementRepo(db),
        keywords=NewsKeywordRepo(db, clock=clock),
        hits=NewsHitRepo(db, clock=clock),
        clock=clock,
    )


def news_item(db, code="600519", item_id="N1", *, title=None, at=AT, market="A"):
    NewsRepo(db).upsert_many(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "source": "eastmoney_f10",
                    "item_id": item_id,
                    "published_at": at,
                    "title": title or f"标题 {item_id}",
                    "summary": None,
                    "url": f"https://example.test/{item_id}",
                }
            ],
            schema=dict(empty_news().schema),
        )
    )


def announcement(db, code="600519", art_code="AN1", *, title=None, on=TODAY, market="A"):
    AnnouncementRepo(db).upsert(
        pl.DataFrame(
            [
                {
                    "market": market,
                    "code": code,
                    "art_code": art_code,
                    "published_on": on,
                    "title": title or f"公告 {art_code}",
                    "category": None,
                    "url": f"https://example.test/{art_code}.html",
                    "source": "eastmoney_f10",
                }
            ],
            schema=dict(empty_announcements().schema),
        )
    )


def hit(subject: NewsService, db, item_id, *, code="600519", market="A", keyword="回购"):
    rule = subject.keywords.add("u1", market, keyword, code=code)
    subject.hits.record("u1", "eastmoney_f10", item_id, rule.id, hit_at=AT)


def test_announcements_and_news_share_one_line_sorted_by_time(db):
    announcement(db, title="季度报告", on=date(2026, 10, 29))
    news_item(db, item_id="N1", title="回购进展", at=AT)

    items = service(db).feed("u1", "A", ["600519"])

    assert [item.title for item in items] == ["回购进展", "季度报告"]
    assert [item.kind for item in items] == ["news", "announcement"]
    assert items[0].url == "https://example.test/N1"
    assert items[1].url == "https://example.test/AN1.html"
    assert items[1].item_id == "AN1"


def test_the_feed_only_contains_the_requested_market_and_codes(db):
    news_item(db, "600519", "N1")
    news_item(db, "000001", "N2", title="别人的")
    news_item(db, "00700", "N3", title="港股", market="HK")

    items = service(db).feed("u1", "A", ["600519"])

    assert [item.title for item in items] == ["标题 N1"]


def test_hit_words_ride_along_and_keyword_only_keeps_hits(db):
    news_item(db, item_id="N1", title="公司拟回购股份")
    news_item(db, item_id="N2", title="日常经营公告")
    subject = service(db)
    hit(subject, db, "N1")

    everything = subject.feed("u1", "A", ["600519"])
    only_hits = subject.feed("u1", "A", ["600519"], keyword_only=True)

    assert [item.matched for item in everything] == [("回购",), ()]
    assert [item.title for item in only_hits] == ["公司拟回购股份"]
    assert only_hits[0].matched == ("回购",)


def test_two_keywords_on_one_item_keep_both_words(db):
    news_item(db, item_id="N1", title="回购与减持")
    subject = service(db)
    hit(subject, db, "N1", keyword="回购")
    hit(subject, db, "N1", keyword="减持")

    item = subject.feed("u1", "A", ["600519"])[0]

    assert set(item.matched) == {"回购", "减持"}


def test_the_limit_applies_after_merging_the_two_sources(db):
    for index in range(3):
        announcement(db, art_code=f"AN{index}", on=date(2026, 10, 20 + index))
        news_item(db, item_id=f"N{index}", at=datetime(2026, 10, 21 + index, tzinfo=UTC))

    items = service(db).feed("u1", "A", ["600519"], limit=4)

    assert len(items) == 4
    # 合并后取最近 4 条：最新的新闻在最前，最老的那条公告被截掉
    assert items[0].item_id == "N2"
    assert "AN0" not in [item.item_id for item in items]


def test_a_service_without_an_announcement_repo_still_feed_news(db):
    news_item(db, item_id="N1")
    subject = NewsService(
        AdapterRegistry(adapters={}, market_sources={}, sleep=lambda _s: None),
        NewsRepo(db, clock=FrozenClock(NOW)),
        clock=FrozenClock(NOW),
    )

    items = subject.feed("u1", "A", ["600519"])

    assert [item.kind for item in items] == ["news"]


def test_since_keeps_the_feed_window(db):
    news_item(db, item_id="old", at=NOW - timedelta(days=40))
    news_item(db, item_id="new", at=NOW - timedelta(days=1))

    items = service(db).feed("u1", "A", ["600519"], since=NOW - timedelta(days=30))

    assert [item.item_id for item in items] == ["new"]
