"""公告与新闻的统一视图：一条时间线，公告和新闻混排，最新的在前。

读模型只做「合并与排序」，不跨源去重、不改写时间：同一篇文章在公告与新闻两边
各出现一次，是两件可以分别核对的事（D244）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sams.core.news import InfoItem, merge_info_items

T0 = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)


def item(kind: str, *, at: datetime, source: str = "eastmoney") -> InfoItem:
    return InfoItem(
        market="A",
        code="600519",
        kind=kind,
        title=f"{kind} 标题",
        summary=None,
        published_at=at,
        source=source,
        url=f"https://example.com/{kind}",
        as_of=T0,
    )


def test_merged_feed_is_newest_first():
    rows = merge_info_items(
        [
            item("announcement", at=T0 - timedelta(days=2)),
            item("news", at=T0),
            item("announcement", at=T0 - timedelta(hours=1)),
        ]
    )
    assert [row.published_at for row in rows] == [
        T0,
        T0 - timedelta(hours=1),
        T0 - timedelta(days=2),
    ]


def test_announcement_wins_the_same_timestamp():
    rows = merge_info_items(
        [
            item("news", at=T0, source="eastmoney_news"),
            item("announcement", at=T0, source="eastmoney_f10"),
        ]
    )
    assert [row.kind for row in rows] == ["announcement", "news"]


def test_limit_keeps_the_newest_rows():
    rows = merge_info_items(
        [item("news", at=T0 - timedelta(days=day)) for day in range(5)],
        limit=2,
    )
    assert [row.published_at for row in rows] == [T0, T0 - timedelta(days=1)]
