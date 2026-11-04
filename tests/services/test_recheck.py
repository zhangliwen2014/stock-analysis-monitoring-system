"""复检清单的筛选规则（D232 P0-2）。

这一层不碰库：页面把每只票「最近一次已披露财报」与「最近几条公告」交进来，
这里只回答「哪些落在窗口内、距今几天、按什么顺序排」。
"""

from __future__ import annotations

from datetime import date

from sams.services.recheck import recent_announcements, recent_disclosures

AS_OF = date(2026, 10, 31)


def _disclosure(published, *, market="A", code="600519", name="贵州茅台"):
    return {
        "market": market,
        "code": code,
        "name": name,
        "report_date": date(2026, 9, 30),
        "published_date": published,
    }


def _announcement(published_on, *, title="贵州茅台:2026年第三季度报告", code="600519"):
    return {
        "market": "A",
        "code": code,
        "name": "贵州茅台",
        "published_on": published_on,
        "title": title,
        "url": "https://data.eastmoney.com/notices/detail/600519/AN1.html",
    }


def test_a_disclosure_inside_the_window_keeps_its_age_in_days() -> None:
    found = recent_disclosures([_disclosure(date(2026, 10, 28))], as_of=AS_OF)
    assert [(item.code, item.days_since) for item in found] == [("600519", 3)]


def test_the_window_is_inclusive_at_both_ends() -> None:
    rows = [
        _disclosure(AS_OF, code="000001"),
        _disclosure(date(2026, 10, 1), code="000002"),
        _disclosure(date(2026, 9, 30), code="000003"),
    ]
    found = recent_disclosures(rows, as_of=AS_OF)
    assert [item.code for item in found] == ["000001", "000002"]


def test_a_publication_after_the_as_of_day_is_not_a_past_event() -> None:
    found = recent_disclosures([_disclosure(date(2026, 11, 2))], as_of=AS_OF)
    assert found == []


def test_a_row_without_a_publication_date_is_left_to_the_null_note() -> None:
    """港股/美股的披露日没入库：这里不能把它读成「没披露」。"""
    found = recent_disclosures(
        [_disclosure(None, market="HK", code="00700", name="腾讯控股")], as_of=AS_OF
    )
    assert found == []


def test_disclosures_are_newest_first() -> None:
    rows = [
        _disclosure(date(2026, 10, 20), code="000002"),
        _disclosure(date(2026, 10, 28), code="000001"),
    ]
    found = recent_disclosures(rows, as_of=AS_OF)
    assert [item.code for item in found] == ["000001", "000002"]


def test_announcements_are_filtered_by_the_same_window_and_keep_the_title() -> None:
    rows = [
        _announcement(date(2026, 10, 28)),
        _announcement(date(2026, 9, 30), title="旧公告"),
    ]
    found = recent_announcements(rows, as_of=AS_OF)
    assert [item.title for item in found] == ["贵州茅台:2026年第三季度报告"]
    assert found[0].days_since == 3
    assert found[0].url.endswith("AN1.html")


def test_an_announcement_without_a_url_still_shows_its_title() -> None:
    row = _announcement(date(2026, 10, 28))
    row["url"] = None
    found = recent_announcements([row], as_of=AS_OF)
    assert found[0].url == ""
