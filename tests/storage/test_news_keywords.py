"""0039 关键词与命中：作用域、大小写去重、命中一次只落一行。

关键词是用户级、市场级、可选代码级（D245）。`code` 为 NULL 表示该市场全部
自选/持仓/观察；作用域不同的同名关键词是两件事，不能互相挡住。
`news_hits` 的主键负责「同一资讯×关键词只告警一次」（D246）：重跑是幂等的。
"""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import Conflict, ValidationError
from sams.storage.repos.news import NewsRepo
from sams.storage.repos.news_keywords import NewsHitRepo, NewsKeywordRepo

T0 = datetime(2026, 10, 30, 1, 30, tzinfo=UTC)


def keywords(db, clock: FrozenClock | None = None) -> NewsKeywordRepo:
    return NewsKeywordRepo(db, clock=clock or FrozenClock(T0))


def hits(db, clock: FrozenClock | None = None) -> NewsHitRepo:
    return NewsHitRepo(db, clock=clock or FrozenClock(T0))


def test_keyword_round_trips_with_scope_and_channels(db):
    repo = keywords(db)
    made = repo.add("u1", "A", "回购", code="600519", channel_ids=["c1", "c2"])
    assert made.code == "600519"
    assert made.channel_ids == ("c1", "c2")
    assert made.enabled is True

    got = repo.list_for("u1")
    assert [(row.keyword, row.code) for row in got] == [("回购", "600519")]


def test_a_market_wide_keyword_keeps_a_null_code(db):
    repo = keywords(db)
    made = repo.add("u1", "A", "减持")
    assert made.code is None
    assert repo.list_for("u1")[0].code is None


def test_the_same_word_under_the_same_scope_is_rejected_case_insensitively(db):
    repo = keywords(db)
    repo.add("u1", "A", "Buyback", code="600519")
    with pytest.raises(Conflict, match="已经"):
        repo.add("u1", "A", "buyback", code="600519")


def test_the_same_word_in_another_scope_is_a_different_keyword(db):
    repo = keywords(db)
    repo.add("u1", "A", "回购", code="600519")
    repo.add("u1", "A", "回购", code="000001")
    repo.add("u1", "A", "回购")
    assert len(repo.list_for("u1")) == 3


def test_a_keyword_must_be_one_to_forty_characters(db):
    repo = keywords(db)
    for bad in ("", "   ", "字" * 41):
        with pytest.raises(ValidationError, match="1–40"):
            repo.add("u1", "A", bad)


def test_remove_only_touches_your_own_keyword(db):
    repo = keywords(db)
    mine = repo.add("u1", "A", "回购")
    theirs = repo.add("u2", "A", "回购")
    assert repo.remove(mine.id, "u2") is False
    assert repo.remove(mine.id, "u1") is True
    assert [row.id for row in repo.list_for("u1")] == []
    assert [row.id for row in repo.list_for("u2")] == [theirs.id]


def test_enabled_only_skips_switched_off_rows(db):
    repo = keywords(db)
    on = repo.add("u1", "A", "回购")
    repo.add("u1", "A", "减持", enabled=False)
    assert [row.id for row in repo.list_for("u1", enabled_only=True)] == [on.id]
    assert len(repo.list_for("u1")) == 2


def test_a_hit_is_recorded_at_most_once(db):
    subject = hits(db)
    assert subject.record("u1", "src", "N1", "k1") is True
    assert subject.record("u1", "src", "N1", "k1") is False
    assert subject.record("u1", "src", "N1", "k2") is True
    assert subject.record("u2", "src", "N1", "k1") is True


def test_the_window_read_joins_the_news_row_and_the_keyword(db):
    keyword = keywords(db).add("u1", "A", "回购", code="600519")
    NewsRepo(db).upsert_many(
        pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": "600519",
                    "source": "eastmoney_f10",
                    "item_id": "N1",
                    "published_at": T0,
                    "title": "贵州茅台：关于回购股份的公告",
                    "summary": "公司拟回购",
                    "url": "https://example.com/N1",
                }
            ]
        )
    )
    subject = hits(db)
    subject.record("u1", "eastmoney_f10", "N1", keyword.id, hit_at=T0)

    got = subject.list_window(
        "u1", since=T0.replace(hour=0), until=T0.replace(hour=23, minute=59)
    )
    assert len(got) == 1
    row = got[0]
    assert row.title == "贵州茅台：关于回购股份的公告"
    assert row.url == "https://example.com/N1"
    assert row.keyword == "回购"
    assert row.code == "600519"
