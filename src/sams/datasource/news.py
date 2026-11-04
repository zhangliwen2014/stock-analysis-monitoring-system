"""个股新闻的采集契约：JSONP 外壳、标准列、空答复形状、纯解析（S3-B）。

东财搜索接口返回的是 JSONP（`jQuery123({...})`），不是纯 JSON；标题与摘要里带
`<em>` 命中高亮。两件事都在这里剥掉——把高亮留在标题里，屏幕上会出现尖括号，
关键词匹配也会把「命中标记」当成正文的一部分。

发布时间是上海本地时间，落库前换算成 UTC（`news_items.published_at` 是带时区的
时间戳）：拿本地时间直接当 UTC 存，A 股晚上的新闻会落到第二天。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from sams.core.codes import canonical_code, canonical_market
from sams.core.errors import FieldDrift

logger = logging.getLogger("sams.datasource.news")

SHANGHAI = ZoneInfo("Asia/Shanghai")

NEWS_COLUMNS = (
    "market",
    "code",
    "source",
    "item_id",
    "published_at",
    "title",
    "summary",
    "url",
)
_TAG = re.compile(r"</?em>")
_STAMP = "%Y-%m-%d %H:%M:%S"


def has_stock_news(obj: Any) -> bool:
    return callable(getattr(obj, "stock_news", None))


def empty_news() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "market": pl.Utf8,
            "code": pl.Utf8,
            "source": pl.Utf8,
            "item_id": pl.Utf8,
            "published_at": pl.Datetime("us", "UTC"),
            "title": pl.Utf8,
            "summary": pl.Utf8,
            "url": pl.Utf8,
        }
    )


def parse_jsonp(text: str) -> dict:
    """`jQuery123({...})` → dict；纯 JSON 也收（部分边缘节点不包回调）。"""
    body = (text or "").strip()
    if not body:
        raise FieldDrift("eastmoney_f10", "新闻响应为空")
    if not body.startswith("{"):
        start, end = body.find("("), body.rfind(")")
        if start < 0 or end <= start:
            raise FieldDrift("eastmoney_f10", f"新闻响应不是 JSONP: {body[:80]!r}")
        body = body[start + 1 : end]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FieldDrift("eastmoney_f10", f"新闻 JSONP 解不开: {exc}") from exc
    if not isinstance(payload, dict):
        raise FieldDrift("eastmoney_f10", f"新闻响应不是对象: {type(payload).__name__}")
    return payload


def _text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = _TAG.sub("", str(value))
    cleaned = unescape(cleaned).strip()
    return cleaned or None


def _published(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        local = datetime.strptime(str(value).strip(), _STAMP).replace(tzinfo=SHANGHAI)
    except ValueError:
        return None
    return local.astimezone(UTC)


def parse_stock_news(
    payload: object, *, market: str, code: str, source: str
) -> pl.DataFrame:
    """东财个股新闻 payload → `NEWS_COLUMNS`；空列表是 0 行答复。"""
    if not isinstance(payload, dict):
        raise FieldDrift(source, f"新闻响应不是对象: {type(payload).__name__}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise FieldDrift(source, f"{market} {code} 新闻响应缺少 result（接口可能改版）")
    articles = result.get("cmsArticleWebOld")
    if articles is None:
        raise FieldDrift(
            source, f"{market} {code} 新闻响应缺少 result.cmsArticleWebOld（接口可能改版）"
        )
    if not isinstance(articles, list):
        raise FieldDrift(source, f"{market} {code} 的 cmsArticleWebOld 不是列表")
    if not articles:
        return empty_news()

    canonical = canonical_market(market)
    canonical_request = canonical_code(canonical, code)
    rows = []
    for item in articles:
        if not isinstance(item, dict):
            logger.warning("%s %s 新闻列表混进非对象条目，丢弃", source, code)
            continue
        title = _text(item.get("title"))
        url = _text(item.get("url"))
        published = _published(item.get("date"))
        if not title or not url or published is None:
            logger.warning(
                "%s %s 丢弃一条新闻（date=%r title=%r url=%r）",
                source,
                code,
                item.get("date"),
                item.get("title"),
                item.get("url"),
            )
            continue
        rows.append(
            {
                "market": canonical,
                "code": canonical_request,
                "source": source,
                "item_id": _text(item.get("code")),
                "published_at": published,
                "title": title,
                "summary": _text(item.get("content")),
                "url": url,
            }
        )

    if not rows:
        return empty_news()
    return pl.DataFrame(rows, schema=dict(empty_news().schema)).sort(
        "published_at", descending=True
    )


def in_window(frame: pl.DataFrame, market: str, start: date, end: date) -> pl.DataFrame:
    """按市场时区裁窗口：`published_at` 是 UTC，直接和日期比会差一天。"""
    if frame.is_empty():
        return frame
    local_day = (
        pl.col("published_at")
        .dt.convert_time_zone(str(SHANGHAI))
        .dt.date()
    )
    return frame.filter(local_day.is_between(start, end))
