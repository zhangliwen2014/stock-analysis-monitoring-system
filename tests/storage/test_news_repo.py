"""0038 资讯表：同一篇重同步不新增，缺源 id 时指纹稳定，读按发布时刻倒序。

资讯的键是 `(source, item_id)`（D244）：同一篇文章出现在两个源算两条，跨源不合并。
`item_id` 是源自己的 id；源没给时按 `(title, published_at)` 取指纹，同一源内稳定——
否则每跑一次同步就多一条「看起来是新的旧闻」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
from support import FrozenClock

from sams.storage.repos.news import NewsRepo

T0 = datetime(2026, 10, 30, 1, 30, tzinfo=UTC)


def news(**over) -> dict:
    base = {
        "market": "A",
        "code": "600519",
        "source": "eastmoney_news",
        "item_id": "N1",
        "published_at": T0,
        "title": "贵州茅台：关于回购股份的公告",
        "summary": "公司拟以集中竞价方式回购股份",
        "url": "https://finance.eastmoney.com/a/N1.html",
    }
    base.update(over)
    return base


def frame(*rows: dict, **over) -> pl.DataFrame:
    if over:
        rows = (*rows, over)
    if not rows:
        rows = ({},)
    return pl.DataFrame([news(**row) for row in rows])


def repo(db, clock: FrozenClock | None = None) -> NewsRepo:
    return NewsRepo(db, clock=clock or FrozenClock(T0))


def test_news_upsert_is_idempotent_and_normalizes_the_key(db):
    subject = repo(db)
    assert subject.upsert_many(frame()) == 1
    assert subject.upsert_many(frame(market="a", code="sh600519")) == 0
    assert db.row_count("news_items") == 1

    got = subject.latest("A", "600519")
    assert got.height == 1
    assert got.row(0, named=True)["title"] == "贵州茅台：关于回购股份的公告"


def test_news_without_a_source_id_gets_a_stable_fingerprint(db):
    subject = repo(db)
    floaty = frame(item_id=None)
    subject.upsert_many(floaty)
    first = subject.latest("A", "600519").row(0, named=True)["item_id"]
    assert subject.upsert_many(floaty) == 0
    again = subject.latest("A", "600519").row(0, named=True)["item_id"]

    assert first
    assert again == first
    assert db.row_count("news_items") == 1


def test_news_latest_is_newest_first_and_limited(db):
    subject = repo(db)
    subject.upsert_many(
        frame(
            dict(item_id="N1", published_at=T0 - timedelta(days=2)),
            dict(item_id="N2", published_at=T0),
            dict(item_id="N3", published_at=T0 - timedelta(days=1)),
        )
    )
    rows = subject.latest("A", "600519", limit=2)
    assert rows["item_id"].to_list() == ["N2", "N3"]


def test_news_since_filters_by_ingest_time_not_by_publication(db):
    """关键词只对「入库之后」的新资讯生效，所以截止时间看的是 fetched_at。"""
    clock = FrozenClock(T0)
    subject = repo(db, clock)
    subject.upsert_many(frame(item_id="OLD", published_at=T0 - timedelta(days=90)))
    clock.advance(minutes=5)
    subject.upsert_many(frame(item_id="NEW", published_at=T0))

    rows = subject.since(T0 + timedelta(minutes=1))
    assert rows["item_id"].to_list() == ["NEW"]


def test_news_prune_drops_only_items_older_than_the_window(db):
    subject = repo(db)
    subject.upsert_many(
        frame(
            dict(item_id="ANCIENT", published_at=T0 - timedelta(days=401)),
            dict(item_id="FRESH", published_at=T0 - timedelta(days=3)),
        )
    )
    assert subject.prune(days=400) == 1
    assert subject.latest("A", "600519")["item_id"].to_list() == ["FRESH"]


def test_news_without_a_title_or_link_is_dropped(db):
    subject = repo(db)
    assert subject.upsert_many(frame(title="   ")) == 0
    assert subject.upsert_many(frame(item_id="N2", url=None)) == 0
    assert db.row_count("news_items") == 0
