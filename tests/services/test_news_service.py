"""资讯同步用例：链答复 → 幂等入库 → 把「新增几行」原样带回。

`sync` 的返回值是**新增**行数，不是这次问回几行：重跑同一晚的资讯要报 0，
否则「再跑一次看看有没有新东西」永远都会看到正数，运维就失去这个信号了。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from support import FrozenClock

from sams.core.errors import ValidationError
from sams.datasource.news import empty_news
from sams.datasource.registry import AdapterRegistry
from sams.services.news import NewsService
from sams.storage.repos.news import NewsRepo

TODAY = date(2026, 10, 31)
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
STAMP = datetime(2026, 10, 30, 10, 36, 28, tzinfo=UTC)


def news_frame(*ids: str) -> pl.DataFrame:
    rows = [
        {
            "market": "A",
            "code": "600519",
            "source": "eastmoney_f10",
            "item_id": item_id,
            "published_at": STAMP,
            "title": f"标题 {item_id}",
            "summary": None,
            "url": f"https://example.test/{item_id}",
        }
        for item_id in (ids or ("N1",))
    ]
    return pl.DataFrame(rows, schema=dict(empty_news().schema))


class StubNewsAdapter:
    name = "eastmoney_f10"
    markets = frozenset({"A"})

    def __init__(self, frame: pl.DataFrame | None = None) -> None:
        self.frame = frame if frame is not None else news_frame()
        self.calls: list[tuple[str, str, date, date]] = []

    def stock_news(self, market, code, start, end):
        self.calls.append((market, code, start, end))
        return self.frame


def service(db, adapter: StubNewsAdapter) -> NewsService:
    registry = AdapterRegistry(
        adapters={"eastmoney_f10": adapter},
        market_sources={"A": ("eastmoney_f10",)},
        sleep=lambda _seconds: None,
    )
    clock = FrozenClock(NOW)
    return NewsService(registry, NewsRepo(db, clock=clock), clock=clock)


def test_sync_writes_rows_and_a_rerun_reports_zero_new(db):
    adapter = StubNewsAdapter()
    subject = service(db, adapter)
    start = TODAY - timedelta(days=30)
    assert subject.sync("A", "600519", start, TODAY).written == 1
    again = subject.sync("A", "600519", start, TODAY)
    assert again.written == 0
    assert again.fetched == 1
    assert db.row_count("news_items") == 1


def test_empty_answer_is_a_successful_zero(db):
    subject = service(db, StubNewsAdapter(empty_news()))
    summary = subject.sync("A", "600519", TODAY, TODAY)
    assert summary.written == 0 and summary.empty is True
    assert summary.confirmed_empty_by == ("eastmoney_f10",)


def test_default_window_is_the_last_thirty_days(db):
    adapter = StubNewsAdapter()
    service(db, adapter).sync("A", "600519")
    assert adapter.calls == [("A", "600519", TODAY - timedelta(days=30), TODAY)]


def test_hk_is_refused_by_name_instead_of_an_empty_sync(db):
    subject = service(db, StubNewsAdapter())
    with pytest.raises(ValidationError, match="新闻源未接入"):
        subject.sync("HK", "00700", TODAY, TODAY)


def test_a_source_without_the_capability_is_reported_as_skipped(db):
    """两个源：第一个没有这能力，第二个给了行——跳过谁要能在 CLI/健康页看到。"""
    class NoNews:
        name = "akshare"
        markets = frozenset({"A"})

    adapter = StubNewsAdapter()
    registry = AdapterRegistry(
        adapters={"akshare": NoNews(), "eastmoney_f10": adapter},
        market_sources={"A": ("akshare", "eastmoney_f10")},
        sleep=lambda _seconds: None,
    )
    subject = NewsService(registry, NewsRepo(db, clock=FrozenClock(NOW)), clock=FrozenClock(NOW))
    summary = subject.sync("A", "600519", TODAY, TODAY)
    assert summary.written == 1
    assert summary.skipped == (("akshare", "不提供个股新闻"),)
