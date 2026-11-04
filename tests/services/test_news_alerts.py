"""关键词命中与资讯告警（S3-C，D245/D246）。

四条不变式：
1. 同一 `(资讯, 关键词)` 只落一行命中，也只推一次；
2. 一条资讯命中多个词时，命中行各留一条、推送合并成一条，渠道取并集；
3. 关键词只对**入库之后**的新资讯生效——加词之后重同步同一篇不会再提醒；
4. 静音与「只进摘要」都不推送，但命中照记，用户能在页面上看到它命中过。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
from support import FrozenClock

from sams.core.news import InfoItem
from sams.datasource.news import empty_news
from sams.datasource.registry import AdapterRegistry
from sams.services.news import NewsService
from sams.storage.repos.alert_mutes import MuteRepo
from sams.storage.repos.news import NewsRepo
from sams.storage.repos.news_keywords import NewsHitRepo, NewsKeywordRepo

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 31)
STAMP = datetime(2026, 10, 30, 10, 36, 28, tzinfo=UTC)


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch(
        self,
        message,
        *,
        user_id: str,
        channel_ids=(),
        rule_id=None,
        account_id=None,
        is_test=False,
    ) -> str:
        self.calls.append(
            {
                "message": message,
                "user_id": user_id,
                "channel_ids": tuple(channel_ids),
                "rule_id": rule_id,
                "account_id": account_id,
                "is_test": is_test,
            }
        )
        return f"alert-{len(self.calls)}"


class StubNewsAdapter:
    name = "eastmoney_f10"
    markets = frozenset({"A"})

    def stock_news(self, market, code, start, end):
        return pl.DataFrame(
            [
                {
                    "market": "A",
                    "code": code,
                    "source": self.name,
                    "item_id": "N1",
                    "published_at": STAMP,
                    "title": "贵州茅台：关于回购股份的公告",
                    "summary": "公司拟以集中竞价方式减持已回购股份",
                    "url": "https://example.test/N1",
                }
            ],
            schema=dict(empty_news().schema),
        )


def item(**over) -> InfoItem:
    base = {
        "market": "A",
        "code": "600519",
        "kind": "news",
        "title": "贵州茅台：关于回购股份的公告",
        "summary": "公司拟以集中竞价方式回购股份",
        "published_at": STAMP,
        "source": "eastmoney_f10",
        "url": "https://example.test/N1",
        "as_of": NOW,
        "item_id": "N1",
    }
    base.update(over)
    return InfoItem(**base)


def service(db, sender=None, *, adapter=None) -> NewsService:
    clock = FrozenClock(NOW)
    registry = AdapterRegistry(
        adapters={"eastmoney_f10": adapter or StubNewsAdapter()},
        market_sources={"A": ("eastmoney_f10",)},
        sleep=lambda _seconds: None,
    )
    return NewsService(
        registry,
        NewsRepo(db, clock=clock),
        keywords=NewsKeywordRepo(db, clock=clock),
        hits=NewsHitRepo(db, clock=clock),
        mutes=MuteRepo(db, clock=clock),
        sender=sender,
        clock=clock,
    )


def test_the_same_item_and_keyword_alerts_only_once_and_the_rerun_is_quiet(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])

    first = subject.match("u1", [item()])
    assert (first.written, first.alerts) == (1, 1)
    assert len(sender.calls) == 1

    again = subject.match("u1", [item()])
    assert (again.written, again.alerts) == (0, 0)
    assert len(sender.calls) == 1
    assert db.row_count("news_hits") == 1


def test_two_keywords_on_one_item_leave_two_hits_and_push_one_merged_alert(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])
    subject.keywords.add("u1", "A", "减持", channel_ids=["c2"])

    summary = subject.match(
        "u1", [item(summary="公司拟回购，并披露了减持计划")]
    )
    assert (summary.written, summary.alerts) == (2, 1)
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert set(call["channel_ids"]) == {"c1", "c2"}
    assert call["rule_id"] is None
    facts = call["message"].facts
    assert "回购" in facts["keyword"] and "减持" in facts["keyword"]
    assert db.row_count("news_hits") == 2


def test_matching_ignores_case_and_reads_the_summary_too(db):
    subject = service(db)
    subject.keywords.add("u1", "A", "BUYBACK")
    summary = subject.match(
        "u1",
        [item(title="公司公告", summary="The company announced a BuyBack plan")],
    )
    assert summary.written == 1


def test_a_code_scoped_keyword_does_not_hit_another_code(db):
    subject = service(db)
    subject.keywords.add("u1", "A", "回购", code="600519")
    subject.keywords.add("u1", "A", "减持")
    summary = subject.match(
        "u1", [item(code="000001", title="平安银行关于减持的公告", summary=None)]
    )
    assert summary.written == 1  # 只有市场级的「减持」
    hit = db.query("SELECT keyword_id FROM news_hits").row(0, named=True)
    keyword = subject.keywords.get(hit["keyword_id"])
    assert keyword is not None and keyword.code is None


def test_a_keyword_added_after_ingestion_never_backfills(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.sync("A", "600519", TODAY - timedelta(days=30), TODAY)
    assert db.row_count("news_hits") == 0

    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])
    again = subject.sync("A", "600519", TODAY - timedelta(days=30), TODAY)
    assert again.written == 0
    assert db.row_count("news_hits") == 0
    assert sender.calls == []


def test_sync_matches_only_the_rows_it_just_inserted(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])

    summary = subject.sync("A", "600519", TODAY - timedelta(days=30), TODAY)
    assert summary.written == 1
    assert db.row_count("news_hits") == 1
    assert len(sender.calls) == 1


def test_a_muted_hit_is_recorded_but_never_sent(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])
    subject.mutes.add("u1", "A", "600519")

    summary = subject.match("u1", [item()])
    assert (summary.written, summary.alerts, summary.muted) == (1, 0, 1)
    assert sender.calls == []
    assert db.row_count("news_hits") == 1


def test_a_keyword_without_channels_stays_in_the_hit_list_without_pushing(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购")

    summary = subject.match("u1", [item()])
    assert (summary.written, summary.alerts) == (1, 0)
    assert sender.calls == []


def test_the_alert_carries_the_original_link_source_and_as_of(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])

    subject.match("u1", [item()])
    call = sender.calls[0]
    message = call["message"]
    assert message.rule_name == "资讯"
    assert message.stage == "hit"
    assert (message.market, message.code) == ("A", "600519")
    assert call["account_id"] is None and call["is_test"] is False
    facts = message.facts
    assert facts["kind"] == "news"
    assert facts["url"] == "https://example.test/N1"
    assert facts["title"] == "贵州茅台：关于回购股份的公告"
    assert facts["source"] == "eastmoney_f10"
    assert datetime.fromisoformat(facts["published_at"]) == STAMP
    assert datetime.fromisoformat(facts["as_of"]) == NOW


def test_a_news_mute_silences_news_alerts(db):
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])
    subject.mutes.add("u1", "A", "600519", kind="news")

    summary = subject.match("u1", [item()])

    assert (summary.alerts, summary.muted) == (0, 1)
    assert sender.calls == []


def test_a_rule_mute_does_not_silence_news_alerts(db):
    """静音分组（D249）：关掉价格规则，资讯该照推。"""
    sender = RecordingSender()
    subject = service(db, sender)
    subject.keywords.add("u1", "A", "回购", channel_ids=["c1"])
    subject.mutes.add("u1", "A", "600519", kind="rule")

    summary = subject.match("u1", [item()])

    assert (summary.alerts, summary.muted) == (1, 0)
    assert len(sender.calls) == 1
