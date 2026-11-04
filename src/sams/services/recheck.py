"""复检清单：我的标的最近披露了什么、公告了什么（D232 P0-2）。

这一层只筛不留：页面把每只票「最近一次已披露财报」与「最近几条公告」交进来，
这里按截至日与窗口留下窗口内的行，并算好「距今几天」。取数各有各的仓库接口，
拖进来就得带着整库的脾气；窗口规则倒是只有一条，值得单独钉住。

「未来 4 周的事件」目前没有可靠数据源（财报预约日、未来除权日都没入库），
所以这里**不猜**；页面把这件事写给用户看。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

DEFAULT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Disclosure:
    market: str
    code: str
    name: str
    report_date: date
    published_date: date
    days_since: int


@dataclass(frozen=True)
class Announcement:
    market: str
    code: str
    name: str
    published_on: date
    title: str
    url: str
    days_since: int


def _age(day: date | None, as_of: date, window_days: int) -> int | None:
    """窗口是闭区间 `[as_of - window_days, as_of]`；未来日期不算「已发生」。"""
    if day is None:
        return None
    days = (as_of - day).days
    if 0 <= days <= window_days:
        return days
    return None


def recent_disclosures(
    rows: Iterable[Mapping], *, as_of: date, window_days: int = DEFAULT_WINDOW_DAYS
) -> list[Disclosure]:
    found: list[Disclosure] = []
    for row in rows:
        published = row.get("published_date")
        days = _age(published, as_of, window_days)
        if days is None:
            continue
        found.append(
            Disclosure(
                market=str(row["market"]),
                code=str(row["code"]),
                name=str(row.get("name") or row["code"]),
                report_date=row["report_date"],
                published_date=published,
                days_since=days,
            )
        )
    found.sort(key=lambda item: (item.published_date, item.market, item.code), reverse=True)
    return found


def recent_announcements(
    rows: Iterable[Mapping], *, as_of: date, window_days: int = DEFAULT_WINDOW_DAYS
) -> list[Announcement]:
    found: list[Announcement] = []
    for row in rows:
        days = _age(row.get("published_on"), as_of, window_days)
        if days is None:
            continue
        found.append(
            Announcement(
                market=str(row["market"]),
                code=str(row["code"]),
                name=str(row.get("name") or row["code"]),
                published_on=row["published_on"],
                title=str(row.get("title") or ""),
                url=str(row.get("url") or ""),
                days_since=days,
            )
        )
    found.sort(key=lambda item: (item.published_on, item.market, item.code), reverse=True)
    return found
