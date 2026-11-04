"""资讯行的展示口径（S3-D）：/news 与个股卡共用一份。

`InfoItem` 只带事实；「今天/昨天/更早」、本地时刻与命中徽标是屏幕的事。
公告只有日期没有时刻：不拿 00:00 冒充时间，日期按源给的日历日直接印。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sams.core.news import InfoItem

KIND_LABELS = {"announcement": "公告", "news": "新闻"}
GROUP_TODAY = "今天"
GROUP_YESTERDAY = "昨天"
GROUP_EARLIER = "更早"


def feed_rows(
    items: Sequence[InfoItem],
    *,
    zone: ZoneInfo,
    today: date,
    names: Mapping[str, str] | None = None,
) -> list[dict]:
    lookup = names or {}
    return [
        {
            "market": item.market,
            "code": item.code,
            "name": lookup.get(item.code, item.code),
            "kind": item.kind,
            "kind_label": KIND_LABELS.get(item.kind, item.kind),
            "title": item.title,
            "url": item.url,
            "source": item.source,
            "matched": item.matched,
            "when": _when(item, zone, today),
            "day": _day(item, zone),
        }
        for item in items
    ]


def group_rows(rows: Sequence[dict], *, today: date) -> list[tuple[str, list[dict]]]:
    """按「今天 / 昨天 / 更早」分组；顺序沿用 feed 的时间倒序。"""
    buckets: list[tuple[str, list[dict]]] = []
    for row in rows:
        label = _group_label(row["day"], today)
        if not buckets or buckets[-1][0] != label:
            buckets.append((label, []))
        buckets[-1][1].append(row)
    return buckets


def _group_label(day: date, today: date) -> str:
    if day == today:
        return GROUP_TODAY
    if day == today - timedelta(days=1):
        return GROUP_YESTERDAY
    return GROUP_EARLIER


def _day(item: InfoItem, zone: ZoneInfo) -> date:
    if item.kind == "announcement":
        # 公告存的是源给的日历日（以 UTC 午夜承载）：再折时区会把东八区的日期挪错
        return item.published_at.date()
    return item.published_at.astimezone(zone).date()


def _when(item: InfoItem, zone: ZoneInfo, today: date) -> str:
    if item.kind == "announcement":
        return item.published_at.strftime("%Y-%m-%d")
    local = item.published_at.astimezone(zone)
    if local.date() == today:
        return local.strftime("%H:%M")
    return local.strftime("%m-%d %H:%M")
